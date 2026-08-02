"""工程师工作台路由"""
import copy
import asyncio
import hashlib
import json
import os
import time
import logging
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from core.database import kv_get, kv_set
from core.hermes_client import Message, MessageRole
from core.app_state import (
    app, projects, hermes_client, global_sm_agent, gitee_sync,
    config_loader, agents_api_config, DEFAULT_API_CONFIG,
    global_pm_team, global_supervisor_team, global_ccb_agent,
    _get_project, _get_hermes, _get_idea_landing,
    _persist_all_async, _persist_idea_landing,
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
    EngineerIdentityReviewRequest,
    EngineerConfirmFixRequest, EngineerApplyFixRequest, EngineerManualRequest,
    EngineerQAChatRequest, EngineerQAInspectRequest,
    ExpertTrainingRequest, ExpertWorkModeRequest, ExpertConfigRequest,
    ExpertChatTrainRequest, ExpertFeedbackRequest, ExpertKnowledgeRequest,
    CCBCheckDeleteMemberRequest, CCBCheckDeleteExpertRequest,
    CCBConfirmDeleteRequest, InjectQCRequest, ProjectTeamAssignRequest,
)
router = APIRouter(tags=["engineer"])


class EngineerConsultationCreateRequest(BaseModel):
    mode: str = "inquiry"
    title: str = ""
    source_issue_id: Optional[str] = None


class EngineerConsultationMessageRequest(BaseModel):
    message: str


class EngineerConsultationRenameRequest(BaseModel):
    title: str


class EngineerRectificationPrepareRequest(BaseModel):
    session_id: str


class EngineerRectificationApplyRequest(BaseModel):
    session_id: str
    proposal_id: str


def _consultation_key(project_id: str) -> str:
    return f"engineer_consultations:{project_id}"


def _consultation_sessions(project_id: str) -> List[Dict[str, Any]]:
    payload = kv_get(_consultation_key(project_id), {"sessions": []})
    sessions = payload.get("sessions") if isinstance(payload, dict) else []
    return sessions if isinstance(sessions, list) else []


def _save_consultation_sessions(project_id: str, sessions: List[Dict[str, Any]]) -> None:
    kv_set(_consultation_key(project_id), {"sessions": sessions, "updated_at": time.time()})


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Extract the first balanced JSON object without treating format as quality."""
    start = text.find("{")
    while start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:index + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(value, dict):
                        return value
                    break
        start = text.find("{", start + 1)
    return None


def _parse_rectification_response(raw: str) -> Dict[str, Any]:
    """Normalize strict JSON, fenced JSON, or prose-wrapped JSON."""
    text = str(raw or "").strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = _extract_json_object(text)
    if not isinstance(parsed, dict):
        raise ValueError("response does not contain an executable change object")
    return parsed


def _consultation_roles(ctx: ProjectContext) -> List[str]:
    roles = ["PM组长", "全栈工程师"]
    for agent in ctx.agents.values():
        role = str(agent.get("role") or agent.get("agent_role") or "").strip()
        if role and role not in roles:
            roles.append(role)
    return roles


def _mentioned_role(text: str, roles: List[str]) -> Optional[str]:
    lowered = text.lower()
    for role in sorted(roles, key=len, reverse=True):
        if f"@{role}".lower() in lowered:
            return role
    return None

_engineer_apply_claim_lock = threading.Lock()
_active_engineer_apply_projects: set[str] = set()


@contextmanager
def _engineer_apply_claim(project_id: str):
    """Reject a second coroutine before it can enter the repair transaction."""
    normalized = str(project_id)
    with _engineer_apply_claim_lock:
        if normalized in _active_engineer_apply_projects:
            raise ProjectWriteFenceConflict(
                "An Engineer repair transaction is already active"
            )
        _active_engineer_apply_projects.add(normalized)
    try:
        yield
    finally:
        with _engineer_apply_claim_lock:
            _active_engineer_apply_projects.discard(normalized)

# 从 routes_team 导入共享的工程师函数
from api.routes_team import _get_engineer, _build_engineer_context
from core.issue_ledger import (
    canonical_defect_id,
    canonical_path,
    canonicalize_issue,
    mark_needs_manual,
)
from core.project_write_fence import (
    ProjectWriteFenceConflict,
    project_write_guard,
)
from core.workspace_integrity import compute_delivery_manifest, compute_workspace_digest


def _unwrap_qc_entry(qc_entry: Any) -> Dict[str, Any]:
    if not isinstance(qc_entry, dict):
        return {}
    qc_data = qc_entry.get("qa", qc_entry)
    return qc_data if isinstance(qc_data, dict) else {}


def _get_subproject_name(ctx: ProjectContext, sp_id: str, qc_data: Dict[str, Any]) -> str:
    if qc_data.get("subproject_name"):
        return qc_data.get("subproject_name", sp_id)
    subproject = next((sp for sp in ctx.subprojects if sp.get("id") == sp_id), None)
    return subproject.get("name", sp_id) if subproject else sp_id


def _canonical_issue_id(issue: Dict[str, Any]) -> str:
    """Return one deterministic issue identity across QA and engineer APIs."""
    return canonical_defect_id(issue)


def _issue_matches_id(issue: Dict[str, Any], requested_id: Any) -> bool:
    return _canonical_issue_id(issue) == str(requested_id or "").strip()


def _canonical_workspace_path(workspace: Path, file_path: Any) -> str:
    raw = canonical_path(file_path)
    if not raw:
        return ""
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path(workspace) / candidate
    resolved = candidate.resolve()
    try:
        relative = resolved.relative_to(Path(workspace).resolve())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="缺陷文件路径越出项目工作区") from exc
    normalized = relative.as_posix()
    return os.path.normcase(normalized) if os.name == "nt" else normalized


def _defect_is_actionable(defect: Dict[str, Any]) -> bool:
    """Only a current, high-confidence ledger identity may authorize writes."""
    return bool(
        defect.get("status") in {"needs_manual", "open"}
        and defect.get("identity_confidence") == "high"
        and defect.get("requires_identity_review") is not True
    )


def _require_actionable_defect(defect: Dict[str, Any], action: str) -> None:
    if defect.get("status") not in {"needs_manual", "open"}:
        raise HTTPException(
            status_code=409,
            detail=f"缺陷状态 {defect.get('status')} 不允许{action}",
        )
    if (
        defect.get("identity_confidence") != "high"
        or defect.get("requires_identity_review") is True
    ):
        raise HTTPException(
            status_code=409,
            detail="缺陷身份置信度不足，必须先完成 identity review",
        )


def _defect_action_projection(defect: Dict[str, Any]) -> Dict[str, Any]:
    """Expose the server decision and authoritative ownership to UI clients."""
    status = str(defect.get("status") or "open")
    action_allowed = _defect_is_actionable(defect)
    if action_allowed:
        blocked_reason = ""
    elif (
        defect.get("identity_confidence") != "high"
        or defect.get("requires_identity_review") is True
    ):
        blocked_reason = "identity_review_required"
    elif status == "fixing":
        blocked_reason = "repair_in_progress"
    elif status == "pending_verification":
        blocked_reason = "authoritative_reinspection_pending"
    else:
        blocked_reason = f"status_{status}_not_actionable"

    source = defect.get("_source_issue")
    source = source if isinstance(source, dict) else {}
    registration = source.get("authoritative_reinspection_registration")
    registration = registration if isinstance(registration, dict) else {}
    authoritative_identity: Optional[Dict[str, Any]] = None
    if registration or status in {"fixing", "pending_verification"}:
        whole_project = str(defect.get("subproject_id") or "") == "__whole_project__"
        authoritative_identity = {
            "kind": "final_qa" if whole_project else "supervisor",
            "registration_id": str(registration.get("registration_id") or ""),
            "run_id": str(
                registration.get("supervisor_run_id")
                or source.get("authoritative_run_id")
                or ""
            ),
            "artifact_sha256": str(
                registration.get("artifact_digest")
                or source.get("repair_artifact_digest")
                or source.get("repair_delivery_artifact_digest")
                or ""
            ),
        }
    return {
        "action_allowed": action_allowed,
        "blocked_reason": blocked_reason,
        "authoritative_run_identity": authoritative_identity,
    }


def _canonical_defects(ctx: ProjectContext) -> List[Dict[str, Any]]:
    """Return the current server-owned defect ledger projection."""
    defects: List[Dict[str, Any]] = []
    for sp_id, qc_entry in ctx.qc_results.items():
        if not isinstance(qc_entry, dict):
            continue
        layers = (
            [qc_entry]
            if "issues_detail" in qc_entry
            else [value for value in qc_entry.values() if isinstance(value, dict)]
        )
        for qc_data in layers:
            checked_at = float(qc_data.get("checked_at") or 0)
            for issue in qc_data.get("issues_detail", []):
                if not isinstance(issue, dict):
                    continue
                canonical = canonicalize_issue(issue)
                defects.append({
                    **canonical,
                    "id": _canonical_issue_id(issue),
                    "defect_id": _canonical_issue_id(issue),
                    "subproject_id": sp_id,
                    "subproject_name": _get_subproject_name(ctx, sp_id, qc_data),
                    "_checked_at": checked_at,
                    "_source_issue": issue,
                })
    return defects


def _find_canonical_defect(ctx: ProjectContext, defect_id: str) -> Dict[str, Any]:
    wanted = str(defect_id or "").strip()
    defects = _canonical_defects(ctx)
    matches = [defect for defect in defects if defect["id"] == wanted]
    if not matches:
        raise HTTPException(status_code=404, detail="未知 defect_id")
    paths = {
        _canonical_workspace_path(Path(ctx.workspace), item.get("file_path"))
        for item in matches
    }
    if len(paths) > 1:
        raise HTTPException(status_code=409, detail="defect_id 对应多个文件，账本身份冲突")
    return max(matches, key=lambda item: item.get("_checked_at", 0))


async def _start_authoritative_reinspection(
    project_id: str, ctx: ProjectContext, defect: Dict[str, Any]
) -> Dict[str, Any]:
    """Delegate to the one authoritative Supervisor/Final QA state machine."""
    subproject_id = str(defect.get("subproject_id") or "")
    if subproject_id == "__whole_project__":
        registration = defect.get("_source_issue", {}).get(
            "authoritative_reinspection_registration"
        )
        from api.routes_adjustments import start_registered_final_qa
        return await start_registered_final_qa(project_id, registration)

    detected_phase = str(defect.get("detected_phase") or "")
    phase_id = detected_phase
    if not phase_id:
        subproject = next(
            (item for item in ctx.subprojects if item.get("id") == subproject_id),
            None,
        )
        phase_id = str((subproject or {}).get("phase_id") or subproject_id)
    if not phase_id:
        raise HTTPException(
            status_code=409,
            detail="缺陷未绑定阶段，无法进入 authoritative Supervisor 复检",
        )
    registration = defect.get("_source_issue", {}).get(
        "authoritative_reinspection_registration"
    )
    if isinstance(registration, dict) and registration.get("scheduled") is True:
        return {
            "success": True,
            "message": "authoritative Supervisor reinspection synchronously scheduled",
            "registration": registration,
            "status": {"status": "continuing", "running": True},
        }
    from api.routes_phases import start_auto_repair
    return await start_auto_repair(project_id, phase_id, "retry_cycle")


def _register_authoritative_reinspection(
    ctx: ProjectContext,
    defect: Dict[str, Any],
    *,
    writer_capability: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Durably bind phase QA to the repaired artifact before guard release."""
    subproject_id = str(defect.get("subproject_id") or "")
    if subproject_id == "__whole_project__":
        from api.routes_adjustments import (
            _adjustment_required_paths,
            register_final_qa_reinspection,
        )
        expected_artifact = str(
            compute_delivery_manifest(
                Path(ctx.workspace),
                required_paths=_adjustment_required_paths(ctx),
            ).get("artifact_sha256") or ""
        )
        registration = register_final_qa_reinspection(
            ctx,
            expected_artifact,
            writer_capability=writer_capability,
        )
        defect["_source_issue"]["repair_artifact_digest"] = expected_artifact
        defect["_source_issue"][
            "authoritative_reinspection_registration"
        ] = registration
        return registration
    phase_id = str(defect.get("detected_phase") or "")
    if not phase_id:
        subproject = next(
            (item for item in ctx.subprojects if item.get("id") == subproject_id),
            None,
        )
        phase_id = str((subproject or {}).get("phase_id") or subproject_id)
    if not phase_id:
        raise HTTPException(
            status_code=409,
            detail="缺陷未绑定阶段，无法登记 authoritative Supervisor 复检",
        )
    from api.routes_phases import (
        _supervisor_scope_snapshot,
        register_authoritative_reinspection,
    )
    expected_artifact = str(
        _supervisor_scope_snapshot(ctx, phase_id).get("artifact_digest") or ""
    )
    registration = register_authoritative_reinspection(
        ctx, phase_id, expected_artifact
    )
    defect["_source_issue"]["repair_artifact_digest"] = expected_artifact
    defect["_source_issue"]["authoritative_reinspection_registration"] = registration
    return registration


async def _run_engineer_targeted_verification(
    ctx: ProjectContext,
    defect: Dict[str, Any],
    agent: Any,
) -> Dict[str, Any]:
    """Replay only the originating deterministic check after Engineer repair.

    This verifier is deliberately independent from Supervisor/Final QA state.
    It never changes phase state, consumes QA rounds, or reopens a completed
    quality cycle.
    """
    source = defect.get("_source_issue")
    source = source if isinstance(source, dict) else defect
    specification = source.get("verification_spec")
    specification = specification if isinstance(specification, dict) else {}
    kind = str(specification.get("kind") or "").strip().lower()
    layer = str(defect.get("layer") or source.get("layer") or "").strip().lower()
    if not kind and layer == "runtime_acceptance":
        kind = "runtime_acceptance"
        specification = {
            "kind": kind,
            "provenance": "derived_from_deterministic_runtime_finding",
        }
    elif not kind and layer in {"syntax", "logic", "layer1", "layer2"}:
        kind = "static_file"
        specification = {
            "kind": kind,
            "provenance": "derived_from_static_finding",
        }

    path = _canonical_workspace_path(Path(ctx.workspace), defect.get("file_path"))
    if path:
        target = (Path(ctx.workspace) / path).resolve()
        if not target.is_file():
            return {
                "status": "failed",
                "passed": False,
                "retryable": False,
                "reason": "target_file_missing_after_repair",
                "issues": [f"整改目标文件不存在：{path}"],
                "verification_spec": specification,
            }
        content = target.read_text(encoding="utf-8", errors="replace")
        static_result = agent._quick_static_check(str(target), content)
        if static_result.get("passed") is not True:
            return {
                "status": "failed",
                "passed": False,
                "retryable": False,
                "reason": "engineer_static_self_check_failed",
                "issues": list(static_result.get("issues") or []),
                "verification_spec": specification,
            }
    else:
        static_result = {"passed": True, "issues": [], "score": 100}

    if kind == "static_file":
        return {
            "status": "verified",
            "passed": True,
            "retryable": False,
            "reason": "originating_static_check_replayed",
            "issues": [],
            "self_check": static_result,
            "verification_spec": specification,
        }

    if kind == "runtime_acceptance":
        from core.runtime_acceptance import run_runtime_acceptance

        phase_manager = _phase_managers.get(ctx.project_id)
        contract = getattr(phase_manager, "project_contract", None) if phase_manager else None
        try:
            runtime_result = await asyncio.to_thread(
                run_runtime_acceptance,
                Path(ctx.workspace),
                f"{ctx.project_id}:engineer:{defect.get('defect_id') or defect.get('id')}",
                None,
                None,
                contract,
                True,
            )
        except Exception as exc:
            return {
                "status": "deferred",
                "passed": False,
                "retryable": True,
                "reason": "targeted_runtime_verifier_unavailable",
                "issues": [f"{exc.__class__.__name__}: {exc}"],
                "verification_spec": specification,
            }
        if runtime_result.get("passed") is True:
            return {
                "status": "verified",
                "passed": True,
                "retryable": False,
                "reason": "originating_runtime_acceptance_replayed",
                "issues": [],
                "runtime_acceptance": runtime_result,
                "verification_spec": specification,
            }
        retryable = bool(runtime_result.get("retryable")) or not bool(
            runtime_result.get("actionable", True)
        )
        return {
            "status": "deferred" if retryable else "failed",
            "passed": False,
            "retryable": retryable,
            "reason": "originating_runtime_acceptance_still_failing",
            "issues": [str(runtime_result.get("summary") or "定向运行验收未通过")],
            "runtime_acceptance": runtime_result,
            "verification_spec": specification,
        }

    return {
        "status": "deferred",
        "passed": False,
        "retryable": True,
        "reason": "originating_verification_spec_unavailable",
        "issues": ["原缺陷没有可安全重放的 verification_spec"],
        "self_check": static_result,
        "verification_spec": specification,
    }


def _apply_fix_transaction(
    project_id: str,
    ctx: ProjectContext,
    agent: Any,
    request: EngineerApplyFixRequest,
) -> Dict[str, Any]:
    """Validate and mutate once; the caller owns the project write guard."""
    defect = _find_canonical_defect(ctx, request.defect_id)
    _require_actionable_defect(defect, "整改写入")
    canonical_issue_path = _canonical_workspace_path(
        Path(ctx.workspace), defect.get("file_path")
    )
    canonical_request_path = _canonical_workspace_path(
        Path(ctx.workspace), request.file_path
    )
    if not canonical_issue_path or not canonical_request_path:
        raise HTTPException(
            status_code=409,
            detail="缺陷必须精确绑定一个项目内文件后才能整改",
        )
    if canonical_issue_path != canonical_request_path:
        raise HTTPException(
            status_code=409,
            detail="请求 file_path 与 canonical defect 不匹配",
        )
    valid_plan, plan_error = agent.validate_confirmed_fix_plan(
        request.defect_id,
        canonical_issue_path,
        request.confirmed_plan_version,
        str(defect.get("observation_id") or defect.get("fingerprint") or ""),
    )
    if not valid_plan:
        raise HTTPException(status_code=409, detail=plan_error)
    confirmed_plan = agent.get_confirmed_fix_plan(
        request.defect_id, request.confirmed_plan_version
    )
    if not confirmed_plan:
        raise HTTPException(
            status_code=409,
            detail="整改方案确认记录缺失或版本不匹配",
        )
    fix_authorization = agent.get_confirmed_fix_authorization(
        request.defect_id, request.confirmed_plan_version
    )
    if not fix_authorization:
        raise HTTPException(
            status_code=409,
            detail="整改方案缺少服务端生成的变更范围授权，请重新确认方案",
        )

    source_issue = defect["_source_issue"]
    target_path = (Path(ctx.workspace) / canonical_issue_path).resolve()
    if not target_path.is_file():
        raise HTTPException(status_code=409, detail="缺陷目标文件不存在")
    current_baseline_sha256 = hashlib.sha256(target_path.read_bytes()).hexdigest()
    if current_baseline_sha256 != str(
        confirmed_plan.get("baseline_sha256") or ""
    ):
        raise HTTPException(
            status_code=409,
            detail="文件 baseline 在方案确认后已变化，请重新生成并确认整改方案",
        )
    expected_artifact_sha256 = str(
        confirmed_plan.get("target_baseline_artifact_sha256") or ""
    )
    current_artifact_sha256 = str(
        compute_delivery_manifest(Path(ctx.workspace)).get("artifact_sha256") or ""
    )
    if (
        not expected_artifact_sha256
        or current_artifact_sha256 != expected_artifact_sha256
    ):
        raise HTTPException(
            status_code=409,
            detail="整改方案绑定的项目 artifact 已变化，请重新生成并确认方案",
        )
    file_edit_counter = getattr(agent, "file_edit_counter", {})
    file_last_score = getattr(agent, "file_last_score", {})
    counter_key = str(target_path)
    transaction = {
        "agent": agent,
        "defect": defect,
        "source_issue": source_issue,
        "issue_before": copy.deepcopy(source_issue),
        "target_path": target_path,
        "target_existed_before": target_path.exists(),
        "target_bytes_before": target_path.read_bytes(),
        "counter_key": counter_key,
        "file_edit_counter": file_edit_counter,
        "file_last_score": file_last_score,
        "counter_present_before": counter_key in file_edit_counter,
        "edit_count_before": int(file_edit_counter.get(counter_key, 0)),
        "score_present_before": counter_key in file_last_score,
        "score_before": file_last_score.get(counter_key),
        "pending_fixes_before": copy.deepcopy(
            getattr(agent, "pending_fixes", {})
        ),
        "canonical_issue_path": canonical_issue_path,
        "baseline_sha256": current_baseline_sha256,
    }
    result = agent.apply_fix(
        defect_id=request.defect_id,
        file_path=canonical_issue_path,
        new_content=request.new_content,
        run_qa=True,
        defect_info={
            key: value for key, value in defect.items()
            if not key.startswith("_")
        },
        record_memory=False,
        fix_authorization=fix_authorization,
    )
    transaction["result"] = result
    if result.get("success"):
        repair_digest = compute_workspace_digest(Path(ctx.workspace))
        post_repair_sha256 = hashlib.sha256(target_path.read_bytes()).hexdigest()
        repair_delivery_artifact_digest = str(
            compute_delivery_manifest(Path(ctx.workspace)).get("artifact_sha256") or ""
        )
        source_issue["status"] = "pending_verification"
        source_issue["repair_applied_at"] = time.time()
        source_issue["repair_workspace_digest"] = repair_digest
        source_issue["fixed_by"] = "engineer"
        source_issue["confirmed_plan_version"] = request.confirmed_plan_version
        source_issue["repair_target_sha256"] = post_repair_sha256
        source_issue["repair_delivery_artifact_digest"] = (
            repair_delivery_artifact_digest
        )
        result["status"] = "pending_verification"
        result["requires_final_qa"] = True
        result["repair_workspace_digest"] = repair_digest
        result["repair_target_sha256"] = post_repair_sha256
        result["repair_delivery_artifact_digest"] = repair_delivery_artifact_digest
    return transaction


def _atomic_restore_bytes(target: Path, content: bytes) -> None:
    """Restore the exact pre-repair bytes without exposing a partial file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.urandom(8).hex()}.rollback")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_fix_transaction(transaction: Dict[str, Any]) -> None:
    """Compensate every local mutation made by ``_apply_fix_transaction``."""
    target_path = transaction["target_path"]
    if transaction["target_existed_before"]:
        _atomic_restore_bytes(target_path, transaction["target_bytes_before"])
    else:
        target_path.unlink(missing_ok=True)

    source_issue = transaction["source_issue"]
    source_issue.clear()
    source_issue.update(copy.deepcopy(transaction["issue_before"]))

    counter_key = transaction["counter_key"]
    file_edit_counter = transaction["file_edit_counter"]
    file_last_score = transaction["file_last_score"]
    if transaction["counter_present_before"]:
        file_edit_counter[counter_key] = transaction["edit_count_before"]
    else:
        file_edit_counter.pop(counter_key, None)
    if transaction["score_present_before"]:
        file_last_score[counter_key] = transaction["score_before"]
    else:
        file_last_score.pop(counter_key, None)
    agent = transaction["agent"]
    if hasattr(agent, "pending_fixes"):
        agent.pending_fixes = copy.deepcopy(transaction["pending_fixes_before"])


def _complete_identity_review(
    ctx: ProjectContext,
    agent: Any,
    defect_id: str,
    request: EngineerIdentityReviewRequest,
) -> Dict[str, Any]:
    """Replace one low-confidence observation with a reviewed canonical identity."""
    defect = _find_canonical_defect(ctx, defect_id)
    if (
        defect.get("identity_confidence") != "low"
        or defect.get("requires_identity_review") is not True
    ):
        raise HTTPException(
            status_code=409,
            detail="该缺陷不处于 requires_identity_review 状态",
        )
    if defect.get("status") not in {"needs_manual", "open"}:
        raise HTTPException(status_code=409, detail="当前缺陷状态不允许身份补录")
    rule_id = str(request.rule_id or "").strip()
    review_reason = str(request.review_reason or "").strip()
    if not rule_id or not review_reason:
        raise HTTPException(status_code=422, detail="rule_id 和 review_reason 必填")
    structured_values = {
        "symbol": str(request.symbol or "").strip(),
        "location": str(request.location or "").strip(),
        "expected": request.expected,
        "actual": request.actual,
    }
    if not any(value not in (None, "") for value in structured_values.values()):
        raise HTTPException(
            status_code=422,
            detail="必须补充 symbol/location/expected/actual 中至少一项",
        )

    source_issue = defect["_source_issue"]
    old_defect_id = str(defect.get("defect_id") or defect_id)
    old_observation_id = str(defect.get("observation_id") or "")
    generation = uuid.uuid4().hex
    reviewed_input = {
        **copy.deepcopy(source_issue),
        "rule_id": rule_id,
        **{
            key: value
            for key, value in structured_values.items()
            if value not in (None, "")
        },
    }
    context = {
        "identity_review_generation": generation,
        "previous_defect_id": old_defect_id,
    }
    if defect.get("status") == "needs_manual":
        reviewed = mark_needs_manual(
            reviewed_input,
            str(
                source_issue.get("needs_manual_reason")
                or "Manual repair required after identity review"
            ),
            observation_context=context,
        )
    else:
        reviewed = canonicalize_issue(
            reviewed_input,
            observation_context=context,
        )
    if (
        reviewed.get("identity_confidence") != "high"
        or reviewed.get("requires_identity_review") is True
    ):
        raise HTTPException(status_code=409, detail="补录内容仍不足以建立高置信身份")
    new_defect_id = str(reviewed.get("defect_id") or "")
    for candidate in _canonical_defects(ctx):
        if (
            candidate.get("_source_issue") is not source_issue
            and candidate.get("defect_id") == new_defect_id
        ):
            raise HTTPException(status_code=409, detail="补录身份与现有缺陷冲突")

    aliases = list(source_issue.get("identity_aliases") or [])
    for alias in (
        old_defect_id,
        source_issue.get("id"),
        source_issue.get("issue_id"),
        source_issue.get("legacy_id"),
        source_issue.get("source_issue_id"),
    ):
        value = str(alias or "").strip()
        if value and value != new_defect_id and value not in aliases:
            aliases.append(value)
    history = list(source_issue.get("observation_history") or [])
    history.append({
        "defect_id": old_defect_id,
        "observation_id": old_observation_id,
        "status": str(defect.get("status") or ""),
        "superseded_by_generation": generation,
        "superseded_at": time.time(),
    })
    reviewed["identity_aliases"] = aliases
    reviewed["observation_history"] = history
    reviewed["identity_review"] = {
        "generation": generation,
        "actor": str(getattr(ctx, "owner_user_id", "") or "project-owner"),
        "reason": review_reason,
        "previous_defect_id": old_defect_id,
        "previous_observation_id": old_observation_id,
        "reviewed_at": time.time(),
    }
    source_issue.clear()
    source_issue.update(reviewed)

    # Authority issued for the superseded low-confidence identity must never
    # carry over to the new canonical identity.
    if hasattr(agent, "confirmed_fix_plans"):
        agent.confirmed_fix_plans.pop(old_defect_id, None)
    if hasattr(agent, "pending_fix_proposals"):
        agent.pending_fix_proposals = {
            digest: proposal
            for digest, proposal in agent.pending_fix_proposals.items()
            if str((proposal or {}).get("defect_id") or "") != old_defect_id
        }
    return {
        **{
            key: value
            for key, value in reviewed.items()
            if not key.startswith("_")
        },
        **_defect_action_projection({
            **reviewed,
            "subproject_id": defect.get("subproject_id"),
            "_source_issue": source_issue,
        }),
    }


@router.get("/engineer/{project_id}/status")
async def engineer_status(project_id: str):
    """获取全能工程师 Agent 状态"""
    _get_project(project_id)  # 校验项目存在
    agent = _get_engineer(project_id)
    return agent.get_status()

@router.get("/engineer/{project_id}/defects")
async def engineer_get_defects(project_id: str):
    """
    获取项目内所有 needs_manual 状态的缺陷单列表。
    来源：ctx.qc_results 中的 issues_detail，status == 'needs_manual'
    """
    ctx = _get_project(project_id)
    manual_defects = [
        {
            **{
                key: value
                for key, value in defect.items()
                if not key.startswith("_")
            },
            **_defect_action_projection(defect),
        }
        for defect in _canonical_defects(ctx)
        if defect.get("status") == "needs_manual"
    ]
    return {"defects": manual_defects, "total": len(manual_defects)}

@router.get("/engineer/{project_id}/all-defects")
async def engineer_get_all_defects(project_id: str, status: Optional[str] = None):
    """
    获取项目内所有状态的缺陷单列表（供前端按状态筛选）。
    status 参数可选：needs_manual / open / fixing / fixed / verified / escalated
    不传则返回全部。

    去重逻辑：同一 id 的缺陷可能来自多次质检，保留 checked_at 最新的
    canonical ledger observation；状态优先级不能代表时间，否则 verified
    后重新复现的 open 缺陷会被隐藏。
    """
    ctx = _get_project(project_id)

    # Latest ledger observation wins. Status priority is unsafe because it
    # permanently hides a verified defect that a later QA run reopened.
    dedupe_map: Dict[str, Dict[str, Any]] = {}
    for defect in _canonical_defects(ctx):
        previous = dedupe_map.get(defect["id"])
        if previous is None or float(defect.get("_checked_at") or 0) >= float(
            previous.get("_checked_at") or 0
        ):
            dedupe_map[defect["id"]] = defect
    all_defects = [
        {
            **{
                key: value
                for key, value in defect.items()
                if not key.startswith("_")
            },
            **_defect_action_projection(defect),
        }
        for defect in dedupe_map.values()
    ]

    # 按 status 过滤（去重后再过滤）
    if status:
        all_defects = [d for d in all_defects if d["status"] == status]

    return {"defects": all_defects, "total": len(all_defects)}


@router.post("/engineer/{project_id}/defects/{defect_id}/identity-review")
async def engineer_complete_identity_review(
    project_id: str,
    defect_id: str,
    request: EngineerIdentityReviewRequest,
):
    """Manually supplement structure for one low-confidence ledger identity."""
    ctx = _get_project(project_id)
    agent = _get_engineer(project_id)
    _build_engineer_context(project_id, agent)
    try:
        with (
            _engineer_apply_claim(project_id),
            project_write_guard(project_id, Path(ctx.workspace)),
        ):
            reviewed = _complete_identity_review(ctx, agent, defect_id, request)
    except ProjectWriteFenceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _persist_all_async()
    return {"success": True, "defect": reviewed}


@router.post("/engineer/{project_id}/chat/repair")
async def engineer_chat_repair(project_id: str, request: EngineerRepairChatRequest):
    """
    整改对话（每个 defect_id 独立上下文，互不污染）。
    第一轮传入 defect_info 让 Agent 了解缺陷详情。
    Agent 输出整改方案，用户确认后调用 /apply-fix 执行。
    """
    ctx = _get_project(project_id)
    agent = _get_engineer(project_id)
    _build_engineer_context(project_id, agent)
    defect = _find_canonical_defect(ctx, request.defect_id)
    if defect.get("status") not in {"needs_manual", "open"}:
        raise HTTPException(status_code=409, detail="该缺陷当前状态不允许创建整改方案")
    all_defects = [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in _canonical_defects(ctx)
    ]
    result = agent.chat_repair(
        defect_id=request.defect_id,
        user_input=request.message,
        defect_info={key: value for key, value in defect.items() if not key.startswith("_")},
        all_defects=all_defects,
    )
    proposal_digest = str(result.get("proposal_digest") or "")
    proposal = getattr(agent, "pending_fix_proposals", {}).get(proposal_digest)
    if proposal_digest and isinstance(proposal, dict):
        manifest = compute_delivery_manifest(Path(ctx.workspace))
        proposal.update({
            "target_baseline_artifact_sha256": str(
                manifest.get("artifact_sha256") or ""
            ),
            "target_baseline_file_sha256": str(
                proposal.get("baseline_sha256") or ""
            ),
            "actor": str(getattr(ctx, "owner_user_id", "") or "project-owner"),
            "scope": copy.deepcopy(proposal.get("authorization") or {}),
        })
        bound_payload = {
            key: copy.deepcopy(proposal.get(key))
            for key in (
                "defect_id",
                "file_path",
                "issue_version",
                "baseline_sha256",
                "target_baseline_file_sha256",
                "target_baseline_artifact_sha256",
                "reply",
                "authorization",
                "actor",
                "scope",
            )
        }
        bound_digest = hashlib.sha256(
            json.dumps(
                bound_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        proposal["proposal_digest"] = bound_digest
        proposal["proposal_generation"] = bound_digest
        if bound_digest != proposal_digest:
            agent.pending_fix_proposals.pop(proposal_digest, None)
            agent.pending_fix_proposals[bound_digest] = proposal
        result["proposal_digest"] = bound_digest
    await _persist_all_async()
    return result

@router.post("/engineer/{project_id}/confirm-fix-plan")
async def engineer_confirm_fix_plan(
    project_id: str, request: EngineerConfirmFixRequest
):
    """Explicitly confirm one immutable proposal; chat text is never consent."""
    ctx = _get_project(project_id)
    agent = _get_engineer(project_id)
    _build_engineer_context(project_id, agent)
    try:
        with project_write_guard(
            project_id, Path(ctx.workspace)
        ) as writer_capability:
            defect = _find_canonical_defect(ctx, request.defect_id)
            _require_actionable_defect(defect, "确认整改方案")
            canonical_issue_path = _canonical_workspace_path(
                Path(ctx.workspace), defect.get("file_path")
            )
            canonical_request_path = _canonical_workspace_path(
                Path(ctx.workspace), request.file_path
            )
            if not canonical_issue_path or canonical_issue_path != canonical_request_path:
                raise HTTPException(
                    status_code=409,
                    detail="确认请求 file_path 与 canonical defect 不匹配",
                )
            observation_id = str(defect.get("observation_id") or "")
            if not observation_id or observation_id != request.observation_id:
                raise HTTPException(
                    status_code=409,
                    detail="确认请求绑定的 observation_id 已过期",
                )
            target = (Path(ctx.workspace) / canonical_issue_path).resolve()
            if not target.is_file():
                raise HTTPException(status_code=409, detail="缺陷目标文件不存在")
            baseline_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
            proposal = getattr(agent, "pending_fix_proposals", {}).get(
                request.proposal_digest
            )
            if not isinstance(proposal, dict):
                raise HTTPException(status_code=409, detail="整改 proposal 不存在")
            expected_artifact = str(
                proposal.get("target_baseline_artifact_sha256") or ""
            )
            current_artifact = str(
                compute_delivery_manifest(Path(ctx.workspace)).get(
                    "artifact_sha256"
                ) or ""
            )
            if not expected_artifact or current_artifact != expected_artifact:
                raise HTTPException(
                    status_code=409,
                    detail="proposal 绑定的项目 artifact 已变化，请重新生成方案",
                )
            confirmation = agent.confirm_fix_proposal(
                defect_id=request.defect_id,
                proposal_digest=request.proposal_digest,
                issue_version=observation_id,
                file_path=canonical_issue_path,
                baseline_sha256=baseline_sha256,
                allow_whole_file=request.allow_whole_file,
            )
            if not confirmation.get("success"):
                raise HTTPException(
                    status_code=409,
                    detail=confirmation.get("error") or "整改 proposal 确认失败",
                )
            confirmed = getattr(agent, "confirmed_fix_plans", {}).get(
                request.defect_id
            )
            if isinstance(confirmed, dict):
                confirmed.update({
                    "actor": str(
                        getattr(ctx, "owner_user_id", "") or "project-owner"
                    ),
                    "target_baseline_artifact_sha256": expected_artifact,
                    "target_baseline_file_sha256": baseline_sha256,
                    "scope": copy.deepcopy(
                        confirmed.get("authorization") or {}
                    ),
                    "confirmation_generation": str(
                        confirmation.get("confirmed_plan_version") or ""
                    ),
                })
    except ProjectWriteFenceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _persist_all_async()
    return confirmation

@router.post("/engineer/{project_id}/apply-fix")
async def engineer_apply_fix(project_id: str, request: EngineerApplyFixRequest):
    """
    执行代码整改：将新内容写入文件（Immutability），同时触发 layer1+layer2 静态验证。
    写完后将对应缺陷状态从 needs_manual 更新为 fixing（等待下次质检复检）。
    """
    ctx = _get_project(project_id)
    agent = _get_engineer(project_id)
    _build_engineer_context(project_id, agent)
    if not request.run_qa:
        raise HTTPException(
            status_code=409,
            detail="apply-fix 必须执行工程师自检与原问题定向复验",
        )
    try:
        with (
            _engineer_apply_claim(project_id),
            project_write_guard(project_id, Path(ctx.workspace)),
        ):
            transaction = _apply_fix_transaction(project_id, ctx, agent, request)
            result = transaction["result"]
            if not result.get("success"):
                return result

            defect = transaction["defect"]
            source_issue = transaction["source_issue"]
            target_path = transaction["target_path"]
            verification = await _run_engineer_targeted_verification(
                ctx, defect, agent
            )
            verification["checked_at"] = time.time()
            verification["artifact_sha256"] = str(
                compute_delivery_manifest(Path(ctx.workspace)).get(
                    "artifact_sha256"
                ) or ""
            )

            if verification.get("status") == "failed":
                _restore_fix_transaction(transaction)
                source_issue = transaction["source_issue"]
                source_issue["engineer_targeted_verification"] = verification
                source_issue["last_engineer_repair_failed_at"] = time.time()
                result = {
                    **result,
                    "success": False,
                    "status": str(source_issue.get("status") or "needs_manual"),
                    "rolled_back": True,
                    "requires_final_qa": False,
                    "targeted_verification": verification,
                    "message": "原问题定向复验未通过，本次整改已原子回滚",
                }
                await _persist_all_async()
                return result

            source_issue["engineer_targeted_verification"] = verification
            source_issue.pop("authoritative_reinspection_registration", None)
            if verification.get("passed") is True:
                source_issue["status"] = "verified"
                source_issue["verified_at"] = time.time()
                source_issue["verified_by"] = "engineer_targeted_verifier"
                result["status"] = "verified"
                result["message"] = "整改已写入，并通过工程师自检与原问题定向复验"
            else:
                source_issue["status"] = "pending_verification"
                source_issue["verification_deferred_at"] = time.time()
                result["status"] = "pending_verification"
                result["message"] = (
                    "整改已写入并通过本地自检；原问题暂不可安全重放，"
                    "已标记待复检，不进入阶段 QA/QC"
                )
            result["requires_final_qa"] = False
            result["targeted_verification"] = verification
    except ProjectWriteFenceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if hasattr(agent, "add_project_memory"):
        agent.add_project_memory(
            f"[第{result.get('edit_count', 1)}次整改] "
            f"{target_path.name} — {str(defect.get('message') or '')[:60]}",
            memory_type="issue",
        )
    await _persist_all_async()
    return result

@router.post("/engineer/{project_id}/generate-manual")
async def engineer_generate_manual(project_id: str, request: EngineerManualRequest):
    """
    一键生成使用手册（保存到 workspace/docs/ 并返回内容）。
    manual_type: user=用户手册 / api=API文档 / deploy=部署手册
    """
    ctx = _get_project(project_id)
    agent = _get_engineer(project_id)
    _build_engineer_context(project_id, agent)
    try:
        with project_write_guard(project_id, Path(ctx.workspace)):
            result = agent.generate_manual(
                manual_type=request.manual_type,
                extra_instruction=request.extra_instruction,
            )
    except ProjectWriteFenceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result

@router.post("/engineer/{project_id}/archive-files")
async def engineer_archive_files(project_id: str):
    """
    扫描 workspace，对所有文件进行分类（source/test/doc/config/output/useless）
    并标注所属开发阶段。返回归档结果和统计数据。
    """
    ctx = _get_project(project_id)
    agent = _get_engineer(project_id)
    _build_engineer_context(project_id, agent)

    # 从 PhaseManager 获取文件-阶段映射
    phase_registry: Dict[str, str] = {}
    if ctx.project_id in _phase_managers:
        pm_inst = _phase_managers[ctx.project_id]
        try:
            registry = pm_inst.file_registry if hasattr(pm_inst, "file_registry") else {}
            for fpath, info in registry.items():
                if isinstance(info, dict):
                    phase_registry[fpath] = info.get("phase_id", "")
                elif isinstance(info, str):
                    phase_registry[fpath] = info
        except Exception:
            pass

    try:
        with project_write_guard(project_id, Path(ctx.workspace)):
            result = agent.archive_files(phase_registry=phase_registry)
    except ProjectWriteFenceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result

@router.get("/engineer/{project_id}/archived-files")
async def engineer_get_archived_files(project_id: str):
    """获取最近一次归档结果（不重新扫描）"""
    _get_project(project_id)
    agent = _get_engineer(project_id)
    cached = agent.get_archive_result()
    if not cached:
        return {"files": [], "stats": {}, "total": 0, "message": "尚未执行归档，请先点击「扫描归档」"}
    return cached


@router.get("/engineer/{project_id}/consultations")
async def engineer_list_consultations(project_id: str):
    ctx = _get_project(project_id)
    return {
        "sessions": _consultation_sessions(project_id),
        "participants": _consultation_roles(ctx),
    }


@router.post("/engineer/{project_id}/consultations")
async def engineer_create_consultation(
    project_id: str, request: EngineerConsultationCreateRequest
):
    ctx = _get_project(project_id)
    mode = str(request.mode or "inquiry").strip().lower()
    if mode not in {"inquiry", "change", "rectification"}:
        raise HTTPException(status_code=422, detail="不支持的会话类型")
    source_issue_binding = None
    source_issue_id = str(request.source_issue_id or "").strip()
    if source_issue_id:
        if mode != "rectification":
            raise HTTPException(status_code=422, detail="source_issue_id only applies to rectification sessions")
        defect = _find_canonical_defect(ctx, source_issue_id)
        source_issue_binding = {
            "defect_id": str(defect.get("defect_id") or defect.get("id") or source_issue_id),
            "observation_id": str(defect.get("observation_id") or ""),
            "verification_spec": copy.deepcopy(
                (defect.get("_source_issue") or {}).get("verification_spec") or {}
            ),
            "bound_at": time.time(),
        }
    sessions = _consultation_sessions(project_id)
    now = time.time()
    session = {
        "id": f"consult-{uuid.uuid4().hex[:10]}",
        "mode": mode,
        "title": str(request.title or "").strip() or ({
            "inquiry": "项目问答", "change": "功能变更讨论", "rectification": "项目整改",
        }[mode]),
        "messages": [],
        "created_at": now,
        "updated_at": now,
    }
    if source_issue_binding:
        session["source_issue_binding"] = source_issue_binding
    sessions.insert(0, session)
    _save_consultation_sessions(project_id, sessions)
    return {"session": session}


@router.patch("/engineer/{project_id}/consultations/{session_id}")
async def engineer_rename_consultation(
    project_id: str,
    session_id: str,
    request: EngineerConsultationRenameRequest,
):
    _get_project(project_id)
    title = str(request.title or "").strip()
    if not title or len(title) > 80:
        raise HTTPException(status_code=422, detail="对话名称长度必须为 1-80 个字符")
    sessions = _consultation_sessions(project_id)
    session = next((item for item in sessions if item.get("id") == session_id), None)
    if not session:
        raise HTTPException(status_code=404, detail="咨询会话不存在")
    session["title"] = title
    session["updated_at"] = time.time()
    _save_consultation_sessions(project_id, sessions)
    return {"session": session}


@router.post("/engineer/{project_id}/consultations/{session_id}/messages")
async def engineer_send_consultation_message(
    project_id: str,
    session_id: str,
    request: EngineerConsultationMessageRequest,
):
    ctx = _get_project(project_id)
    text = str(request.message or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="消息不能为空")
    sessions = _consultation_sessions(project_id)
    session = next((item for item in sessions if item.get("id") == session_id), None)
    if not session:
        raise HTTPException(status_code=404, detail="咨询会话不存在")

    roles = _consultation_roles(ctx)
    agent = _get_engineer(project_id)
    _build_engineer_context(project_id, agent)
    project_background = str(getattr(agent, "project_background", "") or "")
    responder = _mentioned_role(text, roles) or ("全栈工程师" if session.get("mode") == "rectification" else "PM组长")
    mode = str(session.get("mode") or "inquiry")
    mode_instruction = {
        "inquiry": "只回答现有项目的功能、实现、文件与技术细节，不提出或执行文件修改。",
        "change": "与用户讨论现有项目的新增、删除或修改需求，形成可交给全栈工程师的明确方案；本会话只讨论，不写文件。",
        "rectification": "分析用户提交的最终整改方案，澄清影响文件和实现细节；此对话阶段不写文件，确认后由系统生成可审核的文件变更提案。",
    }.get(mode, "只进行项目咨询，不写文件。")
    participants = "、".join(roles)
    system_prompt = (
        f"你正在 METIS 项目咨询工作群中，以【{responder}】身份回答。\n"
        f"{mode_instruction}\n"
        f"可用成员：{participants}。如果问题超出职责，可在回答末尾用 @角色 明确转交。\n"
        "必须基于项目背景，不编造；回答简洁，必要时引用具体文件。\n\n"
        f"项目名称：{ctx.name}\n项目描述：{ctx.description}\n"
        f"项目背景与已确认规划：{project_background[:12000]}"
    )
    history = list(session.get("messages") or [])[-20:]
    messages = [Message(role=MessageRole.SYSTEM, content=system_prompt)]
    for item in history:
        role = MessageRole.USER if item.get("role") == "user" else MessageRole.ASSISTANT
        messages.append(Message(role=role, content=str(item.get("content") or "")))
    messages.append(Message(role=MessageRole.USER, content=text))
    try:
        reply = str(_get_hermes(project_id).chat(messages).get("content") or "").strip()
    except Exception as exc:
        logger.exception("Engineer consultation failed project=%s session=%s", project_id, session_id)
        raise HTTPException(status_code=503, detail="项目咨询暂时不可用") from exc

    now = time.time()
    appended = [
        {"role": "user", "speaker": "用户", "content": text, "ts": now},
        {"role": "assistant", "speaker": responder, "content": reply, "ts": time.time()},
    ]
    delegated_to = _mentioned_role(reply, roles)
    if delegated_to and delegated_to != responder:
        transfer_prompt = (
            f"你是【{delegated_to}】，前一位成员【{responder}】把用户问题转交给你。"
            "请基于项目背景直接回答用户原问题，不重复转述。\n\n"
            f"用户问题：{text}\n前一位成员说明：{reply}\n项目：{ctx.name}\n"
            f"项目背景与规划：{project_background[:10000]}"
        )
        try:
            delegated_reply = str(_get_hermes(project_id).chat([
                Message(role=MessageRole.SYSTEM, content=transfer_prompt),
                Message(role=MessageRole.USER, content=text),
            ]).get("content") or "").strip()
            appended.append({
                "role": "assistant", "speaker": delegated_to,
                "content": delegated_reply, "ts": time.time(),
            })
        except Exception:
            logger.exception("Engineer consultation delegation failed project=%s session=%s", project_id, session_id)
    session.setdefault("messages", []).extend(appended)
    session["updated_at"] = time.time()
    if not str(session.get("title") or "").strip() or session.get("title") in {"项目问答", "功能变更讨论", "项目整改"}:
        session["title"] = text[:28]
    _save_consultation_sessions(project_id, sessions)
    return {"session": session, "responder": responder, "participants": roles}


@router.post("/engineer/{project_id}/rectifications/prepare")
async def engineer_prepare_rectification(
    project_id: str, request: EngineerRectificationPrepareRequest
):
    ctx = _get_project(project_id)
    sessions = _consultation_sessions(project_id)
    session = next((item for item in sessions if item.get("id") == request.session_id), None)
    if not session or session.get("mode") != "rectification":
        raise HTTPException(status_code=404, detail="项目整改会话不存在")
    history = list(session.get("messages") or [])
    if not any(item.get("role") == "user" for item in history):
        raise HTTPException(status_code=409, detail="请先提交并讨论整改方案")

    workspace = Path(ctx.workspace).resolve()
    allowed_suffixes = {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".md", ".yml", ".yaml", ".html", ".css"}
    snapshots: List[str] = []
    used = 0
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in allowed_suffixes:
            continue
        if any(part in {".git", "node_modules", "dist", "build", ".project", "__pycache__"} for part in path.parts):
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        excerpt = content[:8000]
        if used + len(excerpt) > 80000 or len(snapshots) >= 40:
            break
        rel = path.relative_to(workspace).as_posix()
        snapshots.append(f"\n--- FILE {rel} ---\n{excerpt}")
        used += len(excerpt)

    conversation = "\n".join(
        f"{item.get('speaker') or item.get('role')}: {item.get('content') or ''}"
        for item in history[-20:]
    )
    prompt = (
        "你是全栈工程师。根据已确认的整改对话与项目文件，生成最小必要变更。"
        "只返回严格 JSON：{\"summary\":\"...\",\"changes\":[{\"path\":\"相对路径\",\"content\":\"完整文件内容\",\"reason\":\"...\"}]}。"
        "不得修改未涉及文件，不得使用绝对路径，不得省略完整文件内容，最多 10 个文件。\n\n"
        f"整改对话：\n{conversation}\n\n项目文件：{''.join(snapshots)}"
    )
    try:
        raw = str(_get_hermes(project_id).chat([
            Message(role=MessageRole.SYSTEM, content="输出必须是可解析的严格 JSON，不要 Markdown。"),
            Message(role=MessageRole.USER, content=prompt),
        ]).get("content") or "").strip()
        parsed = _parse_rectification_response(raw)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="整改方案未生成有效的结构化文件变更") from exc
    raw_changes = parsed.get("changes") if isinstance(parsed, dict) else None
    if not isinstance(raw_changes, list) or not raw_changes or len(raw_changes) > 10:
        raise HTTPException(status_code=422, detail="整改方案必须包含 1-10 个文件变更")

    agent = _get_engineer(project_id)
    changes = []
    total_size = 0
    for item in raw_changes:
        rel = str((item or {}).get("path") or "").replace("\\", "/").strip()
        content = str((item or {}).get("content") or "")
        try:
            target = agent._resolve_within_workspace(rel)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"非法整改路径：{rel}") from exc
        total_size += len(content.encode("utf-8"))
        if not rel or not content or total_size > 250000:
            raise HTTPException(status_code=422, detail="整改内容为空或总大小超过限制")
        check = agent._quick_static_check(str(target), content)
        if not check.get("passed"):
            raise HTTPException(status_code=422, detail=f"{rel} 自检失败：{check.get('issues')}")
        changes.append({"path": rel, "content": content, "reason": str((item or {}).get("reason") or ""), "self_check": check})

    proposal_id = f"rect-{uuid.uuid4().hex[:12]}"
    proposal = {
        "id": proposal_id,
        "summary": str(parsed.get("summary") or "项目整改"),
        "changes": changes,
        "baseline_artifact_sha256": compute_delivery_manifest(workspace).get("artifact_sha256"),
        "status": "pending_confirm",
        "created_at": time.time(),
    }
    if isinstance(session.get("source_issue_binding"), dict):
        proposal["source_issue_binding"] = copy.deepcopy(session["source_issue_binding"])
    session["proposal"] = proposal
    session["updated_at"] = time.time()
    _save_consultation_sessions(project_id, sessions)
    return {"proposal": proposal}


@router.post("/engineer/{project_id}/rectifications/apply")
async def engineer_apply_rectification(
    project_id: str, request: EngineerRectificationApplyRequest
):
    ctx = _get_project(project_id)
    sessions = _consultation_sessions(project_id)
    session = next((item for item in sessions if item.get("id") == request.session_id), None)
    proposal = session.get("proposal") if isinstance(session, dict) else None
    if not isinstance(proposal, dict) or proposal.get("id") != request.proposal_id:
        raise HTTPException(status_code=404, detail="整改提案不存在")
    if proposal.get("status") != "pending_confirm":
        raise HTTPException(status_code=409, detail="整改提案已执行或已失效")
    workspace = Path(ctx.workspace).resolve()
    current_digest = compute_delivery_manifest(workspace).get("artifact_sha256")
    if current_digest != proposal.get("baseline_artifact_sha256"):
        raise HTTPException(status_code=409, detail="项目文件已变化，请重新生成整改提案")

    agent = _get_engineer(project_id)
    originals: Dict[str, Optional[bytes]] = {}
    tracked_agent_state = {
        name: copy.deepcopy(getattr(agent, name))
        for name in ("file_edit_counter", "file_last_score", "pending_fixes")
        if hasattr(agent, name)
    }
    results = []
    verification: Optional[Dict[str, Any]] = None
    bound_defect: Optional[Dict[str, Any]] = None
    source_issue: Optional[Dict[str, Any]] = None
    source_issue_before: Optional[Dict[str, Any]] = None
    binding = proposal.get("source_issue_binding")
    if isinstance(binding, dict):
        bound_defect = _find_canonical_defect(ctx, str(binding.get("defect_id") or ""))
        if str(bound_defect.get("observation_id") or "") != str(binding.get("observation_id") or ""):
            raise HTTPException(status_code=409, detail="The bound issue changed; regenerate the proposal")
        source_issue = bound_defect.get("_source_issue")
        if not isinstance(source_issue, dict):
            raise HTTPException(status_code=409, detail="The bound source issue is unavailable")
        source_issue_before = copy.deepcopy(source_issue)

    def rollback_changes() -> None:
        for rel, original in originals.items():
            target = agent._resolve_within_workspace(rel)
            if original is None:
                target.unlink(missing_ok=True)
            else:
                _atomic_restore_bytes(target, original)
        for name, value in tracked_agent_state.items():
            setattr(agent, name, copy.deepcopy(value))
        if source_issue is not None and source_issue_before is not None:
            source_issue.clear()
            source_issue.update(copy.deepcopy(source_issue_before))
    try:
        with _engineer_apply_claim(project_id), project_write_guard(project_id, workspace):
            for change in proposal.get("changes") or []:
                target = agent._resolve_within_workspace(change["path"])
                originals[change["path"]] = target.read_bytes() if target.exists() else None
                result = agent.apply_fix(
                    defect_id=f"rectification:{proposal['id']}:{change['path']}",
                    file_path=change["path"],
                    new_content=change["content"],
                    run_qa=False,
                    record_memory=True,
                    fix_authorization={"diff_budget": 100000},
                )
                if not result.get("success"):
                    raise RuntimeError(result.get("message") or result.get("error") or "文件自检失败")
                results.append(result)
            if bound_defect is not None:
                verification = await _run_engineer_targeted_verification(ctx, bound_defect, agent)
                verification["checked_at"] = time.time()
                verification["artifact_sha256"] = str(
                    compute_delivery_manifest(workspace).get("artifact_sha256") or ""
                )
                if verification.get("status") == "failed":
                    rollback_changes()
                    if source_issue is not None:
                        source_issue["engineer_targeted_verification"] = verification
                        source_issue["last_engineer_repair_failed_at"] = time.time()
                    proposal.update({"status": "verification_failed", "verification": verification, "rolled_back": True})
                    session["updated_at"] = time.time()
                    _save_consultation_sessions(project_id, sessions)
                    await _persist_all_async()
                    return {"success": False, "rolled_back": True, "requires_final_qa": False,
                            "proposal": proposal, "targeted_verification": verification}
                if source_issue is not None:
                    source_issue["engineer_targeted_verification"] = verification
                    source_issue.pop("authoritative_reinspection_registration", None)
                    if verification.get("passed") is True:
                        source_issue.update({"status": "verified", "verified_at": time.time(),
                                             "verified_by": "engineer_targeted_verifier"})
                    else:
                        source_issue.update({"status": "pending_verification",
                                             "verification_deferred_at": time.time()})
            await _persist_all_async()
    except Exception as exc:
        rollback_changes()
        raise HTTPException(status_code=409, detail=f"项目整改已回滚：{exc}") from exc

    proposal["status"] = (
        "applied_verified" if verification and verification.get("passed") is True
        else "applied_pending_verification" if verification
        else "applied"
    )
    proposal["applied_at"] = time.time()
    proposal["results"] = results
    proposal["verification"] = verification or {
        "status": "self_checked",
        "passed": True,
        "reason": "generic_rectification_has_no_bound_originating_check",
    }
    session.setdefault("messages", []).append({
        "role": "assistant", "speaker": "全栈工程师",
        "content": f"整改完成：已修改 {len(results)} 个文件，全部通过工程师自检。",
        "ts": time.time(),
    })
    _save_consultation_sessions(project_id, sessions)
    return {
        "success": True,
        "proposal": proposal,
        "results": results,
        "requires_final_qa": False,
        "targeted_verification": verification,
    }

@router.post("/engineer/{project_id}/chat/qa")
async def engineer_chat_qa(project_id: str, request: EngineerQAChatRequest):
    """
    项目问答（独立上下文，与整改对话完全隔离）。
    可回答关于项目技术方案、功能模块、文件位置、阶段规划等任意问题。
    """
    _get_project(project_id)
    agent = _get_engineer(project_id)
    _build_engineer_context(project_id, agent)
    return agent.chat_qa(user_input=request.message)

@router.post("/engineer/{project_id}/run-qa")
async def engineer_run_qa(project_id: str, request: EngineerQAInspectRequest):
    """
    整改完成后由用户手动触发质检（质检组长 QAAgent 负责）。
    与阶段质检逻辑相同，结果更新到 ctx.qc_results。
    """
    ctx = _get_project(project_id)
    agent = _get_engineer(project_id)
    _build_engineer_context(project_id, agent)
    synthetic_defect = {
        "subproject_id": request.subproject_id,
        "detected_phase": "" if request.subproject_id == "__whole_project__" else request.subproject_id,
    }
    # This endpoint never inspects or mutates the ledger itself. It delegates
    # once to the authoritative state machine, eliminating A/B QA results.
    return await _start_authoritative_reinspection(project_id, ctx, synthetic_defect)

@router.post("/engineer/{project_id}/load-context")
async def engineer_load_context(project_id: str):
    """
    （重新）从 PM 组长 final_plan + 子项目 + 质检结果 全量加载项目背景到全能工程师 Agent。
    适用场景：
      - 项目规划确认后手动调用
      - 后端重启后 Agent 背景丢失时重新注入
      - 新的子项目/质检结果产生后同步给 Agent
    """
    ctx = _get_project(project_id)
    agent = _get_engineer(project_id)
    _build_engineer_context(project_id, agent)
    has_plan = bool(agent.project_background)
    return {
        "success": True,
        "has_plan": has_plan,
        "has_subprojects": len(getattr(agent, "_subprojects", [])) > 0,
        "has_qc_results": len(getattr(agent, "_qc_results", {})) > 0,
        "background_length": len(agent.project_background),
        "message": "项目完整背景已加载（规划+子项目+质检汇总）" if has_plan else "尚未生成项目规划，注入了项目基本信息",
    }

@router.get("/engineer/{project_id}/file-edit-stats")
async def engineer_file_edit_stats(project_id: str):
    """
    获取全能工程师对各文件的修改次数统计。
    前端可用于展示哪些文件已被反复修改（超过 3 次上限），
    辅助判断是否需要重新梳理根因。
    """
    _get_project(project_id)
    agent = _get_engineer(project_id)
    return agent.get_file_edit_stats()

# ─── 项目调整（变更工单）─────────────────────────────────────────────────────
#
# 项目完成后，用户在「团队频道」描述调整需求，PM 组长分析影响范围，
# 拆解为多专家变更工单，各专家并行/串行执行，自动触发质检闭环。
# ─────────────────────────────────────────────────────────────────────────────

# 内存存储：project_id → List[adjustment]
_adjustments: Dict[str, List[Dict]] = {}

def _get_adjustments(project_id: str) -> List[Dict]:
    if project_id not in _adjustments:
        _adjustments[project_id] = []
    return _adjustments[project_id]

class AdjustmentChatRequest(BaseModel):
    message: str
    history: Optional[List[Dict]] = None
    context_summary: Optional[str] = None

class AdjustmentConfirmRequest(BaseModel):
    adjustment_id: str
    confirmed: bool
    modifications: Optional[str] = ""

class AdjustmentPhaseConfirmRequest(BaseModel):
    adjustment_id: str
    phase_index: int   # 0-based 阶段序号

