"""项目调整路由"""
import asyncio
import base64
import copy
import hashlib
import json
import os
import re
import shutil
import threading
import time
import logging
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from typing import Optional, List, Dict, Any, Tuple
from pydantic import BaseModel
from core.app_state import (
    app, projects, hermes_client, global_sm_agent, gitee_sync,
    config_loader, agents_api_config, DEFAULT_API_CONFIG,
    global_pm_team, global_supervisor_team, global_ccb_agent,
    _get_project, _get_hermes, _get_idea_landing,
    _persist_all, _persist_all_async, _persist_idea_landing,
    _pm_teams, _phase_managers, _supervisor_leaders,
    ProjectContext, WORKSPACE_ROOT, _project_workspace,
    repair_registry, DefectStatus, ArbiterDecision,
    MCPToolHandler, handle_mcp_request, TOOL_DEFINITIONS,
    HybridMemory, PhaseManager, GiteeSync,
    PMLeaderAgent, PMMemberAgent, SupervisorLeaderAgent,
    ExecutionAgent, IdeaLandingAgent, logger,
)
from models.schemas import (
    ProjectRequest, AnalyzeRequest,
    EmployeeCreateRequest, EmployeeUpdateRequest, ExpertCreateRequest,
    ExpertUpdateRequest, ExpertMemoryRequest, ExpertMatchRequest,
    AdjustmentChatRequest, AdjustmentConfirmRequest,
    AdjustmentPhaseConfirmRequest, EngineerRepairChatRequest,
    EngineerApplyFixRequest, EngineerManualRequest,
    EngineerQAChatRequest, EngineerQAInspectRequest,
    ExpertTrainingRequest, ExpertWorkModeRequest, ExpertConfigRequest,
    ExpertChatTrainRequest, ExpertFeedbackRequest, ExpertKnowledgeRequest,
    CCBCheckDeleteMemberRequest, CCBCheckDeleteExpertRequest,
    CCBConfirmDeleteRequest, InjectQCRequest, ProjectTeamAssignRequest,
)
router = APIRouter(tags=["adjustments"])

# 导入工程师路由中的共享函数和内存存储
from api.routes_engineer import _get_adjustments
from api.routes_supervisor import _run_qc_for_subproject
from core.delivery_contract import collect_required_file_paths, is_delivery_file_path
from core.delivery_documents import load_final_qa_scope
from core.issue_ledger import canonical_defect_id
from core.json_utils import extract_first_json_object
from core.workspace_integrity import (
    collect_delivery_artifact,
    compute_delivery_manifest,
    compute_workspace_digest,
    runtime_artifact_matches,
    runtime_acceptance_passed,
)
from core.project_write_fence import (
    ProjectWriteFenceConflict,
    acquire_project_write_fence,
    get_project_write_fence,
    project_write_guard,
    release_project_write_fence,
    renew_project_write_fence,
    revoke_project_write_fence_generation,
)
from core import expert_lock
from core.issue_ledger import mark_needs_manual
from core.supervisor_quality_state import MAX_BUSINESS_QA_ROUNDS


ADJUSTMENT_TASK_TIMEOUT_SECONDS = 120
_adjustment_tasks: Dict[str, asyncio.Task] = {}
_adjustment_run_ids: Dict[str, str] = {}
_adjustment_fence_tokens: Dict[str, str] = {}


class _FenceExecutionGuard:
    """Capability guard that makes generation checks and writes indivisible."""

    def __init__(
        self,
        project_id: str,
        workspace: Path,
        token: str,
        revoked: Optional[threading.Event] = None,
    ) -> None:
        self.project_id = str(project_id)
        self.workspace = Path(workspace)
        self.token = str(token)
        self.revoked = revoked or threading.Event()

    def __call__(self) -> None:
        if self.revoked.is_set():
            raise ProjectWriteFenceConflict("Execution generation was revoked")
        with project_write_guard(self.project_id, self.workspace, self.token):
            if self.revoked.is_set():
                raise ProjectWriteFenceConflict("Execution generation was revoked")

    @contextmanager
    def write_guard(self):
        if self.revoked.is_set():
            raise ProjectWriteFenceConflict("Execution generation was revoked")
        with project_write_guard(self.project_id, self.workspace, self.token):
            if self.revoked.is_set():
                raise ProjectWriteFenceConflict("Execution generation was revoked")
            yield

    def revoke(self) -> None:
        self.revoked.set()


def _adjustment_required_paths(ctx: ProjectContext) -> List[str]:
    phase_manager = _phase_managers.get(ctx.project_id)
    contract = (
        getattr(phase_manager, "project_contract", {}) or {}
        if phase_manager
        else {}
    )
    if contract.get("locked") and isinstance(contract.get("required_files"), list):
        return sorted({
            str(item.get("path") or "").strip().replace("\\", "/")
            for item in contract["required_files"]
            if isinstance(item, dict)
            and item.get("required", True)
            and is_delivery_file_path(str(item.get("path") or ""))
        }, key=str.casefold)
    leader = _pm_teams.get(ctx.project_id)
    final_plan = leader.final_plan if leader else None
    return collect_required_file_paths(ctx.description or "", final_plan)


def _serialize_adjustment_snapshot(
    manifest: Dict[str, Any],
    files: Dict[str, bytes],
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "manifest": manifest,
        "files": {
            relative: base64.b64encode(content).decode("ascii")
            for relative, content in files.items()
        },
    }


def _take_adjustment_snapshot(ctx: ProjectContext) -> Dict[str, Any]:
    manifest, files = collect_delivery_artifact(
        Path(ctx.workspace),
        required_paths=_adjustment_required_paths(ctx),
    )
    return _serialize_adjustment_snapshot(manifest, files)


def _adjustment_creation_evidence(ctx: ProjectContext) -> Dict[str, Any]:
    manifest = compute_delivery_manifest(
        Path(ctx.workspace),
        required_paths=_adjustment_required_paths(ctx),
    )
    return {
        "manifest": manifest,
        "workspace_digest": compute_workspace_digest(Path(ctx.workspace)),
        "captured_at": time.time(),
    }


def _adjustment_cancel_blocker(
    ctx: ProjectContext,
    adjustment: Dict[str, Any],
) -> Optional[str]:
    """Explain why cancellation cannot prove an unexecuted, unchanged baseline."""
    if adjustment.get("status") != "pending_confirm":
        return (
            "Only an unexecuted pending-confirm adjustment can be cancelled; "
            f"current status is {adjustment.get('status') or 'unknown'}"
        )
    if any(
        adjustment.get(field)
        for field in (
            "active_run",
            "pre_run_snapshot",
            "result_manifest",
            "adjustment_acceptance",
            "requires_final_qa",
        )
    ):
        return "Adjustment has execution evidence and must complete Final QA or recovery"
    creation = adjustment.get("creation_evidence")
    if not isinstance(creation, dict):
        return "Adjustment creation evidence is missing; cancellation is unsafe"
    baseline = creation.get("manifest")
    baseline_workspace_digest = str(creation.get("workspace_digest") or "")
    if not isinstance(baseline, dict) or not baseline_workspace_digest:
        return "Adjustment creation evidence is incomplete; cancellation is unsafe"
    current = compute_delivery_manifest(
        Path(ctx.workspace),
        required_paths=baseline.get("required_paths") or (),
    )
    if (
        current.get("rule_version") != baseline.get("rule_version")
        or current.get("artifact_sha256") != baseline.get("artifact_sha256")
        or compute_workspace_digest(Path(ctx.workspace))
        != baseline_workspace_digest
    ):
        return "Project workspace changed after adjustment creation"
    return None


def _decode_adjustment_snapshot(
    snapshot: object,
) -> Tuple[Dict[str, Any], Dict[str, bytes]]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise ValueError("Adjustment recovery snapshot is missing or invalid")
    manifest = snapshot.get("manifest")
    encoded_files = snapshot.get("files")
    if not isinstance(manifest, dict) or not isinstance(encoded_files, dict):
        raise ValueError("Adjustment recovery snapshot is incomplete")
    files: Dict[str, bytes] = {}
    for relative, encoded in encoded_files.items():
        normalized = PurePosixPath(str(relative))
        if (
            normalized.is_absolute()
            or ".." in normalized.parts
            or str(normalized) in {"", "."}
        ):
            raise ValueError("Adjustment recovery snapshot has an unsafe path")
        files[normalized.as_posix()] = base64.b64decode(
            str(encoded), validate=True
        )
    expected_paths = {
        str(entry.get("path") or "")
        for entry in manifest.get("files", [])
        if isinstance(entry, dict)
    }
    if expected_paths != set(files):
        raise ValueError("Adjustment recovery snapshot does not match its manifest")
    manifest_payload = {
        "rule_version": manifest.get("rule_version"),
        "required_paths": manifest.get("required_paths") or [],
        "files": manifest.get("files") or [],
    }
    encoded_manifest = json.dumps(
        manifest_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if (
        hashlib.sha256(encoded_manifest).hexdigest()
        != manifest.get("artifact_sha256")
        or manifest.get("file_count") != len(files)
        or manifest.get("total_bytes") != sum(len(content) for content in files.values())
    ):
        raise ValueError("Adjustment recovery snapshot manifest identity is corrupt")
    for entry in manifest.get("files", []):
        relative = str(entry["path"])
        content = files[relative]
        if (
            hashlib.sha256(content).hexdigest() != entry.get("sha256")
            or len(content) != entry.get("size")
        ):
            raise ValueError("Adjustment recovery snapshot bytes are corrupt")
    return manifest, files


def _safe_adjustment_target(workspace: Path, relative: str) -> Path:
    root = Path(workspace).resolve()
    target = (root / PurePosixPath(relative)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("Adjustment snapshot path escapes the workspace") from exc
    return target


def _restore_adjustment_snapshot(
    ctx: ProjectContext,
    snapshot: object,
) -> Dict[str, Any]:
    """Transactionally restore every canonical delivery byte in a snapshot."""
    manifest, desired_files = _decode_adjustment_snapshot(snapshot)
    _, current_files = collect_delivery_artifact(
        Path(ctx.workspace),
        required_paths=manifest.get("required_paths") or (),
    )
    root = Path(ctx.workspace).resolve()
    transaction_root = (
        root / ".project" / "adjustment_restore" / uuid.uuid4().hex
    ).resolve()
    metadata_root = (root / ".project" / "adjustment_restore").resolve()
    transaction_root.relative_to(metadata_root)
    transaction_root.mkdir(parents=True, exist_ok=False)
    affected = sorted(set(current_files) | set(desired_files))
    before: Dict[str, Optional[bytes]] = {
        relative: current_files.get(relative) for relative in affected
    }
    prepared: Dict[str, Path] = {}
    applied: List[str] = []
    try:
        for index, relative in enumerate(affected):
            desired = desired_files.get(relative)
            if desired is None:
                continue
            temporary = transaction_root / f"{index}.new"
            temporary.write_bytes(desired)
            prepared[relative] = temporary
        for relative in affected:
            target = _safe_adjustment_target(root, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            desired = desired_files.get(relative)
            if desired is None:
                target.unlink(missing_ok=True)
            else:
                os.replace(prepared[relative], target)
            applied.append(relative)
        restored_manifest = compute_delivery_manifest(
            root,
            required_paths=manifest.get("required_paths") or (),
        )
        if (
            restored_manifest.get("artifact_sha256")
            != manifest.get("artifact_sha256")
        ):
            raise RuntimeError(
                "Adjustment snapshot restore digest verification failed"
            )
    except Exception as apply_error:
        recovery_errors: List[str] = []
        for index, relative in enumerate(reversed(applied)):
            target = _safe_adjustment_target(root, relative)
            original = before[relative]
            try:
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    rollback = transaction_root / f"rollback-{index}.tmp"
                    rollback.write_bytes(original)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(rollback, target)
            except Exception as recovery_error:
                recovery_errors.append(
                    f"{relative}: {recovery_error.__class__.__name__}"
                )
        if recovery_errors:
            raise RuntimeError(
                "Adjustment rollback failed after restore error: "
                + "; ".join(recovery_errors[:3])
            ) from apply_error
        raise
    finally:
        shutil.rmtree(transaction_root, ignore_errors=True)
    return restored_manifest


def _invalidate_final_qa_after_adjustment(
    ctx: ProjectContext,
    adjustment_id: str,
) -> None:
    persisted = ctx.qc_results.setdefault("__whole_project__", {})
    qa_entry = persisted.setdefault("qa", {}) if isinstance(persisted, dict) else {}
    if isinstance(qa_entry, dict):
        qa_entry["passed"] = False
        qa_entry["status"] = "stale"
        qa_entry["stale_reason"] = f"adjustment:{adjustment_id}"
        qa_entry.pop("workspace_digest", None)
        qa_entry.pop("workspace_digest_algorithm", None)
        qa_entry.pop("delivery_manifest", None)
    _final_qa_status[ctx.project_id] = {
        "status": "not_started",
        "all_passed": False,
        "message": "Project changed after adjustment; authoritative Final QA is required",
    }
    ctx.status = "running"


def _build_adjustment_criterion_evidence(
    project_id: str,
    *,
    qa_entry: Dict[str, Any],
    manifest: Dict[str, Any],
    run_id: str,
    qa_generation: str,
) -> List[Dict[str, Any]]:
    """Create one typed, immutable evidence record per contract criterion."""
    artifact_sha256 = str(manifest.get("artifact_sha256") or "")
    runtime = qa_entry.get("runtime_acceptance")
    records: List[Dict[str, Any]] = []
    for adjustment in _get_adjustments(project_id):
        if adjustment.get("status") != "awaiting_final_qa":
            continue
        contract = (adjustment.get("active_run") or {}).get(
            "execution_contract"
        )
        try:
            contract = _validate_adjustment_contract(contract)
        except ValueError:
            continue
        for task in contract.get("tasks") or []:
            for criterion in task.get("criteria") or []:
                evidence_type = str(
                    criterion.get("required_evidence_type") or ""
                )
                if evidence_type in {"runner", "api"}:
                    if not runtime_artifact_matches(runtime, manifest):
                        continue
                    source = {
                        "runtime_status": runtime.get("status"),
                        "runtime_artifact_sha256": runtime.get(
                            "artifact_sha256"
                        ),
                        "checks": runtime.get("checks") or [],
                    }
                elif evidence_type == "semantic":
                    if (
                        qa_entry.get("passed") is not True
                        or qa_entry.get("status") != "passed"
                    ):
                        continue
                    source = {
                        "score": qa_entry.get("score"),
                        "observed_issues_detail": qa_entry.get(
                            "observed_issues_detail", []
                        ),
                    }
                else:
                    continue
                source_sha256 = hashlib.sha256(
                    json.dumps(
                        source,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                payload = {
                    "criterion_id": criterion.get("criterion_id"),
                    "evidence_type": evidence_type,
                    "run_id": run_id,
                    "qa_generation": qa_generation,
                    "artifact_sha256": artifact_sha256,
                    "source_sha256": source_sha256,
                }
                records.append({
                    **payload,
                    "passed": True,
                    "evidence_id": "criterion-evidence-" + hashlib.sha256(
                        json.dumps(
                            payload,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()[:16],
                    "source": source,
                })
    return records


def _pending_adjustment_acceptance(
    project_id: str,
    manifest: Dict[str, Any],
    *,
    qa_entry: Optional[Dict[str, Any]] = None,
    final_qa_status: Optional[Dict[str, Any]] = None,
    run_id: str = "",
    qa_generation: str = "",
) -> Tuple[List[str], List[Dict[str, Any]]]:
    blockers: List[str] = []
    bindings: List[Dict[str, Any]] = []
    for adjustment in _get_adjustments(project_id):
        adjustment_status = str(adjustment.get("status") or "")
        if adjustment_status in {"done", "accepted", "cancelled"}:
            continue
        if adjustment_status != "awaiting_final_qa":
            blockers.append(
                f"adjustment {adjustment.get('id')}: "
                f"{adjustment_status or 'unknown'} is not ready for Final QA"
            )
            continue
        acceptance = adjustment.get("adjustment_acceptance")
        contract = (adjustment.get("active_run") or {}).get(
            "execution_contract"
        )
        try:
            _validate_adjustment_contract(contract)
        except ValueError:
            blockers.append(
                f"adjustment {adjustment.get('id')}: invalid execution contract"
            )
            continue
        if not isinstance(acceptance, dict) or not acceptance.get("changed_paths"):
            blockers.append(
                f"adjustment {adjustment.get('id')}: missing acceptance record"
            )
            continue
        if acceptance.get("contract_sha256") != contract.get("contract_sha256"):
            blockers.append(
                f"adjustment {adjustment.get('id')}: acceptance contract mismatch"
            )
            continue
        criteria = [
            criterion
            for task in contract.get("tasks") or []
            for criterion in task.get("criteria") or []
            if isinstance(criterion, dict)
        ]
        if not criteria:
            blockers.append(
                f"adjustment {adjustment.get('id')}: acceptance criteria are missing"
            )
            continue
        artifact_sha256 = str(manifest.get("artifact_sha256") or "")
        prior_artifact = str(acceptance.get("result_artifact_sha256") or "")
        superseding_rework = next((
            item
            for item in reversed(
                list((final_qa_status or {}).get("adjustment_rework_generations") or [])
            )
            if (
                item.get("root_acceptance_artifact_sha256") == prior_artifact
                or item.get("from_artifact_sha256") == prior_artifact
            )
            and item.get("to_artifact_sha256") == artifact_sha256
        ), None)
        if prior_artifact != artifact_sha256 and not superseding_rework:
            blockers.append(
                f"adjustment {adjustment.get('id')}: stale acceptance artifact"
            )
            continue
        qa_is_authoritative = bool(
            isinstance(qa_entry, dict)
            and qa_entry.get("passed") is True
            and qa_entry.get("status") == "passed"
            and qa_entry.get("final_qa_run_id") == run_id
            and qa_entry.get("final_qa_generation") == qa_generation
            and qa_entry.get("artifact_sha256") == artifact_sha256
            and runtime_artifact_matches(
                qa_entry.get("runtime_acceptance"), manifest
            )
        )
        if not qa_is_authoritative:
            blockers.append(
                f"adjustment {adjustment.get('id')}: "
                "criterion evidence is missing or not authoritative"
            )
            continue
        available_evidence = list(
            qa_entry.get("criterion_evidence") or []
        )
        criterion_results: List[Dict[str, Any]] = []
        missing_criterion = ""
        for criterion in criteria:
            criterion_id = str(criterion.get("criterion_id") or "")
            evidence_type = str(
                criterion.get("required_evidence_type") or ""
            )
            evidence = next((
                item
                for item in available_evidence
                if (
                    isinstance(item, dict)
                    and item.get("passed") is True
                    and item.get("criterion_id") == criterion_id
                    and item.get("evidence_type") == evidence_type
                    and item.get("run_id") == run_id
                    and item.get("qa_generation") == qa_generation
                    and item.get("artifact_sha256") == artifact_sha256
                    and item.get("evidence_id")
                )
            ), None)
            if not evidence:
                missing_criterion = criterion_id
                break
            criterion_results.append({
                "criterion_id": criterion_id,
                "criterion": criterion.get("text"),
                "required_evidence_type": evidence_type,
                "result": "passed",
                "passed": True,
                "authority": "final_qa",
                "evidence": copy.deepcopy(evidence),
                "binding": {
                    "adjustment_id": adjustment.get("id"),
                    "contract_sha256": acceptance.get("contract_sha256"),
                    "artifact_sha256": artifact_sha256,
                    "criterion_id": criterion_id,
                },
            })
        if missing_criterion:
            blockers.append(
                f"adjustment {adjustment.get('id')}: criterion evidence "
                f"is missing for {missing_criterion}"
            )
            continue
        evidence_sha256 = hashlib.sha256(
            json.dumps(
                [item["evidence"] for item in criterion_results],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        previous_generations = list(acceptance.get("generations") or [])
        generation_payload = {
            "contract_sha256": acceptance.get("contract_sha256"),
            "artifact_sha256": artifact_sha256,
            "final_qa_run_id": run_id,
            "qa_generation": qa_generation,
            "evidence_sha256": evidence_sha256,
        }
        generation_id = "acceptance-" + hashlib.sha256(
            json.dumps(
                generation_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        for result in criterion_results:
            result["binding"]["acceptance_generation_id"] = generation_id
        bindings.append({
            "adjustment_id": adjustment.get("id"),
            "contract_sha256": acceptance.get("contract_sha256"),
            "artifact_sha256": artifact_sha256,
            "acceptance_criteria": [
                criterion.get("text") for criterion in criteria
            ],
            "criteria": copy.deepcopy(criteria),
            "criterion_results": criterion_results,
            "modifications": acceptance.get("modifications") or "",
            "acceptance_generation_id": generation_id,
            "supersedes": (
                previous_generations[-1].get("acceptance_generation_id")
                if previous_generations else None
            ),
            "repair_evidence": superseding_rework,
        })
    return blockers, bindings


def _adjustment_is_terminal(adjustment: Dict[str, Any]) -> bool:
    return str(adjustment.get("status") or "") in {
        "done", "accepted", "cancelled",
    }


def _adjustment_final_qa_state_blockers(project_id: str) -> List[str]:
    blockers: List[str] = []
    for adjustment in _get_adjustments(project_id):
        state = str(adjustment.get("status") or "")
        if state in {"done", "accepted", "cancelled", "awaiting_final_qa"}:
            continue
        blockers.append(
            f"adjustment {adjustment.get('id')}: {state or 'unknown'}"
        )
    return blockers


def _accept_adjustment_bindings(
    project_id: str,
    bindings: List[Dict[str, Any]],
    *,
    final_qa_run_id: str,
    artifact_sha256: str,
) -> None:
    """Atomically project accepted evidence into adjustment terminal state."""
    adjustments = _get_adjustments(project_id)
    for binding in bindings:
        adjustment = next((
            item for item in adjustments
            if item.get("id") == binding.get("adjustment_id")
        ), None)
        if not adjustment or adjustment.get("status") != "awaiting_final_qa":
            raise ValueError("Adjustment acceptance target is no longer current")
        acceptance = adjustment.get("adjustment_acceptance")
        if not isinstance(acceptance, dict):
            raise ValueError("Adjustment acceptance record is missing")
        criterion_results = list(binding.get("criterion_results") or [])
        criteria = list(binding.get("acceptance_criteria") or [])
        structured_criteria = list(binding.get("criteria") or [])
        if (
            len(criterion_results) != len(criteria)
            or len(structured_criteria) != len(criteria)
            or any(
                item.get("passed") is not True
                or not item.get("evidence")
                or not item.get("binding")
                for item in criterion_results
            )
        ):
            raise ValueError("Adjustment criterion evidence is incomplete")
        expected_ids = {
            str(item.get("criterion_id") or "")
            for item in structured_criteria
        }
        for item in criterion_results:
            criterion_id = str(item.get("criterion_id") or "")
            evidence = item.get("evidence") or {}
            item_binding = item.get("binding") or {}
            if (
                criterion_id not in expected_ids
                or evidence.get("criterion_id") != criterion_id
                or evidence.get("run_id") != final_qa_run_id
                or evidence.get("artifact_sha256") != artifact_sha256
                or item_binding.get("criterion_id") != criterion_id
                or item_binding.get("artifact_sha256") != artifact_sha256
            ):
                raise ValueError(
                    "Adjustment criterion evidence binding is stale or mismatched"
                )
        generations = acceptance.setdefault("generations", [])
        if not any(
            item.get("acceptance_generation_id")
            == binding.get("acceptance_generation_id")
            for item in generations
        ):
            generations.append(copy.deepcopy(binding))
        acceptance["status"] = "accepted"
        acceptance["final_qa_run_id"] = final_qa_run_id
        acceptance["accepted_artifact_sha256"] = artifact_sha256
        acceptance["criterion_results"] = copy.deepcopy(criterion_results)
        adjustment["status"] = "done"
        adjustment["requires_final_qa"] = False
        active_run = adjustment.get("active_run")
        if isinstance(active_run, dict):
            active_run["status"] = "accepted"
            active_run["final_qa_run_id"] = final_qa_run_id
            active_run["accepted_artifact_sha256"] = artifact_sha256


FINAL_QA_SOURCE_SUFFIXES = frozenset({
    # Web applications may be fully runnable from a single HTML file.
    '.html', '.htm', '.css', '.scss', '.sass', '.less', '.vue', '.svelte', '.astro',
    # JavaScript/TypeScript and common backend/system languages.
    '.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.mts', '.cts',
    '.py', '.java', '.go', '.rs', '.cpp', '.cc', '.cxx', '.c', '.h', '.hpp', '.cs',
    '.php', '.rb', '.swift', '.kt', '.kts', '.scala', '.dart', '.lua',
    '.ex', '.exs', '.erl', '.hrl', '.fs', '.fsx', '.vb',
    # Executable scripts and database source are implementation artifacts too.
    '.sh', '.bash', '.zsh', '.ps1', '.sql',
})
FINAL_QA_IGNORED_PARTS = frozenset({
    '.project', 'node_modules', '.git', 'dist', 'build', '__pycache__',
    '.next', '.nuxt', 'coverage', 'vendor',
})


def _scan_final_qa_source_files(workspace: Optional[Path]) -> Tuple[List[str], int]:
    """Return deliverable source paths and their total line count for final QA."""
    if not workspace or not workspace.exists():
        return [], 0

    source_files: List[str] = []
    total_lines = 0
    for full_path in workspace.rglob('*'):
        if not full_path.is_file() or full_path.suffix.lower() not in FINAL_QA_SOURCE_SUFFIXES:
            continue
        relative = full_path.relative_to(workspace)
        if any(part.lower() in FINAL_QA_IGNORED_PARTS for part in relative.parts):
            continue
        source_files.append(str(relative).replace('\\', '/'))
        try:
            content = full_path.read_text(encoding='utf-8', errors='replace')
            total_lines += len(content.splitlines())
        except OSError:
            pass
    source_files.sort()
    return source_files, total_lines

@router.get("/projects/{project_id}/adjustments")
async def list_adjustments(project_id: str):
    """获取项目所有变更工单列表"""
    _get_project(project_id)
    return {"adjustments": _get_adjustments(project_id)}

@router.post("/projects/{project_id}/adjustments/chat")
async def adjustment_team_chat(project_id: str, request: AdjustmentChatRequest):
    """
    工作群：用户 → PM分析需求 → HR拆解阶段分工 → 阶段负责人响应。
    
    返回多条「群消息」，每条有 role（pm/hr/phase_lead/system）和 content。
    前端按 role 渲染不同样式。

    流程：
    1. PM 分析需求，输出 adjustment_plan JSON
    2. HR 自动将 tasks 按 depends_on 分成顺序阶段
    3. 每个阶段的负责人（phase_lead）简短响应
    4. 如需求不明确，PM 追问用户（只返回 pm 消息，无 plan）
    """
    import re as _re, json as _json
    ctx = _get_project(project_id)
    pm_leader = _get_pm_team(project_id)
    hermes = _get_hermes(project_id)
    from core.hermes_client import Message, MessageRole

    # 构建项目背景
    project_background = ""
    if pm_leader and pm_leader.final_plan:
        project_background = str(pm_leader.final_plan)[:600]
    elif ctx.pm.context_summary:
        project_background = ctx.pm.context_summary[:300]

    # 文件树摘要
    file_summary = ""
    try:
        tree = _list_dir_recursive(ctx.workspace)
        src_node = next((n for n in tree if n["title"] == "src"), None)
        if src_node:
            src_files = []
            def _flat(nodes):
                for n in nodes:
                    if n["type"] == "file":
                        src_files.append(n["key"])
                    else:
                        _flat(n.get("children", []))
            _flat(src_node.get("children", []))
            file_summary = "src/: " + ", ".join(src_files[:15])
            if len(src_files) > 15:
                file_summary += f" ...（共 {len(src_files)} 个文件）"
    except Exception:
        pass

    # ── Step 1: PM 分析需求 ─────────────────────────────────────────────────
    pm_system = (
        "你是项目 PM 组长，负责「项目调整」群的需求分析。\n\n"
        "【规则】\n"
        "- 需求清晰：直接在回复末尾生成变更方案 JSON（```adjustment_plan ... ```）\n"
        "- 需求模糊：直接追问（只追问一次），不生成 JSON\n"
        "- 回复正文 ≤200字，言简意赅\n\n"
        "【变更方案格式】\n"
        "```adjustment_plan\n"
        "{\n"
        '  "title": "变更标题（≤20字）",\n'
        '  "description": "需求描述",\n'
        '  "impact_analysis": "影响范围（1句话）",\n'
        '  "tasks": [\n'
        '    {"task_id":"t001","expert_role":"后端工程师","description":"具体任务","files":["src/api/x.py"],"deliverables":["src/api/x.py"],"acceptance_criteria":["声明行为已通过验证"],"depends_on":[],"priority":"high"}\n'
        '  ]\n'
        "}\n"
        "```\n\n"
        f"【项目背景】\n{project_background}\n\n"
        f"【当前文件结构】\n{file_summary or '暂无'}"
    )

    history = [
        {"role": m.get("role"), "content": m.get("content", "")}
        for m in (request.history or [])[-10:]
        if m.get("role") in ("user", "assistant", "pm", "hr", "phase_lead") and m.get("content")
    ]
    # 把非标准 role 统一映射为 user/assistant（LLM 只识别这两种）
    llm_history = []
    for h in history:
        r = h["role"]
        llm_role = MessageRole.USER if r == "user" else MessageRole.ASSISTANT
        llm_history.append(Message(role=llm_role, content=h["content"]))

    pm_messages = [Message(role=MessageRole.SYSTEM, content=pm_system)] + llm_history
    pm_messages.append(Message(role=MessageRole.USER, content=request.message))

    try:
        pm_resp = hermes.chat(pm_messages)
        pm_reply = pm_resp.get("content", "")
    except Exception as e:
        pm_reply = f"PM 暂时无法响应：{e}"

    # 健壮解析：直接提取首个 JSON 对象，不再依赖 ```adjustment_plan 围栏标记
    adjustment_plan = extract_first_json_object(pm_reply)

    # PM 没有生成方案（追问或出错）→ 直接返回 PM 消息
    pm_text = pm_reply.replace(r'```adjustment_plan[\s\S]*?```', '').strip()
    pm_text = _re.sub(r'```adjustment_plan[\s\S]*?```', '', pm_reply).strip()

    if not adjustment_plan:
        return {
            "messages": [{"role": "pm", "content": pm_text}],
            "has_plan": False,
            "adjustment_plan": None,
        }

    # ── Step 2: HR 将 tasks 拆成阶段 ────────────────────────────────────────
    tasks = adjustment_plan.get("tasks", [])
    # 按 depends_on 拓扑分层（同一层可并行）
    def _topo_layers(tasks: List[Dict]) -> List[List[Dict]]:
        remaining = list(tasks)
        done_ids: set = set()
        layers = []
        limit = len(tasks) + 2
        while remaining and limit > 0:
            limit -= 1
            layer = [t for t in remaining if all(d in done_ids for d in t.get("depends_on", []))]
            if not layer:
                cycle_ids = [t.get("task_id", "?") for t in remaining]
                raise ValueError(
                    f"检测到循环依赖或缺失依赖，无法拓扑排序。"
                    f"剩余任务: {cycle_ids}"
                )
            layers.append(layer)
            for t in layer:
                done_ids.add(t["task_id"])
                remaining.remove(t)
        return layers

    phases = _topo_layers(tasks)

    # 生成 HR 分工消息
    hr_lines = [f"收到 PM 方案，共 {len(phases)} 个执行阶段，任务分配如下：\n"]
    for i, phase_tasks in enumerate(phases):
        hr_lines.append(f"【阶段 {i+1}】{'（首阶段，立即开始）' if i == 0 else f'（等待阶段{i}完成后开始）'}")
        for t in phase_tasks:
            files_str = "、".join(t.get("files", [])[:2]) or "待定"
            hr_lines.append(f"  • {t['expert_role']}：{t['description'][:50]}（涉及文件：{files_str}）")
    hr_text = "\n".join(hr_lines)

    # ── Step 3: 各阶段负责人响应 ─────────────────────────────────────────────
    phase_lead_msgs = []
    for i, phase_tasks in enumerate(phases):
        roles = list({t["expert_role"] for t in phase_tasks})
        roles_str = "、".join(roles[:3])
        if i == 0:
            lead_text = f"阶段{i+1}负责人收到，{roles_str} 已就位，等待确认后立即开始执行。"
        else:
            lead_text = f"阶段{i+1}负责人收到，{roles_str} 就绪，等待前序阶段完成后接手。"
        phase_lead_msgs.append({"role": "phase_lead", "content": lead_text, "phase_index": i})

    # ── 创建变更工单（阶段化） ────────────────────────────────────────────────
    adj_id = f"adj-{uuid.uuid4().hex[:6]}"
    # 为每个 task 初始化状态
    for t in tasks:
        t.setdefault("status", "pending")
        t.setdefault("agent_id", "")
        t.setdefault("output_files", [])

    adjustment = {
        "id": adj_id,
        "title": adjustment_plan.get("title", "变更工单"),
        "description": adjustment_plan.get("description", request.message),
        "impact_analysis": adjustment_plan.get("impact_analysis", ""),
        "tasks": tasks,
        "phases": [{"phase_index": i, "tasks": [t["task_id"] for t in pt], "status": "pending"} for i, pt in enumerate(phases)],
        "current_phase_index": -1,      # -1 = 尚未开始，等待用户确认分工
        "status": "pending_confirm",
        "created_at": time.time(),
        "updated_at": time.time(),
        "exec_log": [],
        "qc_results": {},               # phase_index → qc_result
        "snapshot_version": None,
    }
    try:
        with project_write_guard(project_id, Path(ctx.workspace)):
            adjustment["creation_evidence"] = _adjustment_creation_evidence(ctx)
            _get_adjustments(project_id).append(adjustment)
    except ProjectWriteFenceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _persist_all_async()
    adjustment_plan["adjustment_id"] = adj_id

    # 组合所有群消息
    group_messages = [
        {"role": "pm", "content": pm_text},
        {"role": "hr", "content": hr_text},
        *phase_lead_msgs,
        {"role": "system", "content": f"📋 分工方案已生成，请确认后开始执行第一阶段（共 {len(phases)} 个阶段）", "adj_id": adj_id},
    ]

    return {
        "messages": group_messages,
        "has_plan": True,
        "adjustment_plan": {**adjustment_plan, "phases": adjustment["phases"], "phase_count": len(phases)},
    }

async def _legacy_confirm_adjustment_phase(project_id: str, adjustment_id: str, request: AdjustmentPhaseConfirmRequest):
    """
    用户确认开启某阶段（0-based）。
    - phase_index=0：确认分工方案，开始第一阶段
    - phase_index>0：确认前一阶段已完成，开始下一阶段
    每阶段完成后自动质检，质检不通过自动返工（最多5轮），通过后等待用户确认下一阶段。
    """
    ctx = _get_project(project_id)
    adjustments = _get_adjustments(project_id)
    adj = next((a for a in adjustments if a["id"] == adjustment_id), None)
    if not adj:
        raise HTTPException(status_code=404, detail="变更工单不存在")

    phase_index = request.phase_index
    phases = adj.get("phases", [])
    if phase_index >= len(phases):
        raise HTTPException(status_code=400, detail=f"阶段 {phase_index} 不存在")

    # 前序阶段必须完成
    if phase_index > 0:
        prev = phases[phase_index - 1]
        if prev.get("status") != "done":
            raise HTTPException(status_code=400, detail=f"前一阶段尚未完成（状态：{prev.get('status')}）")

    phases[phase_index]["status"] = "executing"
    adj["current_phase_index"] = phase_index
    adj["status"] = "executing"
    adj["updated_at"] = time.time()
    adj["exec_log"].append(f"[{time.strftime('%H:%M:%S')}] 用户确认，开始阶段 {phase_index + 1}")

    asyncio.create_task(_execute_adjustment_phase(project_id, adjustment_id, phase_index))

    return {
        "success": True,
        "adjustment_id": adjustment_id,
        "phase_index": phase_index,
        "message": f"阶段 {phase_index + 1} 已开始，专家正在执行...",
    }

async def _legacy_confirm_adjustment(project_id: str, adjustment_id: str, request: AdjustmentConfirmRequest):
    """
    用户确认变更工单，触发多专家执行。
    确认后按 depends_on 依赖顺序调度任务。
    """
    ctx = _get_project(project_id)
    adjustments = _get_adjustments(project_id)
    adj = next((a for a in adjustments if a["id"] == adjustment_id), None)
    if not adj:
        raise HTTPException(status_code=404, detail="变更工单不存在")

    if not request.confirmed:
        adj["status"] = "cancelled"
        adj["updated_at"] = time.time()
        return {"success": True, "message": "已取消变更工单"}

    # 追加修改意见
    if request.modifications:
        adj["user_modifications"] = request.modifications

    adj["status"] = "executing"
    adj["updated_at"] = time.time()
    adj["exec_log"].append(f"[{time.strftime('%H:%M:%S')}] 用户确认，开始执行变更工单")

    # 异步调度执行
    asyncio.create_task(_execute_adjustment(project_id, adjustment_id))

    return {
        "success": True,
        "adjustment_id": adjustment_id,
        "message": f"变更工单已确认，开始执行 {len(adj['tasks'])} 个专家任务",
    }

async def _legacy_execute_adjustment_phase(project_id: str, adjustment_id: str, phase_index: int):
    """
    执行变更工单的单个阶段：
    1. 找出本阶段任务，按 depends_on 顺序执行
    2. 阶段完成后自动触发质检（复用阶段质检逻辑）
    3. 质检不通过自动返工（最多5轮）
    4. 质检通过后更新阶段状态 → done，等待用户确认下一阶段
    5. 所有阶段完成后自动提交文件版本快照
    """
    ctx = projects.get(project_id)
    if not ctx:
        return
    adjustments = _get_adjustments(project_id)
    adj = next((a for a in adjustments if a["id"] == adjustment_id), None)
    if not adj:
        return

    def log(msg: str):
        adj["exec_log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        adj["updated_at"] = time.time()

    phases = adj.get("phases", [])
    phase_info = phases[phase_index] if phase_index < len(phases) else None
    if not phase_info:
        return

    phase_task_ids = set(phase_info.get("tasks", []))
    all_tasks = adj.get("tasks", [])
    phase_tasks = [t for t in all_tasks if t["task_id"] in phase_task_ids]

    project_context = ctx.pm.context_summary or ctx.description or ""
    pm_leader = _pm_teams.get(project_id)
    if pm_leader and pm_leader.final_plan:
        project_context = str(pm_leader.final_plan)[:500]

    max_fix_rounds = 3
    completed_ids: set = set()

    for fix_round in range(max_fix_rounds + 1):
        if fix_round > 0:
            log(f"🔄 阶段{phase_index+1} 第{fix_round}轮返工")
        # 拓扑执行本阶段任务
        pending = [t for t in phase_tasks if t["status"] in ("pending", "failed")]
        limit = len(phase_tasks) + 2
        while pending and limit > 0:
            limit -= 1
            ready = [t for t in pending if all(d in completed_ids for d in t.get("depends_on", []))]
            if not ready:
                ready = [pending[0]]
            for t in ready:
                t["status"] = "executing"
                log(f"▶ [{t['task_id']}] {t['expert_role']}：{t['description'][:50]}")
            await asyncio.gather(*[_run_adjustment_task(ctx, adj, t, project_context) for t in ready], return_exceptions=True)
            for t in ready:
                if t["status"] == "done":
                    completed_ids.add(t["task_id"])
            pending = [t for t in phase_tasks if t["status"] in ("pending", "failed")]

        # 质检
        log(f"🔍 阶段{phase_index+1} 执行完毕，触发质检...")
        phases[phase_index]["status"] = "qc_running"
        adj["status"] = "qc_running"

        qc_passed = False
        qc_issues = []
        try:
            sp_ids = list({sp["id"] for sp in ctx.subprojects})
            all_passed = True
            for sp_id in sp_ids:
                sp = next((s for s in ctx.subprojects if s["id"] == sp_id), {})
                sp_name = sp.get("name", sp_id)
                loop = asyncio.get_event_loop()
                qc_entry = await loop.run_in_executor(
                    None, lambda sid=sp_id, sn=sp_name: _run_qc_for_subproject(ctx, sid, sn, is_final_phase=False)
                )
                if not qc_entry.get("passed"):
                    all_passed = False
                    qc_issues.extend([i for i in qc_entry.get("issues_detail", []) if i.get("status") == "open"][:3])
            qc_passed = all_passed
        except Exception as e:
            log(f"⚠️ 质检异常：{e}")
            qc_passed = False  # 质检异常时标记为不通过，需人工介入
            qc_issues.append({"layer": "system", "severity": "error", "message": f"质检工具异常：{str(e)}", "file_path": "", "status": "open"})

        adj["qc_results"][str(phase_index)] = {"passed": qc_passed, "issues": qc_issues, "checked_at": time.time()}

        if qc_passed:
            log(f"✅ 阶段{phase_index+1} 质检通过")
            break
        if fix_round < max_fix_rounds:
            log(f"⚠️ 质检发现 {len(qc_issues)} 个问题，自动返工（{fix_round+1}/{max_fix_rounds}）")
            for t in phase_tasks:
                t["status"] = "pending"
                relevant = [i for i in qc_issues if any(f in i.get("file_path","") for f in (t.get("files") or []))]
                if relevant:
                    # 替换而不是追加，防止多轮返工后描述无限膨胀超出 LLM 上下文限制
                    base_desc = t.get("_original_description") or t["description"]
                    t.setdefault("_original_description", base_desc)
                    t["description"] = base_desc + "\n\n【返工】修复：" + "；".join(i.get("message","")[:40] for i in relevant)
            completed_ids.clear()
        else:
            log(f"❌ 阶段{phase_index+1} 已达最大返工轮次，需人工处理")

    phases[phase_index]["status"] = "done" if qc_passed else "needs_manual"
    phases[phase_index]["qc_passed"] = qc_passed

    # 判断是否所有阶段完成
    all_done = all(p.get("status") in ("done",) for p in phases)
    next_phase_index = phase_index + 1 if phase_index + 1 < len(phases) else None

    if all_done:
        # 最后阶段完成，提交版本快照
        try:
            import hashlib as _hashlib, shutil as _shutil
            commit_id = _hashlib.sha256(f"{project_id}:{adjustment_id}:{time.time()}".encode()).hexdigest()[:12]
            versions_dir = ctx.workspace / ".project" / "versions"
            existing = [int(d.name) for d in versions_dir.iterdir() if d.is_dir() and d.name.isdigit()] if versions_dir.exists() else []
            version = max(existing, default=0) + 1
            snapshot_dir = versions_dir / str(version)
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            src_dir = ctx.workspace / "src"
            committed = []
            if src_dir.exists():
                for src_file in src_dir.rglob("*"):
                    if src_file.is_file():
                        rel = src_file.relative_to(ctx.workspace)
                        snap = snapshot_dir / rel
                        snap.parent.mkdir(parents=True, exist_ok=True)
                        _shutil.copy2(src_file, snap)
                        committed.append(str(rel).replace("\\", "/"))
            import json as _json
            (snapshot_dir / "commit.json").write_text(_json.dumps({
                "commit_id": commit_id, "version": version,
                "message": f"[调整完成] {adj['title']}",
                "committed_at": time.time(), "files": committed, "adjustment_id": adjustment_id,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            adj["snapshot_version"] = version
            log(f"✅ 所有阶段完成！已生成版本快照 v{version}")
        except Exception as e:
            log(f"⚠️ 版本快照失败：{e}")
        adj["status"] = "done"
    elif next_phase_index is not None and qc_passed:
        adj["status"] = "phase_done"
        adj["current_phase_index"] = phase_index
        log(f"⏸ 阶段{phase_index+1}已完成，等待用户确认开启阶段{next_phase_index+1}")
    else:
        adj["status"] = "needs_manual"

    adj["updated_at"] = time.time()
    await _persist_all_async()

async def _legacy_execute_adjustment(project_id: str, adjustment_id: str):
    """
    异步执行变更工单：
    1. 按依赖顺序调度任务
    2. 每个任务创建临时 Agent 执行
    3. 全部完成后自动触发质检
    4. 质检不通过则触发返工循环
    5. 质检通过后自动提交文件版本快照
    """
    ctx = projects.get(project_id)
    if not ctx:
        return

    adjustments = _get_adjustments(project_id)
    adj = next((a for a in adjustments if a["id"] == adjustment_id), None)
    if not adj:
        return

    def log(msg: str):
        adj["exec_log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        adj["updated_at"] = time.time()

    tasks = adj.get("tasks", [])
    task_map = {t["task_id"]: t for t in tasks}
    completed_task_ids: set = set()
    max_rounds = 2  # 最多返工 2 轮

    project_context = ctx.pm.context_summary or ctx.description or ""
    pm_leader = _pm_teams.get(project_id)
    if pm_leader and pm_leader.final_plan:
        project_context = str(pm_leader.final_plan)[:500]

    for round_num in range(max_rounds + 1):
        if round_num > 0:
            log(f"🔄 第 {round_num} 轮返工开始")

        # 按依赖顺序执行任务（简单拓扑排序：无依赖的先执行）
        pending = [t for t in tasks if t["status"] in ("pending", "failed")]
        iteration_limit = len(tasks) + 2
        iteration = 0
        while pending and iteration < iteration_limit:
            iteration += 1
            # 找出所有依赖已满足的任务
            ready = [
                t for t in pending
                if all(dep in completed_task_ids for dep in t.get("depends_on", []))
            ]
            if not ready:
                # 有循环依赖，强制执行第一个
                ready = [pending[0]]

            # 并行执行所有 ready 任务
            coros = []
            for task in ready:
                task["status"] = "executing"
                log(f"▶ 任务 [{task['task_id']}] {task['expert_role']}：{task['description'][:60]}")
                coros.append(_run_adjustment_task(ctx, adj, task, project_context))

            await asyncio.gather(*coros, return_exceptions=True)

            # 更新 pending 列表
            for task in ready:
                if task["status"] == "done":
                    completed_task_ids.add(task["task_id"])
            pending = [t for t in tasks if t["status"] in ("pending", "failed")]

        # 全部任务执行完，触发质检
        log("🔍 所有任务执行完毕，触发质检...")
        adj["status"] = "qc_running"

        qc_passed = False
        qc_issues = []
        try:
            # 找到本次变更涉及的子项目（或使用第一个子项目）
            affected_files = []
            for t in tasks:
                affected_files.extend(t.get("files", []) or t.get("output_files", []))

            # 对所有相关子项目质检
            sp_ids = list({sp["id"] for sp in ctx.subprojects})
            all_passed = True
            for sp_id in sp_ids:
                sp = next((s for s in ctx.subprojects if s["id"] == sp_id), {})
                sp_name = sp.get("name", sp_id)
                loop = asyncio.get_event_loop()
                qc_entry = await loop.run_in_executor(
                    None,
                    lambda sid=sp_id, sn=sp_name: _run_qc_for_subproject(ctx, sid, sn, is_final_phase=False)
                )
                if not qc_entry.get("passed"):
                    all_passed = False
                    open_issues = [i for i in qc_entry.get("issues_detail", []) if i.get("status") == "open"]
                    qc_issues.extend(open_issues[:5])

            qc_passed = all_passed
        except Exception as e:
            log(f"⚠️ 质检执行异常：{e}")
            qc_passed = False  # 质检异常时标记为不通过，需人工介入
            qc_issues.append({"layer": "system", "severity": "error", "message": f"质检工具异常：{str(e)}", "file_path": "", "status": "open"})

        adj["qc_result"] = {
            "passed": qc_passed,
            "issues": qc_issues,
            "checked_at": time.time(),
        }

        if qc_passed:
            break

        if round_num < max_rounds:
                # 质检不通过，标记失败任务重新执行
                log(f"⚠️ 质检发现 {len(qc_issues)} 个问题，触发返工（第 {round_num + 1}/{max_rounds} 轮）")
                for t in tasks:
                    t["status"] = "pending"
                    # 将质检问题注入任务描述（替换而非追加，防止多轮返工后描述无限膨胀）
                    relevant = [i for i in qc_issues if any(f in (i.get("file_path","")) for f in (t.get("files") or []))]
                    if relevant:
                        base_desc = t.get("_original_description") or t["description"]
                        t.setdefault("_original_description", base_desc)
                        t["description"] = base_desc + f"\n\n【返工指令】修复以下质检问题：\n" + "\n".join(
                            f"- {i.get('message','')} ({i.get('file_path','')})" for i in relevant
                        )
                completed_task_ids.clear()
        else:
            log("❌ 已达最大返工轮次，部分问题需人工处理")

    # 质检完成后，自动提交文件版本快照
    if qc_passed:
        try:
            import hashlib as _hashlib, shutil as _shutil
            commit_id = _hashlib.sha256(f"{project_id}:{adjustment_id}:{time.time()}".encode()).hexdigest()[:12]
            versions_dir = ctx.workspace / ".project" / "versions"
            existing = [int(d.name) for d in versions_dir.iterdir() if d.is_dir() and d.name.isdigit()] if versions_dir.exists() else []
            version = max(existing, default=0) + 1
            snapshot_dir = versions_dir / str(version)
            snapshot_dir.mkdir(parents=True, exist_ok=True)

            # 只快照 src/ 目录
            src_dir = ctx.workspace / "src"
            committed = []
            if src_dir.exists():
                for src_file in src_dir.rglob("*"):
                    if src_file.is_file():
                        rel = src_file.relative_to(ctx.workspace)
                        snap = snapshot_dir / rel
                        snap.parent.mkdir(parents=True, exist_ok=True)
                        _shutil.copy2(src_file, snap)
                        committed.append(str(rel).replace("\\", "/"))

            import json as _json
            meta = {
                "commit_id": commit_id,
                "version": version,
                "message": f"[调整] {adj['title']}",
                "committed_at": time.time(),
                "files": committed,
                "adjustment_id": adjustment_id,
            }
            (snapshot_dir / "commit.json").write_text(
                _json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            adj["snapshot_version"] = version
            log(f"✅ 变更完成！文件已快照为版本 v{version}（{len(committed)} 个文件）")
        except Exception as e:
            log(f"⚠️ 版本快照失败：{e}")

    adj["status"] = "done" if qc_passed else "needs_manual"
    adj["updated_at"] = time.time()
    await _persist_all_async()

async def _legacy_run_adjustment_task(ctx, adj: Dict, task: Dict, project_context: str):
    """执行单个变更任务（创建临时 Agent）"""
    try:
        agent_id = f"adj-agent-{uuid.uuid4().hex[:6]}"
        role = task.get("expert_role", "开发工程师")
        description = task.get("description", "")
        files = task.get("files", [])

        # 构建任务描述（附加文件上下文）
        full_desc = description
        if files:
            full_desc += f"\n\n涉及文件：{', '.join(files)}"
        full_desc += f"\n\n变更背景：{adj.get('description', '')}"

        # 注册临时 Agent
        ctx.agents[agent_id] = {
            "id": agent_id,
            "role": role,
            "phase_id": f"adj-{adj['id']}",
            "subproject_id": adj["id"],
            "status": "working",
            "created_at": time.time(),
            "project_id": ctx.project_id,
            "source": "adjustment",
            "is_temp": True,
        }
        task["agent_id"] = agent_id

        # 找到第一个子项目ID
        sp_id = ctx.subprojects[0]["id"] if ctx.subprojects else adj["id"]

        exec_agent = _make_exec_agent(ctx, agent_id)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: exec_agent.execute_task(
                subproject_id=sp_id,
                subproject_name=f"变更：{adj['title']}",
                description=full_desc,
                tech_stack=[],
                project_context=project_context,
            )
        )

        task["status"] = "done" if result.get("success") else "failed"
        task["output_files"] = result.get("output_files", [])
        ctx.agents[agent_id]["status"] = "completed"
    except Exception as e:
        task["status"] = "failed"
        task["error"] = str(e)


def _adjustment_request_digest(
    *,
    adjustment_id: str,
    mode: str,
    phase_index: Optional[int],
    confirmed: bool,
    modifications: str = "",
) -> str:
    payload = {
        "adjustment_id": adjustment_id,
        "mode": mode,
        "phase_index": phase_index,
        "confirmed": confirmed,
        "modifications": modifications,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _normalize_adjustment_paths(values: object, *, field: str) -> List[str]:
    if not isinstance(values, list) or not values:
        raise ValueError(f"Adjustment task requires explicit {field}")
    normalized: List[str] = []
    for value in values:
        path = PurePosixPath(str(value or "").replace("\\", "/").strip())
        if (
            path.is_absolute()
            or str(path) in {"", "."}
            or ".." in path.parts
            or path.parts[0].casefold() == ".project"
        ):
            raise ValueError(f"Adjustment task has unsafe {field}")
        canonical = path.as_posix()
        if canonical not in normalized:
            normalized.append(canonical)
    return normalized


def _adjustment_criterion(
    task_id: str, raw: object
) -> Dict[str, str]:
    if isinstance(raw, dict):
        text = str(raw.get("text") or raw.get("criterion") or "").strip()
        evidence_type = str(
            raw.get("required_evidence_type") or ""
        ).strip().casefold()
    else:
        text = str(raw or "").strip()
        evidence_type = ""
    if not evidence_type:
        lowered = text.casefold()
        if any(token in lowered for token in ("http", "api", "status code")):
            evidence_type = "api"
        elif any(
            token in lowered
            for token in ("test", "build", "install", "runner", "command")
        ):
            evidence_type = "runner"
        else:
            evidence_type = "semantic"
    if evidence_type not in {"runner", "api", "semantic"}:
        raise ValueError("Adjustment criterion has unsupported evidence type")
    criterion_id = "criterion-" + hashlib.sha256(
        f"{task_id}\0{text}\0{evidence_type}".encode("utf-8")
    ).hexdigest()[:16]
    return {
        "criterion_id": criterion_id,
        "text": text,
        "required_evidence_type": evidence_type,
    }


def _build_adjustment_execution_contract(
    adj: Dict[str, Any],
    selected_tasks: List[Dict[str, Any]],
    *,
    mode: str,
    phase_index: Optional[int],
    request_digest: str,
    modifications: str,
    input_manifest: Dict[str, Any],
) -> Dict[str, Any]:
    tasks: List[Dict[str, Any]] = []
    owned_paths: Dict[str, str] = {}
    for task in selected_tasks:
        task_id = str(task.get("task_id") or "").strip()
        owner = str(task.get("expert_role") or task.get("owner") or "").strip()
        description = str(task.get("description") or "").strip()
        if not task_id or not owner or not description:
            raise ValueError(
                "Adjustment task requires task_id, owner, and description"
            )
        files = _normalize_adjustment_paths(task.get("files"), field="files")
        deliverables = _normalize_adjustment_paths(
            task.get("deliverables") or files,
            field="deliverables",
        )
        acceptance = task.get("acceptance_criteria")
        if isinstance(acceptance, (str, dict)):
            raw_acceptance = [acceptance]
        elif isinstance(acceptance, list):
            raw_acceptance = list(acceptance)
        else:
            raw_acceptance = []
        criteria = [
            _adjustment_criterion(task_id, item)
            for item in raw_acceptance
            if (
                str(item.get("text") or item.get("criterion") or "").strip()
                if isinstance(item, dict)
                else str(item).strip()
            )
        ]
        if not criteria:
            raise ValueError(
                f"Adjustment task {task_id} requires acceptance_criteria"
            )
        for path in files:
            previous_owner = owned_paths.get(path.casefold())
            if previous_owner and previous_owner != task_id:
                raise ValueError(
                    f"Adjustment path {path} has multiple task owners"
                )
            owned_paths[path.casefold()] = task_id
        tasks.append({
            "task_id": task_id,
            "owner": owner,
            "description": description,
            "files": files,
            "deliverables": deliverables,
            "acceptance_criteria": [
                item["text"] for item in criteria
            ],
            "criteria": criteria,
            "depends_on": [
                str(item) for item in task.get("depends_on", []) or []
            ],
        })
    contract = {
        "schema_version": 1,
        "adjustment_id": str(adj.get("id") or ""),
        "mode": mode,
        "phase_index": phase_index,
        "request_digest": request_digest,
        "modifications": str(modifications or ""),
        "input_artifact_sha256": input_manifest.get("artifact_sha256"),
        "tasks": tasks,
    }
    encoded = json.dumps(
        contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        **contract,
        "contract_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _validate_adjustment_contract(contract: object) -> Dict[str, Any]:
    if not isinstance(contract, dict) or contract.get("schema_version") != 1:
        raise ValueError("Adjustment execution contract is missing")
    payload = {
        key: value for key, value in contract.items() if key != "contract_sha256"
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != contract.get("contract_sha256"):
        raise ValueError("Adjustment execution contract was modified")
    return contract


def _path_is_in_adjustment_scope(path: str, scopes: List[str]) -> bool:
    normalized = str(path).replace("\\", "/").strip("/")
    return any(
        normalized == scope.rstrip("/")
        or normalized.startswith(scope.rstrip("/") + "/")
        for scope in scopes
    )


def _record_adjustment_task_receipt(
    ctx: ProjectContext,
    adjustment: Dict[str, Any],
    task: Dict[str, Any],
    contract_task: Dict[str, Any],
    result: object,
    *,
    run_id: str,
) -> Dict[str, Any]:
    if (
        not isinstance(result, dict)
        or result.get("success") is not True
        or task.get("status") != "done"
    ):
        raise ValueError("Adjustment task has no successful execution result")
    output_files = sorted({
        str(path).replace("\\", "/").strip("/")
        for path in result.get("output_files") or task.get("output_files") or []
        if str(path).strip()
    })
    if not output_files:
        raise ValueError("Adjustment success receipt requires output files")
    output_digests: List[Dict[str, Any]] = []
    root = Path(ctx.workspace).resolve()
    for relative in output_files:
        if not _path_is_in_adjustment_scope(
            relative, list(contract_task.get("files") or [])
        ):
            raise ValueError("Adjustment receipt output is outside task scope")
        target = (root / PurePosixPath(relative)).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("Adjustment receipt path escapes workspace") from exc
        if not target.is_file() or target.is_symlink():
            raise ValueError("Adjustment receipt output file is missing")
        content = target.read_bytes()
        output_digests.append({
            "path": relative,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        })
    evidence_payload = {
        "success": True,
        "output_files": output_files,
        "message": str(result.get("message") or ""),
    }
    receipt = {
        "schema_version": 1,
        "run_id": run_id,
        "task_id": str(task.get("task_id") or ""),
        "contract_sha256": str(
            (adjustment.get("active_run") or {})
            .get("execution_contract", {})
            .get("contract_sha256")
            or ""
        ),
        "success": True,
        "agent_id": str(task.get("agent_id") or ""),
        "output_digests": output_digests,
        "evidence": {
            "type": "execution_result",
            "sha256": hashlib.sha256(
                json.dumps(
                    evidence_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        },
        "completed_at": time.time(),
    }
    task["success_receipt"] = receipt
    (adjustment.get("active_run") or {}).setdefault(
        "task_receipts", {}
    )[receipt["task_id"]] = copy.deepcopy(receipt)
    return receipt


def _adjustment_receipt_blockers(
    ctx: ProjectContext, adjustment: Dict[str, Any]
) -> List[str]:
    active_run = adjustment.get("active_run")
    if not isinstance(active_run, dict):
        return [f"adjustment {adjustment.get('id')}: missing active run"]
    try:
        contract = _validate_adjustment_contract(
            active_run.get("execution_contract")
        )
    except ValueError:
        return [f"adjustment {adjustment.get('id')}: invalid execution contract"]
    receipts = active_run.get("task_receipts")
    blockers: List[str] = []
    root = Path(ctx.workspace).resolve()
    for contract_task in contract.get("tasks") or []:
        task_id = str(contract_task.get("task_id") or "")
        receipt = receipts.get(task_id) if isinstance(receipts, dict) else None
        if (
            not isinstance(receipt, dict)
            or receipt.get("success") is not True
            or receipt.get("run_id") != active_run.get("run_id")
            or receipt.get("task_id") != task_id
            or receipt.get("contract_sha256") != contract.get("contract_sha256")
            or not isinstance(receipt.get("evidence"), dict)
            or not receipt.get("output_digests")
        ):
            blockers.append(
                f"adjustment {adjustment.get('id')} task {task_id}: "
                "missing authoritative success receipt"
            )
            continue
        for output in receipt.get("output_digests") or []:
            relative = str(output.get("path") or "")
            target = (root / PurePosixPath(relative)).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                blockers.append(
                    f"adjustment {adjustment.get('id')} task {task_id}: "
                    "receipt output path is unsafe"
                )
                break
            if (
                not re.fullmatch(r"[0-9a-f]{64}", str(output.get("sha256") or ""))
                or not isinstance(output.get("size"), int)
                or int(output.get("size")) < 0
            ):
                blockers.append(
                    f"adjustment {adjustment.get('id')} task {task_id}: "
                    "receipt output digest is invalid"
                )
                break
    return blockers


def _adjustment_task_layers(
    all_tasks: List[Dict[str, Any]],
    selected_tasks: List[Dict[str, Any]],
) -> List[List[Dict[str, Any]]]:
    """Return deterministic DAG layers or fail closed on invalid dependencies."""
    task_map: Dict[str, Dict[str, Any]] = {}
    for task in all_tasks:
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("Adjustment task is missing task_id")
        if task_id in task_map:
            raise ValueError(f"Duplicate adjustment task_id: {task_id}")
        task_map[task_id] = task
    selected_ids = {
        str(task.get("task_id") or "").strip() for task in selected_tasks
    }
    for task in all_tasks:
        task_id = str(task.get("task_id") or "").strip()
        for dependency in task.get("depends_on", []) or []:
            dependency_id = str(dependency or "").strip()
            if dependency_id not in task_map:
                raise ValueError(
                    f"Adjustment task {task_id} has missing dependency {dependency_id}"
                )
    remaining = set(selected_ids)
    completed = {
        task_id
        for task_id, task in task_map.items()
        if task_id not in selected_ids and task.get("status") == "done"
    }
    layers: List[List[Dict[str, Any]]] = []
    while remaining:
        ready_ids = sorted(
            (
                task_id
                for task_id in remaining
                if all(
                    str(dependency or "").strip() in completed
                    for dependency in task_map[task_id].get("depends_on", []) or []
                )
            ),
            key=str.casefold,
        )
        if not ready_ids:
            unresolved = ", ".join(sorted(remaining, key=str.casefold))
            raise ValueError(
                "Adjustment dependency graph has a cycle or unfinished "
                f"cross-phase dependency: {unresolved}"
            )
        layers.append([task_map[task_id] for task_id in ready_ids])
        remaining.difference_update(ready_ids)
        completed.update(ready_ids)
    return layers


async def _run_adjustment_task(
    ctx: ProjectContext,
    adj: Dict[str, Any],
    task: Dict[str, Any],
    project_context: str,
    execution_guard: _FenceExecutionGuard,
    task_contract: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run one adjustment task without converting failed work into completion."""
    execution_guard()
    agent_id = f"adj-agent-{uuid.uuid4().hex[:6]}"
    bound = task_contract or task
    role = bound.get("owner") or bound.get("expert_role") or "Software Engineer"
    description = str(bound.get("description") or "")
    files = list(bound.get("files") or [])
    full_desc = description
    if files:
        full_desc += "\n\nFiles in scope: " + ", ".join(map(str, files))
    full_desc += "\n\nAdjustment context: " + str(adj.get("description") or "")
    modifications = str(
        (adj.get("active_run") or {})
        .get("execution_contract", {})
        .get("modifications")
        or ""
    )
    if modifications:
        full_desc += "\n\nConfirmed user modifications:\n" + modifications
    full_desc += "\n\nAcceptance criteria:\n- " + "\n- ".join(
        bound.get("acceptance_criteria") or []
    )
    ctx.agents[agent_id] = {
        "id": agent_id,
        "role": role,
        "phase_id": f"adj-{adj['id']}",
        "subproject_id": adj["id"],
        "status": "working",
        "created_at": time.time(),
        "project_id": ctx.project_id,
        "source": "adjustment",
        "is_temp": True,
        "allowed_path_prefixes": files,
    }
    task["agent_id"] = agent_id
    task["status"] = "executing"
    subproject_id = ctx.subprojects[0]["id"] if ctx.subprojects else adj["id"]
    from api.routes_execution import _make_exec_agent

    exec_agent = _make_exec_agent(
        ctx,
        agent_id,
        execution_guard=execution_guard,
    )
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: exec_agent.execute_task(
                subproject_id=subproject_id,
                subproject_name=f"Adjustment: {adj['title']}",
                description=full_desc,
                tech_stack=[],
                project_context=project_context,
            ),
        )
        execution_guard()
        if not isinstance(result, dict) or result.get("success") is not True:
            reason = (
                str(result.get("error") or result.get("message") or "task failed")
                if isinstance(result, dict)
                else "task returned an invalid result"
            )
            raise RuntimeError(reason)
        output_files = [
            str(path).replace("\\", "/")
            for path in result.get("output_files") or []
        ]
        if any(
            not _path_is_in_adjustment_scope(path, files)
            for path in output_files
        ):
            raise RuntimeError(
                "Adjustment worker reported output outside its authorized paths"
            )
        task["status"] = "done"
        task["output_files"] = output_files
        ctx.agents[agent_id]["status"] = "completed"
        return result
    except BaseException as exc:
        task["status"] = "failed"
        task["error"] = (
            "cancelled" if isinstance(exc, asyncio.CancelledError) else str(exc)
        )
        ctx.agents[agent_id]["status"] = "failed"
        raise


async def _execute_adjustment_run(
    project_id: str,
    adjustment_id: str,
    *,
    run_id: str,
    mode: str,
    phase_index: Optional[int],
    fence_token: str,
    workspace: Path,
) -> None:
    ctx = projects.get(project_id)
    adjustments = _get_adjustments(project_id)
    adj = next((item for item in adjustments if item.get("id") == adjustment_id), None)
    current_task = asyncio.current_task()
    if not ctx or not adj:
        if (
            _adjustment_run_ids.get(project_id) == run_id
            and _adjustment_tasks.get(project_id) is current_task
        ):
            _adjustment_tasks.pop(project_id, None)
            _adjustment_run_ids.pop(project_id, None)
            owned_token = _adjustment_fence_tokens.pop(project_id, None)
            if owned_token:
                release_project_write_fence(
                    project_id,
                    workspace,
                    owned_token,
                )
        return
    guard = _FenceExecutionGuard(
        project_id,
        Path(ctx.workspace),
        fence_token,
    )

    def log(message: str) -> None:
        adj.setdefault("exec_log", []).append(
            f"[{time.strftime('%H:%M:%S')}] {message}"
        )
        adj["updated_at"] = time.time()

    try:
        contract = _validate_adjustment_contract(
            (adj.get("active_run") or {}).get("execution_contract")
        )
        if (
            contract.get("mode") != mode
            or contract.get("phase_index") != phase_index
            or contract.get("adjustment_id") != adjustment_id
        ):
            raise ValueError("Adjustment execution contract does not match the run")
        pre_manifest, pre_files = _decode_adjustment_snapshot(
            adj.get("pre_run_snapshot")
        )
        if (
            contract.get("input_artifact_sha256")
            != pre_manifest.get("artifact_sha256")
        ):
            raise ValueError("Adjustment contract is bound to another workspace")
        contract_by_id = {
            str(item.get("task_id")): item
            for item in contract.get("tasks") or []
        }
        all_tasks = list(adj.get("tasks") or [])
        if mode == "phase":
            phases = list(adj.get("phases") or [])
            if phase_index is None or phase_index < 0 or phase_index >= len(phases):
                raise ValueError("Adjustment phase does not exist")
            phase = phases[phase_index]
            selected_ids = {
                str(task_id) for task_id in phase.get("tasks", []) or []
            }
            selected_tasks = [
                task
                for task in all_tasks
                if str(task.get("task_id") or "") in selected_ids
            ]
            if selected_ids != {
                str(task.get("task_id") or "") for task in selected_tasks
            }:
                raise ValueError("Adjustment phase references an unknown task")
        else:
            selected_tasks = all_tasks
        if not selected_tasks:
            raise ValueError("Adjustment contains no executable tasks")
        if set(contract_by_id) != {
            str(task.get("task_id") or "") for task in selected_tasks
        }:
            raise ValueError("Adjustment task selection changed after confirmation")
        layers = _adjustment_task_layers(all_tasks, selected_tasks)
        project_context = ctx.pm.context_summary or ctx.description or ""
        pm_leader = _pm_teams.get(project_id)
        if pm_leader and pm_leader.final_plan:
            project_context = str(pm_leader.final_plan)
        for layer in layers:
            renewed = renew_project_write_fence(
                project_id,
                Path(ctx.workspace),
                fence_token,
                ttl_seconds=15 * 60,
            )
            adj["active_run"]["leased_until"] = renewed.get("leased_until")
            guard()
            for task in layer:
                task["status"] = "executing"
            results = await asyncio.wait_for(
                asyncio.gather(
                    *[
                        _run_adjustment_task(
                            ctx,
                            adj,
                            task,
                            project_context,
                            guard,
                            contract_by_id[str(task.get("task_id") or "")],
                        )
                        for task in layer
                    ],
                    return_exceptions=True,
                ),
                timeout=ADJUSTMENT_TASK_TIMEOUT_SECONDS,
            )
            failures = [
                result for result in results if isinstance(result, BaseException)
            ]
            failures.extend(
                RuntimeError(f"task {task.get('task_id')} did not complete")
                for task in layer
                if task.get("status") != "done"
            )
            if not failures:
                for task, result in zip(layer, results):
                    try:
                        with guard.write_guard():
                            _record_adjustment_task_receipt(
                                ctx,
                                adj,
                                task,
                                contract_by_id[
                                    str(task.get("task_id") or "")
                                ],
                                result,
                                run_id=run_id,
                            )
                    except Exception as receipt_error:
                        failures.append(receipt_error)
            if failures:
                raise RuntimeError(
                    "Adjustment task batch failed: "
                    + "; ".join(
                        f"{failure.__class__.__name__}: {failure}"
                        for failure in failures[:3]
                    )
                )
            await _persist_all_async()
        with guard.write_guard():
            result_manifest, result_files = collect_delivery_artifact(
                Path(ctx.workspace),
                required_paths=_adjustment_required_paths(ctx),
            )
        all_paths = set(pre_files) | set(result_files)
        changed_paths = sorted(
            path for path in all_paths
            if pre_files.get(path) != result_files.get(path)
        )
        authorized_scopes = [
            path
            for item in contract_by_id.values()
            for path in item.get("files") or []
        ]
        unauthorized = [
            path for path in changed_paths
            if not _path_is_in_adjustment_scope(path, authorized_scopes)
        ]
        if unauthorized:
            raise RuntimeError(
                "Adjustment changed files outside its immutable contract: "
                + ", ".join(unauthorized[:3])
            )
        if not changed_paths:
            raise RuntimeError(
                "Adjustment produced no verified delivery-file delta"
            )
        for task_id, item in contract_by_id.items():
            if not any(
                _path_is_in_adjustment_scope(path, item.get("files") or [])
                for path in changed_paths
            ):
                raise RuntimeError(
                    f"Adjustment task {task_id} produced no verified scoped delta"
                )
        if mode == "phase":
            phases = list(adj.get("phases") or [])
            phase = phases[int(phase_index)]
            phase["status"] = "done"
            phase["execution_completed_at"] = time.time()
            all_done = all(item.get("status") == "done" for item in phases)
            adj["status"] = "awaiting_final_qa" if all_done else "phase_done"
        else:
            adj["status"] = "awaiting_final_qa"
        adj["result_manifest"] = result_manifest
        adj["adjustment_acceptance"] = {
            "contract_sha256": contract.get("contract_sha256"),
            "input_artifact_sha256": contract.get("input_artifact_sha256"),
            "result_artifact_sha256": result_manifest.get("artifact_sha256"),
            "changed_paths": changed_paths,
            "acceptance_criteria": [
                criterion
                for item in contract_by_id.values()
                for criterion in item.get("acceptance_criteria") or []
            ],
            "criteria": [
                copy.deepcopy(criterion)
                for item in contract_by_id.values()
                for criterion in item.get("criteria") or []
            ],
            "modifications": contract.get("modifications") or "",
            "status": "pending_final_qa",
        }
        adj["requires_final_qa"] = True
        adj["final_qa_trigger"] = {
            "status": "required",
            "route": f"/projects/{project_id}/final-qa",
        }
        adj["active_run"]["status"] = adj["status"]
        adj["active_run"]["finished_at"] = time.time()
        _invalidate_final_qa_after_adjustment(ctx, adjustment_id)
        log("Adjustment execution completed; authoritative Final QA is required")
        await _persist_all_async()
    except BaseException as exc:
        guard.revoke()
        restored = False
        recovery_error = ""
        try:
            with project_write_guard(project_id, Path(ctx.workspace), fence_token):
                _restore_adjustment_snapshot(ctx, adj.get("pre_run_snapshot"))
            restored = True
        except Exception as restore_error:
            recovery_error = restore_error.__class__.__name__
        if isinstance(adj.get("active_run"), dict):
            adj["active_run"]["status"] = (
                "interrupted"
                if isinstance(exc, asyncio.CancelledError)
                else "failed"
            )
            adj["active_run"]["finished_at"] = time.time()
            adj["active_run"]["workspace_restored"] = restored
            if recovery_error:
                adj["active_run"]["recovery_error"] = recovery_error
        adj["status"] = "needs_manual" if restored else "recovery_required"
        adj["failure"] = {
            "type": exc.__class__.__name__,
            "message": (
                "Adjustment execution failed; workspace restored"
                if restored
                else "Adjustment execution failed and snapshot recovery failed"
            ),
        }
        log(adj["failure"]["message"])
        await _persist_all_async()
        if isinstance(exc, asyncio.CancelledError):
            raise
    finally:
        if (
            _adjustment_run_ids.get(project_id) == run_id
            and _adjustment_tasks.get(project_id) is current_task
        ):
            _adjustment_tasks.pop(project_id, None)
            _adjustment_run_ids.pop(project_id, None)
            owned_token = _adjustment_fence_tokens.pop(project_id, None)
            if owned_token:
                try:
                    release_project_write_fence(
                        project_id,
                        Path(ctx.workspace),
                        owned_token,
                    )
                except ProjectWriteFenceConflict:
                    logger.error(
                        "Adjustment fence release failed project=%s run=%s",
                        project_id,
                        run_id,
                    )


async def _start_adjustment_run(
    project_id: str,
    adjustment_id: str,
    *,
    mode: str,
    phase_index: Optional[int],
    request_digest: str,
    modifications: str = "",
) -> Dict[str, Any]:
    ctx = _get_project(project_id)
    adjustments = _get_adjustments(project_id)
    adj = next((item for item in adjustments if item.get("id") == adjustment_id), None)
    if not adj:
        raise HTTPException(status_code=404, detail="Adjustment does not exist")
    awaiting_other = next((
        item for item in adjustments
        if item is not adj
        and not _adjustment_is_terminal(item)
    ), None)
    if awaiting_other:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Adjustment {awaiting_other.get('id')} is not terminal; "
                "a new adjustment cannot start"
            ),
        )
    current_task = _adjustment_tasks.get(project_id)
    active_run = adj.get("active_run")
    if current_task is not None and not current_task.done():
        if (
            isinstance(active_run, dict)
            and active_run.get("request_digest") == request_digest
        ):
            return {
                "success": True,
                "already_running": True,
                "adjustment_id": adjustment_id,
                "run_id": active_run.get("run_id"),
            }
        raise HTTPException(
            status_code=409,
            detail="Another adjustment run is active for this project",
        )
    if isinstance(active_run, dict):
        active_status = str(active_run.get("status") or "")
        if active_status in {"queued", "executing"}:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Persisted adjustment run has no live worker; "
                    "call the adjustment recovery endpoint before retrying"
                ),
            )
        if (
            active_status in {"awaiting_final_qa", "phase_done"}
            and active_run.get("request_digest") == request_digest
        ):
            return {
                "success": True,
                "already_completed": True,
                "adjustment_id": adjustment_id,
                "run_id": active_run.get("run_id"),
                "status": active_status,
            }
        if active_status == "awaiting_final_qa":
            raise HTTPException(
                status_code=409,
                detail="Adjustment was already executed with a different request",
            )
    if mode == "phase":
        phases = list(adj.get("phases") or [])
        if phase_index is None or phase_index < 0 or phase_index >= len(phases):
            raise HTTPException(status_code=400, detail="Adjustment phase does not exist")
        if phase_index > 0 and phases[phase_index - 1].get("status") != "done":
            raise HTTPException(
                status_code=409,
                detail="Previous adjustment phase is not complete",
            )
        selected_ids = {
            str(item) for item in phases[phase_index].get("tasks", []) or []
        }
        selected_tasks = [
            task for task in adj.get("tasks", [])
            if str(task.get("task_id") or "") in selected_ids
        ]
    else:
        selected_tasks = list(adj.get("tasks") or [])
    if not selected_tasks:
        raise HTTPException(status_code=409, detail="Adjustment has no tasks")
    try:
        fence = acquire_project_write_fence(
            project_id,
            Path(ctx.workspace),
            owner=f"adjustment:{adjustment_id}",
            purpose="adjustment",
            ttl_seconds=15 * 60,
        )
    except ProjectWriteFenceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    token = str(fence["token"])
    try:
        with project_write_guard(project_id, Path(ctx.workspace), token):
            snapshot = _take_adjustment_snapshot(ctx)
            contract = _build_adjustment_execution_contract(
                adj,
                selected_tasks,
                mode=mode,
                phase_index=phase_index,
                request_digest=request_digest,
                modifications=modifications,
                input_manifest=snapshot["manifest"],
            )
    except ValueError as exc:
        release_project_write_fence(project_id, Path(ctx.workspace), token)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        release_project_write_fence(project_id, Path(ctx.workspace), token)
        raise
    run_id = uuid.uuid4().hex
    adj["pre_run_snapshot"] = snapshot
    adj["active_run"] = {
        "run_id": run_id,
        "request_digest": request_digest,
        "mode": mode,
        "phase_index": phase_index,
        "status": "queued",
        "started_at": time.time(),
        "fence_lock_id": fence.get("lock_id"),
        "execution_contract": contract,
        "task_receipts": {},
    }
    adj["status"] = "executing"
    adj["updated_at"] = time.time()
    await _persist_all_async()
    task = asyncio.create_task(
        _execute_adjustment_run(
            project_id,
            adjustment_id,
            run_id=run_id,
            mode=mode,
            phase_index=phase_index,
            fence_token=token,
            workspace=Path(ctx.workspace),
        )
    )
    _adjustment_tasks[project_id] = task
    _adjustment_run_ids[project_id] = run_id
    _adjustment_fence_tokens[project_id] = token
    adj["active_run"]["status"] = "executing"
    return {
        "success": True,
        "adjustment_id": adjustment_id,
        "run_id": run_id,
        "status": "executing",
    }


@router.post("/projects/{project_id}/adjustments/{adjustment_id}/confirm-phase")
async def confirm_adjustment_phase(
    project_id: str,
    adjustment_id: str,
    request: AdjustmentPhaseConfirmRequest,
):
    request_digest = _adjustment_request_digest(
        adjustment_id=adjustment_id,
        mode="phase",
        phase_index=request.phase_index,
        confirmed=True,
    )
    return await _start_adjustment_run(
        project_id,
        adjustment_id,
        mode="phase",
        phase_index=request.phase_index,
        request_digest=request_digest,
        modifications="",
    )


@router.post("/projects/{project_id}/adjustments/{adjustment_id}/confirm")
async def confirm_adjustment(
    project_id: str,
    adjustment_id: str,
    request: AdjustmentConfirmRequest,
):
    ctx = _get_project(project_id)
    adjustments = _get_adjustments(project_id)
    adj = next((item for item in adjustments if item.get("id") == adjustment_id), None)
    if not adj:
        raise HTTPException(status_code=404, detail="Adjustment does not exist")
    if not request.confirmed:
        try:
            with project_write_guard(project_id, Path(ctx.workspace)):
                blocker = _adjustment_cancel_blocker(ctx, adj)
                if blocker:
                    raise HTTPException(status_code=409, detail=blocker)
                adj["status"] = "cancelled"
                adj["updated_at"] = time.time()
        except ProjectWriteFenceConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await _persist_all_async()
        return {"success": True, "message": "Adjustment cancelled"}
    modifications = str(request.modifications or "")
    request_digest = _adjustment_request_digest(
        adjustment_id=adjustment_id,
        mode="full",
        phase_index=None,
        confirmed=True,
        modifications=modifications,
    )
    if modifications:
        adj["user_modifications"] = modifications
    return await _start_adjustment_run(
        project_id,
        adjustment_id,
        mode="full",
        phase_index=None,
        request_digest=request_digest,
        modifications=modifications,
    )


@router.post("/projects/{project_id}/adjustments/{adjustment_id}/recover")
async def recover_adjustment_run(project_id: str, adjustment_id: str):
    """Revoke one orphaned generation, restore its snapshot, and allow retry."""
    ctx = _get_project(project_id)
    adjustments = _get_adjustments(project_id)
    adj = next((item for item in adjustments if item.get("id") == adjustment_id), None)
    if not adj:
        raise HTTPException(status_code=404, detail="Adjustment does not exist")
    live_task = _adjustment_tasks.get(project_id)
    if live_task is not None and not live_task.done():
        raise HTTPException(
            status_code=409,
            detail="A live adjustment worker cannot be recovered as orphaned",
        )
    active_run = adj.get("active_run")
    if not isinstance(active_run, dict) or active_run.get("status") not in {
        "queued", "executing", "failed", "interrupted", "recovery_required",
    }:
        raise HTTPException(
            status_code=409,
            detail="Adjustment has no recoverable orphaned generation",
        )
    marker = get_project_write_fence(project_id, Path(ctx.workspace))
    if marker:
        revoke_project_write_fence_generation(
            project_id,
            Path(ctx.workspace),
            expected_owner=f"adjustment:{adjustment_id}",
            expected_purpose="adjustment",
            expected_lock_id=str(active_run.get("fence_lock_id") or ""),
        )
    _adjustment_tasks.pop(project_id, None)
    _adjustment_run_ids.pop(project_id, None)
    _adjustment_fence_tokens.pop(project_id, None)
    try:
        recovery_fence = acquire_project_write_fence(
            project_id,
            Path(ctx.workspace),
            owner=f"adjustment-recovery:{adjustment_id}",
            purpose="adjustment_recovery",
        )
    except ProjectWriteFenceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    recovery_token = str(recovery_fence["token"])
    try:
        with project_write_guard(
            project_id,
            Path(ctx.workspace),
            recovery_token,
        ):
            restored_manifest = _restore_adjustment_snapshot(
                ctx,
                adj.get("pre_run_snapshot"),
            )
        active_run["status"] = "revoked"
        active_run["revoked_at"] = time.time()
        active_run["workspace_restored"] = True
        active_run["restored_artifact_sha256"] = restored_manifest.get(
            "artifact_sha256"
        )
        for task in adj.get("tasks") or []:
            if task.get("status") in {"executing", "failed"}:
                task["status"] = "pending"
            task.pop("success_receipt", None)
        for agent in ctx.agents.values():
            if (
                agent.get("source") == "adjustment"
                and agent.get("subproject_id") == adjustment_id
                and agent.get("status") != "completed"
            ):
                agent["status"] = "revoked"
        adj["status"] = "pending_confirm"
        adj["updated_at"] = time.time()
        await _persist_all_async()
    finally:
        release_project_write_fence(
            project_id,
            Path(ctx.workspace),
            recovery_token,
        )
    return {
        "success": True,
        "adjustment_id": adjustment_id,
        "revoked_run_id": active_run.get("run_id"),
        "status": "pending_confirm",
    }


@router.get("/projects/{project_id}/adjustments/{adjustment_id}")
async def get_adjustment(project_id: str, adjustment_id: str):
    """获取单个变更工单详情（含执行日志和质检结果）"""
    _get_project(project_id)
    adjustments = _get_adjustments(project_id)
    adj = next((a for a in adjustments if a["id"] == adjustment_id), None)
    if not adj:
        raise HTTPException(status_code=404, detail="变更工单不存在")
    return adj

@router.delete("/projects/{project_id}/adjustments/{adjustment_id}")
async def cancel_adjustment(project_id: str, adjustment_id: str):
    """取消变更工单"""
    ctx = _get_project(project_id)
    adjustments = _get_adjustments(project_id)
    adj = next((a for a in adjustments if a["id"] == adjustment_id), None)
    if not adj:
        raise HTTPException(status_code=404, detail="变更工单不存在")
    try:
        with project_write_guard(project_id, Path(ctx.workspace)):
            blocker = _adjustment_cancel_blocker(ctx, adj)
            if blocker:
                raise HTTPException(status_code=409, detail=blocker)
            adj["status"] = "cancelled"
            adj["updated_at"] = time.time()
    except ProjectWriteFenceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _persist_all_async()
    return {"success": True}

# ─── 调试 / 测试工具接口 ──────────────────────────────────────────────────────

class InjectQCRequest(BaseModel):
    qc_results: Dict  # { sp_id: { issues_detail: [...], ... } }

# ─── 最终整体质检 ─────────────────────────────────────────────────────────────

# 存储最终质检进度：project_id → {status, round, issues, logs, ...}
_final_qa_status: Dict[str, Dict] = {}
_final_qa_tasks: Dict[str, asyncio.Task] = {}
_final_qa_api_configs: Dict[str, Optional[Dict[str, Any]]] = {}
_final_qa_fence_tokens: Dict[str, str] = {}
_final_qa_pre_registrations: Dict[str, Dict[str, Any]] = {}
_final_qa_start_mutexes: Dict[str, Tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}
_final_qa_registration_cas_guard = threading.RLock()
_FINAL_QA_REGISTRATION_ANY = object()


def _final_qa_start_mutex(project_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    current = _final_qa_start_mutexes.get(project_id)
    if current is None or current[0] is not loop:
        current = (loop, asyncio.Lock())
        _final_qa_start_mutexes[project_id] = current
    return current[1]


def _final_qa_entry(ctx: ProjectContext) -> Dict[str, Any]:
    whole_project = ctx.qc_results.setdefault("__whole_project__", {})
    return whole_project.setdefault("qa", {}) if isinstance(whole_project, dict) else {}


def _durable_fence_identity(fence: Dict[str, Any]) -> Dict[str, Any]:
    """Persist exact generation identity but never the bearer capability."""
    return {
        "owner": str(fence.get("owner") or ""),
        "purpose": str(fence.get("purpose") or ""),
        "lock_id": str(fence.get("lock_id") or ""),
        "token_digest": str(fence.get("token_sha256") or ""),
        "leased_until": fence.get("leased_until"),
    }


def _persist_final_qa_registration(
    ctx: ProjectContext,
    record: Dict[str, Any],
    *,
    expected_registration_id: object = _FINAL_QA_REGISTRATION_ANY,
) -> bool:
    """Compare-and-set one durable construction generation in memory."""
    with _final_qa_registration_cas_guard:
        entry = _final_qa_entry(ctx)
        current = entry.get("final_qa_registration")
        current_id = (
            str(current.get("registration_id") or "")
            if isinstance(current, dict)
            else None
        )
        if expected_registration_id is None:
            if current_id is not None:
                return False
        elif (
            expected_registration_id is not _FINAL_QA_REGISTRATION_ANY
            and current_id != str(expected_registration_id)
        ):
            return False
        entry["final_qa_registration"] = {
            key: copy.deepcopy(value)
            for key, value in record.items()
            if key != "fence"
        }
        return True


def _clear_final_qa_registration(
    ctx: ProjectContext, registration_id: str
) -> bool:
    with _final_qa_registration_cas_guard:
        entry = _final_qa_entry(ctx)
        current = entry.get("final_qa_registration")
        if (
            not isinstance(current, dict)
            or current.get("registration_id") != registration_id
        ):
            return False
        entry.pop("final_qa_registration", None)
        return True


def _durably_compare_set_final_qa_registration(
    ctx: ProjectContext,
    record: Dict[str, Any],
    *,
    expected_registration_id: Optional[str],
) -> bool:
    """Persist a registration only if its expected generation still owns it."""
    with _final_qa_registration_cas_guard:
        if not _persist_final_qa_registration(
            ctx,
            record,
            expected_registration_id=expected_registration_id,
        ):
            return False
        _persist_all()
        return True


def _durably_compare_delete_final_qa_registration(
    ctx: ProjectContext,
    registration_id: str,
) -> bool:
    """Delete and persist only the caller's exact registration generation."""
    with _final_qa_registration_cas_guard:
        if not _clear_final_qa_registration(ctx, registration_id):
            return False
        _persist_all()
        return True


def _durably_promote_final_qa_registration(
    ctx: ProjectContext,
    registration_id: str,
    status: Dict[str, Any],
) -> bool:
    """Atomically replace one registration with its durable running record."""
    with _final_qa_registration_cas_guard:
        entry = _final_qa_entry(ctx)
        current = entry.get("final_qa_registration")
        if (
            not isinstance(current, dict)
            or current.get("registration_id") != registration_id
        ):
            return False
        entry.pop("final_qa_registration", None)
        entry["final_qa_run"] = json.loads(
            json.dumps(status, ensure_ascii=False)
        )
        _persist_all()
        return True


def _final_qa_construction_record(
    ctx: ProjectContext,
    *,
    registration_id: str,
    owner: str,
    state: str,
    artifact_digest: str,
    required_paths: List[str],
    previous_status: Optional[Dict[str, Any]] = None,
    previous_whole_project_qc: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "registration_id": registration_id,
        "project_id": ctx.project_id,
        "state": state,
        "artifact_digest": artifact_digest,
        "required_paths": list(required_paths),
        "write_fence": {
            "owner": owner,
            "purpose": "final_qa",
            "lock_id": "",
            "token_digest": "",
            "leased_until": None,
        },
        "registered_at": time.time(),
        "had_final_qa_status": previous_status is not None,
        "previous_final_qa_status": copy.deepcopy(previous_status),
        "had_whole_project_qc": previous_whole_project_qc is not None,
        "previous_whole_project_qc": copy.deepcopy(
            previous_whole_project_qc
        ),
    }


def _reset_final_qa_fix_budget(ctx: ProjectContext) -> None:
    """Give a newly triggered whole-project QA cycle its own repair budget."""
    from api.routes_execution import _fix_attempt_counts, _persist_execution_state

    for agent_id in ctx.agents:
        _fix_attempt_counts.pop(agent_id, None)
    _persist_execution_state()

def _final_qa_round_limit() -> int:
    return MAX_BUSINESS_QA_ROUNDS


MAX_FINAL_QC_ROUNDS = _final_qa_round_limit()


def _final_qa_rework_failures(results: List[Any]) -> List[str]:
    """Return every failed or invalid rework result."""
    failures: List[str] = []
    for result in results:
        if isinstance(result, BaseException):
            failures.append(str(result) or result.__class__.__name__)
            continue
        if not isinstance(result, dict):
            failures.append(f"invalid rework result: {type(result).__name__}")
            continue
        if result.get("success") is not True:
            detail = result.get("error") or result.get("status") or "agent reported failure"
            failures.append(str(detail))
    return failures


def _final_qa_state_blockers(ctx: ProjectContext) -> List[str]:
    """Validate only current authoritative generations, never historical Agents."""
    blockers: List[str] = []
    for adjustment in _get_adjustments(ctx.project_id):
        if str(adjustment.get("status") or "") != "awaiting_final_qa":
            continue
        blockers.extend(_adjustment_receipt_blockers(ctx, adjustment))
    return blockers


def _final_qa_phase_blockers(phases: List[Dict[str, Any]]) -> List[str]:
    """Unknown, pending, or merely confirmed phase states must fail closed."""
    blockers: List[str] = []
    for index, phase in enumerate(phases):
        phase_id = str(phase.get("phase_id") or phase.get("id") or index + 1)
        state = str(phase.get("status") or "")
        if not phase.get("user_confirmed"):
            blockers.append(f"phase {phase_id}: not confirmed")
        if state != "completed":
            blockers.append(f"phase {phase_id}: {state or 'unknown'}")
    return blockers


def _final_qa_supervisor_blockers(
    ctx: ProjectContext, phases: List[Dict[str, Any]]
) -> List[str]:
    runs = getattr(ctx, "supervisor_quality_runs", {}) or {}
    blockers: List[str] = []
    for index, phase in enumerate(phases):
        phase_id = str(phase.get("phase_id") or phase.get("id") or index + 1)
        run = runs.get(phase_id)
        if not isinstance(run, dict):
            blockers.append(f"phase {phase_id}: missing Supervisor gate")
            continue
        if run.get("status", run.get("state")) != "completed":
            blockers.append(f"phase {phase_id}: Supervisor gate is not completed")
        if (run.get("completion_gate") or {}).get("passed") is not True:
            blockers.append(f"phase {phase_id}: Supervisor gate has not passed")
    return blockers


def validate_final_qa_readiness(
    ctx: ProjectContext,
    phases: List[Dict[str, Any]],
) -> List[str]:
    """Return the authoritative blockers for starting or committing Final QA."""
    project_id = str(getattr(ctx, "project_id", "") or "")
    return (
        _final_qa_phase_blockers(phases)
        + _final_qa_supervisor_blockers(ctx, phases)
        + (
            _adjustment_final_qa_state_blockers(project_id)
            + _final_qa_state_blockers(ctx)
            if project_id else []
        )
    )


def _runtime_acceptance_required() -> bool:
    """Fail closed on Render unless runtime acceptance is explicitly disabled."""
    configured = os.environ.get("RUNTIME_ACCEPTANCE_REQUIRED")
    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    return os.environ.get("RENDER", "").strip().lower() == "true"


def _previous_passed_runtime_result(ctx: ProjectContext) -> Optional[Dict[str, Any]]:
    """Find reusable runtime evidence across whole-project and phase QA runs."""
    preferred_keys = ["__whole_project__"] + [
        key for key in ctx.qc_results if key != "__whole_project__"
    ]
    for key in preferred_keys:
        stored = ctx.qc_results.get(key, {})
        qa_entry = stored.get("qa", stored) if isinstance(stored, dict) else {}
        candidate = (
            qa_entry.get("runtime_acceptance")
            if isinstance(qa_entry, dict)
            else None
        )
        if isinstance(candidate, dict) and candidate.get("passed") is True:
            return candidate
    return None


def _source_issue_is_disproven(issue: Dict[str, Any], workspace: Optional[Path]) -> bool:
    """Reject source claims that the actual checked-in file directly disproves."""
    if not workspace:
        return False
    relative = _safe_runtime_rework_path(str(issue.get("file_path") or ""))
    target = workspace / relative if relative else None
    if not target or not target.is_file():
        return False
    try:
        source = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    claim = "\n".join((
        str(issue.get("message") or ""),
        str(issue.get("fix_hint") or ""),
    ))
    lowered = claim.lower()

    if target.suffix.lower() in {".tsx", ".jsx"} and "react" in lowered:
        has_react_import = bool(re.search(
            r"import\s+(?:\*\s+as\s+)?React(?:\s*,|\s+from)", source
        ))
        if has_react_import and "React." in source and (
            "react.fc" in lowered
            or "未导入" in claim
            or "未使用" in claim
            or "unused" in lowered
        ):
            return True

    if "express" in lowered and "router" in lowered and re.search(
        r"(?:const|let|var)\s+express\s*=\s*require\(\s*['\"]express['\"]\s*\)",
        source,
        re.IGNORECASE,
    ):
        return True

    if "package.json" in lowered and "dotenv" in lowered:
        package = next(
            (
                parent / "package.json"
                for parent in (target.parent, *target.parents)
                if parent == workspace or workspace in parent.parents
                if (parent / "package.json").is_file()
            ),
            None,
        )
        if package:
            try:
                manifest = json.loads(package.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                manifest = {}
            dependencies = {
                **(manifest.get("dependencies") or {}),
                **(manifest.get("devDependencies") or {}),
            }
            if "dotenv" in dependencies:
                return True

    if "auth" in lowered and ("未导出" in claim or "not export" in lowered):
        match = re.search(
            r"require\(\s*['\"](?P<path>\.{1,2}/[^'\"]*auth[^'\"]*)['\"]\s*\)",
            source,
            re.IGNORECASE,
        )
        if match:
            module = (target.parent / match.group("path")).resolve()
            try:
                module.relative_to(workspace.resolve())
            except ValueError:
                return False
            candidates = [module, Path(str(module) + ".js"), module / "index.js"]
            for candidate in candidates:
                if candidate.is_file() and "module.exports" in candidate.read_text(
                    encoding="utf-8", errors="replace"
                ):
                    return True
    return False


def _final_qa_issue_signature(issue: Dict[str, Any]) -> str:
    """Track convergence without treating changing log tails as new defects."""
    stable_id = str(issue.get("id") or issue.get("defect_id") or "").strip().lower()
    if stable_id:
        return f"id:{stable_id}"
    message_head = str(issue.get("message") or "").splitlines()[0].casefold()
    message_head = re.sub(r"\b(?:line|lines)\s*[:#]?\s*\d+(?::\d+)?\b", " ", message_head)
    message_head = re.sub(r"第\s*\d+\s*行", " ", message_head)
    message_head = re.sub(r"\b(?:fails?|failed|failure)\b", " fail ", message_head)
    message_head = re.sub(r"\b(?:errors?|issues?|problems?)\b", " ", message_head)
    message_head = " ".join(sorted(set(re.findall(
        r"[a-z_$][a-z0-9_.$/-]*|[\u4e00-\u9fff]+", message_head
    )))) or "unspecified"
    return "|".join((
        str(issue.get("layer") or "functionality").lower(),
        str(issue.get("file_path") or "").replace("\\", "/").lower(),
        message_head,
    ))


FINAL_QA_ISSUE_FIELDS = (
    "id",
    "severity",
    "file",
    "line",
    "criterion",
    "message",
    "expected",
    "actual",
    "fix",
    "related_files",
)
FINAL_QA_RUNTIME_CRITERIA = frozenset({
    "runtime.install",
    "runtime.build",
    "runtime.test",
    "runtime.startup",
    "runtime.http",
    "runtime.execution",
})


def _normalize_final_qa_issue(
    issue: Dict[str, Any],
    *,
    valid_criteria: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """Return the fixed compact issue contract used outside the QC engine."""
    severity = str(issue.get("severity") or "error").strip().lower()
    if severity not in {"critical", "error", "warning"}:
        severity = "error"

    raw_file = issue.get("file", issue.get("file_path"))
    file_path = _safe_runtime_rework_path(str(raw_file or "")) or None
    raw_line = issue.get("line", issue.get("line_no"))
    try:
        line = int(raw_line) if raw_line is not None else None
    except (TypeError, ValueError):
        line = None
    if line is not None and line <= 0:
        line = None

    raw_criterion = issue.get("criterion", issue.get("criterion_id"))
    criterion = str(raw_criterion).strip() if raw_criterion else None
    if criterion and valid_criteria is not None and criterion not in valid_criteria:
        criterion = None

    expected = issue.get("expected")
    actual = issue.get("actual")
    expected = str(expected).strip() if expected is not None else None
    actual = str(actual).strip() if actual is not None else None
    if not expected or not actual:
        expected = None
        actual = None

    related_files: List[str] = []
    for raw_path in issue.get("related_files") or []:
        path = _safe_runtime_rework_path(str(raw_path or ""))
        if path and path != file_path and path not in related_files:
            related_files.append(path)
        if len(related_files) == 3:
            break

    identity_source = {
        **issue,
        "file_path": file_path or "",
        "line": line,
        "criterion_id": criterion or "",
    }
    defect_id = str(
        issue.get("defect_id")
        or issue.get("id")
        or canonical_defect_id(identity_source)
    ).strip()
    normalized = {
        "id": defect_id,
        "severity": severity,
        "file": file_path,
        "line": line,
        "criterion": criterion,
        "message": str(issue.get("message") or "").strip()[:160],
        "expected": expected[:120] if expected else None,
        "actual": actual[:240] if actual else None,
        "fix": (
            str(issue.get("fix", issue.get("fix_hint"))).strip()
            if issue.get("fix", issue.get("fix_hint")) is not None
            else ""
        )[:200] or None,
        "related_files": related_files,
    }
    if not normalized["id"] or not normalized["message"]:
        raise ValueError("Final QA issue requires non-empty id and message")
    return normalized


def _final_qa_manual_issue(
    issue: Dict[str, Any], reason: str = ""
) -> Dict[str, Any]:
    """Return one stable, lossless issue shape for every manual handoff."""
    return mark_needs_manual(
        issue,
        reason or str(issue.get("needs_manual_reason") or "Final QA requires manual repair"),
        defaults={
            "subproject_id": str(
                issue.get("subproject_id") or "__whole_project__"
            ),
            "detected_phase": "final",
        },
    )


def _final_qa_manual_issues(
    issues: List[Dict[str, Any]], reason: str
) -> List[Dict[str, Any]]:
    return [_final_qa_manual_issue(issue, reason) for issue in issues]


def _apply_final_qa_external_issues(
    qc_entry: Dict[str, Any],
    external_issues: List[Dict[str, Any]],
) -> None:
    """Merge deterministic external failures and keep verdict text consistent."""
    ledger = list(qc_entry.get("issues_detail") or [])
    by_key = {
        str(item.get("id") or f"{item.get('file_path')}|{item.get('message')}"): index
        for index, item in enumerate(ledger)
    }
    for issue in external_issues:
        item = dict(issue)
        item["status"] = "open"
        key = str(item.get("id") or f"{item.get('file_path')}|{item.get('message')}")
        if key in by_key:
            ledger[by_key[key]] = item
        else:
            by_key[key] = len(ledger)
            ledger.append(item)

    active = [
        item for item in ledger
        if str(item.get("status") or "open") in {"open", "needs_manual"}
    ]
    blocking = sum(
        1 for item in active
        if str(item.get("severity") or "error").lower() != "warning"
    )
    warnings = len(active) - blocking
    score = max(0, 100 - blocking * 10 - warnings * 2)
    qc_entry["issues_detail"] = ledger
    qc_entry["issues"] = [str(item.get("message") or "") for item in active]
    qc_entry["passed"] = blocking == 0
    qc_entry["status"] = "passed" if blocking == 0 else "failed"
    qc_entry["score"] = score
    qc_entry["error_count"] = blocking
    qc_entry["warning_count"] = warnings

    verdict = (
        f"**总体结论**：{'✅ 通过' if blocking == 0 else '❌ 未通过'}"
        f"  |  **综合评分**：{score}/100"
    )
    report = str(qc_entry.get("user_report") or "")
    pattern = re.compile(
        r"^\*\*总体结论\*\*：.*?\*\*综合评分\*\*：\d+/100\s*$",
        re.MULTILINE,
    )
    qc_entry["user_report"] = (
        pattern.sub(verdict, report, count=1)
        if pattern.search(report)
        else verdict + ("\n\n" + report if report else "")
    )


def _final_qa_issue_paths(issue: Dict[str, Any]) -> List[str]:
    """Return the primary defect path plus concrete dependencies named by QA."""
    candidates = [str(issue.get("file", issue.get("file_path")) or "")]
    candidates.extend(str(path) for path in issue.get("related_files") or [])
    candidates.extend(collect_required_file_paths(
        str(issue.get("fix", issue.get("fix_hint")) or "")
    ))
    paths: List[str] = []
    for candidate in candidates:
        path = _safe_runtime_rework_path(candidate)
        parts = Path(path).parts if path else ()
        if path and not Path(path).is_absolute() and ".." not in parts and path not in paths:
            paths.append(path)
    return paths


def _format_final_qa_rework_issue(issue: Dict[str, Any]) -> str:
    """Format a defect with an exact path the execution agent can parse."""
    target_paths = _final_qa_issue_paths(issue)
    text = (
        f"- [{str(issue.get('severity') or 'error').upper()}] "
        f"{issue.get('message', '')}"
    )
    if target_paths:
        text += f"\n  Target file: `{target_paths[0]}`"
        for path in target_paths[1:]:
            text += f"\n  Related target file: `{path}`"
    fix = issue.get("fix", issue.get("fix_hint"))
    if fix:
        text += f"\n  Repair guidance: {fix}"
    return text


def _final_qa_rework_scope(
    agent_info: Dict[str, Any], issues: List[Dict[str, Any]]
) -> List[str]:
    """Lease only the exact files in one rework batch.

    Historical agent scopes can overlap after later integration phases change
    ownership. Exact issue paths keep parallel final-QA repairs disjoint.
    """
    paths: List[str] = []
    for issue in issues:
        for path in _final_qa_issue_paths(issue):
            if path not in paths:
                paths.append(path)
    return paths


class _FinalQAReworkSnapshot(dict):
    def __init__(
        self,
        *args,
        workspace: Path,
        recovery_snapshot: Dict[str, Any],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.workspace = Path(workspace)
        self.recovery_snapshot = recovery_snapshot


def _snapshot_final_qa_rework_files(
    workspace: Optional[Path], issues: List[Dict[str, Any]]
) -> Dict[Path, Optional[bytes]]:
    """Capture the complete canonical delivery artifact before one repair batch."""
    if not workspace:
        return {}
    root = Path(workspace).resolve()
    manifest, files = collect_delivery_artifact(root)
    snapshot: Dict[Path, Optional[bytes]] = {
        (root / relative).resolve(): content
        for relative, content in files.items()
    }
    for issue in issues:
        for relative in _final_qa_issue_paths(issue):
            target = (root / relative).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            if target not in snapshot:
                snapshot[target] = None
    return _FinalQAReworkSnapshot(
        snapshot,
        workspace=root,
        recovery_snapshot=_serialize_adjustment_snapshot(manifest, files),
    )


def _restore_final_qa_rework_snapshot(
    snapshot: Dict[Path, Optional[bytes]],
) -> None:
    """Restore and verify the complete artifact, deleting newly-created files."""
    if isinstance(snapshot, _FinalQAReworkSnapshot):
        _restore_adjustment_snapshot(
            SimpleNamespace(workspace=snapshot.workspace),
            snapshot.recovery_snapshot,
        )
        return
    for target, content in snapshot.items():
        if content is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)


def _serialize_final_qa_rework_snapshot(
    workspace: Path,
    snapshot: Dict[Path, Optional[bytes]],
) -> Dict[str, Optional[str]]:
    root = workspace.resolve()
    if isinstance(snapshot, _FinalQAReworkSnapshot):
        return {
            "schema_version": 2,
            "recovery_snapshot": snapshot.recovery_snapshot,
            "missing_paths": [
                target.resolve().relative_to(root).as_posix()
                for target, content in snapshot.items()
                if content is None
            ],
        }
    return {
        target.resolve().relative_to(root).as_posix(): (
            None if content is None else base64.b64encode(content).decode("ascii")
        )
        for target, content in snapshot.items()
    }


def _deserialize_final_qa_rework_snapshot(
    workspace: Path,
    snapshot: object,
) -> Dict[Path, Optional[bytes]]:
    if not isinstance(snapshot, dict):
        return {}
    root = workspace.resolve()
    if snapshot.get("schema_version") == 2:
        recovery = snapshot.get("recovery_snapshot")
        _, files = _decode_adjustment_snapshot(recovery)
        restored = {
            (root / relative).resolve(): content
            for relative, content in files.items()
        }
        for relative in snapshot.get("missing_paths") or []:
            target = (root / str(relative)).resolve()
            target.relative_to(root)
            restored[target] = None
        return _FinalQAReworkSnapshot(
            restored,
            workspace=root,
            recovery_snapshot=recovery,
        )
    restored: Dict[Path, Optional[bytes]] = {}
    for relative, encoded in snapshot.items():
        target = (root / str(relative)).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("persisted Final QA snapshot has an unsafe path") from exc
        restored[target] = (
            None if encoded is None else base64.b64decode(str(encoded), validate=True)
        )
    return restored


def _validate_final_qa_fence(
    project_id: str,
    workspace: Path,
    token: str,
    revoked: Optional[threading.Event] = None,
) -> None:
    if revoked is not None and revoked.is_set():
        raise ProjectWriteFenceConflict("Final QA repair generation was revoked")
    with project_write_guard(project_id, workspace, token):
        return


def _final_qa_snapshot_changed(
    snapshot: Dict[Path, Optional[bytes]],
) -> bool:
    """Return true only when at least one targeted file actually changed."""
    return any(
        (target.read_bytes() if target.is_file() else None) != before
        for target, before in snapshot.items()
    )


_NON_SOURCE_REWORK_PARTS = {
    ".git", ".project", "node_modules", "dist", "build", "output", "__pycache__"
}


def _runtime_failure_actionable(result: Dict[str, Any]) -> bool:
    category = str(result.get("error_category") or "")
    if category.startswith("infrastructure") or category == "environment_misconfigured":
        return False
    return bool(result.get("actionable", True))


def _safe_runtime_rework_path(path: str) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    candidate = PurePosixPath(raw)
    if not raw or candidate.is_absolute() or ".." in candidate.parts:
        return ""
    normalized = candidate.as_posix()
    parts = {part.casefold() for part in candidate.parts}
    return "" if parts & _NON_SOURCE_REWORK_PARTS else normalized


def _runtime_acceptance_issue(
    result: Dict[str, Any],
    *,
    scope_files: Optional[Dict[str, Dict[str, Any]]] = None,
    valid_criteria: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """Convert a deterministic runtime failure into a normal rework issue."""
    import hashlib

    stage = str(result.get("status") or "failed")
    summary = str(result.get("summary") or "Runtime acceptance did not pass").strip()
    raw_logs = result.get("logs") if isinstance(result.get("logs"), list) else []
    build_logs = [
        str(line).strip()
        for line in raw_logs
        if str(line).strip().startswith("build:")
    ]
    diagnostic_pattern = re.compile(
        r"(?:syntaxerror|typeerror|referenceerror|cannot find|not found|failed|failure|"
        r"expected|received|test suites|tests:|/app/[^\s:]+\.(?:test|spec)\.[^\s:]+:\d+)",
        re.IGNORECASE,
    )
    relative_test_frame = re.compile(
        r"(?:^|[\s(])(?:tests/|backend/tests/)[^\s():]+\.(?:test|spec)\.[^\s():]+:\d+",
        re.IGNORECASE,
    )
    diagnostic_logs = [
        line for line in build_logs
        if diagnostic_pattern.search(line) or relative_test_frame.search(line)
    ]
    selected_logs = list(dict.fromkeys(diagnostic_logs[-18:] + build_logs[-4:]))
    evidence = "\n".join(selected_logs)
    message = f"Isolated runtime acceptance failed during {stage}: {summary}"
    if evidence:
        message += f"\nRecent isolated build output:\n{evidence}"
    message = message[:4000]
    file_path = _safe_runtime_rework_path(str(result.get("file_path") or ""))
    if not file_path:
        for line in build_logs:
            match = re.search(r"/app/([^\s:]+):\d+", line)
            if match:
                file_path = _safe_runtime_rework_path(match.group(1))
                break
    if not file_path:
        # Jest stack frames commonly omit the Docker workspace prefix. Route the
        # issue to the actual failing test instead of package.json so the owning
        # agent receives an actionable file and can repair it automatically.
        for line in build_logs:
            match = re.search(
                r"\(([^()\s]+\.(?:test|spec)\.[^():\s]+):\d+(?::\d+)?\)",
                line,
                re.IGNORECASE,
            )
            if match:
                file_path = _safe_runtime_rework_path(match.group(1))
                if file_path.startswith("tests/"):
                    file_path = "backend/" + file_path
                break
    if not file_path:
        for line in build_logs:
            match = re.search(
                r"((?:frontend/|backend/)?src/[^\s(]+\.(?:ts|tsx|js|jsx))"
                r"\(\d+,\d+\):\s*error\s+TS\d+",
                line,
                re.IGNORECASE,
            )
            if match:
                file_path = _safe_runtime_rework_path(match.group(1))
                if (
                file_path.startswith("src/")
                and (
                    "npm run build --prefix frontend" in "\n".join(build_logs).lower()
                    or "[frontend-build" in "\n".join(build_logs).lower()
                    or "workdir /app/frontend" in "\n".join(build_logs).lower()
                )
                ):
                    file_path = "frontend/" + file_path
                break
    if (
        not file_path
        and "workspace is missing the release dockerfile" in summary.casefold()
    ):
        # Snapshot validation fails before a deploy exists, so this result has
        # no build log or stack frame from which to infer a target.  Keep the
        # release artifact itself in the normal rework/lease/audit path instead
        # of falling through to the unrelated package.json default.
        file_path = "Dockerfile"
    if (
        not file_path
        and stage == "build_failed"
        and (
            "failed to solve" in evidence.lower()
            or "dockerfile" in evidence.lower()
            or 'process "/bin/sh -c' in evidence.lower()
        )
    ):
        file_path = "Dockerfile"
    file_path = _safe_runtime_rework_path(file_path)
    if scope_files is None:
        file_path = file_path or "package.json"
    fix_hint = str(result.get("fix_hint") or "").strip() or (
        "Fix the failing clean install, tests, build, startup, or API contract and rerun final QA."
    )
    lowered_evidence = evidence.lower()
    missing_module = re.search(
        r"cannot find module\s+['\"](\.{1,2}/[^'\"]+)['\"]",
        evidence,
        re.IGNORECASE,
    )
    if missing_module and file_path != "package.json":
        candidate = str(PurePosixPath(file_path).parent / missing_module.group(1))
        if not PurePosixPath(candidate).suffix:
            candidate += ".js"
        candidate = _safe_runtime_rework_path(candidate)
        if candidate and ".." not in PurePosixPath(candidate).parts:
            fix_hint += (
                f" Create the required module `{candidate}` or correct the import in "
                f"`{file_path}` if that module name is wrong."
            )
    if "received constructor: array" in lowered_evidence and "tobeinstanceof" in lowered_evidence:
        fix_hint += (
            " Jest ESM can create cross-realm arrays; replace `.toBeInstanceOf(Array)` "
            "with an `Array.isArray(...)` assertion."
        )
    if re.search(r"req\.(?:end|connect) is not a function", lowered_evidence):
        fix_hint += (
            " Node `http.createServer()` returns a Server, not a ClientRequest; "
            "use `http.get()` or `http.request()` for the backend readiness probe."
        )
    if (
        "referenceerror: http is not defined" in lowered_evidence
        or "createserver" in lowered_evidence and ".request is not a function" in lowered_evidence
    ):
        fix_hint += (
            " Import `{ get }` from `node:http` and call `get(url, callback)` directly; "
            "do not call request/get on a value returned by `createServer()`."
        )
    if "expected: 200" in lowered_evidence and "received: 403" in lowered_evidence:
        fix_hint += (
            " HTTP 403 is an authorization/ownership failure: inspect the failing test setup and "
            "production access rule. If a technician updates a ticket, assign that ticket to the "
            "technician first (or correct the route only if the project contract allows any technician)."
        )
    expected_match = re.search(r"\bexpected:\s*([^\r\n]+)", evidence, re.IGNORECASE)
    actual_match = re.search(r"\breceived:\s*([^\r\n]+)", evidence, re.IGNORECASE)
    location_match = re.search(
        rf"{re.escape(file_path)}:(\d+)(?::\d+)?",
        evidence,
        re.IGNORECASE,
    )
    expected = (
        expected_match.group(1).strip()
        if expected_match
        else "isolated runtime acceptance passes"
    )
    actual = actual_match.group(1).strip() if actual_match else stage
    issue_id = "issue-" + hashlib.sha256(
        f"{file_path}|runtime_acceptance|{message}".encode("utf-8")
    ).hexdigest()[:8]
    legacy_issue = {
        "id": issue_id,
        "message": message,
        "file_path": file_path,
        "responsible_agent_id": "",
        "responsible_agent_role": "",
        "severity": "error",
        "fix_hint": fix_hint,
        "layer": "runtime_acceptance",
        "rule_id": "runtime_acceptance",
        "symbol": "isolated_runtime_gate",
        "location": (
            f"{file_path}:{location_match.group(1)}"
            if location_match
            else file_path
        ),
        "line": int(location_match.group(1)) if location_match else None,
        "line_no": int(location_match.group(1)) if location_match else None,
        "expected": expected,
        "actual": actual,
        "evidence": selected_logs,
        "acceptance_criteria": (
            "The canonical delivery artifact must pass the isolated runtime gate."
        ),
        "source": "deterministic_runtime",
        "actionable": True,
        "status": "open",
        "detected_phase": "final",
        "detected_at": time.time(),
    }
    if scope_files is None:
        return legacy_issue

    if file_path not in scope_files:
        file_path = ""
        legacy_issue["file_path"] = ""
        legacy_issue["line"] = None
        legacy_issue["line_no"] = None
    criterion_by_stage = {
        "install_failed": "runtime.install",
        "build_failed": "runtime.build",
        "test_failed": "runtime.test",
        "startup_failed": "runtime.startup",
        "healthcheck_failed": "runtime.http",
        "http_failed": "runtime.http",
    }
    legacy_issue["criterion"] = criterion_by_stage.get(stage, "runtime.execution")
    return _normalize_final_qa_issue(
        legacy_issue,
        valid_criteria=valid_criteria,
    )


def _store_runtime_acceptance_result(
    ctx: ProjectContext,
    status: Dict[str, Any],
    result: Dict[str, Any],
    *,
    persist: bool = True,
) -> None:
    """Expose live evidence and persist terminal evidence across restarts."""
    status["runtime_acceptance"] = result
    status["artifact_digest"] = result.get("artifact_digest") or result.get("artifact_sha256")
    status["rule_version"] = result.get("rule_version")
    status["error_category"] = result.get("error_category")
    status["retryable"] = bool(result.get("retryable"))
    if persist:
        existing = ctx.qc_results.setdefault("__whole_project__", {})
        qa_entry = existing.setdefault("qa", {}) if isinstance(existing, dict) else {}
        if isinstance(qa_entry, dict):
            qa_entry["runtime_acceptance"] = result


def _store_final_qa_run_status(ctx: ProjectContext, status: Dict[str, Any]) -> None:
    persisted = ctx.qc_results.setdefault("__whole_project__", {})
    qa_entry = persisted.setdefault("qa", {}) if isinstance(persisted, dict) else {}
    if isinstance(qa_entry, dict):
        qa_entry["final_qa_run"] = json.loads(json.dumps(status, ensure_ascii=False))


def _commit_final_qa_qc_entry(
    ctx: ProjectContext,
    status: Dict[str, Any],
    *,
    project_id: str,
    run_id: str,
    generation: str,
    artifact_sha256: str,
    required_files: List[str],
    subproject_id: str,
    qc_entry: Dict[str, Any],
    fence_token: str,
    owner_task: Optional[asyncio.Task] = None,
) -> bool:
    """Atomically publish one compute-only QC result if its run is current."""
    expected_task = owner_task or asyncio.current_task()
    with project_write_guard(project_id, Path(ctx.workspace), fence_token):
        current_manifest = compute_delivery_manifest(
            Path(ctx.workspace),
            required_paths=required_files,
        )
        if (
            _final_qa_status.get(project_id) is not status
            or status.get("run_id") != run_id
            or status.get("qc_generation") != generation
            or _final_qa_tasks.get(project_id) is not expected_task
            or _final_qa_fence_tokens.get(project_id) != fence_token
            or current_manifest.get("artifact_sha256") != artifact_sha256
        ):
            return False
        committed = copy.deepcopy(qc_entry)
        committed["final_qa_run_id"] = run_id
        committed["final_qa_generation"] = generation
        committed["artifact_sha256"] = artifact_sha256
        ctx.qc_results[subproject_id] = {"qa": committed}
        return True

async def _run_final_qa_loop(project_id: str):
    """
    异步执行最终整体质检 + 自动返工循环。
    逻辑与阶段质检保持完全一致：
    1. is_final_phase=True 的四层质检（含文件协作/整体落地）
    2. 发现 open 问题 → 按 agent 分组触发返工
    3. 返工完成 → 再次质检（最多 5 轮）
    4. 超出上限 → 标记 needs_manual
    """
    ctx = projects.get(project_id)
    if not ctx:
        return
    fence_token = _final_qa_fence_tokens.get(project_id)
    if not fence_token:
        raise ProjectWriteFenceConflict("Final QA write fence capability is unavailable")

    status = _final_qa_status.setdefault(project_id, {
        "status": "running",
        "round": 0,
        "total_rounds": MAX_FINAL_QC_ROUNDS,
        "logs": [],
        "all_passed": False,
        "needs_manual": [],
        "qc_summary": {},
        "user_reports": [],
        "started_at": time.time(),
        "current_step": "initializing",
        "steps": [],
    })
    run_id = str(status.get("run_id") or uuid.uuid4().hex)
    status["run_id"] = run_id
    owner_task = asyncio.current_task()
    _store_final_qa_run_status(ctx, status)
    await _persist_all_async()

    def log(msg: str):
        status["logs"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        status["updated_at"] = time.time()

    log("🔍 开始最终整体质检（is_final_phase=True）")
    project_context = ctx.pm.context_summary or ctx.description or ""
    user_api_config = _final_qa_api_configs.get(project_id)
    if not user_api_config or not user_api_config.get("api_key"):
        user_api_config = next((
            agents_api_config.get(agent_id)
            for agent_id in ctx.agents
            if (agents_api_config.get(agent_id) or {}).get("api_key")
        ), None)
    workspace = Path(ctx.workspace) if getattr(ctx, "workspace", None) else None
    final_qa_scope = load_final_qa_scope(
        project_id=project_id,
        workspace=workspace,
    )
    if not final_qa_scope.get("available") or final_qa_scope.get("issues"):
        status.update({
            "status": "failed",
            "all_passed": False,
            "failed_reason": "FINAL_QA_SCOPE_INVALID",
            "message": "Authoritative final delivery documents are unavailable or invalid",
            "scope_issues": list(final_qa_scope.get("issues") or []),
            "current_step": "completed",
            "finished_at": time.time(),
        })
        _store_final_qa_run_status(ctx, status)
        await _persist_all_async()
        return
    scope_files = {
        str(item.get("path") or ""): dict(item)
        for item in final_qa_scope.get("files") or []
        if str(item.get("path") or "")
    }
    valid_criteria = {
        str(item.get("criterion") or "")
        for item in final_qa_scope.get("criteria") or []
        if str(item.get("criterion") or "")
    } | set(FINAL_QA_RUNTIME_CRITERIA)

    def run_qc_with_project_config(
        subproject_id: str,
        subproject_name: str,
        *,
        generation: str,
        artifact_sha256: str,
    ):
        from core.hermes_client import current_user_api_config

        config_token = current_user_api_config.set(user_api_config)
        try:
            isolated_ctx = copy.copy(ctx)
            isolated_ctx.qc_results = copy.deepcopy(ctx.qc_results)
            isolated_ctx.agents = copy.deepcopy(ctx.agents)
            isolated_ctx.subprojects = copy.deepcopy(ctx.subprojects)
            return _run_qc_for_subproject(
                isolated_ctx,
                subproject_id,
                subproject_name,
                is_final_phase=True,
                qa_context={
                    "run_id": run_id,
                    "qa_round_id": generation,
                    "scope_digest": artifact_sha256,
                    "artifact_digest": artifact_sha256,
                    "final_qa_scope": final_qa_scope,
                },
            )
        finally:
            current_user_api_config.reset(config_token)

    async def verify_reworked_phases(phase_ids: List[str]) -> List[str]:
        """Re-run deterministic Pre-QA and phase QC before the next Final QA round."""
        from api.routes_phases import (
            _execute_phase_pre_qa,
            _supervisor_scope_snapshot,
        )

        failures: List[str] = []
        phase_manager = _phase_managers.get(project_id)
        for phase_id in phase_ids:
            phase = phase_manager.get_phase(phase_id) if phase_manager else None
            if not isinstance(phase, dict):
                failures.append(f"phase {phase_id}: unavailable after rework")
                continue
            pre_qa = await asyncio.to_thread(
                _execute_phase_pre_qa, ctx, phase_id
            )
            phase["pre_qa_result"] = copy.deepcopy(pre_qa)
            if pre_qa.get("passed") is not True:
                failures.append(f"phase {phase_id}: Pre-QA failed")
                continue
            scope = _supervisor_scope_snapshot(ctx, phase_id)
            qc_result = await asyncio.to_thread(
                _run_qc_for_subproject,
                ctx,
                phase_id,
                str(phase.get("name") or phase_id),
                is_final_phase=False,
                qa_context={
                    "run_id": run_id,
                    "qa_round_id": f"{run_id}:rework:{phase_id}",
                    "artifact_digest": str(scope.get("artifact_digest") or ""),
                },
            )
            if qc_result.get("passed") is not True:
                failures.append(f"phase {phase_id}: Supervisor QC failed")
        return failures
    # 全项目终审只扫描一次完整工作区。按阶段/专家重复扫描会把同一问题
    # 复制成多份，并导致错误的返工归属。
    qa_targets = [{"id": "__whole_project__", "name": f"{ctx.name}（全项目）"}]
    status["total_items"] = len(qa_targets)
    status["completed_items"] = 0
    status["current_item"] = None

    # P0修复：最终质检前强制验证产物存在性
    log("📋 P0验证：检查项目产出文件...")
    code_file_list = sorted(
        path for path in scope_files
        if Path(path).suffix.lower() in FINAL_QA_SOURCE_SUFFIXES
    )
    total_lines = 0
    for relative in code_file_list:
        try:
            total_lines += len(
                (workspace / relative).read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            )
        except OSError:
            pass
    total_code_files = len(code_file_list)

    log(f"📊 产出统计：{total_code_files} 个代码文件，共 {total_lines} 行")

    required_files = _adjustment_required_paths(ctx)
    existing_files = {
        str(path.relative_to(workspace)).replace('\\', '/').lower()
        for path in workspace.rglob('*')
        if workspace and workspace.exists() and path.is_file()
    } if workspace and workspace.exists() else set()
    missing_required_files = [path for path in required_files if path.lower() not in existing_files]
    status["required_files"] = required_files
    status["missing_required_files"] = missing_required_files
    if missing_required_files:
        status["status"] = "failed"
        status["all_passed"] = False
        status["message"] = "终审失败：缺少需求明确指定的交付文件"
        status["failed_reason"] = "MISSING_REQUIRED_FILES"
        log(f"❌ 终审失败：缺少必需文件：{', '.join(missing_required_files)}")
        status["finished_at"] = time.time()
        status["current_step"] = "completed"
        _store_final_qa_run_status(ctx, status)
        await _persist_all_async()
        return

    # P0硬性门槛：必须有代码文件且代码量合理
    if total_code_files == 0:
        status["status"] = "failed"
        status["all_passed"] = False
        status["message"] = "❌ 终审失败：未发现任何代码文件，项目产出为空"
        status["failed_reason"] = "NO_CODE_FILES"
        log("❌ 终审失败：未发现任何代码文件")
        status["finished_at"] = time.time()
        status["current_step"] = "completed"
        _store_final_qa_run_status(ctx, status)
        await _persist_all_async()
        return

    log(f"✅ 产物验证通过：{total_code_files} 个文件，{total_lines} 行代码")
    status["code_files"] = total_code_files
    status["code_lines"] = total_lines
    from core.runtime_acceptance import preflight_runtime_acceptance
    status["current_step"] = "infrastructure_preflight"
    preflight = await asyncio.to_thread(preflight_runtime_acceptance)
    status["steps"].append({
        "name": "infrastructure_preflight",
        "status": "passed" if preflight.get("passed") else "failed",
    })
    if not preflight.get("passed"):
        runtime_result = {
            **preflight,
            "actionable": False,
            "source": "deterministic_runtime",
        }
        _store_runtime_acceptance_result(ctx, status, runtime_result)
        status.update({
            "status": "infrastructure_blocked",
            "all_passed": False,
            "failed_reason": "RUNTIME_INFRASTRUCTURE_BLOCKED",
            "message": preflight.get("summary") or "Runtime acceptance infrastructure is unavailable",
            "error_category": preflight.get("error_category"),
            "retryable": bool(preflight.get("retryable")),
            "action_required": {"options": ["retry_acceptance"]},
            "current_step": "completed",
            "finished_at": time.time(),
        })
        _store_final_qa_run_status(ctx, status)
        await _persist_all_async()
        return

    previous_runtime_result = _previous_passed_runtime_result(ctx)
    status["code_file_list"] = code_file_list[:10]  # 只记录前10个文件

    def package_script_issues() -> List[Dict[str, Any]]:
        import hashlib

        found: List[Dict[str, Any]] = []
        if not workspace or not workspace.exists():
            return found
        for manifest in workspace.rglob("package.json"):
            if "node_modules" in manifest.parts or not manifest.is_file():
                continue
            relative = str(manifest.relative_to(workspace)).replace("\\", "/")
            try:
                package_data = json.loads(manifest.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                message = f"Invalid package.json syntax at line {exc.lineno}: {exc.msg}"
                issue_id = "issue-" + hashlib.sha256(
                    f"{relative}|runtime_contract|{message}".encode("utf-8")
                ).hexdigest()[:8]
                found.append({
                    "id": issue_id,
                    "message": message,
                    "file_path": relative,
                    "responsible_agent_id": "",
                    "responsible_agent_role": "",
                    "severity": "error",
                    "fix_hint": "Rewrite the file as strict JSON with no comments or trailing explanatory text",
                    "layer": "runtime_contract",
                    "status": "open",
                    "detected_phase": "final",
                    "detected_at": time.time(),
                })
                continue
            for script_name, command in (package_data.get("scripts") or {}).items():
                normalized_command = str(command).replace("\\", "/")
                if normalized_command.lstrip().startswith("node ") and "node_modules/.bin/" in normalized_command:
                    message = f"npm script `{script_name}` incorrectly executes a .bin shell shim with node"
                    issue_id = "issue-" + hashlib.sha256(
                        f"{relative}|runtime_contract|{message}".encode("utf-8")
                    ).hexdigest()[:8]
                    found.append({
                        "id": issue_id,
                        "message": message,
                        "file_path": relative,
                        "responsible_agent_id": "",
                        "responsible_agent_role": "",
                        "severity": "error",
                        "fix_hint": "Invoke the local CLI directly, for example `jest`",
                        "layer": "runtime_contract",
                        "status": "open",
                        "detected_phase": "final",
                        "detected_at": time.time(),
                    })
        return found

    previous_issue_signatures: set[str] = set()
    stagnant_rounds = 0
    pending_rework_snapshot: Dict[Path, Optional[bytes]] = {}
    pending_rework_signatures: set[str] = set()
    for round_num in range(1, MAX_FINAL_QC_ROUNDS + 1):
        refreshed_scope = load_final_qa_scope(
            project_id=project_id,
            workspace=workspace,
        )
        if not refreshed_scope.get("available") or refreshed_scope.get("issues"):
            status.update({
                "status": "failed",
                "all_passed": False,
                "failed_reason": "FINAL_QA_SCOPE_INVALID",
                "message": "Authoritative final delivery scope became invalid",
                "scope_issues": list(refreshed_scope.get("issues") or []),
            })
            break
        final_qa_scope = refreshed_scope
        scope_files = {
            str(item.get("path") or ""): dict(item)
            for item in final_qa_scope.get("files") or []
            if str(item.get("path") or "")
        }
        valid_criteria = {
            str(item.get("criterion") or "")
            for item in final_qa_scope.get("criteria") or []
            if str(item.get("criterion") or "")
        } | set(FINAL_QA_RUNTIME_CRITERIA)
        renewed_fence = renew_project_write_fence(
            project_id, Path(ctx.workspace), fence_token, ttl_seconds=15 * 60
        )
        status.setdefault("write_fence", {})["leased_until"] = renewed_fence.get(
            "leased_until"
        )
        status["round"] = round_num
        status["completed_items"] = 0
        status["status"] = f"qc_running_round_{round_num}"
        status["current_step"] = "llm_quality_review"
        with project_write_guard(project_id, Path(ctx.workspace), fence_token):
            qc_manifest = compute_delivery_manifest(
                Path(ctx.workspace),
                required_paths=required_files,
            )
        qc_artifact_sha256 = str(qc_manifest.get("artifact_sha256") or "")
        qc_generation = f"{run_id}:{round_num}:{uuid.uuid4().hex}"
        status["qc_generation"] = qc_generation
        status["qc_artifact_sha256"] = qc_artifact_sha256
        _store_final_qa_run_status(ctx, status)
        await _persist_all_async()
        log(f"━━ 第 {round_num} 轮质检（共 {MAX_FINAL_QC_ROUNDS} 轮）")

        # 对完整项目工作区做一次整体质检
        all_passed = True
        open_issues_by_agent: Dict[str, List[Dict]] = {}  # agent_id → issues
        latest_open_issues: List[Dict] = []

        for sp_index, sp in enumerate(qa_targets):
            sp_id = sp["id"]
            sp_name = sp.get("name", sp_id)
            status["current_item"] = {"id": sp_id, "name": sp_name}
            status["updated_at"] = time.time()
            try:
                loop = asyncio.get_event_loop()
                qc_entry = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda sid=sp_id, sn=sp_name, gen=qc_generation, sha=qc_artifact_sha256: run_qc_with_project_config(
                            sid,
                            sn,
                            generation=gen,
                            artifact_sha256=sha,
                        ),
                    ),
                    timeout=90.0,
                )
            except asyncio.TimeoutError:
                log(f"⚠️ 子项目「{sp_name}」质检超时")
                all_passed = False
                status["user_reports"].append({
                    "round": round_num, "subproject_id": sp_id, "name": sp_name,
                    "passed": False, "score": 0, "error_count": 1,
                    "warning_count": 0, "report": "质检超时，请重试或人工检查。",
                    "checked_at": time.time(),
                })
                status["completed_items"] += 1
                status.update({
                    "status": "infrastructure_blocked",
                    "all_passed": False,
                    "failed_reason": "QC_TIMEOUT",
                    "message": "Final QA provider timed out; retry acceptance",
                    "retryable": True,
                    "action_required": {"options": ["retry_acceptance"]},
                    "current_step": "completed",
                    "finished_at": time.time(),
                })
                _store_final_qa_run_status(ctx, status)
                await _persist_all_async()
                return
            except Exception as e:
                log(f"⚠️ 子项目「{sp_name}」质检异常：{e}")
                all_passed = False
                status["user_reports"].append({
                    "round": round_num, "subproject_id": sp_id, "name": sp_name,
                    "passed": False, "score": 0, "error_count": 1,
                    "warning_count": 0, "report": f"质检执行异常：{e}",
                    "checked_at": time.time(),
                })
                status["completed_items"] += 1
                status.update({
                    "status": "infrastructure_blocked",
                    "all_passed": False,
                    "failed_reason": "QC_EXECUTION_ERROR",
                    "message": f"Final QA provider failed: {e.__class__.__name__}",
                    "retryable": True,
                    "action_required": {"options": ["retry_acceptance"]},
                    "current_step": "completed",
                    "finished_at": time.time(),
                })
                _store_final_qa_run_status(ctx, status)
                await _persist_all_async()
                return

            sp_passed = qc_entry.get("passed", False)
            qc_entry["issues_detail"] = [
                item for item in qc_entry.get("issues_detail", [])
                if not _source_issue_is_disproven(item, workspace)
            ]
            _apply_final_qa_external_issues(qc_entry, [])
            sp_passed = qc_entry.get("passed", False)
            open_issues = [
                i for i in qc_entry.get("issues_detail", [])
                if i.get("status") == "open"
            ]
            runtime_issues = package_script_issues()
            if runtime_issues:
                sp_passed = False
                open_issues.extend(runtime_issues)
                qc_entry["error_count"] = qc_entry.get("error_count", 0) + len(runtime_issues)
                qc_entry["user_report"] = (
                    qc_entry.get("user_report", "")
                    + "\n\n**Runtime contract failures:**\n"
                    + "\n".join(
                        f"- [{issue['file_path']}] {issue['message']}"
                        for issue in runtime_issues
                    )
                )

            # LLM/static review is necessary but not sufficient.  Only run the
            # expensive external gate once those checks are green.  Generated
            # code is built and executed in a dedicated Render service, never
            # inside the MeTis web process that holds production secrets.
            if (
                sp_passed
                and all_passed
                and sp_index == len(qa_targets) - 1
            ):
                from core.runtime_acceptance import run_runtime_acceptance

                log("Running artifact-bound runtime acceptance...")
                existing_runtime_result = qc_entry.get(
                    "runtime_acceptance"
                )
                reusable_runtime_result = (
                    existing_runtime_result
                    if isinstance(existing_runtime_result, dict)
                    and existing_runtime_result.get("passed") is True
                    else previous_runtime_result
                )
                try:
                    runtime_contract = (
                        getattr(
                            _phase_managers.get(project_id),
                            "project_contract",
                            None,
                        )
                        or None
                    )
                    runtime_result = await asyncio.to_thread(
                        run_runtime_acceptance,
                        workspace,
                        project_id,
                        reusable_runtime_result,
                        required_files,
                        runtime_contract,
                    )
                except Exception as exc:
                    runtime_result = {
                        "enabled": True,
                        "passed": False,
                        "status": "error",
                        "summary": f"Runtime acceptance raised {exc.__class__.__name__}: {exc}",
                        "error_category": "infrastructure_provider_error",
                        "retryable": True,
                        "actionable": False,
                    }
                _store_runtime_acceptance_result(
                    ctx,
                    status,
                    runtime_result,
                    persist=False,
                )
                qc_entry["runtime_acceptance"] = runtime_result
                required = _runtime_acceptance_required()
                if required and not runtime_result.get("enabled"):
                    status["status"] = "failed"
                    status["all_passed"] = False
                    status["failed_reason"] = "RUNTIME_ACCEPTANCE_UNAVAILABLE"
                    status["message"] = "Runtime acceptance is required but not configured"
                    log("Final QA stopped: isolated runtime acceptance is unavailable")
                    status["finished_at"] = time.time()
                    status["current_item"] = None
                    status["current_step"] = "completed"
                    _store_final_qa_run_status(ctx, status)
                    await _persist_all_async()
                    return
                if runtime_result.get("enabled") and not runtime_result.get("passed"):
                    if not _runtime_failure_actionable(runtime_result):
                        status.update({
                            "status": "infrastructure_blocked",
                            "all_passed": False,
                            "failed_reason": "RUNTIME_INFRASTRUCTURE_BLOCKED",
                            "message": runtime_result.get("summary") or "Runtime acceptance infrastructure failed",
                            "error_category": runtime_result.get("error_category"),
                            "retryable": bool(runtime_result.get("retryable")),
                            "action_required": {"options": ["retry_acceptance"]},
                            "current_step": "completed",
                            "finished_at": time.time(),
                        })
                        _store_final_qa_run_status(ctx, status)
                        await _persist_all_async()
                        return
                    runtime_issue = _runtime_acceptance_issue(
                        runtime_result,
                        scope_files=scope_files,
                        valid_criteria=valid_criteria,
                    )
                    runtime_issues.append(runtime_issue)
                    open_issues.append(runtime_issue)
                    sp_passed = False
                    qc_entry["passed"] = False
                    qc_entry["error_count"] = qc_entry.get("error_count", 0) + 1
                    qc_entry["user_report"] = (
                        qc_entry.get("user_report", "")
                        + "\n\n**Isolated runtime acceptance failure:**\n"
                        + f"- {runtime_issue['message']}"
                    )
            _apply_final_qa_external_issues(qc_entry, runtime_issues)
            if not _commit_final_qa_qc_entry(
                ctx,
                status,
                project_id=project_id,
                run_id=run_id,
                generation=qc_generation,
                artifact_sha256=qc_artifact_sha256,
                required_files=required_files,
                subproject_id=sp_id,
                qc_entry=qc_entry,
                fence_token=fence_token,
                owner_task=owner_task,
            ):
                # A newer run/generation or artifact owns persistence now.
                # The compute-only result remains isolated and is discarded.
                return
            sp_passed = bool(qc_entry.get("passed", False))
            status["qc_summary"][sp_id] = {
                "name": sp_name,
                "passed": sp_passed,
                "open_count": len(open_issues),
                "score": qc_entry.get("score", 0),
            }
            status["user_reports"].append({
                "round": round_num,
                "subproject_id": sp_id,
                "name": sp_name,
                "passed": sp_passed,
                "score": qc_entry.get("score", 0),
                "error_count": qc_entry.get("error_count", 0),
                "warning_count": qc_entry.get("warning_count", 0),
                "report": qc_entry.get("user_report", ""),
                "checked_at": time.time(),
            })
            status["completed_items"] += 1
            status["updated_at"] = time.time()
            if not sp_passed:
                all_passed = False
                normalized_open_issues: List[Dict[str, Any]] = []
                for issue in open_issues:
                    try:
                        normalized = _normalize_final_qa_issue(
                            issue,
                            valid_criteria=valid_criteria,
                        )
                    except (TypeError, ValueError):
                        normalized = _normalize_final_qa_issue({
                            "severity": "error",
                            "message": "Final QA returned an invalid structured issue",
                        })
                    normalized_open_issues.append(normalized)
                    owner = scope_files.get(str(normalized.get("file") or ""), {})
                    agent_id = str(owner.get("agent_id") or "")
                    if agent_id:
                        open_issues_by_agent.setdefault(agent_id, []).append(normalized)
                open_issues = normalized_open_issues
                latest_open_issues.extend(normalized_open_issues)
            log(f"  {'✅' if sp_passed else '❌'} {sp_name}：{len(open_issues)} 个问题")

        status["active_issues"] = latest_open_issues

        # A repair batch is allowed to trigger the next QC round only when it
        # actually shrank the previous blocker set.  If it introduced a new
        # blocker or failed to resolve any old blocker, restore the exact
        # pre-repair bytes and stop for manual intervention.
        if pending_rework_signatures and not all_passed:
            current_signatures = {
                _final_qa_issue_signature(issue) for issue in latest_open_issues
            }
            resolved_signatures = pending_rework_signatures - current_signatures
            new_signatures = current_signatures - pending_rework_signatures
            if new_signatures or not resolved_signatures:
                try:
                    _restore_final_qa_rework_snapshot(pending_rework_snapshot)
                    rollback_message = "Final QA rework regressed or made no progress; workspace restored"
                except Exception as exc:
                    rollback_message = (
                        "Final QA rework regressed and snapshot restore failed: "
                        f"{exc.__class__.__name__}"
                    )
                status.update({
                    "status": "needs_manual",
                    "all_passed": False,
                    "needs_manual": _final_qa_manual_issues(
                        latest_open_issues, rollback_message
                    ),
                    "action_required": {"options": ["manual_fix", "rebuild_phase"]},
                    "regression": bool(new_signatures),
                    "rollback_message": rollback_message,
                    "current_step": "completed",
                    "finished_at": time.time(),
                })
                log(rollback_message)
                _store_final_qa_run_status(ctx, status)
                await _persist_all_async()
                return
            pending_rework_snapshot = {}
            pending_rework_signatures = set()

        if all_passed:
            phase_manager = _phase_managers.get(project_id)
            current_phases = phase_manager.phases if phase_manager else []
            state_blockers = validate_final_qa_readiness(
                ctx, current_phases
            )
            if state_blockers:
                status["all_passed"] = False
                status["status"] = "failed"
                status["failed_reason"] = "INCONSISTENT_EXECUTION_STATE"
                status["message"] = "Final QA checks passed, but execution state is not clean"
                status["state_blockers"] = state_blockers
                log("Final QA stopped: " + " | ".join(state_blockers[:5]))
                break
            with project_write_guard(project_id, Path(ctx.workspace), fence_token):
                delivery_manifest = compute_delivery_manifest(
                    Path(ctx.workspace), required_paths=required_files
                )
                workspace_digest = compute_workspace_digest(Path(ctx.workspace))
            persisted = ctx.qc_results.setdefault("__whole_project__", {})
            qa_entry = persisted.setdefault("qa", {}) if isinstance(persisted, dict) else {}
            if not isinstance(qa_entry, dict):
                status["all_passed"] = False
                status["status"] = "failed"
                status["failed_reason"] = "INVALID_QA_PERSISTENCE"
                status["message"] = "Final QA result could not be bound to the workspace"
                log("Final QA stopped: invalid persisted QA entry")
                break
            if not runtime_artifact_matches(
                qa_entry.get("runtime_acceptance"), delivery_manifest
            ):
                qa_entry["passed"] = False
                qa_entry["status"] = "failed"
                status["all_passed"] = False
                status["status"] = "failed"
                status["failed_reason"] = "RUNTIME_ARTIFACT_MISMATCH"
                status["message"] = (
                    "Runtime acceptance does not prove the current delivery artifact"
                )
                log("Final QA stopped: runtime artifact binding is stale or unrelated")
                break
            qa_entry["criterion_evidence"] = (
                _build_adjustment_criterion_evidence(
                    project_id,
                    qa_entry=qa_entry,
                    manifest=delivery_manifest,
                    run_id=run_id,
                    qa_generation=qc_generation,
                )
            )
            adjustment_blockers, adjustment_bindings = (
                _pending_adjustment_acceptance(
                    project_id,
                    delivery_manifest,
                    qa_entry=qa_entry,
                    final_qa_status=status,
                    run_id=run_id,
                    qa_generation=qc_generation,
                )
            )
            if adjustment_blockers:
                qa_entry["passed"] = False
                qa_entry["status"] = "failed"
                status["all_passed"] = False
                status["status"] = "failed"
                status["failed_reason"] = "ADJUSTMENT_ACCEPTANCE_STALE"
                status["message"] = adjustment_blockers[0]
                log("Final QA stopped: " + " | ".join(adjustment_blockers[:3]))
                break
            with project_write_guard(
                project_id, Path(ctx.workspace), fence_token
            ):
                qa_entry["workspace_digest"] = workspace_digest
                qa_entry["workspace_digest_algorithm"] = "sha256-paths-and-bytes-v1"
                qa_entry["delivery_manifest"] = delivery_manifest
                qa_entry["adjustment_acceptance_bindings"] = adjustment_bindings
                qa_entry["passed"] = True
                qa_entry["status"] = "passed"
                _accept_adjustment_bindings(
                    project_id,
                    adjustment_bindings,
                    final_qa_run_id=run_id,
                    artifact_sha256=str(
                        delivery_manifest.get("artifact_sha256") or ""
                    ),
                )
                status["workspace_digest"] = workspace_digest
                status["delivery_manifest"] = delivery_manifest
                status["all_passed"] = True
                status["status"] = "passed"
            log(f"✅ 全部通过！最终整体质检完成（第 {round_num} 轮）")
            break

        unassigned_issues = [
            issue for issue in latest_open_issues
            if not str(
                scope_files.get(str(issue.get("file") or ""), {}).get("agent_id")
                or ""
            ).strip()
        ]
        if unassigned_issues:
            message = (
                "Final QA found blocking issues without an authorized repair owner; "
                "no rework or follow-up QC was started"
            )
            status.update({
                "status": "needs_manual",
                "all_passed": False,
                "needs_manual": _final_qa_manual_issues(
                    unassigned_issues, message
                ),
                "failed_reason": "REWORK_OWNER_UNRESOLVED",
                "message": message,
                "action_required": {"options": ["manual_fix", "rebuild_phase"]},
                "current_step": "completed",
                "finished_at": time.time(),
            })
            log(message)
            _store_final_qa_run_status(ctx, status)
            await _persist_all_async()
            return

        current_signatures = {
            _final_qa_issue_signature(issue) for issue in latest_open_issues
        }
        if current_signatures and current_signatures == previous_issue_signatures:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
        previous_issue_signatures = current_signatures
        no_progress = stagnant_rounds >= 2

        if round_num >= MAX_FINAL_QC_ROUNDS or no_progress:
            # 超出上限，标记 needs_manual
            reason = (
                "连续两轮返修后问题未变化"
                if no_progress
                else f"已达最大质检轮次（{MAX_FINAL_QC_ROUNDS}）"
            )
            log(f"❌ {reason}，剩余问题标记为需人工介入")
            manual_list = []
            for iss in latest_open_issues:
                manual_list.append(_final_qa_manual_issue(
                    iss, f"最终整体质检停止：{reason}，请人工介入"
                ))
            status["needs_manual"] = manual_list
            status["status"] = "needs_manual"
            status["current_step"] = "completed"
            status["action_required"] = {"options": ["manual_fix", "rebuild_phase"]}
            _store_final_qa_run_status(ctx, status)
            await _persist_all_async()
            break

        # 还有问题 → 触发对应 Agent 返工
        log(f"⚠️ 发现问题，触发 {len(open_issues_by_agent)} 个 Agent 返工...")
        status["status"] = f"rework_round_{round_num}"

        pending_rework_signatures = set(current_signatures)
        dispatched_issues_by_agent = {
            agent_id: issues[:5]
            for agent_id, issues in open_issues_by_agent.items()
        }
        authorized_rework_paths = sorted({
            path
            for issues in dispatched_issues_by_agent.values()
            for issue in issues
            for path in _final_qa_issue_paths(issue)
        })
        if not authorized_rework_paths:
            message = (
                "Final QA found issues but no safe delivery-file target was identified; "
                "no rework or follow-up QC was started"
            )
            status.update({
                "status": "needs_manual",
                "all_passed": False,
                "needs_manual": _final_qa_manual_issues(
                    latest_open_issues, message
                ),
                "failed_reason": "REWORK_TARGET_UNRESOLVED",
                "message": message,
                "action_required": {"options": ["manual_fix", "rebuild_phase"]},
                "current_step": "completed",
                "finished_at": time.time(),
            })
            log(message)
            _store_final_qa_run_status(ctx, status)
            await _persist_all_async()
            return
        pending_rework_snapshot = _snapshot_final_qa_rework_files(
            workspace, latest_open_issues
        )
        if not pending_rework_snapshot:
            message = (
                "Final QA found issues but no safe delivery-file target was identified; "
                "no rework or follow-up QC was started"
            )
            status.update({
                "status": "needs_manual",
                "all_passed": False,
                "needs_manual": _final_qa_manual_issues(
                    latest_open_issues, message
                ),
                "failed_reason": "REWORK_TARGET_UNRESOLVED",
                "message": message,
                "action_required": {"options": ["manual_fix", "rebuild_phase"]},
                "current_step": "completed",
                "finished_at": time.time(),
            })
            log(message)
            _store_final_qa_run_status(ctx, status)
            await _persist_all_async()
            return
        status["rework_snapshot"] = _serialize_final_qa_rework_snapshot(
            Path(ctx.workspace), pending_rework_snapshot
        )
        status["rework_snapshot_state"] = "prepared"
        _store_final_qa_run_status(ctx, status)
        await _persist_all_async()

        from api.routes_execution import _run_agent_task
        rework_tasks = []
        rework_guard = _FenceExecutionGuard(
            project_id,
            Path(ctx.workspace),
            fence_token,
        )
        rework_lock_ids: List[str] = []
        rework_batches: List[tuple[str, List[Dict[str, Any]], Dict[str, Any], str, Any]] = []
        for agent_id, issues in dispatched_issues_by_agent.items():
            agent_info = ctx.agents.get(agent_id, {})
            sp_id = agent_info.get("subproject_id", "")
            sp = next((s for s in ctx.subprojects if s.get("id") == sp_id), None)
            # The project-wide Final QA fence already owns the underlying
            # ExpertLock ["*"] lease. Claiming another file lease here would
            # conflict with our own generation. The capability token plus the
            # narrowed ExecutionAgent allowed paths is the worker authorization.
            claim = {
                "success": True,
                "lock_id": "",
                "leased_until": (
                    status.get("write_fence") or {}
                ).get("leased_until"),
            }
            if not claim.get("success"):
                for lock_id in rework_lock_ids:
                    expert_lock.release_lock(lock_id)
                for _, _, claimed_agent, _, _ in rework_batches:
                    claimed_agent["allowed_path_prefixes"] = claimed_agent.pop(
                        "_final_qa_previous_allowed_paths", []
                    )
                status["status"] = "failed"
                status["all_passed"] = False
                status["failed_reason"] = "REWORK_LOCK_UNAVAILABLE"
                status["message"] = "Final QA rework could not acquire its file lease"
                status["retryable"] = True
                status["action_required"] = {"options": ["retry_acceptance"]}
                log(
                    "Final QA rework lock failed: "
                    + str(claim.get("error") or "unavailable")
                )
                status["finished_at"] = time.time()
                status["current_item"] = None
                status["current_step"] = "completed"
                _store_final_qa_run_status(ctx, status)
                await _persist_all_async()
                return
            agent_info["_final_qa_previous_allowed_paths"] = list(
                agent_info.get("allowed_path_prefixes") or []
            )
            agent_info["allowed_path_prefixes"] = _final_qa_rework_scope(
                agent_info, issues
            )
            rework_batches.append((agent_id, issues, agent_info, sp_id, sp))

        # Acquire the whole batch before mutating any Agent lifecycle. A later
        # lock failure must not strand an earlier Agent in fix_required.
        for agent_id, issues, agent_info, sp_id, sp in rework_batches:
            combined = "\n".join(
                _format_final_qa_rework_issue(issue)
                for issue in issues[:5]
            )
            fix_desc = (
                f"{sp.get('description','') if sp else ''}\n\n"
                "【质检修复任务】\n"
                f"【最终整体质检·第{round_num}轮返工】\n"
                f"以下 {len(issues)} 个问题需修复：\n{combined}"
            )
            from core.agent_lifecycle import transition_agent
            transition_agent(
                agent_info,
                "fix_required",
                progress=0,
                message=f"Final QA round {round_num} requires rework",
            )
            agent_info["fix_task"] = fix_desc

            rework_tasks.append(_run_agent_task(
                project_id=project_id,
                agent_id=agent_id,
                subproject_id=sp_id or agent_id,
                subproject_name=sp.get("name", agent_info.get("role", "")) if sp else agent_info.get("role", ""),
                description=fix_desc,
                tech_stack=sp.get("tech_stack", []) if sp else [],
                project_context=project_context,
                user_api_config=user_api_config,
                defer_fix_qc=True,
                execution_guard=rework_guard,
            ))

        # 等待所有返工任务完成（最多 120s）
        try:
            rework_results = await asyncio.wait_for(
                asyncio.gather(*rework_tasks, return_exceptions=True),
                timeout=120.0,
            )
            rework_errors = _final_qa_rework_failures(rework_results)
            for result, batch in zip(rework_results, rework_batches):
                if not isinstance(result, dict):
                    continue
                allowed = _final_qa_rework_scope(batch[2], batch[1])
                outside = [
                    str(path).replace("\\", "/")
                    for path in result.get("output_files") or []
                    if not _path_is_in_adjustment_scope(str(path), allowed)
                ]
                if outside:
                    rework_errors.append(
                        "rework reported output outside authorized issue paths: "
                        + ", ".join(outside[:3])
                    )
            affected_phase_ids = sorted({
                str(scope_files.get(path, {}).get("phase_id") or "")
                for path in authorized_rework_paths
                if str(scope_files.get(path, {}).get("phase_id") or "")
            })
            if not rework_errors:
                rework_errors.extend(
                    await verify_reworked_phases(affected_phase_ids)
                )
            if isinstance(pending_rework_snapshot, _FinalQAReworkSnapshot):
                _, before_rework_files = _decode_adjustment_snapshot(
                    pending_rework_snapshot.recovery_snapshot
                )
                _, after_rework_files = collect_delivery_artifact(
                    Path(ctx.workspace)
                )
                changed_rework_paths = {
                    path
                    for path in set(before_rework_files) | set(after_rework_files)
                    if before_rework_files.get(path) != after_rework_files.get(path)
                }
                outside_actual = sorted(
                    path for path in changed_rework_paths
                    if not _path_is_in_adjustment_scope(
                        path, authorized_rework_paths
                    )
                )
                if outside_actual:
                    rework_errors.append(
                        "rework changed files outside authorized issue paths: "
                        + ", ".join(outside_actual[:3])
                    )
            if rework_errors:
                rework_guard.revoke()
                with project_write_guard(
                    project_id, Path(ctx.workspace), fence_token
                ):
                    _restore_final_qa_rework_snapshot(
                        pending_rework_snapshot
                    )
                status["status"] = "failed"
                status["all_passed"] = False
                status["failed_reason"] = "REWORK_FAILED"
                status["message"] = "Final QA rework failed; no further QC round was started"
                status["rework_errors"] = rework_errors
                status["retryable"] = True
                status["action_required"] = {"options": ["retry_acceptance"]}
                log("Final QA rework failed: " + " | ".join(rework_errors[:3]))
                status["finished_at"] = time.time()
                status["current_item"] = None
                status["current_step"] = "completed"
                _store_final_qa_run_status(ctx, status)
                await _persist_all_async()
                return
            if not rework_tasks:
                status.update({
                    "status": "needs_manual",
                    "all_passed": False,
                    "failed_reason": "REWORK_NOT_DISPATCHED",
                    "message": "Final QA found issues but dispatched no repair task",
                    "needs_manual": _final_qa_manual_issues(
                        latest_open_issues, status["message"]
                    ),
                    "action_required": {"options": ["manual_fix", "rebuild_phase"]},
                    "current_step": "completed",
                    "finished_at": time.time(),
                })
                log(status["message"])
                _store_final_qa_run_status(ctx, status)
                await _persist_all_async()
                return
            if not _final_qa_snapshot_changed(pending_rework_snapshot):
                try:
                    _restore_final_qa_rework_snapshot(pending_rework_snapshot)
                    rollback_message = "Final QA rework returned successfully but changed no targeted file"
                except Exception as exc:
                    rollback_message = (
                        "Final QA rework changed no targeted file and snapshot restore failed: "
                        f"{exc.__class__.__name__}"
                    )
                status.update({
                    "status": "needs_manual",
                    "all_passed": False,
                    "failed_reason": "REWORK_NO_PROGRESS",
                    "message": rollback_message,
                    "needs_manual": _final_qa_manual_issues(
                        latest_open_issues, rollback_message
                    ),
                    "rework_files_changed": False,
                    "action_required": {"options": ["manual_fix", "rebuild_phase"]},
                    "current_step": "completed",
                    "finished_at": time.time(),
                })
                log(rollback_message)
                _store_final_qa_run_status(ctx, status)
                await _persist_all_async()
                return
            if isinstance(pending_rework_snapshot, _FinalQAReworkSnapshot):
                before_manifest, _ = _decode_adjustment_snapshot(
                    pending_rework_snapshot.recovery_snapshot
                )
                after_manifest = compute_delivery_manifest(
                    Path(ctx.workspace), required_paths=required_files
                )
                awaiting_adjustment = next((
                    item for item in _get_adjustments(project_id)
                    if item.get("status") == "awaiting_final_qa"
                ), None)
                root_artifact = str(
                    (
                        (awaiting_adjustment or {}).get(
                            "adjustment_acceptance"
                        ) or {}
                    ).get("result_artifact_sha256")
                    or before_manifest.get("artifact_sha256")
                    or ""
                )
                status.setdefault(
                    "adjustment_rework_generations", []
                ).append({
                    "generation_id": (
                        f"rework-{run_id}-{round_num}-"
                        f"{len(status.get('adjustment_rework_generations') or []) + 1}"
                    ),
                    "round": round_num,
                    "root_acceptance_artifact_sha256": root_artifact,
                    "from_artifact_sha256": before_manifest.get(
                        "artifact_sha256"
                    ),
                    "to_artifact_sha256": after_manifest.get(
                        "artifact_sha256"
                    ),
                    "authorized_paths": authorized_rework_paths,
                    "repair_evidence": [{
                        "agent_id": batch[0],
                        "issue_ids": [
                            issue.get("defect_id") or issue.get("id")
                            for issue in batch[1]
                        ],
                        "output_files": (
                            result.get("output_files") or []
                            if isinstance(result, dict) else []
                        ),
                        "success": (
                            result.get("success") is True
                            if isinstance(result, dict) else False
                        ),
                    } for result, batch in zip(
                        rework_results, rework_batches
                    )],
                })
            status.pop("rework_snapshot", None)
            status["rework_snapshot_state"] = "committed"
        except asyncio.TimeoutError:
            rework_guard.revoke()
            try:
                with project_write_guard(
                    project_id, Path(ctx.workspace), fence_token
                ):
                    _restore_final_qa_rework_snapshot(pending_rework_snapshot)
                status["rework_snapshot_state"] = "restored_after_timeout"
                status.pop("rework_snapshot", None)
            except Exception as exc:
                status["rework_snapshot_state"] = "restore_failed"
                status["recovery_error"] = exc.__class__.__name__
            status["status"] = "failed"
            status["all_passed"] = False
            status["failed_reason"] = "REWORK_TIMEOUT"
            status["message"] = "Final QA rework timed out; workspace may still be changing"
            status["retryable"] = True
            status["action_required"] = {"options": ["retry_acceptance"]}
            log("Final QA rework timed out; stopped before the next QC round")
            status["finished_at"] = time.time()
            status["current_item"] = None
            status["current_step"] = "completed"
            _store_final_qa_run_status(ctx, status)
            await _persist_all_async()
            return
        finally:
            for _, _, agent_info, _, _ in rework_batches:
                previous_allowed = agent_info.pop(
                    "_final_qa_previous_allowed_paths", []
                )
                agent_info["allowed_path_prefixes"] = previous_allowed
            for lock_id in rework_lock_ids:
                expert_lock.release_lock(lock_id)

        log(f"✅ 第 {round_num} 轮返工完成，准备再次质检...")

    status["finished_at"] = time.time()
    status["current_item"] = None
    status["current_step"] = "completed"
    _store_final_qa_run_status(ctx, status)
    await _persist_all_async()


def _on_final_qa_done(project_id: str, task: asyncio.Task) -> None:
    """Keep the task alive and expose unexpected failures to polling clients."""
    if _final_qa_tasks.get(project_id) is not task:
        return
    _final_qa_tasks.pop(project_id, None)
    _final_qa_api_configs.pop(project_id, None)
    fence_token = _final_qa_fence_tokens.pop(project_id, None)
    cancelled = task.cancelled()
    error = None if cancelled else task.exception()
    status = _final_qa_status.get(project_id)
    if status is not None and (cancelled or error is not None):
        status["status"] = "interrupted" if cancelled else "failed"
        status["all_passed"] = False
        status["message"] = (
            "Final QA was interrupted and can be resumed safely"
            if cancelled else f"全项目质检执行失败：{error}"
        )
        status["retryable"] = True
        status["action_required"] = {"options": ["retry_acceptance"]}
        status["finished_at"] = time.time()
        status["updated_at"] = time.time()
    ctx = projects.get(project_id)
    if ctx is not None and status is not None:
        if fence_token:
            try:
                release_project_write_fence(
                    project_id, Path(ctx.workspace), fence_token
                )
            except ProjectWriteFenceConflict:
                logger.error(
                    "Final QA write fence could not be released project=%s",
                    project_id,
                )
        _store_final_qa_run_status(ctx, status)
        try:
            asyncio.get_running_loop().create_task(_persist_all_async())
        except RuntimeError:
            pass

def register_final_qa_reinspection(
    ctx: ProjectContext,
    expected_artifact_digest: str,
    *,
    writer_capability: Optional[str] = None,
) -> Dict[str, Any]:
    """Claim the Final QA capability before an Engineer repair guard releases."""
    project_id = ctx.project_id
    expected = str(expected_artifact_digest or "").strip()
    phase_manager = _phase_managers.get(project_id)
    phases = phase_manager.phases if phase_manager else []
    blockers = validate_final_qa_readiness(ctx, phases)
    previous = ctx.qc_results.get("__whole_project__", {})
    previous_entry = previous.get("qa", previous) if isinstance(previous, dict) else {}
    persisted_run = (
        previous_entry.get("final_qa_run")
        if isinstance(previous_entry, dict)
        else None
    )
    persisted_registration = (
        previous_entry.get("final_qa_registration")
        if isinstance(previous_entry, dict)
        else None
    )
    if isinstance(persisted_registration, dict):
        blockers.append("persisted Final QA registration must be reconciled first")
    if isinstance(persisted_run, dict) and persisted_run.get("rework_snapshot"):
        blockers.append("persisted Final QA rework recovery must complete first")
    if not phases or blockers:
        raise ProjectWriteFenceConflict(
            "Final QA reinspection cannot be registered: "
            + "; ".join(blockers[:5] or ["phases unavailable"])
        )
    current = _final_qa_status.get(project_id, {})
    if current and current.get("status") not in {
        "passed", "needs_manual", "failed", "not_started",
        "interrupted", "infrastructure_blocked",
    }:
        raise ProjectWriteFenceConflict("Final QA is already active")
    manifest = compute_delivery_manifest(
        Path(ctx.workspace),
        required_paths=_adjustment_required_paths(ctx),
    )
    if not expected or manifest.get("artifact_sha256") != expected:
        raise ProjectWriteFenceConflict(
            "Engineer repair artifact changed before Final QA registration"
        )
    registration_id = uuid.uuid4().hex
    owner = f"final-qa:{project_id}:{registration_id}"
    record = _final_qa_construction_record(
        ctx,
        registration_id=registration_id,
        owner=owner,
        state="acquiring",
        artifact_digest=expected,
        required_paths=_adjustment_required_paths(ctx),
        previous_status=_final_qa_status.get(project_id),
        previous_whole_project_qc=ctx.qc_results.get("__whole_project__"),
    )
    if not _durably_compare_set_final_qa_registration(
        ctx,
        record,
        expected_registration_id=None,
    ):
        raise ProjectWriteFenceConflict(
            "Final QA registration lost its durable construction CAS"
        )
    try:
        fence = acquire_project_write_fence(
            project_id,
            Path(ctx.workspace),
            owner=owner,
            purpose="final_qa",
            ttl_seconds=15 * 60,
            writer_capability=writer_capability,
        )
    except Exception:
        _durably_compare_delete_final_qa_registration(
            ctx, registration_id
        )
        raise
    record["state"] = "registered"
    record["fence"] = fence
    record["write_fence"] = _durable_fence_identity(fence)
    _final_qa_pre_registrations[project_id] = record
    _final_qa_fence_tokens[project_id] = str(fence["token"])
    if not _durably_compare_set_final_qa_registration(
        ctx,
        record,
        expected_registration_id=registration_id,
    ):
        release_project_write_fence(
            project_id, Path(ctx.workspace), str(fence["token"])
        )
        raise ProjectWriteFenceConflict(
            "Final QA registration was superseded before fence publication"
        )
    return {
        "registration_id": registration_id,
        "project_id": project_id,
        "artifact_digest": expected,
        "scheduled": False,
    }


def cancel_registered_final_qa(
    ctx: ProjectContext,
    registration_id: str,
    expected_artifact_digest: str,
) -> bool:
    """Release only the exact unused Final QA pre-registration."""
    project_id = ctx.project_id
    record = _final_qa_pre_registrations.get(project_id)
    if (
        not isinstance(record, dict)
        or record.get("registration_id") != registration_id
        or record.get("artifact_digest") != expected_artifact_digest
    ):
        raise ProjectWriteFenceConflict(
            "Final QA pre-registration identity does not match"
        )
    task = _final_qa_tasks.get(project_id)
    if task is not None and not task.done():
        raise ProjectWriteFenceConflict(
            "Final QA task already owns the registration"
        )
    record["state"] = "cancelling"
    if not _durably_compare_set_final_qa_registration(
        ctx,
        record,
        expected_registration_id=registration_id,
    ):
        raise ProjectWriteFenceConflict(
            "Final QA pre-registration was superseded before cancellation"
        )
    token = str((record.get("fence") or {}).get("token") or "")
    release_project_write_fence(project_id, Path(ctx.workspace), token)
    with _final_qa_registration_cas_guard:
        current = _final_qa_entry(ctx).get("final_qa_registration")
        if (
            not isinstance(current, dict)
            or current.get("registration_id") != registration_id
        ):
            raise ProjectWriteFenceConflict(
                "Final QA cancellation generation was superseded"
            )
        if record.get("had_final_qa_status"):
            _final_qa_status[project_id] = copy.deepcopy(
                record.get("previous_final_qa_status")
            )
        else:
            _final_qa_status.pop(project_id, None)
        if record.get("had_whole_project_qc"):
            ctx.qc_results["__whole_project__"] = copy.deepcopy(
                record.get("previous_whole_project_qc")
            )
        else:
            ctx.qc_results.pop("__whole_project__", None)
        _final_qa_fence_tokens.pop(project_id, None)
        _final_qa_pre_registrations.pop(project_id, None)
        _persist_all()
    return True


async def start_registered_final_qa(
    project_id: str, registration: Optional[Dict[str, Any]]
):
    if not isinstance(registration, dict):
        raise HTTPException(
            status_code=409, detail="Final QA pre-registration is missing"
        )
    return await _trigger_final_qa(project_id, registration)


async def reconcile_interrupted_final_qa_runs() -> int:
    """Recover persisted Final QA generations before the API starts serving."""
    recovered = 0
    terminal = {
        "passed",
        "failed",
        "needs_manual",
        "infrastructure_blocked",
        "recovery_blocked",
    }
    for project_id, ctx in list(projects.items()):
        entry = _final_qa_entry(ctx)
        registration = entry.get("final_qa_registration")
        run = entry.get("final_qa_run")
        candidate: Optional[Dict[str, Any]] = None
        if isinstance(registration, dict):
            candidate = registration
        elif (
            isinstance(run, dict)
            and str(run.get("status") or "") not in terminal
        ):
            candidate = run
        if not isinstance(candidate, dict):
            continue
        if (
            _final_qa_tasks.get(project_id) is not None
            and not _final_qa_tasks[project_id].done()
        ):
            continue
        workspace = Path(ctx.workspace)
        try:
            workspace_available = workspace.is_dir()
        except OSError:
            workspace_available = False
        if not workspace_available:
            if isinstance(registration, dict):
                retryable_registration = copy.deepcopy(registration)
                retryable_registration.update({
                    "recovery_retryable": True,
                    "recovery_error": "project_workspace_unavailable",
                    "last_recovery_attempt_at": time.time(),
                })
                _persist_final_qa_registration(
                    ctx,
                    retryable_registration,
                    expected_registration_id=str(
                        registration.get("registration_id") or ""
                    ),
                )
            retryable_status = {
                **(copy.deepcopy(run) if isinstance(run, dict) else {}),
                "status": "recovery_required",
                "all_passed": False,
                "retryable": True,
                "message": (
                    "Final QA recovery is waiting for the project workspace"
                ),
                "action_required": {"options": ["retry_recovery"]},
                "current_step": "workspace_unavailable",
            }
            _final_qa_status[project_id] = retryable_status
            if isinstance(run, dict):
                _store_final_qa_run_status(ctx, retryable_status)
            await _persist_all_async()
            continue
        if (
            isinstance(registration, dict)
            and registration.get("state") == "cancelling"
        ):
            identity = registration.get("write_fence") or {}
            owner = str(identity.get("owner") or "")
            purpose = str(identity.get("purpose") or "")
            lock_id = str(
                identity.get("lock_id")
                or identity.get("lease_id")
                or ""
            )
            try:
                marker = get_project_write_fence(project_id, workspace)
            except OSError:
                registration["recovery_retryable"] = True
                registration["recovery_error"] = (
                    "project_workspace_unavailable"
                )
                _persist_final_qa_registration(
                    ctx,
                    registration,
                    expected_registration_id=str(
                        registration.get("registration_id") or ""
                    ),
                )
                await _persist_all_async()
                continue
            if marker and not lock_id:
                if (
                    marker.get("owner") != owner
                    or marker.get("purpose") != purpose
                ):
                    continue
                lock_id = str(marker.get("lock_id") or "")
            if marker and owner and purpose and lock_id:
                revoke_project_write_fence_generation(
                    project_id,
                    workspace,
                    expected_owner=owner,
                    expected_purpose=purpose,
                    expected_lock_id=lock_id,
                )
            if registration.get("had_final_qa_status"):
                _final_qa_status[project_id] = copy.deepcopy(
                    registration.get("previous_final_qa_status")
                )
            else:
                _final_qa_status.pop(project_id, None)
            if registration.get("had_whole_project_qc"):
                ctx.qc_results["__whole_project__"] = copy.deepcopy(
                    registration.get("previous_whole_project_qc")
                )
            else:
                ctx.qc_results.pop("__whole_project__", None)
            _final_qa_pre_registrations.pop(project_id, None)
            _final_qa_fence_tokens.pop(project_id, None)
            await _persist_all_async()
            recovered += 1
            continue

        recovery_status = {
            **(copy.deepcopy(run) if isinstance(run, dict) else {}),
            "registration_id": str(candidate.get("registration_id") or ""),
            "artifact_digest": str(
                candidate.get("artifact_digest")
                or (run or {}).get("artifact_digest")
                or ""
            ),
            "required_paths": list(
                candidate.get("required_paths")
                or (run or {}).get("required_paths")
                or ()
            ),
            "write_fence": copy.deepcopy(
                candidate.get("write_fence")
                or (run or {}).get("write_fence")
                or {}
            ),
            "status": "recovering",
            "all_passed": False,
            "retryable": False,
            "message": "Final QA generation recovery is in progress",
            "current_step": "startup_recovery",
            "recovery_started_at": time.time(),
        }
        _final_qa_status[project_id] = recovery_status
        _store_final_qa_run_status(ctx, recovery_status)
        await _persist_all_async()

        fence_identity = candidate.get("write_fence")
        if not isinstance(fence_identity, dict) and isinstance(run, dict):
            fence_identity = run.get("write_fence")
        owner = str((fence_identity or {}).get("owner") or "")
        purpose = str((fence_identity or {}).get("purpose") or "")
        lock_id = str(
            (fence_identity or {}).get("lock_id")
            or (fence_identity or {}).get("lease_id")
            or ""
        )
        if owner and purpose and not lock_id:
            try:
                construction_marker = get_project_write_fence(
                    project_id, workspace
                )
            except OSError as exc:
                recovery_status.update({
                    "status": "recovery_required",
                    "retryable": True,
                    "message": (
                        "Final QA recovery is waiting for the project "
                        f"workspace: {exc.__class__.__name__}"
                    ),
                    "action_required": {"options": ["retry_recovery"]},
                    "current_step": "workspace_unavailable",
                })
                _store_final_qa_run_status(ctx, recovery_status)
                await _persist_all_async()
                continue
            if construction_marker:
                if (
                    construction_marker.get("owner") != owner
                    or construction_marker.get("purpose") != purpose
                ):
                    recovery_status.update({
                        "status": "recovery_blocked",
                        "retryable": False,
                        "message": (
                            "Final QA construction intent conflicts with "
                            "another fence generation"
                        ),
                        "current_step": "recovery_blocked",
                    })
                    _store_final_qa_run_status(ctx, recovery_status)
                    await _persist_all_async()
                    continue
                lock_id = str(construction_marker.get("lock_id") or "")
        if not owner or not purpose:
            recovery_status.update({
                "status": "recovery_blocked",
                "retryable": False,
                "message": (
                    "Persisted Final QA generation lacks exact fence identity; "
                    "manual recovery is required"
                ),
                "current_step": "recovery_blocked",
            })
            _store_final_qa_run_status(ctx, recovery_status)
            await _persist_all_async()
            continue
        try:
            if lock_id:
                revoke_project_write_fence_generation(
                    project_id,
                    workspace,
                    expected_owner=owner,
                    expected_purpose=purpose,
                    expected_lock_id=lock_id,
                )
            recovery_generation = uuid.uuid4().hex
            recovery_owner = (
                f"final-qa:{project_id}:recovery:{recovery_generation}"
            )
            recovery_intent = _final_qa_construction_record(
                ctx,
                registration_id=recovery_generation,
                owner=recovery_owner,
                state="reclaiming",
                artifact_digest=str(
                    recovery_status.get("qc_artifact_sha256")
                    or candidate.get("artifact_digest")
                    or recovery_status.get("artifact_digest")
                    or ""
                ),
                required_paths=list(
                    candidate.get("required_paths")
                    or recovery_status.get("required_paths")
                    or ()
                ),
                previous_whole_project_qc=ctx.qc_results.get(
                    "__whole_project__"
                ),
            )
            expected_registration_id = (
                str(candidate.get("registration_id") or "")
                if isinstance(registration, dict)
                else None
            )
            if not _durably_compare_set_final_qa_registration(
                ctx,
                recovery_intent,
                expected_registration_id=expected_registration_id,
            ):
                raise ProjectWriteFenceConflict(
                    "Final QA recovery construction lost its registration CAS"
                )
            new_fence = acquire_project_write_fence(
                project_id,
                workspace,
                owner=recovery_owner,
                purpose="final_qa",
                ttl_seconds=15 * 60,
            )
        except OSError as exc:
            recovery_status.update({
                "status": "recovery_required",
                "retryable": True,
                "message": (
                    "Final QA recovery is waiting for the project "
                    f"workspace: {exc.__class__.__name__}"
                ),
                "action_required": {"options": ["retry_recovery"]},
                "current_step": "workspace_unavailable",
            })
            _store_final_qa_run_status(ctx, recovery_status)
            await _persist_all_async()
            continue
        except ProjectWriteFenceConflict as exc:
            recovery_status.update({
                "status": "recovery_blocked",
                "retryable": False,
                "message": f"Final QA generation recovery was blocked: {exc}",
                "current_step": "recovery_blocked",
            })
            _store_final_qa_run_status(ctx, recovery_status)
            await _persist_all_async()
            continue

        token = str(new_fence["token"])
        try:
            serialized_snapshot = (
                run.get("rework_snapshot")
                if isinstance(run, dict)
                else None
            )
            if serialized_snapshot:
                snapshot = _deserialize_final_qa_rework_snapshot(
                    Path(ctx.workspace), serialized_snapshot
                )
                with project_write_guard(
                    project_id, Path(ctx.workspace), token
                ):
                    _restore_final_qa_rework_snapshot(snapshot)
                recovery_status.pop("rework_snapshot", None)
                recovery_status["rework_snapshot_state"] = (
                    "restored_after_restart"
                )

            required_paths = list(
                candidate.get("required_paths")
                or recovery_status.get("required_paths")
                or ()
            )
            expected_artifact = str(
                recovery_status.get("qc_artifact_sha256")
                or candidate.get("artifact_digest")
                or recovery_status.get("artifact_digest")
                or ""
            )
            with project_write_guard(
                project_id, Path(ctx.workspace), token
            ):
                restored_manifest = compute_delivery_manifest(
                    Path(ctx.workspace),
                    required_paths=required_paths,
                )
            if (
                not expected_artifact
                or restored_manifest.get("artifact_sha256")
                != expected_artifact
            ):
                raise ProjectWriteFenceConflict(
                    "Recovered workspace does not match the persisted artifact"
                )

            registration_id = str(
                candidate.get("registration_id") or uuid.uuid4().hex
            )
            reclaimed = {
                "schema_version": 1,
                "registration_id": registration_id,
                "project_id": project_id,
                "state": "reclaimed",
                "artifact_digest": expected_artifact,
                "required_paths": required_paths,
                "fence": new_fence,
                "write_fence": _durable_fence_identity(new_fence),
                "registered_at": time.time(),
                "had_final_qa_status": False,
                "previous_final_qa_status": None,
                "had_whole_project_qc": True,
                "previous_whole_project_qc": copy.deepcopy(
                    ctx.qc_results.get("__whole_project__")
                ),
            }
            _final_qa_pre_registrations[project_id] = reclaimed
            _final_qa_fence_tokens[project_id] = token
            if not _durably_compare_set_final_qa_registration(
                ctx,
                reclaimed,
                expected_registration_id=recovery_generation,
            ):
                raise ProjectWriteFenceConflict(
                    "Final QA reclaimed generation lost its registration CAS"
                )
            _store_final_qa_run_status(ctx, recovery_status)
            await _persist_all_async()
            _final_qa_status.pop(project_id, None)
            await _trigger_final_qa(
                project_id,
                {
                    "registration_id": registration_id,
                    "artifact_digest": expected_artifact,
                },
            )
            recovered += 1
        except Exception as exc:
            try:
                release_project_write_fence(
                    project_id, Path(ctx.workspace), token
                )
            except ProjectWriteFenceConflict:
                pass
            _final_qa_fence_tokens.pop(project_id, None)
            _final_qa_pre_registrations.pop(project_id, None)
            retryable = isinstance(exc, OSError)
            recovery_status.update({
                "status": (
                    "recovery_required" if retryable else "recovery_blocked"
                ),
                "retryable": retryable,
                "message": (
                    f"Final QA recovery is retryable: {exc}"
                    if retryable
                    else f"Final QA recovery failed: {exc}"
                ),
                "current_step": (
                    "workspace_unavailable"
                    if retryable
                    else "recovery_blocked"
                ),
            })
            if retryable:
                recovery_status["action_required"] = {
                    "options": ["retry_recovery"]
                }
            if not _clear_final_qa_registration(
                ctx, str(recovery_generation or "")
            ):
                _clear_final_qa_registration(
                    ctx, str(candidate.get("registration_id") or "")
                )
            _final_qa_status[project_id] = recovery_status
            _store_final_qa_run_status(ctx, recovery_status)
            await _persist_all_async()
    return recovered


async def _trigger_final_qa(
    project_id: str,
    registration: Optional[Dict[str, Any]] = None,
):
    async with _final_qa_start_mutex(project_id):
        return await _trigger_final_qa_locked(project_id, registration)


async def _trigger_final_qa_locked(
    project_id: str,
    registration: Optional[Dict[str, Any]] = None,
):
    """
    触发最终整体质检：
    - is_final_phase=True（含文件协作/整体落地检查）
    - 自动返工循环（最多 5 轮）
    - 5 轮未解决 → 标记 needs_manual，用户可在文件管理中整改
    质检逻辑与阶段质检完全一致
    """
    ctx = _get_project(project_id)
    phase_manager = _phase_managers.get(project_id)
    phases = phase_manager.phases if phase_manager else []
    phase_blockers = validate_final_qa_readiness(ctx, phases)
    if not phases or phase_blockers:
        detail = "All phases must be confirmed and completed before Final QA"
        if phase_blockers:
            detail += ": " + "; ".join(phase_blockers[:5])
        raise HTTPException(status_code=409, detail=detail)
    current = _final_qa_status.get(project_id, {})
    if current and current.get("status") not in {
        "passed", "needs_manual", "failed", "not_started",
        "interrupted", "infrastructure_blocked",
    }:
        return {"success": True, "already_running": True, "message": "全项目质检循环已在运行", "project_id": project_id}
    owned_registration_id = ""
    if registration is not None:
        record = _final_qa_pre_registrations.get(project_id)
        if (
            not isinstance(record, dict)
            or record.get("registration_id") != registration.get("registration_id")
            or record.get("artifact_digest") != registration.get("artifact_digest")
        ):
            raise HTTPException(
                status_code=409,
                detail="Final QA pre-registration is stale or mismatched",
            )
        fence = record["fence"]
        owned_registration_id = str(record.get("registration_id") or "")
        registered_manifest = compute_delivery_manifest(
            Path(ctx.workspace),
            required_paths=record.get("required_paths") or (),
        )
        if (
            registered_manifest.get("artifact_sha256")
            != record.get("artifact_digest")
        ):
            cancel_registered_final_qa(
                ctx,
                str(record.get("registration_id") or ""),
                str(record.get("artifact_digest") or ""),
            )
            raise HTTPException(
                status_code=409,
                detail="Final QA registered artifact changed before startup",
            )
    else:
        # Every phase is confirmed and no Final QA task is running, so claim a
        # new project capability for the ordinary API path.
        ordinary_generation = uuid.uuid4().hex
        owned_registration_id = ordinary_generation
        ordinary_owner = f"final-qa:{project_id}:{ordinary_generation}"
        ordinary_required = _adjustment_required_paths(ctx)
        ordinary_manifest = compute_delivery_manifest(
            Path(ctx.workspace), required_paths=ordinary_required
        )
        ordinary_intent = _final_qa_construction_record(
            ctx,
            registration_id=ordinary_generation,
            owner=ordinary_owner,
            state="acquiring",
            artifact_digest=str(
                ordinary_manifest.get("artifact_sha256") or ""
            ),
            required_paths=ordinary_required,
            previous_status=_final_qa_status.get(project_id),
            previous_whole_project_qc=ctx.qc_results.get(
                "__whole_project__"
            ),
        )
        if not _durably_compare_set_final_qa_registration(
            ctx,
            ordinary_intent,
            expected_registration_id=None,
        ):
            raise HTTPException(
                status_code=409,
                detail="Final QA construction is already registered",
            )
        try:
            fence = acquire_project_write_fence(
                project_id,
                Path(ctx.workspace),
                owner=ordinary_owner,
                purpose="final_qa",
                ttl_seconds=15 * 60,
            )
        except Exception as exc:
            _durably_compare_delete_final_qa_registration(
                ctx, ordinary_generation
            )
            if isinstance(exc, ProjectWriteFenceConflict):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise
        ordinary_intent["state"] = "registered"
        ordinary_intent["fence"] = fence
        ordinary_intent["write_fence"] = _durable_fence_identity(fence)
        if not _durably_compare_set_final_qa_registration(
            ctx,
            ordinary_intent,
            expected_registration_id=ordinary_generation,
        ):
            release_project_write_fence(
                project_id,
                Path(ctx.workspace),
                str(fence["token"]),
            )
            raise HTTPException(
                status_code=409,
                detail="Final QA construction generation was superseded",
            )
    fence_token = str(fence["token"])
    _final_qa_fence_tokens[project_id] = fence_token
    # 用户修改后重跑时必须重新验证上一轮人工问题，不能永久锁死 needs_manual。
    previous = ctx.qc_results.get("__whole_project__", {})
    previous_entry = previous.get("qa", previous) if isinstance(previous, dict) else {}
    persisted_run = (
        previous_entry.get("final_qa_run")
        if isinstance(previous_entry, dict)
        else None
    )
    if isinstance(persisted_run, dict) and persisted_run.get("rework_snapshot"):
        try:
            snapshot = _deserialize_final_qa_rework_snapshot(
                Path(ctx.workspace), persisted_run["rework_snapshot"]
            )
            with project_write_guard(project_id, Path(ctx.workspace), fence_token):
                _restore_final_qa_rework_snapshot(snapshot)
        except Exception as exc:
            release_project_write_fence(project_id, Path(ctx.workspace), fence_token)
            _final_qa_fence_tokens.pop(project_id, None)
            _final_qa_pre_registrations.pop(project_id, None)
            raise HTTPException(
                status_code=409,
                detail=(
                    "Persisted Final QA rework snapshot could not be restored; "
                    "manual recovery is required"
                ),
            ) from exc
    with project_write_guard(project_id, Path(ctx.workspace), fence_token):
        previous_manifest = (
            previous_entry.get("delivery_manifest")
            if isinstance(previous_entry, dict)
            else {}
        )
        current_manifest = compute_delivery_manifest(
            Path(ctx.workspace),
            required_paths=(
                previous_manifest.get("required_paths") or ()
                if isinstance(previous_manifest, dict)
                else ()
            ),
        )
    if (
        isinstance(previous_entry, dict)
        and previous_entry.get("passed") is True
        and previous_entry.get("status") == "passed"
        and isinstance(previous_entry.get("delivery_manifest"), dict)
        and previous_entry["delivery_manifest"].get("artifact_sha256")
        == current_manifest.get("artifact_sha256")
        and (
            not _runtime_acceptance_required()
            or runtime_artifact_matches(
                previous_entry.get("runtime_acceptance"), current_manifest
            )
        )
    ):
        release_project_write_fence(project_id, Path(ctx.workspace), fence_token)
        _final_qa_fence_tokens.pop(project_id, None)
        _final_qa_pre_registrations.pop(project_id, None)
        _clear_final_qa_registration(ctx, owned_registration_id)
        await _persist_all_async()
        return {
            "success": True,
            "already_completed": True,
            "message": "当前工作区已通过最终整体质检，无需重复执行",
            **await get_final_qa_status(project_id),
        }
    if isinstance(previous_entry, dict):
        previous_entry.pop("workspace_digest", None)
        previous_entry.pop("workspace_digest_algorithm", None)
        previous_entry.pop("delivery_manifest", None)
        previous_entry["passed"] = False
        previous_entry["status"] = "running"
    for issue in previous_entry.get("issues_detail", []) if isinstance(previous_entry, dict) else []:
        if issue.get("status") == "needs_manual":
            issue["status"] = "fixing"
            issue["fix_rounds"] = 0
            issue.pop("needs_manual_reason", None)
    _reset_final_qa_fix_budget(ctx)
    run_required_paths = (
        list(record.get("required_paths") or ())
        if registration is not None
        else _adjustment_required_paths(ctx)
    )
    with project_write_guard(project_id, Path(ctx.workspace), fence_token):
        run_manifest = compute_delivery_manifest(
            Path(ctx.workspace),
            required_paths=run_required_paths,
        )
    # 初始化/重置状态
    _final_qa_status[project_id] = {
        "run_id": uuid.uuid4().hex,
        "registration_id": (
            str(record.get("registration_id") or "")
            if registration is not None
            else owned_registration_id
        ),
        "artifact_digest": run_manifest.get("artifact_sha256"),
        "required_paths": run_required_paths,
        "status": "running",
        "round": 0,
        "total_rounds": MAX_FINAL_QC_ROUNDS,
        "logs": [],
        "all_passed": False,
        "needs_manual": [],
        "qc_summary": {},
        "user_reports": [],
        "total_items": 0,
        "completed_items": 0,
        "current_item": None,
        "started_at": time.time(),
        "current_step": "queued",
        "steps": [],
        "write_fence": _durable_fence_identity(fence),
    }
    if not _durably_promote_final_qa_registration(
        ctx,
        owned_registration_id,
        _final_qa_status[project_id],
    ):
        release_project_write_fence(
            project_id, Path(ctx.workspace), fence_token
        )
        _final_qa_fence_tokens.pop(project_id, None)
        _final_qa_status.pop(project_id, None)
        raise HTTPException(
            status_code=409,
            detail="Final QA registration lost its promotion CAS",
        )
    from core.hermes_client import current_user_api_config
    _final_qa_api_configs[project_id] = current_user_api_config.get()
    task = asyncio.create_task(_run_final_qa_loop(project_id))
    _final_qa_tasks[project_id] = task
    _final_qa_pre_registrations.pop(project_id, None)
    task.add_done_callback(lambda completed, pid=project_id: _on_final_qa_done(pid, completed))
    return {
        "success": True,
        "message": f"最终整体质检已启动（最多 {MAX_FINAL_QC_ROUNDS} 轮，后台运行）",
        "project_id": project_id,
    }


@router.post("/projects/{project_id}/final-qa")
async def trigger_final_qa(project_id: str):
    return await _trigger_final_qa(project_id)


@router.get("/projects/{project_id}/final-qa/status")
async def get_final_qa_status(project_id: str):
    """获取最终整体质检进度"""
    ctx = _get_project(project_id)
    status = _final_qa_status.get(project_id)
    persisted = ctx.qc_results.get("__whole_project__", {})
    persisted_qa = persisted.get("qa", persisted) if isinstance(persisted, dict) else {}
    expected_manifest = (
        persisted_qa.get("delivery_manifest")
        if isinstance(persisted_qa, dict)
        else None
    )
    try:
        current_manifest = compute_delivery_manifest(
            Path(ctx.workspace),
            required_paths=(
                expected_manifest.get("required_paths") or ()
                if isinstance(expected_manifest, dict)
                else ()
            ),
        )
    except (OSError, ValueError):
        current_manifest = {}
    workspace_is_current = bool(
        isinstance(expected_manifest, dict)
        and expected_manifest.get("artifact_sha256")
        == current_manifest.get("artifact_sha256")
        and expected_manifest.get("rule_version")
        == current_manifest.get("rule_version")
    )
    phase_manager = _phase_managers.get(project_id)
    phases = phase_manager.phases if phase_manager else []
    supervisor_is_current = not validate_final_qa_readiness(ctx, phases)
    workspace_is_current = workspace_is_current and supervisor_is_current
    if status is not None and status.get("status") == "passed" and not workspace_is_current:
        status = {
            **status,
            "status": "failed",
            "all_passed": False,
            "failed_reason": "STALE_WORKSPACE",
            "message": "Project files changed after Final QA; run Final QA again",
        }
        _final_qa_status[project_id] = status
    if status is None:
        # Live progress is process-local, while the completed whole-project QA
        # result is persisted with ProjectContext. Reconstruct a terminal
        # response after a deploy/restart instead of regressing to not_started.
        qa_result = persisted_qa
        persisted_run = (
            qa_result.get("final_qa_run")
            if isinstance(qa_result, dict)
            else None
        )
        if isinstance(persisted_run, dict) and persisted_run.get("status") not in {
            "passed", "failed", "needs_manual", "infrastructure_blocked",
            "recovery_blocked",
        }:
            status = {
                **persisted_run,
                "status": "recovery_required",
                "all_passed": False,
                "message": (
                    "Final QA restart recovery has not reclaimed its exact "
                    "generation; retry is not yet authorized"
                ),
                "action_required": {"options": ["inspect_recovery"]},
                "retryable": False,
                "restored_from_persisted_result": True,
            }
            _final_qa_status[project_id] = status
            return {"project_id": project_id, **status}
        if (
            isinstance(persisted_run, dict)
            and persisted_run.get("status") in {
                "failed", "needs_manual", "infrastructure_blocked",
                "recovery_blocked",
            }
        ):
            status = {
                **persisted_run,
                "restored_from_persisted_result": True,
                "workspace_digest_current": workspace_is_current,
            }
            _final_qa_status[project_id] = status
            return {"project_id": project_id, **status}
        if isinstance(qa_result, dict) and (
            "passed" in qa_result
            or qa_result.get("status") in {"passed", "failed", "needs_manual"}
        ):
            restored_status = str(qa_result.get("status") or "")
            runtime_evidence = qa_result.get("runtime_acceptance")
            runtime_evidence_passed = bool(
                isinstance(runtime_evidence, dict)
                and runtime_evidence.get("enabled") is True
                and runtime_evidence.get("passed") is True
                and runtime_evidence.get("status") == "passed"
            )
            runtime_evidence_failed = bool(
                isinstance(runtime_evidence, dict)
                and runtime_evidence.get("enabled") is True
                and not runtime_evidence_passed
            )
            if _runtime_acceptance_required() and not runtime_evidence_passed:
                restored_status = "failed"
            elif runtime_evidence_failed:
                restored_status = "failed"
            elif not workspace_is_current:
                restored_status = "failed"
            elif qa_result.get("passed") is True:
                restored_status = "passed"
            elif restored_status not in {"failed", "needs_manual"}:
                restored_status = "failed"
            status = {
                "status": restored_status,
                "round": int(qa_result.get("qc_round") or 0),
                "total_rounds": MAX_FINAL_QC_ROUNDS,
                "all_passed": restored_status == "passed",
                "qc_summary": {
                    "score": qa_result.get("score"),
                    "error_count": qa_result.get("error_count", 0),
                    "warning_count": qa_result.get("warning_count", 0),
                    "fixed_count": qa_result.get("fixed_count", 0),
                },
                "needs_manual": [
                    issue
                    for issue in qa_result.get("issues_detail", [])
                    if issue.get("status") == "needs_manual"
                ],
                "finished_at": qa_result.get("checked_at"),
                "runtime_acceptance": qa_result.get("runtime_acceptance"),
                "workspace_digest_current": workspace_is_current,
                "restored_from_persisted_result": True,
            }
        else:
            status = {"status": "not_started"}
    return {"project_id": project_id, **status}

@router.post("/debug/projects/{project_id}/inject-qc")
async def debug_inject_qc(project_id: str, request: InjectQCRequest):
    """
    调试用：直接将 qc_results 注入到后端内存中的项目对象。
    仅在 DEBUG 环境变量为 true 时可用，生产环境返回 404 隐藏端点。
    """
    import os as _os
    if _os.environ.get("DEBUG", "").lower() != "true":
        raise HTTPException(status_code=404, detail="未找到")
    ctx = _get_project(project_id)
    ctx.qc_results.update(request.qc_results)
    await _persist_all_async()
    total = sum(len(v.get("issues_detail", [])) for v in ctx.qc_results.values())
    return {"success": True, "injected_subprojects": list(request.qc_results.keys()), "total_issues": total}

# ─── 指标计算 API ─────────────────────────────────────────────────────────────
