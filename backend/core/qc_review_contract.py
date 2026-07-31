"""Authoritative input and output contracts for semantic QC review."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence

from core.database import load_project_files
from core.delivery_documents import (
    PHASE_DELIVERY_DIRECTORY,
    RESPONSIBILITY_RELATIVE_PATH,
    is_qc_excluded_path,
    load_phase_qa_scope,
)
from core.phase_execution_contract import acceptance_criterion_contracts


PACKET_SCHEMA_VERSION = "qc-review-packet/v1"
RESULT_SCHEMA_VERSION = "qc-review-result/v1"


class QCContractError(ValueError):
    """The authoritative QC input or reviewer output is invalid."""


def _json_document(record: Mapping[str, Any], path: str) -> Dict[str, Any]:
    try:
        value = json.loads(bytes(record["content"]).decode("utf-8"))
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QCContractError(f"invalid QC source document: {path}") from exc
    if not isinstance(value, dict):
        raise QCContractError(f"QC source document must be an object: {path}")
    return value


def _safe_workspace_file(workspace: Path, raw_path: Any) -> tuple[str, Path]:
    path = str(raw_path or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    candidate = Path(path)
    if not path or candidate.is_absolute() or ".." in candidate.parts:
        raise QCContractError(f"unsafe QC file path: {raw_path!r}")
    root = workspace.resolve()
    target = (root / candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise QCContractError(f"QC file escapes workspace: {path}") from exc
    return "/".join(candidate.parts), target


def _file_payload(
    workspace: Path,
    path: str,
    *,
    metadata: Mapping[str, Any] | None = None,
    dependency: bool = False,
) -> Dict[str, Any]:
    normalized, target = _safe_workspace_file(workspace, path)
    if not target.is_file():
        raise QCContractError(f"QC file is missing: {normalized}")
    try:
        payload = target.read_bytes()
        content = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise QCContractError(f"QC file is not readable UTF-8: {normalized}") from exc
    digest = hashlib.sha256(payload).hexdigest()
    expected = str((metadata or {}).get("sha256") or "")
    if expected and digest != expected.removeprefix("sha256:"):
        raise QCContractError(f"QC file hash mismatch: {normalized}")
    return {
        "path": normalized,
        "task_id": str((metadata or {}).get("task_id") or ""),
        "agent_id": str((metadata or {}).get("agent_id") or ""),
        "agent_role": str((metadata or {}).get("agent_role") or ""),
        "revision": int((metadata or {}).get("revision") or 0),
        "sha256": digest,
        "size_bytes": len(payload),
        "content_complete": True,
        "content": content,
        "dependency": dependency,
    }


def build_qc_review_packet(
    *,
    project_id: str,
    phase_id: str,
    workspace: Path | str,
    phase: Mapping[str, Any],
    output_files: Sequence[str],
    dependency_files: Sequence[str] = (),
    qa_context: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Build one immutable QC input from runner-owned delivery documents."""
    workspace = Path(workspace)
    phase_path = f"{PHASE_DELIVERY_DIRECTORY}/{phase_id}.json"
    records = load_project_files(project_id)
    if RESPONSIBILITY_RELATIVE_PATH not in records or phase_path not in records:
        raise QCContractError("authoritative phase delivery documents are unavailable")

    responsibility = _json_document(
        records[RESPONSIBILITY_RELATIVE_PATH], RESPONSIBILITY_RELATIVE_PATH,
    )
    delivery = _json_document(records[phase_path], phase_path)
    if responsibility.get("schema_version") != "file-responsibility/v1":
        raise QCContractError("unsupported responsibility document schema")
    if delivery.get("schema_version") != "phase-delivery/v1":
        raise QCContractError("unsupported phase delivery document schema")
    if str(delivery.get("project_id") or "") != project_id:
        raise QCContractError("phase delivery project_id mismatch")
    if str(delivery.get("phase_id") or "") != phase_id:
        raise QCContractError("phase delivery phase_id mismatch")

    scope = load_phase_qa_scope(
        project_id=project_id,
        phase_id=phase_id,
        workspace=workspace,
    )
    if not scope.get("available"):
        raise QCContractError("authoritative phase QA scope is unavailable")
    if scope.get("issues") or scope.get("incomplete_task_ids"):
        raise QCContractError(
            "authoritative phase QA scope is invalid: "
            + json.dumps({
                "issues": scope.get("issues") or [],
                "incomplete_task_ids": scope.get("incomplete_task_ids") or [],
            }, ensure_ascii=False, sort_keys=True)
        )

    scope_by_path = {
        str(item.get("path") or "").replace("\\", "/"): item
        for item in scope.get("files") or []
    }
    requested = list(dict.fromkeys(
        str(path).replace("\\", "/")
        for path in output_files
        if str(path).strip() and not is_qc_excluded_path(path)
    ))
    if set(requested) != set(scope_by_path):
        raise QCContractError("QC output file set does not match authoritative delivery scope")

    mechanical_ids = {
        str(value) for value in phase.get("mechanically_passed_criterion_ids") or []
    }
    tasks = []
    criterion_ids: set[str] = set()
    for task_id, raw in sorted((delivery.get("tasks") or {}).items()):
        if not isinstance(raw, Mapping) or raw.get("status") != "completed":
            raise QCContractError(f"QC task is not completed: {task_id}")
        task_files = [
            path for path, item in scope_by_path.items()
            if str(item.get("task_id") or "") == str(task_id)
        ]
        if not task_files:
            continue
        criteria = []
        for contract in acceptance_criterion_contracts(
            str(task_id),
            raw.get("acceptance_criteria") or [],
            artifact_paths=task_files,
        ):
            criterion_id = str(contract["criterion_id"])
            if criterion_id in mechanical_ids:
                continue
            if str(contract.get("source_class") or "semantic") != "semantic":
                continue
            if criterion_id in criterion_ids:
                raise QCContractError(f"duplicate QC criterion_id: {criterion_id}")
            criterion_ids.add(criterion_id)
            criteria.append({
                **contract,
                "type": "semantic",
                "required": True,
            })
        tasks.append({
            "task_id": str(task_id),
            "name": str(raw.get("name") or ""),
            "objective": str(raw.get("objective") or ""),
            "functional_details": list(raw.get("functional_details") or []),
            "implementation": str(raw.get("implementation") or ""),
            "dependencies": [str(value) for value in raw.get("dependencies") or []],
            "acceptance_criteria": criteria,
            "owned_files": sorted(task_files),
        })

    phase_criteria = []
    for contract in acceptance_criterion_contracts(
        phase_id,
        phase.get("acceptance_criteria") or [],
        artifact_paths=sorted(scope_by_path),
    ):
        criterion_id = str(contract["criterion_id"])
        if criterion_id in mechanical_ids:
            continue
        if str(contract.get("source_class") or "semantic") != "semantic":
            continue
        if criterion_id in criterion_ids:
            raise QCContractError(f"duplicate QC criterion_id: {criterion_id}")
        criterion_ids.add(criterion_id)
        phase_criteria.append({
            **contract,
            "type": "semantic",
            "required": True,
        })
    if phase_criteria:
        tasks.append({
            "task_id": phase_id,
            "name": str(phase.get("name") or "Phase acceptance"),
            "objective": str(phase.get("objective") or phase.get("description") or ""),
            "functional_details": list(
                phase.get("functional_details") or phase.get("work_items") or []
            ),
            "implementation": "",
            "dependencies": [str(value) for value in phase.get("dependencies") or []],
            "acceptance_criteria": phase_criteria,
            "owned_files": sorted(scope_by_path),
        })

    files = [
        _file_payload(workspace, path, metadata=scope_by_path[path])
        for path in sorted(scope_by_path)
    ]
    dependency_payloads = []
    for path in sorted(set(str(value) for value in dependency_files if str(value))):
        normalized = path.replace("\\", "/")
        if normalized in scope_by_path:
            continue
        dependency_payloads.append(
            _file_payload(workspace, normalized, dependency=True)
        )

    context = dict(qa_context or {})
    artifact_digest = str(context.get("artifact_digest") or "")
    if not artifact_digest:
        raise QCContractError("QC artifact_digest is required")
    pre_qa = dict(phase.get("pre_qa_result") or {})
    if pre_qa and pre_qa.get("passed") is not True:
        raise QCContractError("QC cannot start before Pre-QA passes")

    return {
        "schema_version": PACKET_SCHEMA_VERSION,
        "project": {
            "project_id": project_id,
            "phase_id": phase_id,
            "execution_generation": str(phase.get("execution_generation") or ""),
            "artifact_digest": artifact_digest,
            "qa_round_id": str(context.get("qa_round_id") or ""),
        },
        "phase": {
            "name": str(phase.get("name") or ""),
            "objective": str(phase.get("objective") or phase.get("description") or ""),
            "functional_details": list(
                phase.get("functional_details") or phase.get("work_items") or []
            ),
            "technical_requirements": list(
                delivery.get("effective_technical_requirements") or []
            ),
        },
        "tasks": tasks,
        "files": files,
        "dependency_files": dependency_payloads,
        "pre_qa": {
            "passed": pre_qa.get("passed") is True,
            "mechanically_passed_criterion_ids": sorted(mechanical_ids),
            "evidence": list(pre_qa.get("evidence") or []),
        },
        "responsibility": {
            "ledger_revision": int(scope.get("responsibility_ledger_revision") or 0),
        },
        "review_policy": {
            "blocking_severities": ["critical", "error"],
            "allow_unlocated_blocker": False,
            "request_more_context": True,
        },
    }


def packet_acceptance_contracts(packet: Mapping[str, Any]) -> list[Dict[str, Any]]:
    return [
        {
            **dict(criterion),
            "task_id": str(task.get("task_id") or ""),
        }
        for task in packet.get("tasks") or []
        for criterion in task.get("acceptance_criteria") or []
    ]


def validate_qc_review_result(
    packet: Mapping[str, Any],
    *,
    acceptance_observations: Iterable[Mapping[str, Any]],
    issues: Iterable[Mapping[str, Any]],
) -> None:
    """Fail closed when semantic QC output is not bound to its input packet."""
    contracts = packet_acceptance_contracts(packet)
    expected = {str(item.get("criterion_id") or "") for item in contracts}
    observations = list(acceptance_observations)
    actual = [str(item.get("criterion_id") or "") for item in observations]
    if len(actual) != len(set(actual)) or not set(actual).issubset(expected):
        raise QCContractError("QC result contains invalid semantic criterion observations")
    allowed_files = {
        str(item.get("path") or "")
        for key in ("files", "dependency_files")
        for item in packet.get(key) or []
    }
    for observation in observations:
        observed = [str(path) for path in observation.get("observed_files") or []]
        if not str(observation.get("observation") or "").strip():
            raise QCContractError("QC criterion observation is empty")
        if observation.get("passed") is True and not observed:
            raise QCContractError("passed QC criterion has no observed file")
        if not set(observed).issubset(allowed_files):
            raise QCContractError("QC criterion references a file outside the packet")
    for issue in issues:
        severity = str(issue.get("severity") or "warning").lower()
        if severity not in {"error", "critical"}:
            continue
        path = str(issue.get("file") or issue.get("file_path") or "")
        if path not in allowed_files:
            raise QCContractError("blocking QC issue is not bound to a packet file")
        if not str(issue.get("message") or "").strip():
            raise QCContractError("blocking QC issue has no concrete message")
        if not (issue.get("line") or str(issue.get("symbol") or "").strip()):
            raise QCContractError("blocking QC issue has no line or symbol")
        for field in ("expected", "actual", "evidence", "fix_hint"):
            if not str(issue.get(field) or "").strip():
                raise QCContractError(
                    f"blocking QC issue is missing structured {field}"
                )


__all__ = [
    "PACKET_SCHEMA_VERSION",
    "RESULT_SCHEMA_VERSION",
    "QCContractError",
    "build_qc_review_packet",
    "packet_acceptance_contracts",
    "validate_qc_review_result",
]
