"""执行 Agent 路由"""
import asyncio
import copy
import hashlib
import time
import json
import logging
import os
import threading
from contextlib import nullcontext
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Header, Request
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from core.persistence import load_execution_status, load_fix_attempt_counts, save_application_state
from core.agent_lifecycle import transition_agent
from core.hermes_client import current_user_api_config
from core import expert_lock
from core.security_audit import record_audit_event, redact_text, redact_value
from core.project_write_fence import (
    ProjectExecutionGuard,
    ProjectWriteFenceConflict,
    project_write_guard,
)
from core.delivery_documents import record_successful_task_delivery
from core.execution_runs import (
    DurableRunRegistry,
    ExecutionRunError,
    IdempotencyConflict,
    IdempotencyStore,
    InvalidTransition,
    LeaseConflict,
    RunNotFound,
)
from core.app_state import (
    app, projects, hermes_client, global_sm_agent, gitee_sync,
    config_loader, agents_api_config, user_api_configs, DEFAULT_API_CONFIG,
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
from core.handoff import HandoffPayload, make_rubric_from_requirements
from agents.execution_agent import _is_runtime_artifact
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
    ProjectTeamAssignRequest,
)

router = APIRouter(tags=["execution"])

_TRANSACTION_INTERNAL_PARTS = {
    ".git",
    ".project",
    ".pytest_cache",
    ".mypy_cache",
    ".cache",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".tox",
}


def _reject_locked_phase_execution_bypass(
    project_id: str,
    *,
    agent_id: str = "",
) -> None:
    """Locked task DAGs may only use the phase task-attempt coordinator."""
    phase_manager = _phase_managers.get(project_id)
    if not phase_manager or not (
        getattr(phase_manager, "project_contract", {}) or {}
    ).get("locked"):
        return
    ctx = projects.get(project_id)
    phase_id = ""
    if agent_id and ctx is not None:
        phase_id = str(
            (ctx.agents.get(agent_id) or {}).get("phase_id") or ""
        )
    if any(
        not phase_id or str(phase.get("phase_id") or "") == phase_id
        for phase in phase_manager.phases
    ):
        raise HTTPException(
            status_code=410,
            detail=(
                "Locked phase execution is available only through the "
                "phase DAG task-attempt coordinator"
            ),
        )


class RunCancelRequest(BaseModel):
    reason: str = "cancelled by user"


class RunTakeoverRequest(BaseModel):
    force: bool = False
    lease_seconds: float = 300.0


class RunResolveRequest(BaseModel):
    status: str
    reason: str = "resolved by human operator"
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    required_evidence: List[str] = Field(default_factory=lambda: ["artifact_validation"])


_run_registry = DurableRunRegistry()
_execution_idempotency = IdempotencyStore()
_active_run_tasks: Dict[str, asyncio.Task] = {}
_run_cancel_events: Dict[str, threading.Event] = {}
_run_execution_guards: Dict[str, ProjectExecutionGuard] = {}
_RUN_TIMEOUT_SECONDS = max(30.0, float(os.environ.get("AGENT_RUN_TIMEOUT_SECONDS", "900")))
_RUN_LEASE_SECONDS = max(15.0, float(os.environ.get("AGENT_RUN_LEASE_SECONDS", "60")))
_RUN_MAX_RETRIES = max(0, min(5, int(os.environ.get("AGENT_RUN_MAX_RETRIES", "2"))))
_RUN_RETRY_BACKOFF = max(0.1, float(os.environ.get("AGENT_RUN_RETRY_BACKOFF_SECONDS", "2")))


# 全局执行状态缓存：agent_id → {status, progress, output_files, logs, ...}
execution_status: Dict[str, Dict] = {}


def _normalized_output_path(value: Any) -> str:
    normalized = str(value).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _workspace_delivery_file_exists(workspace: Path, value: Any) -> bool:
    """Check a contract path against the workspace without allowing traversal."""
    normalized = _normalized_output_path(value)
    relative = Path(normalized)
    if not normalized or relative.is_absolute() or ".." in relative.parts:
        return False
    root = Path(workspace).resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return target.is_file()


def _missing_workspace_delivery_files(
    workspace: Path,
    required_files: List[str],
    owned_files: List[str],
) -> List[str]:
    """Return contract files absent from disk or not owned by the current agent."""
    # Requirements are extracted from free-form prose and may capitalize a
    # repository directory at the beginning of a sentence (for example
    # ``Backend/package.json``).  Treat contract matching as case-insensitive,
    # then validate the agent's actual declared path on disk.  This keeps the
    # ownership and traversal protections while avoiding a Linux-only false
    # negative for an otherwise valid canonical lowercase repository layout.
    owned_by_key = {
        _normalized_output_path(path).casefold(): _normalized_output_path(path)
        for path in owned_files
    }
    missing: List[str] = []
    for path in required_files:
        # 运行时二进制/数据文件（.db 等）LLM 无法生成，不判缺失。
        if _is_runtime_artifact(path):
            continue
        owned_path = owned_by_key.get(_normalized_output_path(path).casefold())
        if owned_path is None or not _workspace_delivery_file_exists(workspace, owned_path):
            missing.append(path)
    return missing


def _record_output_file_evidence(
    ctx: ProjectContext,
    agent_id: str,
    output_files: List[Any],
) -> Dict[str, Any]:
    """Persist runner-controlled file evidence before lease release/recovery.

    A model's ``output_files`` claim is never sufficient by itself.  Only safe,
    existing workspace files are recorded and every record includes the digest
    of the bytes that will later be verified.
    """
    root = Path(ctx.workspace).resolve()
    files: List[Dict[str, Any]] = []
    for value in dict.fromkeys(output_files or []):
        normalized = _normalized_output_path(value)
        relative = Path(normalized)
        if not normalized or relative.is_absolute() or ".." in relative.parts:
            continue
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if not target.is_file():
            continue
        payload = target.read_bytes()
        files.append({
            "path": normalized,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        })
    evidence = {
        "kind": "delivery_files",
        "run_id": (execution_status.get(agent_id) or {}).get("run_id"),
        "files": files,
        "recorded_at": time.time(),
    }
    if agent_id in ctx.agents:
        ctx.agents[agent_id]["delivery_evidence"] = evidence
    return evidence


def _canonical_transaction_scope(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    if raw in {"*", "/*"}:
        return "*"
    is_prefix = raw.endswith("/")
    if raw.endswith("/*"):
        raw = raw[:-1]
        is_prefix = True
    if "\x00" in raw or any(token in raw for token in ("*", "?", "[", "]")):
        raise ValueError(f"Execution transaction scope has an invalid glob: {value!r}")
    candidate = Path(raw.rstrip("/"))
    if (
        not raw
        or candidate.is_absolute()
        or ".." in candidate.parts
        or (len(raw) >= 2 and raw[1] == ":")
    ):
        raise ValueError(f"Execution transaction scope is unsafe: {value!r}")
    normalized = "/".join(part for part in candidate.parts if part not in {"", "."})
    if not normalized:
        raise ValueError("Execution transaction scope is empty")
    if normalized.split("/", 1)[0].casefold() in _TRANSACTION_INTERNAL_PARTS:
        raise ValueError(
            f"Execution transaction scope targets internal metadata: {value!r}"
        )
    canonical = normalized.casefold()
    return canonical + "/" if is_prefix else canonical


def _execution_transaction_scopes(
    ctx: ProjectContext,
    agent_id: str,
    attempt_scope: Optional[Dict[str, Any]] = None,
) -> List[str]:
    immutable_scope = dict(attempt_scope or {})
    if not immutable_scope:
        # Legacy executions may dynamically expand repair/full-stack targets.
        # Serialize them project-wide so write permission and rollback scope
        # cannot diverge.
        return ["*"]
    has_explicit_scope_contract = any(
        key in immutable_scope
        for key in (
            "allowed_path_prefixes",
            "allowed_prefixes",
            "required_files",
            "rebuild_file_specs",
            "workspace_exclusive",
        )
    )
    raw_scopes = (
        list(
            immutable_scope.get("allowed_path_prefixes")
            or immutable_scope.get("allowed_prefixes")
            or []
        )
        + list(immutable_scope.get("required_files") or [])
        + [
            item.get("path")
            for item in (immutable_scope.get("rebuild_file_specs") or [])
            if isinstance(item, dict) and item.get("path")
        ]
    )
    scopes: List[str] = []
    scope_origins: Dict[str, str] = {}
    for value in raw_scopes:
        canonical = _canonical_transaction_scope(value)
        origin = _normalized_output_path(value).strip()
        previous = scope_origins.get(canonical)
        if previous is not None and previous != origin:
            raise ValueError(
                "Execution transaction scope has a case or alias collision: "
                f"{previous!r}, {origin!r}"
            )
        scope_origins[canonical] = origin
        if canonical not in scopes:
            scopes.append(canonical)
    if immutable_scope and has_explicit_scope_contract and not scopes:
        if immutable_scope.get("workspace_exclusive") is True:
            return ["*"]
        raise ValueError("Execution transaction scope contract is explicitly empty")
    if immutable_scope and not scopes:
        raise ValueError(
            "Execution artifact policy has no immutable transaction scope"
        )
    return scopes


def _transaction_scope_covers(scopes: List[str], relative_path: Any) -> bool:
    path = _normalized_output_path(relative_path).strip("/").casefold()
    if not path:
        return False
    return any(
        scope.casefold() == "*"
        or path == scope.casefold()
        or (
            scope.endswith("/")
            and path.startswith(scope.casefold())
        )
        for scope in scopes
    )


def _transaction_scope_intersects(scopes: List[str], relative_path: Any) -> bool:
    path = _normalized_output_path(relative_path).strip("/").casefold()
    if not path:
        return "*" in scopes
    return _transaction_scope_covers(scopes, path) or any(
        scope != "*" and scope.rstrip("/").startswith(path + "/")
        for scope in scopes
    )


def _collect_execution_transaction_files(
    workspace: Path,
    scopes: List[str],
) -> Dict[str, bytes]:
    root = Path(workspace).resolve(strict=True)
    files: Dict[str, bytes] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            relative = child.relative_to(root).as_posix()
            is_junction = getattr(child, "is_junction", lambda: False)
            if child.is_symlink() or is_junction():
                if _transaction_scope_intersects(scopes, relative):
                    raise ValueError(
                        f"Execution transaction scope crosses a link: {relative}"
                    )
                continue
            if child.is_dir():
                if child.name.casefold() not in _TRANSACTION_INTERNAL_PARTS:
                    pending.append(child)
                continue
            if child.is_file() and _transaction_scope_covers(scopes, relative):
                files[relative] = child.read_bytes()
    return files


def _ensure_agent_run_file_lease(
    ctx: ProjectContext,
    agent_id: str,
    run_id: str,
    attempt_scope: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Bind one fresh file lease to a durable run, including resumed runs."""
    agent = ctx.agents.get(agent_id)
    if not agent:
        raise LeaseConflict("agent no longer exists")

    project_id = str(
        getattr(ctx, "project_id", "") or agent.get("project_id") or ""
    )
    expert_id = str(agent.get("expert_id") or agent_id)
    subproject_id = str(agent.get("subproject_id") or agent_id)
    released_locks: set[str] = set()
    current_lock = str(agent.get("lock_id") or "")
    if current_lock and agent.get("lock_run_id") == run_id:
        renewed = expert_lock.renew_lock(current_lock)
        if renewed.get("success"):
            agent["locked_until"] = renewed.get("leased_until")
            return {**renewed, "reused": True}
        expert_lock.release_lock(current_lock)
        released_locks.add(current_lock)
    elif current_lock:
        # The phase-planning lease or a previous failed run must not be reused.
        expert_lock.release_lock(current_lock)
        released_locks.add(current_lock)

    # A crashed worker can leave a durable run lease behind after the Agent's
    # lock_id field was cleared. Release only this exact expert/subproject run
    # lineage; phase-planning and sibling task leases must remain untouched.
    run_task_prefix = f"{subproject_id}:run:"
    for lock in expert_lock.get_active_locks(
        expert_id=expert_id,
        project_id=project_id,
    ):
        lock_id = str(lock.get("lock_id") or "")
        task_id = str(lock.get("task_id") or "")
        if (
            lock_id
            and lock_id not in released_locks
            and str(lock.get("expert_id") or "") == expert_id
            and str(lock.get("project_id") or "") == project_id
            and task_id.startswith(run_task_prefix)
            and len(task_id) > len(run_task_prefix)
        ):
            expert_lock.release_lock(lock_id)
            released_locks.add(lock_id)
    agent["lock_id"] = None
    agent["lock_run_id"] = None
    agent["locked_until"] = None

    scopes = _execution_transaction_scopes(ctx, agent_id, attempt_scope)
    task_id = f"{subproject_id}:run:{run_id}"
    claimed = expert_lock.atomic_claim_lock(
        expert_id,
        project_id,
        task_id,
        scopes,
    )
    if not claimed.get("success"):
        raise LeaseConflict(str(claimed.get("error") or "unable to acquire run file lease"))
    agent["lock_id"] = claimed["lock_id"]
    agent["lock_run_id"] = run_id
    agent["locked_until"] = claimed.get("leased_until")
    return claimed

def _make_exec_agent(
    ctx: ProjectContext,
    agent_id: str,
    user_api_config: Optional[Dict[str, Any]] = None,
    execution_guard=None,
    attempt_scope: Optional[Dict[str, Any]] = None,
) -> ExecutionAgent:
    """根据 agent_info 和 agents_api_config 构建 ExecutionAgent 实例"""
    agent_info = ctx.agents.get(agent_id, {})
    immutable_scope = dict(attempt_scope or {})
    scope_required_files = list(
        immutable_scope.get("required_files") or []
    )
    scope_required_files.extend(
        item.get("path")
        for item in (immutable_scope.get("rebuild_file_specs") or [])
        if isinstance(item, dict) and item.get("path")
    )
    if immutable_scope:
        if "allowed_path_prefixes" in immutable_scope:
            configured_prefixes = list(
                immutable_scope.get("allowed_path_prefixes") or []
            )
        else:
            configured_prefixes = list(
                immutable_scope.get("allowed_prefixes") or []
            )
    else:
        configured_prefixes = list(
            agent_info.get("allowed_path_prefixes") or []
        )
    allowed_path_prefixes = list(dict.fromkeys(configured_prefixes + (
        scope_required_files
        if immutable_scope
        else (
            list(agent_info.get("required_rebuild_files") or [])
            + list(agent_info.get("required_delivery_files") or [])
        )
    )))
    role_label = str(agent_info.get("role") or "").lower().replace("-", " ")
    is_fullstack = any(token in role_label for token in ("full stack", "fullstack", "全栈"))
    if is_fullstack and not immutable_scope:
        # Compatibility for projects created before canonical role scopes were
        # enforced.  Earlier single-role frontend phases could write a React
        # app at repository-root ``src/``.  A later integration agent otherwise
        # owns only ``frontend/`` and can never repair the real application.
        legacy_candidates = (
            "src/", "server/", "index.html", "tsconfig.json",
            "vite.config.ts", "vite.config.js",
        )
        for candidate in legacy_candidates:
            target = ctx.workspace / candidate.rstrip("/")
            if target.exists() and candidate not in allowed_path_prefixes:
                allowed_path_prefixes.append(candidate)
    api_cfg = copy.deepcopy(
        user_api_config
        if user_api_config is not None
        else (agents_api_config.get(agent_id) or DEFAULT_API_CONFIG)
    )
    expert_pool = None
    if agent_info.get("expert_id"):
        try:
            from core.expert_pool import get_expert_pool
            expert_pool = get_expert_pool(str(getattr(ctx, "owner_user_id", "") or ""))
        except Exception as exc:
            logger.warning("加载专家池失败，跳过专家记忆注入 agent=%s: %s", agent_id, exc)
    execution_agent = ExecutionAgent(
        agent_id=agent_id,
        role=agent_info.get("role", "开发工程师"),
        workspace=ctx.workspace,
        hermes_client=hermes_client,
        skill_names=agent_info.get("skill_names", []),
        api_config=api_cfg,
        expert_id=agent_info.get("expert_id"),
        project_id=agent_info.get("project_id") or getattr(ctx, "project_id", None),
        phase_id=agent_info.get("phase_id"),
        expert_pool=expert_pool,
        allowed_path_prefixes=allowed_path_prefixes,
        required_output_files=list(dict.fromkeys(
            list(immutable_scope.get("required_files") or [])
            if immutable_scope
            else (
                list(agent_info.get("required_rebuild_files") or [])
                + list(agent_info.get("required_delivery_files") or [])
            )
        )),
        rebuild_file_specs=list(
            (immutable_scope.get("rebuild_file_specs") or [])
            if "rebuild_file_specs" in immutable_scope
            else (agent_info.get("rebuild_file_specs") or [])
        ),
        artifact_policy=immutable_scope or agent_info.get("artifact_policy") or {
            "kind": (
                "architecture_document"
                if agent_info.get("expert_type") == "architecture"
                else "runnable"
            ),
            "required_files": list(agent_info.get("required_delivery_files") or []),
            "allowed_prefixes": list(agent_info.get("allowed_path_prefixes") or []),
        },
        execution_guard=execution_guard,
        immutable_path_scope=bool(immutable_scope),
    )
    execution_agent._hermes.usage_user_id = str(getattr(ctx, "owner_user_id", "") or "")
    execution_agent._hermes.usage_project_id = str(getattr(ctx, "project_id", "") or "")
    return execution_agent

def _run_with_user_api_config(user_api_config, callback):
    """Run blocking model work with an isolated request-level API config."""
    if user_api_config is None:
        return callback()
    token = current_user_api_config.set(copy.deepcopy(user_api_config))
    try:
        return callback()
    finally:
        current_user_api_config.reset(token)


def _project_owner_api_config(ctx: ProjectContext) -> Dict[str, Any]:
    owner_user_id = str(getattr(ctx, "owner_user_id", "") or "")
    return copy.deepcopy(
        user_api_configs.get(owner_user_id) or DEFAULT_API_CONFIG
    )


# 修复次数上限：防止质检→修复无限循环
_MAX_FIX_ATTEMPTS = 5
# 记录每个 agent 的修复次数：agent_id → count
_fix_attempt_counts: Dict[str, int] = {}
# Restore execution state from disk
_loaded = load_execution_status()
execution_status.update(_loaded)
_loaded_fix = load_fix_attempt_counts()
_fix_attempt_counts.update(_loaded_fix)


def _persist_execution_state() -> None:
    save_application_state({
        "execution_status": redact_value(execution_status),
        "fix_attempt_counts": _fix_attempt_counts,
    })


def _refresh_project_rollup(ctx: ProjectContext) -> None:
    """Derive project and phase status from executable agent/subproject state."""
    phase_manager = _phase_managers.get(ctx.project_id)
    if phase_manager and phase_manager.phases:
        executable = [sp for sp in ctx.subprojects if sp.get("agent_id")]
        for phase in phase_manager.phases:
            phase_id = phase.get("phase_id")
            child_ids = set(phase.get("subprojects") or [])
            children = [
                sp for sp in executable
                if sp.get("phase_id") == phase_id or sp.get("id") in child_ids
            ]
            if not children and len(phase_manager.phases) == 1:
                children = executable
            if not children:
                continue

            # User acceptance is a durable delivery milestone. Whole-project
            # QA may attempt a later repair, but an ordinary child rollup must
            # never silently rewrite an accepted phase to ``failed``. Reopening
            # an accepted phase is a separate, explicit workflow operation.
            if phase.get("user_confirmed"):
                phase["status"] = "completed"
                phase["progress"] = 100
                phase["completed_at"] = phase.get("completed_at") or time.time()
                continue

            statuses = [sp.get("status", "pending") for sp in children]
            progresses = [int(sp.get("progress") or 0) for sp in children]
            if any(status in {"failed", "fix_limit_reached"} for status in statuses):
                phase["status"] = "failed"
            elif all(status == "completed" for status in statuses):
                # Execution completion is not acceptance. A phase may only
                # complete after QC passes and the user explicitly confirms.
                if phase.get("user_confirmed"):
                    phase["status"] = "completed"
                    phase["completed_at"] = phase.get("completed_at") or time.time()
                elif phase.get("review_passed"):
                    phase["status"] = "reviewing"
                else:
                    phase["status"] = "qa_pending"
            elif any(status in {"working", "in_progress", "running", "re_checking", "fixing"} for status in statuses):
                phase["status"] = "in_progress"
            else:
                # A started phase can temporarily have only queued/idle agents.
                # Do not regress it to pending: the UI would hide its plan and QC controls.
                phase["status"] = "in_progress" if phase.get("started_at") else "pending"
            phase["progress"] = int(sum(progresses) / len(progresses)) if progresses else 0

    rollup_rows = list(ctx.subprojects)
    if phase_manager and phase_manager.phases:
        rollup_rows = list(phase_manager.phases) + [sp for sp in ctx.subprojects if sp.get("agent_id")]
    ctx.status = ctx._derive_project_status(rollup_rows)


async def _start_phase_quality_cycle_if_ready(ctx: ProjectContext) -> None:
    """Start initial QC/repair after every executable task in a phase finishes."""
    phase_manager = _phase_managers.get(ctx.project_id)
    if not phase_manager:
        return

    from api import routes_phases

    executable = [sp for sp in ctx.subprojects if sp.get("agent_id")]
    for phase in phase_manager.phases:
        if phase.get("user_confirmed") or phase.get("review_passed"):
            continue
        phase_id = phase.get("phase_id")
        key = f"{ctx.project_id}-{phase_id}"
        state = routes_phases._auto_repair_states.get(key)
        if state and routes_phases._detach_stale_phase_quality_state(
            ctx,
            str(phase_id),
            phase,
        ):
            await routes_phases._persist_all_async()
            state = None
        child_ids = set(phase.get("subprojects") or [])
        children = [
            sp for sp in executable
            if sp.get("phase_id") == phase_id or sp.get("id") in child_ids
        ]
        if not children and len(phase_manager.phases) == 1:
            children = executable
        dispatch_plan = phase.get("execution_dispatch_plan") or {}
        locked_task_ids = [
            str(task_id) for task_id in (dispatch_plan.get("task_ids") or [])
            if str(task_id)
        ]
        if locked_task_ids:
            generation = str(phase.get("execution_generation") or "")
            contract_digest = str(phase.get("execution_contract_digest") or "")
            artifact_baseline_digest = str(
                phase.get("execution_artifact_baseline_digest") or ""
            )
            requirements_revision = int(
                phase.get("execution_requirements_revision") or 0
            )
            coordinator = phase.get("execution_coordinator") or {}
            repair_task_ids = {
                str(task_id)
                for task_id in (coordinator.get("repair_task_ids") or [])
                if str(task_id)
            }
            dispatch_attempt_digest = str(
                coordinator.get("dispatch_attempt_digest") or ""
            )
            receipts_by_task: Dict[str, List[Dict[str, Any]]] = {}
            phase_agents = [
                agent for agent in ctx.agents.values()
                if str(agent.get("phase_id") or "") == str(phase_id)
                and not agent.get("verification_only")
            ]
            for agent in phase_agents:
                for task_id, receipt in (
                    agent.get("task_execution_receipts") or {}
                ).items():
                    if str(task_id) in locked_task_ids and isinstance(receipt, dict):
                        # A retry can finish after persistence replaced the
                        # coordinator's in-memory receipt object. Recover the
                        # completion edge from the durable run instead of
                        # leaving the phase permanently projected at 99%.
                        start_run_id = str(receipt.get("start_run_id") or "")
                        if (
                            start_run_id
                            and (
                                str(receipt.get("status") or "").lower()
                                != "succeeded"
                                or not receipt.get("completion_run_id")
                            )
                        ):
                            try:
                                durable_run = await asyncio.to_thread(
                                    _run_registry.get, start_run_id,
                                )
                            except Exception:
                                durable_run = {}
                            payload = durable_run.get("payload") or {}
                            if (
                                durable_run.get("status") == "succeeded"
                                and str(payload.get("project_id") or "")
                                == str(ctx.project_id)
                                and str(payload.get("phase_id") or "")
                                == str(phase_id)
                                and str(payload.get("task_id") or "")
                                == str(task_id)
                                and str(payload.get("agent_id") or "")
                                == str(agent.get("id") or "")
                                and str(payload.get("execution_generation") or "")
                                == generation
                                and str(payload.get("contract_digest") or "")
                                == contract_digest
                                and int(payload.get("requirements_revision") or 0)
                                == requirements_revision
                                and str(
                                    payload.get("artifact_baseline_digest") or ""
                                ) == artifact_baseline_digest
                            ):
                                receipt.update({
                                    "completion_run_id": start_run_id,
                                    "started_at": durable_run.get("started_at"),
                                    "finished_at": durable_run.get("finished_at"),
                                    "status": "succeeded",
                                    "result": copy.deepcopy(
                                        durable_run.get("result") or {}
                                    ),
                                })
                        receipts_by_task.setdefault(str(task_id), []).append(receipt)
            locked_ready = (
                bool(generation)
                and bool(routes_phases._current_phase_coordinator_run(
                    ctx.project_id, str(phase_id), phase,
                ))
            )
            if locked_ready:
                locked_ready = all(
                    len(receipts_by_task.get(task_id) or []) == 1
                    and str(receipts_by_task[task_id][0].get("status") or "").lower()
                    == "succeeded"
                    and bool(receipts_by_task[task_id][0].get("completion_run_id"))
                    and str(
                        receipts_by_task[task_id][0].get("execution_generation") or ""
                    ) == generation
                    and str(receipts_by_task[task_id][0].get("contract_digest") or "")
                    == contract_digest
                    and str(
                        receipts_by_task[task_id][0].get(
                            "artifact_baseline_digest"
                        ) or ""
                    ) == artifact_baseline_digest
                    and int(
                        receipts_by_task[task_id][0].get("requirements_revision") or 0
                    ) == requirements_revision
                    and (
                        task_id not in repair_task_ids
                        or (
                            bool(dispatch_attempt_digest)
                            and str(
                                receipts_by_task[task_id][0].get(
                                    "dispatch_attempt_digest"
                                ) or ""
                            ) == dispatch_attempt_digest
                            and str(
                                receipts_by_task[task_id][0].get(
                                    "repair_coordinator_run_id"
                                ) or ""
                            ) == str(
                                coordinator.get("durable_run_id") or ""
                            )
                        )
                    )
                    for task_id in locked_task_ids
                )
            if not locked_ready:
                # Once a persisted quality/repair state exists it owns the
                # projected Agent and subproject lifecycle.  A temporarily
                # unprojectable execution receipt must not rewind completed
                # participants after a restart: doing so makes the Supervisor
                # attempt the illegal transition ``succeeded -> running``.
                if state:
                    logger.info(
                        "Locked phase execution projection deferred without "
                        "rewriting persisted quality state project=%s phase=%s "
                        "quality_status=%s",
                        ctx.project_id,
                        phase_id,
                        state.get("status"),
                    )
                    continue
                # Keep each successful task terminal while later DAG tasks run.
                # Phase-level readiness is still guarded by locked_ready, so
                # preserving the task result cannot start QC early.
                continue
            projectable_statuses = {"completed", "succeeded"}
            if not (
                state
                and str(state.get("status") or "") == "pre_qa_failed"
            ):
                projectable_statuses.update({
                    "working", "fixing", "re_checking",
                })
            nonprojectable = [
                f"{agent.get('id') or 'unknown'}:"
                f"{str(agent.get('status') or 'idle').lower()}"
                for agent in phase_agents
                if str(agent.get("status") or "idle").lower()
                not in projectable_statuses
            ]
            if nonprojectable:
                # Durable task receipts prove execution completion, but they do
                # not erase a later QA/repair lifecycle state. In particular,
                # fix_required must survive restart until a repair coordinator
                # produces a new attempt-bound receipt.
                logger.info(
                    "Locked phase quality projection deferred project=%s "
                    "phase=%s agent_states=%s",
                    ctx.project_id,
                    phase_id,
                    ",".join(nonprojectable[:12]),
                )
                continue
            for agent in phase_agents:
                transition_agent(
                    agent, "completed", progress=100,
                    message="All locked task attempts completed",
                )
            for child in children:
                child["status"] = "completed"
                child["progress"] = 100
        rebuild_failures = routes_phases._phase_rebuild_terminal_failures(ctx, phase_id)
        if rebuild_failures and routes_phases._rebuild_lifecycle_pending(state):
            await routes_phases._fail_closed_rebuild_execution(
                ctx, phase_id, rebuild_failures,
            )
            continue
        if not children or not all(sp.get("status") == "completed" for sp in children):
            continue

        if state and state.get("status") == "pre_qa_failed":
            await routes_phases._resume_completed_pre_qa_repair_runs(
                ctx,
                str(phase_id),
                state,
            )
            continue

        # 放行 idle/starting/stale：首次 start_auto_repair 可能因 artifact_digest
        # 等前置条件未就绪而失败（state 创建为 idle 但 machine 未起）。若不放行，
        # 后续每次循环都 continue，start_auto_repair 永不重试 → phase 卡 reviewing。
        _retryable = {"error", "needs_manual", "rebuild_started", "idle", "starting", "stale"}
        if state and state.get("status") not in _retryable:
            continue

        phase["status"] = "reviewing"
        try:
            await routes_phases.start_auto_repair(ctx.project_id, phase_id)
        except Exception:
            # start_auto_repair 前置条件未就绪（如 artifact 未绑定）不应让执行循环
            # 静默中断；记录后下一轮重试。真实失败由 auto_repair state 自身状态机标记。
            import logging as _lg
            _lg.getLogger("routes_execution").exception(
                "start_auto_repair raised for phase=%s; will retry next cycle", phase_id
            )


def _reset_fix_attempt(agent_id: str) -> None:
    """公开 API：重置指定 Agent 的修复次数计数器"""
    _fix_attempt_counts[agent_id] = 0


def _safe_create_task(coro, name: str = "") -> asyncio.Task:
    """安全创建后台任务：注册错误回调，防止静默失败"""
    task = asyncio.create_task(coro)
    def _on_task_done(t: asyncio.Task):
        try:
            t.result()
        except asyncio.CancelledError:
            pass
        except Exception as ex:
            logger.error("后台任务异常 [%s]: %s", name or "unnamed", ex, exc_info=True)
    task.add_done_callback(_on_task_done)
    return task


def _sanitize_error(exc: Exception) -> str:
    """清理异常消息，移除内部路径防止信息泄漏"""
    msg = redact_text(exc)
    # 移除常见的内部路径模式
    import re as _re
    msg = _re.sub(r"['\"]?(?:/[\w./-]+)+['\"]?", "…", msg)
    if len(msg) > 200:
        msg = msg[:200] + "…"
    return msg or "任务执行失败"


def _is_retryable_initial_delivery_failure(result: Dict[str, Any]) -> bool:
    """Allow one orchestration-level retry for malformed model deliveries."""
    if result.get("success") is True:
        return False
    # ExecutionAgent has already bounded its local format retries and returns a
    # typed, non-business failure.  The orchestration layer is intentionally a
    # separate single recovery attempt with a fresh agent/context.
    if (
        result.get("failure_category") == "model_failed"
        and result.get("retryable") is True
        and (result.get("model_failure_evidence") or {}).get("kind")
        == "model_output_format"
    ):
        return True
    message = str(result.get("error") or "")
    return bool(message) and ExecutionAgent._is_retryable_delivery_error(ValueError(message))


_agent_task_locks: Dict[tuple[str, str], asyncio.Lock] = {}
_ACTIVE_EXECUTION_STATUSES = {
    "queued", "working", "in_progress", "running", "re_checking", "fixing",
}
_AGENT_HEARTBEAT_INTERVAL_SECONDS = max(
    15.0,
    min(300.0, float(expert_lock.LOCK_TTL_SECONDS) / 3),
)


async def _maintain_agent_lease(
    ctx: ProjectContext,
    agent_id: str,
    stop_event: asyncio.Event,
    lease_state: Dict[str, str],
    expected_lock_id: str = "",
) -> None:
    """Keep lifecycle heartbeat and the file lease alive while execution runs."""
    while not stop_event.is_set():
        agent = ctx.agents.get(agent_id)
        if not agent:
            return

        now = time.time()
        agent["heartbeat_at"] = now
        agent["updated_at"] = now
        execution_status.setdefault(agent_id, {})["heartbeat_at"] = now

        current_lock_id = str(agent.get("lock_id") or "")
        if expected_lock_id and current_lock_id != expected_lock_id:
            lease_state["error"] = (
                "Expert lease generation changed during execution"
            )
            return
        lock_id = expected_lock_id or current_lock_id
        if lock_id:
            try:
                loop = asyncio.get_running_loop()
                renewed = await loop.run_in_executor(
                    None,
                    expert_lock.renew_lock,
                    lock_id,
                )
            except Exception as exc:
                lease_state["error"] = f"Expert lease renewal failed: {_sanitize_error(exc)}"
                return
            if not renewed.get("success"):
                lease_state["error"] = "Expert lease was lost during execution"
                return
            agent["locked_until"] = renewed.get("leased_until")

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=_AGENT_HEARTBEAT_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            continue


def _mark_agent_task_failed(
    ctx: ProjectContext,
    agent_id: str,
    subproject_id: str,
    error: str,
) -> None:
    """Synchronize failure across execution, Agent and subproject state."""
    current = execution_status.setdefault(agent_id, {})
    current.update({
        "status": "failed",
        "progress": 0,
        "error": error,
    })
    agent = ctx.agents.get(agent_id)
    if agent is not None:
        try:
            transition_agent(agent, "failed", progress=0, message=error)
        except ValueError:
            transition_agent(agent, "working", progress=0, message="Failure reconciliation")
            transition_agent(agent, "failed", progress=0, message=error)
    for subproject in ctx.subprojects:
        if subproject.get("id") == subproject_id:
            subproject["status"] = "failed"
            subproject["progress"] = 0
            subproject["error"] = error
            break


def _capture_execution_transaction(
    ctx: ProjectContext,
    agent_id: str,
    subproject_id: str,
    attempt_scope: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    scopes = _execution_transaction_scopes(ctx, agent_id, attempt_scope)
    files = _collect_execution_transaction_files(Path(ctx.workspace), scopes)
    phase_manager = _phase_managers.get(ctx.project_id)
    registry = getattr(phase_manager, "file_registry", None)
    return {
        "scopes": scopes,
        "lock_id": str((ctx.agents.get(agent_id) or {}).get("lock_id") or ""),
        "files": files,
        "agent": copy.deepcopy(ctx.agents.get(agent_id)),
        "subproject": copy.deepcopy(next(
            (item for item in ctx.subprojects if item.get("id") == subproject_id),
            None,
        )),
        "file_registry": (
            {
                path: copy.deepcopy(entry)
                for path, entry in registry.items()
                if _transaction_scope_covers(scopes, path)
            }
            if isinstance(registry, dict) else None
        ),
        "fix_attempt_present": agent_id in _fix_attempt_counts,
        "fix_attempt_count": _fix_attempt_counts.get(agent_id),
        "execution_status_present": agent_id in execution_status,
        "execution_status": copy.deepcopy(execution_status.get(agent_id)),
    }


def _safe_execution_transaction_target(root: Path, relative: str) -> Path:
    candidate = Path(_normalized_output_path(relative))
    if (
        not str(candidate)
        or candidate.is_absolute()
        or ".." in candidate.parts
    ):
        raise ValueError(f"Execution transaction path is unsafe: {relative!r}")
    cursor = root
    for part in candidate.parts:
        cursor = cursor / part
        is_junction = getattr(cursor, "is_junction", lambda: False)
        if cursor.is_symlink() or (cursor.exists() and is_junction()):
            raise ValueError(
                f"Execution transaction path crosses a link: {relative}"
            )
    resolved_parent = cursor.parent.resolve()
    resolved_parent.relative_to(root)
    return cursor


def _restore_execution_transaction(
    ctx: ProjectContext,
    agent_id: str,
    subproject_id: str,
    snapshot: Dict[str, Any],
    execution_guard=None,
) -> None:
    root = Path(ctx.workspace).resolve()
    scopes = list(snapshot.get("scopes") or [])
    if not scopes:
        raise ValueError("Execution transaction snapshot has no immutable scope")
    expected_lock_id = str(snapshot.get("lock_id") or "")
    if execution_guard is not None and (
        not expected_lock_id
        or str(getattr(execution_guard, "lock_id", "") or "") != expected_lock_id
    ):
        raise ValueError(
            "Execution transaction snapshot is not bound to the active lock"
        )

    if execution_guard is not None and hasattr(
        execution_guard, "compensation_guard"
    ):
        restore_guard = execution_guard.compensation_guard()
    elif execution_guard is not None and hasattr(
        execution_guard, "write_guard"
    ):
        restore_guard = execution_guard.write_guard()
    else:
        restore_guard = nullcontext()
    with restore_guard:
        # Compatibility for older injected guards while production
        # ProjectExecutionGuard keeps rollback validation and mutations atomic.
        if execution_guard is not None and not hasattr(
            execution_guard, "write_guard"
        ):
            execution_guard()
        before_files = dict(snapshot.get("files") or {})
        current_scoped_files = _collect_execution_transaction_files(root, scopes)
        for relative in sorted(
            set(current_scoped_files) - set(before_files), reverse=True
        ):
            target = _safe_execution_transaction_target(root, relative)
            target.unlink(missing_ok=True)
        for relative, content in before_files.items():
            target = _safe_execution_transaction_target(root, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{os.getpid()}.restore.tmp")
            try:
                temporary.write_bytes(content)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)

        prior_agent = snapshot.get("agent")
        if prior_agent is None:
            ctx.agents.pop(agent_id, None)
        else:
            ctx.agents[agent_id] = copy.deepcopy(prior_agent)
        prior_subproject = snapshot.get("subproject")
        for index, item in enumerate(ctx.subprojects):
            if item.get("id") == subproject_id:
                if prior_subproject is None:
                    ctx.subprojects.pop(index)
                else:
                    ctx.subprojects[index] = copy.deepcopy(prior_subproject)
                break
        phase_manager = _phase_managers.get(ctx.project_id)
        if phase_manager is not None and snapshot.get("file_registry") is not None:
            current_registry = getattr(phase_manager, "file_registry", {})
            if not isinstance(current_registry, dict):
                current_registry = {}
            for path in list(current_registry):
                if _transaction_scope_covers(scopes, path):
                    current_registry.pop(path, None)
            current_registry.update(copy.deepcopy(snapshot["file_registry"]))
            phase_manager.file_registry = current_registry
        if snapshot.get("fix_attempt_present"):
            _fix_attempt_counts[agent_id] = snapshot.get("fix_attempt_count")
        else:
            _fix_attempt_counts.pop(agent_id, None)
        if snapshot.get("execution_status_present"):
            execution_status[agent_id] = copy.deepcopy(snapshot.get("execution_status"))
        else:
            execution_status.pop(agent_id, None)


async def _run_agent_task_unlocked(
    project_id: str,
    agent_id: str,
    subproject_id: str,
    subproject_name: str,
    description: str,
    tech_stack: List[str],
    project_context: str,
    user_api_config: Optional[Dict[str, Any]] = None,
    defer_fix_qc: bool = False,
    artifact_policy: Optional[Dict[str, Any]] = None,
    execution_guard=None,
    phase_attempt_payload: Optional[Dict[str, Any]] = None,
):
    """后台异步执行单个 Agent 任务"""
    # 记录Agent执行启动
    logger.info(
        "[Agent开始执行] project_id=%s agent_id=%s subproject=%s name=%s",
        project_id, agent_id, subproject_id, subproject_name
    )
    
    ctx = projects.get(project_id)
    if not ctx:
        logger.warning("[Agent执行中止] project_id=%s 不存在", project_id)
        return {"success": False, "status": "failed", "error": "Project context not found"}
    user_api_config = copy.deepcopy(
        user_api_config
        if user_api_config is not None
        else current_user_api_config.get()
    )
    attempt_scope = dict(artifact_policy or {})
    transaction_snapshot = _capture_execution_transaction(
        ctx, agent_id, subproject_id, attempt_scope,
    )
    transaction_committed = False

    # The durable run payload owns this attempt's immutable file scope. Never
    # copy it onto the shared Agent record: sibling task attempts may use the
    # same Agent with distinct files and evidence contracts.
    is_fix_task = "质检修复任务" in description or "【质检修复任务】" in description
    previous_output_files = list((ctx.agents.get(agent_id) or {}).get("output_files") or [])

    # 修复次数上限检查
    if is_fix_task:
        fix_count = _fix_attempt_counts.get(agent_id, 0) + 1
        _fix_attempt_counts[agent_id] = fix_count
        if fix_count > _MAX_FIX_ATTEMPTS:
            # 超过上限，标记为需要人工介入，不再自动修复
            if agent_id in ctx.agents:
                transition_agent(ctx.agents[agent_id], "fix_limit_reached", progress=0, message="Fix attempt limit reached")
                ctx.agents[agent_id]["fix_limit_message"] = (
                    f"已自动修复 {_MAX_FIX_ATTEMPTS} 次仍未通过质检，"
                    f"请人工检查代码或调整任务描述后手动重新执行。"
                )
            execution_status[agent_id] = {
                "status": "fix_limit_reached",
                "progress": 0,
                "logs": [f"[{time.strftime('%H:%M:%S')}] 已达到最大修复次数 {_MAX_FIX_ATTEMPTS}，需要人工介入"],
                "output_files": [],
                "error": f"已自动修复 {_MAX_FIX_ATTEMPTS} 次仍未通过，请人工检查",
            }
            return {"success": False, **execution_status[agent_id]}
    else:
        # 新任务重置修复计数
        _fix_attempt_counts[agent_id] = 0

    # 更新状态为 working
    execution_status[agent_id] = {
        "status": "working",
        "progress": 0,
        "logs": [],
        "output_files": [],
        "fix_attempt": _fix_attempt_counts.get(agent_id, 0) if is_fix_task else 0,
    }
    if agent_id in ctx.agents:
        transition_agent(ctx.agents[agent_id], "working", progress=10, message="Execution started")
        if is_fix_task:
            ctx.agents[agent_id]["fix_attempt"] = _fix_attempt_counts.get(agent_id, 0)
    for sp in ctx.subprojects:
        if sp["id"] == subproject_id:
            sp["status"] = "in_progress"
            sp["progress"] = 10
            sp.pop("error", None)
            break
    # A durable retry must immediately clear stale failed projections. Waiting
    # until the worker finishes leaves the project/phase falsely terminal and
    # makes acceptance resume logic stop polling an otherwise healthy run.
    _refresh_project_rollup(ctx)

    heartbeat_stop = asyncio.Event()
    lease_state: Dict[str, str] = {}
    heartbeat_task = asyncio.create_task(
        _maintain_agent_lease(
            ctx,
            agent_id,
            heartbeat_stop,
            lease_state,
            str(transaction_snapshot.get("lock_id") or ""),
        ),
        name=f"agent-heartbeat-{agent_id}",
    )
    result: Dict[str, Any] = {}

    try:
        exec_agent = _make_exec_agent(
            ctx,
            agent_id,
            user_api_config=user_api_config,
            execution_guard=execution_guard,
            attempt_scope=attempt_scope,
        )
        # 在线程池中运行（避免阻塞事件循环）
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: _run_with_user_api_config(
                user_api_config,
                lambda: exec_agent.execute_task(
                    subproject_id=subproject_id,
                    subproject_name=subproject_name,
                    description=description,
                    tech_stack=tech_stack,
                    project_context=project_context,
                ),
            )
        )

        # Capture immutable file evidence before checking/releasing the lease.
        # A completion-edge lease loss therefore becomes a verification state,
        # not an excuse to discard or blindly rerun already-written output.
        delivery_evidence = _record_output_file_evidence(
            ctx, agent_id, list(result.get("output_files") or []),
        )
        result["delivery_evidence"] = delivery_evidence

        # Record a completion-edge heartbeat before evaluating the result.
        # This makes short executions observable and verifies that the lease
        # was still owned when the worker returned, even on coarse Windows
        # event-loop timers where the periodic task may only run once.
        agent_for_lease = ctx.agents.get(agent_id)
        completion_lock_id = str(transaction_snapshot.get("lock_id") or "")
        if (
            completion_lock_id
            and str((agent_for_lease or {}).get("lock_id") or "")
            != completion_lock_id
        ):
            lease_state["error"] = (
                "Expert lease generation changed at execution completion"
            )
        if completion_lock_id and not lease_state.get("error"):
            try:
                renewed = await loop.run_in_executor(
                    None, expert_lock.renew_lock, completion_lock_id,
                )
                if not renewed.get("success"):
                    lease_state["error"] = "Expert lease was lost at execution completion"
            except Exception as exc:
                lease_state["error"] = f"Expert lease completion check failed: {_sanitize_error(exc)}"

        # The model occasionally ignores the JSON-only response contract even
        # after the agent's short parser retries.  Recreate the execution agent
        # once with a focused recovery instruction instead of hard-failing the
        # entire project at the first phase.
        if _is_retryable_initial_delivery_failure(result):
            first_logs = list(result.get("logs") or [])
            if is_fix_task:
                recovery_description = (
                    description
                    + "\n\nREPAIR DELIVERY RECOVERY: the previous repair response was "
                    "rejected as malformed, incomplete, or truncated and no file was "
                    "overwritten. Return the complete content of the one assigned repair "
                    "file now as strict JSON only: "
                    '{"files":[{"path":"exact/assigned/path","content":"complete content"}]}. '
                    "Do not shorten unchanged sections, omit code, return commentary, or "
                    "use Markdown fences."
                )
            else:
                recovery_description = (
                    description
                    + "\n\nDELIVERY RECOVERY: the previous response could not be parsed. "
                    "Return the complete assigned implementation now as strict JSON only: "
                    '{"files":[{"path":"relative/path","content":"complete content"}]}. '
                    "Every mandatory delivery-contract path must appear exactly once. "
                    "Do not return commentary or Markdown fences."
                )
            recovery_agent = _make_exec_agent(
                ctx,
                agent_id,
                user_api_config=user_api_config,
                execution_guard=execution_guard,
                attempt_scope=attempt_scope,
            )
            recovery_result = await loop.run_in_executor(
                None,
                lambda: _run_with_user_api_config(
                    user_api_config,
                    lambda: recovery_agent.execute_task(
                        subproject_id=subproject_id,
                        subproject_name=subproject_name,
                        description=recovery_description,
                        tech_stack=tech_stack,
                        project_context=project_context,
                    ),
                ),
            )
            recovery_result["logs"] = (
                first_logs
                + ["Automatic orchestration retry after malformed delivery"]
                + list(recovery_result.get("logs") or [])
            )
            result = recovery_result
            delivery_evidence = _record_output_file_evidence(
                ctx, agent_id, list(result.get("output_files") or []),
            )
            result["delivery_evidence"] = delivery_evidence

        if lease_state.get("error"):
            result["success"] = False
            result["status"] = "pending_verification"
            result["error"] = lease_state["error"]
            result["recovery_reason"] = "lease_lost_after_delivery"
            result.setdefault("logs", []).append(
                lease_state["error"] + "; delivery evidence recorded for deterministic recovery"
            )

        if is_fix_task:
            preserved = [
                path for path in previous_output_files
                if _workspace_delivery_file_exists(ctx.workspace, path)
            ]
            result["output_files"] = list(dict.fromkeys(
                preserved + list(result.get("output_files") or [])
            ))

        # 更新执行状态
        if attempt_scope:
            required_output_files = list(dict.fromkeys(
                attempt_scope.get("required_files") or []
            ))
        else:
            required_rebuild_files = list(
                (ctx.agents.get(agent_id) or {}).get("required_rebuild_files") or []
            )
            required_delivery_files = list(
                (ctx.agents.get(agent_id) or {}).get("required_delivery_files") or []
            )
            required_output_files = list(dict.fromkeys(
                required_rebuild_files + required_delivery_files
            ))
        if required_output_files:
            # Repair rounds intentionally return only the files touched in that
            # round. The delivery contract describes the durable workspace, so
            # validate it against disk instead of misclassifying earlier,
            # already accepted files as missing.
            missing = _missing_workspace_delivery_files(
                ctx.workspace,
                required_output_files,
                list(result.get("output_files") or []),
            )
            if missing:
                result["success"] = False
                result["status"] = "failed"
                result["missing_required_files"] = missing
                result["error"] = f"Missing {len(missing)} required delivery files"
                result.setdefault("logs", []).append("Missing required files: " + ", ".join(missing))

        if phase_attempt_payload:
            await asyncio.to_thread(
                _assert_current_phase_attempt, phase_attempt_payload,
            )

        execution_status[agent_id] = {
            "status": result.get("status", "completed" if result.get("success") else "failed"),
            "progress": 100 if result.get("success") else 0,
            "output_files": result.get("output_files", []),
            "logs": result.get("logs", []),
            "summary": result.get("summary", ""),
            "error": result.get("error", ""),
            "missing_required_files": result.get("missing_required_files", []),
            "delivery_evidence": result.get("delivery_evidence"),
            "recovery_reason": result.get("recovery_reason"),
        }

        # 更新 ctx 中的 agent 状态
        if agent_id in ctx.agents:
            transition_agent(
                ctx.agents[agent_id],
                execution_status[agent_id]["status"],
                progress=execution_status[agent_id]["progress"],
                message="Execution finished",
            )
            ctx.agents[agent_id]["output_files"] = result.get("output_files", [])

        delivery_files = (
            (result.get("delivery_evidence") or {}).get("files") or []
        )
        if result.get("success") and delivery_files:
            phase_manager = _phase_managers.get(project_id)
            agent_info = ctx.agents.get(agent_id, {})
            if phase_manager:
                for file_path in result.get("output_files", []):
                    normalized = str(file_path).replace("\\", "/")
                    if normalized.startswith("output/") or normalized.endswith(".log"):
                        continue
                    phase_manager.register_file(
                        file_path=normalized,
                        agent_id=agent_id,
                        agent_role=agent_info.get("role", ""),
                        phase_id=agent_info.get("phase_id", ""),
                        subproject_id=subproject_id,
                        task_id=str(
                            (phase_attempt_payload or {}).get("task_id")
                            or subproject_id
                        ),
                    )

        # 更新子项目进度
        for sp in ctx.subprojects:
            if sp["id"] == subproject_id:
                sp["status"] = "completed" if result.get("success") else "failed"
                sp["progress"] = 100 if result.get("success") else 0
                sp["output_files"] = list(result.get("output_files") or [])
                if result.get("success"):
                    sp.pop("error", None)
                else:
                    sp["error"] = result.get("error") or "Execution failed without a diagnostic"
                break

        # Repair execution never runs the raw ad-hoc QC helper.  The phase
        # Supervisor state machine below is the only authority allowed to
        # reconcile defects or project Agent/phase status.
        if is_fix_task and result.get("success") and not defer_fix_qc:
            execution_status[agent_id]["qc_after_fix"] = {
                "status": "pending_authoritative_supervisor",
                "fix_attempt": _fix_attempt_counts.get(agent_id, 0),
            }

        heartbeat_stop.set()
        await heartbeat_task
        if lease_state.get("error"):
            # The file evidence was captured before the completion-edge lease
            # check. Preserve a recoverable state instead of reporting success
            # or scheduling another model rewrite from output_files alone.
            result["success"] = False
            result["status"] = "pending_verification"
            result["error"] = lease_state["error"]
            if agent_id in ctx.agents:
                transition_agent(
                    ctx.agents[agent_id],
                    "failed",
                    progress=100,
                    message="Delivery awaits deterministic verification after lease loss",
                )
                ctx.agents[agent_id]["recovery_status"] = "pending_verification"

        if phase_attempt_payload:
            await asyncio.to_thread(
                _assert_current_phase_attempt, phase_attempt_payload,
            )

        if result.get("success") and delivery_files:
            agent_info = ctx.agents.get(agent_id, {})
            phase_id = str(
                agent_info.get("phase_id")
                or (phase_attempt_payload or {}).get("phase_id")
                or ""
            )
            phase = next(
                (
                    item for item in (
                        getattr(_phase_managers.get(project_id), "phases", []) or []
                    )
                    if str(item.get("phase_id") or item.get("id") or "") == phase_id
                ),
                {},
            )
            phase_plan = (
                dict(phase.get("phase_plan") or {})
                if isinstance(phase.get("phase_plan"), dict)
                else {}
            )
            task_id = str(
                (phase_attempt_payload or {}).get("task_id")
                or subproject_id
                or ""
            )
            if phase_plan.get("schema_version") != "phase-plan/v1":
                raise ValueError(
                    "Execution requires the confirmed phase-plan/v1 contract"
                )
            if not any(
                isinstance(item, dict) and str(item.get("task_id") or "") == task_id
                for item in phase_plan.get("tasks") or []
            ):
                raise ValueError(
                    f"Execution task is absent from phase-plan/v1: {task_id}"
                )
            documents = await asyncio.to_thread(
                record_successful_task_delivery,
                workspace=Path(ctx.workspace),
                project_id=project_id,
                phase_id=phase_id,
                phase_plan=phase_plan,
                task_id=task_id,
                agent_id=agent_id,
                expert_id=str(agent_info.get("expert_id") or ""),
                agent_role=str(agent_info.get("role") or ""),
                summary=str(result.get("summary") or ""),
                delivery_evidence=result.get("delivery_evidence") or {},
                baseline_files=transaction_snapshot.get("files") or {},
            )
            document_projection = {
                "phase_delivery_path": str(
                    documents["phase_delivery_path"].relative_to(ctx.workspace)
                ).replace("\\", "/"),
                "responsibility_path": str(
                    documents["responsibility_path"].relative_to(ctx.workspace)
                ).replace("\\", "/"),
                "responsibility_ledger_revision": documents[
                    "responsibility_ledger_revision"
                ],
            }
            result["delivery_documents"] = document_projection
            execution_status[agent_id]["delivery_documents"] = document_projection
            if agent_id in ctx.agents:
                agent_record = ctx.agents[agent_id]
                agent_record["delivery_documents"] = document_projection
                # phase-plan/v1 intentionally leaves paths to the executing
                # expert. Once the runner has committed those exact bytes and
                # bound ownership, QA and any repair lease must recognize only
                # those delivered paths as the Agent's concrete file scope.
                committed_paths = {
                    str(item.get("path") or "").replace("\\", "/")
                    for item in documents.get("files") or []
                    if str(item.get("path") or "").strip()
                }
                agent_record["allowed_path_prefixes"] = sorted(
                    {
                        str(path).replace("\\", "/")
                        for path in agent_record.get("allowed_path_prefixes") or []
                        if str(path).strip()
                    }
                    | committed_paths
                )

        _refresh_project_rollup(ctx)
        # Completing the last executable task in a phase must advance the
        # real workflow into QC. Locked DAG attempts are finalized by their
        # phase coordinator, so they must not start QA before the coordinator
        # persists the current-generation completion receipt.
        receipt_projection = _locked_agent_receipt_projection(
            ctx, ctx.agents.get(agent_id, {}),
        )
        if (
            receipt_projection is None
            or receipt_projection.get("status") == "completed"
        ):
            await _start_phase_quality_cycle_if_ready(ctx)
        await _persist_all_async()
        # Also persist execution status
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _persist_execution_state)
        transaction_committed = bool(result.get("success"))
        return result

    except LeaseConflict as e:
        return {
            "success": False,
            "status": "blocked",
            "error": _sanitize_error(e),
            "output_files": [],
            "logs": [],
        }
    except Exception as e:
        # 记录详细错误日志
        logger.error(
            "[Agent执行失败] project_id=%s agent_id=%s subproject=%s error=%s",
            project_id, agent_id, subproject_id, e, exc_info=True
        )
        
        execution_status[agent_id] = {
            "status": "failed",
            "progress": 0,
            "error": _sanitize_error(e),
            "output_files": [],
            "logs": [f"[{time.strftime('%H:%M:%S')}] 执行失败: {str(e)}"],
        }
        
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _persist_execution_state)
        _mark_agent_task_failed(ctx, agent_id, subproject_id, _sanitize_error(e))
        _refresh_project_rollup(ctx)
        await _persist_all_async()
        result = {"success": False, **execution_status[agent_id]}
        return result
    finally:
        heartbeat_stop.set()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        if not transaction_committed:
            try:
                await asyncio.to_thread(
                    _restore_execution_transaction,
                    ctx,
                    agent_id,
                    subproject_id,
                    transaction_snapshot,
                    execution_guard,
                )
            except Exception as restore_error:
                agent = ctx.agents.get(agent_id)
                if agent is not None:
                    agent["recovery_status"] = "pending_verification"
                execution_status.setdefault(agent_id, {}).update({
                    "status": "pending_verification",
                    "recovery_reason": "transaction_restore_not_authorized",
                    "error": _sanitize_error(restore_error),
                })
                result.update({
                    "success": False,
                    "status": "pending_verification",
                    "recovery_reason": "transaction_restore_not_authorized",
                    "error": _sanitize_error(restore_error),
                })
                await _persist_all_async()
                await asyncio.to_thread(_persist_execution_state)
                logger.error(
                    "Execution transaction restore failed project=%s agent=%s: %s",
                    project_id,
                    agent_id,
                    _sanitize_error(restore_error),
                    exc_info=True,
                )
        # A completed or failed execution no longer owns its delivery files.
        # Keeping the lease active blocks the next phase (and can survive
        # across Render restarts) even though the Agent is already terminal.
        agent = ctx.agents.get(agent_id)
        owned_lock_id = str(transaction_snapshot.get("lock_id") or "")
        current_lock_id = str(agent.get("lock_id") or "") if agent else ""
        if owned_lock_id and current_lock_id == owned_lock_id:
            try:
                await asyncio.to_thread(expert_lock.release_lock, owned_lock_id)
            except Exception as exc:
                logger.warning(
                    "Failed to release terminal Agent lock project=%s agent=%s: %s",
                    project_id, agent_id, _sanitize_error(exc),
                )
            if agent is not None:
                agent["lock_id"] = None
                agent["lock_run_id"] = None
                agent["locked_until"] = None

async def _run_agent_task(
    project_id: str,
    agent_id: str,
    subproject_id: str,
    subproject_name: str,
    description: str,
    tech_stack: List[str],
    project_context: str,
    user_api_config: Optional[Dict[str, Any]] = None,
    defer_fix_qc: bool = False,
    artifact_policy: Optional[Dict[str, Any]] = None,
    execution_guard=None,
    phase_attempt_payload: Optional[Dict[str, Any]] = None,
):
    """Serialize executions for one expert to prevent lifecycle state races."""
    key = (project_id, agent_id)
    lock = _agent_task_locks.setdefault(key, asyncio.Lock())
    async with lock:
        return await _run_agent_task_unlocked(
            project_id=project_id,
            agent_id=agent_id,
            subproject_id=subproject_id,
            subproject_name=subproject_name,
            description=description,
            tech_stack=tech_stack,
            project_context=project_context,
            user_api_config=user_api_config,
            defer_fix_qc=defer_fix_qc,
            artifact_policy=artifact_policy,
            execution_guard=execution_guard,
            phase_attempt_payload=phase_attempt_payload,
        )


def _canonical_run_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return the secret-free payload that is safe to persist and replay."""
    allowed = {
        "project_id", "agent_id", "subproject_id", "subproject_name",
        "description", "tech_stack", "project_context", "defer_fix_qc",
        "artifact_policy", "execution_generation", "phase_id", "task_id",
        "contract_digest", "requirements_revision",
        "artifact_baseline_digest", "dispatch_attempt_digest",
        "phase_coordinator_run_id",
    }
    return {key: payload[key] for key in sorted(payload) if key in allowed}


def _assert_current_phase_attempt(payload: Dict[str, Any]) -> None:
    """Block orphaned locked-task runs before they can write project files."""
    coordinator_run_id = str(
        payload.get("phase_coordinator_run_id") or ""
    )
    if not coordinator_run_id:
        project_id = str(payload.get("project_id") or "")
        phase_manager = _phase_managers.get(project_id)
        if (
            phase_manager
            and (
                getattr(phase_manager, "project_contract", {}) or {}
            ).get("locked")
            and payload.get("phase_id")
            and payload.get("task_id")
        ):
            raise LeaseConflict(
                "locked phase task is missing coordinator identity"
            )
        return
    project_id = str(payload.get("project_id") or "")
    phase_id = str(payload.get("phase_id") or "")
    phase_manager = _phase_managers.get(project_id)
    phase = phase_manager.get_phase(phase_id) if phase_manager else None
    coordinator = (phase or {}).get("execution_coordinator") or {}
    try:
        coordinator_run = _run_registry.get(coordinator_run_id)
    except Exception as exc:
        raise LeaseConflict("phase coordinator run is unavailable") from exc
    coordinator_payload = coordinator_run.get("payload") or {}
    expected_identity = {
        "execution_generation": str(
            phase.get("execution_generation") or ""
        ) if phase else "",
        "contract_digest": str(
            phase.get("execution_contract_digest") or ""
        ) if phase else "",
        "requirements_revision": int(
            phase.get("execution_requirements_revision") or 0
        ) if phase else 0,
        "artifact_baseline_digest": str(
            phase.get("execution_artifact_baseline_digest") or ""
        ) if phase else "",
    }
    task_id = str(payload.get("task_id") or "")
    agent_id = str(payload.get("agent_id") or "")
    task_owners = [
        str(task.get("agent_id") or "")
        for wave in (
            ((phase or {}).get("execution_dispatch_plan") or {}).get("waves")
            or []
        )
        for task in (wave or [])
        if isinstance(task, dict)
        and str(task.get("task_id") or "") == task_id
    ]
    dispatch_plan = (phase or {}).get("execution_dispatch_plan") or {}
    plan_task_ids = [
        str(item) for item in (dispatch_plan.get("task_ids") or [])
        if str(item)
    ]
    task_owner_matches = (
        task_owners == [agent_id]
        and plan_task_ids.count(task_id) == 1
    )
    repair_task_ids = {
        str(item)
        for item in (coordinator.get("repair_task_ids") or [])
        if str(item)
    }
    repair_attempt_matches = (
        not repair_task_ids
        or (
            task_id in repair_task_ids
            and bool(str(payload.get("dispatch_attempt_digest") or ""))
        )
    )
    project_contract = (
        getattr(phase_manager, "project_contract", {}) or {}
        if phase_manager else {}
    )
    task_scope_matches = True
    if int(project_contract.get("contract_version") or 0) >= 3:
        expected_task_files = {
            str(item.get("path") or "").replace("\\", "/")
            for item in (project_contract.get("required_files") or [])
            if isinstance(item, dict)
            and str(item.get("phase_id") or "") == phase_id
            and str(item.get("task_id") or "") == task_id
            and item.get("required", True)
            and str(item.get("path") or "")
        }
        rebuild_manifest = (phase or {}).get("rebuild_file_manifest") or {}
        rebuild_files_by_task = rebuild_manifest.get("by_task_id")
        if isinstance(rebuild_files_by_task, dict):
            executable_task_files = {
                str(item).replace("\\", "/")
                for item in (rebuild_files_by_task.get(task_id) or [])
                if str(item)
            }
            expected_task_files &= executable_task_files
        artifact_policy = payload.get("artifact_policy") or {}
        required_task_files = {
            str(item).replace("\\", "/")
            for item in (
                artifact_policy.get("planned_files")
                or artifact_policy.get("required_files")
                or []
            )
            if str(item)
        }
        allowed_task_files = {
            str(item).replace("\\", "/")
            for item in (artifact_policy.get("allowed_path_prefixes") or [])
            if str(item)
        }
        def _scope_covers(path: str) -> bool:
            return any(
                path == scope
                or (scope.endswith("/") and path.startswith(scope))
                for scope in allowed_task_files
            )

        task_scope_matches = (
            required_task_files == expected_task_files
            and (
                artifact_policy.get("workspace_exclusive") is True
                or all(_scope_covers(path) for path in expected_task_files)
            )
        ) if expected_task_files else not required_task_files
    if (
        not phase
        or str(coordinator_run.get("run_type") or "") != "phase.dispatch"
        or str(coordinator.get("durable_run_id") or "")
        != coordinator_run_id
        or str(coordinator.get("status") or "") not in {
            "starting", "running",
        }
        or coordinator_run.get("status") not in {"pending", "running"}
        or str(coordinator_payload.get("project_id") or "") != project_id
        or str(coordinator_payload.get("phase_id") or "") != phase_id
        or not task_id
        or not agent_id
        or not task_owner_matches
        or not repair_attempt_matches
        or not task_scope_matches
        or str(payload.get("execution_generation") or "")
        != expected_identity["execution_generation"]
        or str(payload.get("contract_digest") or "")
        != expected_identity["contract_digest"]
        or int(payload.get("requirements_revision") or 0)
        != expected_identity["requirements_revision"]
        or str(payload.get("artifact_baseline_digest") or "")
        != expected_identity["artifact_baseline_digest"]
        or str(coordinator_payload.get("execution_generation") or "")
        != expected_identity["execution_generation"]
        or str(coordinator_payload.get("contract_digest") or "")
        != expected_identity["contract_digest"]
        or int(coordinator_payload.get("requirements_revision") or 0)
        != expected_identity["requirements_revision"]
        or str(coordinator_payload.get("artifact_baseline_digest") or "")
        != expected_identity["artifact_baseline_digest"]
        or str(payload.get("dispatch_attempt_digest") or "")
        != str(coordinator.get("dispatch_attempt_digest") or "")
        or str(coordinator_payload.get("dispatch_attempt_digest") or "")
        != str(coordinator.get("dispatch_attempt_digest") or "")
    ):
        raise LeaseConflict("phase task attempt is stale or superseded")


def _run_idempotency_key(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "agent.execute:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _guard_cancelled_run(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise RuntimeError("execution was cancelled or exceeded its hard timeout")


async def _maintain_durable_run_lease(
    run_id: str,
    owner: str,
    stop_event: asyncio.Event,
) -> None:
    interval = max(5.0, _RUN_LEASE_SECONDS / 3)
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            try:
                await asyncio.to_thread(
                    _run_registry.heartbeat,
                    run_id,
                    owner,
                    lease_seconds=_RUN_LEASE_SECONDS,
                )
            except LeaseConflict:
                execution_fence = _run_execution_guards.get(run_id)
                if execution_fence:
                    execution_fence.revoke()
                cancel = _run_cancel_events.get(run_id)
                if cancel:
                    cancel.set()
                return


async def _wait_for_agent_run_file_lease(
    ctx: ProjectContext,
    agent_id: str,
    run_id: str,
    attempt_scope: Dict[str, Any],
    payload: Dict[str, Any],
    cancel_event: threading.Event,
) -> Dict[str, Any]:
    """Wait through ordinary file contention without failing the task run.

    The durable run is already claimed and heartbeating.  Keeping that single
    attempt alive prevents lock contention from consuming execution retries or
    propagating a false critical failure to the phase coordinator.
    """
    delay = 0.25
    waiting = False
    while True:
        _guard_cancelled_run(cancel_event)
        await asyncio.to_thread(_assert_current_phase_attempt, payload)
        try:
            lease = await asyncio.to_thread(
                _ensure_agent_run_file_lease,
                ctx,
                agent_id,
                run_id,
                attempt_scope,
            )
        except LeaseConflict as exc:
            waiting = True
            agent = ctx.agents.get(agent_id)
            if agent is None:
                raise
            agent["recovery_status"] = "waiting_file_lock"
            agent["file_lock_wait_reason"] = _sanitize_error(exc)
            agent.setdefault("file_lock_waiting_since", time.time())
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)
            continue
        if waiting:
            agent = ctx.agents.get(agent_id) or {}
            agent["recovery_status"] = "resumed_after_file_lock"
            agent.pop("file_lock_wait_reason", None)
            agent.pop("file_lock_waiting_since", None)
        return lease


def _evidence_for_result(run_id: str, result: Dict[str, Any]):
    """Build runner-controlled evidence; model prose and file existence never pass."""
    from core.evidence import create_evidence, evaluate_evidence_gate

    validation = result.get("validation")
    valid = bool(isinstance(validation, dict) and validation.get("valid") is True)
    issues = list(validation.get("issues") or []) if isinstance(validation, dict) else []
    record = create_evidence(
        "artifact_validation",
        run_id,
        "metis.runner",
        {
            "validator": "execution_agent.delivery_validator",
            "checks": [{"name": "deterministic delivery validation", "passed": valid}],
            "output_summary": "; ".join(str(item) for item in issues[:5]) or "validation passed",
        },
    )
    gate = evaluate_evidence_gate([record], ["artifact_validation"], run_id=run_id)
    return record, gate


async def _execute_durable_agent_run(
    run_id: str,
    agent_id: str = "",
    project_id: str = "",
    description: str = "",
) -> Dict[str, Any]:
    """Execute all bounded attempts for one persisted run."""
    last_result: Dict[str, Any] = {}
    try:
        while True:
            run = await asyncio.to_thread(_run_registry.get, run_id)
            if run["status"] != "pending":
                return run
            due = float(run.get("next_attempt_at") or 0)
            delay = max(0.0, due - time.time())
            if delay:
                await asyncio.sleep(delay)

            owner = f"metis-worker:{os.getpid()}:{run_id}"
            try:
                run = await asyncio.to_thread(
                    _run_registry.claim,
                    run_id,
                    owner,
                    lease_seconds=_RUN_LEASE_SECONDS,
                )
            except LeaseConflict:
                return await asyncio.to_thread(_run_registry.get, run_id)

            cancel_event = threading.Event()
            _run_cancel_events[run_id] = cancel_event
            lease_stop = asyncio.Event()
            lease_task = asyncio.create_task(
                _maintain_durable_run_lease(run_id, owner, lease_stop),
                name=f"run-lease-{run_id}",
            )
            payload = dict(run.get("payload") or {})
            project_id = str(payload.get("project_id") or project_id)
            agent_id = str(payload.get("agent_id") or agent_id)
            subproject_id = str(payload.get("subproject_id") or "")
            ctx = projects.get(project_id)
            if ctx is None or agent_id not in ctx.agents:
                lease_stop.set()
                await lease_task
                blocked = await asyncio.to_thread(
                    _run_registry.block, run_id, reason="project or agent no longer exists",
                )
                return blocked
            try:
                await asyncio.to_thread(
                    _assert_current_phase_attempt, payload,
                )
            except LeaseConflict as exc:
                lease_stop.set()
                await lease_task
                blocked = await asyncio.to_thread(
                    _run_registry.block,
                    run_id,
                    reason=f"phase task attempt rejected: {_sanitize_error(exc)}",
                )
                return blocked
            execution_fence = ProjectExecutionGuard(
                project_id,
                Path(ctx.workspace),
                generation=f"{run_id}:{run.get('attempt_count', 0)}",
                authorization_check=(
                    (lambda: _assert_current_phase_attempt(payload))
                    if payload.get("phase_coordinator_run_id")
                    else None
                ),
            )
            _run_execution_guards[run_id] = execution_fence
            try:
                lease_claim = await _wait_for_agent_run_file_lease(
                    ctx,
                    agent_id,
                    run_id,
                    dict(payload.get("artifact_policy") or {}),
                    payload,
                    cancel_event,
                )
                execution_fence.bind_expert_lock(
                    str(lease_claim.get("lock_id") or "")
                )
            except (ValueError, RuntimeError) as exc:
                lease_stop.set()
                await lease_task
                reason = f"run file lease invalid: {_sanitize_error(exc)}"
                blocked = await asyncio.to_thread(
                    _run_registry.block, run_id, reason=reason,
                )
                return blocked
            owner_config = _project_owner_api_config(ctx)

            try:
                last_result = await asyncio.wait_for(
                    _run_agent_task(
                        project_id=project_id,
                        agent_id=agent_id,
                        subproject_id=subproject_id,
                        subproject_name=str(payload.get("subproject_name") or "Task"),
                        description=str(payload.get("description") or ""),
                        tech_stack=list(payload.get("tech_stack") or []),
                        project_context=str(payload.get("project_context") or ""),
                        user_api_config=owner_config,
                        defer_fix_qc=bool(payload.get("defer_fix_qc")),
                        artifact_policy=payload.get("artifact_policy"),
                        execution_guard=execution_fence,
                        phase_attempt_payload=payload,
                    ),
                    timeout=float(run["timeout_seconds"]),
                )
                if cancel_event.is_set():
                    execution_fence.revoke()
                    current = await asyncio.to_thread(_run_registry.get, run_id)
                    if current["status"] == "running":
                        current = await asyncio.to_thread(
                            _run_registry.cancel,
                            run_id,
                            reason="execution cancellation observed",
                            actor=owner,
                        )
                    return current

                evidence, gate = _evidence_for_result(run_id, last_result)
                last_result["evidence"] = [evidence.to_dict()]
                last_result["evidence_gate"] = gate.to_dict()
                if last_result.get("success") and gate.passed:
                    finished = await asyncio.to_thread(
                        _run_registry.succeed, run_id, owner, result=redact_value(last_result),
                    )
                elif last_result.get("status") == "pending_verification":
                    finished = await asyncio.to_thread(
                        _run_registry.block,
                        run_id,
                        reason="delivery recorded but file lease was lost; deterministic verification required",
                    )
                elif last_result.get("status") == "blocked":
                    finished = await asyncio.to_thread(
                        _run_registry.block,
                        run_id,
                        reason=str(
                            last_result.get("error")
                            or "phase task attempt was superseded"
                        ),
                    )
                else:
                    error = str(
                        last_result.get("error")
                        or "; ".join(gate.reasons)
                        or "execution failed without verified evidence"
                    )
                    last_result.update({"success": False, "status": "failed", "error": error})
                    finished = await asyncio.to_thread(
                        _run_registry.fail,
                        run_id,
                        owner,
                        error=error,
                        retryable=True,
                    )
            except asyncio.TimeoutError:
                cancel_event.set()
                execution_fence.revoke()
                error = f"execution exceeded hard timeout of {run['timeout_seconds']} seconds"
                _mark_agent_task_failed(ctx, agent_id, subproject_id, error)
                finished = await asyncio.to_thread(
                    _run_registry.mark_timeout, run_id, owner, error=error,
                )
            except asyncio.CancelledError:
                cancel_event.set()
                execution_fence.revoke()
                current = await asyncio.to_thread(_run_registry.get, run_id)
                if current["status"] == "running":
                    await asyncio.to_thread(
                        _run_registry.cancel,
                        run_id,
                        reason="background task cancelled",
                        actor=owner,
                    )
                raise
            except Exception as exc:
                execution_fence.revoke()
                error = _sanitize_error(exc)
                _mark_agent_task_failed(ctx, agent_id, subproject_id, error)
                try:
                    finished = await asyncio.to_thread(
                        _run_registry.fail,
                        run_id,
                        owner,
                        error=error,
                        retryable=True,
                    )
                except ExecutionRunError:
                    finished = await asyncio.to_thread(_run_registry.get, run_id)
            finally:
                execution_fence.revoke()
                lease_stop.set()
                await lease_task

            execution_status.setdefault(agent_id, {}).update({
                "run_id": run_id,
                "run_status": finished["status"],
                "attempt_count": finished["attempt_count"],
                "max_retries": finished["max_retries"],
            })
            if finished["status"] == "pending":
                continue
            if finished["status"] != "succeeded":
                execution_status.setdefault(agent_id, {})["status"] = finished["status"]
                execution_status[agent_id]["error"] = finished.get("last_error") or last_result.get("error", "")
            return finished
    finally:
        _run_cancel_events.pop(run_id, None)
        _run_execution_guards.pop(run_id, None)
        _active_run_tasks.pop(run_id, None)


async def _schedule_durable_agent_run(
    payload: Dict[str, Any],
    *,
    client_idempotency_key: Optional[str] = None,
) -> tuple[Dict[str, Any], bool]:
    payload = _canonical_run_payload(payload)
    actor_id = f"{payload['project_id']}:{payload['agent_id']}"
    client_reservation = None
    if client_idempotency_key:
        client_reservation = await asyncio.to_thread(
            _execution_idempotency.reserve,
            "agent.execute",
            actor_id,
            client_idempotency_key,
            payload,
        )
        if not client_reservation["acquired"] and client_reservation["status"] == "completed":
            prior = client_reservation.get("response") or {}
            return await asyncio.to_thread(_run_registry.get, prior["run_id"]), False

    run, created = await asyncio.to_thread(
        _run_registry.create_or_get_run,
        idempotency_key=_run_idempotency_key(payload),
        run_type="agent.execute",
        actor_id=actor_id,
        timeout_seconds=_RUN_TIMEOUT_SECONDS,
        payload=payload,
        project_id=payload["project_id"],
        critical=True,
        max_retries=_RUN_MAX_RETRIES,
        retry_backoff=_RUN_RETRY_BACKOFF,
    )
    if client_idempotency_key and client_reservation and client_reservation["acquired"]:
        await asyncio.to_thread(
            _execution_idempotency.complete,
            "agent.execute",
            actor_id,
            client_idempotency_key,
            payload,
            resource_type="execution_run",
            resource_id=run["run_id"],
            response={"run_id": run["run_id"]},
        )
    if run["status"] == "pending" and run["run_id"] not in _active_run_tasks:
        task = _safe_create_task(
            _execute_durable_agent_run(
                run["run_id"],
                agent_id=str(payload["agent_id"]),
                project_id=str(payload["project_id"]),
                description=str(payload.get("description") or ""),
            ),
            name=f"durable-run-{run['run_id']}",
        )
        _active_run_tasks[run["run_id"]] = task
    return run, created


async def resume_pending_execution_runs() -> int:
    """Resume persisted pending attempts after startup using their safe payload."""
    # Reconcile phase authority before scheduling child attempts. Otherwise a
    # stale pending child can acquire a lease in the gap before its failed or
    # superseded coordinator is made terminal.
    from api.routes_phases import resume_pending_phase_dispatches
    resumed = await resume_pending_phase_dispatches()
    pending = await asyncio.to_thread(
        _run_registry.list_runs,
        statuses=["pending"],
        run_type="agent.execute",
        limit=1000,
    )
    for run in pending:
        payload = dict(run.get("payload") or {})
        if payload.get("project_id") not in projects:
            await asyncio.to_thread(
                _run_registry.block,
                run["run_id"],
                reason="project unavailable during startup recovery",
            )
            continue
        if run["run_id"] not in _active_run_tasks:
            _active_run_tasks[run["run_id"]] = _safe_create_task(
                _execute_durable_agent_run(
                    run["run_id"],
                    agent_id=str(payload.get("agent_id") or ""),
                    project_id=str(payload.get("project_id") or ""),
                    description=str(payload.get("description") or ""),
                ),
                name=f"recovered-run-{run['run_id']}",
            )
            resumed += 1
    for ctx in list(projects.values()):
        try:
            await _start_phase_quality_cycle_if_ready(ctx)
        except Exception:
            logger.exception(
                "Skipping invalid project quality recovery during startup: %s",
                getattr(ctx, "project_id", "unknown"),
            )
    return resumed


@router.post("/projects/{project_id}/agents/{agent_id}/execute")
async def execute_agent_task(
    project_id: str,
    agent_id: str,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key", max_length=256),
):
    """
    手动触发单个执行 Agent 开始工作。
    Agent 会调用 LLM 生成代码/文档，写入项目 workspace。
    执行在后台异步进行，立即返回任务已启动的响应。
    """
    if not isinstance(idempotency_key, str):
        idempotency_key = None
    ctx = _get_project(project_id)
    _reject_locked_phase_execution_bypass(project_id, agent_id=agent_id)
    try:
        with project_write_guard(project_id, ctx.workspace):
            pass
    except ProjectWriteFenceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if agent_id not in ctx.agents:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} 不存在")

    agent_info = ctx.agents[agent_id]
    agent_status = str(agent_info.get("status") or "idle").strip().lower()
    persisted_status = str(
        (execution_status.get(agent_id) or {}).get("status") or ""
    ).strip().lower()
    if agent_status in _ACTIVE_EXECUTION_STATUSES or persisted_status in _ACTIVE_EXECUTION_STATUSES:
        return {
            "success": True,
            "already_running": True,
            "message": f"Agent {agent_id} 已在执行中",
            "agent_id": agent_id,
            "status": persisted_status or agent_status,
        }
    if agent_status == "completed":
        return {
            "success": True,
            "already_completed": True,
            "message": f"Agent {agent_id} 已完成，无需重复执行",
            "agent_id": agent_id,
            "status": "completed",
        }
    sp_id = agent_info.get("subproject_id", "")

    # 找到对应子项目
    sp = next((s for s in ctx.subprojects if s["id"] == sp_id), None)
    if not sp:
        # 没有子项目信息，用 agent 自身信息构造
        sp = {
            "id": sp_id or agent_id,
            "name": agent_info.get("subproject_name", agent_info.get("role", "任务")),
            "description": f"由 {agent_info.get('role', '执行 Agent')} 负责的任务",
            "tech_stack": [],
        }

    execution_contract = agent_info.get("execution_contract") or {}
    # Manual reruns must execute the same contract as the automatic first run.
    # Falling back to the placeholder subproject is retained only for projects
    # created before execution contracts were persisted.
    project_context = (
        execution_contract.get("project_context")
        or ctx.pm.context_summary
        or ctx.description
        or ""
    )
    # C10: 把 PM 结构化规划（需求锚点 + 验收标准 + rubric）通过 HandoffPayload
    # 注入执行上下文，避免 PM 语义在交接时被压扁为一段非结构化摘要而丢失。
    # 仅补充（不覆盖）原有 project_context，向后兼容。
    try:
        _phase_mgr = _phase_managers.get(ctx.project_id)
        _phase_id = str(agent_info.get("phase_id") or "")
        _phase = _phase_mgr.get_phase(_phase_id) if (_phase_mgr and _phase_id) else None
        if isinstance(_phase, dict):
            _requirements = [
                str(r).strip() for r in (
                    _phase.get("requirement_units")
                    or _phase.get("requirements")
                    or []
                ) if str(r).strip()
            ] or [str(ctx.description or "").strip()][:1]
            _acceptance = [
                str(a).strip() for a in (_phase.get("acceptance_criteria") or [])
                if str(a).strip()
            ]
            if _requirements or _acceptance:
                _payload = HandoffPayload(
                    phase=_phase_id,
                    phase_name=str(_phase.get("phase_name") or _phase.get("name") or ""),
                    task_id=str(agent_info.get("task_id") or _phase_id),
                    from_agent_role="PM",
                    to_agent_role=str(agent_info.get("role") or ""),
                    requirements=_requirements,
                    acceptance_criteria=_acceptance,
                    rubric=make_rubric_from_requirements(_requirements),
                )
                _handoff_ctx = _payload.to_context_str()
                project_context = (
                    f"{_handoff_ctx}\n\n【项目背景】\n{project_context}"
                    if project_context else _handoff_ctx
                )
    except Exception:
        # handoff 上下文为增强项，构建失败不得阻断执行主流程
        pass
    base_description = (
        execution_contract.get("description")
        or sp.get("description", "")
    )

    # 后台异步执行：如果 Agent 已被标记为修复中/待修复，则优先使用返工任务
    exec_description = agent_info.get("fix_task") if agent_info.get("status") in ("fix_required", "fixing") and agent_info.get("fix_task") else base_description
    if agent_info.get("status") in ("fix_required", "fixing") and agent_info.get("fix_task"):
        exec_description = (
            f"{base_description}\n\n"
            f"【质检修复任务】\n{agent_info.get('fix_task', '')}\n\n"
            f"原始问题：请根据 PM/监督的修复指令完成返工"
        )
    if idempotency_key:
        execution_generation: Any = (
            "client:" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]
        )
    else:
        execution_generation = int(agent_info.get("execution_generation") or 0) + 1
        agent_info["execution_generation"] = execution_generation
    run, created = await _schedule_durable_agent_run({
        "project_id": project_id,
        "agent_id": agent_id,
        "subproject_id": execution_contract.get("subproject_id") or sp["id"],
        "subproject_name": execution_contract.get("subproject_name") or sp.get("name", "Task"),
        "description": exec_description,
        "tech_stack": list(execution_contract.get("tech_stack") or sp.get("tech_stack", [])),
        "project_context": project_context,
        "artifact_policy": execution_contract.get("artifact_policy"),
        "defer_fix_qc": bool(
            execution_contract.get("defer_fix_qc")
            or agent_info.get("pre_qa_repair_pending")
        ),
        "execution_generation": execution_generation,
    }, client_idempotency_key=idempotency_key)

    execution_status.setdefault(agent_id, {}).update({
        "run_id": run["run_id"],
        "run_status": run["status"],
    })

    return {
        "success": True,
        "message": f"Agent {agent_id} 已开始工作，正在后台生成代码...",
        "agent_id": agent_id,
        "subproject": sp.get("name"),
        "run_id": run["run_id"],
        "run_status": run["status"],
        "created": created,
    }

def _locked_agent_receipt_projection(
    ctx: ProjectContext,
    agent_info: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Project locked-task completion only after its durable receipt commits."""
    locked_tasks = [
        task for task in (agent_info.get("locked_tasks") or [])
        if isinstance(task, dict) and str(task.get("task_id") or "")
    ]
    if not locked_tasks:
        return None

    phase_id = str(agent_info.get("phase_id") or "")
    phase_manager = _phase_managers.get(ctx.project_id)
    phase = phase_manager.get_phase(phase_id) if phase_manager else None
    if not isinstance(phase, dict):
        return {"status": "working", "progress": 99}
    if (
        str(phase.get("status") or "").lower() == "completed"
        and phase.get("user_confirmed") is True
        and isinstance(phase.get("validated_completion_receipt"), dict)
    ):
        return {"status": "completed", "progress": 100}

    receipts = agent_info.get("task_execution_receipts") or {}
    coordinator = phase.get("execution_coordinator") or {}
    repair_task_ids = {
        str(task_id)
        for task_id in (coordinator.get("repair_task_ids") or [])
        if str(task_id)
    }
    dispatch_attempt_digest = str(
        coordinator.get("dispatch_attempt_digest") or ""
    )
    failure_statuses = {
        "failed", "error", "blocked", "cancelled", "canceled",
        "timeout", "timed_out",
    }
    expected_identity = {
        "phase_id": phase_id,
        "execution_generation": str(phase.get("execution_generation") or ""),
        "contract_digest": str(phase.get("execution_contract_digest") or ""),
        "requirements_revision": int(
            phase.get("execution_requirements_revision") or 0
        ),
        "artifact_baseline_digest": str(
            phase.get("execution_artifact_baseline_digest") or ""
        ),
    }
    for task in locked_tasks:
        task_id = str(task.get("task_id") or "")
        receipt = receipts.get(task_id)
        if not isinstance(receipt, dict):
            persisted = execution_status.get(
                str(agent_info.get("id") or ""),
                {},
            )
            persisted_status = str(
                persisted.get("status")
                or persisted.get("run_status")
                or ""
            ).strip().lower()
            if persisted_status in failure_statuses:
                return {
                    "status": "failed",
                    "progress": 0,
                    "error": str(
                        persisted.get("error")
                        or f"locked task {task_id} failed before receipt commit"
                    ),
                }
            agent_status = str(agent_info.get("status") or "").strip().lower()
            if agent_status in {"queued", "pending", "idle", "waiting"}:
                return {"status": "queued", "progress": 0}
            return {"status": "working", "progress": min(
                int(agent_info.get("progress") or 0), 99,
            )}
        is_current = (
            str(receipt.get("task_id") or task_id) == task_id
            and str(receipt.get("agent_id") or "")
            == str(agent_info.get("id") or "")
            and str(receipt.get("phase_id") or "") == expected_identity["phase_id"]
            and str(receipt.get("execution_generation") or "")
            == expected_identity["execution_generation"]
            and str(receipt.get("contract_digest") or "")
            == expected_identity["contract_digest"]
            and int(receipt.get("requirements_revision") or 0)
            == expected_identity["requirements_revision"]
            and str(receipt.get("artifact_baseline_digest") or "")
            == expected_identity["artifact_baseline_digest"]
            and (
                task_id not in repair_task_ids
                or (
                    bool(dispatch_attempt_digest)
                    and str(
                        receipt.get("dispatch_attempt_digest") or ""
                    ) == dispatch_attempt_digest
                    and str(
                        receipt.get("repair_coordinator_run_id") or ""
                    ) == str(coordinator.get("durable_run_id") or "")
                )
            )
        )
        durable_status = ""
        if not is_current:
            if durable_status in {"pending", "queued"}:
                return {"status": "queued", "progress": 0}
            return {"status": "working", "progress": min(
                int(agent_info.get("progress") or 0), 99,
            )}
        receipt_status = str(receipt.get("status") or "").strip().lower()
        if receipt_status in failure_statuses:
            return {
                "status": "failed",
                "progress": 0,
                "error": str(
                    receipt.get("error")
                    or receipt.get("error_code")
                    or f"locked task {task_id} failed"
                ),
            }
        if receipt_status != "succeeded":
            durable_run_id = str(
                receipt.get("completion_run_id")
                or receipt.get("start_run_id")
                or ""
            )
            durable_run = None
            if durable_run_id:
                try:
                    durable_run = _run_registry.get(durable_run_id)
                except RunNotFound:
                    durable_run = None
            durable_status = str(
                (durable_run or {}).get("status") or ""
            ).strip().lower()
            if durable_status in failure_statuses:
                durable_result = (durable_run or {}).get("result")
                if not isinstance(durable_result, dict):
                    durable_result = {}
                return {
                    "status": "failed",
                    "progress": 0,
                    "error": str(
                        (durable_run or {}).get("last_error")
                        or durable_result.get("error")
                        or f"locked task {task_id} failed"
                    ),
                }
            return {"status": "working", "progress": 99}
        if not str(receipt.get("completion_run_id") or ""):
            # Receipt was persisted before the run committed its completion
            # edge.  Fall back to the durable execution_status ledger which is
            # atomically updated after the run finishes.
            persisted = execution_status.get(agent_info.get("id") or "")
            if (
                isinstance(persisted, dict)
                and str(persisted.get("status") or "").lower() == "completed"
                and persisted.get("progress") == 100
            ):
                continue
            return {"status": "working", "progress": 99}
    from api import routes_phases

    if not routes_phases._current_phase_coordinator_run(
        ctx.project_id, phase_id, phase,
    ):
        return {"status": "working", "progress": 99}
    return {"status": "completed", "progress": 100}


@router.get("/projects/{project_id}/agents/{agent_id}/status")
async def get_agent_execution_status(project_id: str, agent_id: str):
    """获取执行 Agent 的实时工作状态"""
    ctx = _get_project(project_id)
    agent_info = ctx.agents.get(agent_id, {})
    exec_info = execution_status.get(agent_id, {})
    status = exec_info.get("status") or agent_info.get("status", "idle")
    progress = exec_info.get("progress", 0)
    error = exec_info.get("error", "")
    receipt_projection = _locked_agent_receipt_projection(ctx, agent_info)
    if receipt_projection:
        status = receipt_projection["status"]
        progress = receipt_projection["progress"]
        error = receipt_projection.get("error", "")
    return {
        "agent_id": agent_id,
        "role": agent_info.get("role"),
        "status": status,
        "progress": progress,
        "output_files": exec_info.get("output_files", agent_info.get("output_files", [])),
        "logs": exec_info.get("logs", []),
        "summary": exec_info.get("summary", ""),
        "error": error,
        "run_id": exec_info.get("run_id"),
        "run_status": exec_info.get("run_status"),
        "attempt_count": exec_info.get("attempt_count", 0),
        "max_retries": exec_info.get("max_retries", 0),
    }


def _request_actor(request: Request) -> tuple[str, str]:
    user = getattr(request.state, "current_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return str(user.user_id), str(user.role)


async def _get_project_run(project_id: str, run_id: str) -> Dict[str, Any]:
    try:
        run = await asyncio.to_thread(_run_registry.get, run_id)
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail="execution run not found") from exc
    if run.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="execution run not found")
    return run


@router.get("/projects/{project_id}/runs")
async def list_project_runs(project_id: str, status: Optional[str] = None, limit: int = 100):
    _get_project(project_id)
    statuses = [status] if status else None
    try:
        runs = await asyncio.to_thread(
            _run_registry.list_runs,
            statuses=statuses,
            project_id=project_id,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"project_id": project_id, "runs": runs, "count": len(runs)}


@router.get("/projects/{project_id}/runs/{run_id}")
async def get_project_run(project_id: str, run_id: str):
    run = await _get_project_run(project_id, run_id)
    run["events"] = await asyncio.to_thread(_run_registry.events, run_id)
    return run


@router.post("/projects/{project_id}/runs/{run_id}/cancel")
async def cancel_project_run(
    project_id: str,
    run_id: str,
    body: RunCancelRequest,
    request: Request,
):
    await _get_project_run(project_id, run_id)
    actor_id, actor_role = _request_actor(request)
    try:
        run = await asyncio.to_thread(
            _run_registry.cancel,
            run_id,
            reason=body.reason,
            actor=actor_id,
        )
    except (InvalidTransition, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    cancel_event = _run_cancel_events.get(run_id)
    if cancel_event:
        cancel_event.set()
    execution_fence = _run_execution_guards.get(run_id)
    if execution_fence:
        execution_fence.revoke()
    task = _active_run_tasks.get(run_id)
    if task and not task.done():
        task.cancel()
    record_audit_event(
        "EXECUTION_CANCELLED",
        actor_id,
        actor_role=actor_role,
        outcome="cancelled",
        project_id=project_id,
        resource_type="execution_run",
        resource_id=run_id,
        source_ip=request.client.host if request.client else "unknown",
        details={"reason": body.reason},
    )
    return run


@router.post("/projects/{project_id}/runs/{run_id}/takeover")
async def take_over_project_run(
    project_id: str,
    run_id: str,
    body: RunTakeoverRequest,
    request: Request,
):
    await _get_project_run(project_id, run_id)
    actor_id, actor_role = _request_actor(request)
    cancel_event = _run_cancel_events.get(run_id)
    if cancel_event:
        cancel_event.set()
    execution_fence = _run_execution_guards.get(run_id)
    if execution_fence:
        execution_fence.revoke()
    try:
        run = await asyncio.to_thread(
            _run_registry.take_over,
            run_id,
            f"human:{actor_id}",
            lease_seconds=body.lease_seconds,
            force=body.force,
        )
    except (InvalidTransition, LeaseConflict, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record_audit_event(
        "EXECUTION_TAKEOVER",
        actor_id,
        actor_role=actor_role,
        project_id=project_id,
        resource_type="execution_run",
        resource_id=run_id,
        source_ip=request.client.host if request.client else "unknown",
        details={"force": body.force, "lease_seconds": body.lease_seconds},
    )
    return run


@router.post("/projects/{project_id}/runs/{run_id}/resolve")
async def resolve_project_run(
    project_id: str,
    run_id: str,
    body: RunResolveRequest,
    request: Request,
):
    run = await _get_project_run(project_id, run_id)
    actor_id, actor_role = _request_actor(request)
    owner = f"human:{actor_id}"
    status = body.status.strip().lower()
    try:
        if status == "succeeded":
            from core.evidence import evaluate_evidence_gate
            gate = evaluate_evidence_gate(
                body.evidence,
                body.required_evidence,
                run_id=run_id,
            )
            if not gate.passed:
                raise HTTPException(status_code=409, detail={
                    "message": "verified evidence is required before success",
                    "gate": gate.to_dict(),
                })
            run = await asyncio.to_thread(
                _run_registry.succeed,
                run_id,
                owner,
                result={"resolved_by": actor_id, "evidence": redact_value(body.evidence)},
            )
        elif status == "failed":
            run = await asyncio.to_thread(
                _run_registry.fail,
                run_id,
                owner,
                error=body.reason,
                retryable=False,
            )
        elif status == "blocked":
            run = await asyncio.to_thread(_run_registry.block, run_id, reason=body.reason)
        elif status == "cancelled":
            run = await asyncio.to_thread(
                _run_registry.cancel, run_id, reason=body.reason, actor=actor_id,
            )
        else:
            raise HTTPException(status_code=422, detail="unsupported resolution status")
    except HTTPException:
        raise
    except (ExecutionRunError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record_audit_event(
        "EXECUTION_RESOLVED",
        actor_id,
        actor_role=actor_role,
        outcome="success" if status == "succeeded" else "failure",
        project_id=project_id,
        resource_type="execution_run",
        resource_id=run_id,
        source_ip=request.client.host if request.client else "unknown",
        details={"status": status, "reason": body.reason},
    )
    return run

@router.post("/projects/{project_id}/execute-all")
async def execute_all_agents(project_id: str):
    """
    触发项目所有执行 Agent 开始工作（批量后台执行）。
    每个 Agent 独立异步执行，互不阻塞。
    """
    ctx = _get_project(project_id)
    _reject_locked_phase_execution_bypass(project_id)
    try:
        with project_write_guard(project_id, ctx.workspace):
            pass
    except ProjectWriteFenceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not ctx.agents:
        raise HTTPException(status_code=400, detail="项目还没有执行 Agent，请先启动项目")

    started = []
    fallback_project_context = ctx.pm.context_summary or ctx.description or ""
    phase_manager = _phase_managers.get(project_id)
    current_phase_id = ""
    if phase_manager:
        current_phase = None
        if hasattr(phase_manager, "get_current_phase"):
            current_phase = phase_manager.get_current_phase()
        if not current_phase or str(current_phase.get("status") or "").lower() not in {
            "active", "in_progress",
        }:
            current_phase = next(
                (
                    phase for phase in phase_manager.phases
                    if str(phase.get("status") or "").lower() in {"active", "in_progress"}
                ),
                None,
            )
        if not current_phase:
            raise HTTPException(status_code=409, detail="没有可执行的当前阶段")
        current_phase_id = str(current_phase.get("phase_id") or "")

    for agent_id, agent_info in ctx.agents.items():
        agent_status = str(agent_info.get("status") or "idle").strip().lower()
        persisted_status = str(
            (execution_status.get(agent_id) or {}).get("status") or ""
        ).strip().lower()
        if agent_status not in {"queued", "idle"}:
            continue
        if persisted_status in _ACTIVE_EXECUTION_STATUSES or persisted_status == "completed":
            continue
        if current_phase_id and str(agent_info.get("phase_id") or "") != current_phase_id:
            continue

        sp_id = agent_info.get("subproject_id", "")
        sp = next((s for s in ctx.subprojects if s["id"] == sp_id), None)
        if not sp:
            sp = {
                "id": sp_id or agent_id,
                "name": agent_info.get("subproject_name", agent_info.get("role", "任务")),
                "description": f"由 {agent_info.get('role', '执行 Agent')} 负责的任务",
                "tech_stack": [],
            }

        execution_contract = agent_info.get("execution_contract") or {}
        run, created = await _schedule_durable_agent_run({
            "project_id": project_id,
            "agent_id": agent_id,
            "subproject_id": execution_contract.get("subproject_id") or sp["id"],
            "subproject_name": execution_contract.get("subproject_name") or sp.get("name", "Task"),
            "description": execution_contract.get("description") or sp.get("description", ""),
            "tech_stack": list(execution_contract.get("tech_stack") or sp.get("tech_stack", [])),
            "project_context": execution_contract.get("project_context") or fallback_project_context,
            "artifact_policy": execution_contract.get("artifact_policy"),
            "defer_fix_qc": bool(execution_contract.get("defer_fix_qc")),
        })
        started.append({
            "agent_id": agent_id,
            "role": agent_info.get("role"),
            "subproject": sp.get("name"),
            "run_id": run["run_id"],
            "run_status": run["status"],
            "created": created,
        })

    return {
        "success": True,
        "message": f"已触发 {len(started)} 个 Agent 开始工作，代码将写入项目文件夹",
        "started": started,
    }
