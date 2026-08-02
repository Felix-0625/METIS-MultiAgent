"""PM 团队路由"""
import asyncio
import copy
import time
import json
import logging
import uuid
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
    ExecutionAgent, IdeaLandingAgent,
    logger,
)
from models.schemas import (
    ProjectRequest, AnalyzeRequest, SubprojectConfirmRequest,
    SubprojectRequest, AgentCreateRequest, AgentApiConfigRequest,
    SkillImportRequest, SkillSearchRequest, ChangeRequest,
    GiteeConfigRequest, GiteePushRequest, DefaultApiConfigRequest,
    SupervisorChatRequest, RepairStartRequest, ProposalSubmitRequest,
    ProposalReviewRequest, ArbiterForcePassRequest,
    SkillIngestRequest, SkillConfirmRequest, ChatHistorySaveRequest,
    FileWriteRequest, FileStagingRequest, FileCommitRequest,
    FileRollbackRequest, HRReassignRequest, PMTeamChatRequest,
    PlanConfirmRequest, SupervisorReviewChatRequest,
    PhasePMChatRequest, PhaseDescUpdateRequest,
    IssueSubmitToPMRequest, BatchIssueSubmitToPMRequest,
    PhasePlanExpertRequest, ExpertRequirement, PhaseExpertMatchRequest,
    ExpertAssignment, PhaseExpertConfirmRequest,
    EmployeeCreateRequest, EmployeeUpdateRequest,
    ExpertCreateRequest, ExpertUpdateRequest,
    ExpertMemoryRequest, ExpertMatchRequest,
    ExpertTrainingRequest, ExpertWorkModeRequest,
    ExpertConfigRequest, ExpertChatTrainRequest,
    ExpertFeedbackRequest, ExpertKnowledgeRequest,
    CCBCheckDeleteMemberRequest, CCBCheckDeleteExpertRequest,
    CCBConfirmDeleteRequest, EngineerRepairChatRequest,
    EngineerApplyFixRequest, EngineerManualRequest,
    EngineerQAChatRequest, EngineerQAInspectRequest,
    AdjustmentChatRequest, AdjustmentConfirmRequest,
    AdjustmentPhaseConfirmRequest, InjectQCRequest,
    IdeaChatRequest, IdeaNewConvRequest, IdeaConvMetaRequest,
    IdeaPinRequest, IdeaAdvancePhaseRequest, IdeaUserMemoryRequest,
    ProjectTeamAssignRequest, RequirementsRevisionRequest,
)
from api.routes_team import _engineer_agents, _get_engineer
from agents.base.hermes_agent import AgentType
from core.phase_manager import UnsupportedExecutionRoleError
from core.project_contract import validate_plan_for_confirmation
from core.json_utils import extract_first_json_array

router = APIRouter(tags=["pm"])

FULLSTACK_ENGINEER_EXPERT_ID = "expert-fullstack-engineer-001"


def _requirements_metadata(leader: PMLeaderAgent) -> Dict[str, Any]:
    return {
        "requirements_revision": leader.requirements_revision,
        "requirements_digest": leader.requirements_digest,
    }


def _requirements_match(
    leader: PMLeaderAgent,
    expected_revision: int,
    expected_digest: str,
) -> bool:
    return (
        leader.requirements_revision == expected_revision
        and leader.requirements_digest == expected_digest
    )


def _invalidate_requirement_bound_plan(
    leader: PMLeaderAgent,
    *,
    reason: str,
) -> None:
    stale_plan = leader.final_plan or leader.draft_plan
    if stale_plan is not None:
        leader.blocked_draft = copy.deepcopy(stale_plan)
    leader.draft_plan = None
    leader.final_plan = None
    leader.project_contract = {}
    leader.plan_generation = {}
    leader.plan_confirmed = False
    leader.plan_status = "validation_failed"
    leader.draft_blocked_reason = reason


_REQUIREMENT_STATE_FIELDS = (
    "canonical_requirements",
    "requirements_revision",
    "requirements_digest",
    "requirement_events",
    "draft_plan",
    "final_plan",
    "plan_confirmed",
    "project_contract",
    "plan_generation",
    "plan_status",
    "draft_blocked_reason",
    "blocked_draft",
    "last_plan_violations",
)
_requirements_transaction_locks: Dict[tuple[int, str], asyncio.Lock] = {}


class _PersistedConfirmationRejection(HTTPException):
    """A confirmation rejection whose state is already durable."""


class _AlreadyHeldRequirementsLock:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args) -> None:
        return None


def _requirements_transaction_lock(project_id: str) -> asyncio.Lock:
    loop_id = id(asyncio.get_running_loop())
    return _requirements_transaction_locks.setdefault(
        (loop_id, project_id), asyncio.Lock()
    )


def _snapshot_requirement_state(leader: PMLeaderAgent) -> Dict[str, Any]:
    return {
        field: copy.deepcopy(getattr(leader, field))
        for field in _REQUIREMENT_STATE_FIELDS
    }


def _restore_requirement_state(
    leader: PMLeaderAgent,
    snapshot: Dict[str, Any],
) -> None:
    for field, value in snapshot.items():
        setattr(leader, field, value)


def _phase_execution_lifecycle_started(
    project_id: str,
    phase_manager: Any,
) -> bool:
    """Return whether changing the canonical contract would orphan execution."""
    phases = list(getattr(phase_manager, "phases", None) or [])
    phase_ids = {
        str(phase.get("phase_id") or "")
        for phase in phases
        if isinstance(phase, dict) and phase.get("phase_id")
    }
    if any(
        phase.get("execution_generation")
        or phase.get("started_at")
        or str(phase.get("status") or "").lower()
        not in {"", "pending", "planned", "ready"}
        for phase in phases
        if isinstance(phase, dict)
    ):
        return True
    ctx = projects.get(project_id)
    if not ctx:
        return False
    if any(
        bool(agent_phase_id := str(agent.get("phase_id") or ""))
        and (not phase_ids or agent_phase_id in phase_ids)
        for agent in (getattr(ctx, "agents", {}) or {}).values()
        if isinstance(agent, dict)
    ):
        return True
    if any(
        bool(child_phase_id := str(child.get("phase_id") or ""))
        and (not phase_ids or child_phase_id in phase_ids)
        and child.get("agent_id")
        for child in (getattr(ctx, "subprojects", []) or [])
        if isinstance(child, dict)
    ):
        return True
    return any(
        bool(supervisor_phase_id := str(phase_id or ""))
        and (not phase_ids or supervisor_phase_id in phase_ids)
        for phase_id in (
            getattr(ctx, "supervisor_quality_runs", {}) or {}
        )
    )


async def _commit_requirements_transaction(
    project_id: str,
    leader: PMLeaderAgent,
    mutate,
) -> Any:
    """Serialize canonical CAS, plan invalidation, phase invalidation and disk."""
    async with _requirements_transaction_lock(project_id):
        snapshot = _snapshot_requirement_state(leader)
        previous_phase_manager = _phase_managers.get(project_id)
        execution_started = _phase_execution_lifecycle_started(
            project_id,
            previous_phase_manager,
        )
        try:
            result = mutate()
        except Exception:
            _restore_requirement_state(leader, snapshot)
            raise
        revision_changed = (
            leader.requirements_revision != snapshot["requirements_revision"]
            or leader.requirements_digest != snapshot["requirements_digest"]
        )
        if revision_changed and execution_started:
            _restore_requirement_state(leader, snapshot)
            raise ValueError(
                "canonical requirements are locked after phase execution starts"
            )
        if revision_changed:
            _phase_managers.pop(project_id, None)
        try:
            await _persist_all_async()
        except Exception as exc:
            _restore_requirement_state(leader, snapshot)
            if previous_phase_manager is not None:
                _phase_managers[project_id] = previous_phase_manager
            else:
                _phase_managers.pop(project_id, None)
            raise HTTPException(
                status_code=503,
                detail="canonical requirements persistence failed",
            ) from exc
        return result


async def _canonical_snapshot_for_cas(
    project_id: str,
    ctx: Any,
    leader: PMLeaderAgent,
    expected_revision: int,
    expected_digest: str,
) -> str:
    def initialize_and_read() -> str:
        if (
            expected_revision != leader.requirements_revision
            or expected_digest != leader.requirements_digest
        ):
            raise ValueError("requirements revision conflict")
        if (
            not leader.canonical_requirements
            and str(getattr(ctx, "description", "") or "").strip()
        ):
            leader.record_user_requirements(
                ctx.description,
                source="user",
                expected_revision=expected_revision,
                expected_digest=expected_digest,
            )
        if not leader.canonical_requirements:
            raise ValueError("canonical requirements cannot be empty")
        return leader.canonical_requirements

    return await _commit_requirements_transaction(
        project_id, leader, initialize_and_read
    )


_PRIVATE_REQUIREMENT_FIELDS = {
    "source_requirements",
    "requirements_summary",
    "requirement_units",
    "source_constraints",
}


def _public_pm_payload(value: Any) -> Any:
    """Return a UI DTO without canonical requirement or attachment text."""
    if isinstance(value, dict):
        return {
            key: _public_pm_payload(item)
            for key, item in value.items()
            if key not in _PRIVATE_REQUIREMENT_FIELDS
        }
    if isinstance(value, list):
        return [_public_pm_payload(item) for item in value]
    return copy.deepcopy(value)


async def _commit_synthesis_result(
    project_id: str,
    leader: PMLeaderAgent,
    *,
    bound_revision: int,
    bound_digest: str,
    result: Dict[str, Any],
    rollback_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """CAS and persist one synthesis result as a single linearized commit."""
    async with _requirements_transaction_lock(project_id):
        if not _requirements_match(leader, bound_revision, bound_digest):
            _invalidate_requirement_bound_plan(
                leader,
                reason="requirements_revision_changed_during_synthesis",
            )
            try:
                await _persist_all_async()
            except Exception:
                if rollback_snapshot is not None:
                    _restore_requirement_state(leader, rollback_snapshot)
                raise
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "requirements changed during synthesis; regenerate",
                    **_requirements_metadata(leader),
                },
            )
        try:
            await _persist_all_async()
        except Exception:
            if rollback_snapshot is not None:
                _restore_requirement_state(leader, rollback_snapshot)
            raise
        return _public_pm_payload({
            **result,
            **_requirements_metadata(leader),
        })


def _allows_fullstack_expert(role_label: str) -> bool:
    role_lower = (role_label or "").lower()
    return any(keyword in role_lower for keyword in ("全栈", "全能工程师", "fullstack"))


@router.post("/projects/{project_id}/analyze")
async def analyze_requirements(project_id: str, request: AnalyzeRequest):
    ctx = _get_project(project_id)
    # 把前端历史转为 PMAgent 期望的格式 [{"role": "user"/"assistant", "content": "..."}]
    history = []
    for msg in (request.history or []):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            history.append({"role": role, "content": content})
    result = ctx.pm.analyze_requirement(
        request.requirements,
        history=history,
        context_summary=request.context_summary,
    )
    await _persist_all_async()
    return result

@router.post("/projects/{project_id}/design")
async def design_solution(project_id: str, requirements: Dict):
    ctx = _get_project(project_id)
    return ctx.pm.design_solution(requirements)

@router.get("/projects/{project_id}/subprojects")
async def get_subprojects(project_id: str):
    ctx = _get_project(project_id)
    return {"subprojects": ctx.pm.subprojects}

@router.post("/projects/{project_id}/subprojects/confirm")
async def confirm_subproject(project_id: str, request: SubprojectConfirmRequest):
    ctx = _get_project(project_id)
    result = ctx.pm.confirm_subproject(request.subproject_id, request.confirmed, request.modifications or "")
    await _persist_all_async()
    return result

@router.post("/projects/{project_id}/plan/generate")
async def generate_plan(project_id: str):
    ctx = _get_project(project_id)
    result = ctx.pm.generate_plan()
    if result.get("success"):
        ctx.status = "team_building"
        await _persist_all_async()
    return result

async def _extract_subprojects_from_history(ctx: "ProjectContext") -> List[Dict]:
    """
    从 PM Agent 对话历史中用 LLM 提取子项目列表。
    返回 [] 表示提取失败或历史为空。
    """
    from core.hermes_client import Message, MessageRole

    # 收集对话历史：优先用内存历史，其次用持久化历史
    hist = ctx.pm.conversation_history or []
    if not hist:
        # 尝试从持久化文件读取
        try:
            hist = load_chat_history(ctx.project_id, "pm")
        except Exception:
            hist = []

    if not hist:
        return []

    # 拼接对话文本
    lines = []
    for h in hist:
        role_label = "用户" if h.get("role") == "user" else "PM Agent"
        content = h.get("content", "")
        if content:
            lines.append(f"{role_label}：{content[:500]}")  # 截断避免 token 超限
    history_text = "\n".join(lines[-30:])  # 最近 30 条

    extract_prompt = [
        Message(role=MessageRole.SYSTEM, content=(
            "你是一个项目管理助手。请从以下 PM Agent 对话历史中提取子项目列表。\n\n"
            "输出格式（严格 JSON 数组，不要有其他文字）：\n"
            '[{"id":"sp-001","name":"子项目名称","description":"简短描述","roles_needed":["开发工程师"],"tech_stack":[],"priority":"normal"}]\n\n'
            "规则：\n"
            "- 如果对话中明确提到了子项目/模块/功能模块，按实际提取\n"
            "- 如果没有明确子项目，根据项目整体目标拆分为 2-4 个合理的子项目\n"
            "- 只输出 JSON 数组，不要有任何解释文字"
        )),
        Message(role=MessageRole.USER, content=f"项目名称：{ctx.name}\n项目描述：{ctx.description}\n\n对话历史：\n{history_text}"),
    ]

    try:
        resp = ctx.pm.hermes.chat(extract_prompt)
        content = resp.get("content", "").strip()
        # 健壮解析：提取首个 JSON 数组，兼容 markdown 包裹/尾随文字
        subprojects = extract_first_json_array(content)
        if isinstance(subprojects, list):
            if subprojects:
                # 确保每个子项目有必要字段
                result = []
                for i, sp in enumerate(subprojects):
                    result.append({
                        "id": sp.get("id") or f"sp-{i+1:03d}",
                        "name": sp.get("name") or f"子项目 {i+1}",
                        "description": sp.get("description") or "",
                        "roles_needed": sp.get("roles_needed") or ["开发工程师"],
                        "tech_stack": sp.get("tech_stack") or [],
                        "priority": sp.get("priority") or "normal",
                        "status": "pending",
                    })
                return result
    except Exception:
        pass
    return []

@router.post("/projects/{project_id}/launch")
async def launch_project(project_id: str):
    """
    一键启动项目：
    1. 从 PM Agent 获取当前规划（subprojects + team_requirements）
    2. HR Agent 根据规划创建执行 Agent，分配 Skill
    3. Supervisor Agent 为每个子项目创建任务并开始调度
    4. 更新项目状态为 running
    
    **必须用户手动确认草稿规划后才可启动**
    """
    ctx = _get_project(project_id)

    # ── 检查用户是否已确认草稿规划 ────────────────────────────────────────
    pm_leader = _pm_teams.get(project_id)
    if (
        pm_leader is not None
        and pm_leader.draft_blocked_reason
        == "legacy_confirmed_requires_regeneration"
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Legacy confirmed plan is read-only and cannot execute; "
                "regenerate and confirm a ProjectContract v3 plan"
            ),
        )
    if not pm_leader or not pm_leader.plan_confirmed:
        raise HTTPException(
            status_code=400,
            detail="项目尚未确认规划。请先生成草稿规划并确认后再启动项目。"
        )
    if not pm_leader.final_plan:
        raise HTTPException(
            status_code=400,
            detail="未找到已确认的最终规划，无法启动项目。"
        )

    artifact_metadata_payload = pm_leader.final_plan.get("artifact_metadata") or {}
    plan_revision = pm_leader.final_plan.get(
        "requirements_revision",
        artifact_metadata_payload.get("requirements_revision"),
    )
    plan_digest = pm_leader.final_plan.get(
        "requirements_digest",
        artifact_metadata_payload.get("requirements_digest"),
    )
    if pm_leader.requirements_revision and (
        plan_revision != pm_leader.requirements_revision
        or plan_digest != pm_leader.requirements_digest
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "confirmed plan requirements binding is stale",
                **_requirements_metadata(pm_leader),
            },
        )
    expected = pm_leader.canonical_requirements or str(
        (pm_leader.project_contract or {}).get("source_requirements") or ""
    )
    validation = validate_plan_for_confirmation(
        pm_leader.final_plan,
        pm_leader.project_contract or pm_leader.final_plan.get("project_contract"),
        expected_source_requirements=expected,
    )
    if not validation.valid:
        raise HTTPException(
            status_code=409,
            detail={"message": "Confirmed plan no longer satisfies the canonical contract",
                    "validation": validation.to_dict()},
        )
    phase_manager = await asyncio.to_thread(_get_phase_manager, project_id)
    if not phase_manager.phases:
        phase_manager.init_phases_from_plan(pm_leader.get_final_plan_for_hr() or pm_leader.final_plan)
        for phase in phase_manager.phases:
            phase["requirements_revision"] = pm_leader.requirements_revision
            phase["requirements_digest"] = pm_leader.requirements_digest
    if any(
        phase.get("requirements_revision") != pm_leader.requirements_revision
        or phase.get("requirements_digest") != pm_leader.requirements_digest
        for phase in phase_manager.phases
    ):
        raise HTTPException(
            status_code=409,
            detail="phase snapshot requirements binding is stale",
        )
    first_pending = next(
        (phase for phase in phase_manager.phases if phase.get("status", "pending") == "pending"),
        None,
    )
    if not first_pending:
        raise HTTPException(status_code=409, detail="No pending confirmed phase is eligible for launch")
    from api.routes_phases import start_phase
    result = await start_phase(project_id, str(first_pending["phase_id"]))
    return {**result, "launch_alias": True}

# 每个项目的 PM 团队实例（懒加载）

def _get_pm_team(project_id: str) -> PMLeaderAgent:
    if project_id not in _pm_teams:
        ctx = _get_project(project_id)
        memory = HybridMemory(f"memory/{project_id}_pm_team")
        leader = PMLeaderAgent(hermes_client=hermes_client, memory_store=memory)
        _pm_teams[project_id] = leader
    return _pm_teams[project_id]

def _get_supervisor_leader(project_id: str) -> SupervisorLeaderAgent:
    if project_id not in _supervisor_leaders:
        ctx = _get_project(project_id)
        memory = HybridMemory(f"memory/{project_id}_sup_leader")
        leader = SupervisorLeaderAgent(
            hermes_client=hermes_client,
            memory_store=memory,
            project_id=project_id,
        )
        _supervisor_leaders[project_id] = leader
    return _supervisor_leaders[project_id]

def _get_phase_manager(project_id: str) -> PhaseManager:
    if project_id not in _phase_managers:
        ctx = _get_project(project_id)
        _phase_managers[project_id] = PhaseManager(project_id, ctx.workspace)
    return _phase_managers[project_id]

@router.get("/projects/{project_id}/pm-team/members")
async def get_pm_team_members(project_id: str):
    """获取 PM 团队成员列表"""
    _get_project(project_id)
    leader = await asyncio.to_thread(_get_pm_team, project_id)
    return {
        "leader": {"agent_id": leader.agent_id, "name": "PM 组长", "type": "pm_leader"},
        "members": leader.get_members_info(),
        "plan_confirmed": leader.plan_confirmed,
        "has_draft_plan": leader.draft_plan is not None,
    }

@router.post("/projects/{project_id}/pm-team/chat")
async def pm_team_chat(project_id: str, request: PMTeamChatRequest):
    """
    与 PM 团队组长对话（强制流程：需求分析→方案讨论→方案确认）
    """
    ctx = _get_project(project_id)
    leader = _get_pm_team(project_id)
    revision_request = request.requirements_revision
    needs_initialization = (
        not leader.canonical_requirements
        and bool(str(getattr(ctx, "description", "") or "").strip())
    )
    if revision_request is not None or needs_initialization:
        def apply_chat_requirements() -> Dict[str, Any]:
            if revision_request is not None:
                if not str(revision_request.content or "").strip():
                    raise ValueError("canonical requirements cannot be empty")
                if (
                    revision_request.expected_revision != leader.requirements_revision
                    or revision_request.expected_digest != leader.requirements_digest
                ):
                    raise ValueError("requirements revision conflict")
            if (
                not leader.canonical_requirements
                and str(getattr(ctx, "description", "") or "").strip()
            ):
                leader.record_user_requirements(ctx.description, source="user")
            if revision_request is not None:
                return leader.record_user_requirements(
                    revision_request.content,
                    source=revision_request.source,
                    expected_revision=leader.requirements_revision,
                    expected_digest=leader.requirements_digest,
                    supersedes=revision_request.supersedes,
                    replace=revision_request.replace,
                )
            return _requirements_metadata(leader)

        try:
            await _commit_requirements_transaction(
                project_id, leader, apply_chat_requirements
            )
        except ValueError as exc:
            status_code = 422 if "cannot be empty" in str(exc) else 409
            raise HTTPException(
                status_code=status_code,
                detail={"message": str(exc), **_requirements_metadata(leader)},
            ) from exc
    history = [
        {"role": m.get("role"), "content": m.get("content", "")}
        for m in (request.history or [])
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    result = leader.chat_with_user(
        user_input=request.message,
        history=history,
        context_summary=request.context_summary,
    )
    await _persist_all_async()
    return {**result, **_requirements_metadata(leader)}


@router.post("/projects/{project_id}/pm-team/requirements/revise")
async def revise_pm_requirements(
    project_id: str,
    request: RequirementsRevisionRequest,
):
    """Apply one explicit canonical-requirements event using CAS."""
    ctx = _get_project(project_id)
    leader = await asyncio.to_thread(_get_pm_team, project_id)

    def apply_revision() -> Dict[str, Any]:
        if (
            request.expected_revision != leader.requirements_revision
            or request.expected_digest != leader.requirements_digest
        ):
            raise ValueError("requirements revision conflict")
        if not str(request.content or "").strip():
            raise ValueError("canonical requirements cannot be empty")
        if (
            not leader.canonical_requirements
            and str(getattr(ctx, "description", "") or "").strip()
        ):
            leader.record_user_requirements(
                ctx.description,
                source="user",
                expected_revision=request.expected_revision,
                expected_digest=request.expected_digest,
            )
            expected_revision = leader.requirements_revision
            expected_digest = leader.requirements_digest
        else:
            expected_revision = request.expected_revision
            expected_digest = request.expected_digest
        return leader.record_user_requirements(
            request.content,
            source=request.source,
            expected_revision=expected_revision,
            expected_digest=expected_digest,
            supersedes=request.supersedes,
            replace=request.replace,
        )

    try:
        result = await _commit_requirements_transaction(
            project_id, leader, apply_revision
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = 422 if "cannot be empty" in detail else 409
        raise HTTPException(
            status_code=status_code,
            detail={"message": detail, **_requirements_metadata(leader)},
        ) from exc
    return {**result, **_requirements_metadata(leader)}


@router.get("/projects/{project_id}/pm-team/requirements/revisions")
async def get_pm_requirement_revisions(project_id: str):
    """Expose revision lineage metadata without canonical requirement text."""
    _get_project(project_id)
    leader = await asyncio.to_thread(_get_pm_team, project_id)
    return {
        **_requirements_metadata(leader),
        "events": [
            {
                "event_id": event.get("event_id"),
                "content_digest": event.get("content_digest"),
                "source": event.get("source"),
                "revision": event.get("revision"),
                "supersedes": list(event.get("supersedes") or []),
                "superseded": bool(event.get("superseded")),
            }
            for event in leader.requirement_events
        ],
    }


@router.post("/projects/{project_id}/pm-team/collect-analyses")
async def collect_pm_analyses(project_id: str, body: Dict):
    """
    触发所有 PM 成员分析需求
    body: {"requirements": "需求描述"}
    """
    ctx = _get_project(project_id)
    leader = _get_pm_team(project_id)
    if "requirements" in body:
        raise HTTPException(
            status_code=422,
            detail="requirements text is server-managed; send revision and digest only",
        )
    if "requirements_revision" not in body or "requirements_digest" not in body:
        raise HTTPException(
            status_code=422,
            detail="requirements_revision and requirements_digest are required",
        )
    try:
        requested_revision = int(body["requirements_revision"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="requirements_revision must be an integer",
        ) from exc
    requested_digest = str(body["requirements_digest"])
    try:
        requirements = await _canonical_snapshot_for_cas(
            project_id, ctx, leader, requested_revision, requested_digest
        )
    except ValueError as exc:
        status_code = 422 if "cannot be empty" in str(exc) else 409
        raise HTTPException(
            status_code=status_code,
            detail={"message": str(exc), **_requirements_metadata(leader)},
        ) from exc
    result = leader.collect_member_analyses(requirements)
    return _public_pm_payload({**result, **_requirements_metadata(leader)})

@router.post("/projects/{project_id}/pm-team/synthesize")
async def synthesize_pm_plan(project_id: str, body: Dict):
    """
    PM 组长汇总各成员分析，生成方案草稿
    body: {"requirements": "需求描述", "fast_mode": true}
    fast_mode=true 时跳过多成员分析，直接由组长一次性生成（速度快）
    """
    ctx = _get_project(project_id)
    leader = await asyncio.to_thread(_get_pm_team, project_id)
    if "requirements" in body:
        raise HTTPException(
            status_code=422,
            detail="requirements text is server-managed; send revision and digest only",
        )
    if "requirements_revision" not in body or "requirements_digest" not in body:
        raise HTTPException(
            status_code=422,
            detail="requirements_revision and requirements_digest are required",
        )
    try:
        requested_revision = int(body.get("requirements_revision"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="requirements_revision must be an integer") from exc
    requested_digest = str(body.get("requirements_digest") or "")
    try:
        requirements = await _canonical_snapshot_for_cas(
            project_id, ctx, leader, requested_revision, requested_digest
        )
    except ValueError as exc:
        status_code = 422 if "cannot be empty" in str(exc) else 409
        raise HTTPException(
            status_code=status_code,
            detail={
                "message": str(exc),
                **_requirements_metadata(leader),
            },
        ) from exc
    bound_revision = leader.requirements_revision
    bound_digest = leader.requirements_digest
    synthesis_snapshot = _snapshot_requirement_state(leader)
    fast_mode = body.get("fast_mode", False)
    if not str(requirements).strip():
        leader.plan_status = "validation_failed"
        leader.plan_generation = {
            "status": "validation_failed", "model_status": None,
            "attempts": [], "validation": None, "updated_at": time.time(),
        }
        result = {
            "success": False,
            "status": "validation_failed",
            "message": "需求文本不能为空，未生成或保存规划草稿",
            "violations": ["requirements must be a non-empty string"],
            "validation": {
                "valid": False,
                "issues": [{
                    "layer": "json_schema", "code": "missing_requirements", "path": "$.requirements",
                    "message": "requirements must be a non-empty string",
                }],
            },
            "can_confirm": False,
        }
        return await _commit_synthesis_result(
            project_id,
            leader,
            bound_revision=bound_revision,
            bound_digest=bound_digest,
            result=result,
            rollback_snapshot=synthesis_snapshot,
        )

    try:
        if fast_mode:
        # 快速模式：直接由 PM 组长生成，不走多成员分析
            result = await asyncio.to_thread(
                leader.synthesize_plan_fast,
                requirements,
                bound_revision,
                bound_digest,
            )
        else:
            result = await asyncio.to_thread(
                leader.synthesize_plan,
                requirements,
                bound_revision,
                bound_digest,
            )
    except Exception as exc:
        logger.error("PM plan synthesis failed (%s)", type(exc).__name__)
        leader.plan_status = "model_failed"
        leader.plan_generation = {
            "status": "model_failed",
            "validation": None,
            "attempts": [{
                "attempt": 1, "status": "model_failed",
                "code": "server_generation_error", "error_type": type(exc).__name__,
            }],
            "updated_at": time.time(),
        }
        result = {
            "success": False,
            "status": "model_failed",
            "message": "总规划模型调用失败，未保存无效草稿",
            "violations": ["server exception during plan generation"],
            "validation": None,
            "generation": leader.plan_generation,
            "can_confirm": False,
        }
    return await _commit_synthesis_result(
        project_id,
        leader,
        bound_revision=bound_revision,
        bound_digest=bound_digest,
        result=result,
        rollback_snapshot=synthesis_snapshot,
    )


async def _commit_confirmation_result(
    project_id: str,
    ctx: Any,
    leader: PMLeaderAgent,
    *,
    expected_revision: int,
    expected_digest: str,
    result: Dict[str, Any],
    _lock_already_held: bool = False,
) -> Dict[str, Any]:
    """CAS, project the confirmed plan, and persist under one requirements lock."""
    lock_context = (
        _AlreadyHeldRequirementsLock()
        if _lock_already_held
        else _requirements_transaction_lock(project_id)
    )
    async with lock_context:
        if not _requirements_match(leader, expected_revision, expected_digest):
            _invalidate_requirement_bound_plan(
                leader,
                reason="requirements_revision_changed_during_confirmation",
            )
            await _persist_all_async()
            raise _PersistedConfirmationRejection(
                status_code=409,
                detail={
                    "message": "requirements changed during confirmation; regenerate",
                    **_requirements_metadata(leader),
                },
            )

        if not result.get("success"):
            await _persist_all_async()
            issue_codes = {
                str(issue.get("code") or "")
                for issue in (result.get("validation") or {}).get("issues") or []
                if isinstance(issue, dict)
            }
            if issue_codes & {
                "phase_tasks_empty",
                "phase_roles_empty",
                "phase_acceptance_criteria_empty",
                "source_requirements_missing",
                "source_requirements_mismatch",
                "requirements_summary_mismatch",
                "requirements_digest_mismatch",
            }:
                raise _PersistedConfirmationRejection(
                    status_code=422, detail=result,
                )
            if result.get("status") == "requirements_revision_conflict":
                raise _PersistedConfirmationRejection(
                    status_code=409,
                    detail=_public_pm_payload(result),
                )
            return _public_pm_payload(result)

        if leader.plan_confirmed and leader.final_plan:
            result["status"] = "confirmed"
            result["can_launch"] = True
        elif not leader.final_plan:
            result["status"] = "needs_plan"
            result["can_launch"] = False
            result.setdefault("success", False)

        if leader.plan_confirmed and leader.final_plan:
            final_plan = leader.get_final_plan_for_hr()
            raw_plan = leader.final_plan

            phase_manager = await asyncio.to_thread(
                _get_phase_manager,
                project_id,
            )
            if final_plan:
                phase_manager.init_phases_from_plan(final_plan)
                for phase in phase_manager.phases:
                    phase["requirements_revision"] = expected_revision
                    phase["requirements_digest"] = expected_digest
                ctx.subprojects = []
                for phase in phase_manager.phases:
                    ctx.subprojects.append({
                        "id": phase["phase_id"],
                        "name": phase["name"],
                        "description": phase.get("description", ""),
                        "phase_id": phase["phase_id"],
                        "roles_needed": phase.get(
                            "roles_needed",
                            ["开发工程师"],
                        ),
                        "tech_stack": phase.get("tech_stack", []),
                        "priority": "normal",
                        "status": "pending",
                        "progress": 0,
                        "agent_count": phase.get("agent_count", 1),
                    })

            supervisor = await asyncio.to_thread(
                _get_supervisor_leader,
                project_id,
            )
            if raw_plan:
                await asyncio.to_thread(
                    supervisor.load_project_plan,
                    raw_plan,
                )

            engineer = await asyncio.to_thread(_get_engineer, project_id)
            if raw_plan:
                await asyncio.to_thread(
                    engineer.load_project_context,
                    raw_plan,
                )

        await _persist_all_async()
        return _public_pm_payload(result)


@router.post("/projects/{project_id}/pm-team/confirm-plan")
async def confirm_pm_plan(project_id: str, request: PlanConfirmRequest):
    """
    用户确认 PM 方案（可附带修改意见）
    确认后方可启动项目
    """
    ctx = _get_project(project_id)
    leader = await asyncio.to_thread(_get_pm_team, project_id)
    if (
        request.requirements_revision is None
        or request.requirements_digest is None
    ):
        raise HTTPException(
            status_code=422,
            detail="requirements_revision and requirements_digest are required",
        )
    required_files = (
        [item.model_dump() for item in request.required_files]
        if request.required_files is not None else None
    )
    async with _requirements_transaction_lock(project_id):
        if not _requirements_match(
            leader,
            request.requirements_revision,
            request.requirements_digest,
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        "requirements revision drift; regenerate before "
                        "confirmation"
                    ),
                    **_requirements_metadata(leader),
                },
            )
        if not leader.draft_plan:
            return {
                "success": False,
                "status": "needs_plan",
                "message": "Generate and validate a draft plan before confirmation",
                "validation": {"valid": False, "issues": [{
                    "layer": "workflow", "code": "missing_draft",
                    "path": "$.draft_plan",
                    "message": "Call the synthesize endpoint before confirm-plan",
                }]},
                "can_launch": False,
            }

        leader_snapshot = _snapshot_requirement_state(leader)
        previous_phase_manager = _phase_managers.get(project_id)
        previous_phase_manager_state = (
            copy.deepcopy(previous_phase_manager.to_dict())
            if previous_phase_manager is not None
            and hasattr(previous_phase_manager, "to_dict")
            else copy.deepcopy(vars(previous_phase_manager))
            if previous_phase_manager is not None else None
        )
        had_subprojects = hasattr(ctx, "subprojects")
        previous_subprojects = copy.deepcopy(getattr(ctx, "subprojects", []))
        previous_supervisor = _supervisor_leaders.get(project_id)
        previous_supervisor_state = (
            copy.deepcopy(previous_supervisor.to_persist())
            if previous_supervisor is not None else None
        )
        previous_engineer = _engineer_agents.get(project_id)
        previous_engineer_state = (
            copy.deepcopy(previous_engineer.to_persist())
            if previous_engineer is not None else None
        )

        def restore_uncommitted_state() -> None:
            _restore_requirement_state(leader, leader_snapshot)
            if had_subprojects:
                ctx.subprojects[:] = previous_subprojects
            elif hasattr(ctx, "subprojects"):
                delattr(ctx, "subprojects")
            if previous_phase_manager is None:
                _phase_managers.pop(project_id, None)
            else:
                if hasattr(previous_phase_manager, "from_dict"):
                    previous_phase_manager.from_dict(
                        copy.deepcopy(previous_phase_manager_state or {}),
                    )
                else:
                    vars(previous_phase_manager).clear()
                    vars(previous_phase_manager).update(
                        copy.deepcopy(previous_phase_manager_state or {}),
                    )
                _phase_managers[project_id] = previous_phase_manager
            if previous_supervisor is None:
                _supervisor_leaders.pop(project_id, None)
            else:
                previous_supervisor.from_persist(
                    copy.deepcopy(previous_supervisor_state or {}),
                )
                _supervisor_leaders[project_id] = previous_supervisor
            if previous_engineer is None:
                _engineer_agents.pop(project_id, None)
            else:
                previous_engineer.from_persist(
                    copy.deepcopy(previous_engineer_state or {}),
                )
                _engineer_agents[project_id] = previous_engineer

        try:
            result = await asyncio.to_thread(
                leader.confirm_plan,
                request.modifications or "",
                required_files,
                request.requirements_revision,
                request.requirements_digest,
            )
            return await _commit_confirmation_result(
                project_id,
                ctx,
                leader,
                expected_revision=request.requirements_revision,
                expected_digest=request.requirements_digest,
                result=result,
                _lock_already_held=True,
            )
        except _PersistedConfirmationRejection:
            raise
        except UnsupportedExecutionRoleError as exc:
            restore_uncommitted_state()
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "unsupported_execution_role",
                    "message": str(exc),
                    "roles": list(exc.roles),
                },
            ) from exc
        except Exception:
            restore_uncommitted_state()
            raise

@router.get("/projects/{project_id}/pm-team/plan")
async def get_pm_draft_plan(project_id: str):
    """获取当前 PM 方案草稿"""
    _get_project(project_id)
    leader = _get_pm_team(project_id)
    return {
        "draft_plan": _public_pm_payload(leader.draft_plan),
        "final_plan": _public_pm_payload(leader.final_plan),
        "plan_confirmed": leader.plan_confirmed,
        "status": leader.plan_status,
        "generation": _public_pm_payload(leader.plan_generation),
        "draft_blocked_reason": leader.draft_blocked_reason,
        "blocked_draft": _public_pm_payload(leader.blocked_draft),
        "member_analyses": leader.member_analyses,
        **_requirements_metadata(leader),
    }
