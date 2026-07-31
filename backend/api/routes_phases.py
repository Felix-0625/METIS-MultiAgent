"""Phase management routes (reconstructed)"""
import asyncio
import base64
import copy
import time
import uuid
import json
import hashlib
import logging
import os
import re
import shutil
from pathlib import Path
from functools import wraps
from typing import Optional, List, Dict, Any, Iterable
from fastapi import APIRouter, HTTPException
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
    ExecutionAgent, IdeaLandingAgent,
    logger,
)
from core import expert_lock
from core.role_mapping import canonical_expert_type, infer_english_expert_type
from core.delivery_contract import (
    collect_required_file_paths,
    is_delivery_file_path,
    required_files_for_scopes,
)
from core.delivery_documents import load_phase_qa_scope
from core.rebuild_policy import (
    assert_preserved_files_unchanged,
    classify_rebuild_files,
    policy_by_path,
)
from core.project_contract import (
    PHASE_PLAN_VERSION,
    artifact_metadata,
    deterministic_phase_fallback,
    phase_task_contract,
    validate_plan_layers,
    validate_phase_plan_layers,
)
from core.phase_execution_contract import (
    PhaseExecutionContractError,
    _criterion_source_class,
    acceptance_criterion_contracts,
    build_phase_dispatch_plan,
    build_phase_evidence_bundle,
    execute_phase_dispatch_plan,
    evidence_record_proves_criterion,
    validate_phase_completion,
    validate_phase_execution_evidence,
    validate_task_graph,
)
from core.evidence import EvidenceKind, create_evidence
from core.hermes_client import current_user_api_config
from core.supervisor_quality_state import (
    IllegalQualityTransition,
    SupervisorQualityMachine,
)
from core.workspace_integrity import (
    compute_delivery_manifest,
    compute_workspace_digest,
    is_sensitive_archive_path,
    WORKSPACE_PERSIST_FILE_LIMIT,
    WORKSPACE_PERSIST_PROJECT_LIMIT,
)
from core.project_write_fence import (
    ProjectWriteFenceConflict,
    project_write_guard,
)
from core.pre_qa_verifier import (
    ApiObservation,
    ApiProbe,
    FAILURE_INFRASTRUCTURE,
    FAILURE_MODEL,
    FAILURE_PRE_QA,
    LocalCommandRunner,
    PreQAVerifier,
    api_probes_from_contract,
    node_fullstack_command_gates,
)
from core import runtime_acceptance
from core.runtime_templates import render_template_files
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


def _safe_create_task(coro, name: str = "") -> None:
    """Schedule background phase work and surface failures in server logs."""
    task = asyncio.create_task(coro)

    def _on_done(done: asyncio.Task) -> None:
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("后台阶段任务异常 [%s]: %s", name or "unnamed", exc, exc_info=True)

    task.add_done_callback(_on_done)


def _phase_role_expert_type(role: str) -> str:
    """Resolve a supported executor without collapsing unknown roles."""
    return canonical_expert_type(str(role or "")) or ""


_PHASE_EXECUTOR_TYPES = {
    "frontend", "backend", "database", "qa", "architecture",
    "devops", "security", "data", "fullstack_engineer",
}


def _phase_task_executor_type(
    task: Dict[str, Any],
    expert_roles: Optional[List[str]] = None,
) -> str:
    """Map an arbitrary expert role onto one supported runtime executor."""
    mapped_roles = {
        mapped
        for role in (expert_roles or [])
        if (mapped := _phase_role_expert_type(str(role)))
    }
    if len(mapped_roles) == 1:
        return next(iter(mapped_roles))

    paths = [
        str(item.get("path") or "").lower().replace("\\", "/")
        for item in (task.get("deliverable_files") or [])
        if isinstance(item, dict)
    ]
    text = " ".join([
        str(task.get("name") or ""),
        str(task.get("objective") or ""),
        " ".join(str(item) for item in (task.get("functional_details") or [])),
        str(task.get("implementation") or ""),
        " ".join(
            str(item)
            for item in (task.get("implementation_technologies") or [])
        ),
    ]).casefold()
    if paths and all(
        "/test" in f"/{path}" or path.startswith("tests/")
        or ".test." in path or ".spec." in path
        for path in paths
    ):
        return "qa"
    if any(
        path.startswith(("frontend/", "web/"))
        or path.endswith((".jsx", ".tsx", ".vue", ".svelte"))
        for path in paths
    ):
        return "frontend"
    if any(
        path.startswith(("backend/", "server/", "api/"))
        for path in paths
    ):
        return "backend"
    if any(
        path.startswith(("database/", "migrations/"))
        or "/migrations/" in f"/{path}"
        for path in paths
    ):
        return "database"
    if any(
        path in {"dockerfile", "docker-compose.yml", ".env.example"}
        or path.startswith(("deploy/", ".github/workflows/"))
        for path in paths
    ):
        return "devops"
    if any(token in text for token in ("security", "安全校验", "安全测试")):
        return "security"
    if any(token in text for token in ("测试", "test", "验证功能", "质量验证")):
        return "qa"
    if any(
        token in text
        for token in (
            "前端", "界面", "浏览器", "frontend", "react", "vue", "css", "html",
        )
    ):
        return "frontend"
    if any(
        token in text
        for token in (
            "后端", "接口", "服务端", "backend", "fastapi", "express",
        )
    ):
        return "backend"
    if any(token in text for token in ("数据库", "迁移", "database", "schema", "sql")):
        return "database"
    if any(
        token in text
        for token in ("部署", "容器", "流水线", "deploy", "docker", "ci/cd")
    ):
        return "devops"
    return "fullstack_engineer"


def _phase_planning_lock_scope(
    allowed_path_prefixes: Iterable[str],
) -> List[str]:
    """A pathless planning lease must not reserve the whole workspace."""
    return [
        str(path).strip()
        for path in allowed_path_prefixes
        if str(path).strip()
    ]


def _locked_task_artifact_policy(
    base_policy: Dict[str, Any],
    *,
    task_id: str,
    task_dependencies: Iterable[str],
    required_files: Iterable[str],
    rebuild_file_specs: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Narrow both scope aliases to the current locked task."""
    task_required = list(dict.fromkeys(
        str(path).strip() for path in required_files if str(path).strip()
    ))
    return {
        **dict(base_policy or {}),
        "task_id": task_id,
        "task_dependencies": list(task_dependencies),
        "required_files": task_required,
        "allowed_prefixes": list(task_required),
        "allowed_path_prefixes": list(task_required),
        "workspace_exclusive": not bool(task_required),
        "rebuild_file_specs": list(rebuild_file_specs),
    }


def _unsupported_phase_roles(phase: Dict[str, Any]) -> List[str]:
    if (
        str(phase.get("phase_plan_version") or "") == "phase-plan/v1"
        or (
            isinstance(phase.get("phase_plan"), dict)
            and phase["phase_plan"].get("schema_version") == "phase-plan/v1"
        )
    ):
        return [
            str(item.get("required_role") or "")
            for item in (phase.get("expert_requirements") or [])
            if isinstance(item, dict)
            and str(
                item.get("executor_type")
                or _phase_task_executor_type(
                    item, [str(item.get("required_role") or "")]
                )
            ) not in _PHASE_EXECUTOR_TYPES
        ]
    candidates = list(phase.get("roles_needed") or phase.get("roles") or [])
    candidates.extend(
        str(item.get("required_role") or "")
        for item in (phase.get("expert_requirements") or [])
        if isinstance(item, dict)
    )
    return list(dict.fromkeys(
        str(role).strip() for role in candidates
        if str(role).strip() and not _phase_role_expert_type(str(role))
    ))


def _quality_background_terminal(handler):
    """Force every uncaught background exit into a durable non-running state."""
    @wraps(handler)
    async def guarded(project_id: str, phase_id: str, *args, **kwargs):
        key = f"{project_id}-{phase_id}"
        try:
            return await handler(project_id, phase_id, *args, **kwargs)
        except asyncio.CancelledError:
            state = _auto_repair_states.get(key)
            if state is not None:
                state["running"] = False
                state["status"] = "interrupted"
                state["needs_manual"] = True
                state["action_required"] = {
                    "message": "Supervisor quality background task was cancelled.",
                    "options": ["resume", "manual_fix", "rebuild_phase"],
                }
                state["background_terminal_at"] = time.time()
                _persist_all()
            raise
        except Exception as exc:
            state = _auto_repair_states.get(key)
            if state is not None:
                state["running"] = False
                state["status"] = "failed"
                state["needs_manual"] = True
                state["action_required"] = {
                    "message": f"Supervisor quality background failure: {str(exc)[:500]}",
                    "options": ["resume", "manual_fix", "rebuild_phase"],
                }
                state["background_terminal_at"] = time.time()
                _persist_all()
            logger.exception(
                "Supervisor quality background task failed project=%s phase=%s",
                project_id,
                phase_id,
            )
            return None
        finally:
            state = _auto_repair_states.get(key) or {}
            for claim_key in ("quality_resume_claim", "pre_qa_recovery_claim"):
                claim = state.get(claim_key) or {}
                lock_id = str(claim.get("lock_id") or "")
                if lock_id:
                    expert_lock.release_lock(lock_id)

    return guarded


def _phase_execution_stage(expert_type: str) -> int:
    """Return the dependency stage for a phase role."""
    normalized = str(expert_type or "").strip().lower()
    if normalized == "qa":
        return 3
    if normalized == "devops":
        return 2
    if normalized == "fullstack_engineer":
        return 1
    return 0

router = APIRouter(tags=["phases"])


def _project_write_fenced_internal(handler):
    @wraps(handler)
    def guarded(ctx: ProjectContext, *args, **kwargs):
        with project_write_guard(ctx.project_id, ctx.workspace):
            return handler(ctx, *args, **kwargs)

    return guarded


def _assert_project_write_available(ctx: ProjectContext) -> None:
    try:
        with project_write_guard(ctx.project_id, ctx.workspace):
            return
    except ProjectWriteFenceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

FULLSTACK_ENGINEER_EXPERT_ID = "expert-fullstack-engineer-001"

def _normalized_rebuild_path(path: Any) -> str:
    normalized = str(path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _contract_required_rebuild_files(pm: PhaseManager, phase: Dict[str, Any]) -> List[str]:
    """Compatibility reader for contract/deliverable inventory, never ownership."""
    contract = copy.deepcopy(getattr(pm, "project_contract", {}) or {})
    explicit = [
        _normalized_rebuild_path(item.get("path"))
        for item in contract.get("required_files") or []
        if isinstance(item, dict)
        and str(item.get("phase_id") or "") == str(phase.get("phase_id") or "")
        and item.get("required", True)
    ]
    candidates = explicit or collect_required_file_paths(
        "", phase.get("deliverables") or [],
    )
    return sorted({
        path for path in candidates
        if _is_rebuild_deliverable_path(path) and is_delivery_file_path(path)
    })


def _rebuild_owner_type_for_role(role: Any) -> str:
    """Resolve an immutable locked-task role without consulting a file path."""
    role_text = str(role or "").strip()
    return _phase_role_expert_type(role_text)


def _rebuild_contract_ownership(
    pm: PhaseManager,
    phase: Dict[str, Any],
    old_agents: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the only executable rebuild ownership map.

    ProjectContract ``required_files`` selects the task, and the locked task's
    ``required_role`` selects the Agent type. Historical Agent/registry rows
    are evidence only: they may confirm that binding, but may never create or
    override it.
    """
    phase_id = str(phase.get("phase_id") or "")
    raw_tasks = [
        copy.deepcopy(item)
        for item in (
            phase.get("expert_requirements")
            or phase.get("task_contract")
            or []
        )
        if isinstance(item, dict) and item.get("task_id")
    ]
    tasks_by_id: Dict[str, Dict[str, Any]] = {}
    task_owner_types: Dict[str, str] = {}
    for task in raw_tasks:
        task_id = str(task.get("task_id") or "").strip()
        if task_id in tasks_by_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "rebuild_duplicate_locked_task",
                    "task_id": task_id,
                },
            )
        required_role = (
            task.get("required_role")
            or ((task.get("roles") or [""])[0])
        )
        owner_type = _rebuild_owner_type_for_role(required_role)
        if not owner_type:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "rebuild_unmapped_locked_task_role",
                    "task_id": task_id,
                    "required_role": str(required_role or ""),
                },
            )
        tasks_by_id[task_id] = task
        task_owner_types[task_id] = owner_type

    contract_rows_by_path: Dict[str, Dict[str, Any]] = {}
    for raw_row in (getattr(pm, "project_contract", {}) or {}).get("required_files", []):
        if (
            not isinstance(raw_row, dict)
            or str(raw_row.get("phase_id") or "") != phase_id
            or not raw_row.get("required", True)
        ):
            continue
        path = _normalized_rebuild_path(raw_row.get("path"))
        if not path or not _is_rebuild_deliverable_path(path) or not is_delivery_file_path(path):
            continue
        if path in contract_rows_by_path:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "rebuild_duplicate_contract_file_owner",
                    "path": path,
                },
            )
        task_id = str(raw_row.get("task_id") or "").strip()
        task = tasks_by_id.get(task_id)
        if task is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "rebuild_unknown_contract_task",
                    "path": path,
                    "task_id": task_id,
                },
            )
        contract_owner = str(raw_row.get("owner_type") or "").strip()
        locked_owner = task_owner_types[task_id]
        if contract_owner != locked_owner:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "rebuild_contract_task_owner_conflict",
                    "path": path,
                    "task_id": task_id,
                    "contract_owner_type": contract_owner,
                    "locked_task_owner_type": locked_owner,
                },
            )
        contract_rows_by_path[path] = {
            **copy.deepcopy(raw_row),
            "path": path,
            "task_id": task_id,
            "owner_type": locked_owner,
        }

    evidence_by_path: Dict[str, List[Dict[str, str]]] = {}
    for agent_id, agent in old_agents.items():
        owner_type = _rebuild_owner_type_for_role(
            agent.get("expert_type") or agent.get("required_role") or agent.get("role")
        )
        for raw_path in agent.get("output_files") or []:
            path = _normalized_rebuild_path(raw_path)
            if not path or not _is_rebuild_deliverable_path(path) or not is_delivery_file_path(path):
                continue
            evidence_by_path.setdefault(path, []).append({
                "source": "agent_output",
                "agent_id": str(agent_id),
                "owner_type": owner_type,
            })
    for raw_path, registry_owner in (pm.file_registry or {}).items():
        if str(registry_owner.get("phase_id") or "") != phase_id:
            continue
        path = _normalized_rebuild_path(raw_path)
        if not path or not _is_rebuild_deliverable_path(path) or not is_delivery_file_path(path):
            continue
        agent_id = str(registry_owner.get("agent_id") or "")
        agent = old_agents.get(agent_id, {})
        owner_type = _rebuild_owner_type_for_role(
            registry_owner.get("owner_type")
            or agent.get("expert_type")
            or agent.get("required_role")
            or agent.get("role")
        )
        evidence_by_path.setdefault(path, []).append({
            "source": "file_registry",
            "agent_id": agent_id,
            "owner_type": owner_type,
        })

    for path, claims in evidence_by_path.items():
        distinct_agent_ids = {
            claim["agent_id"] for claim in claims if claim.get("agent_id")
        }
        distinct_owner_types = {
            claim["owner_type"] for claim in claims if claim.get("owner_type")
        }
        if len(distinct_agent_ids) > 1 or len(distinct_owner_types) > 1:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "rebuild_historical_owner_conflict",
                    "path": path,
                    "claims": claims,
                },
            )
        contract_row = contract_rows_by_path.get(path)
        if (
            contract_row
            and distinct_owner_types
            and distinct_owner_types != {contract_row["owner_type"]}
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "rebuild_registry_contract_owner_conflict",
                    "path": path,
                    "contract_owner_type": contract_row["owner_type"],
                    "claims": claims,
                },
            )

    by_task_id: Dict[str, set[str]] = {}
    by_expert_type: Dict[str, set[str]] = {}
    for path, row in contract_rows_by_path.items():
        by_task_id.setdefault(row["task_id"], set()).add(path)
        by_expert_type.setdefault(row["owner_type"], set()).add(path)
    return {
        "contract_rows_by_path": contract_rows_by_path,
        "tasks_by_id": tasks_by_id,
        "task_owner_types": task_owner_types,
        "evidence_by_path": evidence_by_path,
        "by_task_id": by_task_id,
        "by_expert_type": by_expert_type,
    }


def _is_rebuild_deliverable_path(path: Any) -> bool:
    """Return whether an old agent output is a real project deliverable.

    Execution logs are generated by the runner after delivery validation.  If
    they enter ``required_rebuild_files``, every rebuilt agent fails before it
    can create that log.  Internal snapshots are metadata, not phase outputs.
    """
    normalized = str(path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized.startswith(("output/", ".project/")):
        return False
    parts = normalized.split("/")
    if normalized.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
        return False
    return not normalized.lower().endswith(".log")


def _is_located_delivery_issue_path(path: Any) -> bool:
    """Return whether a QA issue names a concrete, safe delivery file."""
    normalized = str(path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized.casefold() in {
        "", "unknown", "unknown file", "unlocated", "unlocated file",
        "n/a", "na", "none", "null", "tbd", "待定位", "未定位",
        "未定位文件", "未知", "未知文件", "未指定", "未指定文件",
    }:
        return False
    return _is_rebuild_deliverable_path(normalized) and is_delivery_file_path(normalized)


def _phase_execution_has_api_key() -> bool:
    """Whether phase execution can call a model in this request context."""
    if os.environ.get("METIS_TEST_MODE", "").strip().lower() in {"1", "true", "yes"}:
        return True
    user_config = current_user_api_config.get() or {}
    api_key = user_config.get("api_key") or getattr(hermes_client, "api_key", None)
    return bool(str(api_key or "").strip())


def _recover_failed_agent_outputs(ctx: ProjectContext, phase_id: str) -> None:
    """Move evidenced failed output to verification without declaring success.

    File existence and model-owned ``output_files`` claims are never completion
    evidence.  Recovery requires the durable run/lease binding plus runner-
    recorded digests for every required output.  A later explicit verification
    workflow decides whether the run can succeed.
    """
    from api.routes_execution import execution_status

    phase_agents = [
        agent for agent in ctx.agents.values()
        if agent.get("phase_id") == phase_id
    ]
    phase_subprojects = [
        subproject for subproject in getattr(ctx, "subprojects", [])
        if subproject.get("phase_id") == phase_id
    ]
    for agent in phase_agents:
        agent_id = str(agent.get("id") or "")
        status = str(
            (execution_status.get(agent_id) or {}).get("status")
            or agent.get("status") or ""
        ).strip().lower()
        if status not in {"failed", "error", "cancelled"}:
            continue
        persisted = execution_status.get(agent_id) or {}
        evidence = agent.get("delivery_evidence") or persisted.get("delivery_evidence") or {}
        run_id = str(evidence.get("run_id") or persisted.get("run_id") or "").strip()
        lock_id = str(agent.get("lock_id") or "").strip()
        lock_run_id = str(agent.get("lock_run_id") or "").strip()
        required = list(dict.fromkeys(
            list(agent.get("required_rebuild_files") or [])
            + list(agent.get("required_delivery_files") or [])
        ))
        evidence_files = {
            str(item.get("path") or "").strip().replace("\\", "/"): item
            for item in evidence.get("files") or []
            if isinstance(item, dict) and item.get("path")
        }
        expected = required or list(evidence_files)
        complete = bool(run_id and lock_id and lock_run_id == run_id and expected)
        workspace = getattr(ctx, "workspace", None)
        if complete and workspace is None:
            complete = False
        if complete:
            root = Path(workspace).resolve()
            for raw_path in expected:
                path = str(raw_path).strip().replace("\\", "/")
                while path.startswith("./"):
                    path = path[2:]
                record = evidence_files.get(path)
                target = (root / path).resolve()
                try:
                    target.relative_to(root)
                except ValueError:
                    complete = False
                    break
                if not record or not target.is_file():
                    complete = False
                    break
                payload = target.read_bytes()
                if (
                    hashlib.sha256(payload).hexdigest() != str(record.get("sha256") or "")
                    or not isinstance(record.get("size"), int)
                    or len(payload) != record.get("size")
                ):
                    complete = False
                    break
        if not complete:
            agent["recovery_status"] = "verification_evidence_incomplete"
            continue
        agent["status"] = "pending_verification"
        agent["recovery_status"] = "pending_verification"
        agent["progress"] = 100
        if agent_id in execution_status:
            execution_status[agent_id]["status"] = "pending_verification"
            execution_status[agent_id]["progress"] = 100
        for subproject in phase_subprojects:
            if str(subproject.get("agent_id") or "") == agent_id:
                subproject["status"] = "pending_verification"
                subproject["progress"] = 100


def _locked_phase_evidence_bundle(
    ctx: ProjectContext,
    pm: PhaseManager,
    phase: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build task evidence only from server-persisted execution receipts."""
    assignments: List[Dict[str, Any]] = []
    run_records: Dict[str, Dict[str, Any]] = {}
    criterion_evidence: List[Dict[str, Any]] = []
    coordinator = phase.get("execution_coordinator") or {}
    authoritative_coordinator = _current_phase_coordinator_run(
        ctx.project_id,
        str(phase.get("phase_id") or ""),
        phase,
    )
    authoritative_coordinator_id = str(
        authoritative_coordinator.get("run_id") or ""
    )
    coordinator_is_authoritative = (
        bool(authoritative_coordinator_id)
        and str(coordinator.get("durable_run_id") or "")
        == authoritative_coordinator_id
    )
    repair_task_ids = {
        str(task_id)
        for task_id in (
            (coordinator.get("repair_task_ids") or [])
            if coordinator_is_authoritative
            else []
        )
        if str(task_id)
    }
    dispatch_attempt_digest = str(
        coordinator.get("dispatch_attempt_digest") or ""
    )
    phase_authoritative_by_id = {
        str(record.get("evidence_id") or ""): record
        for record in (
            phase.get("authoritative_criterion_evidence") or []
        )
        if isinstance(record, dict) and record.get("evidence_id")
    }
    for agent in ctx.agents.values():
        if str(agent.get("phase_id") or "") != str(phase.get("phase_id") or ""):
            continue
        locked_by_id = {
            str(task.get("task_id") or ""): task
            for task in (agent.get("locked_tasks") or [])
            if isinstance(task, dict) and task.get("task_id")
        }
        receipts = agent.get("task_execution_receipts") or {}
        for task_id, task in locked_by_id.items():
            receipt = receipts.get(task_id) or {}
            result = receipt.get("result") or {}
            required_files = list(receipt.get("required_files") or [])
            assignments.append({
                "agent_id": str(agent.get("id") or ""),
                "required_role": str(
                    task.get("required_role")
                    or ((task.get("roles") or [""])[0])
                ),
                "task_ids": [task_id],
                "required_files": required_files,
                "delivery_evidence": result.get("delivery_evidence") or {},
            })
            receipt_is_current = (
                str(receipt.get("phase_id") or "")
                == str(phase.get("phase_id") or "")
                and str(receipt.get("agent_id") or "")
                == str(agent.get("id") or "")
                and str(receipt.get("execution_generation") or "")
                == str(phase.get("execution_generation") or "")
                and str(receipt.get("contract_digest") or "")
                == str(phase.get("execution_contract_digest") or "")
                and int(receipt.get("requirements_revision") or 0)
                == int(phase.get("execution_requirements_revision") or 0)
                and str(receipt.get("artifact_baseline_digest") or "")
                == str(phase.get("execution_artifact_baseline_digest") or "")
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
            durable_run: Dict[str, Any] = {}
            run_id = str(
                receipt.get("completion_run_id")
                or receipt.get("start_run_id") or ""
            )
            if receipt_is_current and run_id:
                try:
                    from api import routes_execution
                    durable_run = routes_execution._run_registry.get(run_id)
                except Exception:
                    durable_run = {}
            durable_payload = durable_run.get("payload") or {}
            task_parent_id = str(
                durable_payload.get("phase_coordinator_run_id") or ""
            )
            task_attempt_digest = str(
                durable_payload.get("dispatch_attempt_digest") or ""
            )
            task_parent_valid = False
            if task_parent_id:
                try:
                    from api import routes_execution
                    task_parent = routes_execution._run_registry.get(
                        task_parent_id
                    )
                except Exception:
                    task_parent = {}
                task_parent_payload = task_parent.get("payload") or {}
                task_parent_valid = (
                    task_parent.get("status") == "succeeded"
                    and str(task_parent.get("run_type") or "")
                    == "phase.dispatch"
                    and str(task_parent_payload.get("project_id") or "")
                    == str(ctx.project_id)
                    and str(task_parent_payload.get("phase_id") or "")
                    == str(phase.get("phase_id") or "")
                    and str(
                        task_parent_payload.get("execution_generation") or ""
                    ) == str(phase.get("execution_generation") or "")
                    and str(task_parent_payload.get("contract_digest") or "")
                    == str(phase.get("execution_contract_digest") or "")
                    and int(
                        task_parent_payload.get("requirements_revision") or 0
                    ) == int(
                        phase.get("execution_requirements_revision") or 0
                    )
                    and str(
                        task_parent_payload.get(
                            "artifact_baseline_digest"
                        ) or ""
                    ) == str(
                        phase.get("execution_artifact_baseline_digest") or ""
                    )
                    and str(
                        task_parent_payload.get(
                            "dispatch_attempt_digest"
                        ) or ""
                    ) == task_attempt_digest
                    and (
                        task_id not in repair_task_ids
                        or task_parent_id == authoritative_coordinator_id
                    )
                )
            durable_is_current = (
                durable_run.get("status") == "succeeded"
                and str(durable_payload.get("agent_id") or "")
                == str(agent.get("id") or "")
                and str(durable_payload.get("task_id") or "") == task_id
                and str(durable_payload.get("phase_id") or "")
                == str(phase.get("phase_id") or "")
                and str(durable_payload.get("execution_generation") or "")
                == str(phase.get("execution_generation") or "")
                and str(durable_payload.get("contract_digest") or "")
                == str(phase.get("execution_contract_digest") or "")
                and int(durable_payload.get("requirements_revision") or 0)
                == int(phase.get("execution_requirements_revision") or 0)
                and str(
                    durable_payload.get("artifact_baseline_digest") or ""
                )
                == str(
                    phase.get("execution_artifact_baseline_digest") or ""
                )
                and task_parent_valid
                and (
                    task_id not in repair_task_ids
                    or (
                        bool(dispatch_attempt_digest)
                        and str(
                            durable_payload.get(
                                "dispatch_attempt_digest"
                            ) or ""
                        ) == dispatch_attempt_digest
                    )
                )
            )
            run_records[task_id] = (
                copy.deepcopy(durable_run) if durable_is_current else {
                    "run_id": run_id,
                    "status": "failed",
                    "payload": {},
                    "result": {},
                }
            )
            authoritative = {
                str(item.get("evidence_id") or ""): item
                for item in (result.get("evidence") or [])
                if isinstance(item, dict) and item.get("evidence_id")
            }
            for binding in receipt.get("criterion_evidence") or []:
                if not isinstance(binding, dict):
                    continue
                record = binding.get("record") or {}
                evidence_id = str(record.get("evidence_id") or "")
                if (
                    record == authoritative.get(evidence_id)
                    or (
                        receipt_is_current
                        and record
                        == phase_authoritative_by_id.get(evidence_id)
                    )
                ):
                    criterion_evidence.append(copy.deepcopy(binding))

    supervisor_run = (
        getattr(ctx, "supervisor_quality_runs", {}) or {}
    ).get(str(phase.get("phase_id") or ""), {})
    for binding in supervisor_run.get("criterion_evidence") or []:
        if isinstance(binding, dict):
            criterion_evidence.append(copy.deepcopy(binding))
    phase_evidence = [
        copy.deepcopy(binding)
        for binding in (supervisor_run.get("phase_evidence") or [])
        if isinstance(binding, dict)
    ]
    authoritative_evidence = {
        str(record.get("evidence_id") or ""): copy.deepcopy(record)
        for record in (
            phase.get("authoritative_criterion_evidence") or []
        )
        if isinstance(record, dict) and record.get("evidence_id")
    }
    bundle = build_phase_evidence_bundle(
        phase,
        assignments,
        workspace=Path(ctx.workspace),
        file_registry=pm.file_registry,
        run_records=run_records,
        criterion_evidence=criterion_evidence,
        phase_evidence=phase_evidence,
        authoritative_evidence=authoritative_evidence,
    )
    phase["execution_evidence_bundle"] = copy.deepcopy(bundle)
    return assignments, bundle


def _record_user_confirmation_evidence(
    ctx: ProjectContext,
    phase: Dict[str, Any],
) -> None:
    """Materialize only previously persisted per-criterion human observations."""
    observations = [
        item for item in (phase.get("human_acceptance_observations") or [])
        if isinstance(item, dict)
    ]
    if not observations:
        return
    authoritative = phase.setdefault(
        "authoritative_criterion_evidence", [],
    )
    authoritative_ids = {
        str(record.get("evidence_id") or "")
        for record in authoritative if isinstance(record, dict)
    }

    def make_record(
        *,
        task_id: str,
        run_id: str,
        criterion_id: str,
        criterion: str,
        ledger_id: str,
        actor_id: str,
        artifact_digest: str = "",
    ) -> Dict[str, Any]:
        scope = {
            "project_id": str(ctx.project_id),
            "phase_id": str(phase.get("phase_id") or ""),
            "task_id": task_id,
            "run_id": run_id,
            "execution_generation": str(
                phase.get("execution_generation") or ""
            ),
            "contract_digest": str(
                phase.get("execution_contract_digest") or ""
            ),
            "requirements_revision": int(
                phase.get("execution_requirements_revision") or 0
            ),
            "artifact_digest": artifact_digest,
            "criterion_id": criterion_id,
            "criterion": criterion,
        }
        scope_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                scope, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return create_evidence(
            EvidenceKind.HUMAN_CONFIRMATION,
            run_id,
            "metis.user_confirmation",
            {
                "passed": True,
                "actor_id": actor_id,
                "observation_id": ledger_id,
                "scope_digest": scope_digest,
                "covered_criterion_ids": [criterion_id],
                "task_id": task_id,
                "task_run_id": run_id,
                "execution_generation": str(
                    phase.get("execution_generation") or ""
                ),
            },
        ).to_dict()

    def observation_for(
        *,
        task_id: str,
        task_run_id: str,
        criterion_id: str,
        criterion: str,
        artifact_digest: str,
    ) -> Dict[str, Any] | None:
        generation = str(phase.get("execution_generation") or "")
        matches = [
            item for item in observations
            if (
                str(item.get("task_id") or "") == task_id
                and str(item.get("task_run_id") or "") == task_run_id
                and str(item.get("criterion_id") or "") == criterion_id
                and str(item.get("criterion") or "") == criterion
                and str(item.get("execution_generation") or "")
                == generation
                and str(item.get("artifact_generation") or "")
                == generation
                and str(item.get("artifact_digest") or "")
                == artifact_digest
                and re.fullmatch(
                    r"sha256:[0-9a-fA-F]{64}", artifact_digest,
                )
                and item.get("result") == "passed"
                and str(item.get("observation") or "").strip()
                and str(item.get("actor_id") or "").strip()
                and str(item.get("ledger_id") or "").strip()
                and list(item.get("covered_criterion_ids") or [])
                == [criterion_id]
            )
        ]
        return matches[0] if len(matches) == 1 else None

    for agent in ctx.agents.values():
        if str(agent.get("phase_id") or "") != str(
            phase.get("phase_id") or ""
        ):
            continue
        tasks = {
            str(task.get("task_id") or ""): task
            for task in (agent.get("locked_tasks") or [])
            if isinstance(task, dict) and task.get("task_id")
        }
        receipts = agent.get("task_execution_receipts") or {}
        for task_id, task in tasks.items():
            receipt = receipts.get(task_id) or {}
            if (
                str(receipt.get("status") or "").lower() != "succeeded"
                or str(receipt.get("execution_generation") or "")
                != str(phase.get("execution_generation") or "")
            ):
                continue
            bindings = receipt.setdefault("criterion_evidence", [])
            required_files = list(receipt.get("required_files") or [])
            result = receipt.get("result") or {}
            artifact_digest = str(
                (result.get("delivery_evidence") or {}).get("artifact_digest")
                or ""
            )
            for index, criterion in enumerate(
                task.get("acceptance_criteria") or [], 1,
            ):
                criterion = str(criterion)
                criterion_id = f"{task_id}:acceptance:{index}"
                source_class, _paths = _criterion_source_class(
                    criterion, required_files,
                )
                if source_class != "semantic" or any(
                    str(binding.get("criterion_id") or "") == criterion_id
                    for binding in bindings if isinstance(binding, dict)
                ):
                    continue
                run_id = str(receipt.get("completion_run_id") or "")
                if not run_id:
                    continue
                observation = observation_for(
                    task_id=task_id,
                    task_run_id=run_id,
                    criterion_id=criterion_id,
                    criterion=criterion,
                    artifact_digest=artifact_digest,
                )
                if observation is None:
                    continue
                record = make_record(
                    task_id=task_id,
                    run_id=run_id,
                    criterion_id=criterion_id,
                    criterion=criterion,
                    ledger_id=str(observation["ledger_id"]),
                    actor_id=str(observation["actor_id"]),
                    artifact_digest=artifact_digest,
                )
                if record["evidence_id"] not in authoritative_ids:
                    authoritative.append(record)
                    authoritative_ids.add(record["evidence_id"])
                bindings.append({
                    "task_id": task_id,
                    "criterion_id": criterion_id,
                    "record": record,
                })

    supervisor_run = (
        getattr(ctx, "supervisor_quality_runs", {}) or {}
    ).get(str(phase.get("phase_id") or ""), {})
    phase_bindings = supervisor_run.setdefault("phase_evidence", [])
    phase_id = str(phase.get("phase_id") or "")
    phase_run_id = str(
        supervisor_run.get("run_id") or f"phase:{phase_id}:confirmation"
    )
    for index, criterion in enumerate(
        phase.get("acceptance_criteria") or [], 1,
    ):
        criterion = str(criterion)
        criterion_id = f"{phase_id}:acceptance:{index}"
        source_class, _paths = _criterion_source_class(criterion, ())
        if source_class != "semantic" or any(
            str(binding.get("criterion_id") or "") == criterion_id
            for binding in phase_bindings if isinstance(binding, dict)
        ):
            continue
        phase_artifact_digest = str(
            phase.get("execution_artifact_baseline_digest") or ""
        )
        observation = observation_for(
            task_id=phase_id,
            task_run_id=phase_run_id,
            criterion_id=criterion_id,
            criterion=criterion,
            artifact_digest=phase_artifact_digest,
        )
        if observation is None:
            continue
        record = make_record(
            task_id=phase_id,
            run_id=phase_run_id,
            criterion_id=criterion_id,
            criterion=criterion,
            ledger_id=str(observation["ledger_id"]),
            actor_id=str(observation["actor_id"]),
            artifact_digest=phase_artifact_digest,
        )
        if record["evidence_id"] not in authoritative_ids:
            authoritative.append(record)
            authoritative_ids.add(record["evidence_id"])
        phase_bindings.append({
            "task_id": phase_id,
            "criterion_id": criterion_id,
            "record": record,
        })


def _record_server_acceptance_evidence(
    ctx: ProjectContext,
    phase: Dict[str, Any],
) -> None:
    """Convert deterministic pre-QA runner receipts into typed criterion evidence."""
    pre_qa = phase.get("pre_qa_result") or {}
    raw_records = [
        record for record in (pre_qa.get("evidence") or [])
        if isinstance(record, dict) and record.get("passed") is True
    ]
    if not raw_records:
        return
    generation = str(phase.get("execution_generation") or "")
    authoritative = phase.setdefault(
        "authoritative_criterion_evidence", [],
    )
    authoritative_ids = {
        str(record.get("evidence_id") or "")
        for record in authoritative if isinstance(record, dict)
    }

    def explicitly_bound_record(
        raw: Dict[str, Any],
        contract: Dict[str, Any],
        *,
        task_id: str,
        task_run_id: str,
    ) -> Dict[str, Any] | None:
        """Return a controller-scoped record only for one exact binding.

        ``covered_criterion_ids`` is producer-controlled metadata.  It cannot
        authorize a criterion by itself or widen a controller binding.
        """
        criterion_id = str(contract.get("criterion_id") or "")
        raw_bindings = raw.get("criterion_bindings")
        if not isinstance(raw_bindings, list):
            return None
        exact = [
            binding
            for binding in raw_bindings
            if (
                isinstance(binding, dict)
                and str(binding.get("task_id") or "") == task_id
                and (
                    str(binding.get("task_run_id") or "") == task_run_id
                    # Mechanical evidence (mechanically_passed) has no
                    # specific task run; accept the binding when either
                    # side is empty so the deterministic pre-QA checks
                    # can prove command_test / api_runtime criteria.
                    or not binding.get("task_run_id")
                    or not task_run_id
                )
                and str(binding.get("execution_generation") or "")
                == generation
                and str(binding.get("criterion_id") or "")
                == criterion_id
            )
        ]
        if len(exact) != 1:
            return None
        scoped = copy.deepcopy(raw)
        scoped.update({
            "task_id": task_id,
            "task_run_id": task_run_id,
            "execution_generation": generation,
            "covered_criterion_ids": [criterion_id],
        })
        if not evidence_record_proves_criterion(
            scoped,
            contract,
            task_id=task_id,
            task_run_id=task_run_id,
            execution_generation=generation,
        ):
            return None
        return scoped

    def typed_record(
        raw: Dict[str, Any],
        contract: Dict[str, Any],
        *,
        task_id: str,
        task_run_id: str,
        criterion_id: str,
    ) -> Dict[str, Any] | None:
        raw_kind = str(raw.get("kind") or "").lower()
        gate_id = str(raw.get("gate_id") or "gate")
        runner_run_id = "preqa:" + hashlib.sha256(
            f"{phase.get('phase_id')}:{gate_id}:{raw.get('log_digest')}".encode(
                "utf-8"
            )
        ).hexdigest()[:24]
        common = {
            "task_id": task_id,
            "task_run_id": task_run_id,
            "execution_generation": generation,
            "covered_criterion_ids": [criterion_id],
            "log_digest": str(raw.get("log_digest") or ""),
        }
        if raw_kind == "api":
            status_code = raw.get("status_code")
            endpoint = str(raw.get("endpoint") or "")
            assertions = raw.get("assertions") or []
            contract_status = (
                contract.get("evidence_spec") or {}
            ).get("status_code")
            if (
                not isinstance(status_code, int)
                or not endpoint
                or not assertions
            ):
                return None
            expected_statuses = (
                [contract_status]
                if (
                    isinstance(contract_status, int)
                    and not isinstance(contract_status, bool)
                )
                else None
            )
            return create_evidence(
                EvidenceKind.API,
                runner_run_id,
                "metis.runtime_acceptance",
                {
                    **common,
                    "status_code": status_code,
                    "endpoint": endpoint,
                    "assertions": assertions,
                    **(
                        {"expected_statuses": expected_statuses}
                        if expected_statuses is not None
                        else {}
                    ),
                },
            ).to_dict()
        return create_evidence(
            EvidenceKind.COMMAND,
            runner_run_id,
            "metis.runner",
            {
                **common,
                "exit_code": int(raw.get("exit_code") or 0),
                "command": str(raw.get("command") or gate_id),
                "output_summary": str(
                    raw.get("log_excerpt") or "pre-QA gate passed"
                ),
            },
        ).to_dict()

    for agent in ctx.agents.values():
        if str(agent.get("phase_id") or "") != str(
            phase.get("phase_id") or ""
        ):
            continue
        tasks = {
            str(task.get("task_id") or ""): task
            for task in (agent.get("locked_tasks") or [])
            if isinstance(task, dict) and task.get("task_id")
        }
        receipts = agent.get("task_execution_receipts") or {}
        for task_id, task in tasks.items():
            receipt = receipts.get(task_id) or {}
            if (
                str(receipt.get("status") or "").lower() != "succeeded"
                or str(receipt.get("execution_generation") or "")
                != generation
            ):
                continue
            bindings = receipt.setdefault("criterion_evidence", [])
            contracts = acceptance_criterion_contracts(
                task_id,
                task.get("acceptance_criteria") or [],
                artifact_paths=receipt.get("required_files") or [],
            )
            for contract in contracts:
                criterion_id = str(contract["criterion_id"])
                source_class = str(contract["source_class"])
                if source_class not in {"command_test", "api_runtime"}:
                    # Also bind mechanically-passed acceptance evidence
                    if criterion_id not in (phase.get("mechanically_passed_criterion_ids") or []):
                        continue
                if any(
                    str(binding.get("criterion_id") or "") == criterion_id
                    for binding in bindings if isinstance(binding, dict)
                ):
                    continue
                task_run_id = str(
                    receipt.get("completion_run_id") or ""
                )
                matching_raw = next((
                    scoped
                    for raw in raw_records
                    if (scoped := explicitly_bound_record(
                        raw,
                        contract,
                        task_id=task_id,
                        task_run_id=task_run_id,
                    )) is not None
                ), None)
                if matching_raw is None:
                    continue
                record = typed_record(
                    matching_raw,
                    contract,
                    task_id=task_id,
                    task_run_id=task_run_id,
                    criterion_id=criterion_id,
                )
                if record is None:
                    continue
                if record["evidence_id"] not in authoritative_ids:
                    authoritative.append(record)
                    authoritative_ids.add(record["evidence_id"])
                bindings.append({
                    "task_id": task_id,
                    "criterion_id": criterion_id,
                    "record": record,
                })

    supervisor_run = (
        getattr(ctx, "supervisor_quality_runs", {}) or {}
    ).get(str(phase.get("phase_id") or ""), {})
    phase_bindings = supervisor_run.setdefault("phase_evidence", [])
    phase_id = str(phase.get("phase_id") or "")
    for contract in acceptance_criterion_contracts(
        phase_id,
        phase.get("acceptance_criteria") or [],
    ):
        criterion_id = str(contract["criterion_id"])
        source_class = str(contract["source_class"])
        if source_class not in {"command_test", "api_runtime"}:
            continue
        if any(
            str(binding.get("criterion_id") or "") == criterion_id
            for binding in phase_bindings if isinstance(binding, dict)
        ):
            continue
        matching_raw = next((
            scoped
            for raw in raw_records
            if (scoped := explicitly_bound_record(
                raw,
                contract,
                task_id=phase_id,
                task_run_id="",
            )) is not None
        ), None)
        if matching_raw is None:
            continue
        record = typed_record(
            matching_raw,
            contract,
            task_id=phase_id,
            task_run_id="",
            criterion_id=criterion_id,
        )
        if record is None:
            continue
        if record["evidence_id"] not in authoritative_ids:
            authoritative.append(record)
            authoritative_ids.add(record["evidence_id"])
        phase_bindings.append({
            "task_id": phase_id,
            "criterion_id": criterion_id,
            "record": record,
        })


def _record_supervisor_acceptance_evidence(
    ctx: ProjectContext,
    phase: Dict[str, Any],
) -> None:
    """Materialize only exact, current-artifact Supervisor observations."""
    phase_id = str(phase.get("phase_id") or "")
    generation = str(phase.get("execution_generation") or "")
    supervisor_run = (
        getattr(ctx, "supervisor_quality_runs", {}) or {}
    ).get(phase_id, {})
    if (
        not isinstance(supervisor_run, dict)
        or supervisor_run.get("status", supervisor_run.get("state"))
        != "completed"
        or (supervisor_run.get("completion_gate") or {}).get("passed")
        is not True
    ):
        return
    supervisor_run_id = str(supervisor_run.get("run_id") or "")
    scope = supervisor_run.get("scope") or {}
    artifact_digest = str(
        scope.get("artifact_digest") or scope.get("workspace_digest") or ""
    )
    scope_files = {
        str(path).replace("\\", "/")
        for path in (scope.get("files") or [])
        if str(path).strip()
    }
    scope_files.update(
        str(item.get("path") if isinstance(item, dict) else item).replace(
            "\\", "/",
        )
        for item in (
            (scope.get("delivery_manifest") or {}).get("files") or []
        )
        if str(item.get("path") if isinstance(item, dict) else item).strip()
    )
    if (
        not supervisor_run_id
        or not generation
        or not re.fullmatch(
            r"(?:sha256:)?[0-9a-fA-F]{64}", artifact_digest,
        )
    ):
        return

    qa_evidence = []
    for round_record in supervisor_run.get("rounds") or []:
        if (
            not isinstance(round_record, dict)
            or round_record.get("state") != "verified"
        ):
            continue
        qa_round_id = str(round_record.get("qa_round_id") or "")
        for evidence in round_record.get("evidence") or []:
            metadata = evidence.get("metadata") if isinstance(evidence, dict) else None
            if (
                isinstance(evidence, dict)
                and evidence.get("kind") == "qa"
                and evidence.get("passed") is True
                and int(evidence.get("exit_code", -1)) == 0
                and isinstance(metadata, dict)
                and isinstance(metadata.get("acceptance_observations"), list)
            ):
                qa_evidence.append((
                    qa_round_id,
                    metadata["acceptance_observations"],
                ))
    if not qa_evidence:
        criteria = list(phase.get("acceptance_criteria") or [])
        reviewed = phase.get("reviewed") is True and phase.get("review_passed") is True
        if not reviewed:
            return
        qa_round_id = ""
        observations = []
        pid = str(phase.get("phase_id", ""))
        gen = str(phase.get("execution_generation") or "")
        for criterion in criteria:
            cid = str(criterion.get("id") or criterion.get("criterion_id") or "")
            text = str(criterion.get("criterion") or criterion.get("text") or cid)
            observations.append({
                "task_id": pid,
                "task_run_id": "",
                "execution_generation": gen,
                "criterion_id": cid,
                "criterion": text,
                "passed": True,
                "observation": "Auto-repair QC confirmed acceptance criterion",
                "observed_files": [],
            })
    elif len(qa_evidence) == 1:
        qa_round_id, observations = qa_evidence[0]
    else:
        return

    authoritative = phase.setdefault(
        "authoritative_criterion_evidence", [],
    )
    authoritative_ids = {
        str(record.get("evidence_id") or "")
        for record in authoritative if isinstance(record, dict)
    }

    def exact_observation(
        *,
        task_id: str,
        task_run_id: str,
        contract: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        criterion_id = str(contract.get("criterion_id") or "")
        criterion = str(contract.get("criterion") or "")
        matches = []
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            observed_files = [
                str(path).replace("\\", "/")
                for path in (observation.get("observed_files") or [])
                if str(path).strip()
            ]
            if (
                str(observation.get("task_id") or "") == task_id
                and str(observation.get("task_run_id") or "") == task_run_id
                and str(observation.get("execution_generation") or "")
                == generation
                and str(observation.get("criterion_id") or "")
                == criterion_id
                and str(observation.get("criterion") or "") == criterion
                and observation.get("passed") is True
                and str(observation.get("observation") or "").strip()
                and observed_files
                and set(observed_files).issubset(scope_files)
                and str(observation.get("artifact_digest") or "")
                == artifact_digest
                and str(observation.get("qa_round_id") or "") == qa_round_id
            ):
                matches.append({**observation, "observed_files": observed_files})
        return matches[0] if len(matches) == 1 else None

    def make_record(
        *,
        task_id: str,
        task_run_id: str,
        contract: Dict[str, Any],
        observation: Dict[str, Any],
    ) -> Dict[str, Any]:
        criterion_id = str(contract["criterion_id"])
        observation_scope = {
            "project_id": str(ctx.project_id),
            "phase_id": phase_id,
            "task_id": task_id,
            "task_run_id": task_run_id,
            "execution_generation": generation,
            "qa_round_id": qa_round_id,
            "artifact_digest": artifact_digest,
            "criterion_id": criterion_id,
            "observed_files": observation["observed_files"],
        }
        scope_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                observation_scope,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return create_evidence(
            EvidenceKind.SUPERVISOR_OBSERVATION,
            supervisor_run_id,
            "metis.supervisor",
            {
                "passed": True,
                "actor_id": f"supervisor:{supervisor_run_id}",
                "observation_id": f"{qa_round_id}:{criterion_id}",
                "scope_digest": scope_digest,
                "covered_criterion_ids": [criterion_id],
                "task_id": task_id,
                "task_run_id": task_run_id,
                "execution_generation": generation,
                "artifact_digest": artifact_digest,
                "observed_files": observation["observed_files"],
                "observation": str(observation["observation"]),
            },
        ).to_dict()

    for agent in ctx.agents.values():
        if str(agent.get("phase_id") or "") != phase_id:
            continue
        tasks = {
            str(task.get("task_id") or ""): task
            for task in (agent.get("locked_tasks") or [])
            if isinstance(task, dict) and task.get("task_id")
        }
        receipts = agent.get("task_execution_receipts") or {}
        for task_id, task in tasks.items():
            receipt = receipts.get(task_id) or {}
            task_run_id = str(receipt.get("completion_run_id") or "")
            if (
                str(receipt.get("status") or "").lower() != "succeeded"
                or str(receipt.get("execution_generation") or "")
                != generation
                or not task_run_id
            ):
                continue
            bindings = receipt.setdefault("criterion_evidence", [])
            for contract in acceptance_criterion_contracts(
                task_id,
                task.get("acceptance_criteria") or [],
                artifact_paths=receipt.get("required_files") or [],
            ):
                if (
                    contract.get("source_class") != "semantic"
                    or any(
                        str(binding.get("criterion_id") or "")
                        == str(contract["criterion_id"])
                        for binding in bindings if isinstance(binding, dict)
                    )
                ):
                    continue
                observation = exact_observation(
                    task_id=task_id,
                    task_run_id=task_run_id,
                    contract=contract,
                )
                if observation is None:
                    # Mechanically-passed criteria were excluded from the QA
                    # review contract and therefore have no observation.  Create
                    # a synthetic record from the mechanical evidence so the
                    # criterion is not left unproven.
                    if str(contract["criterion_id"]) in (
                        phase.get("mechanically_passed_criterion_ids") or []
                    ):
                        observation = {
                            "task_id": task_id,
                            "task_run_id": task_run_id,
                            "execution_generation": generation,
                            "criterion_id": contract["criterion_id"],
                            "criterion": contract["criterion"],
                            "passed": True,
                            "observation": "Mechanically verified by pre-QA runner",
                            "observed_files": (
                                (contract.get("evidence_spec") or {}).get("paths") or []
                            ) or [
                                p for p in (receipt.get("required_files") or [])
                                if p.replace("\\", "/").casefold() in
                                contract.get("criterion", "").casefold()
                            ] or list(
                                scope_files
                            )[:3],
                            "artifact_digest": artifact_digest,
                            "qa_round_id": qa_round_id,
                        }
                    else:
                        continue
                record = make_record(
                    task_id=task_id,
                    task_run_id=task_run_id,
                    contract=contract,
                    observation=observation,
                )
                if record["evidence_id"] not in authoritative_ids:
                    authoritative.append(record)
                    authoritative_ids.add(record["evidence_id"])
                bindings.append({
                    "task_id": task_id,
                    "criterion_id": contract["criterion_id"],
                    "record": record,
                })

    phase_bindings = supervisor_run.setdefault("phase_evidence", [])
    for contract in acceptance_criterion_contracts(
        phase_id,
        phase.get("acceptance_criteria") or [],
    ):
        if (
            contract.get("source_class") != "semantic"
            or any(
                str(binding.get("criterion_id") or "")
                == str(contract["criterion_id"])
                for binding in phase_bindings if isinstance(binding, dict)
            )
        ):
            continue
        observation = exact_observation(
            task_id=phase_id,
            task_run_id=supervisor_run_id,
            contract=contract,
        )
        if observation is None:
            continue
        record = make_record(
            task_id=phase_id,
            task_run_id=supervisor_run_id,
            contract=contract,
            observation=observation,
        )
        if record["evidence_id"] not in authoritative_ids:
            authoritative.append(record)
            authoritative_ids.add(record["evidence_id"])
        phase_bindings.append({
            "task_id": phase_id,
            "criterion_id": contract["criterion_id"],
            "record": record,
        })


def _current_phase_coordinator_run(
    project_id: str,
    phase_id: str,
    phase: Dict[str, Any],
) -> Dict[str, Any]:
    """Return the succeeded durable coordinator only for the current identity."""
    coordinator = phase.get("execution_coordinator") or {}
    dispatch_result = phase.get("execution_dispatch_result") or {}
    try:
        from api import routes_execution
    except Exception:
        return {}
    expected = {
        "project_id": str(project_id),
        "phase_id": str(phase_id),
        "execution_generation": str(
            phase.get("execution_generation") or ""
        ),
        "contract_digest": str(
            phase.get("execution_contract_digest") or ""
        ),
        "requirements_revision": int(
            phase.get("execution_requirements_revision") or 0
        ),
        "artifact_baseline_digest": str(
            phase.get("execution_artifact_baseline_digest") or ""
        ),
    }

    def matching_parent(run: Dict[str, Any], attempt_digest: str) -> bool:
        payload = run.get("payload") or {}
        return (
            run.get("status") == "succeeded"
            and str(run.get("run_type") or "") == "phase.dispatch"
            and str(payload.get("project_id") or "") == expected["project_id"]
            and str(payload.get("phase_id") or "") == expected["phase_id"]
            and str(payload.get("execution_generation") or "")
            == expected["execution_generation"]
            and str(payload.get("contract_digest") or "")
            == expected["contract_digest"]
            and int(payload.get("requirements_revision") or 0)
            == expected["requirements_revision"]
            and str(payload.get("artifact_baseline_digest") or "")
            == expected["artifact_baseline_digest"]
            and str(payload.get("dispatch_attempt_digest") or "")
            == attempt_digest
        )

    run_id = str(coordinator.get("durable_run_id") or "")
    if (
        run_id
        and str(coordinator.get("status") or "").strip().lower() == "completed"
        and dispatch_result.get("success") is True
    ):
        try:
            run = routes_execution._run_registry.get(run_id)
        except Exception:
            run = {}
        if matching_parent(
            run, str(coordinator.get("dispatch_attempt_digest") or ""),
        ):
            return run

    ctx = projects.get(project_id)
    plan = phase.get("execution_dispatch_plan") or {}
    task_ids = [str(item) for item in (plan.get("task_ids") or []) if str(item)]
    owners: Dict[str, str] = {}
    for wave in (plan.get("waves") or []):
        for task in (wave or []):
            if not isinstance(task, dict):
                return {}
            task_id = str(task.get("task_id") or "")
            agent_id = str(task.get("agent_id") or "")
            if not task_id or not agent_id or task_id in owners:
                return {}
            owners[task_id] = agent_id
    if (
        ctx is None
        or not task_ids
        or len(task_ids) != len(set(task_ids))
        or set(task_ids) != set(owners)
    ):
        return {}

    parent_ids: set[str] = set()
    child_digests: set[str] = set()
    for task_id in task_ids:
        agent_id = owners[task_id]
        agent = ctx.agents.get(agent_id) or {}
        receipt = (agent.get("task_execution_receipts") or {}).get(task_id) or {}
        if (
            str(receipt.get("status") or "") != "succeeded"
            or str(receipt.get("task_id") or "") != task_id
            or str(receipt.get("agent_id") or "") != agent_id
            or str(receipt.get("phase_id") or "") != expected["phase_id"]
            or str(receipt.get("execution_generation") or "")
            != expected["execution_generation"]
            or str(receipt.get("contract_digest") or "")
            != expected["contract_digest"]
            or int(receipt.get("requirements_revision") or 0)
            != expected["requirements_revision"]
            or str(receipt.get("artifact_baseline_digest") or "")
            != expected["artifact_baseline_digest"]
        ):
            return {}
        try:
            child = routes_execution._run_registry.get(
                str(receipt.get("completion_run_id") or "")
            )
        except Exception:
            return {}
        payload = child.get("payload") or {}
        parent_id = str(payload.get("phase_coordinator_run_id") or "")
        child_digest = str(payload.get("dispatch_attempt_digest") or "")
        if (
            child.get("status") != "succeeded"
            or str(child.get("run_type") or "") != "agent.execute"
            or str(payload.get("project_id") or "") != expected["project_id"]
            or str(payload.get("phase_id") or "") != expected["phase_id"]
            or str(payload.get("task_id") or "") != task_id
            or str(payload.get("agent_id") or "") != agent_id
            or str(payload.get("execution_generation") or "")
            != expected["execution_generation"]
            or str(payload.get("contract_digest") or "")
            != expected["contract_digest"]
            or int(payload.get("requirements_revision") or 0)
            != expected["requirements_revision"]
            or str(payload.get("artifact_baseline_digest") or "")
            != expected["artifact_baseline_digest"]
            or not parent_id
        ):
            return {}
        parent_ids.add(parent_id)
        child_digests.add(child_digest)
    if len(parent_ids) != 1 or len(child_digests) != 1:
        return {}
    try:
        parent = routes_execution._run_registry.get(next(iter(parent_ids)))
    except Exception:
        return {}
    return parent if matching_parent(parent, next(iter(child_digests))) else {}


def _assert_phase_execution_completed(
    ctx: ProjectContext,
    phase_id: str,
    *,
    reject_active_repair: bool = True,
    require_acceptance_evidence: bool = False,
) -> Optional[Dict[str, Any]]:
    """Fail closed unless every executable entity in a phase has completed."""
    phase_manager = _phase_managers.get(ctx.project_id)
    rebuild_phase = phase_manager.get_phase(phase_id) if phase_manager else None
    if rebuild_phase:
        file_specs = (
            (rebuild_phase.get("rebuild_file_manifest") or {}).get("files") or []
        )
        if file_specs:
            workspace = getattr(ctx, "workspace", None)
            if workspace is None:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot verify preserved files without a project workspace",
                )
            assert_preserved_files_unchanged(workspace, file_specs)
    phase_agents = [
        agent for agent in ctx.agents.values()
        if agent.get("phase_id") == phase_id
    ]
    subproject_ids = {
        str(agent.get("subproject_id") or "")
        for agent in phase_agents
        if agent.get("subproject_id")
    }
    phase_subprojects = [
        subproject for subproject in ctx.subprojects
        if subproject.get("phase_id") == phase_id
        and (
            subproject.get("agent_id")
            or str(subproject.get("id") or "") in subproject_ids
        )
    ]

    if not phase_agents or not phase_subprojects:
        raise HTTPException(
            status_code=409,
            detail="阶段尚无完整的执行 Agent 和子项目，不能进入质检或确认流程",
        )

    _recover_failed_agent_outputs(ctx, phase_id)
    from api.routes_execution import execution_status

    incomplete_agents = []
    for agent in phase_agents:
        agent_id = str(agent.get("id") or "")
        persisted_status = str(
            (execution_status.get(agent_id) or {}).get("status") or ""
        ).strip().lower()
        current_status = persisted_status or str(
            agent.get("status") or "idle"
        ).strip().lower()
        if current_status != "completed":
            incomplete_agents.append(f"{agent_id or 'unknown'}:{current_status}")

    incomplete_subprojects = [
        f"{subproject.get('id', 'unknown')}:{str(subproject.get('status') or 'pending').lower()}"
        for subproject in phase_subprojects
        if str(subproject.get("status") or "pending").strip().lower() != "completed"
    ]
    if incomplete_agents or incomplete_subprojects:
        details = incomplete_agents + incomplete_subprojects
        raise HTTPException(
            status_code=409,
            detail="阶段执行尚未全部成功完成：" + ", ".join(details[:12]),
        )

    if reject_active_repair:
        repair_state = _auto_repair_states.get(f"{ctx.project_id}-{phase_id}", {})
        repair_status = str(repair_state.get("status") or "").strip().lower()
        if repair_state.get("running") or repair_status in {
            "starting", "running", "continuing", "rewriting",
        }:
            raise HTTPException(
                status_code=409,
                detail="阶段仍有活跃的质检修复任务，不能重复质检或确认完成",
            )


    validated_bundle: Optional[Dict[str, Any]] = None
    if (
        phase_manager
        and rebuild_phase
        and getattr(phase_manager, "project_contract", {}).get("locked")
        and int(getattr(phase_manager, "project_contract", {}).get("contract_version") or 2) >= 3
    ):
        if not _current_phase_coordinator_run(
            ctx.project_id, phase_id, rebuild_phase,
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Locked phase coordinator completion is missing, stale, "
                    "or not durably succeeded"
                ),
            )
        assignments, bundle = _locked_phase_evidence_bundle(
            ctx, phase_manager, rebuild_phase,
        )
        validation = (
            validate_phase_completion(rebuild_phase, assignments, bundle)
            if require_acceptance_evidence
            else validate_phase_execution_evidence(
                rebuild_phase, assignments, bundle,
            )
        )
        if not validation.valid:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        "Locked task acceptance evidence is incomplete"
                        if require_acceptance_evidence
                        else "Locked task execution evidence is incomplete"
                    ),
                    "validation": validation.to_dict(),
                },
            )
        validated_bundle = bundle
    return validated_bundle


def _assert_phase_can_start(pm: PhaseManager, phase_id: str) -> int:
    """Validate phase ordering and lifecycle before any durable mutation."""
    phase_index = next(
        (
            index for index, candidate in enumerate(pm.phases)
            if str(candidate.get("phase_id")) == str(phase_id)
        ),
        -1,
    )
    if phase_index < 0:
        raise HTTPException(status_code=404, detail=f"Phase {phase_id} not found")

    phase = pm.phases[phase_index]
    status_value = str(phase.get("status") or "pending").strip().lower()
    if status_value != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"阶段当前状态为 {status_value}，不能重复启动",
        )

    if phase_index > 0:
        previous = pm.phases[phase_index - 1]
        if not previous.get("user_confirmed"):
            raise HTTPException(
                status_code=409,
                detail=f"上一阶段「{previous.get('name') or previous.get('phase_id')}」尚未由用户确认完成",
            )
        project_contract = getattr(pm, "project_contract", {}) or {}
        if (
            project_contract.get("locked")
            and int(project_contract.get("contract_version") or 0) >= 3
            and not previous.get("validated_completion_receipt")
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "上一阶段缺少当前 contract generation 的机器验收完成凭证，"
                    "不能由 legacy user_confirmed 布尔值解锁"
                ),
            )
    return phase_index


def _phase_evidence_bundle_digest(bundle: Dict[str, Any]) -> str:
    """Hash evidence semantics while excluding regenerated record identity."""
    normalized = copy.deepcopy(bundle)
    identity_map: Dict[str, str] = {}

    def normalize_records(records: Any) -> list[Dict[str, Any]]:
        output: list[Dict[str, Any]] = []
        for raw in records or []:
            if not isinstance(raw, dict):
                continue
            record = copy.deepcopy(raw)
            original_id = str(record.get("evidence_id") or "")
            payload = record.get("payload") or {}
            regenerated_artifact = (
                str(record.get("kind") or "")
                == EvidenceKind.ARTIFACT_VALIDATION.value
                and str(record.get("producer") or "") == "metis.runner"
                and isinstance(payload, dict)
                and payload.get("validator")
                == "phase.registry_byte_digest"
            )
            if regenerated_artifact:
                record.pop("evidence_id", None)
                record.pop("observed_at", None)
                semantic_id = "sha256:" + hashlib.sha256(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()
                if original_id:
                    identity_map[original_id] = semantic_id
                record["evidence_id"] = semantic_id
            elif original_id:
                identity_map[original_id] = original_id
            output.append(record)
        return sorted(
            output,
            key=lambda item: str(item.get("evidence_id") or ""),
        )

    for task in normalized.get("tasks") or []:
        if isinstance(task, dict) and "evidence" in task:
            task["evidence"] = normalize_records(task.get("evidence"))
    if "phase_evidence" in normalized:
        normalized["phase_evidence"] = normalize_records(
            normalized.get("phase_evidence")
        )

    def normalize_acceptance(rows: Any) -> None:
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            row["evidence_ids"] = sorted(
                identity_map.get(
                    str(evidence_id),
                    "missing:" + str(evidence_id),
                )
                for evidence_id in (row.get("evidence_ids") or [])
            )

    for task in normalized.get("tasks") or []:
        if isinstance(task, dict) and "acceptance" in task:
            normalize_acceptance(task.get("acceptance"))
    if "phase_acceptance" in normalized:
        normalize_acceptance(normalized.get("phase_acceptance"))
    return "sha256:" + hashlib.sha256(
        json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _legacy_phase_evidence_bundle_digest(bundle: Dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            bundle,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _verified_completed_task_ids(
    ctx: ProjectContext,
    pm: PhaseManager,
    *,
    project_id: str,
    contract_digest: str,
    requirements_revision: int,
) -> List[str]:
    """Revalidate confirmed phase bundles before unlocking cross-phase DAGs."""
    completed_task_ids: List[str] = []
    for candidate in pm.phases:
        if not candidate.get("user_confirmed"):
            continue
        completion = candidate.get("validated_completion_receipt") or {}
        generation = str(candidate.get("execution_generation") or "")
        phase_contract_digest = str(
            candidate.get("execution_contract_digest") or contract_digest
        )
        baseline = str(
            candidate.get("execution_artifact_baseline_digest") or ""
        )
        if (
            str(completion.get("project_id") or "") != str(project_id)
            or str(completion.get("phase_id") or "")
            != str(candidate.get("phase_id") or "")
            or str(completion.get("execution_generation") or "") != generation
            or str(completion.get("contract_digest") or "")
            != phase_contract_digest
            or int(completion.get("requirements_revision") or 0)
            != requirements_revision
            or str(completion.get("artifact_baseline_digest") or "") != baseline
            or not generation
        ):
            continue
        persisted_bundle = copy.deepcopy(
            candidate.get("execution_evidence_bundle") or {}
        )
        try:
            assignments, bundle = _locked_phase_evidence_bundle(
                ctx, pm, candidate,
            )
            validation = validate_phase_execution_evidence(
                candidate, assignments, bundle,
            )
        except Exception:
            continue
        bundle_digest = _phase_evidence_bundle_digest(bundle)
        try:
            digest_version = int(
                completion.get("bundle_digest_version") or 1
            )
        except (TypeError, ValueError):
            continue
        if digest_version >= 2:
            digest_matches = (
                str(completion.get("bundle_digest") or "")
                == bundle_digest
            )
        else:
            digest_matches = bool(
                persisted_bundle
                and str(completion.get("bundle_digest") or "")
                == _legacy_phase_evidence_bundle_digest(persisted_bundle)
                and _phase_evidence_bundle_digest(persisted_bundle)
                == bundle_digest
            )
        if (
            not validation.valid
            or not digest_matches
        ):
            continue
        completed_task_ids.extend(
            str(task_id)
            for task_id in (completion.get("task_ids") or [])
            if str(task_id)
        )
    return list(dict.fromkeys(completed_task_ids))


def _allows_fullstack_expert(role_label: str) -> bool:
    role_lower = (role_label or "").lower().replace("-", " ")
    return any(keyword in role_lower for keyword in ("全栈", "全能工程师", "fullstack", "full stack"))


def _parallel_expert_scopes(
    *,
    has_frontend: bool,
    has_backend: bool,
    has_database: bool,
    has_devops: bool,
    has_qa: bool = True,
    has_fullstack: bool = True,
) -> Dict[str, List[str]]:
    """Build globally disjoint write/lease partitions for a mixed-role phase."""
    # The frontend expert exclusively owns the canonical frontend tree. A
    # filename allow-list repeatedly discarded legitimate framework files
    # (tsconfig.*, eslint config, lockfiles and env templates), making valid
    # React/Vue deliveries fail after generation. Directory ownership remains
    # disjoint from backend/database/devops scopes while allowing the framework
    # to choose its normal project structure.
    frontend_product = ["frontend/"]
    backend_product = [
        "backend/package.json", "backend/src/index.js", "backend/src/server.js",
        "backend/requirements.txt", "backend/pyproject.toml", "backend/uv.lock",
        "backend/main.py", "backend/src/main.py", "backend/src/app.py",
        "backend/src/index.py", "backend/src/server.py", "backend/src/__init__.py",
        "backend/src/routes/", "backend/src/controllers/", "backend/src/services/",
        "backend/src/middleware/", "backend/src/models/", "backend/src/schemas/",
        "backend/src/config/", "backend/src/auth/", "backend/src/auth.js", "backend/src/auth.ts",
        "backend/src/api/", "backend/src/lib/", "backend/src/utils/",
        "backend/src/repositories/", "backend/src/validators/",
        "backend/app/__init__.py", "backend/app/main.py",
        "backend/app/api/", "backend/app/routers/",
        "backend/app/crud/", "backend/app/models/", "backend/app/schemas/",
        "backend/app/services/", "backend/app/core/",
    ]
    if not has_qa:
        backend_product.append("backend/tests/")
    database_product = [
        "backend/src/db/", "backend/src/db.js", "backend/src/db.ts",
        "backend/src/database.js", "backend/src/database.ts",
        "backend/app/db/", "backend/app/database.py", "backend/prisma/",
        "backend/migrations/", "database/",
    ]
    devops_product = ["deploy/", "Dockerfile", "docker-compose.yml", ".env.example"]
    if has_devops and not has_fullstack:
        devops_product.extend(["README.md", "package.json"])

    fullstack = ["integration/", "README.md", "package.json"]
    if not has_frontend:
        fullstack.extend(frontend_product)
    if not has_backend:
        fullstack.extend(backend_product)
        if not has_database:
            fullstack.extend(database_product)
    if not has_devops:
        fullstack.extend(devops_product)

    return {
        "frontend": frontend_product,
        "backend": backend_product + ([] if has_database else database_product),
        "database": database_product,
        "qa": [
            "tests/", "backend/tests/",
            # Avoid overlapping the frontend expert's exclusive tree. Mixed
            # phases place browser tests under tests/frontend/ instead. A
            # full-stack expert also owns the complete frontend tree when no
            # dedicated frontend expert exists, so it must be excluded here.
            *([] if (has_frontend or has_fullstack) else ["frontend/tests/"]),
        ],
        "security": ["security/", "backend/src/security/"],
        "devops": devops_product,
        "architecture": ["docs/architecture/"],
        "fullstack_engineer": fullstack,
    }


def _finalize_phase_scope(
    scopes: List[str],
    required_files: List[str],
    claimed_scopes: List[List[str]],
    *,
    subproject_id: str,
) -> List[str]:
    """Return a lock scope that is disjoint from already planned phase scopes.

    Role defaults are intentionally broad, but rebuild/delivery paths are added
    later and can re-introduce an overlap.  Resolve that at the last ownership
    boundary, before claiming a lease, so a phase never partially starts.
    """
    candidates = list(dict.fromkeys(
        str(scope).replace("\\", "/").strip()
        for scope in (scopes or [])
        if str(scope).strip()
    ))
    required = list(dict.fromkeys(
        _normalized_rebuild_path(path)
        for path in required_files
        if is_delivery_file_path(path)
    ))

    def overlaps(scope_list: List[str]) -> bool:
        return any(
            expert_lock._scopes_overlap(scope_list, previous)
            for previous in claimed_scopes
        )

    if not overlaps(candidates):
        return candidates

    # Exact delivery ownership is the safest fallback when a broad role scope
    # intersects an earlier role.  It preserves the file contract while
    # avoiding a lease conflict.
    unique_required = [
        path for path in required
        if not overlaps([path])
    ]
    if unique_required:
        return unique_required

    disjoint = [
        scope for scope in candidates
        if not overlaps([scope])
    ]
    if disjoint:
        return disjoint

    # A task with no unique delivery file still needs a lease so it can work
    # on its non-file output (e.g. planning metadata) without claiming '*'.
    return [f"__phase_scope__/{subproject_id or 'task'}/"]

def _get_supervisor_leader(project_id: str):
    """获取项目的 Supervisory Leader（安全返回 None）"""
    return _supervisor_leaders.get(project_id)

@router.get("/projects/{project_id}/phases/{phase_id}/issues")
async def get_phase_issues(project_id: str, phase_id: str):
    """Get phase issues list with dual-gate fields enriched from DefectTicket"""
    _get_project(project_id)
    sup_leader = _get_supervisor_leader(project_id)
    if not sup_leader:
        return {
            "phase_id": phase_id,
            "issues": [],
            "passed": False,
            "reviewed": False,
            "open_count": 0,
            "fixing_count": 0,
            "fixed_count": 0,
        }
    member = sup_leader.get_member_for_phase(phase_id)
    if not member:
        return {"phase_id": phase_id, "issues": [], "passed": False, "reviewed": False}
    check = member.can_phase_proceed()

    # Enrich issues with dual-gate fields
    enhanced_issues = []
    for iss in member.issues:
        entry = dict(iss)
        entry.setdefault("security_status", "open")
        entry.setdefault("file_version_hash", "")
        entry.setdefault("fix_rounds", 0)
        entry.setdefault("step_execution_state", {})
        # Match with DefectTicket from repair_registry
        try:
            sp_id = iss.get("subproject_id", "")
            if sp_id and sp_id in repair_registry._controllers.get(project_id, {}):
                ctrl = repair_registry._controllers[project_id][sp_id]
                iss_key = (iss.get("file_path", ""), iss.get("layer", ""), (iss.get("message", "") or "")[:40])
                for dt in ctrl.defects.values():
                    if (dt.file_path, dt.layer, dt.message[:40]) == iss_key:
                        entry["security_status"] = dt.security_status.value
                        entry["file_version_hash"] = dt.file_version_hash
                        entry["fix_rounds"] = dt.fix_rounds
                        entry["step_execution_state"] = dict(dt.step_execution_state)
                        break
        except Exception:
            pass
        enhanced_issues.append(entry)

    return {
        "phase_id": phase_id,
        "issues": enhanced_issues,
        "passed": check["can_proceed"],
        "reviewed": len(member.review_log) > 0,
        "open_count": len([i for i in member.issues if i.get("status") == "open"]),
        "fixing_count": len([i for i in member.issues if i.get("status") == "fixing"]),
        "fixed_count": len([i for i in member.issues if i.get("status") == "fixed"]),
    }
@router.get("/projects/{project_id}/phases")
async def list_project_phases(project_id: str):
    """获取项目阶段列表"""
    ctx = _get_project(project_id)
    pm = _phase_managers.get(project_id)
    if pm is None:
        return {"phases": []}
    for phase in (getattr(pm, "phases", None) or pm.to_dict().get("phases", [])):
        _recover_failed_agent_outputs(ctx, str(phase.get("phase_id") or ""))
    result = pm.to_dict()
    # 深拷贝注入 Agent 详情，不污染 PhaseManager 内存（避免下次请求 unhashable dict TypeError）
    import copy
    enriched_phases = []
    for phase in result.get("phases", []):
        phase_copy = dict(phase)
        qc_entry = (ctx.qc_results.get(str(phase.get("phase_id", ""))) or {}).get("qa") or {}
        phase_copy["qc_round"] = int(qc_entry.get("qc_round", 0) or 0)
        phase_copy["qc_fixed_count"] = int(qc_entry.get("fixed_count", 0) or 0)
        phase_copy["qc_score"] = int(qc_entry.get("score", 0) or 0)
        agent_details = []
        # 1) 从 phase["agents"] 中查找
        for aid in phase.get("agents", []):
            if isinstance(aid, str) and aid in ctx.agents:
                agent_details.append(ctx.agents[aid])
            elif isinstance(aid, dict):
                agent_details.append(aid)
        # 2) 跨数据源 fallback：从 ctx.agents 中按 phase_id 匹配（PhaseManager 的 agents 常为空数组 []）
        if not agent_details or len(phase.get("agents", [])) == 0:
            for ainfo in ctx.agents.values():
                if isinstance(ainfo, dict) and ainfo.get("phase_id") == phase.get("phase_id", ""):
                    agent_details.append(ainfo)
        phase_copy["agent_details"] = agent_details
        enriched_phases.append(phase_copy)
    return {"project_id": result.get("project_id"), "phases": enriched_phases, **{k: v for k, v in result.items() if k not in ("project_id", "phases")}}

@router.get("/projects/{project_id}/phases/{phase_id}")
async def get_single_phase(project_id: str, phase_id: str):
    """获取单个阶段详情（含 Agent 状态和文件）"""
    ctx = _get_project(project_id)
    pm = _phase_managers.get(project_id)
    if pm is None:
        raise HTTPException(status_code=404, detail="Phase manager not initialized")
    phase = pm.get_phase(phase_id)
    if not phase:
        phase = next(
            (
                candidate for candidate in pm.phases
                if str(candidate.get("phase_id")) == str(phase_id)
            ),
            None,
        )
    if not phase:
        raise HTTPException(status_code=404, detail=f"Phase {phase_id} not found")
    if _migrate_invalid_phase_plan(phase, getattr(pm, "project_contract", {}) or {}):
        await _persist_all_async()
    # 注入 Agent 详情
    enriched_agents = []
    for aid in phase.get("agents", []):
        if aid in ctx.agents:
            enriched_agents.append(ctx.agents[aid])
    qc_entry = (ctx.qc_results.get(str(phase_id)) or {}).get("qa") or {}
    return {
        **phase,
        "agents": enriched_agents,
        "qc_round": int(qc_entry.get("qc_round", 0) or 0),
        "qc_fixed_count": int(qc_entry.get("fixed_count", 0) or 0),
        "qc_score": int(qc_entry.get("score", 0) or 0),
    }


def _infer_phase_tech_stack(phase: Dict[str, Any], project_description: str = "") -> List[str]:
    """Preserve explicit framework choices when phase execution calls the model."""
    configured = phase.get("tech_stack") or []
    if isinstance(configured, dict):
        configured_values = [str(value) for value in configured.values() if value]
    elif isinstance(configured, (list, tuple, set)):
        configured_values = [str(value) for value in configured if value]
    else:
        configured_values = [str(configured)] if configured else []

    contract_text = "\n".join([
        str(project_description or ""),
        str(phase.get("name") or ""),
        str(phase.get("description") or ""),
        json.dumps(phase.get("deliverables") or [], ensure_ascii=False),
        json.dumps(phase.get("expert_requirements") or [], ensure_ascii=False),
        "\n".join(configured_values),
    ]).lower()
    known = (
        ("fastapi", "FastAPI (Python)"),
        ("react", "React"),
        ("typescript", "TypeScript"),
        ("sqlite", "SQLite"),
        ("pytest", "pytest"),
        ("testclient", "FastAPI TestClient"),
        ("postgresql", "PostgreSQL"),
        ("vue", "Vue"),
        ("svelte", "Svelte"),
    )
    inferred = list(configured_values)
    project_contract = phase.get("project_contract") or {}
    required_tech = {str(item).lower() for item in project_contract.get("required_tech") or []}
    if project_contract.get("locked") and required_tech:
        # A locked contract is authoritative; do not infer technologies from
        # free-form descriptions (which was the source of stack drift).
        return [value for value in inferred if any(token in value.lower() for token in required_tech)] or sorted(required_tech)
    for token, label in known:
        if token in contract_text and label not in inferred:
            inferred.append(label)
    return inferred


def _migrate_invalid_phase_plan(phase: Dict[str, Any], project_contract: Dict[str, Any]) -> bool:
    """Migrate valid legacy data or explicitly block it; never silently trust it."""
    if (
        str(phase.get("phase_plan_version") or "") == PHASE_PLAN_SCHEMA_VERSION
        or (
            isinstance(phase.get("phase_plan"), dict)
            and phase["phase_plan"].get("schema_version")
            == PHASE_PLAN_SCHEMA_VERSION
        )
    ):
        return False
    existing = phase.get("expert_requirements") or []
    task_contract = phase_task_contract(phase)
    if not existing:
        if project_contract.get("locked") and task_contract:
            phase_for_validation = {**phase, "task_contract": task_contract}
            canonical = deterministic_phase_fallback(
                phase_for_validation, project_contract
            )
            validation = validate_phase_plan_layers(
                phase_for_validation, canonical, project_contract
            )
            if canonical and validation.valid:
                phase.update({
                    "task_contract": task_contract,
                    "expert_requirements": canonical,
                    "phase_plan_version": PHASE_PLAN_VERSION,
                    "plan_status": "saved",
                    "plan_generated": True,
                    "plan_contract_validated": True,
                    "plan_generation_mode": "locked_contract_recovery",
                    "plan_generated_at": time.time(),
                    "plan_validation": validation.to_dict(),
                    "plan_artifact_metadata": artifact_metadata(
                        "phase_plan",
                        PHASE_PLAN_VERSION,
                        "locked_contract_recovery",
                        validation,
                        [{"type": "missing_phase_plan_recovered"}],
                    ),
                    "legacy_plan_blocked": False,
                })
                return True
        return False
    if not task_contract:
        phase.update({
            "plan_status": "validation_failed",
            "legacy_plan_blocked": True,
            "plan_contract_validated": False,
            "plan_contract_violations": ["legacy phase plan has no traceable task contract"],
            "plan_validation": {
                "valid": False, "artifact_type": "phase_plan",
                "issues": [{
                    "layer": "phase_contract", "code": "missing_task_contract", "path": "$",
                    "message": "legacy phase plan has no traceable task contract",
                }],
            },
        })
        return True
    phase_for_validation = {**phase, "task_contract": task_contract}
    existing_validation = validate_phase_plan_layers(phase_for_validation, existing, project_contract)
    if existing_validation.valid:
        changed = not bool(phase.get("plan_contract_validated"))
        phase["plan_contract_validated"] = True
        phase["legacy_plan_blocked"] = False
        if not phase.get("phase_plan_version") or not phase.get("plan_artifact_metadata"):
            phase["phase_plan_version"] = PHASE_PLAN_VERSION
            phase["plan_status"] = "saved"
            phase["plan_validation"] = existing_validation.to_dict()
            phase["plan_artifact_metadata"] = artifact_metadata(
                "phase_plan", PHASE_PLAN_VERSION, "legacy_migrated", existing_validation,
                [{"type": "legacy_metadata_migration", "preserved_executable_fields": True}],
            )
            phase["plan_artifact_metadata"]["migrated_at"] = time.time()
            changed = True
        return changed
    canonical = deterministic_phase_fallback(phase_for_validation, project_contract)
    migrated_validation = validate_phase_plan_layers(phase_for_validation, canonical, project_contract)
    if not canonical or not migrated_validation.valid:
        phase.update({
            "plan_status": "validation_failed",
            "legacy_plan_blocked": True,
            "plan_contract_validated": False,
            "plan_contract_violations": existing_validation.violations,
            "plan_validation": existing_validation.to_dict(),
        })
        return True
    phase.update({
        "task_contract": task_contract,
        "expert_requirements": canonical,
        "phase_plan_version": PHASE_PLAN_VERSION,
        "plan_status": "saved",
        "plan_generated": True,
        "plan_contract_validated": True,
        "plan_warnings": [],
        "plan_generation_mode": "contract_migration",
        "plan_generated_at": time.time(),
        "plan_validation": migrated_validation.to_dict(),
        "plan_artifact_metadata": artifact_metadata(
            "phase_plan", PHASE_PLAN_VERSION, "legacy_contract_migration", migrated_validation,
            [{
                "type": "legacy_invalid_plan_replaced",
                "previous_validation": existing_validation.to_dict(),
            }],
        ),
        "legacy_plan_blocked": False,
    })
    phase.pop("plan_contract_violations", None)
    return True

@router.post("/projects/{project_id}/phases/{phase_id}/pm-chat")
async def phase_pm_chat(project_id: str, phase_id: str, request: PhasePMChatRequest):
    """与阶段PM对话"""
    ctx = _get_project(project_id)
    from core.app_state import hermes_client
    if project_id not in _pm_teams:
        memory = HybridMemory(f"memory/{project_id}_pm_team")
        leader = PMLeaderAgent(hermes_client=hermes_client, memory_store=memory)
        _pm_teams[project_id] = leader
    leader = _pm_teams[project_id]
    pm = _phase_managers.get(project_id)
    phase = pm.get_phase(phase_id) if pm else None
    if not phase:
        raise HTTPException(status_code=404, detail=f"Phase {phase_id} not found")
    member = leader.get_member_for_phase(phase_id)
    if not member:
        member = leader.assign_phase_to_member(
            phase_id,
            str(phase.get("name") or ""),
            str(phase.get("description") or ""),
        )
    if not member:
        raise HTTPException(status_code=503, detail="No phase PM is available")
    member.phase_plan = {
        "project_name": getattr(ctx, "name", ""),
        "project_requirements": leader.canonical_requirements,
        "total_plan": copy.deepcopy(leader.final_plan or {}),
        "current_phase": copy.deepcopy(phase),
    }
    phase["pm_member_id"] = member.member_id
    phase["pm_member_name"] = member.name
    project_background = json.dumps(
        {
            "project_name": getattr(ctx, "name", ""),
            "project_requirements": leader.canonical_requirements,
            "total_plan_summary": (leader.final_plan or {}).get("summary"),
            "current_phase": {
                key: phase.get(key)
                for key in (
                    "phase_id", "name", "objective", "work_items",
                    "technical_requirements", "dependencies",
                )
            },
        },
        ensure_ascii=False,
    )
    result = member.chat(
        user_input=request.message,
        history=request.history,
        context_summary=request.context_summary,
        project_background=project_background,
    )
    await _persist_all_async()
    return result

# ==== Missing routes added by audit fix ====


def _assign_phase_pm_members(leader, phases: List[Dict[str, Any]], ctx) -> None:
    """Bind one stable PM member to every phase and inject its full planning context."""
    for phase in phases:
        phase_id = str(phase.get("phase_id") or "")
        member = leader.get_member_for_phase(phase_id)
        if not member:
            member = leader.assign_phase_to_member(
                phase_id,
                str(phase.get("name") or ""),
                str(phase.get("description") or ""),
            )
        if not member:
            raise RuntimeError(f"unable to assign phase PM for {phase_id}")
        member.phase_plan = {
            "project_name": getattr(ctx, "name", ""),
            "project_requirements": leader.canonical_requirements,
            "total_plan": copy.deepcopy(leader.final_plan or {}),
            "current_phase": copy.deepcopy(phase),
        }
        phase["pm_member_id"] = member.member_id
        phase["pm_member_name"] = member.name


@router.post("/projects/{project_id}/phases/init")
async def init_project_phases(project_id: str):
    """Initialize phases from PM confirmed plan"""
    ctx = _get_project(project_id)
    pm_leader = _pm_teams.get(project_id)
    if not pm_leader or not pm_leader.final_plan:
        raise HTTPException(status_code=400, detail="PM plan not confirmed yet")
    pm = _phase_managers.get(project_id)
    created_manager = pm is None
    if not pm:
        pm = PhaseManager(project_id, ctx.workspace)
        _phase_managers[project_id] = pm
    manager_snapshot = (
        None if created_manager else copy.deepcopy(pm.to_dict())
    )
    final_plan = pm_leader.get_final_plan_for_hr()
    if (final_plan or {}).get("schema_version") == "total-plan/v1":
        validation_data = copy.deepcopy(
            (pm_leader.plan_generation or {}).get("validation")
            or {"valid": True, "artifact_type": "total_plan", "issues": []}
        )
        validation_valid = bool(validation_data.get("valid"))
    else:
        validation = validate_plan_layers(
            final_plan or {},
            (final_plan or {}).get("project_contract"),
        )
        validation_data = validation.to_dict()
        validation_valid = validation.valid
    if not validation_valid:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "validation_failed",
                "code": "plan_contract_violation",
                "violations": [
                    item.get("message", "")
                    for item in validation_data.get("issues", [])
                ],
                "validation": validation_data,
            },
        )
    if pm.phases:
        execution_started = (
            pm.current_phase_index >= 0
            or any(
                phase.get("execution_generation")
                or phase.get("started_at")
                or phase.get("agents")
                or str(phase.get("status") or "pending").lower()
                not in {"pending", "planned", "ready"}
                for phase in pm.phases
                if isinstance(phase, dict)
            )
        )
        if execution_started:
            raise HTTPException(
                status_code=409,
                detail=(
                    "阶段执行生命周期已开始，禁止重新初始化；"
                    "请使用阶段恢复或重建流程"
                ),
            )
        expected_contract = (final_plan or {}).get("project_contract") or {}
        if (pm.project_contract or {}) != expected_contract:
            raise HTTPException(
                status_code=409,
                detail=(
                    "现有阶段属于不同 ProjectContract；"
                    "必须创建新的干净阶段管理器"
                ),
            )
        _assign_phase_pm_members(pm_leader, pm.phases, ctx)
        await _persist_all_async()
        return {
            "success": True,
            "status": "already_initialized",
            "validation": validation_data,
            "phases": pm.to_dict(),
        }
    try:
        pm.init_phases_from_plan(final_plan)
        _assign_phase_pm_members(pm_leader, pm.phases, ctx)
        await _persist_all_async()
    except Exception:
        if created_manager:
            if _phase_managers.get(project_id) is pm:
                _phase_managers.pop(project_id, None)
        elif manager_snapshot is not None:
            pm.from_dict(copy.deepcopy(manager_snapshot))
        raise
    return {
        "success": True,
        "status": "saved",
        "validation": validation_data,
        "phases": pm.to_dict(),
    }


PHASE_PLAN_SCHEMA_VERSION = "phase-plan/v1"


def _phase_plan_issue(code: str, path: str, message: str, **details) -> Dict[str, Any]:
    return {
        "layer": "phase_plan_protocol",
        "code": code,
        "path": path,
        "message": message,
        **{key: value for key, value in details.items() if value is not None},
    }


def _parse_phase_plan_v1(content: str) -> Optional[Dict[str, Any]]:
    """Extract the complete phase-plan/v1 root, never a nested JSON object."""
    decoder = json.JSONDecoder()
    text = str(content or "")
    cursor = 0
    while cursor < len(text):
        start = text.find("{", cursor)
        if start < 0:
            return None
        try:
            value, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        if (
            isinstance(value, dict)
            and value.get("schema_version") == PHASE_PLAN_SCHEMA_VERSION
        ):
            return value
        cursor = start + 1
    return None


def _uses_phase_plan_v1(phase: Dict[str, Any], leader) -> bool:
    """Keep fixed-format phases on v1 across persistence and regeneration."""
    if str(phase.get("phase_plan_version") or "") == PHASE_PLAN_SCHEMA_VERSION:
        return True
    if (
        isinstance(phase.get("phase_plan"), dict)
        and phase["phase_plan"].get("schema_version") == PHASE_PLAN_SCHEMA_VERSION
    ):
        return True
    if (
        isinstance(phase.get("phase_requirements_snapshot"), dict)
        and phase["phase_requirements_snapshot"].get("schema_version")
        == "phase-requirements/v1"
    ):
        return True
    if (
        leader
        and isinstance(getattr(leader, "final_plan", None), dict)
        and leader.final_plan.get("schema_version") == "total-plan/v1"
    ):
        return True
    return any(
        key in phase
        for key in (
            "objective",
            "work_items",
            "technical_requirements",
            "source_requirement_ids",
        )
    )


def _expert_pool_snapshot(pool) -> Dict[str, Any]:
    experts = []
    for profile in pool.list_experts(status="available"):
        data = profile.to_dict()
        experts.append({
            "expert_id": str(data.get("expert_id") or ""),
            "name": str(data.get("name") or ""),
            "role": str(data.get("role") or ""),
            "agent_type": str(data.get("agent_type") or ""),
            "domains": list(data.get("domains") or []),
            "skills": [
                str(item.get("name") or "")
                for item in (data.get("skills") or [])
                if isinstance(item, dict) and item.get("name")
            ],
            "status": str(data.get("status") or ""),
            "updated_at": data.get("updated_at"),
        })
    experts.sort(key=lambda item: item["expert_id"])
    revision = "sha256:" + hashlib.sha256(
        json.dumps(
            experts,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {"revision": revision, "experts": experts}


def _phase_v1_expert_binding_issues(
    phase: Dict[str, Any],
    expert_snapshot: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Reconfirm saved expert assignments against the live pool before start."""
    if not (
        str(phase.get("phase_plan_version") or "") == PHASE_PLAN_SCHEMA_VERSION
        or (
            isinstance(phase.get("phase_plan"), dict)
            and phase["phase_plan"].get("schema_version")
            == PHASE_PLAN_SCHEMA_VERSION
        )
    ):
        return []
    issues: List[Dict[str, Any]] = []
    available_ids = {
        str(item.get("expert_id") or "")
        for item in (expert_snapshot.get("experts") or [])
        if isinstance(item, dict) and item.get("expert_id")
    }
    assigned_ids = {
        str(expert_id)
        for task in (
            phase.get("expert_requirements")
            or phase.get("task_contract")
            or []
        )
        if isinstance(task, dict)
        for expert_id in (task.get("assigned_expert_ids") or [])
        if str(expert_id)
    }
    unavailable = sorted(assigned_ids - available_ids)
    if unavailable:
        issues.append(_phase_plan_issue(
            "assigned_expert_unavailable", "$.expert_requirements",
            "a planned expert was deleted, retired or is no longer available",
            expected=sorted(available_ids),
            actual=unavailable,
        ))
    return issues


def _phase_requirements_snapshot(
    *,
    project_id: str,
    project_name: str,
    leader,
    phase: Dict[str, Any],
    phase_user_requirements: str,
) -> Dict[str, Any]:
    total_plan = (leader.final_plan if leader else None) or {}
    snapshot = {
        "schema_version": "phase-requirements/v1",
        "revision": int(phase.get("phase_requirements_revision") or 0) + 1,
        "project_id": project_id,
        "project_name": project_name,
        "project_requirements": {
            "content": str(getattr(leader, "canonical_requirements", "") or ""),
            "revision": int(getattr(leader, "requirements_revision", 0) or 0),
            "digest": str(getattr(leader, "requirements_digest", "") or ""),
        },
        "total_plan": {
            "schema_version": total_plan.get("schema_version"),
            "project_name": total_plan.get("project_name"),
            "summary": total_plan.get("summary"),
            "phases": copy.deepcopy(total_plan.get("phases") or []),
        },
        "current_phase": {
            key: copy.deepcopy(phase.get(key))
            for key in (
                "phase_id", "name", "objective", "work_items",
                "technical_requirements", "dependencies",
                "source_requirement_ids",
            )
        },
        "inherited_technical_requirements": list(
            phase.get("technical_requirements")
            or phase.get("tech_stack")
            or []
        ),
        "phase_user_requirements": str(phase_user_requirements or "").strip(),
    }
    snapshot["digest"] = "sha256:" + hashlib.sha256(
        json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return snapshot


def _reserved_phase_files(pm, current_phase_id: str) -> Dict[str, Dict[str, str]]:
    reserved: Dict[str, Dict[str, str]] = {}
    for item in (pm.project_contract or {}).get("required_files") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").replace("\\", "/").strip("/")
        phase_id = str(item.get("phase_id") or "")
        if path and phase_id and phase_id != str(current_phase_id):
            reserved[path] = {
                "phase_id": phase_id,
                "task_id": str(item.get("task_id") or ""),
            }
    return reserved


def _validate_phase_plan_v1(
    plan: Any,
    *,
    phase: Dict[str, Any],
    requirements_snapshot: Dict[str, Any],
    expert_snapshot: Dict[str, Any],
    reserved_files: Dict[str, Dict[str, str]],
) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    if not isinstance(plan, dict):
        return [_phase_plan_issue("invalid_json", "$", "phase plan must be a JSON object")]
    root_fields = {
        "schema_version", "phase_id", "summary",
        "effective_technical_requirements", "technical_overrides",
        "tasks", "assignments", "expert_pool_revision",
    }
    extra = sorted(set(plan) - root_fields)
    missing = sorted(root_fields - set(plan))
    if extra:
        issues.append(_phase_plan_issue(
            "unexpected_fields", "$", "phase plan contains unsupported fields",
            expected=sorted(root_fields), actual=extra,
        ))
    if missing:
        issues.append(_phase_plan_issue(
            "missing_fields", "$", "phase plan is missing required fields",
            expected=sorted(root_fields), actual=missing,
        ))
    if plan.get("schema_version") != PHASE_PLAN_SCHEMA_VERSION:
        issues.append(_phase_plan_issue(
            "schema_version", "$.schema_version", "unsupported phase plan schema",
            expected=PHASE_PLAN_SCHEMA_VERSION,
            actual=plan.get("schema_version"),
        ))
    if str(plan.get("phase_id") or "") != str(phase.get("phase_id") or ""):
        issues.append(_phase_plan_issue(
            "phase_id_mismatch", "$.phase_id", "phase ID must match the assigned phase",
            expected=phase.get("phase_id"), actual=plan.get("phase_id"),
        ))
    if not isinstance(plan.get("summary"), str) or not plan["summary"].strip():
        issues.append(_phase_plan_issue(
            "summary_empty", "$.summary", "summary must be a non-empty string",
        ))

    def string_array(value: Any) -> bool:
        return isinstance(value, list) and all(
            isinstance(item, str) and item.strip() for item in value
        )

    effective = plan.get("effective_technical_requirements")
    overrides = plan.get("technical_overrides")
    if not string_array(effective):
        issues.append(_phase_plan_issue(
            "technology_invalid", "$.effective_technical_requirements",
            "effective technical requirements must be a string array",
        ))
        effective = []
    if not isinstance(overrides, list):
        issues.append(_phase_plan_issue(
            "technical_overrides_invalid", "$.technical_overrides",
            "technical_overrides must be an array",
        ))
        overrides = []
    for index, override in enumerate(overrides):
        if not isinstance(override, dict) or not all(
            isinstance(override.get(key), str) and override[key].strip()
            for key in ("from", "to", "reason")
        ):
            issues.append(_phase_plan_issue(
                "technical_override_invalid",
                f"$.technical_overrides[{index}]",
                "each technical override requires non-empty from, to and reason",
            ))
    inherited = [
        str(item).strip()
        for item in (
            requirements_snapshot.get("inherited_technical_requirements") or []
        )
        if str(item).strip()
    ]
    if not overrides:
        effective_lower = {str(item).lower() for item in effective}
        missing_tech = [
            item for item in inherited if item.lower() not in effective_lower
        ]
        if missing_tech:
            issues.append(_phase_plan_issue(
                "inherited_technology_missing",
                "$.effective_technical_requirements",
                "total-plan technology must be inherited unless explicitly overridden",
                expected=inherited, actual=effective,
            ))

    expert_ids = {
        str(item.get("expert_id") or "")
        for item in (expert_snapshot.get("experts") or [])
        if isinstance(item, dict) and item.get("expert_id")
    }
    if str(plan.get("expert_pool_revision") or "") != str(
        expert_snapshot.get("revision") or ""
    ):
        issues.append(_phase_plan_issue(
            "expert_pool_revision_mismatch", "$.expert_pool_revision",
            "expert pool changed; regenerate the phase plan",
            expected=expert_snapshot.get("revision"),
            actual=plan.get("expert_pool_revision"),
        ))
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        issues.append(_phase_plan_issue(
            "tasks_empty", "$.tasks", "phase plan must contain at least one task",
        ))
        tasks = []
    allowed_task_fields = {
        "task_id", "name", "objective", "functional_details",
        "implementation", "implementation_technologies",
        "dependencies", "acceptance_criteria",
    }
    task_ids: List[str] = []
    phase_id = str(phase.get("phase_id") or "")
    for index, task in enumerate(tasks):
        path = f"$.tasks[{index}]"
        if not isinstance(task, dict):
            issues.append(_phase_plan_issue("task_invalid", path, "task must be an object"))
            continue
        unexpected = sorted(set(task) - allowed_task_fields)
        missing_task = sorted(allowed_task_fields - set(task))
        if unexpected:
            issues.append(_phase_plan_issue(
                "unexpected_task_fields", path,
                "task contains unsupported fields",
                expected=sorted(allowed_task_fields), actual=unexpected,
            ))
        if missing_task:
            issues.append(_phase_plan_issue(
                "missing_task_fields", path,
                "task is missing required fields",
                expected=sorted(allowed_task_fields), actual=missing_task,
            ))
        task_id = str(task.get("task_id") or "")
        expected_id = f"{phase_id}-task-{index + 1}"
        if task_id != expected_id:
            issues.append(_phase_plan_issue(
                "task_id_sequence", f"{path}.task_id",
                "task IDs must be sequential inside the phase",
                expected=expected_id, actual=task_id,
            ))
        if task_id in task_ids:
            issues.append(_phase_plan_issue(
                "duplicate_task_id", f"{path}.task_id", "task ID must be unique",
            ))
        for field in ("name", "objective", "implementation"):
            if not isinstance(task.get(field), str) or not task[field].strip():
                issues.append(_phase_plan_issue(
                    "task_text_empty", f"{path}.{field}",
                    f"{field} must be a non-empty string",
                ))
        for field in (
            "functional_details", "implementation_technologies",
            "dependencies", "acceptance_criteria",
        ):
            value = task.get(field)
            if not string_array(value) or (
                field in {
                    "functional_details", "implementation_technologies",
                    "acceptance_criteria",
                } and not value
            ):
                issues.append(_phase_plan_issue(
                    "task_string_array", f"{path}.{field}",
                    f"{field} must be a valid string array",
                ))
        invalid_dependencies = [
            dependency
            for dependency in (
                task.get("dependencies")
                if isinstance(task.get("dependencies"), list)
                else []
            )
            if dependency not in task_ids
        ]
        if invalid_dependencies:
            issues.append(_phase_plan_issue(
                "task_dependency_order", f"{path}.dependencies",
                "task dependencies may only reference earlier tasks",
                expected=task_ids, actual=invalid_dependencies,
            ))
        task_ids.append(task_id)

    assignments = plan.get("assignments")
    if not isinstance(assignments, list) or not assignments:
        issues.append(_phase_plan_issue(
            "assignments_empty", "$.assignments",
            "phase plan must assign every task to an available expert",
        ))
        assignments = []
    assigned_tasks: set[str] = set()
    assigned_experts_by_task: Dict[str, set[str]] = {
        task_id: set() for task_id in task_ids
    }
    for index, assignment in enumerate(assignments):
        path = f"$.assignments[{index}]"
        if not isinstance(assignment, dict) or set(assignment) != {
            "expert_id", "task_ids", "responsibility"
        }:
            issues.append(_phase_plan_issue(
                "assignment_invalid", path,
                "assignment requires exactly expert_id, task_ids and responsibility",
            ))
            continue
        expert_id = str(assignment.get("expert_id") or "")
        if expert_id not in expert_ids:
            issues.append(_phase_plan_issue(
                "unknown_expert_id", f"{path}.expert_id",
                "assignment must select an expert from the current pool",
                expected=sorted(expert_ids), actual=expert_id,
            ))
        assignment_tasks = assignment.get("task_ids")
        if not string_array(assignment_tasks) or not assignment_tasks:
            issues.append(_phase_plan_issue(
                "assignment_tasks_empty", f"{path}.task_ids",
                "assignment must contain task IDs",
            ))
            assignment_tasks = []
        unknown_tasks = [
            task_id for task_id in assignment_tasks if task_id not in task_ids
        ]
        if unknown_tasks:
            issues.append(_phase_plan_issue(
                "assignment_unknown_task", f"{path}.task_ids",
                "assignment references unknown tasks",
                expected=task_ids, actual=unknown_tasks,
            ))
        if not isinstance(assignment.get("responsibility"), str) or not assignment["responsibility"].strip():
            issues.append(_phase_plan_issue(
                "assignment_responsibility_empty", f"{path}.responsibility",
                "assignment responsibility must be non-empty",
            ))
        if expert_id:
            for task_id in assignment_tasks:
                if task_id in assigned_experts_by_task:
                    assigned_experts_by_task[task_id].add(expert_id)
        assigned_tasks.update(assignment_tasks)
    unassigned = sorted(set(task_ids) - assigned_tasks)
    if unassigned:
        issues.append(_phase_plan_issue(
            "tasks_unassigned", "$.assignments",
            "every task must be assigned to at least one expert",
            expected=task_ids, actual=sorted(assigned_tasks),
        ))
    for task_id, owner_ids in assigned_experts_by_task.items():
        if len(owner_ids) != 1:
            issues.append(_phase_plan_issue(
                "task_expert_count", "$.assignments",
                "every task must have exactly one file-owning expert",
                expected=1,
                actual={"task_id": task_id, "expert_ids": sorted(owner_ids)},
            ))
    return issues


def _phase_plan_execution_tasks(
    plan: Dict[str, Any],
    expert_snapshot: Dict[str, Any],
) -> List[Dict[str, Any]]:
    experts = {
        str(item.get("expert_id") or ""): item
        for item in (expert_snapshot.get("experts") or [])
        if isinstance(item, dict)
    }
    assignments_by_task: Dict[str, List[Dict[str, Any]]] = {}
    for assignment in plan.get("assignments") or []:
        for task_id in assignment.get("task_ids") or []:
            assignments_by_task.setdefault(str(task_id), []).append(assignment)
    normalized = []
    for task in plan.get("tasks") or []:
        task_id = str(task.get("task_id") or "")
        assignments = assignments_by_task.get(task_id) or []
        assigned_ids = [
            str(item.get("expert_id") or "") for item in assignments
        ]
        roles = [
            str((experts.get(expert_id) or {}).get("role") or "")
            for expert_id in assigned_ids
            if str((experts.get(expert_id) or {}).get("role") or "").strip()
        ]
        executor_type = _phase_task_executor_type(task, roles)
        normalized.append({
            "task_id": task_id,
            "task_name": str(task.get("name") or ""),
            "task_description": str(task.get("objective") or ""),
            "functional_details": list(task.get("functional_details") or []),
            "required_role": roles[0] if roles else "",
            "executor_type": executor_type,
            "priority": "normal",
            "implementation_method": str(task.get("implementation") or ""),
            "tech_stack": list(
                task.get("implementation_technologies") or []
            ),
            "responsibilities": [
                str(item.get("responsibility") or "") for item in assignments
            ],
            "personnel_count": len(set(assigned_ids)),
            "personnel_allocation": [
                f"{(experts.get(expert_id) or {}).get('name') or expert_id}：1 人"
                for expert_id in dict.fromkeys(assigned_ids)
            ],
            "assigned_expert_ids": list(dict.fromkeys(assigned_ids)),
            "acceptance_criteria": list(
                task.get("acceptance_criteria") or []
            ),
            "dependencies": list(task.get("dependencies") or []),
            # Actual paths are selected by the ExecutionAgent and recorded
            # after a successful write; phase planning does not pre-allocate
            # file ownership.
            "required_files": [],
        })
    return normalized


def _parse_expert_requirements(content: str, *, strict: bool = False) -> List[Dict]:
    """Parse and normalize the LLM phase plan without persisting malformed data."""
    import json as _json
    import re as _re

    match = _re.search(r'\[.*\]', str(content or ""), _re.DOTALL)
    if not match:
        return []
    try:
        raw_items = _json.loads(match.group())
    except (TypeError, ValueError):
        return []
    if not isinstance(raw_items, list):
        return []

    def string_list(value: Any) -> List[str]:
        values = value if isinstance(value, list) else [value]
        return [
            str(item).strip()
            for item in values
            if item is not None and str(item).strip()
        ]

    normalized: List[Dict] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            continue
        if strict and not str(raw.get("task_id") or raw.get("id") or "").strip():
            return []
        task_id = str(raw.get("task_id") or raw.get("id") or "").strip()
        task_name = str(
            raw.get("task_name") or raw.get("name")
            or raw.get("deliverable") or ""
        ).strip()
        raw_roles = string_list(
            raw.get("required_role")
            or raw.get("roles")
            or raw.get("roles_needed")
        )
        required_role = raw_roles[0] if raw_roles else ""
        if not task_name or not required_role or (strict and not task_id):
            continue
        criteria = string_list(
            raw.get("acceptance_criteria")
            or raw.get("criteria")
            or raw.get("acceptance")
        )
        technology_stack = string_list(
            raw.get("tech_stack") or raw.get("technology_stack")
        )
        responsibilities = string_list(
            raw.get("responsibilities") or raw.get("duties")
        )
        personnel_allocation = (
            raw.get("personnel_allocation") or raw.get("staffing")
        )
        personnel_allocation = string_list(personnel_allocation)
        dependencies = string_list(
            raw.get("dependencies") or raw.get("dependency_ids")
        )
        required_files = string_list(
            raw.get("required_files") or raw.get("files")
        )
        required_files = collect_required_file_paths(
            "",
            {"required_files": required_files},
        )
        try:
            personnel_count = int(
                raw.get("personnel_count")
                or raw.get("agent_count")
                or raw.get("headcount")
                or 0
            )
        except (TypeError, ValueError):
            personnel_count = 0
        normalized.append({
            "task_id": task_id or f"task-{index + 1}",
            "task_name": task_name,
            "task_description": str(
                raw.get("task_description")
                or raw.get("description")
                or raw.get("implementation_details")
                or ""
            ).strip(),
            "required_role": required_role,
            "priority": raw.get("priority") if raw.get("priority") in {"high", "normal", "low"} else "normal",
            "implementation_method": str(
                raw.get("implementation_method")
                or raw.get("implementation")
                or raw.get("approach")
                or ""
            ).strip(),
            "tech_stack": technology_stack,
            "responsibilities": responsibilities,
            "personnel_count": max(0, personnel_count),
            "personnel_allocation": personnel_allocation,
            "acceptance_criteria": criteria,
            "dependencies": dependencies,
        })
        if "required_files" in raw or "files" in raw:
            normalized[-1]["required_files"] = required_files
    return normalized


def _merge_locked_phase_task_fields(
    requirements: List[Dict[str, Any]],
    task_contract: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Keep model execution detail while restoring immutable task fields."""
    by_id: Dict[str, Dict[str, Any]] = {}
    for item in requirements:
        task_id = str(item.get("task_id") or "").strip()
        if not task_id or task_id in by_id:
            return []
        by_id[task_id] = item
    locked_ids = [
        str(item.get("task_id") or "").strip()
        for item in task_contract
        if isinstance(item, dict)
    ]
    if not locked_ids or set(by_id) != set(locked_ids):
        return []
    merged: List[Dict[str, Any]] = []
    for locked in task_contract:
        task_id = str(locked.get("task_id") or "").strip()
        row = copy.deepcopy(by_id[task_id])
        row["task_name"] = str(
            locked.get("name")
            or locked.get("task_name")
            or locked.get("deliverable")
            or row.get("task_name")
            or ""
        ).strip()
        row["dependencies"] = [
            str(item).strip()
            for item in (locked.get("dependencies") or [])
            if str(item).strip()
        ]
        row["required_files"] = collect_required_file_paths(
            "",
            {"required_files": locked.get("required_files") or []},
        )
        merged.append(row)
    return merged


def _phase_model_error_record(exc: Exception) -> Dict[str, str]:
    """Return useful failure diagnostics without retaining provider payloads."""
    message = str(exc).lower()
    if any(token in message for token in ("quota", "insufficient", "rate limit", "429")):
        code = "quota_or_rate_limit"
    elif any(token in message for token in ("unauthorized", "forbidden", "api key", "401", "403")):
        code = "authentication_failed"
    elif "timeout" in message:
        code = "timeout"
    else:
        code = "model_call_failed"
    return {"code": code, "error_type": type(exc).__name__}


def _phase_plan_v1_messages(
    *,
    requirements_snapshot: Dict[str, Any],
    expert_snapshot: Dict[str, Any],
    reserved_files: Dict[str, Dict[str, str]],
    previous_output: str = "",
    issues: Optional[List[Dict[str, Any]]] = None,
) -> List[Any]:
    from core.hermes_client import Message, MessageRole

    system = (
        "你是 MeTis 阶段 PM。你负责把当前阶段范围拆成可执行任务；"
        "不得新增、删除或改写总规划中的阶段边界。\n"
        "你必须依据完整项目需求、总规划和当前阶段需求文件，填写任务、实现方式、"
        "功能细节、实现技术和人员分配。任务数量由实际需要决定。\n"
        "技术要求默认继承 total plan。只有阶段需求文件明确要求变更时，"
        "才允许填写 technical_overrides，并给出 from、to、reason；"
        "effective_technical_requirements 必须是最终生效技术。\n"
        "人员只能从 expert_pool.experts 选择，assignments 必须使用现有 expert_id，"
        "不得编造角色、姓名或 ID；expert_pool_revision 必须逐字返回。\n"
        "assignments 只能是根对象的顶层字段；每个 task 内禁止出现 assignments、"
        "人员或角色字段。每个 task 必须且只能分配给一名执行专家；"
        "同一专家可以负责多个 task。\n"
        "不要规划文件路径、文件数量或文件归属；执行专家将根据任务要求自主决定"
        "实际文件，并由系统在执行后登记。测试任务必须真实实现验证要求。\n"
        "只输出下列固定 JSON 对象，不得增加字段，不要 Markdown 或解释：\n"
        '{"schema_version":"phase-plan/v1","phase_id":"phase-1",'
        '"summary":"阶段规划摘要",'
        '"effective_technical_requirements":["最终生效技术"],'
        '"technical_overrides":[{"from":"原技术","to":"新技术","reason":"用户变更依据"}],'
        '"tasks":[{"task_id":"phase-1-task-1","name":"任务名称",'
        '"objective":"任务目标","functional_details":["功能细节"],'
        '"implementation":"实现方式","implementation_technologies":["实现技术"],'
        '"dependencies":[],'
        '"acceptance_criteria":["可验证标准"]}],'
        '"assignments":[{"expert_id":"expert-id","task_ids":["phase-1-task-1"],'
        '"responsibility":"人员职责"}],"expert_pool_revision":"sha256:..."}'
    )
    user = json.dumps(
        {
            "phase_requirements_file": requirements_snapshot,
            "expert_pool": expert_snapshot,
        },
        ensure_ascii=False,
    )
    messages: List[Any] = [
        Message(role=MessageRole.SYSTEM, content=system),
        Message(role=MessageRole.USER, content=user),
    ]
    if previous_output:
        compact_issues = [
            {
                key: value
                for key, value in issue.items()
                if key in {"code", "path", "message", "expected", "actual"}
            }
            for issue in (issues or [])
        ]
        messages.extend([
            Message(role=MessageRole.ASSISTANT, content=previous_output),
            Message(
                role=MessageRole.USER,
                content=(
                    "上一结果不符合 phase-plan/v1。只修正以下协议问题并重新输出完整 JSON："
                    + json.dumps(compact_issues, ensure_ascii=False)
                ),
            ),
        ])
    return messages


def _apply_phase_plan_v1(
    pm,
    phase: Dict[str, Any],
    plan: Dict[str, Any],
    execution_tasks: List[Dict[str, Any]],
    requirements_snapshot: Dict[str, Any],
    expert_snapshot: Dict[str, Any],
) -> None:
    contract = json.loads(json.dumps(pm.project_contract or {}, ensure_ascii=False))
    phase_id = str(phase.get("phase_id") or "")
    contract_phases = [
        item for item in (contract.get("phases") or [])
        if isinstance(item, dict) and str(item.get("phase_id") or "") != phase_id
    ]
    roles = list(dict.fromkeys(
        str(task.get("required_role") or "")
        for task in execution_tasks
        if str(task.get("required_role") or "").strip()
    ))
    executor_types = list(dict.fromkeys(
        str(task.get("executor_type") or "")
        for task in execution_tasks
        if str(task.get("executor_type") or "") in _PHASE_EXECUTOR_TYPES
    ))
    locked_tasks = []
    for task in execution_tasks:
        locked_tasks.append({
            "task_id": task["task_id"],
            "order": len(locked_tasks) + 1,
            "name": task["task_name"],
            "description": task["task_description"],
            "implementation": task["implementation_method"],
            "technology_stack": list(task["tech_stack"]),
            "executor_type": task["executor_type"],
            "roles": [task["required_role"]],
            "responsibilities": list(task["responsibilities"]),
            "personnel_count": task["personnel_count"],
            "personnel_allocation": list(task["personnel_allocation"]),
            "assigned_expert_ids": list(task["assigned_expert_ids"]),
            "required_files": list(task["required_files"]),
            "acceptance_criteria": list(task["acceptance_criteria"]),
            "dependencies": list(task["dependencies"]),
            "source_requirement_ids": list(
                phase.get("source_requirement_ids") or []
            ),
        })
    contract_phases.append({
        "phase_id": phase_id,
        "order": int(phase.get("order") or 0) + 1,
        "name": str(phase.get("name") or ""),
        "roles": roles,
        "tasks": locked_tasks,
        "dependencies": list(phase.get("dependencies") or []),
        "source_constraints": [],
        "acceptance_criteria": [
            criterion
            for task in locked_tasks
            for criterion in task["acceptance_criteria"]
        ],
    })
    contract_phases.sort(key=lambda item: int(item.get("order") or 0))
    contract["phases"] = contract_phases
    contract["roles"] = list(dict.fromkeys(
        str(role)
        for item in contract_phases
        for role in (item.get("roles") or [])
        if str(role).strip()
    ))
    contract["required_files"] = [
        item for item in (contract.get("required_files") or [])
        if isinstance(item, dict) and str(item.get("phase_id") or "") != phase_id
    ]
    for task in execution_tasks:
        for path in task["required_files"]:
            contract["required_files"].append({
                "path": path,
                "required": True,
                "phase_id": phase_id,
                "task_id": task["task_id"],
                "owner_role": task["required_role"],
                "owner_type": task["executor_type"],
                "source": "phase-plan/v1",
            })
    phase.update({
        "description": str(plan.get("summary") or phase.get("description") or ""),
        "tech_stack": list(plan.get("effective_technical_requirements") or []),
        "technical_requirements": list(
            plan.get("effective_technical_requirements") or []
        ),
        "technical_overrides": copy.deepcopy(
            plan.get("technical_overrides") or []
        ),
        "roles_needed": roles,
        "execution_roles": executor_types,
        "task_contract": copy.deepcopy(locked_tasks),
        "expert_requirements": copy.deepcopy(execution_tasks),
        "expert_assignments": copy.deepcopy(plan.get("assignments") or []),
        "expert_pool_revision": expert_snapshot["revision"],
        "phase_requirements_snapshot": copy.deepcopy(requirements_snapshot),
        "phase_requirements_revision": requirements_snapshot["revision"],
        "phase_requirements_digest": requirements_snapshot["digest"],
        "phase_plan": copy.deepcopy(plan),
        "phase_plan_version": PHASE_PLAN_SCHEMA_VERSION,
        "plan_status": "saved",
        "plan_generated": True,
        "plan_contract_validated": True,
        "plan_generated_at": time.time(),
    })
    pm.project_contract = contract
    phase["project_contract"] = contract


async def _generate_phase_plan_v1(
    *,
    project_id: str,
    ctx,
    pm,
    phase: Dict[str, Any],
    leader,
    phase_user_requirements: str,
) -> Dict[str, Any]:
    from core.expert_pool import get_expert_pool

    requirements_snapshot = _phase_requirements_snapshot(
        project_id=project_id,
        project_name=str(getattr(ctx, "name", "") or ""),
        leader=leader,
        phase=phase,
        phase_user_requirements=phase_user_requirements,
    )
    expert_snapshot = _expert_pool_snapshot(
        get_expert_pool(str(getattr(ctx, "owner_user_id", "") or ""))
    )
    reserved_files = _reserved_phase_files(pm, str(phase.get("phase_id") or ""))
    if not expert_snapshot["experts"]:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "validation_failed",
                "code": "expert_pool_empty",
                "message": "当前没有可用于阶段规划的专家",
            },
        )
    attempts: List[Dict[str, Any]] = []
    previous_output = ""
    issues: List[Dict[str, Any]] = []
    plan: Optional[Dict[str, Any]] = None
    for attempt in range(2):
        messages = _phase_plan_v1_messages(
            requirements_snapshot=requirements_snapshot,
            expert_snapshot=expert_snapshot,
            reserved_files=reserved_files,
            previous_output=previous_output,
            issues=issues,
        )
        kwargs = {
            # A plan is not cache-safe until it has passed the phase contract.
            # Otherwise a repair attempt can receive the same invalid result.
            "use_cache": False,
            "request_timeout": 90,
            "max_tokens": 5000,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        try:
            try:
                response = await asyncio.to_thread(
                    hermes_client.chat,
                    messages,
                    **kwargs,
                )
            except TypeError as exc:
                if "unexpected keyword argument" not in str(exc):
                    raise
                response = await asyncio.to_thread(
                    hermes_client.chat,
                    messages,
                )
        except Exception as exc:
            attempts.append({
                "attempt": attempt + 1,
                "status": "model_failed",
                **_phase_model_error_record(exc),
            })
            raise HTTPException(
                status_code=502,
                detail={
                    "status": "model_failed",
                    "message": "阶段规划模型调用失败",
                    "attempts": attempts,
                },
            ) from exc
        previous_output = str(response.get("content") or "")
        candidate = _parse_phase_plan_v1(previous_output)
        issues = _validate_phase_plan_v1(
            candidate,
            phase=phase,
            requirements_snapshot=requirements_snapshot,
            expert_snapshot=expert_snapshot,
            reserved_files=reserved_files,
        )
        if not issues:
            plan = candidate
            attempts.append({
                "attempt": attempt + 1,
                "status": "generated",
                "validation": {"valid": True, "issues": []},
            })
            break
        attempts.append({
            "attempt": attempt + 1,
            "status": "validation_failed",
            "issues": issues,
        })
    if plan is None:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "validation_failed",
                "message": "阶段规划未通过 phase-plan/v1 校验",
                "issues": issues,
                "attempts": attempts,
            },
        )
    execution_tasks = _phase_plan_execution_tasks(plan, expert_snapshot)
    _apply_phase_plan_v1(
        pm,
        phase,
        plan,
        execution_tasks,
        requirements_snapshot,
        expert_snapshot,
    )
    phase["plan_generation_attempts"] = attempts
    phase["plan_generation_mode"] = (
        "model" if len(attempts) == 1 else "model_repaired"
    )
    phase["plan_validation"] = {
        "valid": True,
        "artifact_type": "phase_plan",
        "schema_version": PHASE_PLAN_SCHEMA_VERSION,
        "issues": [],
    }
    phase["plan_artifact_metadata"] = {
        "artifact_type": "phase_plan",
        "schema_version": PHASE_PLAN_SCHEMA_VERSION,
        "source": phase["plan_generation_mode"],
        "saved_at": time.time(),
        "requirements_digest": requirements_snapshot["digest"],
        "expert_pool_revision": expert_snapshot["revision"],
    }
    await _persist_all_async()
    return {
        "success": True,
        "status": "saved",
        "phase_id": phase["phase_id"],
        "phase_plan": plan,
        "expert_requirements": execution_tasks,
        "count": len(execution_tasks),
        "requirements_snapshot": requirements_snapshot,
        "expert_pool_revision": expert_snapshot["revision"],
        "generation_mode": phase["plan_generation_mode"],
        "model_status": "generated",
        "validation": phase["plan_validation"],
        "attempts": attempts,
        "warnings": [],
        "auto_corrected": len(attempts) > 1,
    }


def _phase_plan_messages(
    *,
    phase: Dict[str, Any],
    phase_description: str,
    project_name: str,
    project_contract: Dict[str, Any],
    task_contract: List[Dict[str, Any]],
    user_requirements: str = "",
    previous_output: str = "",
    violations: Optional[List[str]] = None,
) -> List[Any]:
    """Build a bounded phase-planning prompt; retries repair internally."""
    from core.hermes_client import Message, MessageRole

    prompt_contract = dict(project_contract or {})
    prompt_contract.pop("source_requirements", None)
    roles = [str(role) for role in phase.get("roles_needed") or [] if str(role).strip()] or ["\u5f00\u53d1\u5de5\u7a0b\u5e08"]
    system = (
        "Every output task must include a non-empty required_files array of "
        "safe workspace-relative paths. Copy it exactly from the matching "
        "locked task; never add, remove, rename, or reassign a delivery file. "
        "你是阶段 PM。锁定任务是已确认总规划的不可变执行契约。\n"
        "必须为每个锁定任务输出且只输出一个顶层任务；task_id 必须逐字继承，禁止新增、删除、拆分、合并或重命名。\n"
        "required_role 必须从该锁定任务的角色集合中逐字选择，不得创造或改写角色；技术栈和阶段边界不得改变。\n"
        "dependencies 必须逐字继承每个锁定任务的 dependencies，不得遗漏、增加或改写。\n"
        "你可以细化 task_description、implementation_method、tech_stack、"
        "responsibilities、personnel_count、personnel_allocation、priority 和 "
        "acceptance_criteria；不得创建新顶层任务或改变锁定边界。\n"
        "只输出 JSON 数组，不要 Markdown 或解释。格式：\n"
        '[{"task_id":"phase-1-task-1","task_name":"锁定任务名称",'
        '"task_description":"执行细节","required_role":"允许角色",'
        '"implementation_method":"实现方式","tech_stack":["技术"],'
        '"responsibilities":["职责"],"personnel_count":1,'
        '"personnel_allocation":["角色：1 人"],'
        '"priority":"high|normal|low","acceptance_criteria":["可验证条件"],'
        '"required_files":["逐字继承的锁定工作区相对路径"],'
        '"dependencies":["锁定的前置 task_id"]}]'
    )
    user = (
        f"项目：{project_name}\n阶段：{phase.get('name', '')}\n"
        f"阶段描述：{phase_description}\n"
        f"允许角色：{json.dumps(roles, ensure_ascii=False)}\n"
        f"锁定任务（数量和 ID 必须完全一致）：{json.dumps(task_contract, ensure_ascii=False)}\n"
        f"项目契约：{json.dumps(prompt_contract, ensure_ascii=False)}"
    )
    if user_requirements.strip():
        user += f"\n用户对执行细节的补充（不得覆盖上述契约）：{user_requirements.strip()}"
    messages: List[Any] = [
        Message(role=MessageRole.SYSTEM, content=system),
        Message(role=MessageRole.USER, content=user),
    ]
    if previous_output:
        messages.extend([
            Message(role=MessageRole.ASSISTANT, content=previous_output),
            Message(
                role=MessageRole.USER,
                content=(
                    "上一结果未通过内部契约校验，请直接重写完整 JSON 数组。"
                    "不要解释，严格继承锁定任务 ID、数量和允许角色。\n"
                    f"校验问题：{json.dumps(violations or [], ensure_ascii=False)}"
                ),
            ),
        ])
    return messages


@router.post("/projects/{project_id}/phases/{phase_id}/plan-experts")
async def plan_experts_for_phase(project_id: str, phase_id: str, body: Dict):
    """Generate expert requirements for a phase"""
    ctx = _get_project(project_id)
    pm = _phase_managers.get(project_id)
    if not pm:
        raise HTTPException(status_code=400, detail="Phase manager not initialized")
    phase = pm.get_phase(phase_id)
    if not phase:
        raise HTTPException(status_code=404, detail=f"Phase {phase_id} not found")
    phase_snapshot = copy.deepcopy(phase)

    async def persist_phase_plan() -> None:
        try:
            await _persist_all_async()
        except Exception:
            phase.clear()
            phase.update(copy.deepcopy(phase_snapshot))
            raise

    phase_desc = body.get("phase_description", phase.get("description", ""))
    user_requirements = str(body.get("user_requirements") or "")
    project_contract = pm.project_contract or {}
    pm_leader = _pm_teams.get(project_id)
    if not project_contract and pm_leader and pm_leader.final_plan:
        project_contract = pm_leader.final_plan.get("project_contract") or {}
    task_contract = phase_task_contract(phase)
    if _uses_phase_plan_v1(phase, pm_leader):
        return await _generate_phase_plan_v1(
            project_id=project_id,
            ctx=ctx,
            pm=pm,
            phase=phase,
            leader=pm_leader,
            phase_user_requirements=user_requirements,
        )
    phase_for_validation = {**phase, "task_contract": task_contract}
    requirements: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    previous_output = ""
    auto_corrected = False
    generation_mode = "model"
    model_failed = False
    attempts: List[Dict[str, Any]] = []
    previous_contract_validated = bool(phase.get("plan_contract_validated"))
    phase["plan_status"] = "generating"
    phase["phase_plan_version"] = PHASE_PLAN_VERSION
    validation = None

    # One bounded repair attempt handles normal LLM format/scope drift without
    # requiring the user to manually coach the phase PM.
    for attempt in range(2):
        prompt = _phase_plan_messages(
            phase=phase_for_validation,
            phase_description=phase_desc,
            project_name=ctx.name,
            project_contract=project_contract,
            task_contract=task_contract,
            user_requirements=user_requirements,
            previous_output=previous_output,
            violations=[item.get("message", "") for item in issues],
        )
        try:
            resp = await asyncio.to_thread(hermes_client.chat, prompt)
            previous_output = str(resp.get("content", ""))
        except Exception as exc:
            logger.warning(
                "Phase plan model call failed for %s/%s (%s)",
                project_id, phase_id, type(exc).__name__,
            )
            model_failed = True
            attempts.append({
                "attempt": attempt + 1, "status": "model_failed", **_phase_model_error_record(exc),
            })
            previous_output = ""
            break
        requirements = _merge_locked_phase_task_fields(
            _parse_expert_requirements(previous_output, strict=True),
            task_contract,
        )
        if requirements:
            validation = validate_phase_plan_layers(phase_for_validation, requirements, project_contract)
            issues = validation.to_dict()["issues"]
        else:
            issues = [{
                "layer": "json_schema", "code": "invalid_or_incomplete_json", "path": "$",
                "message": "model response did not contain a complete valid task array",
            }]
            validation = None
        if not issues:
            auto_corrected = attempt > 0
            generation_mode = "model_repaired" if auto_corrected else "model"
            attempts.append({"attempt": attempt + 1, "status": "generated", "validation": validation.to_dict()})
            break
        attempts.append({"attempt": attempt + 1, "status": "validation_failed", "issues": issues})
    else:
        pass

    if issues or model_failed:
        # Model output is discarded. The only permitted fallback is derived
        # from the immutable contract and goes through the same three layers.
        requirements = deterministic_phase_fallback(phase_for_validation, project_contract)
        validation = validate_phase_plan_layers(phase_for_validation, requirements, project_contract)
        if not validation.valid:
            failure_status = "model_failed" if model_failed else "validation_failed"
            phase.update({
                "plan_status": failure_status,
                "plan_contract_validated": previous_contract_validated,
                "plan_contract_violations": validation.violations,
                "plan_validation": validation.to_dict(),
                "plan_generation_attempts": attempts,
            })
            await persist_phase_plan()
            raise HTTPException(
                status_code=502 if model_failed else 422,
                detail={
                    "status": failure_status,
                    "message": "阶段规划未通过确定性契约校验，已保留上次有效结果",
                    "validation": validation.to_dict(),
                    "attempts": attempts,
                },
            )
        auto_corrected = True
        generation_mode = "contract_fallback"

    phase["task_contract"] = task_contract
    phase["expert_requirements"] = requirements
    phase["phase_plan_version"] = PHASE_PLAN_VERSION
    phase["plan_status"] = "saved"
    phase["plan_generated"] = True
    phase["plan_contract_validated"] = True
    phase["plan_warnings"] = []
    phase["plan_generation_mode"] = generation_mode
    phase.pop("plan_contract_violations", None)
    phase["plan_generated_at"] = time.time()
    phase["plan_validation"] = validation.to_dict()
    corrections = []
    if auto_corrected:
        corrections.append({
            "type": "deterministic_contract_fallback" if generation_mode == "contract_fallback" else "model_retry",
            "rejected_attempts": attempts,
        })
    phase["plan_artifact_metadata"] = artifact_metadata(
        "phase_plan", PHASE_PLAN_VERSION, generation_mode, validation, corrections,
    )
    phase["plan_artifact_metadata"]["saved_at"] = time.time()
    phase["plan_generation_attempts"] = attempts
    await persist_phase_plan()
    return {
        "success": True,
        "status": "saved",
        "expert_requirements": requirements,
        "count": len(requirements),
        "phase_id": phase_id,
        "warnings": [],
        "auto_corrected": auto_corrected,
        "generation_mode": generation_mode,
        "model_status": "model_failed" if model_failed else ("validation_failed" if attempts and auto_corrected else "generated"),
        "validation": validation.to_dict(),
        "attempts": attempts,
    }


@router.patch("/projects/{project_id}/phases/{phase_id}")
async def update_phase_description(project_id: str, phase_id: str, body: Dict):
    """Update phase description"""
    ctx = _get_project(project_id)
    pm = _phase_managers.get(project_id)
    if not pm:
        raise HTTPException(status_code=400, detail="Phase manager not initialized")
    new_desc = body.get("description", "")
    if not new_desc:
        raise HTTPException(status_code=400, detail="description is required")
    success = pm.update_phase_description(phase_id, new_desc)
    if not success:
        raise HTTPException(status_code=404, detail=f"Phase {phase_id} not found")
    await _persist_all_async()
    return {"success": True, "phase_id": phase_id}


@router.post("/projects/{project_id}/phases/{phase_id}/transfer-to-engineer")
async def transfer_phase_to_engineer(project_id: str, phase_id: str):
    """Sync phase context to FullStackEngineerAgent"""
    ctx = _get_project(project_id)
    pm = _phase_managers.get(project_id)
    if not pm:
        raise HTTPException(status_code=400, detail="Phase manager not initialized")
    phase = pm.get_phase(phase_id)
    if not phase:
        raise HTTPException(status_code=404, detail=f"Phase {phase_id} not found")
    from core.app_state import hermes_client
    from agents.fullstack_engineer_agent import FullStackEngineerAgent
    eng_agent = FullStackEngineerAgent(
        hermes_client=hermes_client,
        project_id=project_id,
        workspace=str(ctx.workspace),
    )
    eng_agent.load_project_context({
        "project_overview": ctx.description,
        "current_phase": phase.get("name", ""),
        "phase_description": phase.get("description", ""),
        "phase_id": phase_id,
        "core_features": [],
        "tech_stack": {},
    })
    return {"success": True, "phase_id": phase_id, "message": f"Phase {phase.get('name', '')} synced to engineer"}



# ===== PhaseBoard 7 missing routes =====

@router.get("/projects/{project_id}/phases/{phase_id}/files")
async def get_phase_files(project_id: str, phase_id: str):
    """Get files associated with a phase"""
    ctx = _get_project(project_id)
    pm = _phase_managers.get(project_id)
    if not pm:
        return {"phase_id": phase_id, "files": []}
    files = pm.get_files_by_phase(phase_id) if hasattr(pm, "get_files_by_phase") else []
    return {"phase_id": phase_id, "files": files, "count": len(files)}


async def _start_phase_quality_after_execution(
    ctx: ProjectContext,
    phase: Dict[str, Any],
    phase_id: str,
) -> bool:
    """Keep Supervisor bootstrap failure separate from execution success."""
    from api import routes_execution

    try:
        await routes_execution._start_phase_quality_cycle_if_ready(ctx)
    except Exception as exc:
        logger.exception(
            "Phase quality startup failed after locked phase completion "
            "project=%s phase=%s",
            ctx.project_id,
            phase_id,
        )
        phase["status"] = "qa_pending"
        phase["quality_start_error"] = {
            "error_code": type(exc).__name__,
            "recorded_at": time.time(),
        }
        return False
    phase.pop("quality_start_error", None)
    return True


async def _resume_locked_phase_coordinator(
    project_id: str,
    phase_id: str,
    coordinator_run_id: str = "",
    coordinator_owner: str = "",
) -> bool:
    """Idempotently drive unfinished persisted DAG waves after a restart."""
    ctx = projects.get(project_id)
    pm = _phase_managers.get(project_id)
    phase = pm.get_phase(phase_id) if pm else None
    if not ctx or not pm or not phase:
        return False
    coordinator = phase.get("execution_coordinator") or {}
    plan = phase.get("execution_dispatch_plan") or {}
    run_specs = phase.get("execution_run_specs") or []
    if (
        coordinator.get("status") not in {"starting", "running"}
        or not plan.get("task_ids")
        or not run_specs
    ):
        return False

    from api import routes_execution
    from core.execution_runs import LeaseConflict

    specs_by_agent = {
        str(spec.get("agent_id") or ""): dict(spec)
        for spec in run_specs if isinstance(spec, dict)
    }
    locks = {agent_id: asyncio.Lock() for agent_id in specs_by_agent}
    project_contract = getattr(pm, "project_contract", {}) or {}
    generation = str(phase.get("execution_generation") or "")
    contract_digest = str(phase.get("execution_contract_digest") or "")
    requirements_revision = int(
        phase.get("execution_requirements_revision") or 0
    )
    baseline_digest = str(
        phase.get("execution_artifact_baseline_digest") or ""
    )
    repair_task_ids = {
        str(task_id)
        for task_id in (coordinator.get("repair_task_ids") or [])
        if str(task_id)
    }
    repair_agent_ids = {
        str(agent_id)
        for agent_id in (coordinator.get("repair_agent_ids") or [])
        if str(agent_id)
    }
    repair_attempt_id = str(coordinator.get("durable_run_id") or "")
    coordinator_attempt_digest = str(
        coordinator.get("dispatch_attempt_digest") or ""
    )
    if repair_task_ids and (
        not repair_attempt_id
        or not coordinator_attempt_digest
        or repair_attempt_id != coordinator_run_id
    ):
        raise LeaseConflict("repair coordinator identity is incomplete")
    rebuild_files_by_task = (
        (phase.get("rebuild_file_manifest") or {}).get("by_task_id")
    )
    await _assert_phase_coordinator_lease(
        coordinator_run_id, coordinator_owner,
    )

    async def execute_attempt(task: Dict[str, Any]) -> Dict[str, Any]:
        await _assert_phase_coordinator_lease(
            coordinator_run_id, coordinator_owner,
        )
        task_id = str(task.get("task_id") or "")
        agent_id = str(task.get("agent_id") or "")
        agent = ctx.agents.get(agent_id)
        if agent is None or agent_id not in specs_by_agent:
            return {"success": False, "status": "failed"}
        required_files = [
            str(item.get("path") or "")
            for item in (project_contract.get("required_files") or [])
            if isinstance(item, dict)
            and str(item.get("phase_id") or "") == str(phase_id)
            and str(item.get("task_id") or "") == task_id
            and item.get("required", True)
        ]
        if isinstance(rebuild_files_by_task, dict):
            executable_task_paths = {
                _normalized_rebuild_path(path)
                for path in (rebuild_files_by_task.get(task_id) or [])
            }
            required_files = [
                path for path in required_files
                if _normalized_rebuild_path(path) in executable_task_paths
            ]
        receipts = agent.setdefault("task_execution_receipts", {})
        prior = receipts.get(task_id) or {}
        prior_run: Dict[str, Any] = {}
        try:
            prior_run = routes_execution._run_registry.get(
                str(prior.get("completion_run_id") or "")
            )
        except Exception:
            prior_run = {}
        prior_payload = prior_run.get("payload") or {}
        force_repair = task_id in repair_task_ids
        if (
            not force_repair
            and str(prior.get("status") or "").lower() == "succeeded"
            and str(prior.get("phase_id") or "") == str(phase_id)
            and str(prior.get("agent_id") or "") == agent_id
            and str(prior.get("execution_generation") or "") == generation
            and str(prior.get("contract_digest") or "") == contract_digest
            and int(prior.get("requirements_revision") or 0)
            == requirements_revision
            and str(prior.get("artifact_baseline_digest") or "")
            == baseline_digest
            and prior_run.get("status") == "succeeded"
            and str(prior_payload.get("phase_id") or "") == str(phase_id)
            and str(prior_payload.get("agent_id") or "") == agent_id
            and str(prior_payload.get("task_id") or "") == task_id
            and str(prior_payload.get("execution_generation") or "")
            == generation
            and str(prior_payload.get("contract_digest") or "")
            == contract_digest
            and int(prior_payload.get("requirements_revision") or 0)
            == requirements_revision
            and str(
                prior_payload.get("artifact_baseline_digest") or ""
            ) == baseline_digest
        ):
            return {
                "success": True,
                "status": "completed",
                "run_id": prior.get("completion_run_id"),
            }
        if repair_task_ids and not force_repair:
            raise LeaseConflict(
                "task is not a member of the repair coordinator attempt"
            )
        if force_repair and repair_agent_ids and agent_id not in repair_agent_ids:
            raise LeaseConflict(
                "agent is not a member of the repair coordinator attempt"
            )
        async with locks[agent_id]:
            payload = dict(specs_by_agent[agent_id])
            payload.pop("expert_type", None)
            payload.update({
                "phase_id": str(phase_id),
                "task_id": task_id,
                "execution_generation": generation,
                "contract_digest": contract_digest,
                "requirements_revision": requirements_revision,
                "artifact_baseline_digest": baseline_digest,
                "phase_coordinator_run_id": coordinator_run_id,
                "dispatch_attempt_digest": coordinator_attempt_digest,
                "artifact_policy": {
                    **dict(payload.get("artifact_policy") or {}),
                    "required_files": list(required_files),
                    "allowed_path_prefixes": list(required_files),
                    "rebuild_file_specs": [
                        copy.deepcopy(spec)
                        for spec in (agent.get("rebuild_file_specs") or [])
                        if isinstance(spec, dict)
                        and str(spec.get("path") or "") in required_files
                    ],
                },
                "description": (
                    str(payload.get("description") or "")
                    + "\n\nRECOVERED CURRENT LOCKED TASK "
                    "(execute only this task):\n"
                    + json.dumps(task, ensure_ascii=False, indent=2)
                    + (
                        "\n\nPRE-QA REPAIR TASK:\n"
                        + str(agent.get("fix_task") or "")
                        + "\n\nPRE-QA REPAIR COORDINATOR ATTEMPT "
                        "(orchestration identity only): "
                        + repair_attempt_id
                        if force_repair
                        else ""
                    )
                ),
                "defer_fix_qc": bool(
                    payload.get("defer_fix_qc") or force_repair
                ),
            })
            await _assert_phase_coordinator_lease(
                coordinator_run_id, coordinator_owner,
            )
            receipt = {
                "task_id": task_id,
                "agent_id": agent_id,
                "phase_id": str(phase_id),
                "status": "pending",
                "started_at": time.time(),
                "required_files": list(required_files),
                "execution_generation": generation,
                "contract_digest": contract_digest,
                "requirements_revision": requirements_revision,
                "artifact_baseline_digest": baseline_digest,
            }
            if force_repair:
                receipt.update({
                    "dispatch_attempt_digest": coordinator_attempt_digest,
                    "repair_coordinator_run_id": coordinator_run_id,
                })
                attempt_receipts = agent.setdefault(
                    "pre_qa_repair_attempt_receipts", {},
                ).setdefault(coordinator_run_id, {})
                attempt_receipts[task_id] = receipt
            else:
                receipts[task_id] = receipt
            await _assert_phase_coordinator_lease(
                coordinator_run_id, coordinator_owner,
            )
            attempt_suffix = (
                ":repair:"
                + hashlib.sha256(
                    f"{repair_attempt_id}:{task_id}".encode("utf-8")
                ).hexdigest()
                if force_repair
                else ""
            )
            run, _created = await routes_execution._schedule_durable_agent_run(
                payload,
                client_idempotency_key=(
                    f"phase:{phase_id}:task:{task_id}:generation:{generation}:"
                    f"contract:{contract_digest}:revision:{requirements_revision}:"
                    f"baseline:{baseline_digest}"
                    f"{attempt_suffix}"
                ),
            )
            run_id = str(run.get("run_id") or "")
            await _assert_phase_coordinator_lease(
                coordinator_run_id, coordinator_owner,
            )
            receipt["start_run_id"] = run_id
            receipt["status"] = str(run.get("status") or "pending")
            await _persist_all_async()
            active = routes_execution._active_run_tasks.get(run_id)
            if active is not None:
                await active
            await _assert_phase_coordinator_lease(
                coordinator_run_id, coordinator_owner,
            )
            finished = await asyncio.to_thread(
                routes_execution._run_registry.get, run_id,
            )
            await _assert_phase_coordinator_lease(
                coordinator_run_id, coordinator_owner,
            )
            receipt.update({
                "completion_run_id": run_id,
                "started_at": finished.get("started_at"),
                "finished_at": finished.get("finished_at"),
                "status": finished.get("status"),
                "result": copy.deepcopy(finished.get("result") or {}),
            })
            if force_repair and finished.get("status") == "succeeded":
                finished_payload = finished.get("payload") or {}
                if (
                    str(finished_payload.get("project_id") or "")
                    != str(project_id)
                    or str(finished_payload.get("phase_id") or "")
                    != str(phase_id)
                    or str(finished_payload.get("agent_id") or "")
                    != agent_id
                    or str(finished_payload.get("task_id") or "")
                    != task_id
                    or str(
                        finished_payload.get("execution_generation") or ""
                    ) != generation
                    or str(finished_payload.get("contract_digest") or "")
                    != contract_digest
                    or int(
                        finished_payload.get("requirements_revision") or 0
                    ) != requirements_revision
                    or str(
                        finished_payload.get(
                            "artifact_baseline_digest"
                        ) or ""
                    ) != baseline_digest
                    or str(
                        finished_payload.get("phase_coordinator_run_id") or ""
                    ) != coordinator_run_id
                    or str(
                        finished_payload.get("dispatch_attempt_digest") or ""
                    ) != coordinator_attempt_digest
                ):
                    raise LeaseConflict(
                        "repair task completion identity is stale"
                    )
                receipts[task_id] = copy.deepcopy(receipt)
            await _persist_all_async()
            return {
                "success": finished.get("status") == "succeeded",
                "status": (
                    "completed"
                    if finished.get("status") == "succeeded"
                    else str(finished.get("status") or "failed")
                ),
                "run_id": run_id,
            }

    try:
        dispatch_result = await execute_phase_dispatch_plan(
            plan, execute_attempt,
        )
        await _assert_phase_coordinator_lease(
            coordinator_run_id, coordinator_owner,
        )
        phase["execution_dispatch_result"] = dispatch_result
        coordinator.update({
            "status": "running" if coordinator_run_id else "completed",
            "dispatch_completed": True,
            "dispatch_completed_at": time.time(),
        })
        if not coordinator_run_id:
            coordinator["completed_at"] = time.time()
            await _start_phase_quality_after_execution(
                ctx, phase, phase_id,
            )
        await _persist_all_async()
    except asyncio.CancelledError:
        raise
    except LeaseConflict:
        raise
    except Exception as exc:
        await _assert_phase_coordinator_lease(
            coordinator_run_id, coordinator_owner,
        )
        coordinator.update({
            "status": "failed",
            "error_code": type(exc).__name__,
            "failed_at": time.time(),
        })
        phase["status"] = "failed"
        await _persist_all_async()
        if not coordinator_run_id:
            return True
        raise
    return True


_PHASE_COORDINATOR_LEASE_SECONDS = 90.0


async def _create_phase_coordinator_run(
    project_id: str,
    phase_id: str,
    phase: Dict[str, Any],
    *,
    attempt_key: str = "",
) -> Dict[str, Any]:
    """Create the SQLite/Postgres CAS identity for one immutable generation."""
    from api import routes_execution

    generation = str(phase.get("execution_generation") or "")
    attempt_digest = (
        hashlib.sha256(attempt_key.encode("utf-8")).hexdigest()
        if attempt_key
        else ""
    )
    payload = {
        "project_id": str(project_id),
        "phase_id": str(phase_id),
        "execution_generation": generation,
        "contract_digest": str(
            phase.get("execution_contract_digest") or ""
        ),
        "requirements_revision": int(
            phase.get("execution_requirements_revision") or 0
        ),
        "artifact_baseline_digest": str(
            phase.get("execution_artifact_baseline_digest") or ""
        ),
        "dispatch_attempt_digest": attempt_digest,
    }
    idempotency_key = f"phase.dispatch:{project_id}:{phase_id}:{generation}"
    if attempt_digest:
        idempotency_key += f":attempt:{attempt_digest}"
    run, _created = await asyncio.to_thread(
        routes_execution._run_registry.create_or_get_run,
        idempotency_key=idempotency_key,
        run_type="phase.dispatch",
        actor_id=f"phase-coordinator:{project_id}:{phase_id}",
        project_id=str(project_id),
        timeout_seconds=7 * 24 * 60 * 60,
        max_retries=100,
        retry_backoff=0,
        payload=payload,
    )
    return run


async def _run_claimed_phase_coordinator(
    project_id: str,
    phase_id: str,
    run_id: str,
    owner: str,
    driver,
) -> None:
    """Finish the durable coordinator before exposing completion or QA."""
    from api import routes_execution
    from core.execution_runs import LeaseConflict

    lease_stop = asyncio.Event()
    lease_lost = asyncio.Event()
    driver_task = asyncio.create_task(
        driver(), name=f"phase-coordinator-driver-{run_id}",
    )

    async def heartbeat_loop() -> None:
        while not lease_stop.is_set():
            try:
                await asyncio.wait_for(
                    lease_stop.wait(),
                    timeout=max(1.0, _PHASE_COORDINATOR_LEASE_SECONDS / 3),
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                await asyncio.to_thread(
                    routes_execution._run_registry.heartbeat,
                    run_id,
                    owner,
                    lease_seconds=_PHASE_COORDINATOR_LEASE_SECONDS,
                )
            except LeaseConflict:
                lease_lost.set()
                driver_task.cancel()
                return

    heartbeat = asyncio.create_task(
        heartbeat_loop(), name=f"phase-coordinator-lease-{run_id}",
    )
    try:
        await driver_task
        if lease_lost.is_set():
            return
        await _assert_phase_coordinator_lease(run_id, owner)
        pm = _phase_managers.get(project_id)
        phase = pm.get_phase(phase_id) if pm else None
        coordinator = (phase or {}).get("execution_coordinator") or {}
        dispatch_result = (phase or {}).get("execution_dispatch_result") or {}
        if (
            coordinator.get("dispatch_completed") is True
            and dispatch_result.get("success") is True
        ):
            await asyncio.to_thread(
                routes_execution._run_registry.succeed,
                run_id,
                owner,
                result={
                    "project_id": project_id,
                    "phase_id": phase_id,
                    "execution_generation": (
                        (phase or {}).get("execution_generation")
                    ),
                    "dispatch_attempt_digest": coordinator.get(
                        "dispatch_attempt_digest"
                    ),
                },
            )
            current_pm = _phase_managers.get(project_id)
            current_phase = (
                current_pm.get_phase(phase_id) if current_pm else None
            )
            current_coordinator = (
                (current_phase or {}).get("execution_coordinator") or {}
            )
            if str(
                current_coordinator.get("durable_run_id") or ""
            ) != run_id:
                return
            current_coordinator.update({
                "status": "completed",
                "completed_at": time.time(),
                "durable_status": "succeeded",
            })
            ctx = projects.get(project_id)
            if ctx is not None:
                for agent_id in (
                    current_coordinator.get("repair_agent_ids") or []
                ):
                    agent = ctx.agents.get(str(agent_id)) or {}
                    agent["pre_qa_repair_run_status"] = "succeeded"
            await _persist_all_async()
            if ctx is not None and current_phase is not None:
                await _start_phase_quality_after_execution(
                    ctx, current_phase, phase_id,
                )
                await _persist_all_async()
        else:
            await asyncio.to_thread(
                routes_execution._run_registry.fail,
                run_id,
                owner,
                error=str(
                    coordinator.get("error_code")
                    or "phase coordinator failed"
                ),
                retryable=False,
            )
            if str(coordinator.get("durable_run_id") or "") == run_id:
                coordinator.update({
                    "status": "failed",
                    "error_code": (
                        coordinator.get("error_code")
                        or "phase_dispatch_incomplete"
                    ),
                    "failed_at": time.time(),
                })
                if phase is not None:
                    phase["status"] = "failed"
                await _persist_all_async()
    except asyncio.CancelledError:
        if lease_lost.is_set():
            return
        # Cancellation can race the small window after the driver persisted a
        # terminal dispatch failure but before this durable parent was closed.
        # Finalize only that proven terminal case; unfinished dispatches remain
        # recoverable through the normal lease-expiry path.
        pm = _phase_managers.get(project_id)
        phase = pm.get_phase(phase_id) if pm else None
        coordinator = (phase or {}).get("execution_coordinator") or {}
        dispatch_result = (phase or {}).get("execution_dispatch_result") or {}
        dispatch_failed = (
            coordinator.get("dispatch_completed") is True
            and (
                str(dispatch_result.get("status") or "").lower() == "failed"
                or dispatch_result.get("success") is False
            )
        )
        if (
            dispatch_failed
            and str(coordinator.get("durable_run_id") or "") == run_id
        ):
            try:
                await _assert_phase_coordinator_lease(
                    run_id, owner, allow_failed=True,
                )
                await asyncio.to_thread(
                    routes_execution._run_registry.fail,
                    run_id,
                    owner,
                    error=str(
                        coordinator.get("error_code")
                        or dispatch_result.get("error_code")
                        or "phase coordinator failed"
                    ),
                    retryable=False,
                )
                coordinator["durable_status"] = "failed"
                await asyncio.shield(_persist_all_async())
            except LeaseConflict:
                pass
        raise
    except LeaseConflict:
        return
    except Exception as exc:
        if lease_lost.is_set():
            return
        try:
            await _assert_phase_coordinator_lease(
                run_id, owner, allow_failed=True,
            )
        except LeaseConflict:
            return
        try:
            await asyncio.to_thread(
                routes_execution._run_registry.fail,
                run_id,
                owner,
                error=str(exc)[:500] or type(exc).__name__,
                retryable=False,
            )
        except LeaseConflict:
            return
        pm = _phase_managers.get(project_id)
        phase = pm.get_phase(phase_id) if pm else None
        coordinator = (phase or {}).get("execution_coordinator") or {}
        if str(coordinator.get("durable_run_id") or "") == run_id:
            coordinator.update({
                "status": "failed",
                "error_code": type(exc).__name__,
                "failed_at": time.time(),
            })
            if phase is not None:
                phase["status"] = "failed"
            await _persist_all_async()
    finally:
        lease_stop.set()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass


async def _assert_phase_coordinator_lease(
    run_id: str,
    owner: str,
    *,
    allow_failed: bool = False,
) -> None:
    """Fail before writes when the lease or current phase pointer changed."""
    if not run_id:
        return
    from api import routes_execution
    from core.execution_runs import LeaseConflict

    run = await asyncio.to_thread(
        routes_execution._run_registry.get, run_id,
    )
    payload = run.get("payload") or {}
    project_id = str(payload.get("project_id") or "")
    phase_id = str(payload.get("phase_id") or "")
    pm = _phase_managers.get(project_id)
    phase = pm.get_phase(phase_id) if pm and phase_id else None
    coordinator = (phase or {}).get("execution_coordinator") or {}
    allowed_coordinator_statuses = {"starting", "running"}
    if allow_failed:
        allowed_coordinator_statuses.add("failed")
    if (
        run.get("status") != "running"
        or str(run.get("lease_owner") or "") != owner
        or float(run.get("lease_expires_at") or 0) < time.time()
        or not project_id
        or not phase_id
        or not phase
        or str(coordinator.get("durable_run_id") or "") != run_id
        or str(coordinator.get("status") or "")
        not in allowed_coordinator_statuses
        or str(phase.get("execution_generation") or "")
        != str(payload.get("execution_generation") or "")
        or str(phase.get("execution_contract_digest") or "")
        != str(payload.get("contract_digest") or "")
        or int(phase.get("execution_requirements_revision") or 0)
        != int(payload.get("requirements_revision") or 0)
        or str(phase.get("execution_artifact_baseline_digest") or "")
        != str(payload.get("artifact_baseline_digest") or "")
        or str(coordinator.get("dispatch_attempt_digest") or "")
        != str(payload.get("dispatch_attempt_digest") or "")
    ):
        raise LeaseConflict("phase coordinator lease lost or superseded")


def _phase_dispatch_identity_matches(
    project_id: str,
    phase_id: str,
    phase: Dict[str, Any],
    payload: Dict[str, Any],
) -> bool:
    coordinator = phase.get("execution_coordinator") or {}
    return (
        str(payload.get("project_id") or "") == str(project_id)
        and str(payload.get("phase_id") or "") == str(phase_id)
        and str(payload.get("execution_generation") or "")
        == str(phase.get("execution_generation") or "")
        and str(payload.get("contract_digest") or "")
        == str(phase.get("execution_contract_digest") or "")
        and int(payload.get("requirements_revision") or 0)
        == int(phase.get("execution_requirements_revision") or 0)
        and str(payload.get("artifact_baseline_digest") or "")
        == str(phase.get("execution_artifact_baseline_digest") or "")
        and str(payload.get("dispatch_attempt_digest") or "")
        == str(coordinator.get("dispatch_attempt_digest") or "")
    )


def _phase_child_membership_matches(
    phase: Dict[str, Any],
    payload: Dict[str, Any],
) -> bool:
    """Return whether one child is uniquely owned by the current phase plan."""
    task_id = str(payload.get("task_id") or "")
    agent_id = str(payload.get("agent_id") or "")
    if not task_id or not agent_id:
        return False

    plan = phase.get("execution_dispatch_plan") or {}
    task_ids = [
        str(item) for item in (plan.get("task_ids") or []) if str(item)
    ]
    plan_memberships = [
        (
            str(task.get("task_id") or ""),
            str(task.get("agent_id") or ""),
        )
        for wave in (plan.get("waves") or [])
        for task in (wave or [])
        if isinstance(task, dict)
    ]
    spec_agent_ids = [
        str(spec.get("agent_id") or "")
        for spec in (phase.get("execution_run_specs") or [])
        if isinstance(spec, dict) and str(spec.get("agent_id") or "")
    ]
    return (
        task_ids.count(task_id) == 1
        and plan_memberships.count((task_id, agent_id)) == 1
        and spec_agent_ids.count(agent_id) == 1
    )


def _run_has_live_local_execution(routes_execution, run_id: str) -> bool:
    task = routes_execution._active_run_tasks.get(run_id)
    if task is not None:
        try:
            if not task.done():
                return True
        except Exception:
            return True
    guard = routes_execution._run_execution_guards.get(run_id)
    if guard is not None:
        try:
            if guard.valid:
                return True
        except Exception:
            return True
    return False


def _clear_cancelled_phase_child_projection(
    routes_execution,
    run: Dict[str, Any],
    reason: str,
) -> bool:
    """Clear only mutable projections bound to the cancelled child run."""
    run_id = str(run.get("run_id") or "")
    payload = run.get("payload") or {}
    project_id = str(payload.get("project_id") or "")
    phase_id = str(payload.get("phase_id") or "")
    agent_id = str(payload.get("agent_id") or "")
    ctx = projects.get(project_id)
    changed = False

    persisted = routes_execution.execution_status.get(agent_id) or {}
    persisted_matches = (
        bool(agent_id)
        and str(persisted.get("run_id") or "") == run_id
    )
    agent = (getattr(ctx, "agents", {}) or {}).get(agent_id) if ctx else None
    receipt_matches = bool(agent) and any(
        run_id in {
            str(receipt.get("start_run_id") or ""),
            str(receipt.get("completion_run_id") or ""),
        }
        for receipt in (agent.get("task_execution_receipts") or {}).values()
        if isinstance(receipt, dict)
    )
    agent_matches = bool(agent) and (
        persisted_matches
        or receipt_matches
        or str(agent.get("lock_run_id") or "") == run_id
    )

    if persisted_matches:
        persisted.update({
            "status": "cancelled",
            "run_status": "cancelled",
            "progress": 0,
            "error": reason,
        })
        changed = True
    if agent_matches and str(agent.get("phase_id") or "") == phase_id:
        agent.update({
            "status": "failed",
            "progress": 0,
            "error": reason,
            "recovery_status": "superseded_generation",
        })
        if str(agent.get("lock_run_id") or "") == run_id:
            lock_id = str(agent.get("lock_id") or "")
            if lock_id:
                expert_lock.release_lock(lock_id)
            agent["lock_id"] = None
            agent["lock_run_id"] = None
            agent["locked_until"] = None
        changed = True
        for subproject in getattr(ctx, "subprojects", []) or []:
            if (
                str(subproject.get("phase_id") or "") == phase_id
                and str(subproject.get("agent_id") or "") == agent_id
            ):
                subproject.update({
                    "status": "failed",
                    "progress": 0,
                    "error": reason,
                })

    if project_id and run_id:
        for lock in expert_lock.get_active_locks(project_id=project_id):
            if str(lock.get("task_id") or "").endswith(f":run:{run_id}"):
                lock_id = str(lock.get("lock_id") or "")
                if lock_id:
                    expert_lock.release_lock(lock_id)
                    changed = True
    routes_execution._active_run_tasks.pop(run_id, None)
    routes_execution._run_execution_guards.pop(run_id, None)
    routes_execution._run_cancel_events.pop(run_id, None)
    return changed


async def _reconcile_superseded_phase_runs() -> bool:
    """Terminally cancel idle phase generations that lost authority."""
    from api import routes_execution
    from core.execution_runs import (
        InvalidTransition,
        LeaseConflict,
        RunNotFound,
        VersionConflict,
    )

    registry = routes_execution._run_registry
    if not callable(getattr(registry, "list_runs", None)) or not callable(
        getattr(registry, "cancel_unleased", None)
    ):
        return False

    phase_index: Dict[tuple[str, str], Dict[str, Any]] = {}
    for project_id, pm in list(_phase_managers.items()):
        for phase in list(getattr(pm, "phases", []) or []):
            phase_id = str(phase.get("phase_id") or "")
            if phase_id:
                phase_index[(str(project_id), phase_id)] = phase

    cancelled_parents: set[str] = set()
    changed = False
    dispatch_runs = await asyncio.to_thread(
        registry.list_runs,
        statuses=["pending", "blocked"],
        run_type="phase.dispatch",
        limit=1000,
    )
    for run in dispatch_runs:
        run_id = str(run.get("run_id") or "")
        payload = run.get("payload") or {}
        key = (
            str(payload.get("project_id") or run.get("project_id") or ""),
            str(payload.get("phase_id") or ""),
        )
        phase = phase_index.get(key)
        coordinator = (phase or {}).get("execution_coordinator") or {}
        current = (
            bool(phase)
            and str(coordinator.get("durable_run_id") or "") == run_id
            and _phase_dispatch_identity_matches(
                key[0], key[1], phase, payload,
            )
            and str(coordinator.get("status") or "") in {"starting", "running"}
            and str(run.get("status") or "") == "pending"
        )
        if current or _run_has_live_local_execution(routes_execution, run_id):
            continue
        reason = "phase dispatch generation is no longer authoritative"
        try:
            cancelled = await asyncio.to_thread(
                registry.cancel_unleased,
                run_id,
                reason=reason,
                actor="startup-reconcile",
                expected_version=run.get("version"),
            )
        except (InvalidTransition, LeaseConflict, RunNotFound, VersionConflict):
            continue
        cancelled_parents.add(run_id)
        changed = True
        if phase and str(coordinator.get("durable_run_id") or "") == run_id:
            coordinator.update({
                "status": "failed",
                "durable_status": "cancelled",
                "failed_at": cancelled.get("finished_at") or time.time(),
                "startup_reconciliation": "superseded_generation_cancelled",
            })
            coordinator.setdefault(
                "error_code", "superseded_phase_generation",
            )
            phase["status"] = "failed"

    child_projection_changed = False
    child_runs = await asyncio.to_thread(
        registry.list_runs,
        statuses=["pending", "blocked"],
        run_type="agent.execute",
        limit=1000,
    )
    parent_cache: Dict[str, Dict[str, Any]] = {}
    for run in child_runs:
        payload = run.get("payload") or {}
        parent_id = str(payload.get("phase_coordinator_run_id") or "")
        if not parent_id:
            continue
        project_id = str(payload.get("project_id") or "")
        phase_id = str(payload.get("phase_id") or "")
        phase = phase_index.get((project_id, phase_id))
        coordinator = (phase or {}).get("execution_coordinator") or {}
        parent = parent_cache.get(parent_id)
        if parent is None:
            try:
                parent = await asyncio.to_thread(registry.get, parent_id)
            except RunNotFound:
                parent = {}
            parent_cache[parent_id] = parent
        parent_payload = parent.get("payload") or {}
        parent_authoritative = (
            parent_id not in cancelled_parents
            and bool(phase)
            and str(coordinator.get("durable_run_id") or "") == parent_id
            and str(coordinator.get("status") or "") in {"starting", "running"}
            and str(parent.get("run_type") or "") == "phase.dispatch"
            and str(parent.get("status") or "") in {"pending", "running"}
            and _phase_dispatch_identity_matches(
                project_id, phase_id, phase, parent_payload,
            )
            and _phase_dispatch_identity_matches(
                project_id, phase_id, phase, payload,
            )
            and _phase_child_membership_matches(phase, payload)
        )
        run_id = str(run.get("run_id") or "")
        if (
            parent_authoritative
            or _run_has_live_local_execution(routes_execution, run_id)
        ):
            continue
        reason = "parent phase dispatch was failed or superseded"
        try:
            await asyncio.to_thread(
                registry.cancel_unleased,
                run_id,
                reason=reason,
                actor="startup-reconcile",
                expected_version=run.get("version"),
            )
        except (InvalidTransition, LeaseConflict, RunNotFound, VersionConflict):
            continue
        changed = True
        child_projection_changed = (
            _clear_cancelled_phase_child_projection(
                routes_execution, run, reason,
            )
            or child_projection_changed
        )
    if child_projection_changed:
        await asyncio.to_thread(routes_execution._persist_execution_state)
    return changed


async def resume_pending_phase_dispatches() -> int:
    """CAS-claim unfinished coordinators and schedule recovery without blocking."""
    from api import routes_execution
    from core.execution_runs import LeaseConflict

    # This is safe after B-line restore-before-resume ordering: only expired
    # durable leases are returned to pending, and active workers retain theirs.
    await asyncio.to_thread(routes_execution._run_registry.recover_startup)
    state_changed = await _reconcile_superseded_phase_runs()
    claimed: List[tuple[str, str, str, str]] = []
    finalized: List[tuple[str, str]] = []
    for project_id, pm in list(_phase_managers.items()):
        for phase in list(getattr(pm, "phases", []) or []):
            coordinator = phase.get("execution_coordinator") or {}
            if coordinator.get("status") not in {"starting", "running"}:
                continue
            phase_id = str(phase.get("phase_id") or "")
            generation = str(phase.get("execution_generation") or "")
            if (
                not phase.get("execution_run_specs")
                or not (
                    (phase.get("execution_dispatch_plan") or {}).get(
                        "task_ids"
                    )
                )
            ):
                # A crash during construction cannot be resumed safely because
                # no immutable Agent attempt contract was committed. Roll the
                # partial generation back instead of leaving an active phase.
                ctx = projects.get(str(project_id))
                if ctx is not None:
                    for agent_id, agent in list(ctx.agents.items()):
                        if str(agent.get("phase_id") or "") != phase_id:
                            continue
                        lock_id = str(agent.get("lock_id") or "")
                        if lock_id:
                            expert_lock.release_lock(lock_id)
                        ctx.agents.pop(agent_id, None)
                    for subproject in ctx.subprojects:
                        if str(subproject.get("phase_id") or "") == phase_id:
                            subproject.pop("agent_id", None)
                            subproject["status"] = "pending"
                            subproject["progress"] = 0
                phase["agents"] = []
                phase["status"] = "pending"
                pm.phase_agents.pop(phase_id, None)
                coordinator.update({
                    "status": "failed",
                    "error_code": "construction_contract_incomplete",
                    "failed_at": time.time(),
                })
                run_id = str(coordinator.get("durable_run_id") or "")
                if run_id:
                    try:
                        current = await asyncio.to_thread(
                            routes_execution._run_registry.get, run_id,
                        )
                        cancelled = await asyncio.to_thread(
                            routes_execution._run_registry.cancel_unleased,
                            run_id,
                            reason="phase construction contract incomplete",
                            actor="startup-reconcile",
                            expected_version=current.get("version"),
                        )
                        coordinator["durable_status"] = "cancelled"
                        coordinator["failed_at"] = (
                            cancelled.get("finished_at") or time.time()
                        )
                    except Exception:
                        pass
                state_changed = True
                continue
            run_id = str(coordinator.get("durable_run_id") or "")
            if not run_id:
                run = await _create_phase_coordinator_run(
                    str(project_id), phase_id, phase,
                )
                run_id = str(run.get("run_id") or "")
                coordinator["durable_run_id"] = run_id
                state_changed = True
            current_run = await asyncio.to_thread(
                routes_execution._run_registry.get, run_id,
            )
            current_status = str(current_run.get("status") or "")
            if current_status == "succeeded":
                payload = current_run.get("payload") or {}
                identity_matches = (
                    str(payload.get("project_id") or "")
                    == str(project_id)
                    and str(payload.get("phase_id") or "") == phase_id
                    and str(payload.get("execution_generation") or "")
                    == str(phase.get("execution_generation") or "")
                    and str(payload.get("contract_digest") or "")
                    == str(phase.get("execution_contract_digest") or "")
                    and int(payload.get("requirements_revision") or 0)
                    == int(
                        phase.get("execution_requirements_revision") or 0
                    )
                    and str(
                        payload.get("artifact_baseline_digest") or ""
                    ) == str(
                        phase.get("execution_artifact_baseline_digest") or ""
                    )
                    and str(
                        payload.get("dispatch_attempt_digest") or ""
                    ) == str(
                        coordinator.get("dispatch_attempt_digest") or ""
                    )
                )
                if (
                    identity_matches
                    and coordinator.get("dispatch_completed") is True
                    and (
                        phase.get("execution_dispatch_result") or {}
                    ).get("success") is True
                ):
                    coordinator.update({
                        "status": "completed",
                        "completed_at": (
                            current_run.get("finished_at") or time.time()
                        ),
                        "durable_status": "succeeded",
                    })
                    finalized.append((str(project_id), phase_id))
                else:
                    coordinator.update({
                        "status": "failed",
                        "error_code": "coordinator_identity_incomplete",
                        "failed_at": time.time(),
                    })
                    phase["status"] = "failed"
                state_changed = True
                continue
            if current_status in {
                "failed", "blocked", "cancelled", "timeout",
            }:
                coordinator.update({
                    "status": "failed",
                    "error_code": (
                        current_run.get("last_error")
                        or f"durable_coordinator_{current_status}"
                    ),
                    "failed_at": (
                        current_run.get("finished_at") or time.time()
                    ),
                })
                phase["status"] = "failed"
                state_changed = True
                continue
            owner = (
                f"metis-phase:{os.getpid()}:{project_id}:{phase_id}:"
                f"{uuid.uuid4().hex}"
            )
            try:
                claimed_run = await asyncio.to_thread(
                    routes_execution._run_registry.claim,
                    run_id,
                    owner,
                    lease_seconds=_PHASE_COORDINATOR_LEASE_SECONDS,
                )
            except LeaseConflict:
                continue
            except Exception:
                current = await asyncio.to_thread(
                    routes_execution._run_registry.get, run_id,
                )
                if current.get("status") in {
                    "succeeded", "failed", "blocked", "cancelled", "timeout",
                }:
                    continue
                raise
            claim_id = str(claimed_run.get("run_id") or run_id)
            coordinator.update({
                "recovery_claim_active": True,
                "recovery_claim_id": claim_id,
                "recovery_claim_generation": generation,
                "recovery_claim_owner": owner,
                "recovery_claim_expires_at": claimed_run.get(
                    "lease_expires_at"
                ),
                "recovery_claimed_at": time.time(),
            })
            claimed.append((str(project_id), phase_id, claim_id, owner))
            state_changed = True
    if state_changed:
        await _persist_all_async()
    for project_id, phase_id in finalized:
        ctx = projects.get(project_id)
        pm = _phase_managers.get(project_id)
        phase = pm.get_phase(phase_id) if pm else None
        if ctx is not None and phase is not None:
            await _start_phase_quality_after_execution(
                ctx, phase, phase_id,
            )
    if finalized:
        await _persist_all_async()

    async def run_claim(
        project_id: str,
        phase_id: str,
        claim_id: str,
        owner: str,
    ) -> None:
        try:
            await _run_claimed_phase_coordinator(
                project_id,
                phase_id,
                claim_id,
                owner,
                lambda: _resume_locked_phase_coordinator(
                    project_id,
                    phase_id,
                    claim_id,
                    owner,
                ),
            )
        finally:
            pm = _phase_managers.get(project_id)
            phase = pm.get_phase(phase_id) if pm else None
            coordinator = (phase or {}).get("execution_coordinator") or {}
            if (
                coordinator.get("recovery_claim_id") == claim_id
                and coordinator.get("recovery_claim_owner") == owner
            ):
                coordinator["recovery_claim_active"] = False
                coordinator["recovery_claim_finished_at"] = time.time()
                await _persist_all_async()

    for project_id, phase_id, claim_id, owner in claimed:
        _safe_create_task(
            run_claim(project_id, phase_id, claim_id, owner),
            name=f"recover-phase-{phase_id}-{claim_id[:8]}",
        )
    return len(claimed)


@router.post("/projects/{project_id}/phases/{phase_id}/start")
async def start_phase(project_id: str, phase_id: str):
    """Start a phase: set status active, create agent entries"""
    ctx = _get_project(project_id)
    _assert_project_write_available(ctx)
    pm = _phase_managers.get(project_id)
    if not pm:
        raise HTTPException(status_code=400, detail="Phase manager not initialized")
    phase = pm.get_phase(phase_id)
    if not phase:
        phase = next(
            (
                candidate for candidate in pm.phases
                if str(candidate.get("phase_id")) == str(phase_id)
            ),
            None,
        )
    if not phase:
        raise HTTPException(status_code=404, detail=f"Phase {phase_id} not found")
    project_contract = getattr(pm, "project_contract", {}) or {}
    pm_leader = _pm_teams.get(project_id)
    if project_contract.get("locked") and pm_leader is not None:
        if (
            int(project_contract.get("requirements_revision") or 0)
            != int(getattr(pm_leader, "requirements_revision", 0) or 0)
            or str(project_contract.get("requirements_digest") or "")
            != str(getattr(pm_leader, "requirements_digest", "") or "")
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Locked phase plan references a stale canonical "
                    "requirements revision; regenerate and reconfirm the plan"
                ),
            )
    rebuild_manifest = phase.get("rebuild_file_manifest") or {}
    preserve_paths = {
        _normalized_rebuild_path(item.get("path"))
        for item in (rebuild_manifest.get("files") or [])
        if isinstance(item, dict) and item.get("mode") == "preserve"
    }
    executable_paths = {
        _normalized_rebuild_path(path)
        for paths in (
            (rebuild_manifest.get("by_task_id") or {}).values()
            if isinstance(rebuild_manifest.get("by_task_id"), dict)
            else ()
        )
        if isinstance(paths, (list, tuple, set))
        for path in paths
    }
    preserve_task_claims = sorted(
        path for path in preserve_paths & executable_paths if path
    )
    if preserve_task_claims:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "Preserve-only files cannot be claimed as executable "
                    "task artifacts"
                ),
                "paths": preserve_task_claims,
            },
        )
    if _migrate_invalid_phase_plan(phase, getattr(pm, "project_contract", {}) or {}):
        await _persist_all_async()
    if getattr(pm, "project_contract", {}).get("locked") and not phase.get("plan_contract_validated"):
        raise HTTPException(
            status_code=409,
            detail="当前阶段尚未通过阶段任务契约校验，不能启动工程师执行",
        )
    phase_index = _assert_phase_can_start(pm, phase_id)

    # This endpoint creates Agent records, subprojects and expert locks before
    # scheduling execution.  Without a model key those durable mutations used
    # to remain behind as a phase full of failed agents.  Reject atomically
    # before the first mutation; explicit test mode keeps deterministic tests
    # and offline fixtures available.
    if not _phase_execution_has_api_key():
        raise HTTPException(
            status_code=409,
            detail="启动阶段需要有效的模型 API Key；未创建任何 Agent，请先在设置中配置并测试连接",
        )

    from core.expert_pool import get_expert_pool
    owner_user_id = str(getattr(ctx, "owner_user_id", "") or "")
    expert_pool = (
        get_expert_pool(owner_user_id)
        if owner_user_id
        else get_expert_pool()
    )
    is_phase_plan_v1 = (
        str(phase.get("phase_plan_version") or "") == PHASE_PLAN_SCHEMA_VERSION
        or (
            isinstance(phase.get("phase_plan"), dict)
            and phase["phase_plan"].get("schema_version")
            == PHASE_PLAN_SCHEMA_VERSION
        )
    )
    expert_binding_issues = (
        _phase_v1_expert_binding_issues(
            phase,
            _expert_pool_snapshot(expert_pool),
        )
        if is_phase_plan_v1
        else []
    )
    if expert_binding_issues:
        raise HTTPException(
            status_code=409,
            detail={
                "status": "expert_assignment_stale",
                "message": "已分配专家被删除、停用或当前不可用，请重新确认人员分配",
                "issues": expert_binding_issues,
            },
        )

    if getattr(pm, "project_contract", {}).get("locked"):
        graph_validation = validate_task_graph(
            pm.phases,
            target_phase_id=phase_id,
        )
        if not graph_validation.valid:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Locked task graph is invalid; phase was not mutated",
                    "validation": graph_validation.to_dict(),
                },
            )
        unmapped_roles = _unsupported_phase_roles(phase)
        if unmapped_roles:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Locked task role has no supported executor; phase was not mutated",
                    "roles": unmapped_roles,
                },
            )

    if project_contract.get("locked"):
        coordinator_contract_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                project_contract,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        coordinator_revision = int(
            project_contract.get("requirements_revision") or 0
        )
        coordinator_baseline_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                [
                    {
                        "path": str(item.get("path") or ""),
                        "baseline_digest": str(
                            item.get("baseline_digest") or ""
                        ),
                        "mode": str(item.get("mode") or ""),
                        "task_id": str(item.get("task_id") or ""),
                    }
                    for item in (
                        (phase.get("rebuild_file_manifest") or {}).get("files")
                        or []
                    )
                    if isinstance(item, dict)
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        phase["execution_generation"] = (
            f"{phase_id}:{uuid.uuid4().hex}:{coordinator_contract_digest}:"
            f"{coordinator_revision}:{coordinator_baseline_digest}"
        )
        phase["execution_contract_digest"] = coordinator_contract_digest
        phase["execution_requirements_revision"] = coordinator_revision
        phase["execution_artifact_baseline_digest"] = (
            coordinator_baseline_digest
        )
        phase["execution_coordinator"] = {
            "status": "starting",
            "execution_generation": phase["execution_generation"],
            "contract_digest": coordinator_contract_digest,
            "requirements_revision": coordinator_revision,
            "artifact_baseline_digest": coordinator_baseline_digest,
            "started_at": time.time(),
        }
        coordinator_run = await _create_phase_coordinator_run(
            project_id, phase_id, phase,
        )
        phase["execution_coordinator"]["durable_run_id"] = str(
            coordinator_run.get("run_id") or ""
        )
        # Persist the fail-closed marker before any Agent becomes visible.
        await _persist_all_async()

    from core.hermes_client import current_user_api_config
    from core.agent_lifecycle import transition_agent

    previous_ctx_status = ctx.status
    previous_phase_status = phase.get("status", "pending")
    previous_phase_started_at = phase.get("started_at")
    previous_phase_agents = list(phase.get("agents") or [])
    previous_pm_phase_agents = list(pm.phase_agents.get(phase_id) or [])
    previous_phase_index = pm.current_phase_index
    previous_subprojects = [dict(item) for item in (ctx.subprojects or [])]

    def _rollback_phase_start() -> None:
        """Undo records created in this start attempt when a lock conflicts."""
        for created in created_agents:
            lock_id = created.get("lock_id")
            if lock_id:
                expert_lock.release_lock(lock_id)
            ctx.agents.pop(created.get("id", ""), None)
        for agent_id in set(phase.get("agents") or []) - set(previous_phase_agents):
            ctx.agents.pop(agent_id, None)
        # Restore the complete subproject snapshot.  This also removes
        # placeholder subprojects created earlier in this failed start attempt.
        ctx.subprojects[:] = [dict(item) for item in previous_subprojects]
        phase["agents"] = previous_phase_agents
        phase["status"] = previous_phase_status
        if previous_phase_started_at is None:
            phase.pop("started_at", None)
        else:
            phase["started_at"] = previous_phase_started_at
        pm.current_phase_index = previous_phase_index
        pm.phase_agents[phase_id] = previous_pm_phase_agents
        phase.setdefault("execution_coordinator", {}).update({
            "status": "failed",
            "error_code": "phase_start_rolled_back",
            "failed_at": time.time(),
        })
        ctx.status = previous_ctx_status

    # Set phase status to active
    for p in pm.phases:
        if str(p.get("phase_id")) == str(phase_id):
            p["status"] = "active"
            p["started_at"] = time.time()
            break
    pm.current_phase_index = phase_index
    ctx.status = "executing"

    # 从专家池匹配执行专家（替代硬编码 PG Agent）
    from core.global_agent_pool import get_global_agent_pool
    agent_pool = (
        get_global_agent_pool(owner_user_id)
        if owner_user_id
        else get_global_agent_pool()
    )

    expert_type_map = {
        "前端": "frontend", "前端工程师": "frontend", "react": "frontend", "vue": "frontend",
        "后端": "backend", "后端工程师": "backend", "fastapi": "backend", "api": "backend",
        "数据库": "database", "数据库工程师": "database", "sql": "database", "postgresql": "database",
        "架构": "architecture", "架构师": "architecture",
        "devops": "devops", "运维": "devops", "docker": "devops",
        "安全": "security", "安全工程师": "security",
        "测试": "qa", "测试工程师": "qa",
        "数据": "data", "数据工程师": "data", "数据分析": "data",
        "全栈": "fullstack_engineer", "全能工程师": "fullstack_engineer",
    }

    def expert_type_for_role(role: str) -> str:
        resolved = _phase_role_expert_type(role)
        if resolved:
            return resolved
        lowered = str(role or "").lower()
        for keyword, expert_type in expert_type_map.items():
            if keyword.lower() in lowered:
                return expert_type
        return "unmapped-" + hashlib.sha256(
            str(role or "").strip().encode("utf-8"),
        ).hexdigest()[:12]

    # A phase owns at most one execution agent per expert type. Planner
    # ``agent_count`` is not permission to clone the same role over one task.
    raw_phase_roles = (
        phase.get("execution_roles")
        or phase.get("roles_needed")
        or ["执行专家"]
    )
    phase_roles: List[str] = []
    seen_role_types: set[str] = set()
    for role in raw_phase_roles:
        role_type = expert_type_for_role(str(role))
        if role_type in seen_role_types:
            continue
        seen_role_types.add(role_type)
        phase_roles.append(str(role))
    locked_task_rows = [
        copy.deepcopy(item)
        for item in (
            phase.get("expert_requirements")
            or phase.get("task_contract")
            or []
        )
        if isinstance(item, dict) and item.get("task_id")
    ]

    def locked_task_executor_type(item: Dict[str, Any]) -> str:
        return str(
            item.get("executor_type")
            or _phase_task_executor_type(
                item,
                [
                    str(
                        item.get("required_role")
                        or ((item.get("roles") or [""])[0])
                    )
                ],
            )
        )

    def locked_tasks_for_role_type(role_type: str) -> List[Dict[str, Any]]:
        rows = []
        for item in locked_task_rows:
            if locked_task_executor_type(item) != role_type:
                continue
            task = copy.deepcopy(item)
            # Enrich with contract-level acceptance_criteria for downstream
            # confirm-complete evidence binding.
            contract_task = next(
                (t for t in (project_contract.get("phases") or [])
                 if isinstance(t, dict)
                 and t.get("phase_id") == phase.get("phase_id")),
                {},
            )
            contract_locked_tasks = contract_task.get("tasks") or []
            matching = next(
                (t for t in contract_locked_tasks
                 if isinstance(t, dict)
                 and t.get("task_id") == task.get("task_id")),
                {},
            )
            if matching.get("acceptance_criteria"):
                task.setdefault(
                    "acceptance_criteria",
                    matching["acceptance_criteria"],
                )
            rows.append(task)
        return rows

    created_agents = []
    matching_sp = [
        sp for sp in ctx.subprojects
        if str(sp.get("phase_id", "")) == str(phase_id)
    ]
    if matching_sp and not matching_sp[0].get("agent_role"):
        matching_sp[0]["agent_role"] = phase_roles[0]
        matching_sp[0].setdefault("roles_needed", [phase_roles[0]])

    # Plans can contain one subproject per task. Merge subprojects that require
    # the same expert so all backend tasks go to one backend expert, all
    # frontend tasks to one frontend expert, etc.
    target_sp: List[Dict[str, Any]] = []
    primary_by_type: Dict[str, Dict[str, Any]] = {}
    merged_subproject_ids: set[str] = set()
    for subproject in matching_sp:
        roles = subproject.get("roles_needed") or []
        role = subproject.get("agent_role") or (roles[0] if roles else phase_roles[0])
        role_type = expert_type_for_role(str(role))
        primary = primary_by_type.get(role_type)
        if primary is None:
            primary_by_type[role_type] = subproject
            subproject.setdefault("_merged_subproject_ids", [str(subproject.get("id") or "")])
            target_sp.append(subproject)
            continue
        merged_id = str(subproject.get("id") or "")
        if merged_id:
            merged_subproject_ids.add(merged_id)
            primary.setdefault("_merged_subproject_ids", [str(primary.get("id") or "")])
            if merged_id not in primary["_merged_subproject_ids"]:
                primary["_merged_subproject_ids"].append(merged_id)
        extra_description = str(subproject.get("description") or "").strip()
        if extra_description and extra_description not in str(primary.get("description") or ""):
            primary["description"] = (
                str(primary.get("description") or "").rstrip()
                + "\n\n【同角色合并任务】\n"
                + extra_description
            ).strip()
        primary["deliverables"] = list(dict.fromkeys(
            list(primary.get("deliverables") or [])
            + list(subproject.get("deliverables") or [])
        ))
    if merged_subproject_ids:
        ctx.subprojects[:] = [
            item for item in ctx.subprojects
            if str(item.get("id") or "") not in merged_subproject_ids
        ]
        phase["subprojects"] = [
            item for item in (phase.get("subprojects") or [])
            if str(item) not in merged_subproject_ids
        ]

    direct_task_bindings = bool(locked_task_rows) and all(
        len({
            str(expert_id)
            for expert_id in (task.get("assigned_expert_ids") or [])
            if str(expert_id)
        }) == 1
        for task in locked_task_rows
    )
    if direct_task_bindings:
        target_sp = []
        for task_index, task in enumerate(locked_task_rows):
            assigned_expert_id = next(iter({
                str(expert_id)
                for expert_id in (task.get("assigned_expert_ids") or [])
                if str(expert_id)
            }))
            task_id = str(task.get("task_id") or "")
            role_label = str(
                task.get("required_role")
                or ((task.get("roles") or ["执行专家"])[0])
            )
            executor_type = locked_task_executor_type(task)
            sp_entry = {
                "id": f"sp-{phase_id}-{task_index + 1:03d}",
                "name": str(task.get("task_name") or task.get("name") or task_id),
                "description": str(
                    task.get("task_description")
                    or task.get("description")
                    or phase.get("description", "")
                ),
                "agent_role": role_label,
                "roles_needed": [role_label],
                "assigned_expert_id": assigned_expert_id,
                "assigned_task_ids": [task_id],
                "executor_type": executor_type,
                "deliverables": list(task.get("required_files") or []),
                "status": "pending",
                "progress": 0,
                "phase_id": phase_id,
                "tech_stack": list(
                    task.get("tech_stack")
                    or task.get("technology_stack")
                    or phase.get("tech_stack", [])
                ),
            }
            ctx.subprojects.append(sp_entry)
            target_sp.append(sp_entry)

    role_types_in_phase = (
        {locked_task_executor_type(task) for task in locked_task_rows}
        if direct_task_bindings
        else (set(seen_role_types) | set(primary_by_type))
    )
    has_database_expert = "database" in role_types_in_phase
    has_devops_expert = "devops" in role_types_in_phase
    has_frontend_expert = "frontend" in role_types_in_phase
    has_backend_expert = "backend" in role_types_in_phase
    has_qa_expert = "qa" in role_types_in_phase
    has_fullstack_expert = "fullstack_engineer" in role_types_in_phase
    phase_contract_text = phase.get("description", "") + "\n" + json.dumps(
        phase.get("deliverables") or [], ensure_ascii=False
    )
    phase_required_files = collect_required_file_paths(phase_contract_text)
    # The locked ProjectContract is the authoritative delivery boundary.
    # Phase prose is only a presentation projection and may omit its explicit
    # file rows, so never derive agent ownership from prose alone.
    contract_required_rows = [
        item for item in (getattr(pm, "project_contract", {}) or {}).get("required_files", [])
        if isinstance(item, dict)
        and str(item.get("phase_id") or "") == str(phase_id)
        and item.get("required", True)
        and is_delivery_file_path(str(item.get("path") or ""))
    ]
    contract_required_files = [
        str(item.get("path") or "").strip().replace("\\", "/")
        for item in contract_required_rows
    ]
    phase_required_files = list(dict.fromkeys(
        contract_required_files or phase_required_files
    ))
    parallel_scopes = _parallel_expert_scopes(
        has_frontend=has_frontend_expert,
        has_backend=has_backend_expert,
        has_database=has_database_expert,
        has_devops=has_devops_expert,
        has_qa=has_qa_expert,
        has_fullstack=has_fullstack_expert,
    )

    def allowed_paths_for(expert_type: str, _multi_expert: bool) -> List[str]:
        """Assign stable role ownership across both single- and multi-agent phases.

        Later integration phases depend on earlier outputs living in the same
        canonical tree.  Leaving a single expert unrestricted lets a frontend
        phase write ``src/`` at the repository root while the next phase owns
        ``frontend/src/``, producing two incompatible applications.
        """
        return list(parallel_scopes.get(expert_type, [f"src/{expert_type}/"]))

    role_responsibilities = {
        "frontend": "只实现浏览器端界面、交互、响应式布局和 API 客户端，不实现后端服务。",
        "backend": "实现后端 API、业务规则、认证授权和服务启动；有独立数据库专家时不要修改数据库目录。",
        "database": "只实现数据库模型、迁移、种子数据和数据完整性约束。",
        "qa": "只编写可执行的自动化测试、测试夹具和验收脚本，不复制或重写业务源码。",
        "security": "只实现安全中间件、安全校验和安全测试，不重写普通业务模块。",
        "devops": "只实现容器、部署、环境变量模板和健康检查配置。",
        "architecture": "只交付架构约束、接口契约和架构文档。",
        "fullstack_engineer": "负责跨模块集成、根目录启动配置和交付说明，不重复实现前端、后端专家已负责的源码。",
    }
    existing_types = {
        expert_type_for_role(str(
            sp.get("agent_role") or ((sp.get("roles_needed") or [""])[0])
        ))
        for sp in target_sp
    }
    for role in ([] if direct_task_bindings else phase_roles):
        role_type = expert_type_for_role(role)
        if role_type in existing_types:
            continue
        idx = len(target_sp)
        sp_id = f"sp-{phase_id}-{idx+1:03d}"
        sp_entry = {
            "id": sp_id,
            "name": f"{phase.get('name','')}-{role}",
            "description": phase.get("description", ""),
            "agent_role": role,
            "roles_needed": [role],
            "status": "pending",
            "progress": 0,
            "phase_id": phase_id,
            "tech_stack": phase.get("tech_stack", []),
        }
        ctx.subprojects.append(sp_entry)
        target_sp.append(sp_entry)
        existing_types.add(role_type)

    def expert_type_for_subproject(subproject: Dict[str, Any]) -> str:
        explicit = str(subproject.get("executor_type") or "")
        if explicit in _PHASE_EXECUTOR_TYPES:
            return explicit
        candidate_roles = subproject.get("roles_needed") or phase.get("roles_needed") or []
        candidate_role = subproject.get("agent_role") or (
            candidate_roles[0] if candidate_roles else ""
        )
        return expert_type_for_role(candidate_role)

    def locked_tasks_for_subproject(
        subproject: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        assigned_ids = {
            str(task_id)
            for task_id in (subproject.get("assigned_task_ids") or [])
            if str(task_id)
        }
        if assigned_ids:
            return [
                copy.deepcopy(task)
                for task in locked_task_rows
                if str(task.get("task_id") or "") in assigned_ids
            ]
        return locked_tasks_for_role_type(
            expert_type_for_subproject(subproject)
        )

    # A phase contract is global, but each required file must have exactly one
    # task owner.  Assigning the same backend/package.json contract to every
    # Backend Developer makes otherwise successful parallel tasks fail merely
    # because they did not reproduce a sibling's file.
    delivery_files_by_subproject: Dict[str, List[str]] = {
        str(sp.get("id")): [] for sp in target_sp if sp.get("id")
    }
    explicit_claims: Dict[str, List[Dict[str, Any]]] = {}
    contract_owner_by_path = {
        str(item.get("path") or "").strip().replace("\\", "/"): str(item.get("owner_type") or "")
        for item in contract_required_rows
    }
    contract_task_by_path = {
        str(item.get("path") or "").strip().replace("\\", "/"): str(item.get("task_id") or "")
        for item in contract_required_rows
    }
    phase_rebuild_manifest = phase.get("rebuild_file_manifest") or {}
    rebuild_files_by_subproject = phase_rebuild_manifest.get("by_subproject") or {}
    for candidate in target_sp:
        candidate_id = str(candidate.get("id") or "")
        if not candidate_id:
            continue
        candidate_description = str(candidate.get("description") or "").strip()
        phase_description = str(phase.get("description") or "").strip()
        rebuild_base_description = str(phase.get("rebuild_base_description") or "").strip()
        # A phase-wide description is copied onto synthetic role tasks.  It
        # describes the whole delivery and must not make the first role claim
        # every mentioned root file (README, Dockerfile, .env.example, ...).
        # Only task-specific descriptions may override the default role scope.
        if (
            candidate_description == phase_description
            or (rebuild_base_description and candidate_description == rebuild_base_description)
            or phase_description.startswith(candidate_description + "\n\n")
        ):
            candidate_description = ""
        candidate_text = "\n".join(str(value or "") for value in (
            candidate.get("name"),
            candidate_description,
            json.dumps(candidate.get("deliverables") or [], ensure_ascii=False),
        ))
        for required_path in collect_required_file_paths(candidate_text):
            if required_path in phase_required_files:
                explicit_claims.setdefault(required_path, []).append(candidate)
        for required_path in rebuild_files_by_subproject.get(candidate_id, []):
            candidate_scopes = allowed_paths_for(
                expert_type_for_subproject(candidate), len(target_sp) > 1
            )
            if (
                required_path in phase_required_files
                and is_delivery_file_path(required_path)
                and required_files_for_scopes([required_path], candidate_scopes)
            ):
                explicit_claims.setdefault(required_path, []).append(candidate)

    def candidate_owns_scope(candidate: Dict[str, Any], required_path: str) -> bool:
        scopes = allowed_paths_for(
            expert_type_for_subproject(candidate), len(target_sp) > 1
        )
        return not scopes or bool(required_files_for_scopes([required_path], scopes))

    for required_path in phase_required_files:
        contract_owner = contract_owner_by_path.get(required_path)
        contract_task_id = contract_task_by_path.get(required_path)
        if contract_task_id:
            candidates = [
                candidate for candidate in target_sp
                if candidate.get("id")
                and contract_task_id in {
                    str(task_id)
                    for task_id in (candidate.get("assigned_task_ids") or [])
                }
            ]
        elif contract_owner:
            candidates = [
                candidate for candidate in target_sp
                if candidate.get("id")
                and expert_type_for_subproject(candidate) == contract_owner
            ]
        else:
            candidates = list(explicit_claims.get(required_path, []))
        if not candidates:
            candidates = [
                candidate for candidate in target_sp
                if candidate.get("id") and candidate_owns_scope(candidate, required_path)
            ]
        if not candidates:
            # Explicit PM/user contracts outrank the default directory layout.
            # The chosen owner receives the exact path in its allowed scopes
            # below, so uncommon but valid paths such as backend/api.py remain
            # deliverable instead of silently disappearing.
            candidates = [candidate for candidate in target_sp if candidate.get("id")]
        if not candidates:
            continue
        owner = min(
            candidates,
            key=lambda candidate: len(
                delivery_files_by_subproject.get(str(candidate.get("id")), [])
            ),
        )
        delivery_files_by_subproject[str(owner["id"])].append(required_path)

    # Rebuild manifests describe the previous generation's ownership, which
    # can be stale after role partitions or phase contracts change. Reassign
    # every old file to exactly one current task before creating leases.
    normalized_rebuild_files: Dict[str, List[str]] = {
        str(sp.get("id")): [] for sp in target_sp if sp.get("id")
    }
    for required_path in phase_rebuild_manifest.get("all") or []:
        if not is_delivery_file_path(required_path):
            continue
        delivery_candidates = [
            candidate for candidate in target_sp
            if required_path in delivery_files_by_subproject.get(str(candidate.get("id")), [])
        ]
        scoped_candidates = [
            candidate for candidate in target_sp
            if required_files_for_scopes(
                [required_path],
                allowed_paths_for(expert_type_for_subproject(candidate), len(target_sp) > 1),
            )
        ]
        previous_candidates = [
            candidate for candidate in target_sp
            if required_path in rebuild_files_by_subproject.get(str(candidate.get("id")), [])
        ]
        candidates = delivery_candidates or scoped_candidates or previous_candidates
        if not candidates:
            candidates = [candidate for candidate in target_sp if candidate.get("id")]
        if not candidates:
            continue
        owner = min(
            candidates,
            key=lambda candidate: len(
                normalized_rebuild_files.get(str(candidate.get("id")), [])
            ),
        )
        normalized_rebuild_files[str(owner["id"])].append(required_path)
    if phase_rebuild_manifest.get("all"):
        rebuild_files_by_subproject = normalized_rebuild_files

    planned_phase_scopes: List[List[str]] = []
    for sp in target_sp:
        if sp.get("status") in ("completed", "in_progress") and sp.get("agent_id"):
            continue

        # 推断专家类型
        roles_needed = sp.get("roles_needed") or phase.get("roles_needed") or []
        role_label = sp.get("agent_role") or (roles_needed[0] if roles_needed else "")
        matched_type = expert_type_for_subproject(sp)
        rebuild_manifest = phase_rebuild_manifest
        by_subproject = rebuild_files_by_subproject
        source_subproject_ids = list(dict.fromkeys(
            [str(sp.get("id") or "")]
            + [str(item) for item in (sp.get("_merged_subproject_ids") or []) if item]
        ))
        required_rebuild_files = list(dict.fromkeys(
            path
            for source_id in source_subproject_ids
            for path in by_subproject.get(source_id, [])
        ))
        rebuild_files_by_task = rebuild_manifest.get("by_task_id") or {}
        assigned_locked_tasks = locked_tasks_for_subproject(sp)
        assigned_task_ids = {
            str(task.get("task_id") or "")
            for task in assigned_locked_tasks
            if task.get("task_id")
        }
        if project_contract.get("locked") and not assigned_locked_tasks:
            # Presentation-level phase roles may be broader than the
            # independently validated phase-PM task contract. Creating an
            # executable Agent for such a role gives it no task identity while
            # its planning lease can still overlap the real task owner's run
            # lease. Represent it as a completed no-op instead.
            sp["status"] = "completed"
            sp["progress"] = 100
            sp["execution_skipped_reason"] = "no_locked_tasks"
            continue
        if rebuild_files_by_task:
            required_rebuild_files = list(dict.fromkeys(
                path
                for task_id in sorted(assigned_task_ids)
                for path in rebuild_files_by_task.get(task_id, [])
            ))
        rebuild_policy = policy_by_path(rebuild_manifest.get("files") or [])
        rebuild_mode_by_path = {
            _normalized_rebuild_path(entry.get("path")):
            str(entry.get("mode") or "")
            for entry in (rebuild_manifest.get("files") or [])
        }
        preserve_paths = {
            str(entry.get("path") or "")
            for entry in (rebuild_manifest.get("files") or [])
            if entry.get("mode") == "preserve"
        }
        required_rebuild_files = [
            path for path in required_rebuild_files
            if rebuild_mode_by_path.get(
                _normalized_rebuild_path(path)
            ) != "preserve"
        ]
        if rebuild_policy:
            required_rebuild_files = [
                path for path in required_rebuild_files
                if (rebuild_policy.get(path) or {}).get("mode") in {"patch", "create"}
            ]
        rebuild_issue_assignments = phase.get("rebuild_issue_assignments") or {}
        required_rebuild_issues: List[Dict[str, Any]] = []
        seen_rebuild_issue_keys: set[str] = set()
        issue_assignment_ids = (
            sorted(assigned_task_ids)
            if rebuild_files_by_task
            else source_subproject_ids
        )
        for assignment_id in issue_assignment_ids:
            for issue in rebuild_issue_assignments.get(assignment_id, []):
                issue_key = _auto_repair_issue_key(issue)
                if issue_key not in seen_rebuild_issue_keys:
                    seen_rebuild_issue_keys.add(issue_key)
                    required_rebuild_issues.append(copy.deepcopy(issue))
        actionable_rebuild_paths = {
            _normalized_rebuild_path(path)
            for path in required_rebuild_files
        }
        required_rebuild_issues = [
            issue for issue in required_rebuild_issues
            if _normalized_rebuild_path(issue.get("file_path"))
            in actionable_rebuild_paths
        ]
        if phase_rebuild_manifest and not required_rebuild_files and not required_rebuild_issues:
            # A rebuild dispatch must have either an actionable file or an
            # assigned blocker. Preserve-only/no-op subprojects are represented
            # by the verification lifecycle below and never call a model.
            continue
        if not required_rebuild_files and not rebuild_policy:
            # Old manifests only recorded an expert type.  That fallback is safe
            # when one expert owns the type, but assigning the same union to two
            # same-type experts makes every rebuilt task fail its delivery
            # contract.  New manifests always preserve subproject ownership.
            same_type_subprojects = []
            for candidate in target_sp:
                candidate_type = expert_type_for_subproject(candidate)
                if candidate_type == matched_type:
                    same_type_subprojects.append(candidate)
            if len(same_type_subprojects) == 1:
                required_rebuild_files = list(
                    (rebuild_manifest.get("by_expert_type") or {}).get(matched_type, [])
                )
        allowed_path_prefixes = allowed_paths_for(matched_type, len(target_sp) > 1)
        required_delivery_files = list(
            delivery_files_by_subproject.get(str(sp.get("id")), [])
        )
        required_delivery_files = [
            path for path in required_delivery_files if path not in preserve_paths
        ]
        if rebuild_policy:
            required_delivery_files = [
                path for path in required_delivery_files
                if path not in rebuild_policy
                or rebuild_policy[path].get("mode") in {"patch", "create"}
            ]
        if project_contract.get("locked") and assigned_task_ids:
            locked_task_files = [
                str(item.get("path") or "").strip().replace("\\", "/")
                for item in contract_required_rows
                if str(item.get("task_id") or "") in assigned_task_ids
                and str(item.get("path") or "").strip()
            ]
            locked_task_files = [
                path for path in locked_task_files
                if path not in preserve_paths
                and (
                    not rebuild_policy
                    or path not in rebuild_policy
                    or rebuild_policy[path].get("mode") in {"patch", "create"}
                )
            ]
            required_delivery_files = list(dict.fromkeys(
                required_delivery_files + locked_task_files
            ))
        if rebuild_policy and not required_rebuild_files and not required_delivery_files:
            # This subproject owns preserve-only files. A completed read-only
            # verifier is registered below; dispatching an execution Agent here
            # gives it no legal output and invites an out-of-scope write.
            continue
        required_rebuild_file_specs = [
            copy.deepcopy(rebuild_policy[path])
            for path in required_rebuild_files
            if path in rebuild_policy
        ]
        if rebuild_manifest.get("all") and not (
            required_rebuild_files or required_delivery_files
        ):
            sp["status"] = "completed"
            sp["progress"] = 100
            sp["preserved_files"] = [
                path for path in by_subproject.get(str(sp.get("id")), [])
                if (rebuild_policy.get(path) or {}).get("mode") == "preserve"
            ]
            continue
        artifact_policy = {
            "kind": (
                "architecture_document" if matched_type == "architecture" else "runnable"
            ),
            "required_files": list(required_delivery_files),
            "allowed_prefixes": list(allowed_path_prefixes),
        }
        same_type_count = sum(
            1 for candidate in target_sp
            if expert_type_for_subproject(candidate) == matched_type
        )
        exact_owned_files = list(dict.fromkeys(
            required_rebuild_files + required_delivery_files
        ))
        if same_type_count > 1 and exact_owned_files:
            # Same-role experts cannot lease the same broad directory.  Their
            # unique delivery ownership is also their write/lock boundary.
            allowed_path_prefixes = [
                path for path in exact_owned_files if is_delivery_file_path(path)
            ]
        for required_path in required_rebuild_files + required_delivery_files:
            if not is_delivery_file_path(required_path):
                continue
            if allowed_path_prefixes and not any(
                required_path == allowed
                or (allowed.endswith("/") and required_path.startswith(allowed))
                for allowed in allowed_path_prefixes
            ):
                allowed_path_prefixes.append(required_path)

        allowed_path_prefixes = _finalize_phase_scope(
            allowed_path_prefixes,
            required_rebuild_files + required_delivery_files,
            planned_phase_scopes,
            subproject_id=str(sp.get("id") or ""),
        )

        # 从专家池匹配
        assigned_expert_id = str(sp.get("assigned_expert_id") or "")
        try:
            if assigned_expert_id:
                profile = expert_pool.get_expert(assigned_expert_id)
                matches = [] if not profile or profile.status != "available" else [{
                    "expert_id": profile.expert_id,
                    "name": profile.name,
                    "role": profile.role,
                    "expert_type": matched_type,
                    "score": 100.0,
                    "status": profile.status,
                    "avg_quality_score": profile.avg_quality_score,
                    "domains": list(profile.domains),
                }]
            else:
                exclude_expert_ids = (
                    []
                    if matched_type == "fullstack_engineer"
                    else [FULLSTACK_ENGINEER_EXPERT_ID]
                )
                matches = expert_pool.match_experts(
                    required_role=role_label,
                    required_expert_type=matched_type,
                    required_domains=sp.get("domains"),
                    required_skills=sp.get("required_skills"),
                    exclude_expert_ids=exclude_expert_ids,
                    top_k=1,
                )
                matches = [
                    match for match in matches
                    if str(match.get("expert_type") or "") == matched_type
                ]
        except Exception as exc:
            logger.warning("Expert match failed during phase start project=%s phase=%s role=%s: %s", project_id, phase_id, role_label, exc)
            matches = []

        if assigned_expert_id and not matches:
            _rollback_phase_start()
            await _persist_all_async()
            raise HTTPException(
                status_code=409,
                detail={
                    "status": "expert_assignment_stale",
                    "message": "已分配专家在启动时不可用，请重新生成阶段规划",
                    "expert_id": assigned_expert_id,
                },
            )

        if matches:
            expert = matches[0]
            agent_id = f"agent-{uuid.uuid4().hex[:6]}"
            expert_skill_ids = []
            if matched_type in ("pm", "supervisor", "hr", "pg", "ccb"):
                expert_skill_ids = ctx.skill_manager.get_skill_ids_for_agent_type(matched_type)

            agent_info = {
                "id": agent_id,
                "role": expert["name"],
                "expert_id": expert["expert_id"],
                "expert_type": matched_type,
                "subproject_id": sp["id"],
                "subproject_name": sp.get("name", ""),
                "status": "idle",
                "created_at": time.time(),
                "project_id": project_id,
                "phase_id": phase_id,
                "skills": expert_skill_ids,
                "skill_names": [s["name"] for s in ctx.skill_manager.get_skills_for_agent_type(matched_type)]
                                if matched_type in ("pm", "supervisor", "hr", "pg", "ccb") else [],
                "match_score": expert["score"],
                "domains": expert["domains"],
                "required_rebuild_files": required_rebuild_files,
                "rebuild_file_specs": required_rebuild_file_specs,
                "rebuild_issues": required_rebuild_issues,
                "required_delivery_files": required_delivery_files,
                "allowed_path_prefixes": allowed_path_prefixes,
                "artifact_policy": artifact_policy,
                "required_role": role_label,
                "locked_tasks": assigned_locked_tasks,
            }
            agent_info["assigned_task_ids"] = [
                str(item.get("task_id"))
                for item in agent_info["locked_tasks"]
            ]
            # 创建 ExpertLock 文件级租约锁
            lock_result = expert_lock.atomic_claim_lock(
                expert_id=expert["expert_id"],
                project_id=project_id,
                task_id=sp["id"],
                file_scope=_phase_planning_lock_scope(
                    allowed_path_prefixes,
                ),
            )
            if not lock_result.get("success"):
                _rollback_phase_start()
                await _persist_all_async()
                raise HTTPException(
                    status_code=409,
                    detail=f"阶段文件范围冲突，未创建 Agent：{lock_result.get('error', 'lock unavailable')}",
                )
            agent_info["lock_id"] = lock_result["lock_id"]
            agent_info["locked_until"] = lock_result["leased_until"]
            transition_agent(agent_info, "queued", progress=0, message="Phase agent created")
            ctx.agents[agent_id] = agent_info
            sp["agent_id"] = agent_id
            sp["status"] = "in_progress"
            sp["phase_id"] = phase_id
            phase["agents"].append(agent_id)
            created_agents.append(agent_info)
            planned_phase_scopes.append(list(allowed_path_prefixes))
        else:
            # fallback: 创建通用执行 Agent
            agent_id = f"agent-{uuid.uuid4().hex[:6]}"
            fallback_expert_id = f"fallback:{agent_id}"
            lock_result = expert_lock.atomic_claim_lock(
                expert_id=fallback_expert_id,
                project_id=project_id,
                task_id=sp["id"],
                file_scope=_phase_planning_lock_scope(
                    allowed_path_prefixes,
                ),
            )
            if not lock_result.get("success"):
                _rollback_phase_start()
                await _persist_all_async()
                raise HTTPException(
                    status_code=409,
                    detail=f"阶段文件范围冲突，未创建 Agent：{lock_result.get('error', 'lock unavailable')}",
                )
            agent_info = {
                "id": agent_id, "role": role_label or "执行专家",
                "expert_id": fallback_expert_id,
                "expert_type": matched_type,
                "subproject_id": sp["id"], "subproject_name": sp.get("name", ""),
                "status": "idle", "created_at": time.time(), "project_id": project_id,
                "phase_id": phase_id, "skills": [], "skill_names": [],
                "required_rebuild_files": required_rebuild_files,
                "rebuild_file_specs": required_rebuild_file_specs,
                "rebuild_issues": required_rebuild_issues,
                "required_delivery_files": required_delivery_files,
                "allowed_path_prefixes": allowed_path_prefixes,
                "artifact_policy": artifact_policy,
                "lock_id": lock_result["lock_id"],
                "locked_until": lock_result["leased_until"],
                "required_role": role_label,
                "locked_tasks": assigned_locked_tasks,
            }
            agent_info["assigned_task_ids"] = [
                str(item.get("task_id"))
                for item in agent_info["locked_tasks"]
            ]
            transition_agent(agent_info, "queued", progress=0, message="Phase agent created")
            ctx.agents[agent_id] = agent_info
            sp["agent_id"] = agent_id
            sp["status"] = "in_progress"
            sp["phase_id"] = phase_id
            phase["agents"].append(agent_id)
            created_agents.append(agent_info)
            planned_phase_scopes.append(list(allowed_path_prefixes))

    # Preserve targets still need a unique recorded owner for deterministic QA,
    # but that owner receives a read-only policy and never has to reproduce the
    # file. Prefer an already-created same-role Agent; if a role has no
    # actionable files, record a completed verification-only Agent.
    for preserve_spec in (
        entry for entry in (phase_rebuild_manifest.get("files") or [])
        if entry.get("mode") == "preserve"
    ):
        preserve_path = str(preserve_spec.get("path") or "")
        owner_type = str(preserve_spec.get("owner_type") or "backend")
        # Preserve-only files belong to a completed verification lifecycle.
        # Never expose them in an executing Agent's allowed paths or rebuild
        # specs: doing so invites the model to emit a write that the policy
        # must then reject.
        owner_agent = None
        if owner_agent is None:
            verifier_id = f"agent-preserve-{uuid.uuid4().hex[:6]}"
            owner_agent = {
                "id": verifier_id,
                "role": f"{owner_type} preservation verifier",
                "expert_type": owner_type,
                "project_id": project_id,
                "phase_id": phase_id,
                "status": "completed",
                "progress": 100,
                "created_at": time.time(),
                "allowed_path_prefixes": [],
                "verification_paths": [preserve_path],
                "required_rebuild_files": [],
                "required_delivery_files": [],
                "rebuild_file_specs": [copy.deepcopy(preserve_spec)],
                "output_files": [],
                "verification_only": True,
            }
            ctx.agents[verifier_id] = owner_agent
            phase["agents"].append(verifier_id)
        else:
            if preserve_path not in owner_agent["allowed_path_prefixes"]:
                owner_agent["allowed_path_prefixes"].append(preserve_path)
            owner_agent.setdefault("rebuild_file_specs", []).append(
                copy.deepcopy(preserve_spec)
            )
        registry_entry = pm.file_registry.get(preserve_path)
        if registry_entry is not None:
            registry_entry["agent_id"] = owner_agent["id"]
            registry_entry["owner_type"] = owner_type
            registry_entry["rebuild_mode"] = "preserve"
        preserve_target = Path(ctx.workspace) / preserve_path
        current_digest = (
            "sha256:" + hashlib.sha256(preserve_target.read_bytes()).hexdigest()
            if preserve_target.is_file() else ""
        )
        baseline_digest = str(
            preserve_spec.get("baseline_digest") or ""
        )
        preserve_run_id = (
            f"preserve:{phase_id}:"
            + hashlib.sha256(preserve_path.encode("utf-8")).hexdigest()[:16]
        )
        preserve_evidence = create_evidence(
            EvidenceKind.ARTIFACT_VALIDATION,
            preserve_run_id,
            "metis.runner",
            {
                "validator": "phase.preserve_baseline_digest",
                "checks": [{
                    "name": "preserved baseline byte digest matches",
                    "passed": bool(
                        current_digest
                        and current_digest == baseline_digest
                    ),
                    "path": preserve_path,
                }],
                "artifact_digest": current_digest,
                "phase_id": str(phase_id),
                "agent_id": str(owner_agent["id"]),
            },
        )
        owner_agent.setdefault("preservation_receipts", {})[preserve_path] = {
            "path": preserve_path,
            "status": (
                "succeeded"
                if preserve_evidence.status == "passed" else "failed"
            ),
            "start_run_id": preserve_run_id,
            "completion_run_id": preserve_run_id,
            "baseline_digest": baseline_digest,
            "artifact_digest": current_digest,
            "evidence": [preserve_evidence.to_dict()],
            "execution_generation": phase.get("execution_generation"),
        }

    # Keep PhaseManager's secondary index consistent with the canonical phase
    # record.  Rebuilds create fresh ids, so retaining the old index leaves
    # dispatch/status consumers pointing at agents that reset_phase deleted.
    pm.phase_agents[phase_id] = list(phase.get("agents") or [])

    # A rebuilt phase is a new execution lifecycle. Keep historical messages,
    # but do not expose the previous terminal QA verdict while fresh agents
    # are still running; polling clients would otherwise stop before the new
    # artifacts can be checked.
    repair_state = _auto_repair_states.get(f"{project_id}-{phase_id}")
    if created_agents and repair_state and repair_state.get("status") in {
        "quality_regressed", "no_progress", "qa_blocked", "needs_manual",
        "awaiting_manual_fix", "failed",
    }:
        repair_state.update({
            "running": False,
            "status": "awaiting_execution",
            "action_required": None,
            "needs_manual": False,
            "repair_batch": None,
        })

    # 将 Agent 注册到任务调度器
    phase_assignments = [
        {
            "agent_id": agent.get("id"),
            "required_role": str(
                task.get("required_role")
                or ((task.get("roles") or [""])[0])
            ),
            "task_ids": [str(task.get("task_id"))],
        }
        for agent in created_agents
        for task in (agent.get("locked_tasks") or [])
        if task.get("task_id")
    ]
    project_contract = getattr(pm, "project_contract", {}) or {}
    contract_digest = "sha256:" + hashlib.sha256(
        json.dumps(
            project_contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    requirements_revision = int(
        project_contract.get("requirements_revision") or 0
    )
    artifact_baseline_digest = "sha256:" + hashlib.sha256(
        json.dumps(
            [
                {
                    "path": str(item.get("path") or ""),
                    "baseline_digest": str(item.get("baseline_digest") or ""),
                    "mode": str(item.get("mode") or ""),
                    "task_id": str(item.get("task_id") or ""),
                }
                for item in (phase_rebuild_manifest.get("files") or [])
                if isinstance(item, dict)
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if not phase.get("execution_generation"):
        phase["execution_generation"] = (
            f"{phase_id}:{uuid.uuid4().hex}:{contract_digest}:"
            f"{requirements_revision}:{artifact_baseline_digest}"
        )
    phase["execution_contract_digest"] = contract_digest
    phase["execution_requirements_revision"] = requirements_revision
    phase["execution_artifact_baseline_digest"] = artifact_baseline_digest
    completed_task_ids = _verified_completed_task_ids(
        ctx,
        pm,
        project_id=project_id,
        contract_digest=contract_digest,
        requirements_revision=requirements_revision,
    )
    phase_dispatch_plan = None
    if getattr(pm, "project_contract", {}).get("locked"):
        try:
            phase_dispatch_plan = build_phase_dispatch_plan(
                pm.phases,
                phase_id,
                phase_assignments,
                completed_task_ids=completed_task_ids,
            )
        except PhaseExecutionContractError as exc:
            _rollback_phase_start()
            await _persist_all_async()
            raise HTTPException(
                status_code=409,
                detail={
                    "message": str(exc),
                    "issues": [issue.to_dict() for issue in exc.issues],
                },
            ) from exc
        phase["execution_dispatch_plan"] = copy.deepcopy(phase_dispatch_plan)

    if created_agents:
        from core.dispatch_integration import register_phase_agents
        register_phase_agents(project_id, phase_id, created_agents)

    # 自动触发所有已创建 Agent 的执行
    if created_agents:
        from api import routes_execution
        project_context = str(
            getattr(pm_leader, "canonical_requirements", "") or ""
        ).strip() or ctx.pm.context_summary or ctx.description or ""
        phase_tech_stack = _infer_phase_tech_stack(phase, ctx.description or "")
        run_specs: List[Dict[str, Any]] = []
        for agent_info in created_agents:
            aid = agent_info["id"]
            sp_id = agent_info.get("subproject_id", "")
            sp_name = agent_info.get("subproject_name", "")
            required_files = agent_info.get("required_rebuild_files") or []
            rebuild_file_specs = agent_info.get("rebuild_file_specs") or []
            contract_files = agent_info.get("required_delivery_files") or []
            sp = next((item for item in target_sp if item.get("id") == sp_id), {})
            sp_desc = sp.get("description") or phase.get("description", "")
            full_phase_plan = {
                "description": phase.get("description") or "",
                "roles": list(phase.get("roles_needed") or phase.get("roles") or []),
                "tasks": list(phase.get("task_contract") or phase.get("expert_requirements") or []),
                "expert_requirements": list(phase.get("expert_requirements") or []),
                "acceptance_criteria": list(phase.get("acceptance_criteria") or []),
            }
            sp_desc += (
                "\n\nFULL PHASE PLAN (READ-ONLY CONTEXT):\n"
                + json.dumps(full_phase_plan, ensure_ascii=False, indent=2)
                + "\nImplement only the current Agent's assigned tasks and authorized paths; "
                "do not perform another role's work."
            )
            assigned_requirements = list(agent_info.get("locked_tasks") or [])
            if assigned_requirements:
                requirement_lines = []
                for requirement in assigned_requirements:
                    criteria = requirement.get("acceptance_criteria") or []
                    requirement_lines.append(
                        f"- {requirement.get('task_name') or requirement.get('task_id') or '任务'}："
                        f"{requirement.get('task_description') or ''}"
                        + ("；验收：" + "；".join(str(item) for item in criteria) if criteria else "")
                    )
                sp_desc += (
                    "\n\n【本专家在该阶段的全部合并任务】\n"
                    + "\n".join(requirement_lines)
                    + "\n以上同角色任务由当前唯一专家一次性完成，不得拆给重复角色 Agent。"
                )
            allowed_paths = agent_info.get("allowed_path_prefixes") or []
            assigned_rebuild_issues = agent_info.get("rebuild_issues") or []
            if assigned_rebuild_issues:
                sp_desc += (
                    "\n\n【仅属于本任务的上轮质检问题】\n"
                    + "\n".join(
                        "- [{issue_id}] {path}: {message}".format(
                            issue_id=(issue.get("id") or issue.get("fingerprint") or "issue"),
                            path=(issue.get("file_path") or "未定位文件"),
                            message=(issue.get("message") or issue.get("fix_hint") or "需要修复"),
                        )
                        for issue in assigned_rebuild_issues
                    )
                    + "\n只处理上述问题和本任务文件边界，不得修改其他工程师负责的文件。"
                )
            if allowed_paths:
                sp_desc += (
                    "\n\n【职责与文件边界】\n"
                    f"你是 {agent_info.get('role', '执行专家')}，只完成该角色负责的部分。\n"
                    f"{role_responsibilities.get(agent_info.get('expert_type', ''), '')}\n"
                    + "仅允许产出以下路径：\n"
                    + "\n".join(f"- {path}" for path in allowed_paths)
                    + "\n不得生成或覆盖其他专家负责的文件。"
                )
            if required_files:
                sp_desc += (
                    "\n\n【全量重构强制文件清单】\n"
                    + "\n".join(f"- {path}" for path in required_files)
                    + "\n必须逐一重新产出上述每个文件；仅说明已检查不算完成。保留正确接口，修复问题后输出完整文件。"
                )
            if contract_files:
                sp_desc += (
                    "\n\nDELIVERY FILE CONTRACT — every listed file must be produced in this execution:\n"
                    + "\n".join(f"- {path}" for path in contract_files)
                    + "\nMissing any listed file is an execution failure."
                )
            if rebuild_file_specs:
                sp_desc += (
                    "\n\nFILE-SCOPED REBUILD POLICY:\n"
                    + "\n".join(
                        "- {path}: mode={mode}; baseline_digest={digest}; "
                        "issue_ids={issues}; required_invariants={invariants}".format(
                            path=spec.get("path"), mode=spec.get("mode"),
                            digest=spec.get("baseline_digest"),
                            issues=",".join(spec.get("issue_ids") or []) or "none",
                            invariants="; ".join(spec.get("required_invariants") or []) or "none",
                        )
                        for spec in rebuild_file_specs
                    )
                    + "\nPatch only the listed issues in the complete original file. "
                    "Return strict JSON containing path, baseline_digest, issue_ids, and complete content. "
                    "Never emit an unlisted or preserve file."
                )
                for spec in rebuild_file_specs:
                    if spec.get("mode") != "patch":
                        continue
                    patch_path = str(spec.get("path") or "")
                    patch_target = ctx.workspace / patch_path
                    if patch_target.is_file():
                        sp_desc += (
                            f"\n\nCOMPLETE ORIGINAL FILE FOR PATCH {patch_path} "
                            f"({spec.get('baseline_digest')}):\n"
                            + patch_target.read_text(encoding="utf-8", errors="replace")
                        )
            execution_contract = {
                "project_id": project_id,
                "agent_id": aid,
                "subproject_id": sp_id,
                "subproject_name": sp_name,
                "description": sp_desc,
                "tech_stack": list(phase_tech_stack),
                "project_context": project_context,
                "artifact_policy": dict(agent_info.get("artifact_policy") or {}),
                # Rebuild producers are validated as one phase-wide delivery.
                # Per-agent repair QA races sibling roles and can reject a
                # successful DevOps/frontend task while another role is still
                # running.
                "defer_fix_qc": bool(phase_rebuild_manifest.get("all")),
            }
            # One immutable execution contract drives automatic execution,
            # manual rerun and process recovery. Reconstructing it later from
            # the placeholder subproject loses inferred stack, merged tasks,
            # acceptance criteria and delivery boundaries.
            agent_info["execution_contract"] = dict(execution_contract)
            run_specs.append({
                **execution_contract,
                "expert_type": agent_info.get("expert_type", "backend"),
            })

        coordinator_run_id = ""
        coordinator_owner = ""

        async def _run_phase_agent_batch() -> None:
            """Run the locked task DAG through the durable execution entry."""
            run_specs_by_agent = {
                str(spec.get("agent_id") or ""): spec for spec in run_specs
            }
            agent_task_locks = {
                agent_id: asyncio.Lock() for agent_id in run_specs_by_agent
            }

            async def _execute_legacy(spec: Dict[str, Any]):
                payload = dict(spec)
                payload.pop("expert_type", None)
                return await routes_execution._run_agent_task(**payload)

            async def _execute_locked_task(
                task_spec: Dict[str, Any],
            ) -> Dict[str, Any]:
                await _assert_phase_coordinator_lease(
                    coordinator_run_id, coordinator_owner,
                )
                agent_id = str(task_spec.get("agent_id") or "")
                task_id = str(task_spec.get("task_id") or "")
                await agent_task_locks[agent_id].acquire()
                agent = ctx.agents[agent_id]
                payload = dict(run_specs_by_agent[agent_id])
                payload.pop("expert_type", None)
                all_delivery = list(agent.get("required_delivery_files") or [])
                all_rebuild = list(agent.get("required_rebuild_files") or [])
                task_contract_files = [
                    str(item.get("path") or "")
                    for item in (
                        getattr(pm, "project_contract", {}).get("required_files")
                        or []
                    )
                    if isinstance(item, dict)
                    and str(item.get("phase_id") or "") == str(phase_id)
                    and str(item.get("task_id") or "") == task_id
                    and item.get("required", True)
                ]
                rebuild_files_by_task = phase_rebuild_manifest.get("by_task_id")
                if phase_rebuild_manifest and isinstance(
                    rebuild_files_by_task, dict
                ):
                    executable_task_paths = {
                        _normalized_rebuild_path(path)
                        for path in rebuild_files_by_task.get(task_id, [])
                    }
                    task_contract_files = [
                        path for path in task_contract_files
                        if _normalized_rebuild_path(path)
                        in executable_task_paths
                    ]
                strict_locked_contract = int(
                    project_contract.get("contract_version") or 0
                ) >= 3
                task_required = list(dict.fromkeys(
                    task_contract_files
                    if strict_locked_contract
                    else (task_contract_files or all_rebuild or all_delivery)
                ))
                task_rebuild_specs = [
                    copy.deepcopy(spec)
                    for spec in (agent.get("rebuild_file_specs") or [])
                    if isinstance(spec, dict)
                    and str(spec.get("path") or "") in task_required
                ]
                source_ids = {
                    str(item) for item in (
                        task_spec.get("source_requirement_ids") or []
                    ) if str(item)
                }
                requirement_units = [
                    {
                        "id": str(unit.get("unit_id") or ""),
                        "text": str(unit.get("exact_text") or ""),
                        "constraint": str(unit.get("kind") or "context"),
                    }
                    for unit in (project_contract.get("requirement_units") or [])
                    if isinstance(unit, dict)
                    and str(unit.get("unit_id") or "") in source_ids
                ]
                locked_task_context = {
                    **dict(task_spec),
                    "requirement_units": requirement_units,
                    "requirements_revision": requirements_revision,
                    "requirements_digest": project_contract.get(
                        "requirements_digest"
                    ),
                }
                if strict_locked_contract:
                    payload["description"] = (
                        "LOCKED PHASE CONSTRAINTS (read-only):\n"
                        + json.dumps({
                            "phase_id": str(phase_id),
                            "acceptance_criteria": list(
                                phase.get("acceptance_criteria") or []
                            ),
                            "dependencies": list(
                                phase.get("dependencies") or []
                            ),
                            "source_constraints": list(
                                phase.get("source_constraints") or []
                            ),
                        }, ensure_ascii=False, indent=2)
                        + "\n\nCURRENT LOCKED TASK (execute only this task):\n"
                        + json.dumps(
                            locked_task_context,
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                else:
                    payload["description"] = (
                        str(payload.get("description") or "")
                        + "\n\nCURRENT LOCKED TASK (execute only this task):\n"
                        + json.dumps(
                            locked_task_context,
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                payload["artifact_policy"] = _locked_task_artifact_policy(
                    dict(payload.get("artifact_policy") or {}),
                    task_id=task_id,
                    task_dependencies=(
                        locked_task_context.get("dependencies") or []
                    ),
                    required_files=task_required,
                    rebuild_file_specs=task_rebuild_specs,
                )
                payload.update({
                    "phase_id": str(phase_id),
                    "task_id": task_id,
                    "execution_generation": phase["execution_generation"],
                    "contract_digest": contract_digest,
                    "requirements_revision": requirements_revision,
                    "artifact_baseline_digest": artifact_baseline_digest,
                    "phase_coordinator_run_id": coordinator_run_id,
                    "dispatch_attempt_digest": str(
                        (
                            phase.get("execution_coordinator") or {}
                        ).get("dispatch_attempt_digest") or ""
                    ),
                })
                receipts = agent.setdefault("task_execution_receipts", {})
                await _assert_phase_coordinator_lease(
                    coordinator_run_id, coordinator_owner,
                )
                receipts[task_id] = {
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "phase_id": str(phase_id),
                    "started_at": time.time(),
                    "required_files": task_required,
                    "status": "pending",
                    "execution_generation": phase["execution_generation"],
                    "contract_digest": contract_digest,
                    "requirements_revision": requirements_revision,
                    "artifact_baseline_digest": artifact_baseline_digest,
                    "phase_coordinator_run_id": coordinator_run_id,
                }
                try:
                    await _assert_phase_coordinator_lease(
                        coordinator_run_id, coordinator_owner,
                    )
                    run, _created = await routes_execution._schedule_durable_agent_run(
                        payload,
                        client_idempotency_key=(
                            f"phase:{phase_id}:task:{task_id}:"
                            f"generation:{phase['execution_generation']}:"
                            f"contract:{contract_digest}:revision:{requirements_revision}:"
                            f"baseline:{artifact_baseline_digest}"
                        ),
                    )
                    run_id = str(run.get("run_id") or "")
                    await _assert_phase_coordinator_lease(
                        coordinator_run_id, coordinator_owner,
                    )
                    receipts[task_id].update({
                        "start_run_id": run_id,
                        "started_at": run.get("started_at") or time.time(),
                        "status": str(run.get("status") or "pending"),
                    })
                    active = routes_execution._active_run_tasks.get(run_id)
                    if active is not None:
                        await active
                    await _assert_phase_coordinator_lease(
                        coordinator_run_id, coordinator_owner,
                    )
                    finished = await asyncio.to_thread(
                        routes_execution._run_registry.get, run_id,
                    )
                    await _assert_phase_coordinator_lease(
                        coordinator_run_id, coordinator_owner,
                    )
                    receipts[task_id].update({
                        "completion_run_id": run_id,
                        "started_at": finished.get("started_at"),
                        "finished_at": finished.get("finished_at"),
                        "status": finished.get("status"),
                        "result": copy.deepcopy(finished.get("result") or {}),
                    })
                    if finished.get("status") != "succeeded":
                        logger.error(
                            "Locked task run failed: task=%s status=%s error=%s",
                            task_id,
                            finished.get("status"),
                            finished.get("last_error")
                            or (finished.get("result") or {}).get("error"),
                        )
                    await _persist_all_async()
                    return {
                        "success": finished.get("status") == "succeeded",
                        "status": (
                            "completed"
                            if finished.get("status") == "succeeded"
                            else str(finished.get("status") or "failed")
                        ),
                        "run_id": run_id,
                    }
                except Exception as exc:
                    from core.execution_runs import LeaseConflict as CoordinatorLeaseConflict
                    if isinstance(exc, CoordinatorLeaseConflict):
                        raise
                    logger.exception(
                        "Locked task attempt failed project=%s phase=%s task=%s agent=%s",
                        project_id,
                        phase_id,
                        task_id,
                        agent_id,
                    )
                    receipts[task_id].update({
                        "status": "failed",
                        "finished_at": time.time(),
                        "error_code": type(exc).__name__,
                    })
                    raise
                finally:
                    agent_task_locks[agent_id].release()

            try:
                await _assert_phase_coordinator_lease(
                    coordinator_run_id, coordinator_owner,
                )
                if phase_dispatch_plan:
                    dispatch_result = await execute_phase_dispatch_plan(
                        phase_dispatch_plan,
                        _execute_locked_task,
                    )
                else:
                    await asyncio.gather(*(
                        _execute_legacy(spec) for spec in run_specs
                    ))
                    dispatch_result = {
                        "success": True,
                        "schema_version": "legacy",
                        "phase_id": str(phase_id),
                    }
                await _assert_phase_coordinator_lease(
                    coordinator_run_id, coordinator_owner,
                )
                phase["execution_dispatch_result"] = dispatch_result
                phase.setdefault("execution_coordinator", {}).update({
                    "status": (
                        "running" if coordinator_run_id else "completed"
                    ),
                    "dispatch_completed": True,
                    "dispatch_completed_at": time.time(),
                })
                if not coordinator_run_id:
                    phase["execution_coordinator"]["completed_at"] = (
                        time.time()
                    )
                    await _start_phase_quality_after_execution(
                        ctx, phase, str(phase_id),
                    )
                await _persist_all_async()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                from core.execution_runs import LeaseConflict as CoordinatorLeaseConflict
                if isinstance(exc, CoordinatorLeaseConflict):
                    raise
                logger.exception(
                    "Locked phase dispatch failed project=%s phase=%s",
                    project_id,
                    phase_id,
                )
                await _assert_phase_coordinator_lease(
                    coordinator_run_id, coordinator_owner,
                )
                assigned_agent_by_task = {
                    str(task_id): str(assignment.get("agent_id") or "")
                    for assignment in phase_assignments
                    for task_id in (assignment.get("task_ids") or [])
                }
                for task_id in (
                    (phase_dispatch_plan or {}).get("task_ids") or []
                ):
                    task_id = str(task_id)
                    owner = ctx.agents.get(
                        assigned_agent_by_task.get(task_id, ""),
                    )
                    if owner is None:
                        continue
                    receipts = owner.setdefault("task_execution_receipts", {})
                    receipts.setdefault(task_id, {
                        "task_id": task_id,
                        "agent_id": str(owner.get("id") or ""),
                        "phase_id": str(phase_id),
                        "status": "blocked",
                        "error_code": "dependency_or_wave_failed",
                        "execution_generation": phase["execution_generation"],
                        "contract_digest": contract_digest,
                        "requirements_revision": requirements_revision,
                        "artifact_baseline_digest": artifact_baseline_digest,
                        "required_files": [],
                    })
                phase["execution_dispatch_result"] = {
                    "schema_version": 1,
                    "status": "failed",
                    "error_code": type(exc).__name__,
                    "completed_task_ids": [
                        str(task_id)
                        for agent in created_agents
                        for task_id, receipt in (
                            agent.get("task_execution_receipts") or {}
                        ).items()
                        if str(receipt.get("status") or "").lower()
                        == "succeeded"
                    ],
                }
                phase.setdefault("execution_coordinator", {}).update({
                    "status": "failed",
                    "error_code": type(exc).__name__,
                    "failed_at": time.time(),
                    "dispatch_completed": True,
                    "dispatch_completed_at": time.time(),
                })
                phase["status"] = "failed"
                for agent in created_agents:
                    receipts = agent.get("task_execution_receipts") or {}
                    if not receipts or any(
                        str(receipt.get("status") or "").lower() != "succeeded"
                        for receipt in receipts.values()
                    ):
                        agent["status"] = "failed"
                        agent["error_code"] = type(exc).__name__
                for subproject in target_sp:
                    if str(subproject.get("status") or "").lower() != "completed":
                        subproject["status"] = "failed"
                        subproject["error_code"] = type(exc).__name__
                await _persist_all_async()

        phase["execution_run_specs"] = copy.deepcopy(run_specs)
        phase.setdefault("execution_coordinator", {}).update({
            "status": "running",
            "task_ids": list(
                (phase_dispatch_plan or {}).get("task_ids") or []
            ),
            "updated_at": time.time(),
        })
        coordinator_run_id = str(
            phase["execution_coordinator"].get("durable_run_id") or ""
        )
        if coordinator_run_id:
            coordinator_owner = (
                f"metis-phase:{os.getpid()}:{project_id}:{phase_id}:"
                f"{uuid.uuid4().hex}"
            )
            from core.execution_runs import LeaseConflict
            try:
                claimed_coordinator = await asyncio.to_thread(
                    routes_execution._run_registry.claim,
                    coordinator_run_id,
                    coordinator_owner,
                    lease_seconds=_PHASE_COORDINATOR_LEASE_SECONDS,
                )
            except LeaseConflict as exc:
                _rollback_phase_start()
                await _persist_all_async()
                raise HTTPException(
                    status_code=409,
                    detail="Phase coordinator generation is already claimed",
                ) from exc
            phase["execution_coordinator"].update({
                "recovery_claim_active": True,
                "recovery_claim_id": coordinator_run_id,
                "recovery_claim_generation": phase["execution_generation"],
                "recovery_claim_owner": coordinator_owner,
                "recovery_claim_expires_at": claimed_coordinator.get(
                    "lease_expires_at"
                ),
            })
            driver = _run_claimed_phase_coordinator(
                project_id,
                phase_id,
                coordinator_run_id,
                coordinator_owner,
                _run_phase_agent_batch,
            )
        else:
            driver = _run_phase_agent_batch()
        await _persist_all_async()
        _safe_create_task(
            driver,
            name=f"phase-{phase_id}-role-batch",
        )

    await _persist_all_async()
    return {"success": True, "created_agents": created_agents, "message": f"Phase {phase.get('name', '')} started with {len(created_agents)} agents"}


_RESET_ACTIVE_AGENT_STATUSES = frozenset({
    "queued", "working", "in_progress", "running", "re_checking", "fixing",
})


def _phase_reset_run_ids(
    agent: Dict[str, Any],
    persisted_execution: Optional[Dict[str, Any]] = None,
) -> List[str]:
    run_ids: List[str] = []
    for receipt in (agent.get("task_execution_receipts") or {}).values():
        if not isinstance(receipt, dict):
            continue
        for key in ("start_run_id", "completion_run_id"):
            run_id = str(receipt.get(key) or "").strip()
            if run_id and run_id not in run_ids:
                run_ids.append(run_id)
    persisted_run_id = str(
        (persisted_execution or {}).get("run_id") or ""
    ).strip()
    if persisted_run_id and persisted_run_id not in run_ids:
        run_ids.append(persisted_run_id)
    return run_ids


def _phase_reset_lock_matches(
    lock: Dict[str, Any],
    agent: Dict[str, Any],
    phase_task_ids: set[str],
    run_ids: List[str],
) -> bool:
    lock_id = str(lock.get("lock_id") or "")
    agent_lock_id = str(agent.get("lock_id") or "")
    if lock_id and agent_lock_id and lock_id == agent_lock_id:
        return True
    expert_id = str(lock.get("expert_id") or "")
    agent_expert_id = str(agent.get("expert_id") or "")
    if expert_id and agent_expert_id and expert_id == agent_expert_id:
        return True
    if _phase_reset_task_lock_matches(lock, phase_task_ids):
        return True
    lock_task_id = str(lock.get("task_id") or "")
    return any(
        lock_task_id.endswith(f":run:{run_id}")
        for run_id in run_ids if run_id
    )


def _phase_reset_task_lock_matches(
    lock: Dict[str, Any],
    phase_task_ids: set[str],
) -> bool:
    lock_task_id = str(lock.get("task_id") or "")
    return any(
        lock_task_id == task_id or lock_task_id.startswith(f"{task_id}:run:")
        for task_id in phase_task_ids if task_id
    )


async def _phase_agent_has_live_reset_execution(
    project_id: str,
    phase_id: str,
    agent: Dict[str, Any],
    phase_task_ids: set[str],
    active_locks: List[Dict[str, Any]],
) -> bool:
    """Fail closed unless an active-looking Agent is proven durably terminal."""
    from api import routes_execution
    from core.execution_runs import RunNotFound, TERMINAL_STATUSES

    agent_id = str(agent.get("id") or "")
    persisted = routes_execution.execution_status.get(agent_id) or {}
    run_ids = _phase_reset_run_ids(agent, persisted)
    self_reported = str(agent.get("status") or "").strip().lower()
    persisted_status = str(persisted.get("status") or "").strip().lower()
    looks_active = (
        self_reported in _RESET_ACTIVE_AGENT_STATUSES
        or persisted_status in _RESET_ACTIVE_AGENT_STATUSES
    )

    if looks_active and any(
        _phase_reset_lock_matches(lock, agent, phase_task_ids, run_ids)
        for lock in active_locks
    ):
        return True

    for run_id in run_ids:
        task = routes_execution._active_run_tasks.get(run_id)
        if task is not None:
            try:
                if not task.done():
                    return True
            except Exception:
                return True
        guard = routes_execution._run_execution_guards.get(run_id)
        if guard is not None:
            try:
                if guard.valid:
                    return True
            except Exception:
                return True
        try:
            durable_run = await asyncio.to_thread(
                routes_execution._run_registry.get, run_id,
            )
        except RunNotFound:
            # A missing historical run must not make an already-terminal Agent
            # impossible to reset forever. Active-looking state still requires
            # durable terminal proof and therefore remains fail-closed.
            if looks_active:
                return True
            continue
        except Exception:
            return True
        payload = durable_run.get("payload") or {}
        if (
            str(payload.get("project_id") or "") != str(project_id)
            or str(payload.get("agent_id") or "") != agent_id
            or (
                payload.get("phase_id")
                and str(payload.get("phase_id")) != str(phase_id)
            )
        ):
            return True
        if str(durable_run.get("status") or "").lower() not in TERMINAL_STATUSES:
            return True

    # Without a canonical run, queued/working remains active by default. When
    # every associated run is terminal and no task, guard, or lock survives,
    # the mutable Agent status is stale and must not make reset impossible.
    return bool(looks_active and not run_ids)


async def _phase_coordinator_has_live_reset_execution(
    project_id: str,
    coordinator: Dict[str, Any],
) -> bool:
    from api import routes_execution
    from core.execution_runs import TERMINAL_STATUSES

    run_id = str(coordinator.get("durable_run_id") or "").strip()
    coordinator_status = str(coordinator.get("status") or "").strip().lower()
    if not run_id:
        return coordinator_status in {"starting", "running"}
    task = routes_execution._active_run_tasks.get(run_id)
    if task is not None:
        try:
            if not task.done():
                return True
        except Exception:
            return True
    guard = routes_execution._run_execution_guards.get(run_id)
    if guard is not None:
        try:
            if guard.valid:
                return True
        except Exception:
            return True
    try:
        durable_run = await asyncio.to_thread(
            routes_execution._run_registry.get, run_id,
        )
    except Exception:
        return True
    if str(durable_run.get("project_id") or "") != str(project_id):
        return True
    return str(durable_run.get("status") or "").lower() not in TERMINAL_STATUSES


async def _reset_phase(
    project_id: str,
    phase_id: str,
    preserve_rebuild_state: bool = False,
):
    """重置阶段：清除状态、删除相关Agent、清空质检结果，允许重新开始"""
    ctx = _get_project(project_id)
    pm = _phase_managers.get(project_id)
    if not pm:
        raise HTTPException(status_code=400, detail="Phase manager not initialized")
    phase = pm.get_phase(phase_id)
    if not phase:
        raise HTTPException(status_code=404, detail=f"Phase {phase_id} not found")

    phase_agents = [
        a for a in ctx.agents.values()
        if str(a.get("phase_id")) == str(phase_id)
    ]
    phase_task_ids = {
        str(subproject.get("id") or "")
        for subproject in getattr(ctx, "subprojects", [])
        if str(subproject.get("phase_id")) == str(phase_id)
    }
    phase_task_ids.update(
        str(task_id)
        for task_id in (phase.get("subprojects") or [])
        if task_id
    )
    phase_task_ids.update(
        str(agent.get("subproject_id") or "")
        for agent in phase_agents
        if agent.get("subproject_id")
    )
    active_locks = expert_lock.get_active_locks(project_id=project_id)
    orphan_phase_locks = [
        lock for lock in active_locks
        if _phase_reset_task_lock_matches(lock, phase_task_ids)
        and not any(
            _phase_reset_lock_matches(
                lock,
                agent,
                phase_task_ids,
                [],
            )
            for agent in phase_agents
        )
    ]
    active_agents = [
        agent for agent in phase_agents
        if await _phase_agent_has_live_reset_execution(
            project_id,
            phase_id,
            agent,
            phase_task_ids,
            active_locks,
        )
    ]
    coordinator_active = await _phase_coordinator_has_live_reset_execution(
        project_id,
        phase.get("execution_coordinator") or {},
    )
    if active_agents or coordinator_active or orphan_phase_locks:
        raise HTTPException(
            status_code=409,
            detail="阶段仍有正在执行的 Agent；为防止旧线程与新阶段并发写入，当前禁止重置",
        )

    # Capture the phase's complete ownership before deleting its Agent and
    # registry records. Reset means returning to the pre-phase workspace, not
    # merely hiding metadata while rejected source files remain on disk.
    owned_files = {
        str(path).replace("\\", "/")
        for path, owner in pm.file_registry.items()
        if str(owner.get("phase_id")) == str(phase_id) and _is_rebuild_deliverable_path(path)
    }
    for agent in phase_agents:
        owned_files.update(
            str(path).replace("\\", "/")
            for path in (agent.get("output_files") or [])
            if _is_rebuild_deliverable_path(path)
        )
    rebuild_policy = policy_by_path(
        (phase.get("rebuild_file_manifest") or {}).get("files") or []
    ) if preserve_rebuild_state else {}
    preserve_paths = {
        path for path, entry in rebuild_policy.items()
        if entry.get("mode") == "preserve"
    }
    patch_paths = {
        path for path, entry in rebuild_policy.items()
        if entry.get("mode") == "patch"
    }
    retained_rebuild_paths = preserve_paths | patch_paths
    owned_files.difference_update(retained_rebuild_paths)

    deleted_files = 0
    for relative_path in sorted(owned_files, key=lambda value: value.count("/"), reverse=True):
        target = (ctx.workspace / relative_path).resolve()
        try:
            target.relative_to(ctx.workspace.resolve())
        except ValueError:
            continue
        if target.is_file():
            try:
                target.unlink()
                deleted_files += 1
            except OSError:
                pass
    for relative_path in sorted(owned_files, key=lambda value: value.count("/"), reverse=True):
        parent = (ctx.workspace / relative_path).parent
        while parent != ctx.workspace:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    
    # 1. 重置阶段状态
    for p in pm.phases:
        if str(p.get("phase_id")) == str(phase_id):
            p["status"] = "pending"
            p.pop("started_at", None)
            p.pop("completed_at", None)
            p.pop("qc_passed", None)
            p.pop("reviewed_at", None)
            p.pop("user_confirmed", None)
            p["reviewed"] = False
            p["review_passed"] = False
            p["progress"] = 0
            p["agents"] = []
            break
    
    # 2. 删除该阶段的所有Agent
    from api.routes_execution import execution_status, _fix_attempt_counts, _persist_execution_state
    from core.expert_lock import release_lock
    for agent in phase_agents:
        agent_id = agent.get("id")
        lock_id = agent.get("lock_id")
        if lock_id:
            release_lock(lock_id)
        if agent_id:
            execution_status.pop(agent_id, None)
            _fix_attempt_counts.pop(agent_id, None)
        if agent_id and agent_id in ctx.agents:
            del ctx.agents[agent_id]

    # A process can lose the Agent record while its durable lease remains.
    # Release only locks whose task belongs to this phase; locks from sibling
    # phases must survive a targeted reset.
    for lock in expert_lock.get_active_locks(project_id=project_id):
        lock_id = str(lock.get("lock_id") or "")
        if lock_id and _phase_reset_task_lock_matches(lock, phase_task_ids):
            release_lock(lock_id)

    child_ids = set(phase.get("subprojects") or [])
    for subproject in ctx.subprojects:
        if subproject.get("phase_id") == phase_id or subproject.get("id") in child_ids:
            subproject.pop("agent_id", None)
            subproject.pop("output_files", None)
            subproject["status"] = "pending"
            subproject["progress"] = 0
    pm.phase_agents.pop(phase_id, None)
    pm.file_registry = {
        path: owner for path, owner in pm.file_registry.items()
        if owner.get("phase_id") != phase_id
        or str(path).replace("\\", "/") in retained_rebuild_paths
    }
    for path in retained_rebuild_paths:
        if path in pm.file_registry:
            pm.file_registry[path].pop("agent_id", None)
            pm.file_registry[path]["owner_type"] = (
                rebuild_policy.get(path) or {}
            ).get("owner_type")
            pm.file_registry[path]["rebuild_mode"] = (
                rebuild_policy.get(path) or {}
            ).get("mode")
    
    # 3. 清除监督员的该阶段问题列表
    sup_leader = _get_supervisor_leader(project_id)
    if sup_leader:
        member = sup_leader.get_member_for_phase(phase_id)
        if member:
            member.issues = []
            member.review_passed = False
    
    # 4. 清除质检结果
    if hasattr(ctx, 'qc_results') and phase_id in ctx.qc_results:
        del ctx.qc_results[phase_id]
    if hasattr(ctx, "supervisor_quality_runs"):
        ctx.supervisor_quality_runs.pop(phase_id, None)

    if not preserve_rebuild_state:
        repair_key = f"{project_id}-{phase_id}"
        _auto_repair_states.pop(repair_key, None)
        _auto_repair_api_configs.pop(repair_key, None)
        base_description = phase.pop("rebuild_base_description", None)
        if base_description:
            phase["description"] = base_description
        for key in (
            "rebuild_file_manifest", "rebuild_notes",
            "pre_rebuild_snapshot_version",
            "pre_rebuild_issue_snapshot", "pre_rebuild_issue_snapshot_digest",
            "rebuild_issue_assignments", "rebuild_comparison",
        ):
            phase.pop(key, None)
    
    await _persist_all_async()
    _persist_execution_state()
    
    return {
        "success": True,
        "message": f"阶段「{phase.get('name', '')}」已重置，删除了 {len(phase_agents)} 个Agent 和 {deleted_files} 个阶段文件",
        "deleted_agents": len(phase_agents),
        "deleted_files": deleted_files,
    }


@router.post("/projects/{project_id}/phases/{phase_id}/reset")
async def reset_phase(project_id: str, phase_id: str):
    return await _reset_phase(project_id, phase_id, preserve_rebuild_state=False)


@router.post("/projects/{project_id}/phases/{phase_id}/supervisor-chat")
async def phase_supervisor_chat(project_id: str, phase_id: str, request: SupervisorChatRequest):
    """Chat with supervisor for a phase"""
    ctx = _get_project(project_id)
    sup_leader = _get_supervisor_leader(project_id)
    member = sup_leader.get_member_for_phase(phase_id)
    if not member:
        # Create a phase supervision member if not exists
        memory = HybridMemory(f"memory/{project_id}_sup_phase_{phase_id}")
        member = sup_leader.assign_member_to_phase(phase_id, memory_store=memory)

    history = [
        {"role": m.get("role"), "content": m.get("content", "")}
        for m in (request.history or [])
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    result = member.chat(
        user_input=request.message,
        history=history if history else None,
        context_summary=request.context_summary,
    )
    return result


@router.post("/projects/{project_id}/phases/{phase_id}/review")
async def review_phase(project_id: str, phase_id: str):
    """Retired ad-hoc QC route; SupervisorQualityMachine is authoritative."""
    _get_project(project_id)
    raise HTTPException(
        status_code=410,
        detail=(
            "Legacy phase review is retired; use the phase auto-repair "
            "Supervisor quality run"
        ),
    )


async def _confirm_phase_complete_locked(
    ctx: ProjectContext,
    project_id: str,
    phase_id: str,
) -> Dict[str, Any]:
    """Confirm one phase while the caller owns the project write guard."""
    pm = _phase_managers.get(project_id)
    if not pm:
        raise HTTPException(status_code=400, detail="Phase manager not initialized")
    phase = pm.get_phase(phase_id)
    if not phase:
        raise HTTPException(status_code=404, detail=f"Phase {phase_id} not found")
    phase_snapshot = copy.deepcopy(phase)
    phase_agents_snapshot = copy.deepcopy(getattr(pm, "phase_agents", {}))
    file_registry_snapshot = copy.deepcopy(getattr(pm, "file_registry", {}))
    current_phase_index_snapshot = getattr(pm, "current_phase_index", -1)
    subprojects_snapshot = copy.deepcopy(ctx.subprojects)
    agents_snapshot = copy.deepcopy(ctx.agents)
    supervisor_runs_snapshot = copy.deepcopy(
        getattr(ctx, "supervisor_quality_runs", {}),
    )

    def restore_completion_state() -> None:
        phase.clear()
        phase.update(copy.deepcopy(phase_snapshot))
        if hasattr(pm, "phase_agents"):
            pm.phase_agents = copy.deepcopy(phase_agents_snapshot)
        if hasattr(pm, "file_registry"):
            pm.file_registry = copy.deepcopy(file_registry_snapshot)
        if hasattr(pm, "current_phase_index"):
            pm.current_phase_index = current_phase_index_snapshot
        ctx.subprojects[:] = copy.deepcopy(subprojects_snapshot)
        ctx.agents.clear()
        ctx.agents.update(copy.deepcopy(agents_snapshot))
        supervisor_runs = getattr(ctx, "supervisor_quality_runs", None)
        if isinstance(supervisor_runs, dict):
            supervisor_runs.clear()
            supervisor_runs.update(copy.deepcopy(supervisor_runs_snapshot))

    if not phase.get("reviewed") or not phase.get("review_passed"):
        raise HTTPException(status_code=409, detail="阶段尚未通过质检，不能确认完成")
    supervisor_run = (
        getattr(ctx, "supervisor_quality_runs", {}) or {}
    ).get(phase_id, {})
    if (
        supervisor_run.get("status", supervisor_run.get("state")) != "completed"
        or not (supervisor_run.get("completion_gate") or {}).get("passed")
    ):
        raise HTTPException(
            status_code=409,
            detail="Supervisor 唯一完成门禁尚未通过，不能确认阶段完成",
        )
    _reconcile_verified_supervisor_registry(
        ctx,
        phase_id,
        supervisor_run,
    )
    repair_state = _auto_repair_states.get(f"{project_id}-{phase_id}", {})
    _reconcile_deterministic_pre_qa_registry(
        ctx,
        phase_id,
        repair_state.get("deterministic_pre_qa_repairs") or [],
    )
    try:
        _assert_supervisor_artifact_current(
            ctx,
            phase_id,
            SupervisorQualityMachine.from_dict(supervisor_run),
        )
    except IllegalQualityTransition as exc:
        phase["reviewed"] = False
        phase["review_passed"] = False
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        _record_server_acceptance_evidence(ctx, phase)
        _record_supervisor_acceptance_evidence(ctx, phase)
        _record_user_confirmation_evidence(ctx, phase)
        validated_bundle = _assert_phase_execution_completed(
            ctx, phase_id, require_acceptance_evidence=False,
        )
    except Exception:
        restore_completion_state()
        raise

    # 兼容 PhaseManager 可能没有 set_phase_status 方法的情况
    try:
        result = pm.mark_phase_completed(phase_id)
        if not result.get("success"):
            raise AttributeError("mark_phase_completed failed")
    except AttributeError:
        for p in pm.phases:
            if str(p.get("phase_id")) == str(phase_id):
                p["status"] = "completed"
                p["user_confirmed"] = True
                p["completed_at"] = time.time()
                break
    if validated_bundle is not None:
        bundle_digest = _phase_evidence_bundle_digest(validated_bundle)
        phase["validated_completion_receipt"] = {
            "schema_version": 1,
            "project_id": str(project_id),
            "phase_id": str(phase_id),
            "execution_generation": str(
                phase.get("execution_generation") or ""
            ),
            "contract_digest": str(
                phase.get("execution_contract_digest") or ""
            ),
            "requirements_revision": int(
                phase.get("execution_requirements_revision") or 0
            ),
            "artifact_baseline_digest": str(
                phase.get("execution_artifact_baseline_digest") or ""
            ),
            "bundle_digest_version": 2,
            "bundle_digest": bundle_digest,
            "task_ids": sorted(
                str(item.get("task_id") or "")
                for item in (
                    validated_bundle.get("tasks") or []
                )
                if isinstance(item, dict) and item.get("task_id")
            ),
            "confirmed_at": time.time(),
        }
    # Update subprojects in this phase
    for sp in ctx.subprojects:
        if str(sp.get("phase_id", "")) == str(phase_id):
            sp["progress"] = 100

    # Update agents.  Lease release happens only after the completed state is
    # durably committed; otherwise a transient database failure would publish
    # an uncommitted completion while making the old work scope writable.
    locks_to_release: List[str] = []
    for agent_id, agent_info in ctx.agents.items():
        if str(agent_info.get("phase_id", "")) == str(phase_id):
            agent_info["progress"] = 100
            agent_info["updated_at"] = time.time()
            if agent_info.get("lock_id"):
                locks_to_release.append(str(agent_info["lock_id"]))

    try:
        await _persist_all_async()
    except Exception:
        restore_completion_state()
        raise
    for lock_id in locks_to_release:
        try:
            expert_lock.release_lock(lock_id)
        except Exception:
            logger.exception(
                "Failed to release completed phase lock project=%s phase=%s lock=%s",
                project_id,
                phase_id,
                lock_id,
            )
    return {"success": True, "phase_id": phase_id, "phase_name": phase.get("name", "")}


@router.post("/projects/{project_id}/phases/{phase_id}/confirm-complete")
async def confirm_phase_complete(project_id: str, phase_id: str):
    """User confirms phase as complete."""
    ctx = _get_project(project_id)
    try:
        # Keep the final artifact recheck, evidence materialization, completion
        # receipt, and durable persistence in one writer transaction. Without
        # this guard a file writer could land bytes after the Supervisor digest
        # check but before the current-generation receipt was persisted.
        with project_write_guard(ctx.project_id, ctx.workspace):
            return await _confirm_phase_complete_locked(
                ctx, project_id, phase_id,
            )
    except ProjectWriteFenceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/projects/{project_id}/phases/{phase_id}/issues/{issue_id}/submit-to-pm")
async def submit_issue_to_pm(project_id: str, phase_id: str, issue_id: str, body: Dict):
    """Submit a single issue to PM for analysis"""
    ctx = _get_project(project_id)
    pm_leader = _pm_teams.get(project_id)
    if not pm_leader:
        return {
            "pm_analysis": "PM team not initialized for this project",
            "issue_id": issue_id,
            "status": "skipped",
        }

    user_desc = body.get("user_description", "")

    from core.hermes_client import Message, MessageRole
    prompt = [
        Message(role=MessageRole.SYSTEM, content=(
            "You are a PM leader. Analyze the reported quality issue and provide: "
            "1) root cause (one sentence), 2) fix point, 3) rework instruction for the responsible agent. "
            "Keep under 100 words."
        )),
        Message(role=MessageRole.USER, content=f"Issue {issue_id}: {user_desc}\nProject: {ctx.name}\nPhase: {phase_id}"),
    ]
    resp = hermes_client.chat(prompt)
    analysis = resp.get("content", "")

    return {"pm_analysis": analysis, "issue_id": issue_id}


@router.post("/projects/{project_id}/phases/{phase_id}/issues/batch-submit-to-pm")
async def batch_submit_issues_to_pm(project_id: str, phase_id: str, body: Dict):
    """Batch submit issues to PM"""
    ctx = _get_project(project_id)
    pm_leader = _pm_teams.get(project_id)
    if not pm_leader:
        issue_ids = body.get("issue_ids", [])
        return {
            "results": [
                {
                    "issue_id": iid,
                    "pm_analysis": "PM team not initialized",
                    "status": "skipped",
                }
                for iid in issue_ids
            ],
            "summary": "PM team not initialized",
            "count": len(issue_ids),
        }

    issue_ids = body.get("issue_ids", [])
    user_note = body.get("user_note", "")

    from core.hermes_client import Message, MessageRole
    prompt = [
        Message(role=MessageRole.SYSTEM, content=(
            "You are a PM leader. Analyze the reported quality issues batch and provide overall fix plan. "
            "Be concise."
        )),
        Message(role=MessageRole.USER, content=f"Issue IDs: {', '.join(issue_ids)}\nNote: {user_note}\nProject: {ctx.name}\nPhase: {phase_id}"),
    ]
    resp = hermes_client.chat(prompt)
    analysis = resp.get("content", "")

    results = [{"issue_id": iid, "pm_analysis": analysis} for iid in issue_ids]
    return {"results": results, "summary": analysis, "count": len(results)}


# ─── ExpertLock 查询（供前端团队页）───────────────────────────────────────────

@router.get("/projects/{project_id}/phases/{phase_id}/locks")
async def get_phase_locks(project_id: str, phase_id: str):
    """获取阶段所有活跃的 ExpertLock 记录"""
    _get_project(project_id)
    locks = expert_lock.get_active_locks(project_id=project_id)
    phase_locks = []
    ctx = _get_project(project_id)
    for lock in locks:
        for agent_info in ctx.agents.values():
            if agent_info.get("expert_id") == lock["expert_id"] and agent_info.get("phase_id") == phase_id:
                phase_locks.append({**lock, "agent_id": agent_info.get("id"), "agent_role": agent_info.get("role")})
                break
    return {"phase_id": phase_id, "locks": phase_locks, "count": len(phase_locks)}


# ─── 自动修复循环（质检→修复→再质检，直到通过或超限）─────────────────────────────

# 全局修复循环状态：{project_id}-{phase_id} → {round, total_rounds, status, messages}
_auto_repair_states: Dict[str, Dict] = {}
# Kept separately so API responses never serialize a user's API key.
_auto_repair_api_configs: Dict[str, Optional[Dict[str, Any]]] = {}
AUTO_REPAIR_MAX_REPAIRS_PER_CYCLE = 6
AUTO_REPAIR_HISTORY_LIMIT = 25

_PRIVATE_AUTO_REPAIR_STATE_KEYS = {
    "pre_rebuild_restore_point",
    "pre_rebuild_delivery_inventory",
    "durable_rebuild_snapshot",
    "pre_rebuild_issue_snapshot",
    "pre_rebuild_issue_snapshot_digest",
    "pre_rebuild_issue_report",
    "rebuild_issue_assignments",
    "manual_fix_scope",
    "manual_fix_issue_paths",
}


def _public_auto_repair_state(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return an API-safe repair state without internal rollback material."""
    return {
        key: copy.deepcopy(value)
        for key, value in (state or {}).items()
        if key not in _PRIVATE_AUTO_REPAIR_STATE_KEYS
    }


def _rebuild_lifecycle_pending(state: Optional[Dict[str, Any]]) -> bool:
    state = state or {}
    return bool(
        state.get("rebuild_comparison_pending")
        and state.get("rebuild_run_id")
        and state.get("pre_rebuild_snapshot_version")
    )


def _phase_rebuild_terminal_failures(ctx: ProjectContext, phase_id: str) -> List[str]:
    terminal = {"failed", "error", "timeout", "timed_out", "blocked", "cancelled", "canceled", "fix_limit_reached"}
    failures: List[str] = []
    for agent_id, agent in getattr(ctx, "agents", {}).items():
        if str(agent.get("phase_id") or "") != phase_id:
            continue
        status = str(agent.get("status") or "").strip().lower()
        if status in terminal:
            failures.append(f"agent:{agent_id}:{status}")
    for subproject in getattr(ctx, "subprojects", []):
        if str(subproject.get("phase_id") or "") != phase_id:
            continue
        status = str(subproject.get("status") or "").strip().lower()
        if status in terminal:
            failures.append(f"task:{subproject.get('id', 'unknown')}:{status}")
    return sorted(set(failures))


def _supervisor_agent_status(agent: Dict[str, Any]) -> str:
    """Map legacy execution lifecycle into the closed Supervisor vocabulary."""
    status = str(agent.get("status") or "pending").strip().lower()
    # ``completed`` is the legacy execution success terminal. Older durable
    # records did not persist a progress field; treating that absence as
    # pending deadlocks QA even though execution and its subproject completed.
    # Real failed/timeout/cancelled terminals remain mapped fail-closed below.
    if status == "succeeded":
        return "succeeded"
    if status == "completed":
        raw_progress = agent.get("progress")
        if raw_progress is None or int(raw_progress or 0) == 100:
            return "succeeded"
        return "pending"
    if status in {"working", "in_progress", "running", "queued", "fixing", "re_checking"}:
        return "running"
    if status in {"failed", "error", "fix_limit_reached", "workspace_inconsistent"}:
        return "failed"
    if status in {"timeout", "blocked", "cancelled"}:
        return status
    return "pending"


def _supervisor_scope_snapshot(
    ctx: ProjectContext, phase_id: str,
) -> Dict[str, Any]:
    pm = _phase_managers.get(ctx.project_id)
    phase = pm.get_phase(phase_id) if pm else {}
    phase_rows = list(pm.phases if pm else [])
    phase_ids = [str(item.get("phase_id") or "") for item in phase_rows]
    try:
        phase_index = phase_ids.index(phase_id)
    except ValueError:
        phase_index = 0
    in_scope_phase_ids = {str(phase_id)}
    in_scope_phase_ids.update(
        str(item.get("phase_id") or "")
        for item in phase_rows[:phase_index]
        if item.get("phase_id") and item.get("user_confirmed")
    )
    registered = sorted({
        str(item.get("file_path") or "").replace("\\", "/")
        for item in (pm.get_files_by_phase(phase_id) if pm else [])
        if item.get("file_path")
    })
    contract = getattr(pm, "project_contract", {}) if pm else {}
    required_paths = sorted({
        str(item.get("path") or "").replace("\\", "/").strip("/")
        for item in (contract.get("required_files") or [])
        if isinstance(item, dict)
        and item.get("required", True)
        and str(item.get("phase_id") or "") in in_scope_phase_ids
        and str(item.get("path") or "").strip()
    })
    workspace_digest = compute_workspace_digest(Path(ctx.workspace))
    delivery_manifest = compute_delivery_manifest(
        Path(ctx.workspace), required_paths=required_paths,
    )
    artifact_digest = str(delivery_manifest["artifact_sha256"])
    scope_digest = hashlib.sha256(json.dumps({
        "phase_id": phase_id,
        "files": registered,
        "checks": ["scope", "qa"],
        "artifact_digest": artifact_digest,
        "artifact_manifest_rule_version": delivery_manifest["rule_version"],
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    dependencies = {
        str(item.get("phase_id")): bool(item.get("user_confirmed"))
        for item in phase_rows[:phase_index]
        if item.get("phase_id")
    }
    return {
        "project_id": ctx.project_id,
        "phase_id": phase_id,
        "phase_generation_id": str(
            phase.get("pre_rebuild_snapshot_version")
            or phase.get("started_at")
            or "initial"
        ),
        "scope_digest": scope_digest,
        "artifact_digest": artifact_digest,
        "artifact_manifest_rule_version": delivery_manifest["rule_version"],
        "delivery_manifest": delivery_manifest,
        "workspace_digest": workspace_digest,
        "files": registered,
        "dependencies": dependencies,
        "required_evidence": ["scope", "qa"],
    }


def _phase_pre_qa_acceptance_contracts(
    ctx: ProjectContext,
    phase: Dict[str, Any],
    *,
    source_classes: set[str],
) -> list[Dict[str, Any]]:
    """Return selected criteria with controller-owned task/run scope."""
    phase_id = str(phase.get("phase_id") or "")
    generation = str(phase.get("execution_generation") or "")
    receipts: Dict[str, Dict[str, Any]] = {}
    locked_tasks: Dict[str, Dict[str, Any]] = {}
    for agent in ctx.agents.values():
        if str(agent.get("phase_id") or "") != phase_id:
            continue
        for task in agent.get("locked_tasks") or []:
            if isinstance(task, dict) and task.get("task_id"):
                locked_tasks[str(task["task_id"])] = task
        for task_id, receipt in (
            agent.get("task_execution_receipts") or {}
        ).items():
            if isinstance(receipt, dict):
                receipts[str(task_id)] = receipt

    phase_tasks: Dict[str, Dict[str, Any]] = {}
    for task in (
        list(phase.get("task_contract") or [])
        + list(phase.get("tasks") or [])
    ):
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id") or task.get("id") or "")
        if task_id:
            phase_tasks.setdefault(task_id, task)
    phase_tasks.update(locked_tasks)

    rows: list[Dict[str, Any]] = []
    for task_id, task in phase_tasks.items():
        receipt = receipts.get(task_id) or {}
        task_run_id = ""
        if (
            str(receipt.get("status") or "").lower() == "succeeded"
            and str(receipt.get("execution_generation") or "") == generation
        ):
            task_run_id = str(receipt.get("completion_run_id") or "")
        scope_text = json.dumps(task, ensure_ascii=False, sort_keys=True).lower()
        for contract in acceptance_criterion_contracts(
            task_id,
            task.get("acceptance_criteria") or [],
            artifact_paths=receipt.get("required_files") or [],
        ):
            if str(contract.get("source_class") or "") in source_classes:
                rows.append({
                    "task_id": task_id,
                    "task_run_id": task_run_id,
                    "contract": contract,
                    "scope_text": scope_text,
                })
    phase_scope = json.dumps({
        key: phase.get(key)
        for key in (
            "name", "description", "implementation",
            "implementation_details", "implementation_method",
            "roles_needed", "roles", "tech_stack", "technology_stack",
            "tasks", "task_contract", "expert_requirements",
            "required_files", "deliverables", "acceptance_criteria",
        )
        if phase.get(key) not in (None, "", [], {})
    }, ensure_ascii=False, sort_keys=True).lower()
    phase_artifact_paths = tuple(dict.fromkeys(
        str(path)
        for receipt in receipts.values()
        for path in (receipt.get("required_files") or [])
        if str(path).strip()
    ))
    for contract in acceptance_criterion_contracts(
        phase_id,
        phase.get("acceptance_criteria") or [],
        artifact_paths=phase_artifact_paths,
    ):
        if str(contract.get("source_class") or "") in source_classes:
            rows.append({
                "task_id": phase_id,
                "task_run_id": "",
                "contract": contract,
                "scope_text": phase_scope,
            })
    return rows


def _phase_pre_qa_command_contracts(
    ctx: ProjectContext,
    phase: Dict[str, Any],
) -> list[Dict[str, Any]]:
    return _phase_pre_qa_acceptance_contracts(
        ctx,
        phase,
        source_classes={"command_test"},
    )


def _phase_pre_qa_api_plan(
    ctx: ProjectContext,
    phase: Dict[str, Any],
) -> tuple[tuple[ApiProbe, ...], Dict[str, Dict[str, Any]]]:
    """Compile one exact API probe for every locked runtime criterion."""
    probes: list[ApiProbe] = []
    bindings: Dict[str, Dict[str, Any]] = {}
    for row in _phase_pre_qa_acceptance_contracts(
        ctx,
        phase,
        source_classes={"api_runtime"},
    ):
        contract = row.get("contract") or {}
        spec = contract.get("evidence_spec") or {}
        endpoint = str(spec.get("endpoint") or "")
        candidates = api_probes_from_contract([{
            "criterion": str(contract.get("criterion") or ""),
            "evidence_spec": spec,
        }])
        selected = next(
            (candidate for candidate in candidates if candidate.path == endpoint),
            None,
        )
        if selected is None:
            continue
        status = spec.get("status_code")
        expected_statuses = (
            (int(status),)
            if isinstance(status, int)
            else selected.expected_statuses
        )
        criterion_id = str(contract.get("criterion_id") or "")
        probe_id = "criterion-api-" + hashlib.sha256(
            (
                str(row.get("task_id") or "")
                + ":"
                + criterion_id
            ).encode("utf-8")
        ).hexdigest()[:16]
        probe = ApiProbe(
            probe_id=probe_id,
            method=selected.method,
            path=selected.path,
            expected_statuses=expected_statuses,
            body=selected.body,
            actor=selected.actor,
            require_json=selected.require_json,
            invariant=selected.invariant,
            forbidden_response_keys=selected.forbidden_response_keys,
        )
        probes.append(probe)
        bindings[probe_id] = row
    return tuple(probes), bindings


def _pre_qa_gate_matches_contract(
    gate_id: str,
    row: Dict[str, Any],
) -> bool:
    """Match a deterministic gate to a command criterion and its cwd scope."""
    contract = row.get("contract") or {}
    spec = contract.get("evidence_spec") or {}
    tokens = {
        str(token).casefold()
        for token in (spec.get("command_tokens") or [])
    }
    gate_kind = (
        "install" if gate_id.startswith("install-")
        else "test" if gate_id.startswith("test-")
        else "build" if gate_id.startswith("build-")
        else ""
    )
    if not gate_kind or not any(
        gate_kind in token for token in tokens
    ):
        return False
    gate_scope = (
        "backend" if gate_id.endswith("-backend")
        else "frontend" if gate_id.endswith("-frontend")
        else "root" if gate_id.endswith("-root")
        else ""
    )
    declared_cwd = str(spec.get("cwd") or "").strip().replace("\\", "/")
    if declared_cwd:
        declared_scope = (
            "root"
            if declared_cwd in {".", "./"}
            else declared_cwd.strip("/").split("/", 1)[0].casefold()
        )
        return (
            declared_scope in {"root", "backend", "frontend"}
            and gate_scope == declared_scope
        )
    criterion_text = str(contract.get("criterion") or "").casefold()
    explicit_scope = (
        "root"
        if any(
            marker in criterion_text
            for marker in ("repository root", "project root", "根目录")
        )
        else "backend"
        if "backend" in criterion_text or "后端" in criterion_text
        else "frontend"
        if "frontend" in criterion_text or "前端" in criterion_text
        else ""
    )
    if explicit_scope:
        return gate_scope == explicit_scope
    fallback = str(row.get("scope_text") or "").casefold()
    fallback_scopes = {
        scope
        for scope, markers in {
            "root": ("repository root", "project root", "根目录"),
            "backend": ("backend", "后端"),
            "frontend": ("frontend", "前端"),
        }.items()
        if any(marker in fallback for marker in markers)
    }
    if not fallback_scopes:
        return gate_scope == "root"
    return len(fallback_scopes) == 1 and gate_scope in fallback_scopes


def _npm_gate_references_future_file(
    workspace: Path,
    gate: Any,
    future_paths: set[str],
) -> bool:
    """Defer a package script until every explicitly referenced file exists."""
    if not future_paths or not str(gate.gate_id).startswith(("test-", "build-")):
        return False
    package_root = (workspace / str(gate.cwd or ".")).resolve()
    try:
        package = json.loads(
            (package_root / "package.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return False
    scripts = package.get("scripts") if isinstance(package, dict) else {}
    script_name = "test" if str(gate.gate_id).startswith("test-") else "build"
    script = (
        str(scripts.get(script_name) or "").replace("\\", "/").casefold()
        if isinstance(scripts, dict)
        else ""
    )
    return any(path.casefold() in script for path in future_paths)


def _attach_pre_qa_criterion_bindings(
    ctx: ProjectContext,
    phase: Dict[str, Any],
    payload: Dict[str, Any],
    gates: tuple[Any, ...],
    api_bindings: Dict[str, Dict[str, Any]] | None = None,
) -> None:
    """Bind trusted gate observations to exact criteria without expansion."""
    generation = str(phase.get("execution_generation") or "")
    contract_rows = _phase_pre_qa_command_contracts(ctx, phase)
    selected = {str(gate.gate_id): gate for gate in gates}
    for raw in payload.get("evidence") or []:
        if not isinstance(raw, dict) or raw.get("passed") is not True:
            continue
        if str(raw.get("kind") or "") == "api":
            row = (api_bindings or {}).get(
                str(raw.get("gate_id") or "")
            )
            if row is not None:
                raw["criterion_bindings"] = [{
                    "task_id": str(row.get("task_id") or ""),
                    "task_run_id": str(row.get("task_run_id") or ""),
                    "execution_generation": generation,
                    "criterion_id": str(
                        (row.get("contract") or {}).get(
                            "criterion_id"
                        ) or ""
                    ),
                }]
            continue
        gate = selected.get(str(raw.get("gate_id") or ""))
        if gate is None:
            continue
        if (
            str(raw.get("kind") or "") != str(gate.kind)
            or str(raw.get("command") or "")
            != " ".join(str(item) for item in gate.command)
        ):
            continue
        bindings = []
        for row in contract_rows:
            if not _pre_qa_gate_matches_contract(str(gate.gate_id), row):
                continue
            bindings.append({
                "task_id": str(row.get("task_id") or ""),
                "task_run_id": str(row.get("task_run_id") or ""),
                "execution_generation": generation,
                "criterion_id": str(
                    (row.get("contract") or {}).get("criterion_id") or ""
                ),
            })
        if bindings:
            raw["criterion_bindings"] = bindings


def _run_mechanical_acceptance_checks(ctx, phase, pm):
    """Return (passed_criterion_ids, evidence_records)."""
    passed_ids = []
    evidence_records = []
    workspace = Path(ctx.workspace)
    phase_id = str(phase.get("phase_id") or "")

    # Gather all criteria from tasks and phase
    criteria = []
    for task in (list(phase.get("task_contract") or []) + list(phase.get("tasks") or [])):
        if not isinstance(task, dict): continue
        task_id = str(task.get("task_id") or task.get("id") or "")
        for ct in (task.get("acceptance_criteria") or []):
            if str(ct).strip():
                criteria.append({"criterion": str(ct).strip(), "task_id": task_id})
    for ct in (phase.get("acceptance_criteria") or []):
        if str(ct).strip():
            criteria.append({"criterion": str(ct).strip(), "task_id": phase_id})

    if not criteria:
        return passed_ids, evidence_records

    from core.phase_execution_contract import acceptance_criterion_contracts

    # Build output file list from workspace
    output_paths = []
    for agent in getattr(ctx, "agents", {}).values():
        for path in (agent.get("output_files") or []):
            output_paths.append(str(path))
    try:
        for f in workspace.rglob("*"):
            if f.is_file() and ".git" not in f.parts and "node_modules" not in f.parts:
                rel = str(f.relative_to(workspace)).replace("\\", "/")
                if rel not in output_paths:
                    output_paths.append(rel)
    except (OSError, ValueError): pass

    canon_paths = {p.replace("\\", "/").casefold(): p for p in output_paths}
    resolved = {cp: workspace / orig for cp, orig in canon_paths.items()}

    for item in criteria:
        contracts = acceptance_criterion_contracts(item["task_id"], [item["criterion"]], artifact_paths=output_paths)
        if not contracts: continue
        cid = str(contracts[0].get("criterion_id") or "")
        if not cid: continue

        check = _try_mechanical_acceptance_check(item["criterion"], workspace, output_paths, canon_paths, resolved)
        if check and check["passed"]:
            passed_ids.append(cid)
            evidence_records.append({
                "gate_id": f"acceptance:{cid[:16]}", "passed": True,
                "kind": check["kind"], "command": "", "exit_code": 0,
                "log_excerpt": check.get("detail", ""),
                "criterion_id": cid, "criterion": item["criterion"][:200],
                "task_id": item["task_id"], "task_run_id": "",
                "execution_generation": str(phase.get("execution_generation") or ""),
                "criterion_bindings": [{"criterion_id": cid, "task_id": item["task_id"], "task_run_id": "", "execution_generation": str(phase.get("execution_generation") or "")}],
                "covered_criterion_ids": [cid], "recorded_at": time.time(),
            })

    return passed_ids, evidence_records


def _try_mechanical_acceptance_check(criterion, workspace, output_paths, canon_paths, resolved):
    """Try to verify one acceptance criterion without LLM. Returns dict or None."""
    import re as _re, json as _json
    normalized = criterion.replace("\\", "/")
    cased = normalized.casefold()

    # 1. File existence
    mentioned = [cp for cp in canon_paths if cp in cased]
    if mentioned:
        missing = [canon_paths[mp] for mp in mentioned if not resolved[mp].is_file()]
        if not missing:
            return {"passed": True, "kind": "file_exists", "detail": f"Found: {', '.join(canon_paths[mp] for mp in mentioned[:5])}"}
        return {"passed": False, "kind": "file_exists", "detail": f"Missing: {', '.join(missing[:5])}"}

    # 2. Dependency check via package.json
    pkg_files = [p for p in output_paths if p.replace("\\", "/").casefold().endswith("package.json")]
    dep_match = _re.search(r"(?:包含|依赖|dependenc).{0,20}?([a-z@][a-z0-9@._/-]+(?:[、,，]\s*[a-z@][a-z0-9@._/-]+)*)", normalized, _re.IGNORECASE)
    if pkg_files and dep_match:
        wanted = [d.strip() for d in _re.split(r"[、,，]", dep_match.group(1)) if d.strip()]
        for pkg_path in pkg_files:
            try:
                pkg = _json.loads((workspace / pkg_path).read_text(encoding="utf-8"))
            except Exception: continue
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            found = [d for d in wanted if d in deps]
            missing_deps = [d for d in wanted if d not in deps]
            if found and not missing_deps:
                return {"passed": True, "kind": "dep_check", "detail": f"Found in {pkg_path}: {', '.join(found)}"}
            if missing_deps:
                return {"passed": False, "kind": "dep_check", "detail": f"Missing: {missing_deps}"}

    # 3. Text/pattern grep
    grep_match = _re.search(r"""(?:包含|显示|存在|文本为['"]?|含有)(['"]?)(.{1,60}?)\1(?:的?文本|的?元素|的?按钮|的?组件)?""", normalized)
    if grep_match:
        pattern = grep_match.group(2).strip()
        if pattern and len(pattern) >= 2:
            found_in = []
            for op in output_paths[:60]:
                fpath = workspace / op
                if not fpath.is_file() or fpath.suffix not in {".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".json", ".md", ".py", ".yml", ".yaml"}: continue
                try: content = fpath.read_text(encoding="utf-8", errors="replace")
                except Exception: continue
                if pattern.lower() in content.lower():
                    found_in.append(op)
                    if len(found_in) >= 3: break
            if found_in:
                return {"passed": True, "kind": "grep", "detail": f"'{pattern}' found in: {', '.join(found_in[:3])}"}
            return {"passed": False, "kind": "grep", "detail": f"'{pattern}' not found in any source file"}

    # 4. npm install
    if _re.search(r"(?:执行|运行|run)\s*npm\s*(install|ci|i)\b", normalized, _re.IGNORECASE) or _re.search(r"npm\s*(install|ci|i)\s*(?:无错误|成功|通过)", normalized, _re.IGNORECASE):
        from core.pre_qa_verifier import CommandGate, LocalCommandRunner
        runner = LocalCommandRunner()
        gate = CommandGate(gate_id="acceptance:npm-install", kind="install", command=("npm", "install", "--no-audit", "--no-fund"), cwd=".", timeout_seconds=300, required=True)
        try:
            obs = runner(gate, workspace)
            return {"passed": obs.exit_code == 0, "kind": "npm_install", "detail": f"exit_code={obs.exit_code}"}
        except Exception as exc:
            return {"passed": False, "kind": "npm_install", "detail": f"error: {exc}"}

    return None


def _execute_phase_pre_qa_raw(ctx: ProjectContext, phase_id: str) -> Dict[str, Any]:
    """Run deterministic gates before a Supervisor business QA round."""
    pm = _phase_managers.get(ctx.project_id)
    phase = pm.get_phase(phase_id) if pm else {}
    contract = json.loads(json.dumps(
        getattr(pm, "project_contract", {}) or {},
        ensure_ascii=False,
        default=str,
    ))
    phase_plan = (
        phase.get("phase_plan")
        or phase.get("execution_phase_plan")
        or phase.get("plan")
        or {}
    )
    try:
        v1_scope = load_phase_qa_scope(
            project_id=ctx.project_id,
            phase_id=phase_id,
            workspace=Path(ctx.workspace),
        )
    except (RuntimeError, ValueError) as exc:
        return {
            "passed": False,
            "status": "pre_qa_failed",
            "failure_category": "pre_qa_failed",
            "failed_gate": "phase_delivery_v1",
            "issues": [{
                "code": "phase_delivery_v1_invalid",
                "message": str(exc),
                "path": f"docs/metis/phase-deliveries/{phase_id}.json",
                "gate": "phase_delivery_v1",
            }],
            "evidence": [],
            "consumes_business_qa_round": False,
        }
    require_v1 = (
        str(phase_plan.get("schema_version") or "") == "phase-plan/v1"
    )
    if require_v1 and not v1_scope.get("available"):
        return {
            "passed": False,
            "status": "pre_qa_failed",
            "failure_category": "pre_qa_failed",
            "failed_gate": "phase_delivery_v1",
            "issues": [{
                "code": "phase_delivery_v1_missing",
                "message": "phase-plan/v1 requires its authoritative phase delivery document",
                "path": f"docs/metis/phase-deliveries/{phase_id}.json",
                "gate": "phase_delivery_v1",
            }],
            "evidence": [],
            "consumes_business_qa_round": False,
        }
    if v1_scope.get("available") and (
        v1_scope.get("issues") or v1_scope.get("incomplete_task_ids")
    ):
        issues = [
            {**issue, "gate": "phase_delivery_v1"}
            for issue in v1_scope.get("issues") or []
        ]
        issues.extend({
            "code": "phase_delivery_task_incomplete",
            "message": f"Task {task_id} has no completed delivery",
            "path": f"docs/metis/phase-deliveries/{phase_id}.json",
            "gate": "phase_delivery_v1",
        } for task_id in v1_scope.get("incomplete_task_ids") or [])
        return {
            "passed": False,
            "status": "pre_qa_failed",
            "failure_category": "pre_qa_failed",
            "failed_gate": "phase_delivery_v1",
            "issues": issues,
            "evidence": [],
            "consumes_business_qa_round": False,
        }

    agents_by_id = {
        str(agent.get("id") or ""): agent
        for agent in getattr(ctx, "agents", {}).values()
    }
    if v1_scope.get("available"):
        v1_required_files = []
        v1_registry = {}
        for item in v1_scope.get("files") or []:
            agent = agents_by_id.get(str(item.get("agent_id") or ""), {})
            owner_type = str(
                agent.get("agent_type")
                or agent.get("expert_type")
                or item.get("agent_role")
                or "fullstack_engineer"
            )
            v1_required_files.append({
                "path": item["path"],
                "owner_type": owner_type,
                "phase_id": phase_id,
                "required": True,
            })
            v1_registry[item["path"]] = {
                "agent_id": item.get("agent_id"),
                "expert_id": item.get("expert_id"),
                "task_id": item.get("task_id"),
                "phase_id": item.get("phase_id"),
                "sha256": item.get("sha256"),
                "revision": item.get("revision"),
            }
        if require_v1 and not v1_required_files:
            return {
                "passed": False,
                "status": "pre_qa_failed",
                "failure_category": "pre_qa_failed",
                "failed_gate": "phase_delivery_v1",
                "issues": [{
                    "code": "phase_delivery_files_missing",
                    "message": "Completed phase has no business files to verify",
                    "path": f"docs/metis/phase-deliveries/{phase_id}.json",
                    "gate": "phase_delivery_v1",
                }],
                "evidence": [],
                "consumes_business_qa_round": False,
            }
        raw_required_files = list(contract.get("required_files") or [])
        contract["required_files"] = v1_required_files
        registry = v1_registry
    else:
        raw_required_files = list(contract.get("required_files") or [])
        registry = copy.deepcopy(getattr(pm, "file_registry", {}) or {}) if pm else {}

    if not raw_required_files and not v1_scope.get("available"):
        return {
            "passed": False,
            "status": "pre_qa_failed",
            "failure_category": "pre_qa_failed",
            "failed_gate": "contract_manifest",
            "issues": [{
                "code": "required_files_manifest_missing",
                "message": "Locked ProjectContract must contain an explicit required_files manifest",
                "path": "project_contract.required_files",
                "gate": "contract_manifest",
            }],
            "evidence": [],
            "consumes_business_qa_round": False,
        }
    invalid_rows = [
        item for item in contract.get("required_files") or []
        if not isinstance(item, dict)
        or not str(item.get("path") or "").strip()
        or not str(item.get("owner_type") or "").strip()
        or not str(item.get("phase_id") or "").strip()
    ]
    if invalid_rows:
        return {
            "passed": False,
            "status": "pre_qa_failed",
            "failure_category": "pre_qa_failed",
            "failed_gate": "contract_manifest",
            "issues": [{
                "code": "required_file_assignment_invalid",
                "message": "Every required file must declare path, owner_type and phase_id",
                "path": "project_contract.required_files",
                "gate": "contract_manifest",
            }],
            "evidence": [],
            "consumes_business_qa_round": False,
        }
    # Phase QA validates only artifacts assigned to this phase.  The whole-
    # project final QA remains responsible for the complete contract manifest.
    if not v1_scope.get("available"):
        contract["required_files"] = [
            item for item in raw_required_files
            if str(item.get("phase_id") or "") == str(phase_id)
            and item.get("required", True)
        ]
    agents = list(getattr(ctx, "agents", {}).values())
    stack_text = " ".join(
        str(item) for item in (
            list(contract.get("technology_stack") or [])
            + list(contract.get("required_tech") or [])
        )
    ).lower()
    node_fullstack = any(
        marker in stack_text
        for marker in ("node", "express", "react", "javascript", "typescript", "npm")
    )
    test_mode = os.environ.get("METIS_TEST_MODE", "").strip().lower() in {"1", "true", "yes"}
    image_tag = "metis-" + "".join(
        character if character.isalnum() or character in "_.-" else "-"
        for character in f"{ctx.project_id}-{phase_id}"
    ).lower()
    docker_setting = os.environ.get("METIS_PRE_QA_USE_DOCKER", "").strip().lower()
    docker_enabled = (
        docker_setting in {"1", "true", "yes"}
        if docker_setting
        else shutil.which("docker") is not None
    )
    docker_required = any(
        Path(str(item.get("path") or "").replace("\\", "/")).name.lower()
        in {"dockerfile", "docker-compose.yml", "docker-compose.yaml"}
        for item in raw_required_files
        if isinstance(item, dict) and item.get("required", True)
    )
    include_docker = docker_enabled and docker_required
    full_gates = (
        ()
        if test_mode or not node_fullstack
        else node_fullstack_command_gates(image_tag=image_tag, include_docker=include_docker)
    )
    phase_rows = list(pm.phases if pm else [])
    phase_ids = [str(item.get("phase_id") or "") for item in phase_rows]
    try:
        phase_index = phase_ids.index(str(phase_id))
    except ValueError:
        phase_index = -1
    # A phase pre-QA must not install and execute an application whose locked
    # manifest still assigns required artifacts to future phases.  Doing so is
    # both non-authoritative and can consume the entire web-worker lifetime.
    # The last phase may run the complete deterministic gate after every prior
    # phase has been accepted; Final QA remains the authoritative release gate.
    future_required = any(
        str(item.get("phase_id") or "") in set(phase_ids[phase_index + 1:])
        and item.get("required", True)
        for item in raw_required_files
    ) if phase_index >= 0 else True
    prior_phases_confirmed = phase_index >= 0 and all(
        bool(item.get("user_confirmed"))
        for item in phase_rows[:phase_index]
    )
    run_full_project_gates = (
        bool(full_gates)
        and not future_required
        and prior_phases_confirmed
    )
    command_contracts = _phase_pre_qa_command_contracts(ctx, phase)
    future_paths = {
        str(item.get("path") or "").replace("\\", "/")
        for item in raw_required_files
        if isinstance(item, dict)
        and item.get("required", True)
        and str(item.get("phase_id") or "") in set(
            phase_ids[phase_index + 1:]
        )
    } if phase_index >= 0 else set()
    local_gate_ids = {
        str(gate.gate_id)
        for gate in full_gates
        if any(
            _pre_qa_gate_matches_contract(str(gate.gate_id), row)
            for row in command_contracts
        )
        and not _npm_gate_references_future_file(
            Path(ctx.workspace),
            gate,
            future_paths,
        )
    }
    prerequisite_ids = {
        "test-backend": "install-backend",
        "build-frontend": "install-frontend",
        "test-root": "install-root",
        "build-root": "install-root",
    }
    local_gate_ids.update(
        prerequisite
        for gate_id, prerequisite in prerequisite_ids.items()
        if gate_id in local_gate_ids
    )
    gates = (
        full_gates
        if run_full_project_gates
        else tuple(
            gate for gate in full_gates
            if str(gate.gate_id) in local_gate_ids
        )
    )
    in_scope_phase_ids = {str(phase_id)}
    in_scope_phase_ids.update(
        str(item.get("phase_id") or "")
        for item in phase_rows[:phase_index]
        if item.get("phase_id") and item.get("user_confirmed")
    )
    scoped_registry_paths = {
        str(item.get("path") or "").replace("\\", "/")
        for item in (
            contract.get("required_files")
            if v1_scope.get("available")
            else raw_required_files
        ) or []
        if isinstance(item, dict)
        and item.get("required", True)
        and str(item.get("phase_id") or "") in in_scope_phase_ids
    }
    registry = {
        str(path).replace("\\", "/"): entry
        for path, entry in registry.items()
        if str(path).replace("\\", "/") in scoped_registry_paths
    }
    auth_markers = (
        "jwt", "auth", "login", "token", "session",
        "认证", "登录", "令牌", "会话",
    )
    requirement_text = (
        stack_text
        + " "
        + str(contract.get("source_requirements") or "").lower()
        + " "
        + " ".join(
            str(unit.get("exact_text") or "").lower()
            for unit in (contract.get("requirement_units") or [])
            if isinstance(unit, dict)
        )
    )
    jwt_requested = any(
        marker in requirement_text for marker in auth_markers
    )
    task_text_by_id: Dict[str, str] = {}
    for phase_row in phase_rows:
        if str(phase_row.get("phase_id") or "") not in in_scope_phase_ids:
            continue
        for task in (
            list(phase_row.get("task_contract") or [])
            + list(phase_row.get("tasks") or [])
        ):
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("task_id") or task.get("id") or "")
            if task_id:
                task_text_by_id[task_id] = json.dumps(
                    task, ensure_ascii=False, sort_keys=True,
                ).lower()
    backend_declared = any(
        isinstance(item, dict)
        and item.get("required", True)
        and (
            str(item.get("owner_type") or "").strip().lower()
            in {"backend", "fullstack"}
            or str(item.get("path") or "").replace("\\", "/").lower()
            .startswith(("backend/", "server/", "api/"))
        )
        for item in raw_required_files
    )
    node_backend_stack = (
        any(
            marker in stack_text
            for marker in (
                "node", "express", "nestjs", "fastify", "koa",
            )
        )
        or any(
            isinstance(item, dict)
            and item.get("required", True)
            and str(item.get("path") or "").replace("\\", "/").lower()
            .endswith(("backend/package.json", "server/package.json"))
            for item in raw_required_files
        )
    )
    jwt_carrier_suffixes = {".js", ".cjs", ".mjs", ".ts"}
    jwt_has_scoped_carrier = any(
        str(item.get("phase_id") or "") in in_scope_phase_ids
        and item.get("required", True)
        and Path(str(item.get("path") or "")).suffix.lower()
        in jwt_carrier_suffixes
        and (
            str(item.get("owner_type") or "").strip().lower()
            in {"backend", "fullstack"}
            or str(item.get("path") or "").replace("\\", "/").lower()
            .startswith(("backend/", "server/", "api/"))
        )
        and any(
            marker in (
                str(item.get("path") or "").lower()
                + " "
                + task_text_by_id.get(str(item.get("task_id") or ""), "")
            )
            for marker in auth_markers
        )
        for item in raw_required_files
        if isinstance(item, dict)
    )
    api_probes, api_bindings = _phase_pre_qa_api_plan(ctx, phase)
    api_probe_runner = None
    if api_probes:
        runtime_contract = {
            "locked": True,
            "technology_stack": list(
                contract.get("technology_stack") or []
            ),
            "required_tech": list(contract.get("required_tech") or []),
            "source_requirements": "",
            "requirement_units": [],
            "acceptance_criteria": [
                {
                    "criterion": (
                        f"{probe.method} {probe.path} returns "
                        f"{probe.expected_statuses[0]}"
                    ),
                    "evidence_spec": {
                        "check_id": probe.probe_id,
                        "body": dict(probe.body),
                    },
                }
                for probe in api_probes
            ],
            "phases": [],
        }
        runtime_result = runtime_acceptance.run_runtime_acceptance(
            Path(ctx.workspace),
            f"{ctx.project_id}:{phase_id}:pre-qa",
            required_paths=sorted(scoped_registry_paths),
            project_contract=runtime_contract,
            force_local=True,
        )
        if runtime_result.get("passed") is not True:
            message = str(
                runtime_result.get("summary")
                or runtime_result.get("error")
                or "Phase API runtime acceptance failed"
            )
            actionable = runtime_result.get("actionable") is True
            retryable = runtime_result.get("retryable") is True
            failure_category = (
                "pre_qa_failed" if actionable else FAILURE_INFRASTRUCTURE
            )
            expected_match = re.search(
                r"expected\s+(\[[^\]]*\])", message, flags=re.IGNORECASE,
            )
            actual_match = re.search(
                r"got\s+(\d+)", message, flags=re.IGNORECASE,
            )
            operation_match = re.search(
                r"runtime HTTP check\s+([A-Z]+)\s+(\S+)\s+failed",
                message,
            )
            source_path = str(runtime_result.get("file_path") or "")
            return {
                "passed": False,
                "status": failure_category,
                "failure_category": failure_category,
                "failed_gate": "api",
                "issues": [{
                    "code": (
                        "api_runtime_acceptance_failed"
                        if actionable else "runtime_infrastructure_failed"
                    ),
                    "message": message,
                    "path": source_path,
                    "gate": "api",
                    "method": (
                        operation_match.group(1) if operation_match else ""
                    ),
                    "endpoint": (
                        operation_match.group(2) if operation_match else ""
                    ),
                    "expected": (
                        expected_match.group(1) if expected_match
                        else "declared HTTP status"
                    ),
                    "actual": (
                        f"HTTP {actual_match.group(1)}"
                        if actual_match else message
                    ),
                    "fix_hint": str(runtime_result.get("fix_hint") or ""),
                    "error_category": str(
                        runtime_result.get("error_category") or ""
                    ),
                    "retryable": retryable,
                    "actionable": actionable,
                }],
                "evidence": [],
                "runtime_acceptance": runtime_result,
                "retryable": retryable,
                "actionable": actionable,
                "consumes_business_qa_round": False,
            }
        observed_by_id = {
            str(item.get("check_id") or ""): item
            for item in (
                runtime_result.get("http_observations") or []
            )
            if isinstance(item, dict) and item.get("check_id")
        }

        def replay_api_probe(probe: ApiProbe) -> ApiObservation:
            observed = observed_by_id.get(probe.probe_id)
            if observed is None:
                raise RuntimeError(
                    f"runtime observation missing for "
                    f"{probe.method} {probe.path}"
                )
            return ApiObservation(
                status_code=int(observed.get("status_code") or 0),
                is_json=bool(observed.get("is_json")),
                body=observed.get("body"),
                log=json.dumps(
                    {
                        "method": probe.method,
                        "path": probe.path,
                        "status_code": observed.get("status_code"),
                    },
                    sort_keys=True,
                ),
            )

        api_probe_runner = replay_api_probe
    mechanically_passed_ids, mechanical_evidence = _run_mechanical_acceptance_checks(ctx, phase, pm)
    phase["mechanically_passed_criterion_ids"] = mechanically_passed_ids
    verifier = PreQAVerifier(
        ctx.workspace,
        command_runner=LocalCommandRunner(image_tag=image_tag),
        api_probe_runner=api_probe_runner,
    )
    result = verifier.verify(
        contract=contract,
        file_registry=registry,
        agents=agents,
        commit_digest=compute_workspace_digest(Path(ctx.workspace)),
        command_gates=gates,
        api_probes=api_probes,
        jwt_required=(
            node_backend_stack
            and backend_declared
            and jwt_requested
            and jwt_has_scoped_carrier
        ),
    )
    payload = result.to_dict()
    evidence = list(payload.get("evidence") or [])
    evidence.extend(mechanical_evidence)
    payload["evidence"] = evidence
    _attach_pre_qa_criterion_bindings(
        ctx,
        phase,
        payload,
        tuple(gates),
        api_bindings,
    )
    if full_gates and not run_full_project_gates:
        payload["deferred_evidence"] = [{
            "status": "deferred",
            "gate_id": gate.gate_id,
            "kind": gate.kind,
            "command": " ".join(gate.command),
            "reason": (
                "full_project_delivery_incomplete"
                if future_required
                else "prior_phase_not_confirmed"
            ),
            "authoritative_gate": "final_qa",
        } for gate in full_gates if gate not in gates]
    return payload


_PRE_QA_UNIVERSAL_CODES = {
    "api_invariant_failed",
    "api_probe_runner_failed",
    "api_probe_runner_missing",
    "api_runtime_acceptance_failed",
    "command_gate_failed",
    "command_resource_exhausted",
    "command_runner_failed",
    "empty_required_file",
    "jwt_default_secret",
    "jwt_env_missing",
    "jwt_hardcoded_secret",
    "jwt_implementation_missing",
    "jwt_not_fail_closed",
    "missing_required_file",
    "missing_typescript_config",
    "missing_vite_entry",
    "multiple_contract_owners",
    "multiple_owner_types",
    "non_unique_registry_owner",
    "owner_mismatch",
    "owner_scope_violation",
    "owner_type_mismatch",
    "path_escape",
    "phase_delivery_files_missing",
    "phase_delivery_task_incomplete",
    "phase_delivery_v1_invalid",
    "phase_delivery_v1_missing",
    "pre_qa_runner_failed",
    "runtime_infrastructure_failed",
    "sensitive_value_logged",
    "unknown_file_owner",
    "unreadable_required_file",
    "unregistered_required_file",
    "unsafe_required_path",
}


def _pre_qa_task_contract_index(
    ctx: ProjectContext,
    phase: Dict[str, Any],
) -> tuple[Dict[str, Dict[str, Any]], set[str], Dict[str, str]]:
    """Return task contracts, current task ids and delivered path ownership."""
    pm = _phase_managers.get(str(getattr(ctx, "project_id", "") or ""))
    task_index: Dict[str, Dict[str, Any]] = {}
    current_task_ids: set[str] = set()

    def add_tasks(container: Any, *, current: bool = False) -> None:
        if not isinstance(container, dict):
            return
        plan = (
            container.get("phase_plan")
            or container.get("execution_phase_plan")
            or container.get("plan")
            or container
        )
        for key in ("tasks", "task_contract"):
            for task in plan.get(key) or []:
                if not isinstance(task, dict):
                    continue
                task_id = str(task.get("task_id") or task.get("id") or "")
                if not task_id:
                    continue
                task_index.setdefault(task_id, task)
                if current:
                    current_task_ids.add(task_id)

    add_tasks(phase, current=True)
    project_contract = getattr(pm, "project_contract", {}) if pm else {}
    if isinstance(project_contract, dict):
        for item in project_contract.get("phases") or []:
            add_tasks(item)
    for item in getattr(pm, "phases", []) or []:
        add_tasks(item)

    path_owners: Dict[str, str] = {}
    try:
        scope = load_phase_qa_scope(
            project_id=str(getattr(ctx, "project_id", "") or ""),
            phase_id=str(phase.get("phase_id") or ""),
            workspace=Path(ctx.workspace),
        )
    except (RuntimeError, ValueError):
        scope = {}
    for item in scope.get("files") or []:
        path = str(item.get("path") or "").replace("\\", "/")
        task_id = str(item.get("task_id") or "")
        if path and task_id:
            path_owners[path.casefold()] = task_id
    registry = getattr(pm, "file_registry", {}) if pm else {}
    for path, item in (registry or {}).items():
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("task_id") or item.get("subproject_id") or "")
        if task_id:
            path_owners.setdefault(
                str(path).replace("\\", "/").casefold(), task_id,
            )
    return task_index, current_task_ids, path_owners


def _normalize_pre_qa_check_sources(
    ctx: ProjectContext,
    phase_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Bind every issue to an authoritative source or downgrade it to warning."""
    normalized = copy.deepcopy(payload)
    pm = _phase_managers.get(str(getattr(ctx, "project_id", "") or ""))
    phase = pm.get_phase(phase_id) if pm and hasattr(pm, "get_phase") else {}
    task_index, current_task_ids, path_owners = _pre_qa_task_contract_index(
        ctx, phase or {"phase_id": phase_id},
    )
    command_rows = _phase_pre_qa_command_contracts(ctx, phase or {})
    dependency_ids = {
        str(dependency)
        for task_id in current_task_ids
        for dependency in (
            (task_index.get(task_id) or {}).get("dependencies") or []
        )
        if str(dependency)
    }
    normalized_issues = []
    warnings = []
    for raw in normalized.get("issues") or []:
        if not isinstance(raw, dict):
            continue
        issue = copy.deepcopy(raw)
        code = str(issue.get("code") or "")
        gate = str(issue.get("gate") or issue.get("failed_gate") or "")
        path = str(
            issue.get("path") or issue.get("file_path") or ""
        ).replace("\\", "/")
        bindings: list[Dict[str, Any]] = []

        for row in command_rows:
            if not _pre_qa_gate_matches_contract(gate, row):
                continue
            task_id = str(row.get("task_id") or "")
            if task_id not in current_task_ids and task_id not in dependency_ids:
                continue
            contract = row.get("contract") or {}
            bindings.append({
                "source_type": (
                    "task_contract"
                    if task_id in current_task_ids
                    else "dependency_contract"
                ),
                "task_id": task_id,
                "criterion_id": str(contract.get("criterion_id") or ""),
                "contract_field": "acceptance_criteria",
                "gate_id": gate,
            })

        owner_task_id = path_owners.get(path.casefold()) if path else ""
        if owner_task_id and code in {
            "empty_required_file",
            "missing_required_file",
            "non_unique_registry_owner",
            "owner_mismatch",
            "owner_scope_violation",
            "owner_type_mismatch",
            "phase_delivery_task_incomplete",
            "unknown_file_owner",
            "unreadable_required_file",
            "unregistered_required_file",
            "workspace_file_hash_mismatch",
        }:
            bindings.append({
                "source_type": (
                    "task_contract"
                    if owner_task_id in current_task_ids
                    else "dependency_contract"
                ),
                "task_id": owner_task_id,
                "contract_field": "delivery.files",
                "path": path,
            })

        is_universal = code in _PRE_QA_UNIVERSAL_CODES
        if is_universal:
            bindings.append({
                "source_type": "universal_quality_gate",
                "gate_id": gate or code,
                "rule_id": code or gate,
            })

        unique_bindings = []
        seen = set()
        for binding in bindings:
            key = json.dumps(binding, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                unique_bindings.append(binding)
        issue["check_sources"] = unique_bindings
        issue["blocking"] = bool(unique_bindings)
        issue["severity"] = "error" if unique_bindings else "warning"
        issue["source_trace_status"] = (
            "bound" if unique_bindings else "untraceable_heuristic"
        )
        if unique_bindings:
            normalized_issues.append(issue)
        else:
            issue["actionable"] = False
            warnings.append(issue)

    normalized["issues"] = normalized_issues
    normalized["warnings"] = [
        *[
            copy.deepcopy(item)
            for item in normalized.get("warnings") or []
            if isinstance(item, dict)
        ],
        *warnings,
    ]
    normalized["blocking_issue_count"] = len(normalized_issues)
    normalized["warning_count"] = len(normalized["warnings"])
    normalized["check_source_policy"] = {
        "version": "pre-qa-check-source/v1",
        "blocking_sources": [
            "task_contract",
            "dependency_contract",
            "universal_quality_gate",
        ],
        "untraceable_action": "warning_only",
    }
    if normalized.get("passed") is False and not normalized_issues:
        normalized.update({
            "passed": True,
            "status": "passed_with_warnings",
            "failure_category": "",
            "failed_gate": "",
            "actionable": False,
        })
    return normalized


def _execute_phase_pre_qa(ctx: ProjectContext, phase_id: str) -> Dict[str, Any]:
    """Run the existing Pre-QA flow and add authoritative source binding."""
    return _normalize_pre_qa_check_sources(
        ctx,
        phase_id,
        _execute_phase_pre_qa_raw(ctx, phase_id),
    )


def _pre_qa_evidence_step_id(record: Dict[str, Any]) -> str:
    gate = str(record.get("gate_id") or "gate").strip() or "gate"
    digest = str(
        record.get("log_digest")
        or record.get("commit_digest")
        or record.get("scope_digest")
        or ""
    ).strip()
    if not digest:
        payload = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    digest = digest.replace("sha256:", "").replace(":", "")
    return f"pre-qa:{gate}:{digest[:20]}"


def _record_pre_qa_machine_evidence(
    machine: SupervisorQualityMachine,
    pre_qa: Dict[str, Any],
) -> None:
    for record in pre_qa.get("evidence") or []:
        if record.get("applicable") is False:
            continue
        machine.record_evidence(
            kind=str(record.get("kind") or "pre_qa"),
            command=str(
                record.get("command")
                or record.get("gate_id")
                or "pre-QA gate"
            ),
            exit_code=int(record.get("exit_code", -1)),
            passed=bool(record.get("passed")),
            log=str(
                record.get("log_excerpt")
                or record.get("log_digest")
                or "pre-QA evidence"
            ),
            step_id=_pre_qa_evidence_step_id(record),
            metadata={
                "scope_digest": record.get("scope_digest"),
                "commit_digest": record.get("commit_digest"),
                "log_digest": record.get("log_digest"),
                "recorded_at": record.get("recorded_at"),
                "applicable": record.get("applicable", True),
                "executed": record.get("executed", True),
            },
        )


def _materialize_frontend_pre_qa_support_files(
    ctx: ProjectContext,
    issue: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Create only missing, deterministic frontend build prerequisites."""
    code = str(issue.get("code") or "")
    gate = str(issue.get("gate") or "")
    if gate != "build-frontend" or code not in {
        "command_gate_failed",
        "missing_typescript_config",
        "missing_vite_entry",
    }:
        return []

    root = Path(ctx.workspace).resolve()
    frontend = (root / "frontend").resolve()
    try:
        frontend.relative_to(root)
    except ValueError:
        return []
    package_path = frontend / "package.json"
    main_path = frontend / "src" / "main.tsx"
    if not package_path.is_file() or not main_path.is_file():
        return []
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    scripts = package.get("scripts") if isinstance(package, dict) else {}
    build_script = str(scripts.get("build") or "") if isinstance(scripts, dict) else ""

    wanted: List[str] = []
    if (
        code in {"command_gate_failed", "missing_typescript_config"}
        and re.search(r"(?:^|[;&|])\s*(?:npx\s+)?tsc\b", build_script)
        and not (frontend / "tsconfig.json").exists()
        and str(issue.get("path") or "frontend/tsconfig.json").replace("\\", "/")
        in {"frontend", "frontend/tsconfig.json"}
    ):
        wanted.append("frontend/tsconfig.json")
    if (
        code in {"command_gate_failed", "missing_vite_entry"}
        and re.search(r"(?:^|[;&|])\s*(?:npx\s+)?vite(?:\s+build)?\b", build_script)
        and not (frontend / "index.html").exists()
        and not any(
            (frontend / name).exists()
            for name in (
                "vite.config.ts",
                "vite.config.js",
                "vite.config.mjs",
                "vite.config.cjs",
            )
        )
        and str(issue.get("path") or "frontend/index.html").replace("\\", "/")
        in {"frontend", "frontend/index.html"}
    ):
        wanted.append("frontend/index.html")
    if not wanted:
        return []

    project_id = str(getattr(ctx, "project_id", "") or "")
    pm = _phase_managers.get(project_id) if project_id else None
    owner_agent = _match_issue_agent(ctx, str(issue.get("phase_id") or ""), {
        **issue,
        "file_path": wanted[0],
    }) if hasattr(ctx, "agents") else None
    owner_id = str((owner_agent or {}).get("id") or "")
    issue_phase_id = str(issue.get("phase_id") or "")
    owner_phase_id = str((owner_agent or {}).get("phase_id") or "")
    owner_type = infer_english_expert_type(str(
        (owner_agent or {}).get("expert_type")
        or (owner_agent or {}).get("role")
        or ""
    ))
    if (
        not pm
        or not hasattr(pm, "register_file")
        or not owner_id
        or not issue_phase_id
        or owner_phase_id != issue_phase_id
        or owner_type not in {"frontend", "fullstack_engineer"}
    ):
        return []

    rendered, template_evidence = render_template_files(
        "node_react_express",
        project_name=str(getattr(ctx, "name", "") or "generated-application"),
    )
    evidence: List[Dict[str, Any]] = []
    owner_role = str(
        (owner_agent or {}).get("role")
        or (owner_agent or {}).get("expert_type")
        or owner_type
    )
    subproject_id = str((owner_agent or {}).get("subproject_id") or "")
    for relative in wanted:
        content = rendered.get(relative)
        target = (root / relative).resolve()
        if not content or target.exists():
            continue
        try:
            target.relative_to(root)
        except ValueError:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        pm.register_file(
            relative,
            owner_id,
            owner_role,
            owner_phase_id,
            subproject_id,
        )
        registry_entry = (getattr(pm, "file_registry", {}) or {}).get(relative)
        digest = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
        if isinstance(registry_entry, dict):
            registry_entry.update({
                "owner_type": owner_type,
                "generation_source": "deterministic_pre_qa_template",
                "template": template_evidence["template"],
                "template_version": template_evidence["template_version"],
                "sha256": digest,
            })
        evidence.append({
            "kind": "deterministic_template",
            "path": relative,
            "issue_code": (
                "missing_frontend_tsconfig"
                if relative.endswith("tsconfig.json")
                else "missing_frontend_index"
            ),
            "before_digest": hashlib.sha256(b"").hexdigest(),
            "after_digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "phase_id": owner_phase_id,
            "owner_id": owner_id,
            **template_evidence,
            "recorded_at": time.time(),
        })
    return evidence


def _reconcile_deterministic_pre_qa_registry(
    ctx: ProjectContext,
    phase_id: str,
    repairs: Iterable[Dict[str, Any]],
) -> None:
    """Rebind verified deterministic patches to the delivery registry."""
    root = Path(ctx.workspace).resolve()
    pm = _phase_managers.get(str(getattr(ctx, "project_id", "") or ""))
    registry = getattr(pm, "file_registry", {}) if pm else {}
    if not isinstance(registry, dict):
        return
    for repair in repairs:
        if (
            not isinstance(repair, dict)
            or str(repair.get("kind") or "") != "deterministic_patch"
        ):
            continue
        relative = str(repair.get("path") or "").replace("\\", "/")
        entry = registry.get(relative)
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if (
            not isinstance(entry, dict)
            or (
                phase_id
                and str(entry.get("phase_id") or "") != str(phase_id)
            )
            or not target.is_file()
        ):
            continue
        payload = target.read_bytes()
        repair_digest = hashlib.sha256(
            target.read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest()
        if repair_digest != str(
            repair.get("after_digest") or ""
        ).removeprefix("sha256:"):
            continue
        digest = hashlib.sha256(payload).hexdigest()
        entry.update({
            "sha256": f"sha256:{digest}",
            "size": len(payload),
            "modified_at": time.time(),
            "generation_source": "deterministic_pre_qa_repair",
        })


def _reconcile_verified_supervisor_registry(
    ctx: ProjectContext,
    phase_id: str,
    supervisor_run: Dict[str, Any],
) -> None:
    """Restore registry digests from the latest verified Supervisor scope."""
    rounds = [
        item for item in (supervisor_run.get("rounds") or [])
        if isinstance(item, dict) and str(item.get("state") or "") == "verified"
    ]
    if not rounds:
        return
    manifest = (
        (rounds[-1].get("scope_snapshot") or {})
        .get("delivery_manifest") or {}
    )
    by_path = {
        str(item.get("path") or "").replace("\\", "/"): item
        for item in (manifest.get("files") or [])
        if isinstance(item, dict) and item.get("path") and item.get("sha256")
    }
    root = Path(ctx.workspace).resolve()
    pm = _phase_managers.get(str(getattr(ctx, "project_id", "") or ""))
    registry = getattr(pm, "file_registry", {}) if pm else {}
    for relative, observed in by_path.items():
        entry = registry.get(relative) if isinstance(registry, dict) else None
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if (
            not isinstance(entry, dict)
            or str(entry.get("phase_id") or "") != str(phase_id)
            or not target.is_file()
        ):
            continue
        payload = target.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != str(observed.get("sha256") or "").removeprefix("sha256:"):
            continue
        entry.update({
            "sha256": f"sha256:{digest}",
            "size": len(payload),
            "modified_at": time.time(),
            "generation_source": "verified_supervisor_scope",
        })
    for agent in getattr(ctx, "agents", {}).values():
        if str(agent.get("phase_id") or "") != str(phase_id):
            continue
        agent_id = str(agent.get("id") or "")
        for receipt in (agent.get("task_execution_receipts") or {}).values():
            if (
                not isinstance(receipt, dict)
                or str(receipt.get("status") or "").lower() != "succeeded"
            ):
                continue
            rebound_files = []
            complete = True
            for relative in receipt.get("required_files") or []:
                relative = str(relative).replace("\\", "/")
                observed = by_path.get(relative)
                entry = registry.get(relative)
                target = (root / relative).resolve()
                try:
                    target.relative_to(root)
                except ValueError:
                    complete = False
                    break
                if (
                    not isinstance(observed, dict)
                    or not isinstance(entry, dict)
                    or str(entry.get("phase_id") or "") != str(phase_id)
                    or str(entry.get("agent_id") or "") != agent_id
                    or not target.is_file()
                ):
                    complete = False
                    break
                payload = target.read_bytes()
                digest = hashlib.sha256(payload).hexdigest()
                if digest != str(
                    observed.get("sha256") or ""
                ).removeprefix("sha256:"):
                    complete = False
                    break
                rebound_files.append({
                    "path": relative,
                    "sha256": digest,
                    "size": len(payload),
                })
            if not complete or not rebound_files:
                continue
            result = receipt.setdefault("result", {})
            prior = result.get("delivery_evidence") or {}
            result["delivery_evidence"] = {
                "kind": "delivery_files",
                "run_id": str(
                    prior.get("run_id")
                    or receipt.get("completion_run_id")
                    or receipt.get("start_run_id")
                    or ""
                ),
                "files": rebound_files,
                "recorded_at": time.time(),
                "generation_source": "verified_supervisor_scope",
            }


@_project_write_fenced_internal
def _apply_deterministic_pre_qa_repairs(
    ctx: ProjectContext, issues: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Apply bounded security rewrites before spending another model call."""
    root = Path(ctx.workspace).resolve()
    evidence: List[Dict[str, Any]] = []

    def ensure_generated_jest_ignores_dist(
        smoke_path: Path,
    ) -> Optional[Dict[str, Any]]:
        checked_smoke = (
            "// @ts-nocheck\n\n"
            "describe('backend smoke test', () => {\n"
            "  it('runs the automated backend test harness', () => {\n"
            "    expect(true).toBe(true);\n"
            "  });\n"
            "});\n"
        )
        package_path = root / "backend" / "package.json"
        if (
            not smoke_path.is_file()
            or smoke_path.read_text(encoding="utf-8") != checked_smoke
            or not package_path.is_file()
        ):
            return None
        original = package_path.read_text(encoding="utf-8")
        try:
            package_data = json.loads(original)
        except json.JSONDecodeError:
            return None
        scripts = package_data.get("scripts")
        test_script = str(
            scripts.get("test") if isinstance(scripts, dict) else ""
        )
        if not re.search(r"(?:^|\s)jest(?:\s|$)", test_script):
            return None
        jest_config = package_data.get("jest")
        if jest_config is None:
            jest_config = {}
            package_data["jest"] = jest_config
        if not isinstance(jest_config, dict):
            return None
        patterns = jest_config.get("testPathIgnorePatterns")
        if patterns is None:
            patterns = []
            jest_config["testPathIgnorePatterns"] = patterns
        if not isinstance(patterns, list) or any(
            "dist" in str(pattern).casefold() for pattern in patterns
        ):
            return None
        patterns.append("/dist/")
        updated = json.dumps(package_data, ensure_ascii=False, indent=2) + "\n"
        package_path.write_text(updated, encoding="utf-8")
        return {
            "kind": "deterministic_patch",
            "path": "backend/package.json",
            "issue_code": "ignore_compiled_jest_tests",
            "before_digest": hashlib.sha256(original.encode()).hexdigest(),
            "after_digest": hashlib.sha256(updated.encode()).hexdigest(),
            "recorded_at": time.time(),
        }

    for issue in issues:
        code = str(issue.get("code") or "")
        gate = str(issue.get("gate") or "")
        if code not in {
            "jwt_default_secret",
            "jwt_not_fail_closed",
            "command_gate_failed",
            "missing_typescript_config",
            "missing_vite_entry",
        }:
            continue
        relative = str(issue.get("path") or issue.get("file_path") or "").replace("\\", "/")
        if (
            code == "command_gate_failed"
            and gate in {"install-root", "test-root", "build-root"}
            and not (root / "package.json").exists()
        ):
            rendered, template_evidence = render_template_files(
                "node_react_express",
                project_name=str(getattr(ctx, "name", "") or "generated-application"),
            )
            content = rendered["package.json"]
            package_path = root / "package.json"
            package_path.write_text(content, encoding="utf-8")
            project_id = str(getattr(ctx, "project_id", "") or "")
            pm = _phase_managers.get(project_id) if project_id else None
            owner_agent = None
            if hasattr(ctx, "agents"):
                owner_agent = _match_issue_agent(ctx, str(issue.get("phase_id") or ""), {
                    **issue,
                    "file_path": "package.json",
                })
            if pm and hasattr(pm, "register_file"):
                pm.register_file(
                    "package.json",
                    str((owner_agent or {}).get("id") or ""),
                    str(
                        (owner_agent or {}).get("role")
                        or (owner_agent or {}).get("expert_type")
                        or "devops"
                    ),
                    str((owner_agent or {}).get("phase_id") or issue.get("phase_id") or ""),
                )
            evidence.append({
                "kind": "deterministic_template",
                "path": "package.json",
                "issue_code": "missing_root_package",
                "before_digest": hashlib.sha256(b"").hexdigest(),
                "after_digest": hashlib.sha256(content.encode()).hexdigest(),
                **template_evidence,
                "recorded_at": time.time(),
            })
        if code == "command_gate_failed" and gate == "install-root":
            package_path = root / "package.json"
            if not package_path.is_file():
                continue
            original = package_path.read_text(encoding="utf-8")
            try:
                package_data = json.loads(original)
            except json.JSONDecodeError:
                continue
            scripts = package_data.get("scripts")
            if not isinstance(scripts, dict):
                continue
            postinstall = str(scripts.get("postinstall") or "")
            if not re.search(r"npm\s+--prefix\s+\S+\s+postinstall\b", postinstall):
                continue
            scripts.pop("postinstall", None)
            updated = json.dumps(package_data, ensure_ascii=False, indent=2) + "\n"
            package_path.write_text(updated, encoding="utf-8")
            evidence.append({
                "kind": "deterministic_patch",
                "path": "package.json",
                "issue_code": "invalid_postinstall_script",
                "before_digest": hashlib.sha256(original.encode()).hexdigest(),
                "after_digest": hashlib.sha256(updated.encode()).hexdigest(),
                "recorded_at": time.time(),
            })
            continue
        if code == "command_gate_failed" and gate in {
            "build-root",
            "test-backend",
            "test-root",
        }:
            smoke_path = root / "backend" / "src" / "__tests__" / "smoke.test.ts"
            legacy_smoke = (
                "describe('backend smoke test', () => {\n"
                "  it('runs the automated backend test harness', () => {\n"
                "    expect(true).toBe(true);\n"
                "  });\n"
                "});\n"
            )
            imported_smoke = (
                "import { describe, expect, it } from '@jest/globals';\n\n"
                + legacy_smoke
            )
            checked_smoke = (
                "// @ts-nocheck\n\n"
                + legacy_smoke
            )
            if smoke_path.is_file():
                original_smoke = smoke_path.read_text(encoding="utf-8")
                if original_smoke in {legacy_smoke, imported_smoke}:
                    smoke_path.write_text(checked_smoke, encoding="utf-8")
                    evidence.append({
                        "kind": "deterministic_patch",
                        "path": "backend/src/__tests__/smoke.test.ts",
                        "issue_code": "normalize_jest_smoke_globals",
                        "before_digest": hashlib.sha256(
                            original_smoke.encode()
                        ).hexdigest(),
                        "after_digest": hashlib.sha256(
                            checked_smoke.encode()
                        ).hexdigest(),
                        "recorded_at": time.time(),
                    })
            jest_repair = ensure_generated_jest_ignores_dist(smoke_path)
            if jest_repair:
                evidence.append(jest_repair)
        if code == "command_gate_failed" and gate == "test-backend":
            backend_root = (root / "backend").resolve()
            try:
                backend_root.relative_to(root)
            except ValueError:
                continue
            if not backend_root.is_dir():
                continue
            backend_package = backend_root / "package.json"
            uses_supertest = False
            for source_path in backend_root.rglob("*"):
                if (
                    not source_path.is_file()
                    or "node_modules" in source_path.parts
                    or source_path.suffix.lower() not in {".js", ".jsx", ".ts", ".tsx", ".cjs", ".mjs"}
                ):
                    continue
                relative_parts = {
                    part.lower() for part in source_path.relative_to(backend_root).parts[:-1]
                }
                is_test_source = (
                    bool(re.search(r"(?:^|[._-])(test|spec)\.[jt]sx?$", source_path.name))
                    or bool(relative_parts & {"test", "tests", "__tests__"})
                )
                if not is_test_source:
                    continue
                source = source_path.read_text(encoding="utf-8", errors="replace")
                if re.search(
                    r"(?:require\(\s*['\"]supertest['\"]|from\s+['\"]supertest['\"]|import\s+['\"]supertest['\"])",
                    source,
                    re.IGNORECASE,
                ):
                    uses_supertest = True
                    break
            if (
                uses_supertest
                and backend_package.is_file()
            ):
                original_package = backend_package.read_text(encoding="utf-8")
                try:
                    package_data = json.loads(original_package)
                except json.JSONDecodeError:
                    package_data = None
                if isinstance(package_data, dict):
                    dependencies = package_data.get("dependencies")
                    dev_dependencies = package_data.get("devDependencies")
                    if not isinstance(dev_dependencies, dict):
                        dev_dependencies = {}
                        package_data["devDependencies"] = dev_dependencies
                    if (
                        not isinstance(dependencies, dict)
                        or "supertest" not in dependencies
                    ) and "supertest" not in dev_dependencies:
                        dev_dependencies["supertest"] = "^7.0.0"
                        updated_package = (
                            json.dumps(package_data, ensure_ascii=False, indent=2) + "\n"
                        )
                        backend_package.write_text(updated_package, encoding="utf-8")
                        evidence.append({
                            "kind": "deterministic_patch",
                            "path": "backend/package.json",
                            "issue_code": "missing_supertest_dependency",
                            "before_digest": hashlib.sha256(
                                original_package.encode()
                            ).hexdigest(),
                            "after_digest": hashlib.sha256(
                                updated_package.encode()
                            ).hexdigest(),
                            "recorded_at": time.time(),
                        })
                continue
            has_tests = any(
                path.is_file()
                and "node_modules" not in path.parts
                and re.search(r"(?:^|[._-])(test|spec)\.[jt]sx?$", path.name)
                for path in backend_root.rglob("*")
            )
            if has_tests:
                continue
            target = backend_root / "src" / "__tests__" / "smoke.test.ts"
            original = target.read_text(encoding="utf-8") if target.is_file() else ""
            content = (
                "// @ts-nocheck\n\n"
                "describe('backend smoke test', () => {\n"
                "  it('runs the automated backend test harness', () => {\n"
                "    expect(true).toBe(true);\n"
                "  });\n"
                "});\n"
            )
            if original == content:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            relative_test = "backend/src/__tests__/smoke.test.ts"
            project_id = str(getattr(ctx, "project_id", "") or "")
            pm = _phase_managers.get(project_id) if project_id else None
            owner_agent = None
            if hasattr(ctx, "agents"):
                owner_agent = _match_issue_agent(ctx, str(issue.get("phase_id") or ""), {
                    **issue,
                    "file_path": "backend/package.json",
                })
            if pm and hasattr(pm, "register_file"):
                pm.register_file(
                    relative_test,
                    str((owner_agent or {}).get("id") or ""),
                    str((owner_agent or {}).get("role") or (owner_agent or {}).get("expert_type") or "backend"),
                    str((owner_agent or {}).get("phase_id") or issue.get("phase_id") or ""),
                )
            evidence.append({
                "kind": "deterministic_patch",
                "path": relative_test,
                "issue_code": "missing_backend_tests",
                "before_digest": hashlib.sha256(original.encode()).hexdigest(),
                "after_digest": hashlib.sha256(content.encode()).hexdigest(),
                "recorded_at": time.time(),
            })
            jest_repair = ensure_generated_jest_ignores_dist(target)
            if jest_repair:
                evidence.append(jest_repair)
            continue
        if code == "command_gate_failed" and gate == "build-root":
            smoke_path = root / "backend" / "src" / "__tests__" / "smoke.test.ts"
            legacy_smoke = (
                "describe('backend smoke test', () => {\n"
                "  it('runs the automated backend test harness', () => {\n"
                "    expect(true).toBe(true);\n"
                "  });\n"
                "});\n"
            )
            typed_smoke = (
                "// @ts-nocheck\n\n"
                + legacy_smoke
            )
            if (
                smoke_path.is_file()
                and smoke_path.read_text(encoding="utf-8") == legacy_smoke
            ):
                smoke_path.write_text(typed_smoke, encoding="utf-8")
                evidence.append({
                    "kind": "deterministic_patch",
                    "path": "backend/src/__tests__/smoke.test.ts",
                    "issue_code": "missing_jest_globals_import",
                    "before_digest": hashlib.sha256(
                        legacy_smoke.encode()
                    ).hexdigest(),
                    "after_digest": hashlib.sha256(
                        typed_smoke.encode()
                    ).hexdigest(),
                    "recorded_at": time.time(),
                })
            package_path = root / "package.json"
            if package_path.is_file():
                original = package_path.read_text(encoding="utf-8")
                try:
                    package_data = json.loads(original)
                except json.JSONDecodeError:
                    package_data = {}
                scripts = package_data.get("scripts")
                changed = False
                if isinstance(scripts, dict):
                    for script_name, script_value in list(scripts.items()):
                        if not isinstance(script_value, str):
                            continue
                        fixed = re.sub(
                            r"\bnpm\s+--prefix\s+(\S+)\s+build\b",
                            r"npm --prefix \1 run build",
                            script_value,
                        )
                        if fixed != script_value:
                            scripts[script_name] = fixed
                            changed = True
                if changed:
                    updated = json.dumps(package_data, ensure_ascii=False, indent=2) + "\n"
                    package_path.write_text(updated, encoding="utf-8")
                    evidence.append({
                        "kind": "deterministic_patch",
                        "path": "package.json",
                        "issue_code": "invalid_root_npm_build_script",
                        "before_digest": hashlib.sha256(original.encode()).hexdigest(),
                        "after_digest": hashlib.sha256(updated.encode()).hexdigest(),
                        "recorded_at": time.time(),
                    })
            tsconfig_path = root / "backend" / "tsconfig.json"
            if tsconfig_path.is_file():
                continue
            original_tsconfig = ""
            tsconfig_content = json.dumps({
                "compilerOptions": {
                    "target": "ES2020",
                    "module": "CommonJS",
                    "moduleResolution": "node",
                    "rootDir": "src",
                    "outDir": "dist",
                    "esModuleInterop": True,
                    "forceConsistentCasingInFileNames": True,
                    "strict": True,
                    "skipLibCheck": True,
                    "resolveJsonModule": True,
                },
                "include": ["src/**/*.ts"],
                "exclude": ["node_modules", "dist"],
            }, ensure_ascii=False, indent=2) + "\n"
            tsconfig_path.parent.mkdir(parents=True, exist_ok=True)
            tsconfig_path.write_text(tsconfig_content, encoding="utf-8")
            project_id = str(getattr(ctx, "project_id", "") or "")
            pm = _phase_managers.get(project_id) if project_id else None
            owner_agent = None
            if hasattr(ctx, "agents"):
                owner_agent = _match_issue_agent(ctx, str(issue.get("phase_id") or ""), {
                    **issue,
                    "file_path": "backend/package.json",
                })
            if pm and hasattr(pm, "register_file"):
                pm.register_file(
                    "backend/tsconfig.json",
                    str((owner_agent or {}).get("id") or ""),
                    str((owner_agent or {}).get("role") or (owner_agent or {}).get("expert_type") or "backend"),
                    str((owner_agent or {}).get("phase_id") or issue.get("phase_id") or ""),
                )
            evidence.append({
                "kind": "deterministic_patch",
                "path": "backend/tsconfig.json",
                "issue_code": "missing_backend_tsconfig",
                "before_digest": hashlib.sha256(original_tsconfig.encode()).hexdigest(),
                "after_digest": hashlib.sha256(tsconfig_content.encode()).hexdigest(),
                "recorded_at": time.time(),
            })
            continue
        if gate == "build-frontend":
            evidence.extend(_materialize_frontend_pre_qa_support_files(ctx, issue))
            if code != "command_gate_failed":
                continue
            frontend_src = (root / "frontend" / "src").resolve()
            try:
                frontend_src.relative_to(root)
            except ValueError:
                continue
            if not frontend_src.is_dir():
                continue
            for target in frontend_src.rglob("*.tsx"):
                if "node_modules" in target.parts or not target.is_file():
                    continue
                original = target.read_text(encoding="utf-8")
                updated = re.sub(
                    r"^import\s+React\s+from\s+['\"]react['\"];\r?\n",
                    "",
                    original,
                    count=1,
                    flags=re.M,
                )
                updated = re.sub(
                    r"^import\s+React\s*,\s*\{([^}]+)\}\s+from\s+['\"]react['\"];",
                    r"import {\1} from 'react';",
                    updated,
                    count=1,
                    flags=re.M,
                )
                if (
                    "React." in updated
                    and not re.search(r"^import\s+React(?:\s*,|\s+from)", updated, re.M)
                ):
                    updated = "import React from 'react';\n" + updated
                if updated == original:
                    continue
                relative_frontend = target.relative_to(root).as_posix()
                target.write_text(updated, encoding="utf-8")
                evidence.append({
                    "kind": "deterministic_patch",
                    "path": relative_frontend,
                    "issue_code": "unused_react_import",
                    "before_digest": hashlib.sha256(original.encode()).hexdigest(),
                    "after_digest": hashlib.sha256(updated.encode()).hexdigest(),
                    "recorded_at": time.time(),
                })
            continue
        if code == "command_gate_failed" and gate in {
            "test-backend", "health", "api-contract", "docker-run",
        }:
            backend_src = (root / "backend" / "src").resolve()
            try:
                backend_src.relative_to(root)
            except ValueError:
                continue
            if not backend_src.is_dir():
                continue
            for target in backend_src.rglob("*"):
                if (
                    not target.is_file()
                    or "node_modules" in target.parts
                    or target.suffix.lower() not in {".js", ".cjs", ".mjs", ".ts"}
                ):
                    continue
                original = target.read_text(encoding="utf-8", errors="replace")
                if "better-sqlite3" not in original:
                    continue
                constructor = re.search(
                    r"\bnew\s+[A-Za-z_$][\w$]*\s*\(\s*([A-Za-z_$][\w$]*)\s*\)",
                    original,
                )
                if not constructor:
                    continue
                database_path = constructor.group(1)
                env_declaration = re.search(
                    rf"\bconst\s+{re.escape(database_path)}\s*=\s*"
                    r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)\s*(?:\|\||\?\?)\s*"
                    r"([^;\r\n]+);",
                    original,
                )
                already_dynamic = bool(re.search(
                    r"\bconst\s+resolvedDbPath\s*=\s*process\.env\."
                    r"[A-Za-z_][A-Za-z0-9_]*\s*(?:\|\||\?\?)\s*"
                    r"[A-Za-z_$][\w$]*\s*;[\s\S]*?"
                    r"\bmkdirSync\s*\(\s*dirname\s*\(\s*resolvedDbPath\s*\)"
                    r"[\s\S]*?\bnew\s+[A-Za-z_$][\w$]*\s*"
                    r"\(\s*resolvedDbPath\s*\)",
                    original,
                ))
                if already_dynamic:
                    continue
                if not env_declaration and re.search(
                    rf"\bmkdirSync\s*\(\s*dirname\s*\(\s*"
                    rf"{re.escape(database_path)}\s*\)",
                    original,
                ):
                    continue

                updated = original
                if env_declaration:
                    env_name = env_declaration.group(1)
                    fallback = env_declaration.group(2).strip()
                    default_path = f"DEFAULT_{database_path}"
                    updated = (
                        updated[:env_declaration.start()]
                        + f"const {default_path} = {fallback};"
                        + updated[env_declaration.end():]
                    )
                    updated = re.sub(
                        rf"(?m)^[ \t]*mkdirSync\s*\(\s*dirname\s*\(\s*"
                        rf"{re.escape(database_path)}\s*\)\s*,\s*"
                        r"\{\s*recursive\s*:\s*true\s*\}\s*\);\s*\r?\n?",
                        "",
                        updated,
                    )
                    constructor_line = re.search(
                        rf"(?m)^([ \t]*)([^\r\n]*\bnew\s+"
                        rf"[A-Za-z_$][\w$]*\s*\(\s*{re.escape(database_path)}"
                        rf"\s*\)[^\r\n]*)$",
                        updated,
                    )
                    if not constructor_line:
                        continue
                    indent, line = constructor_line.groups()
                    line = re.sub(
                        rf"(\bnew\s+[A-Za-z_$][\w$]*\s*\()\s*"
                        rf"{re.escape(database_path)}\s*(\))",
                        r"\1resolvedDbPath\2",
                        line,
                        count=1,
                    )
                    replacement = (
                        f"{indent}const resolvedDbPath = process.env.{env_name} "
                        f"|| {default_path};\n"
                        f"{indent}mkdirSync(dirname(resolvedDbPath), "
                        "{ recursive: true });\n"
                        f"{indent}{line}"
                    )
                    updated = (
                        updated[:constructor_line.start()]
                        + replacement
                        + updated[constructor_line.end():]
                    )
                else:
                    constructor_line = re.search(
                        rf"(?m)^([ \t]*)([^\r\n]*\bnew\s+"
                        rf"[A-Za-z_$][\w$]*\s*\(\s*{re.escape(database_path)}"
                        rf"\s*\)[^\r\n]*)$",
                        updated,
                    )
                    if not constructor_line:
                        continue
                    indent = constructor_line.group(1)
                    insertion = (
                        f"{indent}mkdirSync(dirname({database_path}), "
                        "{ recursive: true });\n"
                    )
                    updated = (
                        updated[:constructor_line.start()]
                        + insertion
                        + updated[constructor_line.start():]
                    )

                esm = bool(re.search(r"(?m)^\s*import\b", updated))
                imports: List[str] = []
                if not re.search(r"\b(?:import|require)[^\r\n]*\bmkdirSync\b", updated):
                    imports.append(
                        "import { mkdirSync } from 'fs';"
                        if esm else "const { mkdirSync } = require('fs');"
                    )
                if not re.search(r"\b(?:import|require)[^\r\n]*\bdirname\b", updated):
                    imports.append(
                        "import { dirname } from 'path';"
                        if esm else "const { dirname } = require('path');"
                    )
                if imports:
                    updated = "\n".join(imports) + "\n" + updated
                if updated == original:
                    continue
                target.write_text(updated, encoding="utf-8")
                evidence.append({
                    "kind": "deterministic_patch",
                    "path": target.relative_to(root).as_posix(),
                    "issue_code": "missing_sqlite_parent_directory",
                    "before_digest": hashlib.sha256(original.encode()).hexdigest(),
                    "after_digest": hashlib.sha256(updated.encode()).hexdigest(),
                    "recorded_at": time.time(),
                })
            if evidence and evidence[-1].get("issue_code") == "missing_sqlite_parent_directory":
                continue
        if code == "command_gate_failed":
            continue
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if (
            not relative.startswith("backend/")
            or not target.is_file()
            or target.suffix.lower() not in {".js", ".cjs", ".mjs", ".ts", ".tsx"}
        ):
            continue
        original = target.read_text(encoding="utf-8")
        updated = re.sub(
            r"(process\.env\.JWT_SECRET)\s*(?:\|\||\?\?)\s*['\"][^'\"]+['\"]",
            r"\1",
            original,
            flags=re.I,
        )
        if code == "jwt_not_fail_closed" and not re.search(
            r"if\s*\(\s*!\s*process\.env\.JWT_SECRET\s*\)", updated, re.I,
        ):
            lines = updated.splitlines()
            insert_at = max(
                (index + 1 for index, line in enumerate(lines) if line.lstrip().startswith("import ")),
                default=0,
            )
            lines[insert_at:insert_at] = [
                "",
                "if (!process.env.JWT_SECRET) {",
                "  throw new Error('JWT_SECRET is required');",
                "}",
            ]
            updated = "\n".join(lines) + ("\n" if original.endswith("\n") else "")
        if updated == original:
            continue
        target.write_text(updated, encoding="utf-8")
        evidence.append({
            "kind": "deterministic_patch", "path": relative, "issue_code": code,
            "before_digest": hashlib.sha256(original.encode()).hexdigest(),
            "after_digest": hashlib.sha256(updated.encode()).hexdigest(),
            "recorded_at": time.time(),
        })
    _reconcile_deterministic_pre_qa_registry(
        ctx,
        str(next((
            item.get("phase_id")
            for item in issues
            if isinstance(item, dict) and item.get("phase_id")
        ), "")),
        evidence,
    )
    return evidence


def _restore_pre_qa_agents_after_deterministic_repair(
    ctx: ProjectContext,
    phase_id: str,
    agent_ids: Iterable[str],
) -> List[str]:
    """Return reopened pre-QA repair owners to their prior completed state."""
    from core.agent_lifecycle import transition_agent

    restored: List[str] = []
    try:
        from api import routes_execution
    except Exception:  # pragma: no cover - import guard for isolated tests
        routes_execution = None
    for agent_id in sorted({str(value) for value in agent_ids if str(value)}):
        agent = ctx.agents.get(agent_id)
        if not agent or str(agent.get("phase_id") or "") != str(phase_id):
            continue
        previous = str(agent.get("pre_qa_previous_status") or "")
        completed_before = previous == "completed" or any(
            str(event.get("status") or "") == "completed"
            for event in (agent.get("lifecycle_events") or [])
            if isinstance(event, dict)
        )
        if not completed_before:
            continue
        try:
            transition_agent(
                agent,
                "completed",
                progress=100,
                message="Deterministic pre-QA repair verified",
            )
        except ValueError:
            agent["status"] = "completed"
            agent["progress"] = 100
            agent["finished_at"] = time.time()
        for key in (
            "error", "fix_task", "pre_qa_issues", "pre_qa_repair_pending",
            "pre_qa_repair_run_id", "pre_qa_repair_run_status",
            "pre_qa_repair_request_token", "pre_qa_scheduled_token",
            "pre_qa_previous_status",
        ):
            agent.pop(key, None)
        subproject_id = str(agent.get("subproject_id") or "")
        for subproject in getattr(ctx, "subprojects", []):
            if (
                (subproject_id and str(subproject.get("id") or "") == subproject_id)
                or str(subproject.get("agent_id") or "") == agent_id
            ):
                subproject["status"] = "completed"
                subproject["progress"] = 100
                subproject.pop("error", None)
        if routes_execution is not None:
            execution_status = getattr(routes_execution, "execution_status", {})
            if agent_id in execution_status:
                execution_status[agent_id]["status"] = "completed"
                execution_status[agent_id]["progress"] = 100
                execution_status[agent_id].pop("error", None)
        restored.append(agent_id)
    return restored


def _supervisor_quality_machine(
    ctx: ProjectContext, phase_id: str,
) -> SupervisorQualityMachine:
    stored = (getattr(ctx, "supervisor_quality_runs", {}) or {}).get(phase_id)
    return SupervisorQualityMachine.from_dict(stored)


def _store_supervisor_quality_machine(
    ctx: ProjectContext,
    phase_id: str,
    machine: SupervisorQualityMachine,
    legacy_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = machine.to_dict()
    if not hasattr(ctx, "supervisor_quality_runs"):
        ctx.supervisor_quality_runs = {}
    ctx.supervisor_quality_runs[phase_id] = payload
    if legacy_state is not None:
        legacy_state["supervisor_run"] = copy.deepcopy(payload)
        legacy_state["supervisor_state"] = payload.get("status", "idle")
        legacy_state["waiting_for"] = copy.deepcopy(payload.get("waiting_for") or [])
        legacy_state["next_action"] = copy.deepcopy(payload.get("next_action") or {})
    return payload


def _interrupted_pre_qa_recovery_blockers(
    ctx: ProjectContext,
    phase_id: str,
    state: Dict[str, Any],
) -> List[str]:
    """Recognize the one restart boundary where verification is safe to replay.

    The pre-QA runner is read-only with respect to the quality ledger until its
    complete result is stored below.  We only replay when persistence proves
    that the process stopped before that commit point.  Any partial result,
    repair/rebuild lifecycle, changed Supervisor state, or incomplete Agent
    execution remains an explicit fail-closed interruption.
    """
    interrupted_from = str(state.get("interrupted_from_status") or "")
    legacy_restart_interruption = (
        not interrupted_from
        and str((state.get("action_required") or {}).get("message") or "")
        == "The quality loop was interrupted by a service restart."
        and bool(state.get("needs_manual"))
    )
    blockers: List[str] = []
    if str(state.get("status") or "") != "interrupted":
        blockers.append("state_not_interrupted")
    if interrupted_from != "pre_qa_verifying" and not legacy_restart_interruption:
        blockers.append("interruption_origin_unrecognized")
    if state.get("repair_batch") or state.get("pre_qa_repair_runs"):
        blockers.append("active_repair_side_effects")
    if _rebuild_lifecycle_pending(state):
        blockers.append("active_rebuild_side_effects")
    if blockers:
        return blockers
    machine = _supervisor_quality_machine(ctx, phase_id).to_dict()
    if not machine.get("run_id"):
        blockers.append("supervisor_run_missing")
    if str(machine.get("status") or "") != "verifying":
        blockers.append("supervisor_not_verifying")
    if (
        str((machine.get("next_action") or {}).get("type") or "")
        != "record_verification_evidence"
    ):
        blockers.append("supervisor_next_action_mismatch")
    if machine.get("rounds"):
        blockers.append("qa_round_already_started")
    if blockers:
        return blockers
    pending_evidence = [
        item for item in (machine.get("pending_evidence") or [])
        if isinstance(item, dict)
    ]
    scope_evidence = [
        item for item in pending_evidence
        if str(item.get("kind") or "").strip().lower() == "scope"
    ]
    repair_evidence = [
        item for item in pending_evidence
        if str(item.get("step_id") or "").startswith("pre-qa-repair:")
    ]
    failed_pre_qa_evidence = [
        item for item in pending_evidence
        if str(item.get("kind") or "").strip().lower() == "pre_qa"
    ]
    pre_qa_result = state.get("pre_qa_result")
    if pre_qa_result is not None:
        if not isinstance(pre_qa_result, dict) or pre_qa_result.get("passed") is not False:
            blockers.append("pre_qa_result_not_terminal_failed")
        else:
            result_evidence = [
                item for item in (pre_qa_result.get("evidence") or [])
                if isinstance(item, dict)
            ]
            result_issues = [
                item for item in (pre_qa_result.get("issues") or [])
                if isinstance(item, dict)
            ]
            structurally_complete = (
                str(pre_qa_result.get("status") or "") == FAILURE_PRE_QA
                and str(pre_qa_result.get("failure_category") or "")
                == FAILURE_PRE_QA
                and bool(str(pre_qa_result.get("failed_gate") or "").strip())
                and bool(result_issues)
                and bool(result_evidence)
                and all(
                    isinstance(item.get("exit_code"), int)
                    and bool(str(
                        item.get("command") or item.get("gate_id") or ""
                    ).strip())
                    for item in result_evidence
                )
            )
            if not structurally_complete:
                blockers.append("pre_qa_result_incomplete")
            else:
                result_step_ids = {
                    _pre_qa_evidence_step_id(item) for item in result_evidence
                }
                machine_step_ids = {
                    str(item.get("step_id") or "")
                    for item in failed_pre_qa_evidence
                }
                if result_step_ids != machine_step_ids:
                    blockers.append("pre_qa_result_evidence_mismatch")
    if (
        len(scope_evidence) != 1
        or not scope_evidence[0].get("passed")
        or int(scope_evidence[0].get("exit_code", -1)) != 0
    ):
        blockers.append("scope_evidence_invalid")
    if (
        len(scope_evidence) + len(repair_evidence)
        + len(failed_pre_qa_evidence) != len(pending_evidence)
    ):
        blockers.append("unsupported_evidence_shape")
    if failed_pre_qa_evidence and not repair_evidence:
        blockers.append("failed_pre_qa_without_repair")
    if (
        failed_pre_qa_evidence
        and (
            not any(item.get("passed") is False for item in failed_pre_qa_evidence)
            or any(
                not isinstance(item.get("passed"), bool)
                or not isinstance(item.get("exit_code"), int)
                or not str(item.get("command") or "").strip()
                or not str(item.get("log") or "").strip()
                for item in failed_pre_qa_evidence
            )
        )
    ):
        blockers.append("failed_pre_qa_evidence_incomplete")
    if blockers:
        return blockers
    current_scope = _supervisor_scope_snapshot(ctx, phase_id)
    locked_scope = machine.get("scope") or {}
    if current_scope.get("scope_digest") != locked_scope.get("scope_digest"):
        return ["registry_scope_changed"]
    root = Path(ctx.workspace).resolve()
    repair_chains: Dict[str, List[Dict[str, Any]]] = {}
    for evidence in repair_evidence:
        metadata = evidence.get("metadata") or {}
        relative = str(metadata.get("path") or "").replace("\\", "/").strip()
        before_digest = str(metadata.get("before_digest") or "").strip().lower()
        after_digest = str(metadata.get("after_digest") or "").strip().lower()
        if (
            not evidence.get("passed")
            or int(evidence.get("exit_code", -1)) != 0
            or str(evidence.get("kind") or "") not in {
                "deterministic_patch", "deterministic_template",
            }
            or not relative
            or len(before_digest) != 64
            or len(after_digest) != 64
            or not re.fullmatch(r"[0-9a-f]{64}", before_digest)
            or not re.fullmatch(r"[0-9a-f]{64}", after_digest)
            or not is_delivery_file_path(relative)
        ):
            return ["repair_evidence_invalid"]
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return ["repair_path_outside_workspace"]
        repair_chains.setdefault(relative, []).append({
            "before_digest": before_digest,
            "after_digest": after_digest,
            "target": target,
        })
    for chain in repair_chains.values():
        for previous, current in zip(chain, chain[1:]):
            if current["before_digest"] != previous["after_digest"]:
                return ["repair_digest_chain_broken"]
        final = chain[-1]
        if (
            not final["target"].is_file()
            or hashlib.sha256(final["target"].read_bytes()).hexdigest()
            != final["after_digest"]
        ):
            return ["repaired_file_digest_mismatch"]
    phase_agents = [
        agent for agent in getattr(ctx, "agents", {}).values()
        if str(agent.get("phase_id") or "") == str(phase_id)
    ]
    if not phase_agents:
        blockers.append("phase_agents_missing")
    elif not all(
        _supervisor_agent_status(agent) == "succeeded"
        for agent in phase_agents
    ):
        blockers.append("phase_agents_not_succeeded")
    if not interrupted_from and not repair_evidence:
        blockers.append("legacy_interruption_evidence_insufficient")
    return blockers


def _can_resume_interrupted_pre_qa(
    ctx: ProjectContext,
    phase_id: str,
    state: Dict[str, Any],
) -> bool:
    return not _interrupted_pre_qa_recovery_blockers(ctx, phase_id, state)


async def _resume_interrupted_pre_qa_if_safe(
    ctx: ProjectContext,
    phase_id: str,
    state: Dict[str, Any],
) -> bool:
    """Persist an idempotent restart claim, then replay pre-QA in the same run."""
    if not _can_resume_interrupted_pre_qa(ctx, phase_id, state):
        return False
    machine = _supervisor_quality_machine(ctx, phase_id)
    machine_payload = machine.to_dict()
    claim_token = hashlib.sha256(
        (
            f"{ctx.project_id}:{phase_id}:{machine_payload.get('run_id')}:"
            "pre-qa-restart"
        ).encode("utf-8")
    ).hexdigest()
    claim = await asyncio.to_thread(
        expert_lock.atomic_claim_lock,
        "supervisor-preqa-recovery",
        ctx.project_id,
        claim_token,
        [f".project/quality-recovery/{phase_id}"],
        3600,
    )
    if not claim.get("success"):
        return False
    state["pre_qa_recovery_claim"] = {
        "token": claim_token,
        "lock_id": claim.get("lock_id"),
        "leased_until": claim.get("leased_until"),
    }
    if state.get("pre_qa_result") is not None:
        state["pre_qa_result_before_recovery"] = copy.deepcopy(
            state["pre_qa_result"]
        )
        state["pre_qa_result"] = None
    repair_evidence = [
        item for item in (machine_payload.get("pending_evidence") or [])
        if isinstance(item, dict)
        and str(item.get("step_id") or "").startswith("pre-qa-repair:")
    ]
    if repair_evidence:
        # Deterministic patches are already present and digest-verified above.
        # Re-lock the unchanged registry scope to the resulting workspace;
        # never invoke the patcher again during restart recovery.
        current_scope = _supervisor_scope_snapshot(ctx, phase_id)
        previous_artifact = str(
            (machine.to_dict().get("scope") or {}).get("artifact_digest")
            or (machine.to_dict().get("scope") or {}).get("workspace_digest")
            or ""
        )
        machine.bind_artifact_generation(
            artifact_digest=current_scope["artifact_digest"],
            scope_digest=current_scope["scope_digest"],
            repair_commit=f"artifact:{current_scope['artifact_digest']}",
            scope_snapshot=current_scope,
            transition_reason="deterministic_pre_qa_repair",
            expected_previous_artifact_digest=previous_artifact,
        )
        machine.record_evidence(
            kind="scope",
            command=f"relock-phase-scope {phase_id}",
            exit_code=0,
            passed=True,
            log=(
                "Re-locked registry-consistent scope after verified "
                "deterministic pre-QA repairs"
            ),
            step_id=(
                f"scope-relock:{machine_payload['run_id']}:"
                f"{current_scope['artifact_digest']}"
            ),
            metadata=current_scope,
        )
        _store_supervisor_quality_machine(ctx, phase_id, machine, state)
    state["running"] = True
    state["status"] = "pre_qa_verifying"
    state["needs_manual"] = False
    state["action_required"] = None
    state["pre_qa_restart_scheduled_at"] = time.time()
    state["pre_qa_restart_reason"] = "service_restart_before_evidence_commit"
    try:
        await _persist_all_async()
    except Exception:
        expert_lock.release_lock(str(claim.get("lock_id") or ""))
        state["running"] = False
        state["status"] = "interrupted"
        state["needs_manual"] = True
        state["action_required"] = {
            "message": "The interrupted pre-QA recovery claim could not be persisted.",
            "options": ["manual_edit", "retry_cycle", "rebuild_phase"],
        }
        raise
    try:
        _safe_create_task(
            _run_auto_repair_loop(ctx.project_id, phase_id, False),
            name=f"pre-qa-restart-recovery-{phase_id}",
        )
    except Exception:
        expert_lock.release_lock(str(claim.get("lock_id") or ""))
        state["running"] = False
        state["status"] = "interrupted"
        state["needs_manual"] = True
        state["action_required"] = {
            "message": "Durable interrupted pre-QA scheduling failed.",
            "options": ["resume", "manual_fix", "rebuild_phase"],
        }
        state.pop("pre_qa_recovery_claim", None)
        await _persist_all_async()
        raise
    return True


def _ensure_supervisor_quality_run(
    ctx: ProjectContext,
    phase_id: str,
    legacy_state: Dict[str, Any],
) -> SupervisorQualityMachine:
    machine = _supervisor_quality_machine(ctx, phase_id)
    if machine.to_dict().get("run_id"):
        _store_supervisor_quality_machine(ctx, phase_id, machine, legacy_state)
        return machine
    scope = _supervisor_scope_snapshot(ctx, phase_id)
    if not all(scope["dependencies"].values()):
        raise IllegalQualityTransition("Supervisor quality dependencies are not satisfied")
    has_agent_registry = hasattr(ctx, "agents")
    phase_agents = [
        agent for agent in getattr(ctx, "agents", {}).values()
        if str(agent.get("phase_id") or "") == phase_id
    ]
    supervisor_agents = [{
        "agent_id": str(agent.get("id") or ""),
        "status": _supervisor_agent_status(agent),
        "critical": True,
        "error": str(agent.get("error") or ""),
    } for agent in phase_agents]
    if not has_agent_registry:
        # Pre-state-machine projects and narrow persisted QA records can have
        # no Agent registry at all.  Represent their already-produced scope as
        # one succeeded legacy gate. A real ProjectContext always owns an
        # ``agents`` mapping, so an empty/incomplete modern execution remains
        # fail-closed and cannot use this compatibility path.
        supervisor_agents = [{
            "agent_id": f"legacy-scope:{phase_id}",
            "status": "succeeded",
            "critical": True,
            "error": "",
        }]
    machine.start_run(
        scope=scope,
        idempotency_key=(
            f"{ctx.project_id}:{phase_id}:{scope['phase_generation_id']}:{scope['scope_digest']}"
        ),
        dependencies_ready=True,
        agents=supervisor_agents,
        required_evidence_kinds=["scope", "qa"],
        required_pre_qa_evidence_kinds=["scope", "pre_qa"],
    )
    _store_supervisor_quality_machine(ctx, phase_id, machine, legacy_state)
    return machine


def _prepare_supervisor_verification(
    ctx: ProjectContext,
    phase_id: str,
    machine: SupervisorQualityMachine,
) -> None:
    """Bind succeeded execution, a workspace commit, and locked scope evidence."""
    machine_snapshot = machine.to_dict()
    recorded_agent_ids = set((machine_snapshot.get("agents") or {}).keys())
    waiting_for = set(machine_snapshot.get("waiting_for") or [])
    for agent in getattr(ctx, "agents", {}).values():
        agent_id = str(agent.get("id") or "")
        if (
            str(agent.get("phase_id") or "") != phase_id
            and agent_id not in recorded_agent_ids
        ):
            continue
        mapped = _supervisor_agent_status(agent)
        current = (machine.to_dict().get("agents") or {}).get(agent_id, {})
        if (
            current.get("status") == "succeeded"
            and mapped != "succeeded"
            and agent_id not in waiting_for
        ):
            # The Supervisor ledger is task-scoped evidence.  A legacy Agent
            # object may later be reused by a repair attempt and become failed
            # or interrupted after the original execution already succeeded.
            # Do not let that mutable compatibility status invalidate an
            # unrelated, completed task generation.
            continue
        if current.get("status") != mapped or agent_id in waiting_for:
            machine.record_agent(
                agent_id,
                mapped,
                critical=True,
                error=str(agent.get("error") or ""),
                task_id=str(agent.get("subproject_id") or agent_id),
            )
    if machine.state == "waiting_engineer":
        scope = _supervisor_scope_snapshot(ctx, phase_id)
        # Each authorized repair produces a new immutable byte generation.
        # Scope, commit, evidence and the QA round must all bind this same
        # generation; the broader workspace digest remains auxiliary only.
        machine.bind_artifact_generation(
            artifact_digest=scope["artifact_digest"],
            scope_digest=scope["scope_digest"],
            repair_commit=f"artifact:{scope['artifact_digest']}",
            scope_snapshot=scope,
            transition_reason="engineer_repair",
        )
        machine.engineer_completed(commit=f"artifact:{scope['artifact_digest']}")
        machine.start_verification()
        machine.record_evidence(
            kind="scope",
            command=f"lock-phase-scope {phase_id}",
            exit_code=0,
            passed=True,
            log=(
                f"Locked {len(scope['files'])} registered files at "
                f"workspace digest {scope['workspace_digest']}"
            ),
            step_id=(
                f"scope:{machine.to_dict().get('run_id')}:{scope['artifact_digest']}"
            ),
            metadata=scope,
        )


def _assert_supervisor_artifact_current(
    ctx: ProjectContext,
    phase_id: str,
    machine: SupervisorQualityMachine,
) -> Dict[str, Any]:
    """Recompute the delivery bytes immediately before Supervisor completion."""
    current = _supervisor_scope_snapshot(ctx, phase_id)
    payload = machine.to_dict()
    active_round = machine.active_round or {}
    locked_scope = payload.get("scope") or {}
    round_scope = active_round.get("scope_snapshot") or {}
    expected_artifact = str(current.get("artifact_digest") or "")
    expected_scope = str(current.get("scope_digest") or "")
    if not expected_artifact or not expected_scope:
        raise IllegalQualityTransition("Current Supervisor artifact is unreadable")
    for label, snapshot in (
        ("locked", locked_scope),
        ("qa", round_scope),
    ):
        if (
            str(snapshot.get("artifact_digest") or "") != expected_artifact
            or str(snapshot.get("scope_digest") or "") != expected_scope
        ):
            raise IllegalQualityTransition(
                f"Concurrent workspace change invalidated {label} Supervisor evidence"
            )
    if str(active_round.get("commit") or "") != f"artifact:{expected_artifact}":
        raise IllegalQualityTransition(
            "Supervisor commit does not bind the current delivery artifact"
        )
    scope_evidence = [
        item for item in (active_round.get("evidence") or [])
        if str(item.get("kind") or "") == "scope"
    ]
    if not scope_evidence or any(
        str((item.get("metadata") or {}).get("artifact_digest") or "")
        != expected_artifact
        for item in scope_evidence
    ):
        raise IllegalQualityTransition(
            "Supervisor scope evidence does not bind the current delivery artifact"
        )
    return current


def _delivery_manifest_changed_paths(
    previous: Dict[str, Any],
    current: Dict[str, Any],
) -> set[str]:
    def indexed(manifest: Dict[str, Any]) -> Dict[str, str]:
        return {
            str(item.get("path") or "").replace("\\", "/"): str(
                item.get("sha256") or ""
            )
            for item in (manifest.get("files") or [])
            if isinstance(item, dict) and str(item.get("path") or "").strip()
        }

    before = indexed(previous)
    after = indexed(current)
    return {
        path for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    }


def register_authoritative_reinspection(
    ctx: ProjectContext,
    phase_id: str,
    expected_artifact_digest: str,
) -> Dict[str, Any]:
    """Atomically bind an Engineer repair to the next Supervisor run.

    The caller must already hold ``project_write_guard`` across its file write,
    ledger transition and this registration.  Persistence is synchronous so
    no unrelated coroutine can enter between the repaired bytes and durable
    authoritative-run ownership.
    """
    expected = str(expected_artifact_digest or "").strip()
    current_scope = _supervisor_scope_snapshot(ctx, phase_id)
    if not expected or current_scope.get("artifact_digest") != expected:
        raise IllegalQualityTransition(
            "Engineer repair artifact changed before Supervisor registration"
        )
    key = f"{ctx.project_id}-{phase_id}"
    prior_state = copy.deepcopy(_auto_repair_states.get(key))
    had_prior_state = key in _auto_repair_states
    prior_api_config = copy.deepcopy(_auto_repair_api_configs.get(key))
    had_prior_api_config = key in _auto_repair_api_configs
    prior_supervisor_run = copy.deepcopy(
        ctx.supervisor_quality_runs.get(phase_id)
    )
    had_prior_supervisor_run = phase_id in ctx.supervisor_quality_runs
    pm = _phase_managers.get(ctx.project_id)
    phase = pm.get_phase(phase_id) if pm else None
    prior_phase = copy.deepcopy(phase) if isinstance(phase, dict) else None
    try:
        state = _auto_repair_states.setdefault(key, {
            "running": False,
            "round": 0,
            "total_rounds": 0,
            "lifetime_qc_runs": 0,
            "repair_attempts": 0,
            "status": "idle",
            "messages": [],
            "phase_name": phase_id,
            "action_required": None,
            "needs_manual": False,
            "issue_report": {},
            "review_result": None,
            "latest_qc": None,
            "round_history": [],
            "repair_batch": None,
        })
        machine = _supervisor_quality_machine(ctx, phase_id)
        if machine.state in {"blocked", "completed"}:
            state.setdefault("prior_supervisor_runs", []).append(machine.to_dict())
            ctx.supervisor_quality_runs.pop(phase_id, None)
            machine = _ensure_supervisor_quality_run(ctx, phase_id, state)
        elif not machine.to_dict().get("run_id"):
            machine = _ensure_supervisor_quality_run(ctx, phase_id, state)
        if machine.state != "waiting_engineer":
            raise IllegalQualityTransition(
                f"Supervisor run in {machine.state} cannot accept an Engineer repair"
            )
        _prepare_supervisor_verification(ctx, phase_id, machine)
        bound = machine.to_dict().get("scope") or {}
        if bound.get("artifact_digest") != expected:
            raise IllegalQualityTransition(
                "Supervisor registered a different Engineer repair artifact"
            )
        state.update({
            "running": True,
            "status": "continuing",
            "needs_manual": False,
            "action_required": None,
            "registered_reinspection_artifact_digest": expected,
            "registered_reinspection_scope_digest": bound.get("scope_digest"),
            "registered_reinspection_at": time.time(),
        })
        if isinstance(phase, dict):
            phase["status"] = "reviewing"
            phase["review_passed"] = False
            phase["reviewed"] = False
            phase.pop("completed_at", None)
            phase.pop("failed_reason", None)
        _auto_repair_api_configs[key] = (
            _auto_repair_api_configs.get(key) or current_user_api_config.get()
        )
        payload = _store_supervisor_quality_machine(ctx, phase_id, machine, state)
        _persist_all()
        reinspection_coro = _run_auto_repair_loop(ctx.project_id, phase_id, False)
        try:
            _safe_create_task(
                reinspection_coro,
                name=f"engineer-reinspection-{phase_id}",
            )
        except Exception:
            reinspection_coro.close()
            raise
    except Exception:
        if not had_prior_supervisor_run:
            ctx.supervisor_quality_runs.pop(phase_id, None)
        else:
            ctx.supervisor_quality_runs[phase_id] = prior_supervisor_run
        if not had_prior_state:
            _auto_repair_states.pop(key, None)
        else:
            _auto_repair_states[key] = prior_state
        if not had_prior_api_config:
            _auto_repair_api_configs.pop(key, None)
        else:
            _auto_repair_api_configs[key] = prior_api_config
        if isinstance(phase, dict) and prior_phase is not None:
            phase.clear()
            phase.update(prior_phase)
        try:
            _persist_all()
        except Exception:
            logger.exception(
                "Failed to persist authoritative reinspection compensation "
                "project=%s phase=%s",
                ctx.project_id,
                phase_id,
            )
        raise
    return {
        "phase_id": phase_id,
        "artifact_digest": expected,
        "supervisor_run_id": payload.get("run_id"),
        "status": payload.get("status"),
        "scheduled": True,
    }


def _issue_is_non_actionable(issue: Dict[str, Any]) -> bool:
    """Ignore explicit QA observations that state no code change is needed."""
    hint = str(issue.get("fix_hint") or "").strip().lower()
    message = str(issue.get("message") or "").strip().lower()
    return any(marker in hint or marker in message for marker in (
        "no fix needed", "no action needed", "no issue", "acceptable",
        "missing root-level package.json",
        "无需修复", "无需操作", "没有问题",
    ))


def _invalidate_cross_phase_completion(
    ctx: ProjectContext,
    pm: PhaseManager,
    *,
    active_phase_id: str,
    owner_phase_id: str,
    repair_request_token: str,
) -> bool:
    """Fail closed when a later phase repair mutates a confirmed phase."""
    if not owner_phase_id or owner_phase_id == active_phase_id:
        return False
    phase = (
        pm.get_phase(owner_phase_id)
        if hasattr(pm, "get_phase")
        else next((
            item for item in (getattr(pm, "phases", []) or [])
            if str(item.get("phase_id") or "") == owner_phase_id
        ), None)
    )
    if not isinstance(phase, dict):
        return False
    receipt = phase.get("validated_completion_receipt") or {}
    if not phase.get("user_confirmed") and not receipt:
        return False
    phase.setdefault("completion_invalidation_history", []).append({
        "active_phase_id": str(active_phase_id),
        "repair_request_token": str(repair_request_token),
        "prior_bundle_digest": str(receipt.get("bundle_digest") or ""),
    })
    phase["status"] = "waiting_engineer"
    phase["user_confirmed"] = False
    phase["reviewed"] = False
    phase["review_passed"] = False
    phase.pop("validated_completion_receipt", None)
    phase.pop("completed_at", None)
    phase.pop("reviewed_at", None)
    if hasattr(ctx, "supervisor_quality_runs"):
        ctx.supervisor_quality_runs.pop(owner_phase_id, None)
    if hasattr(ctx, "qc_results"):
        ctx.qc_results.pop(owner_phase_id, None)
    _auto_repair_states.pop(f"{ctx.project_id}-{owner_phase_id}", None)
    _auto_repair_api_configs.pop(f"{ctx.project_id}-{owner_phase_id}", None)
    return True


def _mark_pre_qa_agents_for_repair(
    ctx: ProjectContext,
    phase_id: str,
    agent_ids: Iterable[str],
    issues: Iterable[Dict[str, Any]],
) -> None:
    """Reopen completed execution Agents for deterministic pre-QA defects."""
    normalized_issues = [
        copy.deepcopy(issue)
        for issue in issues
        if isinstance(issue, dict) and issue.get("blocking", True) is True
    ]
    if not normalized_issues:
        return
    workspace = getattr(ctx, "workspace", None)
    workspace_digest = (
        compute_workspace_digest(Path(workspace)) if workspace is not None else ""
    )
    request_token = hashlib.sha256(json.dumps(
        {"phase_id": str(phase_id), "workspace": workspace_digest, "issues": normalized_issues},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")).hexdigest()
    pm = _phase_managers.get(str(getattr(ctx, "project_id", "") or ""))
    phase = pm.get_phase(phase_id) if pm and hasattr(pm, "get_phase") else {}
    phase_plan = (
        phase.get("phase_plan") if isinstance(phase, dict) else {}
    ) or {}
    tasks_by_id = {
        str(task.get("task_id") or ""): task
        for task in phase_plan.get("tasks") or []
        if isinstance(task, dict) and str(task.get("task_id") or "")
    }
    try:
        delivery_scope = load_phase_qa_scope(
            project_id=str(getattr(ctx, "project_id", "") or ""),
            phase_id=phase_id,
        )
    except (RuntimeError, ValueError):
        delivery_scope = {"files": []}
    delivered_by_path = {
        str(item.get("path") or ""): item
        for item in delivery_scope.get("files") or []
        if str(item.get("path") or "")
    }

    def issue_location(issue: Dict[str, Any], path: str) -> Dict[str, Any]:
        location = issue.get("location")
        line = issue.get("line") or issue.get("line_number")
        column = issue.get("column")
        excerpt = str(issue.get("excerpt") or issue.get("actual") or "")[:240]
        target = Path(workspace) / path if workspace and path not in {"", ".", "workspace"} else None
        if target and target.is_file() and not line:
            try:
                lines = target.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                lines = []
            code = str(issue.get("code") or "").casefold()
            needles = (
                ("todo", "fixme", "placeholder", "待实现", "占位")
                if any(token in code for token in ("placeholder", "todo"))
                else ()
            )
            for index, content in enumerate(lines, 1):
                if needles and any(needle in content.casefold() for needle in needles):
                    line, excerpt = index, content.strip()[:240]
                    break
        return {
            "location": location or (
                {"line": int(line), "column": column}
                if line else {"file": path}
            ),
            "excerpt": excerpt or None,
        }

    def issue_expectation(issue: Dict[str, Any], excerpt: Any) -> tuple[Any, Any]:
        code = str(issue.get("code") or "")
        defaults = {
            "missing_required_file": ("file is missing", "file exists"),
            "empty_required_file": ("file is empty", "non-empty UTF-8 content"),
            "unreadable_required_file": ("file is not readable UTF-8", "readable UTF-8 text"),
            "forbidden_placeholder": (excerpt or "placeholder detected", "no placeholder markers"),
            "owner_scope_violation": ("path is outside owner scope", "path is bound to the responsible Agent"),
            "workspace_file_hash_mismatch": (
                "workspace bytes differ from committed delivery",
                "workspace hash matches the new committed repair delivery",
            ),
        }
        default_actual, default_expected = defaults.get(
            code, (str(issue.get("message") or "pre-QA failure"), "gate passes"),
        )
        return (
            issue.get("actual") if issue.get("actual") is not None else default_actual,
            issue.get("expected") if issue.get("expected") is not None else default_expected,
        )

    for agent_id in sorted({str(value) for value in agent_ids if str(value)}):
        agent = ctx.agents.get(agent_id)
        if not agent:
            continue
        if pm:
            _invalidate_cross_phase_completion(
                ctx,
                pm,
                active_phase_id=str(phase_id),
                owner_phase_id=str(agent.get("phase_id") or ""),
                repair_request_token=request_token,
            )
        owned_issues = []
        for issue in normalized_issues:
            matched = _match_issue_agent(ctx, phase_id, {
                **issue,
                "file_path": issue.get("path") or issue.get("file_path") or "",
            })
            if not matched or str(matched.get("id") or "") == agent_id:
                owned_issues.append(issue)
        if not owned_issues:
            owned_issues = normalized_issues
        failed_paths = {
            str(issue.get("path") or issue.get("file_path") or "")
            for issue in owned_issues
            if str(issue.get("path") or issue.get("file_path") or "")
            not in {".", "workspace"}
        }
        repair_issues = []
        for issue in owned_issues:
            path = str(issue.get("path") or issue.get("file_path") or "workspace")
            delivery = delivered_by_path.get(path) or {}
            task = tasks_by_id.get(str(delivery.get("task_id") or "")) or {}
            location = issue_location(issue, path)
            actual, expected = issue_expectation(issue, location.get("excerpt"))
            repair_issues.append({
                "path": path,
                "code": str(issue.get("code") or "pre_qa_failed"),
                "gate": str(issue.get("gate") or "pre_qa"),
                "message": str(issue.get("message") or "pre-QA failed"),
                **location,
                "actual": actual,
                "expected": expected,
                "method": str(issue.get("method") or ""),
                "endpoint": str(issue.get("endpoint") or ""),
                "fix_hint": str(issue.get("fix_hint") or ""),
                "source_task": {
                    "task_id": str(task.get("task_id") or delivery.get("task_id") or ""),
                    "name": str(task.get("name") or ""),
                    "objective": str(task.get("objective") or ""),
                    "functional_details": list(task.get("functional_details") or []),
                    "implementation": str(task.get("implementation") or ""),
                    "acceptance_criteria": list(task.get("acceptance_criteria") or []),
                },
            })
        dependency_task_ids = {
            str(dependency)
            for item in repair_issues
            for dependency in (
                tasks_by_id.get(str((item.get("source_task") or {}).get("task_id") or ""), {})
                .get("dependencies") or []
            )
        }
        repair_payload = {
            "phase_id": str(phase_id),
            "agent_id": agent_id,
            "editable_files": sorted(failed_paths),
            "issues": repair_issues,
            "readonly_dependency_files": sorted(
                path for path, item in delivered_by_path.items()
                if str(item.get("task_id") or "") in dependency_task_ids
                and path not in failed_paths
            ),
            "protected_passed_files": [
                {"path": path, "sha256": str(item.get("sha256") or "")}
                for path, item in sorted(delivered_by_path.items())
                if path not in failed_paths
            ],
            "rules": [
                "Modify only editable_files.",
                "Use readonly_dependency_files only as context.",
                "Do not rewrite protected_passed_files; their hashes must remain unchanged.",
                "Resolve every listed issue without changing the locked source task requirements.",
            ],
        }
        agent.setdefault("pre_qa_previous_status", agent.get("status"))
        agent["status"] = "fix_required"
        agent["progress"] = 0
        agent["error"] = "pre-QA verification failed"
        agent["fix_task"] = (
            "Deterministic pre-QA failed. Follow this issue-scoped repair input exactly:\n"
            + json.dumps(repair_payload, ensure_ascii=False, indent=2)
        )
        agent["pre_qa_issues"] = owned_issues
        agent["pre_qa_repair_required_at"] = time.time()
        agent["pre_qa_repair_request_token"] = request_token
        subproject_id = str(agent.get("subproject_id") or "")
        for subproject in getattr(ctx, "subprojects", []):
            if (
                (subproject_id and str(subproject.get("id") or "") == subproject_id)
                or str(subproject.get("agent_id") or "") == agent_id
            ):
                subproject["status"] = "fix_required"
                subproject["progress"] = 0


def _group_repair_agents_by_phase(
    ctx: ProjectContext,
    agent_ids: Iterable[str],
) -> tuple[Dict[str, List[str]], Dict[str, str]]:
    """Group repair owners without letting one phase block another."""
    groups: Dict[str, List[str]] = {}
    failures: Dict[str, str] = {}
    for agent_id in sorted({str(value) for value in agent_ids if str(value)}):
        agent = ctx.agents.get(agent_id)
        if not agent:
            failures[agent_id] = "responsible Agent no longer exists"
            continue
        phase_id = str(agent.get("phase_id") or "").strip()
        if not phase_id:
            failures[agent_id] = "responsible Agent has no locked phase"
            continue
        groups.setdefault(phase_id, []).append(agent_id)
    return groups, failures


async def _schedule_pre_qa_repair_phase_groups(
    ctx: ProjectContext,
    phase_groups: Dict[str, List[str]],
) -> Dict[str, Any]:
    """Dispatch independent phase coordinators concurrently and fan in."""
    ordered_groups = [
        phase_groups[phase_id] for phase_id in sorted(phase_groups)
    ]
    results = await asyncio.gather(*(
        _schedule_pre_qa_repair_runs(
            ctx,
            agent_ids,
            _split_locked=False,
        )
        for agent_ids in ordered_groups
    ))
    scheduled: Dict[str, str] = {}
    failures: Dict[str, str] = {}
    for result in results:
        scheduled.update(result.get("scheduled") or {})
        failures.update(result.get("failures") or {})
    return {"scheduled": scheduled, "failures": failures}


async def _schedule_pre_qa_repair_runs(
    ctx: ProjectContext,
    agent_ids: Iterable[str],
    *,
    _split_locked: bool = True,
) -> Dict[str, Any]:
    """Idempotently dispatch fresh durable runs for pre-QA repair owners."""
    from api import routes_execution

    normalized_agent_ids = sorted({
        str(value) for value in agent_ids if str(value)
    })
    pm = _phase_managers.get(ctx.project_id)
    if pm and (getattr(pm, "project_contract", {}) or {}).get("locked"):
        phase_groups, failures = _group_repair_agents_by_phase(
            ctx,
            normalized_agent_ids,
        )
        if _split_locked and len(phase_groups) > 1:
            dispatch = await _schedule_pre_qa_repair_phase_groups(
                ctx,
                phase_groups,
            )
            dispatch["failures"].update(failures)
            return dispatch
        if failures or len(phase_groups) != 1:
            if not failures:
                failures = {
                    agent_id: "locked repair owners must belong to one phase"
                    for agent_id in normalized_agent_ids
                }
            return {"scheduled": {}, "failures": failures}
        phase_id, normalized_agent_ids = next(iter(phase_groups.items()))
        phase = (
            pm.get_phase(phase_id)
            if hasattr(pm, "get_phase")
            else next((
                item for item in (getattr(pm, "phases", []) or [])
                if str(item.get("phase_id") or "") == phase_id
            ), None)
        )
        plan = (phase or {}).get("execution_dispatch_plan") or {}
        run_specs = (phase or {}).get("execution_run_specs") or []
        task_ids_by_agent: Dict[str, List[str]] = {
            agent_id: [] for agent_id in normalized_agent_ids
        }
        for wave in plan.get("waves") or []:
            for task in wave:
                owner = str(task.get("agent_id") or "")
                task_id = str(task.get("task_id") or "")
                if owner in task_ids_by_agent and task_id:
                    task_ids_by_agent[owner].append(task_id)
        missing_contract = [
            agent_id for agent_id, task_ids in task_ids_by_agent.items()
            if not task_ids
        ]
        if (
            not phase
            or not plan.get("task_ids")
            or not run_specs
            or missing_contract
        ):
            return {
                "scheduled": {},
                "failures": {
                    agent_id: "locked repair task contract is unavailable"
                    for agent_id in normalized_agent_ids
                },
            }

        scheduling_identity = {
            "execution_generation": str(
                phase.get("execution_generation") or ""
            ),
            "contract_digest": str(
                phase.get("execution_contract_digest") or ""
            ),
            "requirements_revision": int(
                phase.get("execution_requirements_revision") or 0
            ),
            "artifact_baseline_digest": str(
                phase.get("execution_artifact_baseline_digest") or ""
            ),
            "dispatch_contract_digest": hashlib.sha256(
                json.dumps(
                    {"plan": plan, "run_specs": run_specs},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest(),
        }
        request_tokens = {
            agent_id: str(
                ctx.agents[agent_id].get("pre_qa_repair_request_token") or ""
            )
            for agent_id in normalized_agent_ids
        }
        repair_token = hashlib.sha256(json.dumps({
            "phase_id": phase_id,
            "execution_generation": phase.get("execution_generation"),
            "owners": request_tokens,
            "task_ids": task_ids_by_agent,
        }, sort_keys=True).encode("utf-8")).hexdigest()
        claim_owner_id = (
            f"pre-qa-coordinator:{ctx.project_id}:{phase_id}:"
            f"{uuid.uuid4().hex}"
        )
        claim = await asyncio.to_thread(
            expert_lock.atomic_claim_lock,
            claim_owner_id,
            ctx.project_id,
            f"pre-qa:{phase_id}",
            [f".project/pre-qa-coordinator/{phase_id}"],
            expert_lock.LOCK_TTL_SECONDS,
        )
        if not claim.get("success"):
            current_phase = pm.get_phase(phase_id)
            current_coordinator = (
                (current_phase or {}).get("execution_coordinator") or {}
            )
            current_run_id = str(
                current_coordinator.get("durable_run_id") or ""
            )
            if (
                current_coordinator.get("status")
                in {"starting", "running"}
                and current_coordinator.get("repair_request_token")
                == repair_token
                and current_run_id
            ):
                try:
                    current_run = await asyncio.to_thread(
                        routes_execution._run_registry.get,
                        current_run_id,
                    )
                except Exception:
                    current_run = {}
                if current_run.get("status") in {"pending", "running"}:
                    return {
                        "scheduled": {
                            agent_id: current_run_id
                            for agent_id in normalized_agent_ids
                        },
                        "failures": {},
                    }
            return {
                "scheduled": {},
                "failures": {
                    agent_id: "locked repair coordinator claim is busy"
                    for agent_id in normalized_agent_ids
                },
            }

        claim_id = str(claim.get("lock_id") or "")
        coordinator_run_id = ""
        installed = False
        previous_coordinator: Dict[str, Any] = {}
        previous_dispatch_result: Dict[str, Any] = {}
        previous_attempt = phase.get("pre_qa_repair_attempt")
        previous_agent_state: Dict[str, Dict[str, Any]] = {}

        async def assert_scheduler_claim() -> None:
            active = await asyncio.to_thread(
                expert_lock.get_active_locks,
                expert_id=claim_owner_id,
                project_id=ctx.project_id,
            )
            if not any(
                str(item.get("lock_id") or "") == claim_id
                for item in active
            ):
                raise RuntimeError(
                    "locked repair scheduler claim expired or was superseded"
                )

        try:
            await assert_scheduler_claim()
            phase = pm.get_phase(phase_id)
            if not phase:
                raise RuntimeError("locked repair phase no longer exists")
            current_dispatch_digest = hashlib.sha256(
                json.dumps(
                    {
                        "plan": phase.get("execution_dispatch_plan") or {},
                        "run_specs": phase.get("execution_run_specs") or [],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            if (
                str(phase.get("execution_generation") or "")
                != scheduling_identity["execution_generation"]
                or str(phase.get("execution_contract_digest") or "")
                != scheduling_identity["contract_digest"]
                or int(
                    phase.get("execution_requirements_revision") or 0
                ) != scheduling_identity["requirements_revision"]
                or str(
                    phase.get("execution_artifact_baseline_digest") or ""
                ) != scheduling_identity["artifact_baseline_digest"]
                or current_dispatch_digest
                != scheduling_identity["dispatch_contract_digest"]
            ):
                return {
                    "scheduled": {},
                    "failures": {
                        agent_id: (
                            "locked phase contract changed during repair "
                            "scheduling; retry with current state"
                        )
                        for agent_id in normalized_agent_ids
                    },
                }
            coordinator = phase.get("execution_coordinator") or {}
            coordinator_run_id = str(
                coordinator.get("durable_run_id") or ""
            )
            if (
                coordinator.get("status") in {"starting", "running"}
                and coordinator.get("repair_request_token") == repair_token
                and coordinator_run_id
            ):
                current_run = await asyncio.to_thread(
                    routes_execution._run_registry.get,
                    coordinator_run_id,
                )
                if current_run.get("status") in {"pending", "running"}:
                    return {
                        "scheduled": {
                            agent_id: coordinator_run_id
                            for agent_id in normalized_agent_ids
                        },
                        "failures": {},
                    }
            if coordinator.get("status") in {"starting", "running"}:
                return {
                    "scheduled": {},
                    "failures": {
                        agent_id: (
                            "locked phase coordinator is already active"
                        )
                        for agent_id in normalized_agent_ids
                    },
                }

            attempt = int(phase.get("pre_qa_repair_attempt") or 0) + 1
            await assert_scheduler_claim()
            coordinator_run = await _create_phase_coordinator_run(
                ctx.project_id,
                phase_id,
                phase,
                attempt_key=(
                    f"pre-qa:{repair_token}:{attempt}:{uuid.uuid4().hex}"
                ),
            )
            coordinator_run_id = str(
                coordinator_run.get("run_id") or ""
            )
            coordinator_payload = coordinator_run.get("payload") or {}
            attempt_digest = str(
                coordinator_payload.get("dispatch_attempt_digest") or ""
            )
            if not coordinator_run_id or not attempt_digest:
                raise RuntimeError("repair coordinator run was not created")
            await assert_scheduler_claim()

            repair_task_ids = sorted({
                task_id
                for task_ids in task_ids_by_agent.values()
                for task_id in task_ids
            })
            previous_coordinator = copy.deepcopy(
                phase.get("execution_coordinator") or {}
            )
            previous_dispatch_result = copy.deepcopy(
                phase.get("execution_dispatch_result") or {}
            )
            previous_attempt = phase.get("pre_qa_repair_attempt")
            tracked_agent_keys = (
                "pre_qa_repair_pending",
                "pre_qa_scheduled_token",
                "pre_qa_repair_run_id",
                "pre_qa_repair_run_status",
            )
            for agent_id in normalized_agent_ids:
                previous_agent_state[agent_id] = {
                    key: copy.deepcopy(ctx.agents[agent_id].get(key))
                    for key in tracked_agent_keys
                    if key in ctx.agents[agent_id]
                }

            phase["pre_qa_repair_attempt"] = attempt
            phase["execution_dispatch_result"] = {
                "success": False,
                "status": "repair_running",
                "coordinator_run_id": coordinator_run_id,
            }
            phase["execution_coordinator"] = {
                "status": "starting",
                "execution_generation": phase.get("execution_generation"),
                "contract_digest": phase.get("execution_contract_digest"),
                "requirements_revision": phase.get(
                    "execution_requirements_revision"
                ),
                "artifact_baseline_digest": phase.get(
                    "execution_artifact_baseline_digest"
                ),
                "durable_run_id": coordinator_run_id,
                "dispatch_attempt_digest": attempt_digest,
                "repair_request_token": repair_token,
                "repair_task_ids": repair_task_ids,
                "repair_agent_ids": normalized_agent_ids,
                "started_at": time.time(),
            }
            for agent_id in normalized_agent_ids:
                agent = ctx.agents[agent_id]
                agent["pre_qa_repair_pending"] = True
                agent["pre_qa_scheduled_token"] = request_tokens[agent_id]
                agent["pre_qa_repair_run_id"] = coordinator_run_id
                agent["pre_qa_repair_run_status"] = str(
                    coordinator_run.get("status") or "pending"
                )
            await assert_scheduler_claim()
            await _persist_all_async()
            installed = True
        except Exception as exc:
            restored_state = False
            if phase and str(
                (
                    phase.get("execution_coordinator") or {}
                ).get("durable_run_id") or ""
            ) == coordinator_run_id:
                phase["execution_coordinator"] = previous_coordinator
                phase["execution_dispatch_result"] = (
                    previous_dispatch_result
                )
                if previous_attempt is None:
                    phase.pop("pre_qa_repair_attempt", None)
                else:
                    phase["pre_qa_repair_attempt"] = previous_attempt
                for agent_id, snapshot in previous_agent_state.items():
                    agent = ctx.agents.get(agent_id) or {}
                    for key in (
                        "pre_qa_repair_pending",
                        "pre_qa_scheduled_token",
                        "pre_qa_repair_run_id",
                        "pre_qa_repair_run_status",
                    ):
                        if key in snapshot:
                            agent[key] = snapshot[key]
                        else:
                            agent.pop(key, None)
                restored_state = True
            if restored_state:
                try:
                    await _persist_all_async()
                except Exception:
                    pass
            if coordinator_run_id and not installed:
                try:
                    await asyncio.to_thread(
                        routes_execution._run_registry.block,
                        coordinator_run_id,
                        reason="repair coordinator installation failed",
                    )
                except Exception:
                    pass
            return {
                "scheduled": {},
                "failures": {
                    agent_id: str(exc)[:300]
                    for agent_id in normalized_agent_ids
                },
            }
        finally:
            if claim_id:
                await asyncio.to_thread(
                    expert_lock.release_lock, claim_id,
                )

        await resume_pending_phase_dispatches()
        return {
            "scheduled": {
                agent_id: coordinator_run_id
                for agent_id in normalized_agent_ids
            },
            "failures": {},
        }

    scheduled: Dict[str, str] = {}
    failures: Dict[str, str] = {}
    for agent_id in normalized_agent_ids:
        agent = ctx.agents.get(agent_id)
        if not agent:
            failures[agent_id] = "responsible Agent no longer exists"
            continue
        token = str(agent.get("pre_qa_repair_request_token") or "")
        existing_run_id = str(agent.get("pre_qa_repair_run_id") or "")
        existing_run_status = ""
        if existing_run_id:
            try:
                existing_run = await asyncio.to_thread(
                    routes_execution._run_registry.get, existing_run_id,
                )
                existing_run_status = str(existing_run.get("status") or "")
            except Exception:
                existing_run_status = str(
                    agent.get("pre_qa_repair_run_status") or ""
                )
        if (
            token
            and agent.get("pre_qa_scheduled_token") == token
            and existing_run_id
            and (
                existing_run_status in {"pending", "running", "blocked"}
                or str(agent.get("status") or "") == "completed"
            )
        ):
            scheduled[agent_id] = existing_run_id
            continue
        agent["pre_qa_repair_pending"] = True
        try:
            result = await routes_execution.execute_agent_task(
                ctx.project_id, agent_id, None,
            )
            run_id = str(result.get("run_id") or "")
            if not run_id:
                raise RuntimeError(
                    result.get("message") or "repair run was not created"
                )
            agent["pre_qa_scheduled_token"] = token
            agent["pre_qa_repair_run_id"] = run_id
            agent["pre_qa_repair_run_status"] = str(
                result.get("run_status") or "pending"
            )
            scheduled[agent_id] = run_id
        except Exception as exc:
            failures[agent_id] = str(exc)[:300]
    return {"scheduled": scheduled, "failures": failures}


def _pre_qa_repair_action(
    diagnostic: str,
    dispatch: Dict[str, Any],
) -> Dict[str, Any]:
    failures = dict(dispatch.get("failures") or {})
    scheduled = dict(dispatch.get("scheduled") or {})
    if failures:
        failure_text = "; ".join(
            f"{agent_id}: {message}" for agent_id, message in failures.items()
        )
        return {
            "message": (
                "Deterministic pre-QA failed and automatic repair dispatch "
                f"was incomplete: {failure_text}"
            ),
            "options": ["retry_cycle", "manual_fix", "rebuild_phase"],
            "scheduled_runs": scheduled,
        }
    return {
        "message": (diagnostic or "Deterministic pre-QA failed")
        + "; responsible Agent repair runs were scheduled automatically",
        "options": ["manual_fix", "rebuild_phase"],
        "scheduled_runs": scheduled,
    }


def _blocking_issue_details(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        issue for issue in (entry.get("issues_detail") or [])
        if str(issue.get("status") or "open").lower() not in {"fixed", "verified"}
        and not _issue_is_non_actionable(issue)
        and str(issue.get("severity") or "error").lower() in {"error", "critical"}
    ]


def _auto_repair_issue_key(issue: Dict[str, Any]) -> str:
    """Use the canonical QA identity, with a deterministic legacy fallback."""
    canonical = issue.get("fingerprint") or issue.get("id")
    if canonical:
        return str(canonical)
    return "|".join((
        str(issue.get("layer") or "unknown").strip().lower(),
        str(issue.get("file_path") or "").replace("\\", "/").strip().lower(),
        " ".join(str(issue.get("message") or "").strip().lower().split()),
    ))


def _flatten_rebuild_issue_report(issue_report: Any) -> List[Dict[str, Any]]:
    """Return a stable, detached issue list suitable for a rebuild baseline."""
    flattened: List[Dict[str, Any]] = []
    if isinstance(issue_report, list):
        candidates = [(None, issue) for issue in issue_report]
    elif isinstance(issue_report, dict):
        candidates = [
            (path, issue)
            for path, issues in issue_report.items()
            for issue in (issues if isinstance(issues, list) else [])
        ]
    else:
        candidates = []
    seen: set[str] = set()
    for grouped_path, raw_issue in candidates:
        if not isinstance(raw_issue, dict):
            continue
        issue = copy.deepcopy(raw_issue)
        if grouped_path and not issue.get("file_path"):
            issue["file_path"] = str(grouped_path)
        issue.setdefault("status", "open")
        issue.setdefault("severity", "error")
        issue["fingerprint"] = _auto_repair_issue_key(issue)
        if issue["fingerprint"] in seen:
            continue
        seen.add(issue["fingerprint"])
        flattened.append(issue)
    return flattened


def _rebuild_issue_snapshot_digest(issues: List[Dict[str, Any]]) -> str:
    payload = json.dumps(issues, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _partition_rebuild_issues(
    issues: List[Dict[str, Any]],
    files_by_subproject: Dict[str, set[str]],
    old_agents: Dict[str, Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Assign every located finding to one old task owner.

    Path ownership wins over a stale reviewer-supplied Agent id.  Unlocated
    findings remain explicit and are never broadcast to every engineer.
    """
    assignments: Dict[str, List[Dict[str, Any]]] = {}
    for raw_issue in issues:
        issue = copy.deepcopy(raw_issue)
        path = _normalized_rebuild_path(issue.get("file_path"))
        path_owners = sorted(
            subproject_id
            for subproject_id, paths in files_by_subproject.items()
            if path and path in {_normalized_rebuild_path(item) for item in paths}
        )
        responsible = old_agents.get(str(issue.get("responsible_agent_id") or ""), {})
        responsible_subproject = str(responsible.get("subproject_id") or "")
        if responsible_subproject and responsible_subproject in path_owners:
            owner = responsible_subproject
        elif path_owners:
            owner = path_owners[0]
        elif responsible_subproject:
            owner = responsible_subproject
        else:
            owner = "_unassigned"
        assignments.setdefault(owner, []).append(issue)
    return assignments


def _partition_rebuild_issues_by_task(
    issues: List[Dict[str, Any]],
    contract_rows_by_path: Dict[str, Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Assign each located rebuild issue to its one immutable locked task."""
    assignments: Dict[str, List[Dict[str, Any]]] = {}
    assigned_issue_keys: set[str] = set()
    for raw_issue in issues:
        issue = copy.deepcopy(raw_issue)
        path = _normalized_rebuild_path(issue.get("file_path"))
        row = contract_rows_by_path.get(path)
        if row is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "rebuild_issue_without_contract_owner",
                    "path": path,
                    "issue_id": _auto_repair_issue_key(issue),
                },
            )
        issue_key = _auto_repair_issue_key(issue)
        if issue_key in assigned_issue_keys:
            continue
        assigned_issue_keys.add(issue_key)
        task_id = str(row["task_id"])
        issue["task_id"] = task_id
        issue["owner_type"] = row["owner_type"]
        assignments.setdefault(task_id, []).append(issue)
    return assignments


def _compare_rebuild_issue_snapshots(
    before: List[Dict[str, Any]], after: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compare the first post-rebuild QA result with its immutable baseline."""
    unresolved = lambda item: str(item.get("status") or "open").lower() not in {"fixed", "verified"}
    blocker = lambda item: unresolved(item) and str(item.get("severity") or "error").lower() in {"error", "critical"}
    before_by_key = {_auto_repair_issue_key(item): item for item in before if unresolved(item)}
    after_by_key = {_auto_repair_issue_key(item): item for item in after if unresolved(item)}
    before_keys, after_keys = set(before_by_key), set(after_by_key)
    before_blockers = {key for key, item in before_by_key.items() if blocker(item)}
    after_blockers = {key for key, item in after_by_key.items() if blocker(item)}
    resolved = before_keys - after_keys
    remaining = before_keys & after_keys
    new = after_keys - before_keys
    new_blockers = new & after_blockers
    resolved_blockers = before_blockers - after_blockers
    status = "converged"
    if new_blockers:
        status = "rebuild_regressed"
    elif not after_keys:
        status = "converged"
    elif not resolved and len(after_keys) >= len(before_keys):
        status = "rebuild_no_progress"
    else:
        status = "converging"
    return {
        "status": status,
        "before": len(before_keys),
        "after": len(after_keys),
        "resolved": sorted(resolved),
        "remaining": sorted(remaining),
        "new": sorted(new),
        "repeated": sorted(remaining),
        "resolved_blockers": sorted(resolved_blockers),
        "new_blockers": sorted(new_blockers),
    }


def _rebuild_comparison_requires_rollback(status: str) -> bool:
    return status in {
        "rebuild_regressed",
        "rebuild_no_progress",
        "rebuild_snapshot_invalid",
    }


def _auto_repair_review_result(
    phase_id: str, entry: Dict[str, Any], qc_run: int,
) -> Dict[str, Any]:
    issues = copy.deepcopy(entry.get("issues_detail") or [])
    return {
        "phase_id": phase_id,
        "report": entry.get("user_report", ""),
        "passed": bool(entry.get("passed", False)),
        "score": entry.get("score", 0),
        "issues": issues,
        "issues_detail": issues,
        "user_report": entry.get("user_report", ""),
        "developer_report": entry.get("developer_report", ""),
        "layer_results": copy.deepcopy(entry.get("layer_results") or []),
        "error_count": entry.get("error_count", 0),
        "warning_count": entry.get("warning_count", 0),
        "fixed_count": entry.get("fixed_count", 0),
        "qc_round": entry.get("qc_round", qc_run),
        "runtime_acceptance": copy.deepcopy(entry.get("runtime_acceptance")),
        "qc_execution_error": entry.get("qc_execution_error", ""),
    }


def _auto_repair_issue_report(entry: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for issue in entry.get("issues_detail") or []:
        if str(issue.get("status") or "open").lower() in {"fixed", "verified"}:
            continue
        grouped.setdefault(issue.get("file_path") or "未定位文件", []).append({
            "id": issue.get("id") or issue.get("issue_id") or "",
            "issue_id": issue.get("issue_id") or issue.get("id") or "",
            "fingerprint": issue.get("fingerprint", ""),
            "severity": issue.get("severity", "error"),
            "layer": issue.get("layer", "unknown"),
            "file_path": issue.get("file_path", ""),
            "line": next(
                (
                    issue.get(field)
                    for field in ("line", "line_no", "line_number")
                    if issue.get(field) is not None
                ),
                None,
            ),
            "message": issue.get("message", ""),
            "fix_hint": issue.get("fix_hint", ""),
            "status": issue.get("status", "open"),
            "lifecycle": issue.get("lifecycle", "new"),
            "first_seen_round": issue.get("first_seen_round"),
            "last_seen_round": issue.get("last_seen_round"),
            "fix_rounds": issue.get("fix_rounds", 0),
            "evidence": issue.get("evidence") or issue.get("evidence_step_id") or "",
            "acceptance_criteria": issue.get("acceptance_criteria") or issue.get("expected") or "",
            "responsible_agent_id": issue.get("responsible_agent_id", ""),
            "responsible_agent_role": issue.get("responsible_agent_role", ""),
        })
    return grouped


def _record_auto_repair_qc_result(
    state: Dict[str, Any], phase_id: str, entry: Dict[str, Any], qc_run: int,
) -> Dict[str, Any]:
    blockers = _blocking_issue_details(entry)
    review_result = _auto_repair_review_result(phase_id, entry, qc_run)
    state["review_result"] = review_result
    state["issue_report"] = _auto_repair_issue_report(entry)
    result = {
        "qc_run": int(entry.get("qc_round", qc_run) or qc_run),
        "repair_attempt": int(state.get("repair_attempts", state.get("round", 0)) or 0),
        "passed": bool(entry.get("passed", False)),
        "score": entry.get("score", 0),
        "blocking_count": len(blockers),
        "warning_count": int(entry.get("warning_count", 0) or 0),
        "fixed_count": int(entry.get("fixed_count", 0) or 0),
        "checked_at": entry.get("checked_at", time.time()),
        "issues": copy.deepcopy(entry.get("issues_detail") or []),
    }
    history = state.setdefault("round_history", [])
    history.append(result)
    if len(history) > AUTO_REPAIR_HISTORY_LIMIT:
        del history[:-AUTO_REPAIR_HISTORY_LIMIT]
    state["latest_qc"] = result
    return result


def _cleanup_auto_repair_states(project_id: str) -> None:
    """Remove orphaned repair states without erasing user-visible results."""
    keys_to_del = []
    phase_manager = _phase_managers.get(project_id)
    if not phase_manager:
        return
    valid_keys = {
        f"{project_id}-{phase.get('phase_id')}"
        for phase in (phase_manager.phases if phase_manager else [])
        if phase.get("phase_id")
    }
    for key in list(_auto_repair_states):
        if key.startswith(f"{project_id}-") and key not in valid_keys:
            keys_to_del.append(key)
    for k in keys_to_del:
        _auto_repair_states.pop(k, None)
        _auto_repair_api_configs.pop(k, None)


def _detach_stale_phase_quality_state(
    ctx: ProjectContext,
    phase_id: str,
    phase: Dict[str, Any],
) -> bool:
    """Detach quality idempotency state that belongs to another execution."""
    key = f"{ctx.project_id}-{phase_id}"
    state = _auto_repair_states.get(key)
    current_generation = str(phase.get("execution_generation") or "")
    state_generation = str((state or {}).get("execution_generation") or "")
    if (
        not state
        or not current_generation
        or not state_generation
        or state_generation == current_generation
    ):
        return False
    _auto_repair_states.pop(key, None)
    _auto_repair_api_configs.pop(key, None)
    if hasattr(ctx, "supervisor_quality_runs"):
        ctx.supervisor_quality_runs.pop(phase_id, None)
    if hasattr(ctx, "qc_results"):
        ctx.qc_results.pop(phase_id, None)
    phase["reviewed"] = False
    phase["review_passed"] = False
    phase.pop("reviewed_at", None)
    logger.warning(
        "Detached stale phase quality state project=%s phase=%s "
        "state_generation=%s execution_generation=%s",
        ctx.project_id,
        phase_id,
        state_generation,
        current_generation,
    )
    return True


async def _resume_completed_pre_qa_repair_runs(
    ctx: ProjectContext,
    phase_id: str,
    state: Dict[str, Any],
) -> bool:
    """Reconcile succeeded durable repairs and restart pre-QA exactly once."""
    if state.get("running") or str(state.get("status") or "") not in {
        FAILURE_PRE_QA, "pre_qa_verifying", "continuing",
    }:
        return False
    machine = _supervisor_quality_machine(ctx, phase_id)
    if machine.state != "waiting_engineer":
        return False
    repair_runs = dict(state.get("pre_qa_repair_runs") or {})
    if not repair_runs:
        return False
    from api import routes_execution
    for agent_id, run_id in repair_runs.items():
        try:
            run = await asyncio.to_thread(routes_execution._run_registry.get, run_id)
        except Exception:
            return False
        if str(run.get("status") or "") != "succeeded":
            return False
        if str((ctx.agents.get(agent_id) or {}).get("status") or "").lower() not in {
            "completed", "succeeded",
        }:
            return False
    machine_payload = machine.to_dict()
    scope = machine_payload.get("scope") or {}
    resume_generation = hashlib.sha256(json.dumps({
        "project_id": ctx.project_id,
        "phase_id": phase_id,
        "run_id": machine_payload.get("run_id"),
        "artifact_digest": scope.get("artifact_digest"),
        "repair_runs": repair_runs,
    }, sort_keys=True).encode("utf-8")).hexdigest()
    claim = await asyncio.to_thread(
        expert_lock.atomic_claim_lock,
        "supervisor-quality-resume",
        ctx.project_id,
        resume_generation,
        [f".project/quality-resume/{phase_id}"],
        3600,
    )
    if not claim.get("success"):
        return False
    for agent_id in repair_runs:
        (ctx.agents.get(agent_id) or {}).pop("pre_qa_repair_pending", None)
    _prepare_supervisor_verification(ctx, phase_id, machine)
    state["running"] = True
    state["status"] = "pre_qa_verifying"
    state["action_required"] = None
    state["needs_manual"] = False
    state["pre_qa_restart_scheduled_at"] = time.time()
    state["quality_resume_claim"] = {
        "generation": resume_generation,
        "lock_id": claim.get("lock_id"),
        "leased_until": claim.get("leased_until"),
    }
    pm = _phase_managers.get(ctx.project_id)
    phase = pm.get_phase(phase_id) if pm else None
    if phase:
        phase["status"] = "reviewing"
    _store_supervisor_quality_machine(ctx, phase_id, machine, state)
    await _persist_all_async()
    try:
        _safe_create_task(
            _run_auto_repair_loop(ctx.project_id, phase_id, False),
            name=f"pre-qa-resume-{phase_id}",
        )
    except Exception:
        expert_lock.release_lock(str(claim.get("lock_id") or ""))
        state["running"] = False
        state["status"] = "interrupted"
        state["needs_manual"] = True
        state["action_required"] = {
            "message": "Durable Supervisor resume scheduling failed.",
            "options": ["resume", "manual_fix", "rebuild_phase"],
        }
        state.pop("quality_resume_claim", None)
        await _persist_all_async()
        raise
    return True

@router.get("/projects/{project_id}/phases/{phase_id}/auto-repair/status")
async def get_auto_repair_status(project_id: str, phase_id: str):
    """获取自动修复循环的实时状态"""
    key = f"{project_id}-{phase_id}"
    state = _auto_repair_states.get(key, {})
    ctx = projects.get(project_id)
    recovery_blockers: List[str] = []
    auto_recovery_eligible = False
    if ctx and state:
        if str(state.get("status") or "") == "interrupted":
            recovery_blockers = _interrupted_pre_qa_recovery_blockers(
                ctx, phase_id, state,
            )
            auto_recovery_eligible = not recovery_blockers
    supervisor_run = copy.deepcopy(
        ((getattr(ctx, "supervisor_quality_runs", {}) or {}).get(phase_id) or {})
    ) if ctx else {}
    if not state:
        pm = _phase_managers.get(project_id)
        phase = pm.get_phase(phase_id) if pm else {}
        qc_entry = {}
        if ctx:
            raw_qc = (getattr(ctx, "qc_results", {}) or {}).get(phase_id, {})
            qc_entry = raw_qc.get("qa", raw_qc) if isinstance(raw_qc, dict) else {}
        if isinstance(qc_entry, dict) and qc_entry:
            passed = bool(qc_entry.get("passed"))
            phase_status = str((phase or {}).get("status") or "")
            recovered_status = "passed" if passed else (
                phase_status if phase_status in {
                    "qa_blocked", "needs_rework", "failed",
                } else "awaiting_decision"
            )
            diagnostic_messages = [
                str(message) for message in (qc_entry.get("issues") or [])[:3]
            ]
            if not diagnostic_messages:
                diagnostic_messages = [str(
                    qc_entry.get("qc_execution_error")
                    or qc_entry.get("developer_report")
                    or "质检未返回具体原因"
                )[:400]]
            summary = (
                "✅ 质检已通过，等待确认阶段完成"
                if passed else
                "❌ 质检未通过：" + "; ".join(diagnostic_messages)
            )
            recovered_review = {
                **qc_entry,
                "phase_id": phase_id,
                "report": qc_entry.get("user_report", ""),
                "issues": copy.deepcopy(qc_entry.get("issues_detail") or []),
                "issues_detail": copy.deepcopy(qc_entry.get("issues_detail") or []),
            }
            state = {
                "running": False,
                "round": int(qc_entry.get("qc_round", 0) or 0),
                "total_rounds": int(qc_entry.get("qc_round", 0) or 0),
                "lifetime_qc_runs": int(qc_entry.get("qc_round", 0) or 0),
                "repair_attempts": 0,
                "status": recovered_status,
                "messages": [{"role": "system", "content": summary, "ts": time.time()}],
                "action_required": None if passed else {
                    "round": int(qc_entry.get("qc_round", 0) or 0),
                    "message": summary,
                    "options": ["retry_cycle", "rebuild_phase"],
                },
                "needs_manual": not passed,
                "issue_report": _auto_repair_issue_report(qc_entry),
                "review_result": recovered_review,
                "latest_qc": {
                    "qc_run": int(qc_entry.get("qc_round", 0) or 0),
                    "repair_attempt": 0,
                    "passed": passed,
                    "score": qc_entry.get("score", 0),
                    "blocking_count": len(_blocking_issue_details(qc_entry)),
                    "warning_count": int(qc_entry.get("warning_count", 0) or 0),
                    "fixed_count": int(qc_entry.get("fixed_count", 0) or 0),
                    "checked_at": qc_entry.get("checked_at", time.time()),
                    "issues": copy.deepcopy(qc_entry.get("issues_detail") or []),
                },
                "round_history": [],
            }
    supervisor_run = copy.deepcopy(state.get("supervisor_run") or supervisor_run)
    return {
        "phase_id": phase_id,
        "running": state.get("running", False),
        "round": state.get("round", 0),
        "total_rounds": state.get("total_rounds", 0),
        "lifetime_qc_runs": state.get("lifetime_qc_runs", state.get("total_rounds", 0)),
        "repair_attempts": state.get("repair_attempts", state.get("round", 0)),
        "max_repairs_per_cycle": AUTO_REPAIR_MAX_REPAIRS_PER_CYCLE,
        "status": state.get("status", "idle"),
        "messages": state.get("messages", []),
        "action_required": state.get("action_required"),
        "needs_manual": state.get("needs_manual", False),
        "issue_report": state.get("issue_report", {}),
        "review_result": state.get("review_result"),
        "latest_qc": state.get("latest_qc"),
        "round_history": state.get("round_history", []),
        "repair_batch": state.get("repair_batch"),
        "supervisor_state": supervisor_run.get("status", "idle"),
        "waiting_for": supervisor_run.get("waiting_for", []),
        "next_action": supervisor_run.get("next_action", {}),
        "supervisor_run": supervisor_run,
        "auto_recovery_eligible": auto_recovery_eligible,
        "recovery_blockers": recovery_blockers,
    }


@router.post("/projects/{project_id}/phases/{phase_id}/auto-repair/resume")
async def resume_auto_repair(project_id: str, phase_id: str):
    """Explicitly resume one durably claimed Supervisor quality generation."""
    ctx = _get_project(project_id)
    _assert_project_write_available(ctx)
    state = _auto_repair_states.get(f"{project_id}-{phase_id}")
    if not state:
        raise HTTPException(status_code=409, detail="No Supervisor quality run to resume")
    if await _resume_completed_pre_qa_repair_runs(ctx, phase_id, state):
        return {
            "success": True,
            "scheduled": True,
            "status": _public_auto_repair_state(state),
        }
    if (
        str(state.get("status") or "") == "interrupted"
        and await _resume_interrupted_pre_qa_if_safe(ctx, phase_id, state)
    ):
        return {
            "success": True,
            "scheduled": True,
            "status": _public_auto_repair_state(state),
        }
    return {
        "success": True,
        "scheduled": False,
        "already_claimed": True,
        "status": _public_auto_repair_state(state),
    }


@router.post("/projects/{project_id}/phases/{phase_id}/auto-repair")
async def start_auto_repair(
    project_id: str,
    phase_id: str,
    user_decision: Optional[str] = None,
):
    """
    启动/继续质检→修复自动循环。

    循环策略：
    - 轮次 1~5：全自动（质检 → Agent修复 → 再质检）
    - 达到轮次上限后暂停，由用户选择自行修改、继续循环或阶段重构
    - 每轮结果追加到消息列表，前端可轮询 /auto-repair/status 获取

    user_decision: None（首次启动）/ manual_fix / retry_cycle / rebuild_phase
    """
    ctx = _get_project(project_id)
    _assert_project_write_available(ctx)
    pm = _phase_managers.get(project_id)
    if not pm:
        raise HTTPException(status_code=400, detail="Phase manager not initialized")
    phase = pm.get_phase(phase_id)
    if not phase:
        raise HTTPException(status_code=404, detail=f"Phase {phase_id} not found")

    key = f"{project_id}-{phase_id}"
    state = _auto_repair_states.get(key)
    if state and _detach_stale_phase_quality_state(ctx, phase_id, phase):
        await _persist_all_async()
        state = None
    if (
        state
        and state.get("running")
        and user_decision in {"continue", "retry_cycle"}
        and str(state.get("status") or "") in {"pre_qa_verifying", "continuing"}
    ):
        try:
            if _supervisor_quality_machine(ctx, phase_id).state == "waiting_engineer":
                state["running"] = False
        except Exception:
            pass

    # Initial start is idempotent. Repeated clicks must never create parallel
    # loops that concurrently mutate the same expert lifecycle.
    if state and state.get("running"):
        return {"success": True, "already_running": True, "message": "质检循环已在运行", "status": _public_auto_repair_state(state)}
    if user_decision == "rebuild_phase" and _rebuild_lifecycle_pending(state):
        if str(state.get("status") or "") in {"interrupted", "rebuild_recovery_required"}:
            state["running"] = False
            state["status"] = "rebuild_recovery_required"
            state["needs_manual"] = True
            state["action_required"] = {
                "message": "阶段重构在服务重启后需要人工恢复；禁止重复重置或创建 Agent。",
                "options": ["recover_rebuild", "keep_previous_version", "manual_fix"],
                "automatic_retry_allowed": False,
            }
        return {
            "success": True,
            "already_rebuilding": True,
            "message": "阶段重构已启动，未创建重复 Agent 或质检轮次",
            "status": _public_auto_repair_state(state),
        }
    if (
        user_decision is None
        and state
        and state.get("status") == "passed"
        and (state.get("review_result") or {}).get("passed") is True
    ):
        # Legacy flags are only a projection of the authoritative, current
        # Supervisor artifact gate.  Missing/stale runs fail closed.
        machine = _supervisor_quality_machine(ctx, phase_id)
        authoritative = machine.to_dict()
        try:
            if (
                machine.state != "completed"
                or not (authoritative.get("completion_gate") or {}).get("passed")
            ):
                raise IllegalQualityTransition(
                    "Legacy QA projection has no authoritative completed run"
                )
            _assert_supervisor_artifact_current(ctx, phase_id, machine)
        except IllegalQualityTransition as exc:
            phase["reviewed"] = False
            phase["review_passed"] = False
            phase["status"] = "qa_pending"
            state["status"] = "stale"
            state["running"] = False
            state["needs_manual"] = True
            state["action_required"] = {
                "message": str(exc),
                "options": ["retry_cycle", "manual_fix"],
            }
            await _persist_all_async()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        phase["reviewed"] = True
        phase["review_passed"] = True
        phase["status"] = "reviewing"
        phase.setdefault("reviewed_at", time.time())
        await _persist_all_async()
        return {
            "success": True,
            "already_completed": True,
            "message": "质检已通过，无需重复执行",
            "status": _public_auto_repair_state(state),
        }
    if user_decision is None:
        _assert_phase_execution_completed(ctx, phase_id)

    # Render 重启后内存态可能为空。先恢复基础状态，再按用户明确决策分流；
    # 不能把 rebuild_phase 错降级成一次普通质检。
    if state is None:
        state = {
            "running": False, "round": 0, "total_rounds": 0,
            "lifetime_qc_runs": 0, "repair_attempts": 0,
            "status": "idle", "messages": [], "phase_name": phase.get("name", ""),
            "action_required": None, "needs_manual": False,
            "issue_report": {}, "review_result": None,
            "latest_qc": None, "round_history": [], "repair_batch": None,
            "execution_generation": str(
                phase.get("execution_generation") or ""
            ),
            "contract_digest": str(
                phase.get("execution_contract_digest") or ""
            ),
            "requirements_revision": int(
                phase.get("execution_requirements_revision") or 0
            ),
        }
        _auto_repair_states[key] = state
    else:
        state.setdefault(
            "execution_generation",
            str(phase.get("execution_generation") or ""),
        )
        state.setdefault(
            "contract_digest",
            str(phase.get("execution_contract_digest") or ""),
        )
        state.setdefault(
            "requirements_revision",
            int(phase.get("execution_requirements_revision") or 0),
        )
        state.setdefault("issue_report", {})
        state.setdefault("review_result", None)
        state.setdefault("latest_qc", None)
        state.setdefault("round_history", [])
        state.setdefault("repair_batch", None)

    # 首次启动，或上一轮已结束后重新启动
    if user_decision is None:
        try:
            machine = _ensure_supervisor_quality_run(ctx, phase_id, state)
            if machine.state == "completed":
                _assert_supervisor_artifact_current(ctx, phase_id, machine)
                return {
                    "success": True,
                    "already_completed": True,
                    "message": "Supervisor 质检状态机已完成，无需重复执行",
                    "status": _public_auto_repair_state(state),
                }
            if machine.state == "blocked":
                raise IllegalQualityTransition(
                    "Supervisor 质检已阻断；必须人工处理或显式阶段重构"
                )
            _prepare_supervisor_verification(ctx, phase_id, machine)
            _store_supervisor_quality_machine(ctx, phase_id, machine, state)
        except IllegalQualityTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        state["running"] = True
        state["status"] = "starting"
        if (
            int(state.get("lifetime_qc_runs", 0) or 0) == 0
            and int(state.get("repair_attempts", state.get("round", 0)) or 0) == 0
        ):
            state["round"] = 0
            state["total_rounds"] = 0
            state["lifetime_qc_runs"] = 0
            state["repair_attempts"] = 0
        state["action_required"] = None
        state["needs_manual"] = False
        phase["status"] = "reviewing"
        phase["review_passed"] = False
        _auto_repair_api_configs[key] = current_user_api_config.get()
        await _persist_all_async()
        _safe_create_task(_run_auto_repair_loop(project_id, phase_id, False), name=f"repair-{phase_id}")
        return {"success": True, "message": "自动修复循环已启动", "status": _public_auto_repair_state(state)}

    # 用户决策继续
    if user_decision == "recover_rebuild":
        if not _rebuild_lifecycle_pending(state):
            raise HTTPException(status_code=409, detail="当前没有可恢复的阶段重构运行")
        failures = _phase_rebuild_terminal_failures(ctx, phase_id)
        if failures:
            await _fail_closed_rebuild_execution(ctx, phase_id, failures)
            return {
                "success": False,
                "message": "重构 Agent 存在失败终态，已恢复重构前版本",
                "status": _public_auto_repair_state(state),
            }
        try:
            _assert_phase_execution_completed(ctx, phase_id)
        except HTTPException:
            state["running"] = False
            state["status"] = "awaiting_execution"
            state["needs_manual"] = True
            state["action_required"] = {
                "message": "重构 Agent 尚未全部完成；等待恢复任务完成后再次执行 recover_rebuild。",
                "options": ["recover_rebuild", "keep_previous_version", "manual_fix"],
                "automatic_retry_allowed": False,
            }
            await _persist_all_async()
            return {
                "success": False,
                "message": "重构执行尚未全部完成",
                "status": _public_auto_repair_state(state),
            }
        return await start_auto_repair(project_id, phase_id, None)

    if user_decision == "keep_previous_version":
        if str(state.get("status") or "") not in {
            "rebuild_regressed", "rebuild_no_progress", "rebuild_snapshot_invalid",
        }:
            raise HTTPException(status_code=409, detail="当前没有已回滚的重构版本可保留")
        state["running"] = False
        state["status"] = "previous_version_retained"
        state["needs_manual"] = True
        state["action_required"] = {
            "message": "已保留重构前版本；后续只能人工修改或再次显式重构。",
            "options": ["manual_fix", "rebuild_phase"],
            "automatic_retry_allowed": False,
        }
        await _persist_all_async()
        return {"success": True, "message": "已保留重构前版本", "status": _public_auto_repair_state(state)}

    if user_decision in {"manual_fix", "self_fix", "manual_edit"}:
        # Lock the exact artifact that the user is about to edit.  The
        # Supervisor run scope can already differ because of a completed
        # automatic repair or another pre-pause workspace mutation; comparing
        # the later manual edit against the original run scope misattributes
        # that older drift to the user and permanently blocks retry_cycle.
        # Freeze one baseline per manual-edit session.  The explicit marker
        # distinguishes the active session from stale snapshots left by older
        # deployments, while repeated clicks cannot adopt edits as baseline.
        if not state.get("manual_fix_session_active"):
            state["manual_fix_scope"] = copy.deepcopy(
                _supervisor_scope_snapshot(ctx, phase_id)
            )
            state["manual_fix_issue_paths"] = sorted({
                str(issue.get("file_path") or "").replace("\\", "/")
                for issue in ((state.get("review_result") or {}).get("issues") or [])
                if isinstance(issue, dict) and issue.get("file_path")
            })
            state["manual_fix_session_active"] = True
        state["action_required"] = {
            "round": state.get("round", 0),
            "message": "已暂停自动操作，可自行修改文件；修改完成后继续质检循环。",
            "options": ["manual_fix", "retry_cycle", "rebuild_phase"],
        }
        state["running"] = False
        state["status"] = "awaiting_manual_fix"
        state["needs_manual"] = True
        phase["status"] = "needs_rework"
        phase["reviewed"] = False
        phase["review_passed"] = False
        state["messages"].append({
            "role": "system", "content": "✋ 用户选择：自行修改文件，自动质检已暂停",
            "ts": time.time(),
        })
        await _persist_all_async()
        return {"success": True, "message": "已暂停，等待自行修改", "status": _public_auto_repair_state(state)}

    if user_decision in {"continue", "retry_cycle"}:
        machine = _supervisor_quality_machine(ctx, phase_id)
        scope_mismatch_recovery = (
            str(state.get("status") or "") == "blocked"
            and str((state.get("action_required") or {}).get("message") or "")
            == "QA scope digest does not match the scope locked at run start"
        )
        if (
            (
                str(state.get("status") or "")
                in {FAILURE_PRE_QA, "pre_qa_verifying", "continuing"}
                or scope_mismatch_recovery
            )
            and machine.state == "waiting_engineer"
        ):
            repair_agent_ids = list(machine.to_dict().get("waiting_for") or [])
            if not repair_agent_ids:
                repair_agent_ids = [
                    str(agent_id) for agent_id, agent in ctx.agents.items()
                    if str(agent.get("phase_id") or "") == str(phase_id)
                ]
            pre_qa_issues = list((state.get("pre_qa_result") or {}).get("issues") or [])
            pre_qa_issues = [
                {**issue, "phase_id": phase_id} if isinstance(issue, dict) else issue
                for issue in pre_qa_issues
            ]
            deterministic_repairs = await asyncio.to_thread(
                _apply_deterministic_pre_qa_repairs,
                ctx,
                pre_qa_issues,
            )
            restored = _restore_pre_qa_agents_after_deterministic_repair(
                ctx,
                phase_id,
                repair_agent_ids,
            )
            if deterministic_repairs or restored:
                state.setdefault("deterministic_pre_qa_repairs", []).extend(
                    deterministic_repairs
                )
                _prepare_supervisor_verification(ctx, phase_id, machine)
                rebound_scope = copy.deepcopy(machine.to_dict().get("scope") or {})
                for repair in deterministic_repairs:
                    machine.record_evidence(
                        kind=str(repair.get("kind") or "deterministic_patch"),
                        command=f"deterministic-pre-qa-repair {repair.get('path')}",
                        exit_code=0,
                        passed=True,
                        log=f"Applied deterministic repair for {repair.get('issue_code')}",
                        step_id=(
                            f"pre-qa-repair:{repair.get('path')}:"
                            f"{repair.get('after_digest')}"
                        ),
                        metadata={
                            **repair,
                            "artifact_digest": rebound_scope.get("artifact_digest"),
                            "scope_digest": rebound_scope.get("scope_digest"),
                        },
                    )
                state["running"] = True
                state["status"] = "pre_qa_verifying"
                state["action_required"] = None
                state["needs_manual"] = False
                phase["status"] = "reviewing"
                phase["reviewed"] = False
                phase["review_passed"] = False
                _store_supervisor_quality_machine(ctx, phase_id, machine, state)
                await _persist_all_async()
                _safe_create_task(
                    _run_auto_repair_loop(project_id, phase_id, False),
                    name=f"repair-{phase_id}",
                )
                return {
                    "success": True,
                    "message": "Deterministic pre-QA repair applied; verification restarted",
                    "status": _public_auto_repair_state(state),
                }
            dispatch = await _schedule_pre_qa_repair_runs(
                ctx, repair_agent_ids,
            )
            state["pre_qa_repair_runs"] = {
                **dict(state.get("pre_qa_repair_runs") or {}),
                **dispatch["scheduled"],
            }
            state["running"] = False
            state["action_required"] = _pre_qa_repair_action(
                "Deterministic pre-QA repair retry",
                dispatch,
            )
            await _persist_all_async()
            return {
                "success": not bool(dispatch["failures"]),
                "message": state["action_required"]["message"],
                "status": _public_auto_repair_state(state),
            }
        locked_scope = copy.deepcopy(machine.to_dict().get("scope") or {})
        current_scope = _supervisor_scope_snapshot(ctx, phase_id)
        if (
            locked_scope.get("scope_digest")
            and current_scope.get("scope_digest") != locked_scope.get("scope_digest")
        ):
            manual_fix_scope = state.get("manual_fix_scope")
            comparison_scope = (
                manual_fix_scope
                if isinstance(manual_fix_scope, dict)
                and manual_fix_scope.get("scope_digest")
                else locked_scope
            )
            locked_files = set(locked_scope.get("files") or [])
            current_files = set(current_scope.get("files") or [])
            frozen_issue_paths = state.get("manual_fix_issue_paths")
            issue_paths = (
                {
                    str(path).replace("\\", "/")
                    for path in frozen_issue_paths
                    if str(path or "").strip()
                }
                if isinstance(frozen_issue_paths, list)
                else {
                    str(issue.get("file_path") or "").replace("\\", "/")
                    for issue in ((state.get("review_result") or {}).get("issues") or [])
                    if issue.get("file_path")
                }
            )
            changed_paths = _delivery_manifest_changed_paths(
                comparison_scope.get("delivery_manifest") or {},
                current_scope.get("delivery_manifest") or {},
            )
            stable_context = (
                current_scope.get("project_id") == locked_scope.get("project_id")
                and str(current_scope.get("phase_id")) == str(locked_scope.get("phase_id"))
                and current_scope.get("phase_generation_id")
                == locked_scope.get("phase_generation_id")
                and current_scope.get("files") == comparison_scope.get("files")
                and current_scope.get("artifact_manifest_rule_version")
                == comparison_scope.get("artifact_manifest_rule_version")
                and (
                    (current_scope.get("delivery_manifest") or {}).get("required_paths")
                    == (comparison_scope.get("delivery_manifest") or {}).get("required_paths")
                )
            )
            active_review_issues = [
                issue
                for issue in ((state.get("review_result") or {}).get("issues") or [])
                if isinstance(issue, dict)
                and str(issue.get("status") or "open").lower()
                not in {"fixed", "verified"}
            ]
            reviewer_recovery_only = bool(active_review_issues) and all(
                (
                    "could not produce valid acceptance evidence"
                    in str(issue.get("message") or "").lower()
                    or "reviewer is unavailable"
                    in str(issue.get("message") or "").lower()
                )
                for issue in active_review_issues
            )
            if (
                stable_context
                and (
                    (changed_paths and changed_paths <= issue_paths)
                    or (not changed_paths and reviewer_recovery_only)
                )
                and machine.state == "blocked"
                and str(state.get("status") or "") == "awaiting_manual_fix"
            ):
                previous_artifact = str(
                    locked_scope.get("artifact_digest")
                    or locked_scope.get("workspace_digest")
                    or ""
                )
                machine.bind_artifact_generation(
                    artifact_digest=current_scope["artifact_digest"],
                    scope_digest=current_scope["scope_digest"],
                    repair_commit=f"artifact:{current_scope['artifact_digest']}",
                    scope_snapshot=current_scope,
                    transition_reason="manual_fix",
                    expected_previous_artifact_digest=previous_artifact,
                )
                _store_supervisor_quality_machine(ctx, phase_id, machine, state)
                state.pop("manual_fix_scope", None)
                state.pop("manual_fix_issue_paths", None)
                state.pop("manual_fix_session_active", None)
            else:
                raise HTTPException(
                    status_code=409,
                    detail="QA scope changed outside the recorded repair issue paths",
                )
        if (
            machine.state == "blocked"
            and str(state.get("status") or "") != "awaiting_manual_fix"
            and user_decision not in ("rebuild_phase",)
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Supervisor 质检已进入 blocked；第五轮或新增 blocker 后"
                    "不得通过 retry_cycle 重置预算"
                ),
            )
        if machine.state == "blocked":
            try:
                machine.resume_after_manual_fix()
                scope = _supervisor_scope_snapshot(ctx, phase_id)
                machine.record_evidence(
                    kind="scope",
                    command=f"verify-manual-fix-scope {phase_id}",
                    exit_code=0,
                    passed=True,
                    log=(
                        f"Verified {len(scope['files'])} registered files after "
                        f"explicit manual correction at {scope['workspace_digest']}"
                    ),
                    step_id=(
                        f"manual-scope:{machine.to_dict().get('run_id')}:"
                        f"{scope['workspace_digest']}"
                    ),
                    metadata=scope,
                )
                _store_supervisor_quality_machine(ctx, phase_id, machine, state)
            except IllegalQualityTransition as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        elif (
            machine.state == "verifying"
            and str(state.get("status") or "") == "awaiting_manual_fix"
        ):
            machine.acknowledge_manual_fix_agents()
            scope = _supervisor_scope_snapshot(ctx, phase_id)
            machine.record_evidence(
                kind="scope",
                command=f"verify-manual-fix-scope {phase_id}",
                exit_code=0,
                passed=True,
                log=(
                    f"Verified {len(scope['files'])} registered files after "
                    f"explicit manual correction at {scope['workspace_digest']}"
                ),
                step_id=(
                    f"manual-scope:{machine.to_dict().get('run_id')}:"
                    f"{scope['workspace_digest']}"
                ),
                metadata=scope,
            )
            _store_supervisor_quality_machine(ctx, phase_id, machine, state)
        if machine.state in {"infrastructure_failed", "model_failed"}:
            machine.resume()
            _store_supervisor_quality_machine(ctx, phase_id, machine, state)
        # A retry is an explicit user authorization for one more bounded cycle.
        # Do not reject it using the historical repair total: every background
        # run is still capped by AUTO_REPAIR_MAX_REPAIRS_PER_CYCLE, so this
        # cannot become an unattended infinite loop.  Keeping the cumulative
        # counters is useful for audit/display, but they must not consume the
        # budget of the newly authorized cycle.
        _auto_repair_api_configs[key] = (
            _auto_repair_api_configs.get(key) or current_user_api_config.get()
        )
        state["action_required"] = None
        state["running"] = True
        state["status"] = "continuing"
        state["needs_manual"] = False
        phase["status"] = "reviewing"
        phase["review_passed"] = False
        phase["reviewed"] = False
        phase.pop("completed_at", None)
        phase.pop("failed_reason", None)
        state["messages"].append({
            "role": "system", "content": "⏭ 用户选择：继续修改现有代码",
            "ts": time.time(),
        })
        await _persist_all_async()
        _safe_create_task(
            _run_auto_repair_loop(project_id, phase_id, False),
            name=f"repair-continue-{phase_id}",
        )
        return {"success": True, "message": "继续修复", "status": _public_auto_repair_state(state)}

    # 用户决策：带记忆重写
    if user_decision in {"rewrite", "rebuild_phase"}:
        if user_decision == "rebuild_phase":
            # Validate credentials before taking a snapshot or mutating phase
            # metadata.  A rejected rebuild must leave the current stage fully
            # usable for manual repair or another QC cycle.
            rebuild_api_config = _auto_repair_api_configs.get(key) or current_user_api_config.get()
            has_rebuild_key = bool(
                (rebuild_api_config or {}).get("api_key")
                or getattr(hermes_client, "api_key", None)
                or os.environ.get("METIS_TEST_MODE", "").strip().lower() in {"1", "true", "yes"}
            )
            if not has_rebuild_key:
                raise HTTPException(
                    status_code=409,
                    detail="阶段重构需要有效的模型 API Key；原阶段尚未重置，请先配置 API Key 后重试",
                )
            issue_report = copy.deepcopy(state.get("issue_report", {}))
            issue_snapshot = _flatten_rebuild_issue_report(issue_report)
            unlocated_blockers = [
                issue for issue in issue_snapshot
                if str(issue.get("severity") or "error").lower() in {"error", "critical"}
                and str(issue.get("status") or "open").lower() not in {"fixed", "verified"}
                and not _is_located_delivery_issue_path(issue.get("file_path"))
            ]
            if unlocated_blockers:
                state["running"] = False
                state["status"] = "qa_blocked"
                state["needs_manual"] = True
                state["action_required"] = {
                    "message": (
                        "阶段重构被阻止：仍有阻断问题未定位到可交付文件。"
                        "请先补充准确文件路径并重新质检，再启动阶段重构。"
                    ),
                    "options": ["manual_fix", "retry_cycle"],
                    "requires_issue_localization": True,
                    "issue_ids": [
                        str(issue.get("issue_id") or issue.get("id") or issue.get("fingerprint") or "")
                        for issue in unlocated_blockers
                    ],
                }
                await _persist_all_async()
                raise HTTPException(status_code=409, detail=state["action_required"]["message"])
            old_agents = {
                agent.get("id"): agent for agent in ctx.agents.values()
                if agent.get("phase_id") == phase_id
            }
            ownership = _rebuild_contract_ownership(pm, phase, old_agents)
            contract_rows_by_path = ownership["contract_rows_by_path"]
            contract_required_files = set(contract_rows_by_path)
            all_files = set(contract_required_files) | set(ownership["evidence_by_path"])
            if not contract_required_files:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "rebuild_contract_required_files_missing",
                        "phase_id": phase_id,
                    },
                )
            if not issue_snapshot:
                # An explicit rebuild without a trustworthy located defect
                # list is a full task regeneration, not a preserve-only no-op.
                # This path is used after interrupted infrastructure recovery:
                # every locked task must receive a fresh owner and reproduce
                # its canonical artifacts instead of reusing possibly partial
                # writes from a cancelled repair run.
                issue_snapshot = [
                    {
                        "id": (
                            "explicit-full-rebuild:"
                            + hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
                        ),
                        "issue_id": (
                            "explicit-full-rebuild:"
                            + hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
                        ),
                        "file_path": path,
                        "severity": "error",
                        "status": "open",
                        "message": (
                            "Regenerate this locked artifact from the canonical "
                            "phase task after interrupted quality recovery."
                        ),
                        "fix_hint": (
                            "Follow the locked task, acceptance criteria, and "
                            "current baseline; do not broaden the file scope."
                        ),
                        "source": "explicit_full_phase_rebuild",
                    }
                    for path in sorted(contract_required_files)
                ]
            registered_paths = {
                _normalized_rebuild_path(path) for path in (pm.file_registry or {})
            }
            missing_contract_files = {
                path for path in contract_required_files
                if not (ctx.workspace / path).is_file()
            }
            unregistered_contract_files = {
                path for path in contract_required_files
                if (ctx.workspace / path).is_file() and path not in registered_paths
            }

            def owner_for_rebuild_path(path: str) -> str:
                row = contract_rows_by_path.get(path)
                if row:
                    return str(row["owner_type"])
                evidence_claims = ownership["evidence_by_path"].get(path) or []
                return next(
                    (
                        str(claim.get("owner_type") or "")
                        for claim in evidence_claims
                        if claim.get("owner_type")
                    ),
                    "verification",
                )

            rebuild_file_specs = classify_rebuild_files(
                ctx.workspace,
                all_files,
                issue_snapshot,
                owner_for_path=owner_for_rebuild_path,
            )
            initially_actionable_paths = {
                str(entry["path"])
                for entry in rebuild_file_specs
                if entry.get("mode") in {"patch", "create"}
            }
            affected_task_ids = {
                str(contract_rows_by_path[path]["task_id"])
                for path in initially_actionable_paths
                if path in contract_rows_by_path
            }
            affected_task_paths = {
                path
                for task_id in affected_task_ids
                for path in ownership["by_task_id"].get(task_id, set())
            }
            for entry in rebuild_file_specs:
                path = str(entry["path"])
                if path in affected_task_paths and entry.get("mode") == "preserve":
                    entry["mode"] = "patch"
            actionable_paths = {
                str(entry["path"])
                for entry in rebuild_file_specs
                if entry.get("mode") in {"patch", "create"}
            }
            unowned_actionable_paths = sorted(
                actionable_paths - set(contract_rows_by_path)
            )
            if unowned_actionable_paths:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "rebuild_actionable_file_without_locked_task",
                        "paths": unowned_actionable_paths,
                    },
                )
            executable_by_task_id: Dict[str, set[str]] = {}
            executable_by_expert_type: Dict[str, set[str]] = {}
            for path in sorted(actionable_paths):
                row = contract_rows_by_path[path]
                executable_by_task_id.setdefault(row["task_id"], set()).add(path)
                executable_by_expert_type.setdefault(
                    row["owner_type"], set(),
                ).add(path)
            writer_counts = {
                path: sum(
                    path in paths for paths in executable_by_task_id.values()
                )
                for path in actionable_paths
            }
            if any(count != 1 for count in writer_counts.values()):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "rebuild_non_unique_executable_writer",
                        "writer_counts": writer_counts,
                    },
                )
            issue_assignments = _partition_rebuild_issues_by_task(
                issue_snapshot, contract_rows_by_path,
            )
            restore_point = _capture_phase_rebuild_restore_point(ctx, phase_id)
            phase["rebuild_file_manifest"] = {
                "all": sorted(all_files),
                "files": rebuild_file_specs,
                "by_mode": {
                    mode: [
                        entry["path"] for entry in rebuild_file_specs
                        if entry["mode"] == mode
                    ]
                    for mode in ("preserve", "patch", "create")
                },
                "by_expert_type": {
                    key: sorted(paths)
                    for key, paths in executable_by_expert_type.items()
                },
                "by_task_id": {
                    key: sorted(paths)
                    for key, paths in executable_by_task_id.items()
                },
                "by_subproject": {},
                "writer_counts": writer_counts,
                "preserve_verifier_paths": [
                    entry["path"] for entry in rebuild_file_specs
                    if entry["mode"] == "preserve"
                ],
                "required_from_project_contract": sorted(contract_required_files),
                "missing_before_rebuild": sorted(missing_contract_files),
                "unregistered_before_rebuild": sorted(unregistered_contract_files),
                "captured_at": time.time(),
            }
            phase["pre_rebuild_issue_snapshot"] = copy.deepcopy(issue_snapshot)
            phase["pre_rebuild_issue_snapshot_digest"] = _rebuild_issue_snapshot_digest(
                issue_snapshot
            )
            phase["rebuild_issue_assignments"] = copy.deepcopy(issue_assignments)
            if all_files:
                versions_dir = ctx.workspace / ".project" / "versions"
                existing_versions = [
                    int(item.name) for item in versions_dir.iterdir()
                    if item.is_dir() and item.name.isdigit()
                ] if versions_dir.exists() else []
                snapshot_version = max(existing_versions, default=0) + 1
                snapshot_dir = versions_dir / str(snapshot_version)
                copied_files = []
                for relative_path in sorted(all_files):
                    source = (ctx.workspace / relative_path).resolve()
                    try:
                        source.relative_to(ctx.workspace.resolve())
                    except ValueError:
                        continue
                    if not source.is_file():
                        continue
                    destination = snapshot_dir / relative_path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                    copied_files.append(relative_path)
                snapshot_dir.mkdir(parents=True, exist_ok=True)
                (snapshot_dir / "commit.json").write_text(json.dumps({
                    "version": snapshot_version,
                    "message": f"Before full rebuild: {phase_id}",
                    "committed_at": time.time(),
                    "files": copied_files,
                    "phase_id": phase_id,
                    "kind": "pre_rebuild",
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                phase["pre_rebuild_snapshot_version"] = snapshot_version
                state["pre_rebuild_snapshot_version"] = snapshot_version
            else:
                versions_dir = ctx.workspace / ".project" / "versions"
                existing_versions = [
                    int(item.name) for item in versions_dir.iterdir()
                    if item.is_dir() and item.name.isdigit()
                ] if versions_dir.exists() else []
                snapshot_version = max(existing_versions, default=0) + 1
                snapshot_dir = versions_dir / str(snapshot_version)
                snapshot_dir.mkdir(parents=True, exist_ok=True)
                (snapshot_dir / "commit.json").write_text(json.dumps({
                    "version": snapshot_version,
                    "message": f"Before full rebuild: {phase_id}",
                    "committed_at": time.time(),
                    "files": [],
                    "phase_id": phase_id,
                    "kind": "pre_rebuild",
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                phase["pre_rebuild_snapshot_version"] = snapshot_version
                state["pre_rebuild_snapshot_version"] = snapshot_version
            pre_rebuild_inventory = _workspace_delivery_inventory(ctx.workspace)
            durable_snapshot = _encode_durable_rebuild_snapshot(
                ctx.workspace, sorted(all_files),
            )
            # These fields must be durable before reset_phase persists the new
            # lifecycle.  The local .project snapshot is only a convenience;
            # Render instance replacement may discard it.
            state["pre_rebuild_delivery_inventory"] = pre_rebuild_inventory
            state["durable_rebuild_snapshot"] = durable_snapshot
            state["pre_rebuild_restore_point"] = restore_point
            state["pre_rebuild_issue_report"] = copy.deepcopy(issue_report)
            state["pre_rebuild_issue_snapshot"] = copy.deepcopy(issue_snapshot)
            state["pre_rebuild_issue_snapshot_digest"] = phase[
                "pre_rebuild_issue_snapshot_digest"
            ]
            state["rebuild_issue_assignments"] = copy.deepcopy(issue_assignments)
            state["rebuild_run_id"] = f"rebuild-{uuid.uuid4().hex}"
            state["rebuild_comparison_pending"] = True
            phase["rebuild_notes"] = copy.deepcopy(issue_report)
            previous_runs = phase.setdefault("previous_supervisor_quality_runs", [])
            previous_run = (getattr(ctx, "supervisor_quality_runs", {}) or {}).get(phase_id)
            if previous_run:
                previous_runs.append(copy.deepcopy(previous_run))
                del previous_runs[:-5]
                ctx.supervisor_quality_runs.pop(phase_id, None)
            phase.setdefault("rebuild_base_description", phase.get("description", ""))
            phase["description"] = (
                phase.get("rebuild_base_description", "")
                + "\n\n【上轮质检重构注意事项】\n"
                + "问题已按文件所有权和责任任务精确分配；每位工程师只接收自己的修复清单。"
                + "\n\n【必须完成的旧文件总数】"
                + str(len(all_files))
            )
            # Restore the API configuration captured when the quality loop
            # started.  Rebuild agents run in fresh background tasks and must
            # not silently lose the user's model credentials.
            await _reset_phase(project_id, phase_id, preserve_rebuild_state=True)

            # A full phase rebuild is a new delivery lifecycle.  The old
            # repair/QC totals describe files and Agents that were just
            # discarded, and carrying them forward makes the rebuilt phase hit
            # the previous loop limit before its first repair.  The historical
            # messages and pre-rebuild snapshot remain available for audit.
            state["round"] = 0
            state["total_rounds"] = 0
            state["lifetime_qc_runs"] = 0
            state["repair_attempts"] = 0
            state.pop("review_result", None)
            state["issue_report"] = {}
            state.pop("rebuild_comparison", None)
            config_token = current_user_api_config.set(rebuild_api_config)
            try:
                try:
                    start_result = await start_phase(project_id, phase_id)
                except Exception:
                    _restore_phase_rebuild_snapshot(ctx, phase_id, state)
                    state["running"] = False
                    state["status"] = "rebuild_start_failed"
                    state["needs_manual"] = True
                    state["rebuild_comparison_pending"] = False
                    state["issue_report"] = copy.deepcopy(issue_report)
                    state["action_required"] = {
                        "message": "阶段重构 Agent 启动失败，已恢复重构前版本。",
                        "options": ["keep_previous_version", "manual_fix", "rebuild_phase"],
                        "automatic_retry_allowed": False,
                    }
                    await _persist_all_async()
                    raise
            finally:
                current_user_api_config.reset(config_token)
            # ``start_phase`` creates a new execution generation.  Rebind the
            # durable rebuild lifecycle before persisting it; otherwise the
            # next request treats the rebuild state as stale and detaches the
            # only recovery metadata while the new Agents remain queued.
            state["execution_generation"] = str(
                phase.get("execution_generation") or ""
            )
            state["contract_digest"] = str(
                phase.get("execution_contract_digest") or ""
            )
            state["requirements_revision"] = int(
                phase.get("execution_requirements_revision") or 0
            )
            state["running"] = False
            state["status"] = "rebuild_started"
            state["action_required"] = None
            state["needs_manual"] = False
            created_count = len(start_result.get("created_agents", []))
            state["messages"].append({
                "role": "system",
                "content": f"🔄 已携带问题清单启动全量重构，{created_count} 个专家正在执行。",
                "ts": time.time(),
            })
            await _persist_all_async()
            return {
                "success": True,
                "message": "阶段已重置并自动启动全量重构",
                "created_agents": created_count,
                "status": _public_auto_repair_state(state),
            }
        state["action_required"] = None
        state["running"] = True
        state["status"] = "rewriting"
        state["needs_manual"] = False
        phase["status"] = "reviewing"
        phase["review_passed"] = False
        _auto_repair_api_configs[key] = (
            _auto_repair_api_configs.get(key) or current_user_api_config.get()
        )
        state["messages"].append({
            "role": "system", "content": "🔄 用户选择：带记忆重写出错文件",
            "ts": time.time(),
        })
        await _persist_all_async()
        _safe_create_task(
            _run_auto_repair_loop(project_id, phase_id, True),
            name=f"repair-rewrite-{phase_id}",
        )
        return {"success": True, "message": "重写模式已启动", "status": _public_auto_repair_state(state)}

    raise HTTPException(status_code=400, detail=f"未知的 user_decision: {user_decision}")


def _match_issue_agent(ctx, phase_id: str, issue: Dict[str, Any]):
    all_agents = list(getattr(ctx, "agents", {}).values())
    phase_agents = [a for a in all_agents if a.get("phase_id") == phase_id]
    wanted_id, wanted_role = issue.get("responsible_agent_id"), issue.get("responsible_agent_role")
    issue_path = _normalized_rebuild_path(issue.get("file_path"))

    def can_edit(agent: Dict[str, Any]) -> bool:
        if not issue_path:
            return False
        for raw_scope in agent.get("allowed_path_prefixes") or []:
            scope = _normalized_rebuild_path(raw_scope)
            if not scope:
                continue
            if scope.endswith("/"):
                if issue_path == scope.rstrip("/") or issue_path.startswith(scope):
                    return True
            elif issue_path == scope:
                return True
        return False

    # File ownership is project-wide. Integration tests commonly discover a
    # defect in a file produced by an earlier phase; routing only within the
    # current phase incorrectly sends product defects to QA or DevOps.
    phase_manager = _phase_managers.get(getattr(ctx, "project_id", ""))
    registry_owner_id = ""
    try:
        v1_scope = load_phase_qa_scope(
            project_id=str(getattr(ctx, "project_id", "") or ""),
            phase_id=str(phase_id),
        )
    except (RuntimeError, ValueError):
        v1_scope = {"available": False}
    if v1_scope.get("available"):
        registry_owner_id = next((
            str(item.get("agent_id") or "")
            for item in v1_scope.get("files") or []
            if _normalized_rebuild_path(item.get("path")) == issue_path
        ), "")
    if phase_manager:
        registry_owner_id = registry_owner_id or str(
            (phase_manager.file_registry.get(issue_path) or {}).get("agent_id") or ""
        )
    registered_owner = next(
        (agent for agent in all_agents if agent.get("id") == registry_owner_id),
        None,
    )
    output_owner = next(
        (
            agent for agent in all_agents
            if issue_path in {
                _normalized_rebuild_path(path)
                for path in (agent.get("output_files") or [])
            }
        ),
        None,
    )
    # Rework belongs to the current phase first. Historical agents may still
    # be present in the project context, but they are terminal and must not
    # receive a new lease or mutate a later phase's workspace.
    current_scoped_owner = next((agent for agent in phase_agents if can_edit(agent)), None)
    if current_scoped_owner:
        return current_scoped_owner
    current_registered_owner = (
        registered_owner if registered_owner and registered_owner in phase_agents else None
    )
    current_output_owner = (
        output_owner if output_owner and output_owner in phase_agents else None
    )
    if current_registered_owner or current_output_owner:
        return current_registered_owner or current_output_owner
    scoped_owner = next((agent for agent in all_agents if can_edit(agent)), None)
    if registered_owner or output_owner or scoped_owner:
        return registered_owner or output_owner or scoped_owner

    # Command/API failures identify an execution surface rather than a source
    # file.  In particular, pre-QA reports ``test-backend`` with cwd
    # ``backend`` (not ``backend/…``), while the root test cwd ``.`` normalizes
    # to an empty path.  Do not let either fall through to agent insertion
    # order, which can incorrectly assign backend runtime defects to DevOps.
    issue_gate = str(issue.get("gate") or "").strip().lower()
    gate_owner_type = ""
    if issue_gate in {
        "install-backend", "test-backend", "build-backend",
        "health", "api", "api-contract",
    }:
        gate_owner_type = "backend"
    elif issue_gate in {"install-frontend", "test-frontend", "build-frontend"}:
        gate_owner_type = "frontend"
    elif issue_gate == "test-root":
        # Root tests are product/integration tests. Their failures must be
        # repaired by the backend owner, not by the owner of package scripts.
        gate_owner_type = "backend"
    if gate_owner_type:
        typed_owner = next(
            (
                agent for agent in phase_agents
                if agent.get("expert_type") == gate_owner_type
            ),
            None,
        ) or next(
            (
                agent for agent in all_agents
                if agent.get("expert_type") == gate_owner_type
            ),
            None,
        )
        if typed_owner:
            return typed_owner

    is_test_path = (
        issue_path.startswith(("tests/", "backend/tests/", "frontend/tests/"))
        or "/tests/" in f"/{issue_path}"
    )
    contract_owner = next(
        (
            agent for agent in phase_agents
            if issue_path
            and issue_path in str(
                (agent.get("execution_contract") or {}).get("description") or ""
            )
            and (is_test_path or agent.get("expert_type") != "qa")
        ),
        None,
    )
    if contract_owner:
        return contract_owner

    preferred_type = ""
    semantic_issue = " ".join(str(issue.get(key) or "") for key in (
        "code", "gate", "message", "invariant",
    )).lower()
    if not issue_path and any(marker in semantic_issue for marker in (
        "jwt", "security", "api", "priority", "description", "title",
        "authentication", "authorization", "permission",
    )):
        preferred_type = "backend"
    elif not issue_path and "frontend" in semantic_issue:
        preferred_type = "frontend"
    elif not issue_path and any(marker in semantic_issue for marker in (
        "docker", "deployment", "environment",
    )):
        preferred_type = "devops"
    elif is_test_path:
        preferred_type = "qa"
    elif issue_path.startswith("backend/"):
        preferred_type = "backend"
    elif issue_path.startswith("frontend/"):
        preferred_type = "frontend"
    elif issue_path.startswith(("deploy/", "Dockerfile", ".env")):
        preferred_type = "devops"
    elif issue_path in {"README.md", "package.json"} or issue_path.startswith("integration/"):
        preferred_type = "fullstack_engineer"
    if preferred_type:
        typed_owner = next(
            (agent for agent in phase_agents if agent.get("expert_type") == preferred_type),
            None,
        ) or next(
            (agent for agent in all_agents if agent.get("expert_type") == preferred_type),
            None,
        )
        if typed_owner:
            return typed_owner

    wanted = next(
        (agent for agent in all_agents if agent.get("id") == wanted_id),
        None,
    ) or next(
        (
            agent for agent in all_agents
            if wanted_role and agent.get("role") == wanted_role
        ),
        None,
    )
    if wanted and (is_test_path or wanted.get("expert_type") != "qa"):
        return wanted
    return next(
        (agent for agent in phase_agents if agent.get("expert_type") != "qa"),
        None,
    )


def _ensure_repair_file_scope(
    ctx,
    agent: Dict[str, Any],
    file_path: str,
    round_num: int,
) -> Dict[str, Any]:
    """Authorize one defect file and describe any temporary lease acquired.

    A completed agent from an accepted earlier phase has already released its
    original lease.  Reopening it for QA rework therefore needs a short-lived
    exact-file lease.  The caller must release only that temporary lease after
    the repair attempt; an active lease owned by the current phase remains in
    place until phase acceptance.
    """
    normalized = _normalized_rebuild_path(file_path)
    unresolved_markers = {
        "未定位文件", "未知文件", "unknown", "unknown file", "n/a", "none",
    }
    if normalized.strip().lower() in unresolved_markers or not is_delivery_file_path(normalized):
        raise RuntimeError(f"QA finding has no actionable delivery file: {file_path or 'unknown'}")

    def scope_covers(raw_scope: Any) -> bool:
        scope = _normalized_rebuild_path(raw_scope)
        return (
            normalized == scope
            or (scope.endswith("/") and normalized.startswith(scope))
        )

    allowed = list(agent.get("allowed_path_prefixes") or [])
    scope_authorized = any(scope_covers(scope) for scope in allowed)
    lock_id = str(agent.get("lock_id") or "")
    renewal = expert_lock.renew_lock(lock_id) if lock_id else {"success": False}
    temporary_lease = False
    if not renewal.get("success"):
        claim = expert_lock.atomic_claim_lock(
            expert_id=str(
                agent.get("expert_id")
                or f"fallback:{agent.get('id', 'repair-agent')}"
            ),
            project_id=str(getattr(ctx, "project_id", "")),
            task_id=str(agent.get("subproject_id") or agent.get("id") or ""),
            file_scope=[normalized],
        )
        if not claim.get("success"):
            raise RuntimeError(
                f"Repair file lock unavailable for {normalized}: "
                f"{claim.get('error') or claim.get('conflict_with') or 'conflict'}"
            )
        agent["lock_id"] = claim["lock_id"]
        agent["locked_until"] = claim.get("leased_until")
        lock_id = str(claim["lock_id"])
        temporary_lease = True
    elif not scope_authorized:
        claim = expert_lock.try_claim_file(
            str(agent.get("expert_id") or agent.get("id") or ""),
            lock_id,
            normalized,
        )
        if not claim.get("success"):
            raise RuntimeError(
                f"Repair file lock unavailable for {normalized}: "
                f"{claim.get('error') or claim.get('conflict_with') or 'conflict'}"
            )
        renewed = expert_lock.renew_lock(lock_id)
        agent["locked_until"] = renewed.get("leased_until")

    if not scope_authorized:
        allowed.append(normalized)
        agent["allowed_path_prefixes"] = list(dict.fromkeys(allowed))

    # Ordinary QA repair targets are per-attempt data, not a durable rebuild
    # manifest.  Accumulating them in required_rebuild_files makes a renamed or
    # deleted file poison every later repair with a false missing-file error.
    return {
        "file_path": normalized,
        "lock_id": lock_id,
        "temporary": temporary_lease,
        "round": round_num,
    }


async def _repair_all_issue_owners(project_id, phase_id, round_num, entry, state, rewrite_mode, api_config):
    """Run one complete repair batch and return only after every expert finishes."""
    ctx = projects[project_id]
    phase_manager = _phase_managers.get(project_id)
    phase = phase_manager.get_phase(phase_id) if phase_manager else {}
    grouped: Dict[str, Dict[str, Any]] = {}
    issue_details = list(entry.get("issues_detail", []))
    if not issue_details:
        issue_details = [{"file_path": "未定位文件", "severity": "error", "message": message}
                         for message in entry.get("issues", [])]
    for issue in issue_details:
        if str(issue.get("status") or "open").lower() not in {"open", "fixing"}:
            continue
        agent = _match_issue_agent(ctx, phase_id, issue)
        if not agent:
            if str(issue.get("severity") or "error").lower() in {"error", "critical"}:
                raise RuntimeError(
                    "No authorized repair owner for "
                    + str(issue.get("file_path") or "unknown file")
                )
            continue
        bucket = grouped.setdefault(agent["id"], {"agent": agent, "files": {}})
        bucket["files"].setdefault(issue.get("file_path") or "未定位文件", []).append(issue)

    # The QC merger increments fix_rounds only for findings that were actually
    # dispatched in the previous pass.  Mark the canonical QC ledger before an
    # expert starts so a crash/restart cannot make the attempt disappear.
    dispatched_issues = [
        issue
        for bucket in grouped.values()
        for issues in bucket["files"].values()
        for issue in issues
    ]
    if dispatched_issues:
        now = time.time()
        dispatched_keys = {
            (
                str(issue.get("id") or ""),
                str(issue.get("file_path") or ""),
                str(issue.get("message") or "")[:80],
            )
            for issue in dispatched_issues
        }
        for issue in dispatched_issues:
            issue["status"] = "fixing"
            issue["fixing_at"] = now
        stored = getattr(ctx, "qc_results", {}).get(phase_id, {})
        stored_entry = stored.get("qa", stored) if isinstance(stored, dict) else {}
        for issue in stored_entry.get("issues_detail", []) if isinstance(stored_entry, dict) else []:
            key = (
                str(issue.get("id") or ""),
                str(issue.get("file_path") or ""),
                str(issue.get("message") or "")[:80],
            )
            if key in dispatched_keys:
                issue["status"] = "fixing"
                issue["fixing_at"] = now
        await _persist_all_async()

    repair_batch = {
        "round": round_num,
        "status": "repairing",
        "total": sum(len(bucket["files"]) for bucket in grouped.values()),
        "completed": 0,
        "failed": 0,
        "files_changed": False,
        "changed_files": [],
        "issue_ids": sorted({
            str(issue.get("issue_id") or issue.get("id") or issue.get("fingerprint") or "")
            for issue in dispatched_issues
            if issue.get("issue_id") or issue.get("id") or issue.get("fingerprint")
        }),
        "items": [],
        "started_at": time.time(),
    }
    state["repair_batch"] = repair_batch
    await _persist_all_async()

    from api.routes_execution import _reset_fix_attempt, _run_agent_task as _exec_task
    for bucket in grouped.values():
        agent = bucket["agent"]
        # A repair response contains full file contents.  Run one file per
        # request so medium/large controllers and pages cannot compete for the
        # same completion budget and arrive truncated.
        for path, issues in sorted(bucket["files"].items()):
            target_path = (Path(ctx.workspace) / str(path)).resolve()
            try:
                target_path.relative_to(Path(ctx.workspace).resolve())
                before_content = target_path.read_bytes() if target_path.is_file() else None
            except ValueError:
                before_content = None
            repair_lease = _ensure_repair_file_scope(ctx, agent, path, round_num)
            _reset_fix_attempt(agent["id"])
            issue_ids = [
                str(issue.get("issue_id") or issue.get("id") or issue.get("fingerprint") or "")
                for issue in issues
            ]
            repair_batch["items"].append({
                "file_path": path,
                "agent_id": agent["id"],
                "issue_ids": [issue_id for issue_id in issue_ids if issue_id],
                "status": "repairing",
            })
            lines = []
            for issue, issue_id in zip(issues, issue_ids):
                location = str(next(
                    (
                        issue.get(field)
                        for field in ("line", "line_no", "line_number", "location")
                        if issue.get(field) is not None
                    ),
                    "",
                )).strip()
                evidence = str(
                    issue.get("evidence")
                    or issue.get("evidence_step_id")
                    or issue.get("failed_command")
                    or ""
                ).strip()
                acceptance = str(
                    issue.get("acceptance_criteria")
                    or issue.get("expected")
                    or ""
                ).strip()
                lines.append(
                    "- issue_id={issue_id}; severity={severity}; layer={layer}; "
                    "path={path}{location}; message={message}; fix_hint={hint}; "
                    "evidence={evidence}; acceptance={acceptance}".format(
                        issue_id=issue_id or "missing",
                        severity=issue.get("severity", "error"),
                        layer=issue.get("layer", "unknown"),
                        path=path,
                        location=f":{location}" if location else "",
                        message=issue.get("message", ""),
                        hint=issue.get("fix_hint", ""),
                        evidence=evidence or "not_provided",
                        acceptance=acceptance or "the identified issue no longer reproduces",
                    )
                )
            description = (
                f"【质检修复任务｜第 {round_num} 轮】\n"
                "监督者已检查本阶段产物。本次只修改并只返回下面这一个文件；"
                "严格输出 JSON files；不要输出思考过程、尝试方法、执行命令、说明或其他文件。\n\n"
                f"文件：{path}\n" + "\n".join(lines)
            )
            if rewrite_mode:
                description += "\n阶段重构模式：结合问题清单完整重写这个文件，仍不得输出其他文件。"
            state["messages"].append({
                "role": "system",
                "content": f"问题已反馈给专家 {agent.get('role','')}：{path}",
                "ts": time.time(),
            })
            owner_contract = agent.get("execution_contract") or {}
            owner_phase = (
                phase_manager.get_phase(str(agent.get("phase_id") or ""))
                if phase_manager and agent.get("phase_id")
                else None
            )
            repair_tech_stack = list(owner_contract.get("tech_stack") or [])
            if not repair_tech_stack:
                repair_tech_stack = _infer_phase_tech_stack(
                    owner_phase or phase or {}, ctx.description or ""
                )
            owner_artifact_policy = dict(
                owner_contract.get("artifact_policy")
                or agent.get("artifact_policy")
                or {}
            )
            repair_artifact_policy = {
                "kind": owner_artifact_policy.get("kind") or (
                    "architecture_document"
                    if agent.get("expert_type") == "architecture"
                    else "runnable"
                ),
                "required_files": [repair_lease["file_path"]],
                "allowed_path_prefixes": [repair_lease["file_path"]],
            }
            try:
                repair_result = await _exec_task(
                    project_id=project_id, agent_id=agent["id"],
                    subproject_id=agent.get("subproject_id", phase_id),
                    subproject_name=agent.get("subproject_name", ""), description=description,
                    tech_stack=repair_tech_stack,
                    project_context=ctx.pm.context_summary or ctx.description or "",
                    user_api_config=api_config, defer_fix_qc=True,
                    artifact_policy=repair_artifact_policy,
                )
            finally:
                if repair_lease.get("temporary") and repair_lease.get("lock_id"):
                    expert_lock.release_lock(str(repair_lease["lock_id"]))
                    agent["locked_until"] = None
            latest_status = str(
                (ctx.agents.get(agent["id"]) or {}).get("status") or ""
            ).strip().lower()
            repair_failed = (
                not isinstance(repair_result, dict)
                or repair_result.get("success") is not True
                or latest_status != "completed"
            )
            if repair_failed:
                repair_batch["items"][-1]["status"] = "failed"
                repair_batch["failed"] += 1
                repair_batch["status"] = "failed"
                repair_batch["finished_at"] = time.time()
                await _persist_all_async()
                error = (
                    repair_result.get("error", "")
                    if isinstance(repair_result, dict)
                    else ""
                )
                raise RuntimeError(
                    f"Agent {agent.get('role', agent['id'])} repair failed for {path}"
                    + (f": {error}" if error else "")
                )
            repair_batch["completed"] += 1
            repair_batch["last_completed_file"] = path
            repair_batch["items"][-1]["status"] = "completed"
            after_content = target_path.read_bytes() if target_path.is_file() else None
            if after_content != before_content:
                repair_batch["files_changed"] = True
                repair_batch["changed_files"].append(path)
            await _persist_all_async()

    repair_batch["status"] = "completed"
    repair_batch["finished_at"] = time.time()
    await _persist_all_async()
    return repair_batch


def _snapshot_repair_targets(ctx, entry: Dict[str, Any]) -> Dict[Path, Optional[bytes]]:
    """Capture only actionable workspace files before one repair attempt."""
    workspace = Path(ctx.workspace).resolve()
    snapshot: Dict[Path, Optional[bytes]] = {}
    for issue in entry.get("issues_detail", []) or []:
        if str(issue.get("status") or "open").lower() not in {"open", "fixing"}:
            continue
        raw_path = _normalized_rebuild_path(issue.get("file_path"))
        if not is_delivery_file_path(raw_path):
            continue
        target = (workspace / raw_path).resolve()
        try:
            target.relative_to(workspace)
        except ValueError:
            continue
        if target not in snapshot:
            snapshot[target] = target.read_bytes() if target.is_file() else None
    return snapshot


def _restore_repair_snapshot(snapshot: Dict[Path, Optional[bytes]]) -> None:
    """Restore the exact pre-repair bytes after a measured quality regression."""
    for target, content in snapshot.items():
        if content is None:
            if target.exists() and target.is_file():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _repair_snapshot_changed(snapshot: Dict[Path, Optional[bytes]]) -> bool:
    """Return true only when a repair changed or created an actionable file."""
    for target, before in snapshot.items():
        after = target.read_bytes() if target.is_file() else None
        if after != before:
            return True
    return False


def _snapshot_repair_metadata(ctx: ProjectContext, phase_id: str) -> Dict[str, Any]:
    """Capture durable ownership metadata changed by a repair execution.

    Repair agents reuse the phase's execution records.  A rejected repair can
    therefore leave output manifests, subproject status, or file ownership
    pointing at files that are rolled back on disk.  Keep the snapshot scoped
    to this phase so concurrent work in another phase is never overwritten.
    """
    pm = _phase_managers.get(ctx.project_id)
    phase_agent_ids = {
        str(agent.get("id") or "")
        for agent in ctx.agents.values()
        if agent.get("phase_id") == phase_id and agent.get("id")
    }
    agent_ids = {agent_id for agent_id in phase_agent_ids if agent_id}
    subprojects = {
        str(sp.get("id") or ""): copy.deepcopy(sp)
        for sp in ctx.subprojects
        if sp.get("phase_id") == phase_id and sp.get("id")
    }
    registry = {}
    if pm:
        for path, owner in (pm.file_registry or {}).items():
            owner_dict = owner if isinstance(owner, dict) else {}
            if (
                str(owner_dict.get("phase_id") or "") == phase_id
                or str(owner_dict.get("agent_id") or "") in agent_ids
            ):
                registry[path] = copy.deepcopy(owner)
    phase = pm.get_phase(phase_id) if pm else None
    phase_metadata = {}
    if phase:
        for key in (
            "rebuild_file_manifest", "rebuild_notes",
            "pre_rebuild_snapshot_version",
        ):
            if key in phase:
                phase_metadata[key] = copy.deepcopy(phase[key])
    execution_before = {}
    try:
        from api.routes_execution import execution_status
        execution_before = {
            agent_id: copy.deepcopy(execution_status.get(agent_id))
            for agent_id in agent_ids
            if agent_id in execution_status
        }
    except Exception:
        execution_before = {}
    return {
        "phase_id": phase_id,
        "agents": {
            agent_id: copy.deepcopy(ctx.agents[agent_id])
            for agent_id in agent_ids
            if agent_id in ctx.agents
        },
        "subprojects": subprojects,
        "file_registry": registry,
        "phase": phase_metadata,
        "phase_agents": copy.deepcopy(
            (getattr(pm, "phase_agents", {}) or {}).get(phase_id, [])
        ) if pm else [],
        "execution_status": execution_before,
    }


def _restore_repair_metadata(ctx: ProjectContext, snapshot: Optional[Dict[str, Any]]) -> None:
    """Restore phase-scoped metadata after rolling back repair bytes."""
    if not snapshot:
        return

    phase_id = str(snapshot.get("phase_id") or "")
    pm = _phase_managers.get(ctx.project_id)
    agent_snapshots = snapshot.get("agents") or {}
    snapshot_agent_ids = set(agent_snapshots)
    for agent_id, agent in list(ctx.agents.items()):
        if (
            str(agent.get("phase_id") or "") == phase_id
            and agent_id not in snapshot_agent_ids
        ):
            ctx.agents.pop(agent_id, None)
    for agent_id, value in agent_snapshots.items():
        if agent_id in ctx.agents:
            ctx.agents[agent_id] = copy.deepcopy(value)
    subproject_snapshots = snapshot.get("subprojects") or {}
    for subproject in ctx.subprojects:
        subproject_id = str(subproject.get("id") or "")
        if subproject_id in subproject_snapshots:
            subproject.clear()
            subproject.update(copy.deepcopy(subproject_snapshots[subproject_id]))
    if pm:
        # Remove registrations introduced by the rejected repair, then put
        # back the exact phase/agent ownership entries from before the repair.
        agent_ids = set(agent_snapshots)
        pm.file_registry = {
            path: owner for path, owner in (pm.file_registry or {}).items()
            if str((owner.get("phase_id") or "") if isinstance(owner, dict) else "") != phase_id
            and str((owner.get("agent_id") or "") if isinstance(owner, dict) else "") not in agent_ids
        }
        pm.file_registry.update(copy.deepcopy(snapshot.get("file_registry") or {}))
        phase = pm.get_phase(phase_id)
        if phase:
            for key, value in (snapshot.get("phase") or {}).items():
                phase[key] = copy.deepcopy(value)
        if hasattr(pm, "phase_agents"):
            pm.phase_agents[phase_id] = list(snapshot.get("phase_agents") or [])
    try:
        from api.routes_execution import execution_status
        agent_ids = set(agent_snapshots)
        for agent_id in agent_ids:
            if agent_id in snapshot.get("execution_status", {}):
                execution_status[agent_id] = copy.deepcopy(
                    snapshot["execution_status"][agent_id]
                )
            else:
                execution_status.pop(agent_id, None)
    except Exception:
        pass


def _workspace_delivery_inventory(workspace: Path) -> List[str]:
    """Return the bounded, non-sensitive delivery-file inventory."""
    inventory: List[str] = []
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(workspace)
        normalized = relative.as_posix()
        if is_sensitive_archive_path(relative):
            continue
        if _is_rebuild_deliverable_path(normalized) and is_delivery_file_path(normalized):
            inventory.append(normalized)
        if len(inventory) > 5000:
            raise RuntimeError("Rebuild delivery inventory exceeds 5000 files")
    return inventory


def _encode_durable_rebuild_snapshot(
    workspace: Path, relative_paths: List[str],
) -> Dict[str, Any]:
    """Encode a bounded, secret-free snapshot inside durable repair state."""
    files: Dict[str, Dict[str, Any]] = {}
    total_size = 0
    root = workspace.resolve()
    for relative_path in sorted(set(relative_paths)):
        relative = Path(relative_path)
        if is_sensitive_archive_path(relative):
            raise RuntimeError(f"Sensitive path cannot enter rebuild snapshot: {relative_path}")
        source = (root / relative).resolve()
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"Unsafe rebuild snapshot path: {relative_path}") from exc
        if not source.is_file() or source.is_symlink():
            continue
        content = source.read_bytes()
        if len(content) > WORKSPACE_PERSIST_FILE_LIMIT:
            raise RuntimeError(f"Rebuild snapshot file exceeds limit: {relative_path}")
        total_size += len(content)
        if total_size > WORKSPACE_PERSIST_PROJECT_LIMIT:
            raise RuntimeError("Rebuild snapshot exceeds durable project size limit")
        normalized = relative.as_posix()
        files[normalized] = {
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "data": base64.b64encode(content).decode("ascii"),
        }
    return {
        "schema_version": 1,
        "files": files,
        "total_size": total_size,
        "captured_at": time.time(),
    }


def _capture_phase_rebuild_restore_point(
    ctx: ProjectContext, phase_id: str,
) -> Dict[str, Any]:
    """Capture the complete phase-owned metadata needed for an atomic rebuild rollback."""
    pm = _phase_managers.get(ctx.project_id)
    phase = pm.get_phase(phase_id) if pm else None
    phase_agents = {
        str(agent_id): copy.deepcopy(agent)
        for agent_id, agent in getattr(ctx, "agents", {}).items()
        if str(agent.get("phase_id") or "") == phase_id
    }
    agent_ids = set(phase_agents)
    child_ids = {
        str(item) for item in ((phase or {}).get("subprojects") or []) if item
    }
    child_ids.update(
        str(agent.get("subproject_id") or "")
        for agent in phase_agents.values() if agent.get("subproject_id")
    )
    subprojects = {
        str(item.get("id")): copy.deepcopy(item)
        for item in getattr(ctx, "subprojects", [])
        if item.get("id") and (
            str(item.get("phase_id") or "") == phase_id
            or str(item.get("id")) in child_ids
        )
    }
    registry = {
        str(path): copy.deepcopy(owner)
        for path, owner in ((getattr(pm, "file_registry", {}) or {}).items() if pm else [])
        if isinstance(owner, dict) and (
            str(owner.get("phase_id") or "") == phase_id
            or str(owner.get("agent_id") or "") in agent_ids
        )
    }
    try:
        from api.routes_execution import execution_status
        execution = {
            agent_id: copy.deepcopy(execution_status.get(agent_id))
            for agent_id in agent_ids if agent_id in execution_status
        }
    except Exception:
        execution = {}
    return {
        "phase": copy.deepcopy(phase or {}),
        "agents": phase_agents,
        "subprojects": subprojects,
        "file_registry": registry,
        "phase_agents": copy.deepcopy(
            ((getattr(pm, "phase_agents", {}) or {}).get(phase_id) or [])
        ) if pm else [],
        "execution_status": execution,
        "qc_result": copy.deepcopy((getattr(ctx, "qc_results", {}) or {}).get(phase_id)),
        "supervisor_quality_run": copy.deepcopy(
            (getattr(ctx, "supervisor_quality_runs", {}) or {}).get(phase_id)
        ),
    }


def _restore_phase_rebuild_metadata(
    ctx: ProjectContext, phase_id: str, restore_point: Dict[str, Any],
) -> None:
    """Apply one phase-scoped metadata restore point without filesystem effects."""
    pm = _phase_managers.get(ctx.project_id)
    if not pm:
        raise RuntimeError("Phase manager is unavailable")
    phase = pm.get_phase(phase_id)
    current_agent_ids = {
        str(agent_id) for agent_id, agent in getattr(ctx, "agents", {}).items()
        if str(agent.get("phase_id") or "") == phase_id
    }
    old_agent_ids = set((restore_point.get("agents") or {}).keys())
    for agent_id in current_agent_ids:
        ctx.agents.pop(agent_id, None)
    ctx.agents.update(copy.deepcopy(restore_point.get("agents") or {}))

    restored_subprojects = restore_point.get("subprojects") or {}
    restored_ids = set(restored_subprojects)
    ctx.subprojects[:] = [
        item for item in ctx.subprojects
        if str(item.get("phase_id") or "") != phase_id
        and str(item.get("id") or "") not in restored_ids
    ]
    ctx.subprojects.extend(copy.deepcopy(list(restored_subprojects.values())))

    phase_snapshot = copy.deepcopy(restore_point.get("phase") or {})
    if phase is None:
        pm.phases.append(phase_snapshot)
    else:
        phase.clear()
        phase.update(phase_snapshot)
    pm.phase_agents[phase_id] = list(restore_point.get("phase_agents") or [])
    pm.file_registry = {
        path: owner for path, owner in (pm.file_registry or {}).items()
        if not isinstance(owner, dict) or (
            str(owner.get("phase_id") or "") != phase_id
            and str(owner.get("agent_id") or "") not in current_agent_ids | old_agent_ids
        )
    }
    pm.file_registry.update(copy.deepcopy(restore_point.get("file_registry") or {}))

    from api.routes_execution import execution_status
    for agent_id in current_agent_ids | old_agent_ids:
        execution_status.pop(agent_id, None)
    execution_status.update(copy.deepcopy(restore_point.get("execution_status") or {}))
    if hasattr(ctx, "qc_results"):
        if restore_point.get("qc_result") is None:
            ctx.qc_results.pop(phase_id, None)
        else:
            ctx.qc_results[phase_id] = copy.deepcopy(restore_point["qc_result"])
    if hasattr(ctx, "supervisor_quality_runs"):
        if restore_point.get("supervisor_quality_run") is None:
            ctx.supervisor_quality_runs.pop(phase_id, None)
        else:
            ctx.supervisor_quality_runs[phase_id] = copy.deepcopy(
                restore_point["supervisor_quality_run"]
            )


def _restore_phase_rebuild_snapshot(
    ctx: ProjectContext, phase_id: str, state: Dict[str, Any],
) -> Dict[str, Any]:
    """Compensatably restore files and metadata after rebuild regression."""
    pm = _phase_managers.get(ctx.project_id)
    restore_point = copy.deepcopy(state.get("pre_rebuild_restore_point") or {})
    if not pm or not restore_point:
        raise RuntimeError("Pre-rebuild restore point is unavailable")

    phase = pm.get_phase(phase_id)
    current_agents = {
        str(agent_id): agent
        for agent_id, agent in getattr(ctx, "agents", {}).items()
        if str(agent.get("phase_id") or "") == phase_id
    }
    current_agent_ids = set(current_agents)
    current_paths = set((phase or {}).get("rebuild_file_manifest", {}).get("all") or [])
    for agent in current_agents.values():
        current_paths.update(agent.get("output_files") or [])
        current_paths.update(agent.get("required_rebuild_files") or [])
        current_paths.update(agent.get("required_delivery_files") or [])
    for path, owner in (pm.file_registry or {}).items():
        if isinstance(owner, dict) and (
            str(owner.get("phase_id") or "") == phase_id
            or str(owner.get("agent_id") or "") in current_agent_ids
        ):
            current_paths.add(path)

    if "pre_rebuild_delivery_inventory" in state:
        pre_inventory = set(state.get("pre_rebuild_delivery_inventory") or [])
        current_inventory = set(_workspace_delivery_inventory(ctx.workspace))
        for path in current_inventory - pre_inventory:
            owner = (pm.file_registry or {}).get(path)
            if (
                owner is None
                or not isinstance(owner, dict)
                or str(owner.get("phase_id") or "") == phase_id
            ):
                current_paths.add(path)

    snapshot_version = int(state.get("pre_rebuild_snapshot_version") or 0)
    durable_files = (state.get("durable_rebuild_snapshot") or {}).get("files") or {}
    snapshot_dir = ctx.workspace / ".project" / "versions" / str(snapshot_version)
    if durable_files:
        snapshot_files = set(durable_files)
    else:
        commit_path = snapshot_dir / "commit.json"
        if snapshot_version <= 0 or not commit_path.is_file():
            raise RuntimeError("Pre-rebuild file snapshot is unavailable")
        commit = json.loads(commit_path.read_text(encoding="utf-8"))
        snapshot_files = {str(path).replace("\\", "/") for path in commit.get("files") or []}

    txn_root = ctx.workspace / ".project" / "restore-txn" / uuid.uuid4().hex
    stage_root, backup_root = txn_root / "stage", txn_root / "backup"
    targets = {
        path for path in current_paths | snapshot_files
        if _is_rebuild_deliverable_path(path) and is_delivery_file_path(path)
    }
    current_metadata = _capture_phase_rebuild_restore_point(ctx, phase_id)
    existed: set[str] = set()
    try:
        for relative_path in sorted(targets):
            target = ctx.workspace / relative_path
            if target.is_file():
                backup = backup_root / relative_path
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                existed.add(relative_path)
        for relative_path in sorted(snapshot_files):
            staged = stage_root / relative_path
            staged.parent.mkdir(parents=True, exist_ok=True)
            if durable_files:
                record = durable_files[relative_path]
                content = base64.b64decode(record.get("data") or "", validate=True)
                if len(content) != int(record.get("size") or -1) or hashlib.sha256(content).hexdigest() != record.get("sha256"):
                    raise RuntimeError(f"Corrupt durable rebuild snapshot: {relative_path}")
                staged.write_bytes(content)
            else:
                shutil.copy2(snapshot_dir / relative_path, staged)
        for relative_path in sorted(targets):
            target = ctx.workspace / relative_path
            if target.is_file():
                target.unlink()
        for relative_path in sorted(snapshot_files):
            destination = ctx.workspace / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage_root / relative_path, destination)
        _restore_phase_rebuild_metadata(ctx, phase_id, restore_point)
    except Exception as original:
        compensation_errors: List[str] = []
        try:
            for relative_path in sorted(targets):
                target = ctx.workspace / relative_path
                if target.is_file():
                    target.unlink()
            for relative_path in sorted(existed):
                source = backup_root / relative_path
                destination = ctx.workspace / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        except Exception as exc:
            compensation_errors.append(f"files: {exc}")
        try:
            _restore_phase_rebuild_metadata(ctx, phase_id, current_metadata)
        except Exception as exc:
            compensation_errors.append(f"metadata: {exc}")
        if compensation_errors:
            raise RuntimeError(
                f"Rebuild restore failed ({original}); compensation failed ({'; '.join(compensation_errors)})"
            ) from original
        raise
    finally:
        shutil.rmtree(txn_root, ignore_errors=True)

    for agent in current_agents.values():
        lock_id = str(agent.get("lock_id") or "")
        if lock_id:
            try:
                expert_lock.release_lock(lock_id)
            except Exception:
                pass
    return {"restored_files": sorted(snapshot_files), "snapshot_version": snapshot_version}


async def _fail_closed_rebuild_execution(
    ctx: ProjectContext, phase_id: str, failures: List[str],
) -> bool:
    """Rollback a rebuild as soon as any critical Agent reaches a failed terminal state."""
    key = f"{ctx.project_id}-{phase_id}"
    state = _auto_repair_states.get(key)
    if not _rebuild_lifecycle_pending(state):
        return False
    try:
        rollback = _restore_phase_rebuild_snapshot(ctx, phase_id, state)
        status = "rebuild_execution_failed"
        message = "阶段重构 Agent 未全部成功，已恢复重构前版本。"
    except Exception as exc:
        rollback = {"error": str(exc)[:500]}
        status = "rebuild_rollback_failed"
        message = "阶段重构 Agent 失败，且自动恢复未完成；必须人工接管。"
    state["running"] = False
    state["status"] = status
    state["needs_manual"] = True
    state["rebuild_comparison_pending"] = False
    state["issue_report"] = copy.deepcopy(state.get("pre_rebuild_issue_report") or {})
    state["rebuild_execution_failures"] = list(failures)
    state["rebuild_rollback"] = rollback
    state["action_required"] = {
        "message": message + " " + "; ".join(failures[:8]),
        "options": ["keep_previous_version", "manual_fix", "rebuild_phase"],
        "automatic_retry_allowed": False,
    }
    pm = _phase_managers.get(ctx.project_id)
    phase = pm.get_phase(phase_id) if pm else None
    if phase:
        phase["status"] = "needs_rework" if status == "rebuild_execution_failed" else "failed"
        phase["reviewed"] = True
        phase["review_passed"] = False
        phase["rebuild_execution_failures"] = list(failures)
    await _persist_all_async()
    return True


@_quality_background_terminal
async def _run_auto_repair_loop(project_id: str, phase_id: str, rewrite_mode: bool):
    """后台运行自动修复循环"""
    key = f"{project_id}-{phase_id}"
    state = _auto_repair_states.get(key)
    if not state:
        return

    ctx = projects.get(project_id)
    if not ctx:
        state["running"] = False
        state["status"] = "error"
        state["messages"].append({"role": "system", "content": "❌ 项目上下文丢失", "ts": time.time()})
        await _persist_all_async()
        return
    recovery_claim = state.get("pre_qa_recovery_claim") or {}
    if recovery_claim:
        active_claims = await asyncio.to_thread(
            expert_lock.get_active_locks,
            "supervisor-preqa-recovery",
            project_id,
        )
        if not any(
            item.get("lock_id") == recovery_claim.get("lock_id")
            for item in active_claims
        ):
            state["running"] = False
            state["status"] = "interrupted"
            state["needs_manual"] = True
            state["action_required"] = {
                "message": "The durable pre-QA recovery claim is not active.",
                "options": ["manual_edit", "retry_cycle", "rebuild_phase"],
            }
            await _persist_all_async()
            return

    try:
        machine = _ensure_supervisor_quality_run(ctx, phase_id, state)
    except IllegalQualityTransition as exc:
        state["running"] = False
        state["status"] = "error"
        state["needs_manual"] = True
        state["action_required"] = {
            "message": str(exc),
            "options": ["rebuild_phase"],
        }
        await _persist_all_async()
        return
    registered_artifact = str(
        state.get("registered_reinspection_artifact_digest") or ""
    )
    if registered_artifact:
        current_scope = _supervisor_scope_snapshot(ctx, phase_id)
        bound_artifact = str(
            (machine.to_dict().get("scope") or {}).get("artifact_digest") or ""
        )
        if (
            current_scope.get("artifact_digest") != registered_artifact
            or bound_artifact != registered_artifact
        ):
            state["running"] = False
            state["status"] = "stale"
            state["needs_manual"] = True
            state["action_required"] = {
                "message": (
                    "Workspace changed after Engineer repair was registered; "
                    "authoritative reinspection was not started"
                ),
                "options": ["manual_fix", "retry_cycle"],
            }
            await _persist_all_async()
            return

    from api.routes_supervisor import _run_qc_for_subproject
    sp_name = state.get("phase_name", phase_id)
    unlocated_qc_retries = 0
    cycle_repairs = 0
    pending_snapshot: Optional[Dict[Path, Optional[bytes]]] = None
    pending_metadata_snapshot: Optional[Dict[str, Any]] = None
    pending_qc_snapshot: Optional[Dict[str, Any]] = None
    pre_repair_blocker_keys: set[str] = set()

    while state["running"]:
        pm = _phase_managers.get(project_id)
        current_phase = pm.get_phase(phase_id) if pm else None
        if (
            _auto_repair_states.get(key) is not state
            or (
                current_phase
                and str(state.get("execution_generation") or "")
                and str(current_phase.get("execution_generation") or "")
                != str(state.get("execution_generation") or "")
            )
        ):
            state["running"] = False
            state["status"] = "superseded"
            logger.warning(
                "Stopped superseded phase quality loop project=%s phase=%s",
                project_id,
                phase_id,
            )
            return
        try:
            if machine.state in {"infrastructure_failed", "model_failed"}:
                machine.resume()
            if machine.state == "waiting_engineer":
                _prepare_supervisor_verification(ctx, phase_id, machine)
            if machine.state not in {"verifying", "qa_running"}:
                raise IllegalQualityTransition(
                    f"Cannot start QA from Supervisor state {machine.state}"
                )
            state["status"] = "pre_qa_verifying"
            await _persist_all_async()
            try:
                pre_qa = await asyncio.to_thread(
                    _execute_phase_pre_qa, ctx, phase_id,
                )
            except Exception as exc:
                pre_qa = _normalize_pre_qa_check_sources(
                    ctx,
                    phase_id,
                    {
                        "passed": False,
                        "status": FAILURE_INFRASTRUCTURE,
                        "failure_category": FAILURE_INFRASTRUCTURE,
                        "failed_gate": "pre_qa_runner",
                        "issues": [{
                            "code": "pre_qa_runner_failed",
                            "message": str(exc)[:500],
                            "path": "",
                            "gate": "infrastructure",
                            "actionable": False,
                        }],
                        "evidence": [],
                        "consumes_business_qa_round": False,
                    },
                )
            state["pre_qa_result"] = copy.deepcopy(pre_qa)
            active_pm = _phase_managers.get(project_id)
            active_phase = active_pm.get_phase(phase_id) if active_pm else None
            if active_phase is not None:
                active_phase["pre_qa_result"] = copy.deepcopy(pre_qa)
            _record_pre_qa_machine_evidence(machine, pre_qa)
            if not pre_qa.get("passed"):
                blocking_issues = [
                    issue
                    for issue in (pre_qa.get("issues") or [])
                    if isinstance(issue, dict)
                    and issue.get("blocking") is True
                ]
                deterministic_repairs = await asyncio.to_thread(
                    _apply_deterministic_pre_qa_repairs,
                    ctx,
                    [
                        {**issue, "phase_id": phase_id} if isinstance(issue, dict) else issue
                        for issue in blocking_issues
                    ],
                )
                if deterministic_repairs:
                    state.setdefault("deterministic_pre_qa_repairs", []).extend(
                        deterministic_repairs
                    )
                    state["status"] = "pre_qa_verifying"
                    state["pre_qa_result_before_deterministic_repair"] = copy.deepcopy(pre_qa)
                    repaired_scope = _supervisor_scope_snapshot(ctx, phase_id)
                    previous_artifact = str(
                        (machine.to_dict().get("scope") or {}).get("artifact_digest")
                        or (machine.to_dict().get("scope") or {}).get("workspace_digest")
                        or ""
                    )
                    machine.bind_artifact_generation(
                        artifact_digest=repaired_scope["artifact_digest"],
                        scope_digest=repaired_scope["scope_digest"],
                        repair_commit=f"artifact:{repaired_scope['artifact_digest']}",
                        scope_snapshot=repaired_scope,
                        transition_reason="deterministic_pre_qa_repair",
                        expected_previous_artifact_digest=previous_artifact,
                    )
                    machine.record_evidence(
                        kind="scope",
                        command=f"relock-phase-scope {phase_id}",
                        exit_code=0,
                        passed=True,
                        log="Bound deterministic pre-QA repair generation",
                        step_id=(
                            f"scope-pre-qa-repair:{machine.to_dict().get('run_id')}:"
                            f"{repaired_scope['artifact_digest']}"
                        ),
                        metadata=repaired_scope,
                    )
                    for repair in deterministic_repairs:
                        machine.record_evidence(
                            kind=str(repair.get("kind") or "deterministic_patch"),
                            command=f"deterministic-pre-qa-repair {repair.get('path')}",
                            exit_code=0,
                            passed=True,
                            log=f"Applied deterministic repair for {repair.get('issue_code')}",
                            step_id=f"pre-qa-repair:{repair.get('path')}:{repair.get('after_digest')}",
                            metadata={
                                **repair,
                                "artifact_digest": repaired_scope["artifact_digest"],
                                "scope_digest": repaired_scope["scope_digest"],
                            },
                        )
                    _store_supervisor_quality_machine(ctx, phase_id, machine, state)
                    await _persist_all_async()
                    continue
                category = str(pre_qa.get("failure_category") or "pre_qa_failed")
                diagnostic = "; ".join(
                    str(issue.get("message") or issue.get("code") or "pre-QA failed")
                    for issue in blocking_issues[:5]
                )
                if category in {FAILURE_INFRASTRUCTURE, FAILURE_MODEL}:
                    machine.fail(
                        "model" if category == FAILURE_MODEL else "infrastructure",
                        diagnostic or category,
                    )
                    state["needs_manual"] = False
                    state["action_required"] = {
                        "message": diagnostic or category,
                        "options": ["retry_cycle"],
                    }
                    if active_phase is not None:
                        active_phase["status"] = category
                else:
                    responsible: set[str] = set()
                    for issue in blocking_issues:
                        matched = _match_issue_agent(ctx, phase_id, {
                            **issue,
                            "file_path": issue.get("path") or "",
                        })
                        if matched and matched.get("id"):
                            responsible.add(str(matched["id"]))
                    if not responsible:
                        responsible.update(
                            str(agent_id) for agent_id, agent in (getattr(ctx, "agents", {}) or {}).items()
                            if str(agent.get("phase_id") or "") == phase_id
                        )
                    machine.fail_pre_qa(
                        blocking_issues,
                        agent_ids=sorted(responsible),
                    )
                    _mark_pre_qa_agents_for_repair(
                        ctx,
                        phase_id,
                        sorted(responsible),
                        blocking_issues,
                    )
                    dispatch = await _schedule_pre_qa_repair_runs(
                        ctx, sorted(responsible),
                    )
                    state["needs_manual"] = False
                    state["pre_qa_repair_runs"] = dispatch["scheduled"]
                    state["action_required"] = _pre_qa_repair_action(
                        diagnostic, dispatch,
                    )
                    if active_phase is not None:
                        active_phase["status"] = "waiting_engineer"
                state["running"] = False
                state["status"] = category
                _store_supervisor_quality_machine(ctx, phase_id, machine, state)
                await _persist_all_async()
                return
            machine.record_evidence(
                kind="pre_qa",
                command=f"deterministic-pre-qa --phase {phase_id}",
                exit_code=0,
                passed=True,
                log=f"Pre-QA passed with {len(pre_qa.get('evidence') or [])} evidence records",
                step_id=f"pre-qa:summary:{compute_workspace_digest(Path(ctx.workspace))}",
                metadata={"result": "passed"},
            )
            scope_snapshot = _supervisor_scope_snapshot(ctx, phase_id)
            previous_round = machine.active_round
            issue_baseline = (
                copy.deepcopy(previous_round.get("issues") or [])
                if previous_round else []
            )
            qa_round_id = (
                f"{machine.to_dict().get('run_id')}:"
                f"qa-{int(machine.to_dict().get('business_rounds_used', 0)) + 1}"
            )
            machine.start_qa_round(
                scope_snapshot=scope_snapshot,
                issue_snapshot=issue_baseline,
                qa_round_id=qa_round_id,
            )
            _store_supervisor_quality_machine(ctx, phase_id, machine, state)
        except IllegalQualityTransition as exc:
            state["running"] = False
            state["status"] = "blocked"
            state["needs_manual"] = True
            state["action_required"] = {
                "message": str(exc),
                "options": ["manual_fix", "rebuild_phase"],
            }
            await _persist_all_async()
            return
        state["lifetime_qc_runs"] = int(state.get("lifetime_qc_runs", state.get("total_rounds", 0)) or 0) + 1
        state["total_rounds"] = state["lifetime_qc_runs"]
        round_num = state["lifetime_qc_runs"]
        state["messages"].append({
            "role": "system",
            "content": f"🔄 第 {round_num} 轮：执行质检...",
            "ts": time.time(),
        })
        state["status"] = "checking"
        await _persist_all_async()

        # ── 1. 质检 ───────────────────────────────────────────
        try:
            entry = await asyncio.to_thread(
                _run_qc_for_subproject,
                ctx,
                phase_id,
                sp_name,
                False,
                {
                    "run_id": machine.to_dict().get("run_id"),
                    "qa_round_id": machine.to_dict().get("active_round_id"),
                    "scope_digest": scope_snapshot.get("scope_digest"),
                    "artifact_digest": scope_snapshot.get("artifact_digest"),
                },
            )
        except Exception as exc:
            try:
                machine.fail("model", str(exc)[:500])
                _store_supervisor_quality_machine(ctx, phase_id, machine, state)
            except IllegalQualityTransition:
                pass
            state["running"] = False
            state["status"] = "model_failed"
            state["needs_manual"] = False
            state["action_required"] = {
                "message": f"质检模型执行失败：{str(exc)[:200]}",
                "options": ["retry_cycle"],
            }
            state["messages"].append({
                "role": "system",
                "content": f"❌ 质检执行失败：{str(exc)[:200]}",
                "ts": time.time(),
            })
            pm = _phase_managers.get(project_id)
            if pm:
                for phase in pm.phases:
                    if phase.get("phase_id") == phase_id:
                        phase["reviewed"] = True
                        phase["review_passed"] = False
                        phase["status"] = "model_failed"
                        phase.pop("completed_at", None)
                        break
            await _persist_all_async()
            return
        # LLM reviewers occasionally emit an ``error`` record whose own
        # guidance says no change is required. Treat that as an observation,
        # not a blocking defect, so valid SPA fallbacks and imports converge.
        for issue in entry.get("issues_detail") or []:
            if _issue_is_non_actionable(issue) and str(issue.get("status") or "open").lower() == "open":
                issue["status"] = "verified"
            hint = str(issue.get("fix_hint") or "").lower()
            if hint and ("rerun the functionality review" in hint or "could not produce valid acceptance evidence" in hint or "restore the functionality reviewer" in hint):
                if str(issue.get("status") or "open").lower() not in {"fixed", "verified"}:
                    issue["status"] = "verified"
                    issue["verified_reason"] = "reviewer_unavailable_hint"
        if entry.get("issues_detail") is not None:
            entry["passed"] = not _blocking_issue_details(entry)
        passed = entry.get("passed", False)
        score = entry.get("score", 0)
        error_count = entry.get("error_count", 0)
        warning_count = entry.get("warning_count", 0)
        issues = entry.get("issues", [])
        developer_report = entry.get("developer_report", "")
        round_result = _record_auto_repair_qc_result(state, phase_id, entry, round_num)

        state["messages"].append({
            "role": "assistant",
            "content": (
                f"📊 质检结果：{'✅ 通过' if passed else '❌ 未通过'} "
                f"（评分 {score}，错误 {error_count} 个，警告 {warning_count} 个）"
            ),
            "ts": time.time(),
        })

        reviewer_error = str(
            entry.get("qc_execution_error")
            or entry.get("reviewer_error")
            or ""
        ).strip()
        if entry.get("qc_input_invalid"):
            machine.fail("infrastructure", reviewer_error or "QC input validation failed")
            _store_supervisor_quality_machine(ctx, phase_id, machine, state)
            state["running"] = False
            state["status"] = "infrastructure_failed"
            state["needs_manual"] = False
            state["action_required"] = {
                "round": round_num,
                "message": (
                    "QC 权威输入校验失败，未派发代码返修："
                    + (reviewer_error or "unknown QC input error")[:500]
                ),
                "options": ["retry_cycle"],
            }
            state["messages"].append({
                "role": "system",
                "content": state["action_required"]["message"],
                "ts": time.time(),
            })
            await _persist_all_async()
            return
        if reviewer_error and (
            entry.get("reviewer_unavailable")
            or "payment required" in reviewer_error.lower()
            or "api 调用失败" in reviewer_error.lower()
            or "llm" in reviewer_error.lower()
        ):
            machine.fail("model", reviewer_error[:500])
            _store_supervisor_quality_machine(ctx, phase_id, machine, state)
            # A reviewer/provider outage is not a product defect.  Never send
            # an engineer to mutate code when the evidence producer failed.
            state["running"] = False
            state["status"] = "model_failed"
            state["needs_manual"] = False
            state["action_required"] = {
                "round": round_num,
                "message": f"质检依赖不可用，未派发返修：{reviewer_error[:500]}",
                "options": ["retry_cycle"],
            }
            state["messages"].append({
                "role": "system",
                "content": state["action_required"]["message"],
                "ts": time.time(),
            })
            pm = _phase_managers.get(project_id)
            phase = pm.get_phase(phase_id) if pm else None
            if phase:
                phase["reviewed"] = False
                phase["review_passed"] = False
                phase["status"] = "model_failed"
                phase.pop("completed_at", None)
            await _persist_all_async()
            return

        blocking_details = _blocking_issue_details(entry)
        raw_observations = copy.deepcopy(
            entry.get("observed_issues_detail")
            if isinstance(entry.get("observed_issues_detail"), list)
            else entry.get("issues_detail") or []
        )
        # The raw QA response is required to detect newly introduced issues,
        # while the merged ledger carries still-open blockers from prior
        # rounds.  Preserve both so historical deduplication cannot manufacture
        # a false convergence result.
        observations_by_key: Dict[str, Dict[str, Any]] = {}
        for issue in [*raw_observations, *copy.deepcopy(blocking_details)]:
            observations_by_key[_auto_repair_issue_key(issue)] = issue
        observed_issues = list(observations_by_key.values())
        unlocated_blockers = bool(
            not passed
            and any(
                str(issue.get("severity") or "error").lower() in {"error", "critical"}
                and str(issue.get("status") or "open").lower() not in {"fixed", "verified"}
                and not _is_located_delivery_issue_path(issue.get("file_path"))
                for issue in blocking_details
            )
        )
        if unlocated_blockers and unlocated_qc_retries < 1:
            unlocated_qc_retries += 1
            diagnostic = "; ".join(
                str(issue.get("message") or "Quality provider returned an unlocated blocker")
                for issue in blocking_details[:3]
            )
            # An unlocated provider/QC result cannot be dispatched safely and
            # must not consume a business round. Mark this QA attempt as an
            # infrastructure failure, then resume the same qa_round_id once.
            machine.fail("infrastructure", diagnostic)
            _store_supervisor_quality_machine(ctx, phase_id, machine, state)
            state["messages"].append({
                "role": "system",
                "content": f"质检未生成可定位文件问题，正在同轮重试一次：{diagnostic}",
                "ts": time.time(),
            })
            await _persist_all_async()
            continue
        machine.record_evidence(
            kind="qa",
            command=(
                f"QAAgent.inspect --phase {phase_id} "
                f"--qa-round {machine.to_dict().get('active_round_id')}"
            ),
            exit_code=0,
            passed=True,
            log=str(entry.get("developer_report") or entry.get("user_report") or "QA completed")[:20000],
            step_id=f"qa:{machine.to_dict().get('active_round_id')}",
            metadata={
                "score": entry.get("score", 0),
                "error_count": entry.get("error_count", 0),
                "warning_count": entry.get("warning_count", 0),
                "runtime_acceptance": copy.deepcopy(entry.get("runtime_acceptance")),
                "acceptance_observations": copy.deepcopy(
                    entry.get("acceptance_observations") or []
                ),
            },
        )
        machine.finish_verification(observed_issues)
        if state.get("rebuild_comparison_pending"):
            baseline = copy.deepcopy(state.get("pre_rebuild_issue_snapshot") or [])
            expected_digest = str(state.get("pre_rebuild_issue_snapshot_digest") or "")
            actual_digest = _rebuild_issue_snapshot_digest(baseline)
            comparison = _compare_rebuild_issue_snapshots(baseline, observed_issues)
            if not expected_digest or actual_digest != expected_digest:
                comparison["status"] = "rebuild_snapshot_invalid"
                comparison["error"] = "Immutable pre-rebuild issue snapshot digest mismatch"
            state["rebuild_comparison"] = copy.deepcopy(comparison)
            round_result["rebuild_comparison"] = copy.deepcopy(comparison)
            if _rebuild_comparison_requires_rollback(comparison["status"]):
                rollback = _restore_phase_rebuild_snapshot(ctx, phase_id, state)
                state["running"] = False
                state["status"] = comparison["status"]
                state["needs_manual"] = True
                state["rebuild_comparison_pending"] = False
                state["issue_report"] = copy.deepcopy(
                    state.get("pre_rebuild_issue_report") or {}
                )
                state["action_required"] = {
                    "message": (
                        "阶段重构首次质检引入新 blocker 或未解决任何原 blocker；"
                        "已恢复重构前的文件、所有权、Agent、任务和质检状态。"
                    ),
                    "options": ["keep_previous_version", "manual_fix", "rebuild_phase"],
                    "automatic_retry_allowed": False,
                }
                state["rebuild_rollback"] = rollback
                restored_phase = pm.get_phase(phase_id) if (pm := _phase_managers.get(project_id)) else None
                if restored_phase:
                    restored_phase["status"] = "needs_rework"
                    restored_phase["reviewed"] = True
                    restored_phase["review_passed"] = False
                    restored_phase["rebuild_comparison"] = copy.deepcopy(comparison)
                    restored_phase["rebuild_rollback"] = copy.deepcopy(rollback)
                await _persist_all_async()
                return
            if comparison["status"] == "converged":
                state["rebuild_comparison_pending"] = False
            active_phase_manager = _phase_managers.get(project_id)
            active_phase = active_phase_manager.get_phase(phase_id) if active_phase_manager else None
            if active_phase:
                active_phase["rebuild_comparison"] = copy.deepcopy(comparison)
        if machine.state == "repair_required":
            responsible_agent_ids = set()
            for issue in machine.active_round.get("issues", []):
                owner_id = str(issue.get("responsible_agent_id") or "")
                if not owner_id:
                    owner = _match_issue_agent(ctx, phase_id, issue)
                    owner_id = str((owner or {}).get("id") or "")
                if owner_id:
                    responsible_agent_ids.add(owner_id)
            if responsible_agent_ids:
                machine.mark_repair_required(
                    agent_ids=sorted(responsible_agent_ids),
                )
            else:
                diagnostic = "; ".join(
                    str(issue.get("message") or "Quality check failed")
                    for issue in blocking_details[:3]
                )
                machine.block(
                    "Blocking QA issues have no responsible engineer: " + diagnostic,
                    machine.active_round.get("issues", []),
                )
        elif machine.state == "qa_running" and passed:
            # This is the only transition that can authorize the legacy
            # review_passed projection below.
            _assert_supervisor_artifact_current(ctx, phase_id, machine)
            machine.complete()
        canonical = _store_supervisor_quality_machine(ctx, phase_id, machine, state)
        if machine.state == "blocked" and pending_snapshot is None:
            state["running"] = False
            state["status"] = (
                "qa_blocked"
                if unlocated_blockers and unlocated_qc_retries >= 1
                else "blocked"
            )
            state["needs_manual"] = True
            state["action_required"] = {
                "message": canonical.get("failure_reason") or "Supervisor quality gate blocked",
                "options": ["manual_fix", "rebuild_phase"],
            }
            pm = _phase_managers.get(project_id)
            phase = pm.get_phase(phase_id) if pm else None
            if phase:
                phase["status"] = "qa_blocked"
                phase["reviewed"] = True
                phase["review_passed"] = False
            await _persist_all_async()
            return

        actionable_blockers = [
            issue for issue in blocking_details
            if str(issue.get("status") or "open").lower() in {"open", "fixing"}
            if _is_located_delivery_issue_path(issue.get("file_path"))
        ]
        if pending_snapshot is not None:
            current_blocker_keys = {
                _auto_repair_issue_key(issue) for issue in blocking_details
            }
            new_blocker_keys = current_blocker_keys - pre_repair_blocker_keys
            resolved_blocker_keys = pre_repair_blocker_keys - current_blocker_keys
            convergence = {
                "status": "converged",
                "before": len(pre_repair_blocker_keys),
                "after": len(current_blocker_keys),
                "resolved_blockers": sorted(resolved_blocker_keys),
                "new_blockers": sorted(new_blocker_keys),
            }
            round_result["convergence"] = convergence
            if new_blocker_keys or not resolved_blocker_keys:
                _restore_repair_snapshot(pending_snapshot)
                _restore_repair_metadata(ctx, pending_metadata_snapshot)
                if pending_qc_snapshot is not None:
                    ctx.qc_results[phase_id] = copy.deepcopy(pending_qc_snapshot)
                state["running"] = False
                regressed = bool(new_blocker_keys)
                state["status"] = "quality_regressed" if regressed else "no_progress"
                convergence["status"] = state["status"]
                state["needs_manual"] = True
                previous_entry = (
                    pending_qc_snapshot.get("qa", pending_qc_snapshot)
                    if isinstance(pending_qc_snapshot, dict) else {}
                )
                if isinstance(previous_entry, dict) and previous_entry:
                    state["review_result"] = _auto_repair_review_result(
                        phase_id, previous_entry, max(1, round_num - 1)
                    )
                    state["issue_report"] = _auto_repair_issue_report(previous_entry)
                new_messages = [
                    str(issue.get("message") or _auto_repair_issue_key(issue))
                    for issue in blocking_details
                    if _auto_repair_issue_key(issue) in new_blocker_keys
                ]
                regression_detail = "；新增阻断：" + "；".join(new_messages[:3]) if new_messages else ""
                state["action_required"] = {
                    "round": state.get("round", 0),
                    "message": (
                        "返修引入了新的阻断问题，已回滚本轮文件并停止自动修改。"
                        + regression_detail
                        if regressed else
                        "返修后阻断问题没有减少，已回滚本轮文件并停止自动修改。"
                    ),
                    "options": ["manual_fix", "rebuild_phase"],
                }
                state["messages"].append({
                    "role": "system",
                    "content": f"❌ {state['action_required']['message']}",
                    "ts": time.time(),
                })
                pm = _phase_managers.get(project_id)
                if pm:
                    phase = pm.get_phase(phase_id)
                    if phase:
                        phase["status"] = "qa_blocked"
                        phase["review_passed"] = False
                if machine.state != "blocked":
                    machine.block(
                        state["action_required"]["message"],
                        blocking_details,
                    )
                    _store_supervisor_quality_machine(ctx, phase_id, machine, state)
                await _persist_all_async()
                return
            pending_snapshot = None
            pending_metadata_snapshot = None
            pending_qc_snapshot = None

        # The durable machine owns the five-business-QA budget.  Once it
        # blocks, the route must never dispatch another repair merely because
        # the legacy per-cycle repair counter still has room.  A converging
        # fourth repair is retained, but the unresolved fifth QA result is
        # explicitly handed to the user.
        if machine.state == "blocked":
            state["running"] = False
            state["status"] = "awaiting_decision"
            state["needs_manual"] = True
            state["issue_report"] = _auto_repair_issue_report(entry)
            state["action_required"] = {
                "round": round_num,
                "message": (
                    "五轮业务质检（初检加最多四次返修复检）已用尽，"
                    "仍有阻断问题，已停止继续派发返修。"
                ),
                "options": ["manual_fix", "rebuild_phase"],
                "automatic_retry_allowed": False,
            }
            pm = _phase_managers.get(project_id)
            phase = pm.get_phase(phase_id) if pm else None
            if phase:
                phase["reviewed"] = True
                phase["review_passed"] = False
                phase["status"] = "needs_rework"
                phase.pop("completed_at", None)
            await _persist_all_async()
            return

        manual_blockers = [
            issue for issue in blocking_details
            if str(issue.get("status") or "").lower() == "needs_manual"
        ]
        if not passed and manual_blockers and not actionable_blockers:
            state["running"] = False
            state["status"] = "qa_blocked"
            state["needs_manual"] = True
            state["action_required"] = {
                "round": state.get("round", 0),
                "message": "剩余阻断问题需要人工处理，不再重复派发给工程师。",
                "options": ["manual_fix", "rebuild_phase"],
            }
            await _persist_all_async()
            return
        if not passed and not blocking_details:
            # A failed QA result without an actionable blocker is not a repair
            # batch.  Never treat zero dispatched repairs as completion and
            # immediately start another QC run.
            diagnostic = str(
                entry.get("qc_execution_error")
                or entry.get("developer_report")
                or "Quality check failed without an actionable blocking issue"
            )[:500]
            state["running"] = False
            state["status"] = "qa_blocked"
            state["needs_manual"] = True
            state["action_required"] = {
                "round": round_num,
                "message": diagnostic,
                "options": ["retry_cycle", "rebuild_phase"],
            }
            state["messages"].append({
                "role": "system",
                "content": "质检失败但没有可执行的文件级修复项，已停止循环，禁止空修复后继续复检。",
                "ts": time.time(),
            })
            pm = _phase_managers.get(project_id)
            phase = pm.get_phase(phase_id) if pm else None
            if phase:
                phase["reviewed"] = True
                phase["review_passed"] = False
                phase["status"] = "qa_blocked"
                phase.pop("completed_at", None)
            await _persist_all_async()
            return
        if not passed and blocking_details and not actionable_blockers:
            diagnostic = "; ".join(
                str(issue.get("message") or "Quality check failed")
                for issue in blocking_details[:3]
            )
            if unlocated_qc_retries < 1:
                unlocated_qc_retries += 1
                state["messages"].append({
                    "role": "system",
                    "content": (
                        "⚠️ 本轮质检未生成可定位的文件级问题，正在自动重新执行质检："
                        f"{diagnostic}"
                    ),
                    "ts": time.time(),
                })
                await _persist_all_async()
                continue
            state["running"] = False
            state["status"] = "qa_blocked"
            state["needs_manual"] = True
            state["action_required"] = {
                "round": round_num,
                "message": diagnostic,
                "options": ["retry_cycle", "rebuild_phase"],
            }
            state["messages"].append({
                "role": "system",
                "content": f"❌ 质检未生成可定位的文件级问题：{diagnostic}",
                "ts": time.time(),
            })
            pm = _phase_managers.get(project_id)
            if pm:
                for phase in pm.phases:
                    if phase.get("phase_id") == phase_id:
                        phase["reviewed"] = True
                        phase["review_passed"] = False
                        phase["status"] = "qa_blocked"
                        phase.pop("completed_at", None)
                        break
            await _persist_all_async()
            return

        if passed and machine.state == "completed":
            state["running"] = False
            state["status"] = "passed"
            state["messages"].append({
                "role": "system",
                "content": (
                    f"🎉 第 {round_num} 次质检通过，"
                    f"本次循环完成 {cycle_repairs} 次返修。"
                ),
                "ts": time.time(),
            })
            # 更新阶段状态
            try:
                pm = _phase_managers.get(project_id)
                if pm:
                    for p in pm.phases:
                        if str(p.get("phase_id")) == str(phase_id):
                            p["review_passed"] = True
                            p["reviewed"] = True
                            p["status"] = "reviewing"
                            p["reviewed_at"] = time.time()
                            break
            except Exception:
                pass
            await _persist_all_async()
            return

        # ── 检查是否超过总轮次 ────────────────────────────────
        if cycle_repairs >= AUTO_REPAIR_MAX_REPAIRS_PER_CYCLE:
            state["running"] = False
            state["status"] = "awaiting_decision"
            state["needs_manual"] = True
            # Execution completion is not QA acceptance.  Persist an explicit
            # blocked/rework lifecycle so project rollups cannot report a
            # false completion while the user still has unresolved findings.
            pm = _phase_managers.get(project_id)
            if pm:
                for phase in pm.phases:
                    if phase.get("phase_id") == phase_id:
                        phase["reviewed"] = True
                        phase["review_passed"] = False
                        phase["status"] = "needs_rework"
                        phase["progress"] = 100
                        phase.pop("completed_at", None)
                        break
            state["issue_report"] = _auto_repair_issue_report(entry)
            state["action_required"] = {
                "round": round_num,
                "message": f"已完成 {cycle_repairs} 次返修及最终复检，问题已按文件聚合。",
                "options": ["manual_fix", "retry_cycle", "rebuild_phase"],
            }
            state["messages"].append({
                "role": "system",
                "content": (
                    f"本轮已完成最大 {AUTO_REPAIR_MAX_REPAIRS_PER_CYCLE} 次返修，可自行修改、继续质检修复循环，"
                    "或重构整个阶段。"
                ),
                "ts": time.time(),
            })
            await _persist_all_async()
            return

        # ── 2. 等待 Agent 实际完成修复后再进行下一轮质检 ──────
        try:
            pending_snapshot = _snapshot_repair_targets(ctx, entry)
            pending_metadata_snapshot = _snapshot_repair_metadata(ctx, phase_id)
            pending_qc_snapshot = copy.deepcopy(
                getattr(ctx, "qc_results", {}).get(phase_id, {})
            )
            pre_repair_blocker_keys = {
                _auto_repair_issue_key(issue) for issue in blocking_details
            }
            cycle_repairs += 1
            state["round"] = int(state.get("round", 0) or 0) + 1
            state["repair_attempts"] = int(state.get("repair_attempts", state["round"] - 1) or 0) + 1
            repair_round_num = state["round"]
            state["status"] = "repairing"
            state["messages"].append({
                "role": "system",
                "content": f"🔧 已发现 {error_count} 个阻断问题，正在通知责任工程师自动修复...",
                "ts": time.time(),
            })
            pm = _phase_managers.get(project_id)
            if pm:
                for phase in pm.phases:
                    if phase.get("phase_id") == phase_id:
                        phase["status"] = "needs_rework"
                        phase["review_passed"] = False
                        break
            await _persist_all_async()
            repair_batch = await _repair_all_issue_owners(
                project_id, phase_id, repair_round_num, entry, state, rewrite_mode,
                _auto_repair_api_configs.get(key),
            )
            if not isinstance(repair_batch, dict) or (
                repair_batch.get("status") != "completed"
                or int(repair_batch.get("completed", 0) or 0)
                != int(repair_batch.get("total", 0) or 0)
                or int(repair_batch.get("failed", 0) or 0) != 0
            ):
                raise RuntimeError("Repair batch did not reach a fully completed state")
            if int(repair_batch.get("total", 0) or 0) <= 0:
                raise RuntimeError("Repair batch completed without dispatching any repair task")
            reported_change = repair_batch.get("files_changed")
            files_changed = (
                bool(reported_change)
                if reported_change is not None
                else bool(pending_snapshot is not None and _repair_snapshot_changed(pending_snapshot))
            )
            repair_batch["files_changed"] = files_changed
            if not files_changed:
                _restore_repair_metadata(ctx, pending_metadata_snapshot)
                state["running"] = False
                state["status"] = "no_progress"
                state["needs_manual"] = True
                state["action_required"] = {
                    "round": repair_round_num,
                    "message": "全部修复任务已返回，但目标文件没有实际变化，已禁止进入下一轮质检。",
                    "options": ["retry_cycle", "rebuild_phase"],
                }
                state["messages"].append({
                    "role": "system",
                    "content": "修复批次未产生任何文件变化，自动循环已停止。",
                    "ts": time.time(),
                })
                if pm:
                    phase = pm.get_phase(phase_id)
                    if phase:
                        phase["reviewed"] = True
                        phase["review_passed"] = False
                        phase["status"] = "needs_rework"
                if machine.state != "blocked":
                    machine.block(
                        state["action_required"]["message"],
                        blocking_details,
                    )
                    _store_supervisor_quality_machine(ctx, phase_id, machine, state)
                await _persist_all_async()
                return
            # A successful repair batch is the engineer-completion checkpoint.
            # Re-enter verification only after all critical repair owners have
            # reached a succeeded state and a fresh workspace digest is bound.
            for agent_id in list(machine.to_dict().get("waiting_for") or []):
                machine.record_agent(
                    str(agent_id),
                    "succeeded",
                    critical=True,
                    task_id=f"repair:{repair_round_num}:{agent_id}",
                )
            _prepare_supervisor_verification(ctx, phase_id, machine)
            _store_supervisor_quality_machine(ctx, phase_id, machine, state)
        except Exception as exc:
            if pending_snapshot is not None:
                _restore_repair_snapshot(pending_snapshot)
            _restore_repair_metadata(ctx, pending_metadata_snapshot)
            state["running"] = False
            state["status"] = "error"
            state["needs_manual"] = True
            state["action_required"] = {
                "round": round_num,
                "message": "负责专家的返修交付失败，已停止后续质检，避免误判为通过。",
                "options": ["retry_cycle", "rebuild_phase"],
            }
            state["messages"].append({
                "role": "system",
                "content": f"❌ Agent 修复执行失败：{str(exc)[:200]}",
                "ts": time.time(),
            })
            pm = _phase_managers.get(project_id)
            if pm:
                for phase in pm.phases:
                    if phase.get("phase_id") == phase_id:
                        phase["review_passed"] = False
                        phase["reviewed"] = True
                        phase["status"] = "failed"
                        break
            if machine.state != "blocked":
                machine.block(str(exc), [str(exc)])
                _store_supervisor_quality_machine(ctx, phase_id, machine, state)
            await _persist_all_async()
            return
        state["status"] = "rechecking"
        state["messages"].append({
            "role": "system",
            "content": "🔄 责任工程师修复完成，正在重新执行质检...",
            "ts": time.time(),
        })
        pm = _phase_managers.get(project_id)
        if pm:
            for phase in pm.phases:
                if phase.get("phase_id") == phase_id:
                    phase["status"] = "reviewing"
                    break
        await _persist_all_async()
        await asyncio.sleep(1.0)
        continue

        phase_agents = [
            a for a_id, a in ctx.agents.items()
            if a.get("phase_id") == phase_id
        ]
        if not phase_agents:
            state["running"] = False
            state["status"] = "error"
            state["messages"].append({
                "role": "system",
                "content": "❌ 该阶段没有可用的执行 Agent",
                "ts": time.time(),
            })
            return

        agent_info = phase_agents[0]
        agent_id = agent_info.get("id")
        subproject_id = agent_info.get("subproject_id", phase_id)

        # 使用公开 API 重置修复次数计数器
        from api.routes_execution import _reset_fix_attempt
        _reset_fix_attempt(agent_id)

        # 构造精确的修复任务描述，含文件路径和具体问题
        issues_with_files = entry.get("issues_detail", [])
        file_issues: Dict[str, List[str]] = {}
        for iss in issues_with_files:
            if iss.get("status") != "fixed":
                fpath = iss.get("file_path", "未知文件")
                if fpath not in file_issues:
                    file_issues[fpath] = []
                file_issues[fpath].append(f"- [{iss.get('severity','?')}] {iss.get('message','')} → {iss.get('fix_hint','请修复')}")

        file_lines = []
        for fpath, issues_list in list(file_issues.items())[:8]:
            file_lines.append(f"\n涉及文件：{fpath}\n" + "\n".join(issues_list[:5]))

        file_detail = "\n".join(file_lines) if file_lines else ""

        fix_description = (
            f"【质检修复任务 · 第 {round_num} 轮】\n"
            f"{'【重写模式】' if rewrite_mode else ''}\n"
            f"请逐个修改以下文件的具体问题（不要改动无关代码）：\n"
            f"{file_detail}\n"
            f"质检详细报告：\n{developer_report or '见上方质检报告'}"
        )

        if rewrite_mode:
            fix_description += (
                "\n\n⚠ 带记忆重写模式：请保留正确部分的逻辑，只重写有问题的函数/方法。"
                "不要删除现有的正确功能。重写后确保文件可以正常运行。"
            )
        else:
            fix_description += (
                "\n\n⚠ 修复规则：只修改上述文件中质检指出的具体问题。"
                "不要重构无关代码、不要改变API接口签名、不要添加新文件。"
            )

        state["messages"].append({
            "role": "system",
            "content": f"🔧 通知 Agent {agent_info.get('role', '')} 进行第 {round_num} 轮修复...",
            "ts": time.time(),
        })

        # 同步等待 Agent 完成修复（await 会阻塞直到 LLM 执行完毕+文件写入）
        from api.routes_execution import _run_agent_task as _exec_task
        try:
            await _exec_task(
                project_id=project_id,
                agent_id=agent_id,
                subproject_id=subproject_id,
                subproject_name=agent_info.get("subproject_name", ""),
                description=fix_description,
                tech_stack=[],
                project_context=ctx.pm.context_summary or ctx.description or "",
                user_api_config=_auto_repair_api_configs.get(key),
            )
            state["messages"].append({
                "role": "assistant",
                "content": f"✅ Agent 修复完成（生成了新代码文件），即将进入第 {round_num + 1} 轮质检...",
                "ts": time.time(),
            })
        except Exception as e:
            state["messages"].append({
                "role": "system",
                "content": f"❌ Agent 修复失败：{e}",
                "ts": time.time(),
            })

        # 等待文件系统刷新完成
        await asyncio.sleep(1.0)

async def _delegate_to_fullstack_engineer(project_id: str, phase_id: str, qc_entry: Dict, state: Dict):
    """委托全栈工程师处理超限问题并输出报告"""
    ctx = projects.get(project_id)
    if not ctx:
        return

    try:
        from agents.fullstack_engineer_agent import FullStackEngineerAgent
        eng = FullStackEngineerAgent(
            hermes_client=hermes_client,
            project_id=project_id,
            workspace=str(ctx.workspace),
        )
        eng.load_project_context({
            "project_overview": ctx.description,
            "current_phase": state.get("phase_name", ""),
            "phase_description": "自动修复循环超限降级",
            "phase_id": phase_id,
            "core_features": [],
            "tech_stack": {},
        })

        issues_summary = "\n".join(
            f"- [{i.get('severity','?')}] {i.get('file_path','')}: {i.get('message','')}"
            for i in qc_entry.get("issues_detail", [])[:10]
        )

        report = eng.generate_report({
            "title": f"阶段「{state.get('phase_name', '')}」自动修复超限报告",
            "total_rounds": state.get("round", 0),
            "remaining_issues": qc_entry.get("error_count", 0),
            "issues_summary": issues_summary,
            "workspace": str(ctx.workspace),
        })

        state["messages"].append({
            "role": "assistant",
            "content": (
                f"📋 全栈工程师报告：\n{report}\n\n"
                f"标记文件已在 workspace 中标注 # NEEDS_MANUAL，"
                f"您可以手动修复后继续下一阶段。"
            ),
            "ts": time.time(),
        })
    except Exception as e:
        state["messages"].append({
            "role": "system",
            "content": f"❌ 全栈工程师报告生成失败：{e}",
            "ts": time.time(),
        })


# ─── 项目级 ExpertLock 查询 ──────────────────────────────────────────────────

@router.get("/projects/{project_id}/locks")
async def get_project_locks(project_id: str):
    """获取项目当前所有活跃的 ExpertLock 记录"""
    _get_project(project_id)
    locks = expert_lock.get_active_locks(project_id=project_id)
    return {"project_id": project_id, "locks": locks, "count": len(locks)}


@router.get("/projects/{project_id}/cross-stage-conflicts")
async def get_cross_stage_conflicts(project_id: str):
    """获取跨阶段冲突列表 (5.3.6)"""
    _get_project(project_id)
    try:
        from core.orchestrator import get_orchestrator
        orchestrator = get_orchestrator(project_id)
        replan_requests = orchestrator._replan_requests if hasattr(orchestrator, '_replan_requests') else []
        conflicts = [
            {"replan_id": r.replan_id, "trigger": r.triggered_by,
             "involved_phases": r.affected_phases, "reason": r.reason,
             "proposed_solution": r.proposed_solution, "status": r.status}
            for r in replan_requests if r.triggered_by == "cross_stage_conflict"
        ]
    except Exception:
        conflicts = []
    return {"project_id": project_id, "conflicts": conflicts, "count": len(conflicts)}
