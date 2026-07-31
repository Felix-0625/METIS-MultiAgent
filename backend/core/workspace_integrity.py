"""Deterministic workspace integrity helpers for release gates."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, Mapping, Sequence, Tuple


_ALWAYS_EXCLUDED_PARTS = {
    ".git",
    ".project",
    ".pytest_cache",
    ".mypy_cache",
    ".cache",
    "__pycache__",
    "node_modules",
}
_ROOT_RUNTIME_DIRECTORIES = {
    "dist",
    "build",
    "output",
    "data",
    ".next",
    ".nuxt",
    "coverage",
    "vendor",
    "logs",
    "tmp",
    "temp",
}
_NESTED_BUILD_DIRECTORIES = {
    ".next",
    ".nuxt",
    "build",
    "coverage",
    "dist",
}
DELIVERY_MANIFEST_RULE_VERSION = "delivery-manifest-v2"

WORKSPACE_PERSIST_EXCLUDED_PARTS = {
    ".git",
    ".project",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
}
WORKSPACE_PERSIST_FILE_LIMIT = 2 * 1024 * 1024
WORKSPACE_PERSIST_PROJECT_LIMIT = 25 * 1024 * 1024


def iter_integrity_files(workspace: Path) -> Iterator[Tuple[str, Path]]:
    """Yield stable relative paths for files that define the deliverable."""
    root = workspace.resolve()
    if not root.exists() or not root.is_dir():
        return
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(
            part.casefold() in _ALWAYS_EXCLUDED_PARTS
            for part in relative.parts
        ):
            continue
        if any(
            part.casefold() in _NESTED_BUILD_DIRECTORIES
            for part in relative.parts[:-1]
        ):
            continue
        if (
            relative.parts
            and relative.parts[0].casefold() in _ROOT_RUNTIME_DIRECTORIES
        ):
            continue
        if _is_intrinsically_sensitive(relative):
            continue
        yield relative.as_posix(), path


def compute_workspace_digest(workspace: Path) -> str:
    """Hash paths and bytes so a prior QA result cannot approve new content."""
    digest = hashlib.sha256()
    file_count = 0
    for relative, path in iter_integrity_files(workspace):
        file_count += 1
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    digest.update(f"files:{file_count}".encode("ascii"))
    return digest.hexdigest()


def _canonical_required_path(raw_path: Any) -> str:
    value = str(raw_path or "").strip().replace("\\", "/")
    candidate = PurePosixPath(value)
    parts = tuple(part for part in candidate.parts if part not in {"", "."})
    if (
        not value
        or candidate.is_absolute()
        or value.startswith("/")
        or (len(value) >= 2 and value[1] == ":")
        or any(part == ".." for part in parts)
    ):
        raise ValueError(f"required delivery path is unsafe: {raw_path!r}")
    canonical = "/".join(parts)
    if not canonical:
        raise ValueError("required delivery path is empty")
    relative = Path(*parts)
    if _is_intrinsically_sensitive(relative):
        raise ValueError(f"required delivery path is sensitive: {canonical}")
    return canonical


def _normalize_required_paths(required_paths: Sequence[str] | Iterator[str]) -> list[str]:
    normalized: Dict[str, str] = {}
    for raw_path in required_paths:
        canonical = _canonical_required_path(raw_path)
        folded = canonical.casefold()
        previous = normalized.get(folded)
        if previous is not None and previous != canonical:
            raise ValueError(
                f"required delivery paths collide by case: {previous}, {canonical}"
            )
        normalized[folded] = canonical
    return sorted(normalized.values())


def build_delivery_manifest_from_files(
    files: Mapping[str, bytes],
    *,
    required_paths: Sequence[str] | Iterator[str] = (),
) -> Dict[str, Any]:
    """Build the sole canonical artifact identity from an immutable byte map.

    Callers that already captured bytes (rollback, runtime upload, archive)
    must use this helper rather than re-reading a mutable workspace.
    """
    normalized_required = _normalize_required_paths(required_paths)
    canonical_files: Dict[str, bytes] = {}
    folded_paths: Dict[str, str] = {}
    for raw_path, raw_content in files.items():
        canonical = _canonical_required_path(raw_path)
        folded = canonical.casefold()
        previous = folded_paths.get(folded)
        if previous is not None:
            raise ValueError(
                f"delivery paths collide by case: {previous}, {canonical}"
            )
        if not isinstance(raw_content, bytes):
            raise TypeError(f"delivery content must be bytes: {canonical}")
        folded_paths[folded] = canonical
        canonical_files[canonical] = raw_content

    missing = [
        required
        for required in normalized_required
        if required.casefold() not in folded_paths
    ]
    if missing:
        raise ValueError(
            "required delivery files are missing from artifact: "
            + ", ".join(missing)
        )

    entries = [
        {
            "path": path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
        for path, content in sorted(canonical_files.items())
    ]
    manifest_payload = {
        "rule_version": DELIVERY_MANIFEST_RULE_VERSION,
        "required_paths": normalized_required,
        "files": entries,
    }
    encoded = json.dumps(
        manifest_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        **manifest_payload,
        "artifact_sha256": hashlib.sha256(encoded).hexdigest(),
        "file_count": len(entries),
        "total_bytes": sum(item["size"] for item in entries),
    }


def collect_delivery_artifact(
    workspace: Path,
    *,
    required_paths: Sequence[str] | Iterator[str] = (),
) -> tuple[Dict[str, Any], Dict[str, bytes]]:
    """Read the exact non-secret bytes used by runtime and release gates.

    The manifest is intentionally independent from ``compute_workspace_digest``:
    that broader integrity digest remains useful for detecting local mutations,
    while this versioned manifest is the sole identity of the releasable artifact.
    """
    root = workspace.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("workspace is not a directory")

    files: Dict[str, bytes] = {}
    seen_paths: set[str] = set()
    normalized_required = _normalize_required_paths(required_paths)
    required_lookup = {item.casefold() for item in normalized_required}

    for required in normalized_required:
        lexical_target = root / Path(*PurePosixPath(required).parts)
        cursor = root
        for part in PurePosixPath(required).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError(
                    f"required delivery path contains a symlink: {required}"
                )
        required_target = lexical_target.resolve(strict=True)
        try:
            required_target.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"required delivery path escapes workspace: {required}"
            ) from exc
        if required_target.is_symlink() or not required_target.is_file():
            raise ValueError(
                f"required delivery path is not a regular file: {required}"
            )

    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.is_symlink():
            continue
        resolved = path.resolve(strict=True)
        try:
            relative_path = resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("workspace contains an unsafe file path") from exc
        relative = relative_path.as_posix()
        parts = relative_path.parts
        if any(part.casefold() in _ALWAYS_EXCLUDED_PARTS for part in parts):
            continue
        if (
            any(
                part.casefold() in _NESTED_BUILD_DIRECTORIES
                for part in parts[:-1]
            )
            and relative.casefold() not in required_lookup
        ):
            continue
        if (
            parts
            and parts[0].casefold() in _ROOT_RUNTIME_DIRECTORIES
            and relative.casefold() not in required_lookup
        ):
            continue
        if _is_intrinsically_sensitive(relative_path):
            continue
        collision_key = relative.casefold()
        if collision_key in seen_paths:
            raise ValueError(f"workspace contains colliding delivery paths: {relative}")
        seen_paths.add(collision_key)
        content = resolved.read_bytes()
        files[relative] = content
    manifest = build_delivery_manifest_from_files(
        files,
        required_paths=normalized_required,
    )
    return manifest, files


def compute_delivery_manifest(
    workspace: Path,
    *,
    required_paths: Sequence[str] | Iterator[str] = (),
) -> Dict[str, Any]:
    """Return the canonical, versioned identity of releasable workspace bytes."""
    manifest, _ = collect_delivery_artifact(
        workspace, required_paths=required_paths
    )
    return manifest


def runtime_artifact_matches(
    evidence: object,
    manifest: Dict[str, Any],
) -> bool:
    """Require runtime evidence to name the exact canonical delivery manifest."""
    return bool(
        runtime_acceptance_passed(evidence)
        and isinstance(evidence, dict)
        and evidence.get("artifact_sha256") == manifest.get("artifact_sha256")
        and evidence.get("artifact_manifest_rule_version")
        == manifest.get("rule_version")
    )


def runtime_acceptance_passed(evidence: object) -> bool:
    return bool(
        isinstance(evidence, dict)
        and evidence.get("passed") is True
        and evidence.get("status") == "passed"
        and (
            # Backward-compatible remote receipts use enabled=True.
            evidence.get("enabled") is True
            or (
                evidence.get("enabled") is False
                and evidence.get("mode") == "local"
                and evidence.get("source")
                == "local_deterministic_runtime"
                and bool(evidence.get("profile_sha256"))
                and bool(evidence.get("acceptance_key"))
                and bool(evidence.get("rule_version"))
            )
        )
    )


def workspace_persistence_issues(workspace: Path) -> list[str]:
    """Report files that the durable Render snapshot cannot preserve."""
    root = workspace.resolve()
    if not root.exists() or not root.is_dir():
        return ["Project workspace is missing"]

    issues: list[str] = []
    total_size = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(
            part.casefold() in WORKSPACE_PERSIST_EXCLUDED_PARTS
            for part in relative.parts
        ):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            issues.append(f"{relative.as_posix()}: file metadata is unreadable")
            continue
        if size > WORKSPACE_PERSIST_FILE_LIMIT:
            issues.append(
                f"{relative.as_posix()}: file exceeds the durable snapshot limit"
            )
            continue
        total_size += size
        if total_size > WORKSPACE_PERSIST_PROJECT_LIMIT:
            issues.append("Project source exceeds the durable snapshot size limit")
            break
    return issues


def release_gate_error(
    qc_results: dict,
    workspace: Path,
    *,
    current_manifest: Dict[str, Any] | None = None,
) -> str | None:
    """Return the blocking reason for archive/sign-off release, if any."""
    persistence_issues = workspace_persistence_issues(workspace)
    if persistence_issues:
        return (
            "Project cannot be durably persisted: "
            + "; ".join(persistence_issues[:3])
        )
    persisted = qc_results.get("__whole_project__", {})
    qa_entry = persisted.get("qa", persisted) if isinstance(persisted, dict) else {}
    if not isinstance(qa_entry, dict) or qa_entry.get("passed") is not True:
        return "Final QA has not passed for this project"
    if qa_entry.get("status") != "passed":
        return "Final QA is not in a passed terminal state"
    expected_manifest = qa_entry.get("delivery_manifest")
    if not isinstance(expected_manifest, dict):
        return "Final QA result is missing canonical delivery artifact evidence"
    if current_manifest is None:
        try:
            current_manifest = compute_delivery_manifest(
                workspace,
                required_paths=expected_manifest.get("required_paths") or (),
            )
        except (OSError, ValueError):
            return "Project delivery artifact cannot be read safely"
    if (
        expected_manifest.get("rule_version") != DELIVERY_MANIFEST_RULE_VERSION
        or expected_manifest.get("artifact_sha256")
        != current_manifest.get("artifact_sha256")
    ):
        return "Project files changed after Final QA; run Final QA again"
    if not runtime_artifact_matches(
        qa_entry.get("runtime_acceptance"), current_manifest
    ):
        return "Runtime acceptance has not passed for the current project files"
    return None


def _is_intrinsically_sensitive(relative: Path) -> bool:
    lowered_parts = [part.casefold() for part in relative.parts]
    if any(part.casefold() in _ALWAYS_EXCLUDED_PARTS for part in relative.parts):
        return True
    name = relative.name.casefold()
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    if name in {"id_rsa", "id_ed25519", "secrets.json", "credentials.json"}:
        return True
    if name.endswith((".pem", ".key", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3")):
        return True
    return any(part in {"logs", "tmp", "temp"} for part in lowered_parts)


def is_sensitive_archive_path(relative: Path) -> bool:
    """Keep secrets, databases, generated roots and runtime state out of ZIPs."""
    return bool(
        _is_intrinsically_sensitive(relative)
        or (
            relative.parts
            and relative.parts[0].casefold() in _ROOT_RUNTIME_DIRECTORIES
        )
    )
