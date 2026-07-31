#!/usr/bin/env python3
"""Offline-first operational verification for the MeTis platform.

The default run only reads repository files and local Docker configuration.  It
never deploys, mutates a database, or contacts a service.  Pass ``--base-url``
explicitly to add an unauthenticated HTTP health probe.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import yaml
except ImportError:  # pragma: no cover - exercised by the command-line environment
    yaml = None


ROOT = Path(__file__).resolve().parents[1]
SECRET_KEY = re.compile(r"(?:PASSWORD|SECRET|TOKEN|API_KEY|PRIVATE_KEY)$", re.I)


@dataclass(frozen=True)
class Finding:
    level: str
    code: str
    detail: str


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _load_yaml(relative: str, findings: list[Finding]) -> Any:
    if yaml is None:
        findings.append(Finding("FAIL", "python.yaml", "PyYAML is not installed"))
        return None
    try:
        return yaml.safe_load(_read(relative))
    except (OSError, yaml.YAMLError) as exc:
        findings.append(Finding("FAIL", f"yaml.{relative}", f"cannot parse YAML: {exc}"))
        return None


def _contained_file(relative: str) -> bool:
    try:
        candidate = (ROOT / relative).resolve(strict=True)
        candidate.relative_to(ROOT.resolve())
        return candidate.is_file()
    except (OSError, ValueError):
        return False


def _env_map(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in service.get("envVars", []):
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            result[item["key"]] = item
    return result


def check_blueprint(findings: list[Finding]) -> None:
    data = _load_yaml("render.yaml", findings)
    if not isinstance(data, dict):
        return
    services = data.get("services")
    if not isinstance(services, list) or not services:
        findings.append(Finding("FAIL", "render.services", "render.yaml has no services"))
        return
    web = next((item for item in services if isinstance(item, dict) and item.get("type") == "web"), None)
    if web is None:
        findings.append(Finding("FAIL", "render.web", "render.yaml has no web service"))
        return
    if "env" in web:
        findings.append(Finding("FAIL", "render.deprecated-env", "use runtime instead of deprecated env"))
    elif web.get("runtime") == "docker":
        findings.append(Finding("PASS", "render.runtime", "web service uses runtime: docker"))
    else:
        findings.append(Finding("FAIL", "render.runtime", "web service must use runtime: docker"))

    dockerfile = str(web.get("dockerfilePath") or "Dockerfile")
    context = str(web.get("dockerContext") or ".")
    if _contained_file(dockerfile) and (ROOT / context).resolve().is_dir():
        findings.append(Finding("PASS", "render.docker-paths", "Dockerfile and build context resolve inside the repository"))
    else:
        findings.append(Finding("FAIL", "render.docker-paths", "Dockerfile or build context is missing/unsafe"))

    if web.get("healthCheckPath") == "/health":
        findings.append(Finding("PASS", "render.health-path", "Render probes /health"))
    else:
        findings.append(Finding("FAIL", "render.health-path", "healthCheckPath must be /health"))

    env = _env_map(web)
    database = env.get("DATABASE_URL", {})
    if database.get("sync") is False and "value" not in database:
        findings.append(Finding("PASS", "render.database-secret", "DATABASE_URL is Dashboard-managed"))
    else:
        findings.append(Finding("FAIL", "render.database-secret", "DATABASE_URL must use sync: false without a literal value"))
    require_db = str(env.get("REQUIRE_DATABASE_URL", {}).get("value", "")).lower()
    if require_db == "true":
        findings.append(Finding("PASS", "render.require-database", "production refuses SQLite fallback"))
    else:
        findings.append(Finding("FAIL", "render.require-database", "REQUIRE_DATABASE_URL must be true"))
    for key, item in env.items():
        if SECRET_KEY.search(key) and item.get("value") not in (None, ""):
            findings.append(Finding("FAIL", "render.literal-secret", f"{key} is committed as a literal value"))


def check_container_contract(findings: list[Finding]) -> None:
    try:
        dockerfile = _read("Dockerfile.render")
        nginx = _read("nginx-render.conf")
        supervisor = _read("supervisor.conf")
        startup = _read("backend/startup.sh")
        main = _read("backend/main.py")
    except OSError as exc:
        findings.append(Finding("FAIL", "container.files", f"required container file is missing: {exc}"))
        return

    if (
        "exec supervisord -n" in dockerfile
        and "daemon off;" in supervisor
    ):
        findings.append(Finding("PASS", "container.pid1", "supervisor owns the foreground container lifecycle"))
    else:
        findings.append(Finding("FAIL", "container.pid1", "container process lifecycle is not explicit"))
    if "user=www-data" in supervisor and "stopasgroup=true" in supervisor and "killasgroup=true" in supervisor:
        findings.append(Finding("PASS", "container.shutdown", "FastAPI is non-root and receives grouped shutdown"))
    else:
        findings.append(Finding("FAIL", "container.shutdown", "FastAPI must be non-root with grouped shutdown"))
    if (
        "listen ${PORT};" in nginx
        and "envsubst '$PORT'" in dockerfile
        and 'PORT="${PORT:-10000}"' in dockerfile
        and "EXPOSE 10000" in dockerfile
    ):
        findings.append(Finding("PASS", "container.port", "Nginx listener is rendered from the platform PORT"))
    else:
        findings.append(Finding("FAIL", "container.port", "Nginx listener and exposed port disagree"))
    health_block = re.search(r"location = /health \{(?P<body>.*?)\n\s*\}", nginx, re.S)
    if health_block and "proxy_pass http://127.0.0.1:8000/health" in health_block.group("body"):
        findings.append(Finding("PASS", "container.health-proxy", "/health reaches FastAPI rather than returning a synthetic Nginx success"))
    else:
        findings.append(Finding("FAIL", "container.health-proxy", "/health does not prove FastAPI readiness"))
    if '@app.get("/health"' in main and 'exec uvicorn main:app --host 0.0.0.0 --port 8000' in startup:
        findings.append(Finding("PASS", "container.backend-health", "FastAPI exposes health on the expected host and port"))
    else:
        findings.append(Finding("FAIL", "container.backend-health", "FastAPI health/start command contract is incomplete"))
    init_pos = startup.find("init_db();")
    serve_pos = startup.find("exec uvicorn")
    if 0 <= init_pos < serve_pos:
        findings.append(Finding("PASS", "database.preflight", "database initialization must succeed before FastAPI starts"))
    else:
        findings.append(Finding("FAIL", "database.preflight", "database initialization is not a startup gate"))


def _service_environment(service: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in service.get("environment", []):
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            result[key] = value
        elif isinstance(item, dict):
            result.update({str(key): str(value) for key, value in item.items()})
    return result


def check_compose(findings: list[Finding]) -> None:
    data = _load_yaml("docker-compose.yml", findings)
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        return
    services: dict[str, Any] = data["services"]
    required = {"db", "backend", "frontend", "caddy"}
    missing = sorted(required - services.keys())
    if missing:
        findings.append(Finding("FAIL", "compose.services", f"missing services: {', '.join(missing)}"))
        return
    db = services["db"]
    backend = services["backend"]
    if db.get("ports"):
        findings.append(Finding("FAIL", "compose.database-exposure", "PostgreSQL must not publish a host port by default"))
    else:
        findings.append(Finding("PASS", "compose.database-exposure", "PostgreSQL is private to the Compose network"))
    if "pg_isready" in " ".join(str(part) for part in db.get("healthcheck", {}).get("test", [])):
        findings.append(Finding("PASS", "compose.database-health", "PostgreSQL has an explicit readiness probe"))
    else:
        findings.append(Finding("FAIL", "compose.database-health", "PostgreSQL has no readiness probe"))
    dependency = backend.get("depends_on", {}).get("db", {})
    if dependency.get("condition") == "service_healthy" and backend.get("healthcheck"):
        findings.append(Finding("PASS", "compose.backend-health", "backend waits for PostgreSQL and exposes its own health state"))
    else:
        findings.append(Finding("FAIL", "compose.backend-health", "backend health dependency chain is incomplete"))
    database_url = _service_environment(backend).get("DATABASE_URL", "")
    if (
        (
            database_url.startswith("postgresql://")
            or database_url.startswith("${DATABASE_URL:-postgresql://")
        )
        and "@db:" in database_url
    ):
        findings.append(Finding("PASS", "compose.database-url", "backend uses the private PostgreSQL service"))
    else:
        findings.append(Finding("FAIL", "compose.database-url", "backend DATABASE_URL is not wired to db"))
    volumes = data.get("volumes", {})
    if "pg_data" in volumes and any("pg_data:/var/lib/postgresql/data" in str(item) for item in db.get("volumes", [])):
        findings.append(Finding("PASS", "compose.database-volume", "PostgreSQL data uses a named volume"))
    else:
        findings.append(Finding("FAIL", "compose.database-volume", "PostgreSQL has no durable named volume"))


def check_data_operations(findings: list[Finding]) -> None:
    try:
        database = _read("backend/core/database.py")
    except OSError as exc:
        findings.append(Finding("FAIL", "database.module", str(exc)))
        return
    if "conn.commit()" in database and "conn.rollback()" in database and "CREATE TABLE IF NOT EXISTS" in database:
        findings.append(Finding("PASS", "database.transaction", "startup DDL and write contexts have commit/rollback boundaries"))
    else:
        findings.append(Finding("FAIL", "database.transaction", "database transaction boundary is incomplete"))

    run_tables = ("execution_runs", "execution_run_events", "idempotency_records")
    missing = [table for table in run_tables if table not in database]
    if missing:
        findings.append(Finding("WARN", "database.run-tables", f"durable run tables are not yet declared: {', '.join(missing)}"))
    else:
        findings.append(Finding("PASS", "database.run-tables", "durable run and idempotency tables are initialized"))

    migration_markers = (
        "class Migration",
        "MIGRATIONS",
        "_run_migrations",
        "checksum mismatch",
    )
    if all(marker in database for marker in migration_markers):
        findings.append(Finding("PASS", "database.migrations", "versioned migration infrastructure is present"))
    else:
        findings.append(Finding("WARN", "database.migrations", "schema changes are idempotent DDL only; no versioned migration/rollback runner exists"))

    backup = ROOT / "scripts" / "backup_postgres.sh"
    restore = ROOT / "scripts" / "restore_postgres.sh"
    recovery = ROOT / "scripts" / "postgres_recovery_drill.py"
    if not (backup.is_file() and restore.is_file() and recovery.is_file()):
        findings.append(Finding("FAIL", "database.backup-restore", "versioned backup and restore-drill entrypoints are required"))
        return
    recovery_text = recovery.read_text(encoding="utf-8")
    required_markers = (
        "MANIFEST_SCHEMA_VERSION", "sha256", "--execute", "--target-env",
        "--confirm-target-db", "PRODUCTION_URL_ENV_NAMES",
        "DEFAULT_VERIFICATION_QUERY", "evidence_version",
    )
    missing_markers = [marker for marker in required_markers if marker not in recovery_text]
    if missing_markers:
        findings.append(Finding(
            "FAIL", "database.backup-restore",
            "restore drill is missing safety/evidence contracts: " + ", ".join(missing_markers),
        ))
    else:
        findings.append(Finding(
            "PASS", "database.backup-restore",
            "versioned artifacts, validate-only default, explicit non-production target gates, verification query and evidence are present",
        ))


def check_docker_compose_cli(findings: list[Finding]) -> None:
    docker = shutil.which("docker")
    if not docker:
        findings.append(Finding("WARN", "compose.cli", "Docker CLI unavailable; skipped docker compose config"))
        return
    env = os.environ.copy()
    env.setdefault("POSTGRES_PASSWORD", "verification-placeholder-not-a-secret")
    env.setdefault(
        "METIS_DATA_ENCRYPTION_KEY",
        "verification-placeholder-not-a-secret",
    )
    try:
        result = subprocess.run(
            [docker, "compose", "-f", str(ROOT / "docker-compose.yml"), "config", "--quiet"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        findings.append(Finding("WARN", "compose.cli", "Docker CLI did not answer within 30 seconds; YAML checks still ran"))
        return
    if result.returncode == 0:
        findings.append(Finding("PASS", "compose.cli", "docker compose config --quiet passed"))
    else:
        summary = (result.stderr or result.stdout).strip().splitlines()[-1:]
        findings.append(Finding("FAIL", "compose.cli", summary[0][:300] if summary else "docker compose config failed"))


def check_live_health(base_url: str, findings: list[Finding]) -> None:
    url = f"{base_url.rstrip('/')}/health"
    try:
        request = Request(url, method="GET", headers={"User-Agent": "metis-platform-verifier/1"})
        with urlopen(request, timeout=10) as response:  # nosec B310 - explicit operator-provided URL
            body = response.read(4096)
            status = response.status
        payload = json.loads(body.decode("utf-8"))
        if 200 <= status < 400 and payload.get("status") in {"ok", "healthy"}:
            findings.append(Finding("PASS", "live.health", f"GET {url} returned a healthy response"))
        else:
            findings.append(Finding("FAIL", "live.health", f"GET {url} returned HTTP {status} without healthy status"))
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        findings.append(Finding("FAIL", "live.health", f"GET {url} failed: {type(exc).__name__}"))


def run_checks(base_url: str | None = None, include_compose_cli: bool = True) -> list[Finding]:
    findings: list[Finding] = []
    check_blueprint(findings)
    check_container_contract(findings)
    check_compose(findings)
    check_data_operations(findings)
    if include_compose_cli:
        check_docker_compose_cli(findings)
    if base_url:
        check_live_health(base_url, findings)
    return findings


def _counts(findings: Iterable[Finding]) -> dict[str, int]:
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for finding in findings:
        counts[finding.level] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="optional service origin for GET /health (for example http://127.0.0.1:10000)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--strict-warnings", action="store_true", help="treat warnings as a non-zero result")
    parser.add_argument("--skip-compose-cli", action="store_true", help="skip docker compose config even when Docker is installed")
    args = parser.parse_args(argv)

    findings = run_checks(args.base_url, include_compose_cli=not args.skip_compose_cli)
    counts = _counts(findings)
    if args.json:
        print(json.dumps({"findings": [asdict(item) for item in findings], "summary": counts}, ensure_ascii=False, indent=2))
    else:
        for finding in findings:
            print(f"[{finding.level}] {finding.code}: {finding.detail}")
        print(f"SUMMARY pass={counts['PASS']} warn={counts['WARN']} fail={counts['FAIL']}")
    return 1 if counts["FAIL"] or (args.strict_warnings and counts["WARN"]) else 0


if __name__ == "__main__":
    sys.exit(main())
