"""Deterministic file-level policy for phase rebuilds.

The model may propose content, but this module decides which files are allowed
to change.  Policies are deliberately serialisable because they are persisted
with the phase and reused by retries/recovery.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from core.runtime_templates import materialize_create_mode_templates


class RebuildPolicyError(RuntimeError):
    """Raised when a rebuild attempts to violate its immutable file policy."""


def normalize_rebuild_path(value: Any) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    relative = Path(normalized)
    if not normalized or relative.is_absolute() or ".." in relative.parts:
        raise RebuildPolicyError(f"Invalid rebuild path: {value!r}")
    return normalized


def file_sha256(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _issue_id(issue: Mapping[str, Any]) -> str:
    return str(issue.get("id") or issue.get("fingerprint") or "").strip()


def _invariants(issues: Iterable[Mapping[str, Any]]) -> List[str]:
    values: List[str] = []
    for issue in issues:
        raw = issue.get("required_invariants") or issue.get("invariants") or []
        if isinstance(raw, str):
            raw = [raw]
        values.extend(str(value).strip() for value in raw if str(value).strip())
        hint = str(issue.get("fix_hint") or issue.get("suggestion") or "").strip()
        if hint:
            values.append(hint)
    return list(dict.fromkeys(values))


def classify_rebuild_files(
    workspace: Path,
    paths: Iterable[Any],
    issues: Iterable[Mapping[str, Any]],
    *,
    owner_for_path: Optional[Callable[[str], str]] = None,
    template_metadata: Optional[Mapping[str, Mapping[str, Any]]] = None,
    technology_stack: Iterable[str] = (),
    project_name: str = "generated-application",
) -> List[Dict[str, Any]]:
    """Classify contract/delivery paths as preserve, patch or create.

    Existing files with a located open issue are patch targets. Existing files
    without an issue are immutable preserve targets. Missing files are create
    targets regardless of whether QA also reported their absence.
    """
    root = Path(workspace).resolve()
    issue_map: Dict[str, List[Mapping[str, Any]]] = {}
    for issue in issues:
        if str(issue.get("status") or "open").lower() in {"fixed", "verified"}:
            continue
        raw_path = issue.get("file_path")
        if not raw_path:
            continue
        try:
            path = normalize_rebuild_path(raw_path)
        except RebuildPolicyError:
            continue
        issue_map.setdefault(path, []).append(issue)

    result: List[Dict[str, Any]] = []
    for path in sorted({normalize_rebuild_path(value) for value in paths}):
        target = (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise RebuildPolicyError(f"Rebuild path escapes workspace: {path}") from exc
        digest = file_sha256(target)
        file_issues = issue_map.get(path, [])
        mode = "create" if digest is None else ("patch" if file_issues else "preserve")
        entry: Dict[str, Any] = {
            "path": path,
            "mode": mode,
            "baseline_digest": digest,
            "issue_ids": [value for issue in file_issues if (value := _issue_id(issue))],
            "owner_type": owner_for_path(path) if owner_for_path else "backend",
            "required_invariants": _invariants(file_issues),
        }
        if mode == "create" and template_metadata and path in template_metadata:
            entry.update({
                key: value for key, value in template_metadata[path].items()
                if key in {"template", "template_version"}
            })
        result.append(entry)
    materialized = materialize_create_mode_templates(
        root,
        result,
        technology_stack=technology_stack,
        project_name=project_name,
    )
    for entry in result:
        evidence = materialized.get(entry["path"])
        if evidence:
            entry.update(evidence)
    return result


def policy_by_path(entries: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        normalize_rebuild_path(entry.get("path")): dict(entry)
        for entry in entries
        if isinstance(entry, Mapping) and entry.get("path")
    }


def assert_write_authorized(
    workspace: Path,
    entry: Mapping[str, Any],
    *,
    declared_baseline_digest: Optional[str] = None,
) -> None:
    path = normalize_rebuild_path(entry.get("path"))
    mode = str(entry.get("mode") or "").lower()
    current = file_sha256(Path(workspace) / path)
    expected = entry.get("baseline_digest")
    if mode == "preserve":
        raise RebuildPolicyError(f"Preserve file cannot be overwritten: {path}")
    if mode == "patch":
        if not expected or current != expected:
            raise RebuildPolicyError(
                f"Patch baseline digest mismatch for {path}: expected {expected}, got {current}"
            )
        if declared_baseline_digest != expected:
            raise RebuildPolicyError(
                f"Patch response baseline_digest mismatch for {path}: "
                f"expected {expected}, got {declared_baseline_digest}"
            )
    elif mode == "create":
        template_digest = entry.get("template_digest")
        if expected is not None or (
            current is not None and current != template_digest
        ):
            raise RebuildPolicyError(f"Create target already exists: {path}")
    else:
        raise RebuildPolicyError(f"Unknown rebuild mode for {path}: {mode}")


def assert_preserved_files_unchanged(
    workspace: Path, entries: Iterable[Mapping[str, Any]],
) -> None:
    for entry in entries:
        if str(entry.get("mode") or "").lower() != "preserve":
            continue
        path = normalize_rebuild_path(entry.get("path"))
        current = file_sha256(Path(workspace) / path)
        expected = entry.get("baseline_digest")
        if not expected or current != expected:
            raise RebuildPolicyError(
                f"Preserve file digest changed for {path}: expected {expected}, got {current}"
            )
