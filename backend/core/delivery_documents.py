"""Code-owned delivery and file-responsibility documents.

The model chooses deliverable paths and content.  The runner is the only
authority that records the bytes actually committed to the workspace.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from core.database import commit_project_files, load_project_files


RESPONSIBILITY_SCHEMA = "file-responsibility/v1"
PHASE_DELIVERY_SCHEMA = "phase-delivery/v1"
RESPONSIBILITY_RELATIVE_PATH = "docs/metis/file-responsibility.json"
PHASE_DELIVERY_DIRECTORY = "docs/metis/phase-deliveries"
_GENERATED_PREFIX = "docs/metis/"
_MAX_REBASE_BYTES = 48_000
_workspace_locks: Dict[str, threading.RLock] = {}
_workspace_locks_guard = threading.Lock()
logger = logging.getLogger(__name__)


class DeliveryContentionError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        repair_context: str = "",
        retryable: bool = True,
        path: str = "",
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.repair_context = repair_context
        self.retryable = retryable
        self.path = path


def _workspace_lock(workspace: Path) -> threading.RLock:
    key = str(workspace.resolve()).casefold()
    with _workspace_locks_guard:
        return _workspace_locks.setdefault(key, threading.RLock())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalized_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    candidate = Path(raw)
    if (
        not raw
        or "\x00" in raw
        or candidate.is_absolute()
        or ".." in candidate.parts
    ):
        raise ValueError(f"Unsafe delivery path: {value!r}")
    return "/".join(candidate.parts)


def is_qc_excluded_path(value: Any) -> bool:
    """Return whether a project file is explanatory-only and outside QC scope."""
    return _normalized_path(value).casefold() == "readme.md"


def _safe_target(workspace: Path, relative: str) -> Path:
    root = workspace.resolve()
    target = root / Path(relative)
    cursor = root
    for part in Path(relative).parts:
        cursor = cursor / part
        is_junction = getattr(cursor, "is_junction", lambda: False)
        if cursor.is_symlink() or (cursor.exists() and is_junction()):
            raise ValueError(f"Delivery path crosses a link: {relative}")
    target.parent.resolve().relative_to(root)
    return target


def _load_database_document(
    records: Mapping[str, Mapping[str, Any]],
    relative_path: str,
    schema: str,
    default: Dict[str, Any],
) -> tuple[Dict[str, Any], Optional[str]]:
    record = records.get(relative_path)
    if not isinstance(record, Mapping):
        return default, None
    try:
        value = json.loads(bytes(record["content"]).decode("utf-8"))
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Invalid database delivery document: {relative_path}"
        ) from exc
    if not isinstance(value, dict) or value.get("schema_version") != schema:
        raise ValueError(
            f"Unsupported database delivery document schema: {relative_path}"
        )
    return value, str(record.get("sha256") or "") or None


def _atomic_write_pair(payloads: Iterable[tuple[Path, Dict[str, Any]]]) -> None:
    prepared = []
    replaced = []
    try:
        for path, value in payloads:
            path.parent.mkdir(parents=True, exist_ok=True)
            previous = path.read_bytes() if path.is_file() else None
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            prepared.append((path, temporary, previous))
        for path, temporary, previous in prepared:
            os.replace(temporary, path)
            replaced.append((path, previous))
    except Exception:
        for path, previous in reversed(replaced):
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                rollback = path.with_name(f".{path.name}.{uuid.uuid4().hex}.rollback")
                rollback.write_bytes(previous)
                os.replace(rollback, path)
        raise
    finally:
        for _path, temporary, _previous in prepared:
            temporary.unlink(missing_ok=True)


def _actor(
    *,
    phase_id: str,
    task_id: str,
    task_name: str,
    expert_id: str,
    agent_id: str,
    agent_role: str,
) -> Dict[str, str]:
    return {
        "phase_id": phase_id,
        "task_id": task_id,
        "task_name": task_name,
        "expert_id": expert_id,
        "agent_id": agent_id,
        "agent_role": agent_role,
    }


def _history_entry(
    revision: int,
    action: str,
    actor: Mapping[str, str],
    *,
    before_sha256: Optional[str],
    after_sha256: Optional[str],
    before_size: Optional[int],
    after_size: Optional[int],
) -> Dict[str, Any]:
    return {
        "revision": revision,
        "action": action,
        **dict(actor),
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "before_size_bytes": before_size,
        "after_size_bytes": after_size,
        "recorded_at": _now_iso(),
    }


def _assignment_by_task(phase_plan: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for assignment in phase_plan.get("assignments") or []:
        if not isinstance(assignment, Mapping):
            continue
        for task_id in assignment.get("task_ids") or []:
            result[str(task_id)] = dict(assignment)
    return result


def _planned_tasks(
    phase_plan: Mapping[str, Any],
    existing: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:
    assignments = _assignment_by_task(phase_plan)
    tasks: Dict[str, Dict[str, Any]] = {}
    for raw in phase_plan.get("tasks") or []:
        if not isinstance(raw, Mapping):
            continue
        task_id = str(raw.get("task_id") or "")
        if not task_id:
            continue
        prior = existing.get(task_id) if isinstance(existing.get(task_id), Mapping) else {}
        tasks[task_id] = {
            "task_id": task_id,
            "name": str(raw.get("name") or ""),
            "objective": str(raw.get("objective") or ""),
            "functional_details": list(raw.get("functional_details") or []),
            "implementation": str(raw.get("implementation") or ""),
            "dependencies": list(raw.get("dependencies") or []),
            "acceptance_criteria": list(raw.get("acceptance_criteria") or []),
            "assignment": assignments.get(task_id),
            "status": str(prior.get("status") or "pending"),
            "delivery": prior.get("delivery"),
        }
    return dict(sorted(tasks.items()))


def _safe_delivery_summary(value: Any, file_count: int) -> str:
    text = str(value or "").strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, Mapping):
            declared = parsed.get("summary")
            if isinstance(declared, str) and declared.strip():
                text = declared.strip()
            else:
                text = ""
    if not text or '"content"' in text or len(text) > 300:
        noun = "file" if file_count == 1 else "files"
        return f"Delivered {file_count} {noun}."
    return text


def record_successful_task_delivery(
    *,
    workspace: Path,
    project_id: str,
    phase_id: str,
    phase_plan: Mapping[str, Any],
    task_id: str,
    agent_id: str,
    expert_id: str,
    agent_role: str,
    summary: str,
    delivery_evidence: Mapping[str, Any],
    baseline_files: Mapping[str, bytes],
) -> Dict[str, Any]:
    """Persist Agent outputs and generated delivery documents as one DB commit."""
    workspace = Path(workspace)
    responsibility_path = workspace / RESPONSIBILITY_RELATIVE_PATH
    phase_relative_path = f"{PHASE_DELIVERY_DIRECTORY}/{phase_id}.json"
    phase_delivery_path = (
        workspace / phase_relative_path
    )
    with _workspace_lock(workspace):
        database_files = load_project_files(project_id)
        responsibility, responsibility_hash = _load_database_document(
            database_files,
            RESPONSIBILITY_RELATIVE_PATH,
            RESPONSIBILITY_SCHEMA,
            {
                "schema_version": RESPONSIBILITY_SCHEMA,
                "project_id": project_id,
                "ledger_revision": 0,
                "generated_at": _now_iso(),
                "files": {},
            },
        )
        phase_delivery, phase_delivery_hash = _load_database_document(
            database_files,
            phase_relative_path,
            PHASE_DELIVERY_SCHEMA,
            {
                "schema_version": PHASE_DELIVERY_SCHEMA,
                "project_id": project_id,
                "phase_id": phase_id,
                "summary": str(phase_plan.get("summary") or ""),
                "effective_technical_requirements": list(
                    phase_plan.get("effective_technical_requirements") or []
                ),
                "expert_pool_revision": phase_plan.get("expert_pool_revision"),
                "generated_at": _now_iso(),
                "responsibility_ledger_revision": 0,
                "tasks": {},
            },
        )
        task_name_by_id = {
            str(item.get("task_id") or ""): str(item.get("name") or "")
            for item in phase_plan.get("tasks") or []
            if isinstance(item, Mapping)
        }
        task_name = task_name_by_id.get(task_id, "")
        actor = _actor(
            phase_id=phase_id,
            task_id=task_id,
            task_name=task_name,
            expert_id=expert_id,
            agent_id=agent_id,
            agent_role=agent_role,
        )
        file_records = []
        ledger_changed = False
        files = responsibility.setdefault("files", {})
        baseline_by_key = {
            _normalized_path(path).casefold(): payload
            for path, payload in baseline_files.items()
            if not _normalized_path(path).startswith(_GENERATED_PREFIX)
        }
        output_payloads: Dict[str, bytes] = {}

        for raw in sorted(
            delivery_evidence.get("files") or [],
            key=lambda item: str(item.get("path") or "").casefold(),
        ):
            path = _normalized_path(raw.get("path"))
            if path.startswith(_GENERATED_PREFIX):
                continue
            if is_qc_excluded_path(path):
                continue
            target = _safe_target(workspace, path)
            payload = target.read_bytes()
            sha256 = hashlib.sha256(payload).hexdigest()
            size = len(payload)
            if sha256 != str(raw.get("sha256") or "") or size != int(raw.get("size")):
                raise ValueError(f"Delivery evidence no longer matches workspace file: {path}")

            before = baseline_by_key.get(path.casefold())
            entry = files.get(path)
            if not isinstance(entry, dict):
                entry = None
            output_payloads[path] = payload
            if entry is not None:
                # A task-scoped transaction snapshot can omit a dependency's
                # existing file even though the responsibility ledger already
                # tracks it. The ledger, not snapshot visibility, is the
                # authoritative previous revision for a dependent modification.
                prior_sha = str(entry.get("sha256") or "") or None
                prior_size = int(entry.get("size_bytes") or 0)
                before_sha = (
                    hashlib.sha256(before).hexdigest()
                    if before is not None else prior_sha
                )
                before_size = len(before) if before is not None else prior_size
                action = "reuse" if prior_sha == sha256 else "modify"
            else:
                before_sha = (
                    hashlib.sha256(before).hexdigest()
                    if before is not None else None
                )
                before_size = len(before) if before is not None else None
                action = "create"

            if entry is None and before is not None:
                raise DeliveryContentionError(
                    "untracked_existing_file",
                    (
                        f"{path} existed before task {task_id} but has no "
                        "file-responsibility/v1 database record."
                    ),
                    retryable=False,
                    path=path,
                )

            if entry is None:
                entry = {
                    "status": "active",
                    "current_revision": 1,
                    "sha256": sha256,
                    "size_bytes": size,
                    "created_by": dict(actor),
                    "current_responsible": dict(actor),
                    "history": [
                        _history_entry(
                            1, "create", actor,
                            before_sha256=None,
                            after_sha256=sha256,
                            before_size=None,
                            after_size=size,
                        )
                    ],
                }
                files[path] = entry
                ledger_changed = True
            elif action == "modify":
                revision = int(entry.get("current_revision") or 0) + 1
                entry.update({
                    "status": "active",
                    "current_revision": revision,
                    "sha256": sha256,
                    "size_bytes": size,
                    "current_responsible": dict(actor),
                })
                entry.setdefault("history", []).append(
                    _history_entry(
                        revision, "modify", actor,
                        before_sha256=before_sha,
                        after_sha256=sha256,
                        before_size=before_size,
                        after_size=size,
                    )
                )
                ledger_changed = True

            file_records.append({
                "path": path,
                "action": action,
                "revision": int(entry.get("current_revision") or 1),
                "sha256": sha256,
                "size_bytes": size,
            })

        if ledger_changed:
            responsibility["ledger_revision"] = (
                int(responsibility.get("ledger_revision") or 0) + 1
            )
        responsibility["project_id"] = project_id
        responsibility["generated_at"] = _now_iso()
        responsibility["files"] = dict(sorted(files.items()))

        tasks = _planned_tasks(
            phase_plan,
            phase_delivery.get("tasks") or {},
        )
        if task_id not in tasks:
            tasks[task_id] = {
                "task_id": task_id,
                "name": task_name,
                "objective": "",
                "functional_details": [],
                "implementation": "",
                "dependencies": [],
                "acceptance_criteria": [],
                "assignment": None,
                "status": "pending",
                "delivery": None,
            }
        prior_delivery = (
            tasks[task_id].get("delivery")
            if isinstance(tasks[task_id].get("delivery"), Mapping)
            else {}
        )
        merged_file_records = {
            str(item.get("path") or ""): dict(item)
            for item in prior_delivery.get("files") or []
            if isinstance(item, Mapping) and str(item.get("path") or "")
        }
        merged_file_records.update({
            str(item.get("path") or ""): dict(item)
            for item in file_records
            if str(item.get("path") or "")
        })
        tasks[task_id]["status"] = "completed"
        tasks[task_id]["delivery"] = {
            "agent_id": agent_id,
            "expert_id": expert_id,
            "agent_role": agent_role,
            "summary": _safe_delivery_summary(summary, len(file_records)),
            # A repair normally returns only failed files. Keep the task's
            # previously verified files and replace only paths delivered by
            # this attempt, otherwise a partial repair destroys the phase QA
            # snapshot and makes untouched files look stale.
            "files": [
                merged_file_records[path]
                for path in sorted(merged_file_records)
            ],
        }
        phase_delivery.update({
            "project_id": project_id,
            "phase_id": phase_id,
            "summary": str(phase_plan.get("summary") or ""),
            "effective_technical_requirements": list(
                phase_plan.get("effective_technical_requirements") or []
            ),
            "expert_pool_revision": phase_plan.get("expert_pool_revision"),
            "generated_at": _now_iso(),
            "responsibility_ledger_revision": responsibility["ledger_revision"],
            "tasks": dict(sorted(tasks.items())),
        })

        responsibility_bytes = (
            json.dumps(responsibility, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        phase_delivery_bytes = (
            json.dumps(phase_delivery, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        database_records = {
            path: {
                "content": payload,
                "kind": "agent_output",
                "phase_id": phase_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "expert_id": expert_id,
            }
            for path, payload in output_payloads.items()
        }
        database_records.update({
            RESPONSIBILITY_RELATIVE_PATH: {
                "content": responsibility_bytes,
                "kind": "file_responsibility_document",
                "phase_id": phase_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "expert_id": expert_id,
            },
            phase_relative_path: {
                "content": phase_delivery_bytes,
                "kind": "phase_delivery_document",
                "phase_id": phase_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "expert_id": expert_id,
            },
        })
        persisted = commit_project_files(
            project_id,
            database_records,
            expected_sha256={
                RESPONSIBILITY_RELATIVE_PATH: responsibility_hash,
                phase_relative_path: phase_delivery_hash,
            },
        )
        try:
            _atomic_write_pair((
                (responsibility_path, responsibility),
                (phase_delivery_path, phase_delivery),
            ))
        except OSError:
            logger.exception(
                "Database delivery commit succeeded but workspace document "
                "materialization failed [project=%s phase=%s task=%s]",
                project_id,
                phase_id,
                task_id,
            )
        return {
            "responsibility_path": responsibility_path,
            "phase_delivery_path": phase_delivery_path,
            "files": file_records,
            "responsibility_ledger_revision": responsibility["ledger_revision"],
            "database_files": persisted,
        }


def load_phase_qa_scope(
    *,
    project_id: str,
    phase_id: str,
    workspace: Optional[Path] = None,
) -> Dict[str, Any]:
    """Load and cross-check the authoritative V1 inputs for phase QA."""
    phase_path = f"{PHASE_DELIVERY_DIRECTORY}/{phase_id}.json"
    records = load_project_files(project_id)
    if (
        RESPONSIBILITY_RELATIVE_PATH not in records
        or phase_path not in records
    ):
        return {
            "available": False,
            "files": [],
            "incomplete_task_ids": [],
            "issues": [],
        }

    responsibility, _ = _load_database_document(
        records,
        RESPONSIBILITY_RELATIVE_PATH,
        RESPONSIBILITY_SCHEMA,
        {},
    )
    phase_delivery, _ = _load_database_document(
        records,
        phase_path,
        PHASE_DELIVERY_SCHEMA,
        {},
    )
    issues = []
    scoped_files: Dict[str, Dict[str, Any]] = {}
    delivered_by_path: Dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    incomplete_task_ids = []
    ledger_files = responsibility.get("files") or {}
    for task_id, task in (phase_delivery.get("tasks") or {}).items():
        if (
            not isinstance(task, Mapping)
            or task.get("status") != "completed"
            or not isinstance(task.get("delivery"), Mapping)
        ):
            incomplete_task_ids.append(str(task_id))
            continue
        delivery = task["delivery"]
        for raw in delivery.get("files") or []:
            if not isinstance(raw, Mapping):
                continue
            path = _normalized_path(raw.get("path"))
            if is_qc_excluded_path(path):
                continue
            delivered_by_path.setdefault(path, []).append((str(task_id), raw))

    for path, deliveries in delivered_by_path.items():
        ledger = ledger_files.get(path)
        if not isinstance(ledger, Mapping):
            issues.append({
                "code": "responsibility_record_missing",
                "path": path,
                "message": "Phase delivery file has no responsibility record",
            })
            continue
        responsible = ledger.get("current_responsible")
        if not isinstance(responsible, Mapping):
            issues.append({
                "code": "current_responsible_missing",
                "path": path,
                "message": "Responsibility record has no current owner",
            })
            continue
        actual_sha = str(ledger.get("sha256") or "")
        actual_revision = int(ledger.get("current_revision") or 0)
        current_delivery = next((
            (task_id, raw) for task_id, raw in deliveries
            if str(raw.get("sha256") or "") == actual_sha
            and int(raw.get("revision") or 0) == actual_revision
        ), None)
        if current_delivery is None:
            issues.append({
                "code": "phase_delivery_revision_stale",
                "path": path,
                "message": (
                    "Phase delivery revision/hash does not match the "
                    "current responsibility record"
                ),
            })
            continue
        if workspace is not None:
            target = _safe_target(Path(workspace), path)
            workspace_sha = (
                hashlib.sha256(target.read_bytes()).hexdigest()
                if target.is_file()
                else ""
            )
            if workspace_sha != actual_sha:
                issues.append({
                    "code": "workspace_file_hash_mismatch",
                    "path": path,
                    "message": (
                        "Workspace file bytes do not match the committed "
                        "responsibility record"
                    ),
                })
                continue
        task_id, _raw = current_delivery
        scoped_files[path] = {
            "path": path,
            "phase_id": str(responsible.get("phase_id") or phase_id),
            "task_id": str(responsible.get("task_id") or task_id),
            "agent_id": str(responsible.get("agent_id") or ""),
            "expert_id": str(responsible.get("expert_id") or ""),
            "agent_role": str(responsible.get("agent_role") or ""),
            "revision": actual_revision,
            "sha256": actual_sha,
            "size_bytes": int(ledger.get("size_bytes") or 0),
        }
    return {
        "available": True,
        "files": [
            scoped_files[path] for path in sorted(scoped_files)
        ],
        "incomplete_task_ids": sorted(incomplete_task_ids),
        "issues": issues,
        "responsibility_ledger_revision": int(
            responsibility.get("ledger_revision") or 0
        ),
    }


def load_final_qa_scope(
    *,
    project_id: str,
    workspace: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build the authoritative whole-project QA scope from delivery documents."""
    records = load_project_files(project_id)
    responsibility_record = records.get(RESPONSIBILITY_RELATIVE_PATH)
    phase_paths = sorted(
        path for path in records
        if path.startswith(f"{PHASE_DELIVERY_DIRECTORY}/")
        and path.endswith(".json")
    )
    if responsibility_record is None or not phase_paths:
        return {
            "available": False,
            "files": [],
            "phases": [],
            "criteria": [],
            "issues": [],
        }

    responsibility, _ = _load_database_document(
        records,
        RESPONSIBILITY_RELATIVE_PATH,
        RESPONSIBILITY_SCHEMA,
        {},
    )
    ledger_files = responsibility.get("files") or {}
    delivered: Dict[str, list[tuple[str, str, Mapping[str, Any]]]] = {}
    phases: list[Dict[str, Any]] = []
    criteria: list[Dict[str, Any]] = []
    issues: list[Dict[str, Any]] = []

    for phase_path in phase_paths:
        phase_delivery, _ = _load_database_document(
            records,
            phase_path,
            PHASE_DELIVERY_SCHEMA,
            {},
        )
        phase_id = str(phase_delivery.get("phase_id") or "")
        if str(phase_delivery.get("project_id") or "") != project_id or not phase_id:
            issues.append({
                "code": "phase_delivery_identity_invalid",
                "path": phase_path,
                "message": "Phase delivery document identity is invalid",
            })
            continue
        phase_tasks: list[Dict[str, Any]] = []
        for task_id, raw_task in sorted((phase_delivery.get("tasks") or {}).items()):
            task = raw_task if isinstance(raw_task, Mapping) else {}
            if task.get("status") != "completed" or not isinstance(
                task.get("delivery"), Mapping
            ):
                issues.append({
                    "code": "phase_delivery_task_incomplete",
                    "path": phase_path,
                    "message": f"Phase delivery task is incomplete: {task_id}",
                })
                continue
            delivery = task["delivery"]
            task_files: list[str] = []
            for raw_file in delivery.get("files") or []:
                if not isinstance(raw_file, Mapping):
                    continue
                path = _normalized_path(raw_file.get("path"))
                task_files.append(path)
                delivered.setdefault(path, []).append(
                    (phase_id, str(task_id), raw_file)
                )
            task_criteria = [
                str(value).strip()
                for value in task.get("acceptance_criteria") or []
                if str(value).strip()
            ]
            phase_tasks.append({
                "task_id": str(task_id),
                "name": str(task.get("name") or ""),
                "objective": str(task.get("objective") or ""),
                "acceptance_criteria": task_criteria,
                "files": sorted(set(task_files)),
            })
            criteria.extend({
                "criterion": f"{phase_id}:{task_id}:{index}",
                "phase_id": phase_id,
                "task_id": str(task_id),
                "text": text,
                "files": sorted(set(task_files)),
            } for index, text in enumerate(task_criteria, 1))
        phases.append({
            "phase_id": phase_id,
            "summary": str(phase_delivery.get("summary") or ""),
            "technical_requirements": list(
                phase_delivery.get("effective_technical_requirements") or []
            ),
            "tasks": phase_tasks,
        })

    scoped_files: Dict[str, Dict[str, Any]] = {}
    root = Path(workspace).resolve() if workspace is not None else None
    for path, deliveries in delivered.items():
        ledger = ledger_files.get(path)
        if not isinstance(ledger, Mapping):
            issues.append({
                "code": "responsibility_record_missing",
                "path": path,
                "message": "Final delivery file has no responsibility record",
            })
            continue
        responsible = ledger.get("current_responsible")
        if not isinstance(responsible, Mapping):
            issues.append({
                "code": "current_responsible_missing",
                "path": path,
                "message": "Responsibility record has no current owner",
            })
            continue
        sha256 = str(ledger.get("sha256") or "")
        revision = int(ledger.get("current_revision") or 0)
        current_delivery = next((
            (phase_id, task_id)
            for phase_id, task_id, raw in deliveries
            if str(raw.get("sha256") or "") == sha256
            and int(raw.get("revision") or 0) == revision
        ), None)
        if current_delivery is None:
            issues.append({
                "code": "final_delivery_revision_stale",
                "path": path,
                "message": (
                    "Final delivery revision/hash does not match the "
                    "current responsibility record"
                ),
            })
            continue
        if root is not None:
            target = _safe_target(root, path)
            workspace_sha = (
                hashlib.sha256(target.read_bytes()).hexdigest()
                if target.is_file()
                else ""
            )
            if workspace_sha != sha256:
                issues.append({
                    "code": "workspace_file_hash_mismatch",
                    "path": path,
                    "message": (
                        "Workspace file bytes do not match the committed "
                        "responsibility record"
                    ),
                })
                continue
        phase_id, task_id = current_delivery
        scoped_files[path] = {
            "path": path,
            "phase_id": str(responsible.get("phase_id") or phase_id),
            "task_id": str(responsible.get("task_id") or task_id),
            "agent_id": str(responsible.get("agent_id") or ""),
            "expert_id": str(responsible.get("expert_id") or ""),
            "agent_role": str(responsible.get("agent_role") or ""),
            "revision": revision,
            "sha256": sha256,
            "size_bytes": int(ledger.get("size_bytes") or 0),
        }

    return {
        "available": True,
        "files": [scoped_files[path] for path in sorted(scoped_files)],
        "phases": phases,
        "criteria": criteria,
        "issues": issues,
        "responsibility_ledger_revision": int(
            responsibility.get("ledger_revision") or 0
        ),
    }


def validate_delivery_write_intents(
    *,
    workspace: Path,
    intents: Iterable[Mapping[str, Any]],
    file_registry: Mapping[str, Mapping[str, Any]],
    task_id: str,
    dependencies: Iterable[str],
    fully_exposed_paths: Iterable[str],
) -> None:
    """Reject blind or unrelated replacement before any file is written."""
    workspace = Path(workspace)
    dependency_ids = {str(value) for value in dependencies if str(value)}
    exposed = {_normalized_path(path).casefold() for path in fully_exposed_paths}
    registry = {
        _normalized_path(path).casefold(): value
        for path, value in (file_registry or {}).items()
    }
    seen = set()
    for intent in intents:
        path = _normalized_path(intent.get("path"))
        key = path.casefold()
        if key in seen:
            raise DeliveryContentionError(
                "duplicate_delivery_path",
                f"The delivery returned the same path more than once: {path}",
                path=path,
            )
        seen.add(key)
        if path.startswith(_GENERATED_PREFIX):
            raise DeliveryContentionError(
                "runner_owned_document",
                f"The execution Agent cannot modify runner-owned delivery metadata: {path}",
                retryable=False,
                path=path,
            )
        target = _safe_target(workspace, path)
        if not target.is_file():
            continue
        current = target.read_bytes()
        proposed = str(intent.get("content") or "").encode("utf-8")
        if current == proposed:
            continue
        owner = registry.get(key, {})
        # Locked phase dependencies use canonical phase-plan task IDs.  Keep
        # subproject_id only as a compatibility fallback for legacy records.
        owner_task = str(owner.get("task_id") or owner.get("subproject_id") or "")
        if owner_task and owner_task != task_id and owner_task not in dependency_ids:
            raise DeliveryContentionError(
                "unrelated_file_conflict",
                (
                    f"{path} is currently owned by task {owner_task}; task {task_id} "
                    "does not depend on it. Return a separate module/path instead of "
                    "silently replacing another task's successful delivery."
                ),
                path=path,
            )
        if key not in exposed:
            if len(current) > _MAX_REBASE_BYTES:
                raise DeliveryContentionError(
                    "existing_file_too_large_for_safe_rebase",
                    (
                        f"{path} is {len(current)} bytes and was not supplied completely "
                        "to the model; refusing a whole-file replacement."
                    ),
                    retryable=False,
                    path=path,
                )
            content = current.decode("utf-8", errors="replace")
            repair_context = (
                f"EXISTING FILE REBASE REQUIRED: {path}\n"
                f"Current sha256: {hashlib.sha256(current).hexdigest()}\n"
                "Preserve all existing behavior and return the complete updated file.\n"
                f"--- CURRENT COMPLETE CONTENT START ---\n{content}\n"
                "--- CURRENT COMPLETE CONTENT END ---"
            )
            raise DeliveryContentionError(
                "existing_file_rebase_required",
                f"{path} must be regenerated from its complete current content.",
                repair_context=repair_context,
                path=path,
            )
