#!/usr/bin/env python3
"""Create and safely validate/version PostgreSQL recovery artifacts.

Restore is validate-only by default.  An actual restore requires ``--execute``,
an operator-supplied target environment variable, and an exact database-name
confirmation.  Known production URLs are always rejected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlsplit


MANIFEST_SCHEMA_VERSION = 1
ARTIFACT_FORMAT = "postgresql-custom"
SAFE_TARGET_MARKERS = ("restore", "drill", "test", "tmp", "temporary")
PRODUCTION_URL_ENV_NAMES = (
    "DATABASE_URL",
    "PRODUCTION_DATABASE_URL",
    "RENDER_EXTERNAL_DATABASE_URL",
)
DEFAULT_VERIFICATION_QUERY = (
    "SELECT current_database() AS database, current_user AS role, "
    "current_setting('server_version') AS server_version"
)


class RecoveryError(RuntimeError):
    """A fail-closed backup or restore validation error."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_identity(url: str) -> dict[str, Any]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise RecoveryError("database URL must use postgres:// or postgresql://")
    database = unquote(parsed.path.lstrip("/").split("/", 1)[0])
    if not database:
        raise RecoveryError("database URL must include a database name")
    return {
        "scheme": "postgresql",
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": database,
    }


def _pg_environment(url: str) -> dict[str, str]:
    parsed = urlsplit(url)
    identity = _database_identity(url)
    result = {
        "PGHOST": str(identity["host"]),
        "PGPORT": str(identity["port"]),
        "PGDATABASE": str(identity["database"]),
    }
    if parsed.username:
        result["PGUSER"] = unquote(parsed.username)
    if parsed.password:
        result["PGPASSWORD"] = unquote(parsed.password)
    return result


def _safe_command(command: Sequence[str]) -> list[str]:
    """Return evidence-safe argv; connection URLs are supplied via PGDATABASE."""
    return [str(part) for part in command]


def _scrub(text: str, secrets: Sequence[str] = ()) -> str:
    result = str(text or "")
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED_DATABASE_URL]")
    result = re.sub(r"postgres(?:ql)?://[^\s]+", "[REDACTED_DATABASE_URL]", result)
    return result[:8000]


def _run(
    command: Sequence[str],
    *,
    database_url: str | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    env = os.environ.copy()
    if database_url:
        env.update(_pg_environment(database_url))
    result = subprocess.run(
        list(command),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    evidence = {
        "command": _safe_command(command),
        "exit_code": result.returncode,
        "stdout": _scrub(result.stdout, [database_url or ""]),
        "stderr": _scrub(result.stderr, [database_url or ""]),
    }
    if result.returncode != 0:
        raise RecoveryError(
            f"command failed ({result.returncode}): {' '.join(_safe_command(command))}: "
            f"{evidence['stderr'] or evidence['stdout']}"
        )
    return evidence


def create_backup(
    source_url: str,
    output_dir: Path,
    *,
    label: str = "scheduled",
    pg_dump: str = "pg_dump",
    pg_restore: str = "pg_restore",
) -> tuple[Path, Path, dict[str, Any]]:
    identity = _database_identity(source_url)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,48}", label):
        raise RecoveryError("backup label contains unsafe characters")
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact = output_dir / f"metis-postgres-v{MANIFEST_SCHEMA_VERSION}-{stamp}-{label}.dump"
    dump_evidence = _run(
        [pg_dump, "--format=custom", "--no-owner", "--no-privileges", "--file", str(artifact)],
        database_url=source_url,
    )
    if not artifact.is_file() or artifact.stat().st_size <= 0:
        raise RecoveryError("pg_dump completed without a non-empty artifact")
    list_evidence = _run([pg_restore, "--list", str(artifact)])
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "kind": "metis-postgres-backup",
        "artifact_format": ARTIFACT_FORMAT,
        "artifact_file": artifact.name,
        "artifact_size": artifact.stat().st_size,
        "sha256": _sha256(artifact),
        "created_at": _utc_now(),
        "source": identity,
        "validation": {"pg_restore_list_exit_code": list_evidence["exit_code"]},
        "evidence": {"pg_dump": dump_evidence, "pg_restore_list": list_evidence},
    }
    manifest_path = artifact.with_suffix(artifact.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return artifact, manifest_path, manifest


def validate_artifact(
    artifact: Path,
    manifest_path: Path,
    *,
    pg_restore: str = "pg_restore",
) -> tuple[dict[str, Any], dict[str, Any]]:
    artifact = artifact.resolve()
    manifest_path = manifest_path.resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"cannot read backup manifest: {exc}") from exc
    required = {
        "schema_version", "kind", "artifact_format", "artifact_file",
        "artifact_size", "sha256", "created_at",
    }
    missing = sorted(required - set(manifest))
    if missing:
        raise RecoveryError("backup manifest missing fields: " + ", ".join(missing))
    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise RecoveryError(f"unsupported backup manifest schema_version: {manifest['schema_version']}")
    if manifest["kind"] != "metis-postgres-backup" or manifest["artifact_format"] != ARTIFACT_FORMAT:
        raise RecoveryError("backup manifest kind or format is unsupported")
    if manifest["artifact_file"] != artifact.name:
        raise RecoveryError("backup manifest artifact_file does not match the supplied artifact")
    if not artifact.is_file() or artifact.stat().st_size <= 0:
        raise RecoveryError("backup artifact is missing or empty")
    if int(manifest["artifact_size"]) != artifact.stat().st_size:
        raise RecoveryError("backup artifact size does not match its manifest")
    actual_hash = _sha256(artifact)
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest["sha256"])) or manifest["sha256"] != actual_hash:
        raise RecoveryError("backup artifact checksum does not match its manifest")
    structure = _run([pg_restore, "--list", str(artifact)])
    return manifest, {
        "artifact": str(artifact),
        "manifest": str(manifest_path),
        "schema_version": manifest["schema_version"],
        "sha256": actual_hash,
        "artifact_size": artifact.stat().st_size,
        "structure_validation": structure,
    }


def authorize_restore_target(
    target_url: str,
    *,
    confirm_target_db: str,
    allow_explicit_target: bool,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    environment = os.environ if environment is None else environment
    identity = _database_identity(target_url)
    if confirm_target_db != identity["database"]:
        raise RecoveryError("--confirm-target-db must exactly match the target database name")
    for name in PRODUCTION_URL_ENV_NAMES:
        production_url = str(environment.get(name) or "").strip()
        if production_url:
            try:
                production_identity = _database_identity(production_url)
            except RecoveryError:
                continue
            if all(identity[key] == production_identity[key] for key in ("host", "port", "database")):
                raise RecoveryError(f"refusing to restore the database configured by {name}")
    is_temporary = any(marker in identity["database"].lower() for marker in SAFE_TARGET_MARKERS)
    if not is_temporary and not allow_explicit_target:
        raise RecoveryError(
            "target database is not recognizably temporary; pass --allow-explicit-target "
            "with the exact --confirm-target-db acknowledgement"
        )
    return {**identity, "temporary_name": is_temporary, "explicitly_allowed": allow_explicit_target}


def _validate_query(query: str) -> str:
    normalized = query.strip().rstrip(";").strip()
    if not normalized or ";" in normalized or not re.match(r"^(SELECT|WITH)\b", normalized, re.I):
        raise RecoveryError("verification query must be one read-only SELECT or WITH statement")
    return normalized


def run_restore_drill(
    artifact: Path,
    manifest_path: Path,
    *,
    execute: bool = False,
    target_url: str | None = None,
    confirm_target_db: str = "",
    allow_explicit_target: bool = False,
    verification_query: str = DEFAULT_VERIFICATION_QUERY,
    evidence_out: Path | None = None,
    pg_restore: str = "pg_restore",
    psql: str = "psql",
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    started_at = _utc_now()
    manifest, validation = validate_artifact(artifact, manifest_path, pg_restore=pg_restore)
    evidence: dict[str, Any] = {
        "evidence_version": 1,
        "started_at": started_at,
        "completed_at": None,
        "status": "artifact_validated",
        "restore_executed": False,
        "artifact": {
            "file": manifest["artifact_file"],
            "schema_version": manifest["schema_version"],
            "sha256": validation["sha256"],
            "size": validation["artifact_size"],
        },
        "validation": validation,
        "target": None,
        "steps": [],
    }
    try:
        if execute:
            if not target_url:
                raise RecoveryError("--execute requires a target database URL from --target-env")
            target = authorize_restore_target(
                target_url,
                confirm_target_db=confirm_target_db,
                allow_explicit_target=allow_explicit_target,
                environment=environment,
            )
            query = _validate_query(verification_query)
            evidence["target"] = target
            restore_step = _run(
                [
                    pg_restore, "--clean", "--if-exists", "--exit-on-error",
                    "--no-owner", "--no-privileges", "--dbname", target["database"],
                    str(artifact.resolve()),
                ],
                database_url=target_url,
            )
            evidence["steps"].append({"name": "restore", **restore_step})
            evidence["restore_executed"] = True
            verify_step = _run(
                [psql, "-X", "--set", "ON_ERROR_STOP=1", "--tuples-only", "--command", query],
                database_url=target_url,
            )
            evidence["steps"].append({"name": "verification_query", **verify_step})
            evidence["status"] = "restore_verified"
        evidence["completed_at"] = _utc_now()
        return evidence
    except Exception as exc:
        evidence["status"] = "failed"
        evidence["error"] = _scrub(str(exc), [target_url or ""])
        raise
    finally:
        if evidence_out:
            evidence["completed_at"] = evidence["completed_at"] or _utc_now()
            evidence_out.parent.mkdir(parents=True, exist_ok=True)
            evidence_out.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup", help="create a versioned custom-format backup")
    backup.add_argument("--source-env", default="DATABASE_URL", help="environment variable containing the source URL")
    backup.add_argument("--output-dir", type=Path, required=True)
    backup.add_argument("--label", default="scheduled")
    backup.add_argument("--pg-dump", default="pg_dump")
    backup.add_argument("--pg-restore", default="pg_restore")

    restore = subparsers.add_parser("restore", help="validate an artifact; optionally restore to an approved target")
    restore.add_argument("artifact", type=Path)
    restore.add_argument("--manifest", type=Path)
    restore.add_argument("--execute", action="store_true", help="execute restore after all safety gates")
    restore.add_argument("--target-env", help="environment variable containing the restore target URL")
    restore.add_argument("--confirm-target-db", default="")
    restore.add_argument("--allow-explicit-target", action="store_true")
    restore.add_argument("--verification-query", default=DEFAULT_VERIFICATION_QUERY)
    restore.add_argument("--evidence-out", type=Path)
    restore.add_argument("--pg-restore", default="pg_restore")
    restore.add_argument("--psql", default="psql")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "backup":
            source_url = str(os.environ.get(args.source_env) or "")
            if not source_url:
                raise RecoveryError(f"source database environment variable is empty: {args.source_env}")
            artifact, manifest, _ = create_backup(
                source_url, args.output_dir, label=args.label,
                pg_dump=args.pg_dump, pg_restore=args.pg_restore,
            )
            print(json.dumps({"status": "backup_validated", "artifact": str(artifact), "manifest": str(manifest)}))
            return 0

        manifest = args.manifest or args.artifact.with_suffix(args.artifact.suffix + ".manifest.json")
        target_url = str(os.environ.get(args.target_env) or "") if args.target_env else None
        evidence = run_restore_drill(
            args.artifact,
            manifest,
            execute=args.execute,
            target_url=target_url,
            confirm_target_db=args.confirm_target_db,
            allow_explicit_target=args.allow_explicit_target,
            verification_query=args.verification_query,
            evidence_out=args.evidence_out,
            pg_restore=args.pg_restore,
            psql=args.psql,
        )
        print(json.dumps(evidence, sort_keys=True))
        return 0
    except (RecoveryError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"recovery drill failed: {_scrub(str(exc), list(os.environ.values()))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
