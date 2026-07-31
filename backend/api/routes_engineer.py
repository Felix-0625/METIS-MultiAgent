"""工程师工作台路由"""
import copy
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
            detail="apply-fix 必须进入 authoritative Supervisor/Final QA 强制复检",
        )
    try:
        # Validation, bytes, ledger, authoritative ownership, and task startup
        # are one project-writer transaction.  In particular, whole-project
        # Final QA receives its canonical artifact registration before this
        # guard can release.
        with project_write_guard(
            project_id, Path(ctx.workspace)
        ) as writer_capability:
            transaction = _apply_fix_transaction(project_id, ctx, agent, request)
            result = transaction["result"]
            if not result.get("success"):
                return result

            defect = transaction["defect"]
            source_issue = transaction["source_issue"]
            target_path = transaction["target_path"]
            try:
                registration = _register_authoritative_reinspection(
                    ctx,
                    defect,
                    writer_capability=writer_capability,
                )
                transaction["authoritative_registration"] = registration
                result["authoritative_registration"] = registration
                inspection = await _start_authoritative_reinspection(
                    project_id, ctx, defect
                )
            except Exception as schedule_error:
                registration = transaction.get("authoritative_registration")
                if (
                    str(defect.get("subproject_id") or "") == "__whole_project__"
                    and isinstance(registration, dict)
                ):
                    from api.routes_adjustments import cancel_registered_final_qa
                    cancel_registered_final_qa(
                        ctx,
                        str(registration.get("registration_id") or ""),
                        str(registration.get("artifact_digest") or ""),
                    )

                # Refuse compensation if any non-participating writer changed
                # the repaired generation despite the project guard.
                expected_artifact = str(
                    result.get("repair_delivery_artifact_digest") or ""
                )
                current_artifact = str(
                    compute_delivery_manifest(Path(ctx.workspace)).get(
                        "artifact_sha256"
                    ) or ""
                )
                expected_target = str(result.get("repair_target_sha256") or "")
                current_target = (
                    hashlib.sha256(target_path.read_bytes()).hexdigest()
                    if target_path.is_file()
                    else ""
                )
                if (
                    not expected_artifact
                    or current_artifact != expected_artifact
                    or not expected_target
                    or current_target != expected_target
                ):
                    manual_issue = mark_needs_manual(
                        source_issue,
                        (
                            "Authoritative QA scheduling failed after the repair "
                            "generation changed; automatic rollback was refused"
                        ),
                    )
                    source_issue.clear()
                    source_issue.update(manual_issue)
                    source_issue["rollback_refused_artifact_digest"] = current_artifact
                    source_issue["rollback_refused_target_sha256"] = current_target
                    await _persist_all_async()
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "整改后的 workspace 已被再次修改；为避免覆盖并发写入，"
                            "自动回滚已拒绝，需要人工恢复"
                        ),
                    ) from schedule_error

                _restore_fix_transaction(transaction)
                await _persist_all_async()
                raise
    except ProjectWriteFenceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if hasattr(agent, "add_project_memory"):
        agent.add_project_memory(
            f"[第{result.get('edit_count', 1)}次整改] "
            f"{target_path.name} — {str(defect.get('message') or '')[:60]}",
            memory_type="issue",
        )
    result["authoritative_reinspection"] = inspection
    result["message"] = (
        f"{result.get('message', '整改已写入')}；已进入 authoritative 复检，"
        "当前结果不是 Final QA 通过证明"
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

