"""Supervisor 质检路由"""
import asyncio
import copy
import time
import json
import logging
import re
import hashlib
import uuid
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse
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
    ProjectTeamAssignRequest,
)
from core.workspace_integrity import (
    compute_delivery_manifest,
    release_gate_error,
    workspace_persistence_issues,
)
from core.project_write_fence import (
    ProjectWriteFenceConflict,
    project_write_guard,
)
from core.issue_ledger import (
    canonical_defect_id,
    canonicalize_issue,
    defect_fingerprint,
    find_owner_for_path,
    mark_needs_manual,
    refresh_issue,
)
from core.phase_execution_contract import acceptance_criterion_contracts
from core.qc_review_contract import (
    QCContractError,
    RESULT_SCHEMA_VERSION,
    build_qc_review_packet,
    packet_acceptance_contracts,
)
from core.delivery_documents import load_final_qa_scope, load_phase_qa_scope

router = APIRouter(tags=["supervisor"])

_DETERMINISTIC_QC_LAYERS = {
    "syntax", "security", "collaboration", "runtime_contract",
    "api_contract", "system",
}
QC_DISCOVERY_PASS_LIMIT = 2
QC_ISSUE_REPAIR_ATTEMPT_LIMIT = 3

SIGNOFF_BLOCKER_FIELDS = (
    "code", "scope", "target", "message", "action", "owner",
)


class SignoffBlockerResponse(BaseModel):
    code: str
    scope: str
    target: Optional[str] = None
    message: str
    action: Optional[str] = None
    owner: Optional[str] = None


class SignoffResponse(BaseModel):
    passed: bool
    status: str
    artifact_sha256: Optional[str] = None
    blockers: List[SignoffBlockerResponse]
    receipt: Optional[Dict[str, Any]] = None


def _normalize_signoff_blocker(blocker: Dict[str, Any]) -> Dict[str, Any]:
    scope = str(blocker.get("scope") or "project").strip().lower()
    if scope not in {"file", "agent", "phase", "project", "infrastructure"}:
        scope = "project"
    normalized = {
        "code": str(blocker.get("code") or "SIGNOFF_BLOCKED").strip(),
        "scope": scope,
        "target": (
            str(blocker.get("target")).strip()
            if blocker.get("target") is not None else None
        ) or None,
        "message": str(blocker.get("message") or "Signoff is blocked").strip()[:240],
        "action": (
            str(blocker.get("action")).strip()
            if blocker.get("action") is not None else ""
        )[:200] or None,
        "owner": (
            str(blocker.get("owner")).strip()
            if blocker.get("owner") is not None else None
        ) or None,
    }
    return normalized


def _signoff_adjustments(project_id: str) -> List[Dict[str, Any]]:
    from api.routes_engineer import _get_adjustments

    return list(_get_adjustments(project_id) or [])


def _signoff_release_blocker(message: str) -> Dict[str, Any]:
    lowered = message.casefold()
    if "changed after final qa" in lowered:
        code, action = "FINAL_QA_STALE", "重新执行 Final QA"
    elif "runtime acceptance" in lowered:
        code, action = "FINAL_QA_RUNTIME_INVALID", "重新执行 Final QA"
    elif "final qa" in lowered:
        code, action = "FINAL_QA_NOT_PASSED", "执行 Final QA"
    elif "persist" in lowered or "snapshot" in lowered:
        code, action = "PERSISTENCE_BLOCKED", "修复持久化问题后重试 Signoff"
    else:
        code, action = "FINAL_QA_EVIDENCE_INVALID", "重新执行 Final QA"
    return _normalize_signoff_blocker({
        "code": code,
        "scope": "infrastructure" if code == "PERSISTENCE_BLOCKED" else "project",
        "message": message,
        "action": action,
    })


def _validate_signoff_readiness(
    ctx: ProjectContext,
    phases: List[Dict[str, Any]],
    *,
    project_id: Optional[str] = None,
) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    workspace = Path(ctx.workspace)
    resolved_project_id = str(
        project_id or getattr(ctx, "project_id", "")
    )
    scope = load_final_qa_scope(
        project_id=resolved_project_id,
        workspace=workspace,
    )
    if not scope.get("available") or scope.get("issues"):
        detail = next(iter(scope.get("issues") or []), None)
        message = (
            str(detail.get("message") or detail)
            if isinstance(detail, dict)
            else str(detail or "Authoritative delivery documents are unavailable")
        )
        return [_normalize_signoff_blocker({
            "code": "DELIVERY_DOCUMENTS_INVALID",
            "scope": "project",
            "message": message,
            "action": "修复责任书或阶段交付文档后重新执行 Final QA",
        })], None

    whole_project = (ctx.qc_results or {}).get("__whole_project__", {})
    final_qa = (
        whole_project.get("qa", whole_project)
        if isinstance(whole_project, dict) else {}
    )
    expected_manifest = (
        final_qa.get("delivery_manifest")
        if isinstance(final_qa, dict) else {}
    ) or {}
    try:
        manifest = compute_delivery_manifest(
            workspace,
            required_paths=expected_manifest.get("required_paths") or (),
        )
    except (OSError, ValueError) as exc:
        return [_normalize_signoff_blocker({
            "code": "ARTIFACT_UNREADABLE",
            "scope": "project",
            "message": f"Project delivery artifact cannot be read safely: {exc}",
            "action": "修复交付文件后重新执行 Final QA",
        })], None

    persistence_issues = workspace_persistence_issues(workspace)
    if persistence_issues:
        owners = {
            str(item.get("path") or ""): str(item.get("agent_id") or "") or None
            for item in scope.get("files") or []
            if str(item.get("path") or "")
        }
        blockers = []
        for issue in persistence_issues[:5]:
            candidate = str(issue).split(":", 1)[0].strip()
            target = candidate if candidate in owners else None
            blockers.append(_normalize_signoff_blocker({
                "code": "PERSISTENCE_BLOCKED",
                "scope": "file" if target else "infrastructure",
                "target": target,
                "message": str(issue),
                "action": "修复持久化问题后重试 Signoff",
                "owner": owners.get(target) if target else None,
            }))
        return blockers, manifest

    gate_error = release_gate_error(
        ctx.qc_results,
        workspace,
        current_manifest=manifest,
    )
    if gate_error:
        return [_signoff_release_blocker(gate_error)], manifest

    state_error = _signoff_state_gate_error(ctx, phases)
    if state_error:
        return [_normalize_signoff_blocker({
            "code": "EXECUTION_STATE_INCOMPLETE",
            "scope": "project",
            "message": state_error,
            "action": "完成对应阶段、Agent 或任务后重试 Signoff",
        })], manifest

    active_adjustments = [
        item for item in _signoff_adjustments(resolved_project_id)
        if str(item.get("status") or "") not in {"done", "accepted", "cancelled"}
        or bool(item.get("requires_final_qa"))
    ]
    if active_adjustments:
        adjustment_id = str(active_adjustments[0].get("id") or "") or None
        return [_normalize_signoff_blocker({
            "code": "REWORK_OPEN",
            "scope": "project",
            "target": adjustment_id,
            "message": "Project still has unfinished adjustment or rework",
            "action": "完成返工并重新执行 Final QA",
        })], manifest
    return [], manifest


def _signoff_payload(
    *,
    passed: bool,
    status: str,
    manifest: Optional[Dict[str, Any]],
    blockers: List[Dict[str, Any]],
    receipt: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "passed": passed,
        "status": status,
        "artifact_sha256": (
            str(manifest.get("artifact_sha256") or "")
            if manifest else None
        ) or None,
        "blockers": blockers,
        "receipt": copy.deepcopy(receipt) if receipt is not None else None,
    }


def _signoff_binding(
    ctx: ProjectContext,
    manifest: Dict[str, Any],
) -> Dict[str, Any]:
    qa = ((ctx.qc_results.get("__whole_project__") or {}).get("qa") or {})
    runtime = qa.get("runtime_acceptance") or {}
    final_run = qa.get("final_qa_run") or {}
    final_identity = {
        "run_id": str(final_run.get("run_id") or qa.get("final_qa_run_id") or ""),
        "generation": str(
            final_run.get("qc_generation")
            or final_run.get("generation")
            or qa.get("final_qa_generation")
            or ""
        ),
        "passed": qa.get("passed") is True,
        "status": str(qa.get("status") or ""),
        "artifact_sha256": str(manifest.get("artifact_sha256") or ""),
    }

    def digest(value: Dict[str, Any]) -> str:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    return {
        "project_id": ctx.project_id,
        "artifact_sha256": str(manifest.get("artifact_sha256") or ""),
        "artifact_manifest_rule_version": str(manifest.get("rule_version") or ""),
        "final_qa_run_id": final_identity["run_id"],
        "final_qa_generation": final_identity["generation"],
        "final_qa_digest": digest(final_identity),
        "runtime_evidence_digest": digest(runtime),
    }


def _new_signoff_receipt(
    ctx: ProjectContext,
    manifest: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "schema_version": "metis/signoff-receipt/v1",
        "receipt_id": f"signoff-{uuid.uuid4().hex}",
        **_signoff_binding(ctx, manifest),
        "receipt_revision": 1,
        "terminal_status": "completed",
        "signed_off_at": time.time(),
    }


def _signoff_receipt_matches(
    receipt: Dict[str, Any],
    binding: Dict[str, Any],
) -> bool:
    return (
        receipt.get("schema_version") == "metis/signoff-receipt/v1"
        and all(str(receipt.get(key) or "") == str(value or "")
                for key, value in binding.items())
    )


def _signoff_state_gate_error(ctx: ProjectContext, phases: List[Dict[str, Any]]) -> str | None:
    """Revalidate mutable execution state immediately before sign-off."""
    if not phases:
        return "Project phases are unavailable; sign-off is blocked"
    supervisor_runs = getattr(ctx, "supervisor_quality_runs", {}) or {}
    for index, phase in enumerate(phases):
        phase_id = str(phase.get("phase_id") or phase.get("id") or index + 1)
        if phase.get("status") != "completed" or phase.get("user_confirmed") is not True:
            return f"Phase {phase_id} is not confirmed and completed"
        run = supervisor_runs.get(phase_id)
        if not isinstance(run, dict):
            return f"Phase {phase_id} is missing a current Supervisor completion gate"
        if run.get("status", run.get("state")) != "completed":
            return f"Phase {phase_id} Supervisor quality state is not completed"
        if (run.get("completion_gate") or {}).get("passed") is not True:
            return f"Phase {phase_id} Supervisor completion gate is not passed"

    for agent_id, agent in (getattr(ctx, "agents", {}) or {}).items():
        state = str(agent.get("status") or "")
        progress = int(agent.get("progress") or 0)
        if state != "completed" or progress != 100:
            return f"Agent {agent_id} is not completed at 100%"
    for subproject in getattr(ctx, "subprojects", []) or []:
        subproject_id = str(subproject.get("id") or "")
        phase_id = str(subproject.get("phase_id") or "")
        is_planning_container = (
            subproject_id
            and (
                subproject_id == phase_id
                or (not phase_id and subproject_id.casefold().startswith("phase"))
            )
            and not subproject.get("agent_id")
            and not subproject.get("agent_role")
            and not subproject.get("deliverables")
            and not subproject.get("required_delivery_files")
            and not subproject.get("required_files")
            and not subproject.get("started_at")
            and not subproject.get("completed_at")
        )
        if is_planning_container:
            continue
        subproject_id = subproject_id or "?"
        state = str(subproject.get("status") or "")
        progress = int(subproject.get("progress") or 0)
        if state != "completed" or progress != 100:
            return f"Subproject {subproject_id} is not completed at 100%"
    return None


def _qc_issue_fingerprint(issue: Dict[str, Any]) -> str:
    return defect_fingerprint(issue)


def _stable_qc_issue_id(issue: Dict[str, Any]) -> str:
    return canonical_defect_id(issue)


def _runtime_repair_target(
    pm: Any, evidence_path: str, diagnostic: str = ""
) -> Optional[str]:
    """Return one confident repair target, or ``None`` instead of guessing."""
    normalized = str(evidence_path or "").replace("\\", "/")
    lowered = normalized.lower()
    if not pm:
        return normalized
    diagnostic_lower = str(diagnostic or "").lower()

    registry_paths: List[str] = []
    for registry_key, item in getattr(pm, "file_registry", {}).items():
        path = str(item.get("file_path") or registry_key or "").replace("\\", "/")
        relative = Path(path)
        if not path or relative.is_absolute() or ".." in relative.parts:
            continue
        workspace = getattr(pm, "workspace", None)
        if workspace is not None and not (Path(workspace) / relative).is_file():
            continue
        registry_paths.append(path)

    # Runtime stacks are stronger evidence than a generic package.json fallback.
    def _is_test_file(path: str) -> bool:
        value = path.lower()
        name = Path(value).name
        parts = {part.lower() for part in Path(value).parts}
        return (
            bool(parts & {"tests", "__tests__"})
            or any(token in value for token in (".test.", ".spec."))
            or name.startswith("test_")
            or name.endswith("_test.py")
        )

    for path in registry_paths:
        if _is_test_file(path):
            continue
        if re.search(
            rf"(?<![A-Za-z0-9_.-])(?:file://)?(?:/app/)?{re.escape(path)}"
            rf"(?=[:)\s]|$)",
            str(diagnostic or ""),
            re.IGNORECASE,
        ):
            return path

    test_name = Path(normalized).name.lower()
    path_parts = {part.lower() for part in Path(normalized).parts}
    is_test_path = (
        bool(path_parts & {"tests", "__tests__"})
        or any(token in lowered for token in (".test.", ".spec."))
        or test_name.startswith("test_")
        or test_name.endswith("_test.py")
    )
    if not is_test_path:
        if normalized in {"", "package.json"} and any(
            marker in diagnostic_lower
            for marker in (
                "syntaxerror", "unexpected end of input", "unexpected token",
                "failed to start server", "cannot open database",
            )
        ):
            return None
        return normalized
    if any(marker in diagnostic_lower for marker in (
        "test suite failed to run", "syntaxerror", "unexpected token",
        "no tests found", "no test files found",
    )):
        return normalized

    domain = "backend/" if lowered.startswith("backend/") else (
        "frontend/" if lowered.startswith("frontend/") else ""
    )
    tokens = {
        token for token in re.split(r"[^a-z0-9]+", test_name)
        if len(token) > 2 and token not in {"test", "tests", "spec", "pytest"}
    }
    candidates: List[str] = []
    for path in registry_paths:
        path_lower = path.lower()
        if not path or (domain and not path_lower.startswith(domain)):
            continue
        if _is_test_file(path) or any(token in path_lower for token in (
            "package-lock.json", "node_modules/",
        )):
            continue
        if Path(path).suffix.lower() not in {".py", ".js", ".jsx", ".ts", ".tsx"}:
            continue
        candidates.append(path)
    if not candidates:
        return normalized

    def score(path: str) -> tuple[int, int, str]:
        value = path.lower()
        token_score = sum(100 for token in tokens if token in value)
        route_score = 30 if any(marker in value for marker in (
            "/routes/", "/routers/", "/controllers/", "/services/",
        )) else 0
        entry_score = 10 if Path(value).stem in {"app", "main", "server", "index"} else 0
        return token_score, route_score + entry_score, value

    ranked = sorted(candidates, key=score, reverse=True)
    best_score = score(ranked[0])
    if best_score[0] <= 0:
        return None
    if len(ranked) > 1 and score(ranked[1])[:2] == best_score[:2]:
        return None
    return ranked[0]


def _defer_non_actionable_final_findings_to_runtime(
    result: Dict[str, Any],
) -> bool:
    """Do not mutate product code for an evidence-free LLM rejection."""
    marker = "Functionality review rejected the delivery without a usable blocking finding"
    changed = False
    layer_results = list(result.get("layer_results") or [])
    for layer in layer_results:
        for issue in layer.get("issues") or []:
            if str(issue.get("message") or "") != marker:
                continue
            issue["severity"] = "warning"
            issue["message"] = (
                "Functionality reviewer returned a non-actionable rejection; "
                "isolated runtime acceptance must decide the final verdict"
            )
            issue["fix_hint"] = (
                "Do not change product code without evidence. Run the deterministic "
                "build, test, startup and health-check gate."
            )
            changed = True
    if not changed:
        return False

    blocking = any(
        str(issue.get("severity") or "warning").lower() in {"error", "critical"}
        for layer in layer_results
        for issue in (layer.get("issues") or [])
    )
    if not blocking:
        result["passed"] = True
        result["issues"] = [
            str(issue.get("message") or "")
            for layer in layer_results
            for issue in (layer.get("issues") or [])
        ]
    return True


def _should_append_qc_issue(
    issue: Dict[str, Any],
    allow_discovery: Optional[bool] = None,
    *,
    is_first_qc: Optional[bool] = None,
) -> bool:
    """Bound model discovery while retaining deterministic hard gates."""
    if allow_discovery is None:
        allow_discovery = bool(is_first_qc)
    layer = str(issue.get("layer") or "").strip().lower()
    return bool(allow_discovery) or layer in (
        _DETERMINISTIC_QC_LAYERS | {"io", "runtime_acceptance"}
    )


def _append_qc_observation_history(
    previous_entry: Dict[str, Any],
    findings: List[Dict[str, Any]],
    *,
    qa_context: Optional[Dict[str, Any]],
    qc_round: int,
    observed_at: Optional[float] = None,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Persist every finding separately from the blocking convergence ledger."""
    context = {
        "qc_round": int(qc_round),
        "supervisor_run_id": str((qa_context or {}).get("run_id") or ""),
        "qa_round_id": str((qa_context or {}).get("qa_round_id") or ""),
        "artifact_sha256": str(
            (qa_context or {}).get("artifact_sha256")
            or (qa_context or {}).get("artifact_digest")
            or ""
        ),
        "scope_digest": str((qa_context or {}).get("scope_digest") or ""),
        "producer": "SupervisorQualityMachine",
        "observed_at": time.time() if observed_at is None else float(observed_at),
    }
    round_findings = [
        {
            **copy.deepcopy(issue),
            **{key: value for key, value in context.items() if key not in issue},
        }
        for issue in findings
    ]
    history = list(previous_entry.get("observation_history") or [])
    history.append({**context, "findings": round_findings})
    flattened = [
        copy.deepcopy(finding)
        for observation_round in history
        for finding in (observation_round.get("findings") or [])
        if isinstance(finding, dict)
    ]
    return history, flattened


def _should_reopen_fixed_issue(issue: Optional[Dict[str, Any]]) -> bool:
    canonical = canonicalize_issue(issue or {})
    return bool(
        issue
        and not canonical.get("requires_identity_review")
        and _should_append_qc_issue(issue, allow_discovery=False)
    )


def _has_blocking_qc_issues(issues: List[Dict[str, Any]]) -> bool:
    """Warnings remain visible, but active error/critical findings block progress."""
    # ``needs_manual`` is a durable full-stack-engineer handoff, not an
    # automatic-loop blocker. It remains visible and still gates final release.
    active_statuses = {"open", "fixing"}
    return any(
        issue.get("status") in active_statuses
        and str(issue.get("severity", "")).strip().lower() in {"error", "critical"}
        for issue in issues
    )


def _refresh_issue_routing(old_issue: Dict[str, Any], new_issue: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Refresh ownership when a later deterministic check locates the true fix target."""
    if not new_issue:
        return dict(old_issue)
    return refresh_issue(old_issue, new_issue)


def _reconcile_needs_manual_qc_issue(
    old_issue: Dict[str, Any],
    reproduced_issue: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Keep manual escalation only while its canonical defect is reproduced."""
    if reproduced_issue:
        refreshed = _refresh_issue_routing(old_issue, reproduced_issue)
        return mark_needs_manual(
            refreshed,
            str(old_issue.get("needs_manual_reason") or "Defect remains reproducible"),
        )
    if canonicalize_issue(old_issue).get("requires_identity_review"):
        return {
            **canonicalize_issue(old_issue),
            "status": "needs_manual",
            "identity_review_required": True,
        }
    return {**old_issue, "status": "fixed", "fixed_at": time.time()}


def _reconcile_pending_verification_qc_issue(
    old_issue: Dict[str, Any],
    reproduced_issue: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Resolve an engineer repair without losing its canonical identity."""
    canonical_old = canonicalize_issue(old_issue)
    verification = {
        "verified_at": time.time(),
        "verification_result": "reproduced" if reproduced_issue else "not_reproduced",
        "verification_evidence": {
            "repair_workspace_digest": old_issue.get("repair_workspace_digest", ""),
            "qa_issue_fingerprint": (
                _qc_issue_fingerprint(reproduced_issue) if reproduced_issue else ""
            ),
        },
    }
    if canonical_old.get("requires_identity_review"):
        refreshed = (
            _refresh_issue_routing(canonical_old, reproduced_issue)
            if reproduced_issue else canonical_old
        )
        return {
            **refreshed,
            **verification,
            "status": "pending_verification",
            "identity_review_required": True,
        }
    if reproduced_issue:
        refreshed = _refresh_issue_routing(canonical_old, reproduced_issue)
        return {
            **refreshed,
            **verification,
            "status": "open",
            "fix_rounds": int(old_issue.get("fix_rounds", 0) or 0) + 1,
        }
    return {
        **canonical_old,
        **verification,
        "status": "verified",
    }


def _deduplicate_routed_issues(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse the same canonical defect after routing or wording changes."""
    result: List[Dict[str, Any]] = []
    seen = set()
    for issue in issues:
        key = _qc_issue_fingerprint(issue)
        if key in seen:
            continue
        seen.add(key)
        normalized = dict(issue)
        normalized["fingerprint"] = key
        result.append(normalized)
    return result



@router.post("/projects/{project_id}/supervisor/chat")
async def supervisor_chat(project_id: str, request: SupervisorChatRequest):
    """Supervisor Agent 多轮对话（含历史压缩 + 实时进度注入）"""
    ctx = _get_project(project_id)
    # 获取实时进度数据，注入到 system prompt
    try:
        progress_data = ctx.supervisor.get_progress()
    except Exception:
        progress_data = None
    history = [
        {"role": msg.get("role"), "content": msg.get("content", "")}
        for msg in (request.history or [])
        if msg.get("role") in ("user", "assistant") and msg.get("content")
    ]
    result = ctx.supervisor.chat(
        user_input=request.message,
        progress_data=progress_data,
        history=history,
        context_summary=request.context_summary,
    )
    return result

@router.post("/projects/{project_id}/tasks")
async def create_task(project_id: str, title: str, description: str, agent_type: str, priority: int = 2):
    ctx = _get_project(project_id)
    task = ctx.supervisor.create_task(title=title, description=description, agent_type=AgentType(agent_type), priority=priority)
    return {"task": task.to_dict()}

@router.get("/projects/{project_id}/tasks/list")
async def list_tasks(project_id: str):
    """获取项目任务列表"""
    ctx = _get_project(project_id)
    tasks = ctx.supervisor.dispatcher.dispatcher.queue.list_all()
    return {"tasks": [t.to_dict() for t in tasks]}

@router.get("/projects/{project_id}/progress")
async def get_progress(project_id: str):
    """返回项目真实进度（从 ctx.subprojects 和 PhaseManager 计算）"""
    ctx = _get_project(project_id)
    pm = _phase_managers.get(project_id)
    total = len(ctx.subprojects)
    completed = sum(1 for s in ctx.subprojects if s.get("status") == "completed")
    in_progress = sum(1 for s in ctx.subprojects if s.get("status") in ("in_progress", "executing"))
    failed = sum(1 for s in ctx.subprojects if s.get("status") == "failed")

    # 加入 PhaseManager 阶段数据
    total_phases = 0
    completed_phases = 0
    if pm and pm.phases:
        total_phases = len(pm.phases)
        completed_phases = sum(1 for p in pm.phases if p.get("status") == "completed" or p.get("review_passed"))

    avg_progress = 0
    if total > 0:
        avg_progress = int(sum(s.get("progress", 0) for s in ctx.subprojects) / total)

    return {
        "project_id": project_id,
        "status": ctx.status,
        "total": max(total, total_phases),
        "completed": max(completed, completed_phases),
        "in_progress": in_progress,
        "failed": failed,
        "overall_progress": avg_progress,
        "phase_stats": {
            "total_phases": total_phases,
            "completed_phases": completed_phases,
        "current_phase": (pm.current_phase_index or 0) + 1 if pm and (pm.current_phase_index or 0) >= 0 else 0,
        } if pm else {},
    }

def _get_phase_manager(project_id: str):
    """获取项目的 PhaseManager 实例（安全返回 None）"""
    from core.app_state import _phase_managers
    return _phase_managers.get(project_id)

def _include_package_companion_configs(
    workspace: Path, output_files: List[str]
) -> List[str]:
    """Include user-created TypeScript configs beside registered manifests.

    Manual QA repair can legitimately create a tsconfig after the original
    execution manifest was registered.  Restrict discovery to the directory
    of a package.json already owned by the reviewed phase, so this does not
    widen QA into unrelated phases or arbitrary workspace files.
    """
    workspace_root = Path(workspace).resolve()
    result = list(dict.fromkeys(
        str(path).replace("\\", "/") for path in output_files if str(path).strip()
    ))
    seen = set(result)
    for manifest_path in tuple(result):
        if Path(manifest_path).name != "package.json":
            continue
        manifest_dir = (workspace_root / Path(manifest_path).parent).resolve()
        try:
            manifest_dir.relative_to(workspace_root)
        except ValueError:
            continue
        if not manifest_dir.is_dir():
            continue
        for candidate in manifest_dir.glob("tsconfig*.json"):
            resolved = candidate.resolve()
            try:
                relative = resolved.relative_to(workspace_root).as_posix()
            except ValueError:
                continue
            if resolved.is_file() and relative not in seen:
                result.append(relative)
                seen.add(relative)
    return result


def _run_qc_for_subproject(
    ctx: "ProjectContext",
    sp_id: str,
    sp_name: str,
    is_final_phase: bool = False,
    qa_context: Optional[Dict[str, Any]] = None,
) -> Dict:
    """
    对单个子项目执行质检。
    - is_final_phase=False（默认）：只检查语法+逻辑+本阶段功能实现
    - is_final_phase=True：额外检查文件协作/整体落地（所有阶段代码都写完后才做）
    结果写入 ctx.qc_results[sp_id]，同时生成用户报告和开发者反馈。
    返回完整质检结果字典。
    """
    from agents.quality_agents import QAAgent

    workspace_path = str(ctx.workspace)
    pm = _get_phase_manager(ctx.project_id)

    # A phase review/repair route also enters here.  Do not treat its phase id
    # as a missing subproject: that used to lose the phase contract/owners and
    # fall back to scanning the whole workspace.
    sp_info = next((s for s in ctx.subprojects if s["id"] == sp_id), {})
    phase_info = (
        pm.get_phase(sp_id)
        if pm and hasattr(pm, "get_phase")
        else None
    )
    whole_project_review = sp_id == "__whole_project__"
    phase_ids = [str(item.get("phase_id", "")) for item in (pm.phases if pm else [])]
    if phase_info and phase_ids:
        is_final_phase = is_final_phase or sp_id == phase_ids[-1]
    # Legacy plans deliberately use the phase id as their placeholder
    # subproject id. A phase review must still aggregate the whole phase,
    # rather than silently narrowing to whichever Agent claimed the placeholder.
    agent_id = "" if phase_info else sp_info.get("agent_id", "")
    agent_info = ctx.agents.get(agent_id, {})
    agent_role = agent_info.get("role", "阶段交付团队" if phase_info else "开发工程师")
    phase_agents: List[Dict[str, Any]] = []
    if phase_info:
        phase_subprojects = [
            s for s in ctx.subprojects if s.get("phase_id") == sp_id
        ]
        phase_agents = [
            item for item in ctx.agents.values()
            if item.get("phase_id") == sp_id
        ]
        roles = list(dict.fromkeys(
            str(item.get("role") or "").strip()
            for item in phase_agents
            if str(item.get("role") or "").strip()
        ))
        if roles:
            agent_role = " / ".join(roles)
        descriptions = list(dict.fromkeys(
            str(s.get("description") or "").strip()
            for s in phase_subprojects
            if str(s.get("description") or "").strip()
        ))
        sp_description = str(phase_info.get("description") or sp_name)
        if descriptions:
            sp_description += "\n\n本阶段子任务：\n" + "\n".join(descriptions)
    else:
        sp_description = sp_info.get("description", sp_name)

    external_findings = [
        item for item in ((qa_context or {}).get("external_findings") or [])
        if isinstance(item, dict) and str(item.get("message") or "").strip()
    ]
    if external_findings:
        sp_description += (
            "\n\n[Deterministic Final QA findings for independent Final QC review]\n"
            "These are evidence, not a terminal verdict. Classify each finding, "
            "identify the smallest responsible existing delivery file and return "
            "a structured repair issue for its original owner. Do not dismiss a "
            "project_defect as infrastructure merely because the evidence came "
            "from a machine check.\n"
            + "\n".join(
                f"- rule={item.get('rule_id') or item.get('status') or 'runtime'}; "
                f"severity={item.get('severity') or 'error'}; "
                f"message={item.get('message')}"
                for item in external_findings
            )
        )

    # 只质检本子项目/本阶段的产出文件，避免把整个 workspace 的历史代码都扫进去
    related_files = []
    authoritative_final_scope = (
        (qa_context or {}).get("final_qa_scope")
        if whole_project_review else None
    )
    if whole_project_review and isinstance(authoritative_final_scope, dict):
        related_files = [
            {"file_path": str(item.get("path") or "")}
            for item in authoritative_final_scope.get("files") or []
            if str(item.get("path") or "")
        ]
    elif whole_project_review and pm:
        # Final QA must consume the accepted registry, not a second and
        # narrower suffix scan that silently drops legitimate artifacts.
        related_files = list(pm.file_registry.values())
        if not related_files:
            # Legacy projects may predate the registry. Agent declarations are
            # only a fallback: mixing them into a populated registry revives
            # stale/hallucinated paths from superseded phase generations.
            registered_paths = set()
            for item in ctx.agents.values():
                for path in item.get("output_files") or []:
                    normalized = str(path).replace("\\", "/").strip()
                    if normalized and normalized not in registered_paths:
                        related_files.append({"file_path": normalized})
                        registered_paths.add(normalized)
    elif phase_info and is_final_phase and pm:
        # Final integration QA is the only phase review that must inspect the
        # complete accepted product.  Use the authoritative registry instead
        # of scanning the workspace so rejected/stale files remain excluded.
        try:
            current_index = phase_ids.index(sp_id)
        except ValueError:
            current_index = len(phase_ids) - 1
        accepted_phase_ids = set(phase_ids[:current_index + 1])
        related_files = [
            item for item in pm.file_registry.values()
            if item.get("phase_id") in accepted_phase_ids
        ]
    elif phase_info and hasattr(pm, "get_files_by_phase"):
        related_files = pm.get_files_by_phase(sp_id)
    elif pm and agent_id:
        related_files = pm.get_files_by_agent(agent_id)
    if not related_files and pm:
        phase_id = sp_info.get("phase_id", "")
        if phase_id:
            related_files = pm.get_files_by_phase(phase_id)
    if phase_info and not related_files:
        phase_output_paths = list(dict.fromkeys(
            str(path).replace("\\", "/")
            for item in phase_agents
            for path in (item.get("output_files") or [])
            if str(path).strip()
        ))
        related_files = [{"file_path": path} for path in phase_output_paths]

    if phase_info:
        registered_paths = {
            str(item.get("file_path") or "").replace("\\", "/")
            for item in related_files
        }
        for item in phase_agents:
            for path in item.get("required_delivery_files") or []:
                normalized = str(path).replace("\\", "/").strip()
                if normalized and normalized not in registered_paths:
                    related_files.append({"file_path": normalized})
                    registered_paths.add(normalized)

    # 如果 file_registry 为空，旧子项目质检才回退扫描 workspace 源码文件；
    # 阶段质检绝不能混入前序或无关阶段的文件。
    if not related_files and not phase_info:
        src_dir = ctx.workspace / "src"
        docs_dir = ctx.workspace / "docs"
        for d in (ctx.workspace, src_dir, docs_dir):
            if d.exists():
                for f in d.rglob("*"):
                    if f.is_file() and f.suffix in {
                        ".py", ".ts", ".tsx", ".js", ".jsx", ".html", ".css",
                        ".json", ".yaml", ".yml", ".toml",
                    }:
                        rel = str(f.relative_to(ctx.workspace)).replace("\\", "/")
                        # 排除执行日志和占位 txt
                        if not rel.startswith("output/") and not rel.endswith(".log") and not rel.endswith(".txt"):
                            related_files.append({"file_path": rel})
                if related_files:
                    break
    output_files = [f.get("file_path", "") for f in related_files if f.get("file_path")]
    authoritative_output_files = list(output_files)
    if phase_info:
        phase_delivery_scope = load_phase_qa_scope(
            project_id=str(ctx.project_id),
            phase_id=str(sp_id),
            workspace=ctx.workspace,
        )
        if phase_delivery_scope.get("available"):
            authoritative_output_files = [
                str(item.get("path") or "").replace("\\", "/")
                for item in phase_delivery_scope.get("files") or []
                if str(item.get("path") or "")
            ]
    output_files = _include_package_companion_configs(ctx.workspace, output_files)
    dependency_files: List[str] = []
    if phase_info and pm:
        try:
            current_index = phase_ids.index(sp_id)
        except ValueError:
            current_index = 0
        previous_phase_ids = set(phase_ids[:current_index])
        contract_tokens = (
            "/api/", "/routes/", "/routers/", "/schemas/", "/models/",
            "/services/", "/types/", "main.", "app.", "vite.config",
            "package.json", "requirements.txt",
        )
        for item in pm.file_registry.values():
            path = str(item.get("file_path", "")).replace("\\", "/")
            lowered = "/" + path.lower()
            if (
                item.get("phase_id") in previous_phase_ids
                and path not in output_files
                and any(token in lowered for token in contract_tokens)
            ):
                dependency_files.append(path)

    policy_agents = phase_agents if phase_info else ([agent_info] if agent_info else [])
    policy_kinds = [
        str((item.get("artifact_policy") or {}).get("kind") or "").strip()
        for item in policy_agents
    ]
    artifact_kind = (
        policy_kinds[0]
        if policy_kinds
        and policy_kinds[0]
        and all(kind == policy_kinds[0] for kind in policy_kinds)
        else ""
    )
    review_packet: Optional[Dict[str, Any]] = None
    qc_input_error = ""
    phase_plan = (phase_info or {}).get("phase_plan") or {}
    uses_authoritative_delivery = (
        isinstance(phase_plan, dict)
        and phase_plan.get("schema_version") == "phase-plan/v1"
    )
    if phase_info and uses_authoritative_delivery and qa_context:
        try:
            companion_dependencies = [
                path for path in output_files
                if path not in authoritative_output_files
            ]
            review_packet = build_qc_review_packet(
                project_id=str(ctx.project_id),
                phase_id=sp_id,
                workspace=ctx.workspace,
                phase=phase_info,
                output_files=authoritative_output_files,
                dependency_files=[*dependency_files, *companion_dependencies],
                qa_context=qa_context,
            )
        except QCContractError as exc:
            qc_input_error = str(exc)
    acceptance_contracts: List[Dict[str, Any]] = []
    if whole_project_review and isinstance(authoritative_final_scope, dict):
        acceptance_contracts = [{
            "criterion_id": str(item.get("criterion") or ""),
            "criterion": str(item.get("text") or ""),
            "artifact_paths": list(item.get("files") or []),
        } for item in authoritative_final_scope.get("criteria") or []
        if str(item.get("criterion") or "") and str(item.get("text") or "")]
    mechanically_passed_ids: set[str] = set(phase_info.get("mechanically_passed_criterion_ids") or []) if phase_info else set()
    if phase_info:
        generation = str(phase_info.get("execution_generation") or "")
        task_owner: Dict[str, tuple[Dict[str, Any], Dict[str, Any]]] = {}
        for item in phase_agents:
            locked_tasks = {
                str(task.get("task_id") or ""): task
                for task in (item.get("locked_tasks") or [])
                if isinstance(task, dict) and task.get("task_id")
            }
            receipts = item.get("task_execution_receipts") or {}
            for task_id, task in locked_tasks.items():
                receipt = receipts.get(task_id) or {}
                if (
                    str(receipt.get("status") or "").lower() == "succeeded"
                    and str(receipt.get("execution_generation") or "")
                    == generation
                ):
                    task_owner[task_id] = (task, receipt)
        for task_id, (task, receipt) in task_owner.items():
            task_run_id = str(receipt.get("completion_run_id") or "")
            if not task_run_id:
                continue
            for contract in acceptance_criterion_contracts(
                task_id,
                task.get("acceptance_criteria") or [],
                artifact_paths=receipt.get("required_files") or [],
            ):
                if contract.get("source_class") == "semantic" and contract.get("criterion_id") not in mechanically_passed_ids:
                    acceptance_contracts.append({
                        **contract,
                        "task_id": task_id,
                        "task_run_id": task_run_id,
                        "execution_generation": generation,
                    })
        supervisor_run_id = str((qa_context or {}).get("run_id") or "")
        if supervisor_run_id:
            for contract in acceptance_criterion_contracts(
                sp_id,
                phase_info.get("acceptance_criteria") or [],
            ):
                if contract.get("source_class") == "semantic" and contract.get("criterion_id") not in mechanically_passed_ids:
                    acceptance_contracts.append({
                        **contract,
                        "task_id": sp_id,
                        "task_run_id": supervisor_run_id,
                        "execution_generation": generation,
                    })

    if review_packet is not None:
        # The packet is the authoritative semantic projection. Reclassifying
        # from receipt.required_files breaks pathless tasks because their
        # receipts intentionally have no preallocated file paths.
        packet_projection: List[Dict[str, Any]] = []
        supervisor_run_id = str((qa_context or {}).get("run_id") or "")
        for contract in packet_acceptance_contracts(review_packet):
            task_id = str(contract.get("task_id") or "")
            owner = task_owner.get(task_id)
            task_run_id = (
                str((owner[1] or {}).get("completion_run_id") or "")
                if owner
                else supervisor_run_id if task_id == sp_id else ""
            )
            if not task_run_id:
                qc_input_error = (
                    f"QC packet criterion has no completed task run: {task_id}"
                )
                continue
            packet_projection.append({
                **contract,
                "task_run_id": task_run_id,
                "execution_generation": generation,
            })
        acceptance_contracts = packet_projection

    try:
        if qc_input_error:
            raise QCContractError(qc_input_error)
        if phase_info and not output_files:
            raise RuntimeError(f"阶段 {sp_id} 没有已登记的交付文件")
        qa = QAAgent(hermes_client=hermes_client)
        result = qa.inspect(
            subproject_id=sp_id,
            workspace_path=workspace_path,
            output_files=output_files,
            dependency_files=dependency_files,
            subproject_description=sp_description,
            agent_role=agent_role,
            subproject_name=sp_name,
            is_final_phase=is_final_phase,
            artifact_kind=artifact_kind,
            acceptance_contracts=acceptance_contracts,
            review_packet=review_packet,
        )
    except Exception as e:
        result = {
            "passed": False,
            "score": 0,
            "issues": [f"质检执行异常：{e}"],
            "user_report": f"## 质检报告 — {sp_name}\n\n❌ 质检执行异常：{e}",
            "developer_report": f"质检执行异常，请检查工作区：{workspace_path}\n错误：{e}",
            "layer_results": [],
            "needs_rewrite": False,
            "error_count": 1,
            "warning_count": 0,
            "qc_execution_error": f"{e.__class__.__name__}: {str(e)[:400]}",
            "qc_input_invalid": isinstance(e, QCContractError),
        }

    if acceptance_contracts:
        raw_observations = result.get("acceptance_observations") or []
        normalized_observations: List[Dict[str, Any]] = []
        for contract in acceptance_contracts:
            criterion_id = str(contract.get("criterion_id") or "")
            matching = [
                observation for observation in raw_observations
                if (
                    isinstance(observation, dict)
                    and str(observation.get("criterion_id") or "")
                    == criterion_id
                    and str(observation.get("criterion") or "")
                    == str(contract.get("criterion") or "")
                )
            ]
            if len(matching) != 1:
                continue
            observation = matching[0]
            normalized_observations.append({
                "task_id": str(contract.get("task_id") or ""),
                "task_run_id": str(contract.get("task_run_id") or ""),
                "execution_generation": str(
                    contract.get("execution_generation") or ""
                ),
                "criterion_id": criterion_id,
                "criterion": str(contract.get("criterion") or ""),
                "passed": observation.get("passed") is True,
                "observation": str(
                    observation.get("observation") or ""
                ).strip(),
                "observed_files": list(dict.fromkeys(
                    str(path).replace("\\", "/")
                    for path in (observation.get("observed_files") or [])
                    if str(path).strip()
                )),
                "artifact_digest": str(
                    (qa_context or {}).get("artifact_digest") or ""
                ),
                "qa_round_id": str(
                    (qa_context or {}).get("qa_round_id") or ""
                ),
            })
        result["acceptance_observations"] = normalized_observations
        # Generated acceptance criteria are optional review aids. Preserve any
        # exact, packet-bound observations the reviewer supplied, but never
        # fail an otherwise valid functionality review merely for omitting them.

    # ── 合并新旧质检结果（稳定合并策略 v2，防止问题越修越多）────────────────────────
    #
    # 核心原则（v2 修正）：
    # 1. fixing 状态：新一轮没发现 → 标记 fixed（修好了）；新一轮还发现 → open + fix_rounds+1
    # 2. fixed 状态：新一轮又发现 → 重置 open + fix_rounds+1；否则保持 fixed
    # 3. open 状态：新一轮没发现 → 标记 fixed（代码已修好，质检没扫到）；还发现 → 保持 open
    #    注意：v1 的 open 保持 open 策略导致问题永远不消失，是累积的根本原因
    # 4. needs_manual：不再自动处理
    # 5. 新问题追加：只追加 error 级别（首次质检追加全部）
    # 6. defect identity 与每轮 observation identity 分离并使用共享 schema
    # The integration phase cannot pass on static/LLM inspection alone.
    # Execute the generated product in the isolated Render acceptance service,
    # then feed deterministic failures into the normal ownership repair loop.
    runtime_acceptance_result: Optional[Dict[str, Any]] = None
    run_runtime_inside_qc = bool(
        (qa_context or {}).get("run_runtime_inside_qc")
    )
    if phase_info and is_final_phase and run_runtime_inside_qc:
        _defer_non_actionable_final_findings_to_runtime(result)
    if (
        phase_info
        and is_final_phase
        and run_runtime_inside_qc
        and result.get("passed", False)
    ):
        from api.routes_adjustments import (
            _runtime_failure_actionable,
            _runtime_acceptance_issue,
            _runtime_acceptance_required,
        )
        from core.runtime_acceptance import run_runtime_acceptance

        try:
            previous_whole = ctx.qc_results.get("__whole_project__", {})
            previous_qa = (
                previous_whole.get("qa", previous_whole)
                if isinstance(previous_whole, dict)
                else {}
            )
            previous_runtime = (
                previous_qa.get("runtime_acceptance")
                if isinstance(previous_qa, dict)
                else None
            )
            if not (
                isinstance(previous_runtime, dict)
                and previous_runtime.get("passed") is True
            ):
                for stored_result in ctx.qc_results.values():
                    stored_qa = (
                        stored_result.get("qa", stored_result)
                        if isinstance(stored_result, dict)
                        else {}
                    )
                    candidate = (
                        stored_qa.get("runtime_acceptance")
                        if isinstance(stored_qa, dict)
                        else None
                    )
                    if isinstance(candidate, dict) and candidate.get("passed") is True:
                        previous_runtime = candidate
                        break
            runtime_acceptance_result = run_runtime_acceptance(
                Path(ctx.workspace),
                ctx.project_id,
                previous_runtime,
            )
        except Exception:
            runtime_acceptance_result = {
                "enabled": True,
                "passed": False,
                "status": "infrastructure_blocked",
                "summary": "Isolated runtime acceptance raised an unexpected error",
                "logs": [],
                "error_category": "infrastructure_provider_error",
                "retryable": True,
                "actionable": False,
            }
        runtime_required = _runtime_acceptance_required()
        runtime_executed = (
            runtime_acceptance_result.get("enabled") is True
            or runtime_acceptance_result.get("mode") == "local"
            or runtime_acceptance_result.get("source")
            == "local_deterministic_runtime"
        )
        runtime_failed = (
            runtime_required and not runtime_acceptance_result.get("enabled")
        ) or (
            runtime_executed
            and runtime_acceptance_result.get("passed") is not True
        )
        if runtime_failed:
            if not _runtime_failure_actionable(runtime_acceptance_result):
                result["passed"] = False
                result["runtime_acceptance_blocked"] = True
                result["runtime_acceptance_message"] = runtime_acceptance_result.get("summary")
                result["runtime_acceptance_action_required"] = {
                    "options": ["retry_acceptance"]
                }
            else:
                runtime_issue = _runtime_acceptance_issue(runtime_acceptance_result)
                evidence_path = str(runtime_issue.get("file_path") or "")
                repair_path = _runtime_repair_target(
                    pm, evidence_path, str(runtime_issue.get("message") or "")
                )
                if repair_path is None:
                    runtime_issue["file_path"] = ""
                    runtime_issue["needs_manual"] = True
                    runtime_issue["fix_hint"] = (
                        f"{runtime_issue.get('fix_hint', '')} Runtime diagnostics did not "
                        "identify one registered source file; inspect the runtime stack "
                        "before assigning an engineer."
                    ).strip()
                elif repair_path != evidence_path:
                    runtime_issue["message"] = (
                        f"{runtime_issue.get('message', '')}\n"
                        f"Failing test evidence: {evidence_path}"
                    )
                    runtime_issue["file_path"] = repair_path
                    runtime_issue["fix_hint"] = (
                        f"{runtime_issue.get('fix_hint', '')} "
                        "Repair the product implementation; do not weaken or delete the failing test."
                    ).strip()
                layer_issue = {
                    "message": runtime_issue.get("message", ""),
                    "file": runtime_issue.get("file_path", ""),
                    "line": runtime_issue.get("line"),
                    "line_no": runtime_issue.get("line_no"),
                    "evidence": runtime_issue.get("evidence"),
                    "acceptance_criteria": runtime_issue.get("acceptance_criteria"),
                    "rule_id": runtime_issue.get("rule_id"),
                    "symbol": runtime_issue.get("symbol"),
                    "location": runtime_issue.get("location"),
                    "expected": runtime_issue.get("expected"),
                    "actual": runtime_issue.get("actual"),
                    "severity": "error",
                    "fix_hint": runtime_issue.get("fix_hint", ""),
                    "layer": "runtime_acceptance",
                    "verification_spec": {
                        "kind": "runtime_acceptance",
                        "provenance": "deterministic_runtime_acceptance",
                    },
                    "needs_manual": bool(runtime_issue.get("needs_manual")),
                }
                result["passed"] = False
                result["score"] = min(int(result.get("score", 100) or 100), 90)
                result["issues"] = list(result.get("issues") or []) + [
                    layer_issue["message"]
                ]
                result["layer_results"] = list(result.get("layer_results") or []) + [{
                    "layer": "runtime_acceptance",
                    "passed": False,
                    "issues": [layer_issue],
                }]

    prev_entry = ctx.qc_results.get(sp_id, {}).get("qa", {}) if isinstance(ctx.qc_results.get(sp_id, {}), dict) and "qa" in ctx.qc_results.get(sp_id, {}) else ctx.qc_results.get(sp_id, {})
    prev_issues_raw = prev_entry.get("issues_detail", []) if isinstance(prev_entry, dict) else []  # 上次的结构化问题列表
    discovery_passes_used = int(prev_entry.get("discovery_passes_used", 0) or 0)
    allow_discovery = discovery_passes_used < QC_DISCOVERY_PASS_LIMIT

    # 新一轮发现的结构化问题（从 layer_results 中提取）
    # 获取当前阶段 ID，用于 detected_phase 埋点
    _current_phase_id = ""
    try:
        _pm_inst = _phase_managers.get(ctx.project_id)
        if _pm_inst:
            _cur = _pm_inst.get_current_phase()
            if _cur:
                _current_phase_id = _cur.get("phase_id", "")
    except Exception:
        pass

    new_issues_detail: List[Dict] = []
    for lr in result.get("layer_results", []):
        for iss in lr.get("issues", []):
            issue_path = iss.get("file", "")
            owner = (
                find_owner_for_path(pm.file_registry, issue_path)
                if pm and issue_path else {}
            )
            # The reviewed Agent is not necessarily the file owner.  Route the
            # Issue to the durable file-responsibility owner first and use the
            # reviewed Agent only when the finding has no registered owner.
            issue_agent_id = owner.get("agent_id", "") or agent_id
            issue_agent_role = owner.get("agent_role", "") or agent_role
            issue_payload = {
                "message": iss.get("message", ""),
                "file_path": issue_path,
                "line": iss.get("line"),
                "line_no": iss.get("line_no", iss.get("line")),
                "evidence": iss.get("evidence"),
                "acceptance_criteria": iss.get("acceptance_criteria"),
                "rule_id": iss.get("rule_id") or iss.get("rule"),
                "symbol": iss.get("symbol") or iss.get("symbol_name"),
                "location": iss.get("location") or iss.get("endpoint"),
                "expected": iss.get("expected"),
                "actual": iss.get("actual"),
                "responsible_agent_id": issue_agent_id,
                "responsible_agent_role": issue_agent_role,
                "severity": iss.get("severity", "warning"),
                "fix_hint": iss.get("fix_hint", ""),
                "layer": iss.get("layer") or lr.get("layer", ""),
                "verification_spec": iss.get("verification_spec"),
                "status": "needs_manual" if iss.get("needs_manual") else "open",
                "needs_manual_reason": (
                    "Runtime failure could not be mapped to one source owner"
                    if iss.get("needs_manual") else ""
                ),
                "detected_phase": _current_phase_id,   # 埋点：在哪个阶段首次发现
                "detected_at": time.time(),             # 埋点：首次发现时间
            }
            new_issue = canonicalize_issue(
                issue_payload,
                observation_context={
                    "qa_round_id": str((qa_context or {}).get("qa_round_id") or ""),
                    "subproject_id": sp_id,
                },
            )
            if iss.get("needs_manual"):
                new_issue = mark_needs_manual(
                    new_issue,
                    "Runtime failure could not be mapped to one source owner",
                    observation_context={
                        "qa_round_id": str((qa_context or {}).get("qa_round_id") or ""),
                        "subproject_id": sp_id,
                    },
                )
            new_issues_detail.append(new_issue)

    # 新问题和历史问题统一使用 canonical fingerprint；展示文案不参与身份判断。
    if not result.get("passed", False) and not new_issues_detail:
        message = "; ".join(result.get("issues") or []) or result.get("developer_report") or "Quality check failed"
        fallback_issue = {
            "message": message,
            "file_path": "",
            "responsible_agent_id": agent_id,
            "responsible_agent_role": agent_role,
            "severity": "error",
            "fix_hint": result.get("developer_report", "Generate and verify the required deliverables."),
            "layer": "system",
            "status": "open",
            "detected_phase": _current_phase_id,
            "detected_at": time.time(),
        }
        fallback_issue = canonicalize_issue(
            fallback_issue,
            observation_context={
                "qa_round_id": str((qa_context or {}).get("qa_round_id") or ""),
                "subproject_id": sp_id,
            },
        )
        new_issues_detail.append(fallback_issue)

    new_by_fingerprint = {
        _qc_issue_fingerprint(issue): issue for issue in new_issues_detail
    }
    recovered_layers = {
        str(layer.get("layer") or "").strip().lower()
        for layer in result.get("layer_results", [])
        if isinstance(layer, dict) and layer.get("passed") is True
    }
    merged_issues: List[Dict] = []

    MAX_FIX_ROUNDS = QC_ISSUE_REPAIR_ATTEMPT_LIMIT

    for old_iss in prev_issues_raw:
        old_fingerprint = _qc_issue_fingerprint(old_iss)
        old_iss = {**old_iss, "fingerprint": old_fingerprint}
        old_status = old_iss.get("status", "open")
        fix_rounds = old_iss.get("fix_rounds", 0)

        new_match = new_by_fingerprint.get(old_fingerprint)
        found_again = new_match is not None

        if (
            canonicalize_issue(old_iss).get("requires_identity_review")
            and old_status not in {"needs_manual", "pending_verification"}
        ):
            old_layer = str(old_iss.get("layer") or "").strip().lower()
            if (
                not found_again
                and old_layer in recovered_layers
            ):
                merged_issues.append({
                    **old_iss,
                    "status": "fixed",
                    "fixed_at": time.time(),
                    "verification_result": "reviewer_recovered",
                })
                continue
            reviewed = (
                _refresh_issue_routing(old_iss, new_match)
                if new_match else canonicalize_issue(old_iss)
            )
            merged_issues.append({
                **reviewed,
                "status": old_status,
                "identity_review_required": True,
            })
            continue

        # 人工升级仅在同一缺陷仍可复现时保留；消失后也应正常收敛。
        if old_status == "needs_manual":
            merged_issues.append(
                _reconcile_needs_manual_qc_issue(old_iss, new_match)
            )
            continue

        if old_status == "pending_verification":
            merged_issues.append(
                _reconcile_pending_verification_qc_issue(old_iss, new_match)
            )
            continue

        # verified：已人工确认修复，永久保留
        if old_status == "verified":
            if (
                found_again
                and not canonicalize_issue(old_iss).get("requires_identity_review")
            ):
                reopened = _refresh_issue_routing(old_iss, new_match)
                merged_issues.append({
                    **reopened,
                    "status": "open",
                    "reopened_at": time.time(),
                    "verification_result": "reproduced",
                })
            else:
                merged_issues.append(old_iss)
            continue

        if old_status == "fixed":
            if found_again and _should_reopen_fixed_issue(new_match):
                routed_issue = _refresh_issue_routing(old_iss, new_match)
                merged_issues.append({**routed_issue, "status": "open", "fix_rounds": fix_rounds + 1})
            else:
                merged_issues.append(old_iss)

        elif old_status == "fixing":
            if found_again:
                # Agent 修复后质检仍发现，修复失败
                routed_issue = _refresh_issue_routing(old_iss, new_match)
                new_rounds = fix_rounds + 1
                if new_rounds >= MAX_FIX_ROUNDS:
                    merged_issues.append(mark_needs_manual(
                        {**routed_issue, "fix_rounds": new_rounds},
                        f"已尝试修复 {new_rounds} 次仍未解决，请人工检查",
                    ))
                else:
                    merged_issues.append({**routed_issue, "status": "open", "fix_rounds": new_rounds})
            else:
                if canonicalize_issue(old_iss).get("requires_identity_review"):
                    merged_issues.append({
                        **canonicalize_issue(old_iss),
                        "status": "fixing",
                        "identity_review_required": True,
                    })
                else:
                    # 新一轮没发现 → 修复成功，标记 fixed
                    merged_issues.append({**old_iss, "status": "fixed", "fixed_at": time.time()})

        elif old_status == "open":
            if found_again:
                # 仍然存在，更新 fix_hint
                merged_issues.append(_refresh_issue_routing(old_iss, new_match))
            else:
                if canonicalize_issue(old_iss).get("requires_identity_review"):
                    merged_issues.append({
                        **canonicalize_issue(old_iss),
                        "status": "open",
                        "identity_review_required": True,
                    })
                else:
                    merged_issues.append({**old_iss, "status": "fixed", "fixed_at": time.time()})

    # 缺陷基线只在第一轮建立。后续轮次只复检这份账本：
    # 仍出现则保持 open，不再出现则 fixed。禁止每轮追加新的 LLM
    # 表述，确保问题集合单调收敛而不是越检越多。
    old_fingerprint_set = {_qc_issue_fingerprint(i) for i in prev_issues_raw}
    for new_iss in new_issues_detail:
        if _qc_issue_fingerprint(new_iss) not in old_fingerprint_set:
            if _should_append_qc_issue(new_iss, allow_discovery):
                merged_issues.append(new_iss)
            elif str(new_iss.get("severity") or "warning").lower() in {"error", "critical"}:
                merged_issues.append(mark_needs_manual(
                    new_iss,
                    "Two full QC discovery passes completed; transferred to the full-stack engineer",
                ))

    merged_issues = _deduplicate_routed_issues(merged_issues)

    # 统计合并后的问题数
    open_issues = [i for i in merged_issues if i.get("status") == "open"]
    fixing_issues = [i for i in merged_issues if i.get("status") == "fixing"]
    fixed_issues = [i for i in merged_issues if i.get("status") == "fixed"]
    manual_issues = [i for i in merged_issues if i.get("status") == "needs_manual"]
    active_issues = open_issues + fixing_issues + manual_issues
    # Warnings are advisory findings.  They must stay visible in the report,
    # but only hard error/critical findings block acceptance or trigger an
    # automatic repair/rebuild loop.  Treating a single warning as a failure
    # previously forced five pointless repair rounds and a rebuild decision.
    def _severity(issue: Dict[str, Any]) -> str:
        return str(issue.get("severity", "warning")).strip().lower()

    blocking_issues = [
        issue for issue in active_issues if _severity(issue) in {"error", "critical"}
    ]
    merged_passed = not _has_blocking_qc_issues(active_issues)

    # 计算合并后的综合分数（基于活跃问题数量）
    merged_score = max(
        0,
        100
        - len(blocking_issues) * 10
        - len([i for i in active_issues if _severity(i) == "warning"]) * 2,
    )
    observation_round_number = int(prev_entry.get("qc_round", 0) or 0) + 1
    observation_history, observations_detail = _append_qc_observation_history(
        prev_entry if isinstance(prev_entry, dict) else {},
        new_issues_detail,
        qa_context=qa_context,
        qc_round=observation_round_number,
    )
    manual_fingerprints = {
        _qc_issue_fingerprint(issue)
        for issue in manual_issues
    }
    convergence_observations = [
        {
            **copy.deepcopy(issue),
            **(
                {"status": "needs_manual"}
                if _qc_issue_fingerprint(issue) in manual_fingerprints
                else {}
            ),
        }
        for issue in new_issues_detail
    ]
    
    entry = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "project_id": str(ctx.project_id),
        "phase_id": sp_id if phase_info else "",
        "qc_round_id": str((qa_context or {}).get("qa_round_id") or ""),
        "artifact_digest": str((qa_context or {}).get("artifact_digest") or ""),
        "subproject_id": sp_id,
        "subproject_name": sp_name,
        "responsible_agent_id": agent_id,
        "responsible_agent_role": agent_role,
        "passed": merged_passed,
        "score": merged_score,  # 使用合并后的分数，而非本次原始质检分
        "issues": [i.get("message", "") for i in active_issues],  # 字符串列表（兼容旧接口）
        "issues_detail": merged_issues,  # blocker convergence ledger
        # Immutable raw observation for the Supervisor convergence machine.
        # The merged ledger is a UI/history projection and may intentionally
        # suppress later wording changes; it must never define convergence.
        "observed_issues_detail": convergence_observations,
        "observation_history": observation_history,
        "observations_detail": observations_detail,
        "user_report": result.get("user_report", ""),
        "developer_report": result.get("developer_report", ""),
        "layer_results": result.get("layer_results", []),
        "needs_rewrite": result.get("needs_rewrite", False),
        "error_count": len(blocking_issues),
        "warning_count": len([i for i in active_issues if _severity(i) == "warning"]),
        "fixed_count": len(fixed_issues),
        "checked_at": time.time(),
        "qc_round": observation_round_number,
        "discovery_passes_used": min(
            QC_DISCOVERY_PASS_LIMIT, discovery_passes_used + 1,
        ),
        "discovery_pass_limit": QC_DISCOVERY_PASS_LIMIT,
        "status": "passed" if merged_passed else "failed",
        "runtime_acceptance": runtime_acceptance_result,
        "acceptance_observations": copy.deepcopy(
            result.get("acceptance_observations") or []
        ),
        "criteria_results": copy.deepcopy(
            result.get("acceptance_observations") or []
        ),
        "required_context": copy.deepcopy(result.get("required_context") or []),
        "qc_execution_error": result.get("qc_execution_error", ""),
        "reviewer_unavailable": bool(result.get("reviewer_unavailable")),
        "qc_input_invalid": bool(result.get("qc_input_invalid")),
        "review_packet": {
            "schema_version": review_packet.get("schema_version"),
            "artifact_digest": (review_packet.get("project") or {}).get("artifact_digest"),
            "responsibility_ledger_revision": (
                review_packet.get("responsibility") or {}
            ).get("ledger_revision"),
        } if review_packet else None,
        "fix_task": {
            "agent_id": agent_id,
            "agent_role": agent_role,
            "task": "修复质检问题",
            "detail": result.get("developer_report", ""),
            "needs_rewrite": result.get("needs_rewrite", False),
        } if not merged_passed else None,
    }
    if qa_context:
        entry["supervisor_run_id"] = str(qa_context.get("run_id") or "")
        entry["qa_round_id"] = str(qa_context.get("qa_round_id") or "")
        entry["scope_digest"] = str(qa_context.get("scope_digest") or "")

    ctx.qc_results[sp_id] = {"qa": entry}

    # ── 同步质检结果到 Supervisor 成员的 issues 列表 ──────────────────────────
    # 找到负责本子项目所在阶段的 Supervisor 成员，把 merged_issues 的最新状态同步过去
    # 这样前端质检窗口能实时看到问题是否已修复
    try:
        sp_info = next((s for s in ctx.subprojects if s["id"] == sp_id), {})
        phase_id_for_sync = sp_info.get("phase_id", "")
        if phase_id_for_sync and ctx.project_id in _supervisor_leaders:
            sup_leader_inst = _supervisor_leaders[ctx.project_id]
            sup_member = sup_leader_inst.get_member_for_phase(phase_id_for_sync)
            if sup_member:
                # 用 merged_issues 的最新状态更新 sup_member.issues
                # 策略：按 issue id 匹配，更新 status；新问题追加；已不存在的问题保持原状
                existing_by_id = {i.get("id"): i for i in sup_member.issues}
                for m_iss in merged_issues:
                    iss_id = m_iss.get("id")
                    if iss_id and iss_id in existing_by_id:
                        # 更新状态（只允许向前推进：open→fixing→fixed，不允许回退）
                        old_status = existing_by_id[iss_id].get("status", "open")
                        new_status = m_iss.get("status", "open")
                        status_order = {"open": 0, "fixing": 1, "fixed": 2}
                        if status_order.get(new_status, 0) >= status_order.get(old_status, 0):
                            existing_by_id[iss_id]["status"] = new_status
                            if new_status == "fixed":
                                existing_by_id[iss_id]["fixed_at"] = time.time()
                # 如果有新问题（qc 发现但 sup_member.issues 里没有），追加进去
                existing_ids = set(existing_by_id.keys())
                for m_iss in merged_issues:
                    if m_iss.get("id") not in existing_ids and m_iss.get("status") == "open":
                        m_iss_copy = dict(m_iss)
                        m_iss_copy["phase_id"] = phase_id_for_sync
                        sup_member.issues.append(m_iss_copy)
                # 重新判断阶段是否通过
                open_errs = [
                    i for i in sup_member.issues
                    if i.get("status") == "open" and _severity(i) in {"error", "critical"}
                ]
                if len(open_errs) == 0:
                    sup_member.phase_passed = True
    except Exception:
        pass  # 同步失败不影响主流程

    # 如果合并后仍有未解决问题，把修复任务写入 Agent 状态
    if not merged_passed and agent_id and agent_id in ctx.agents:
        from core.agent_lifecycle import transition_agent
        transition_agent(ctx.agents[agent_id], "fix_required", message="Quality check failed")
        ctx.agents[agent_id]["fix_task"] = result.get("developer_report", "")
        ctx.agents[agent_id]["needs_rewrite"] = result.get("needs_rewrite", False)

    return entry

@router.post("/projects/{project_id}/qc/trigger")
async def trigger_quality_checks(project_id: str, subproject_id: str):
    """Delegate one request to its authoritative phase Supervisor run."""
    ctx = _get_project(project_id)
    sp = next((s for s in ctx.subprojects if s["id"] == subproject_id), None)
    phase_id = str((sp or {}).get("phase_id") or subproject_id)
    phase_manager = _phase_managers.get(project_id)
    if not phase_manager or not phase_manager.get_phase(phase_id):
        raise HTTPException(
            status_code=409,
            detail="子项目未绑定 authoritative Supervisor 阶段",
        )
    from api.routes_phases import start_auto_repair
    delegated = await start_auto_repair(project_id, phase_id)
    return {
        "delegated": True,
        "authoritative_gate": "supervisor_quality_run",
        "subproject_id": subproject_id,
        "phase_id": phase_id,
        "run": delegated,
    }

@router.post("/projects/{project_id}/qc/trigger-all")
async def trigger_all_qc(project_id: str):
    """
    触发所有子项目的四层质检（QA/Perf/Sec/UXO）。
    结果持久化到 ctx.qc_results，前端通过 GET /qc/results 读取。
    """
    _get_project(project_id)
    phase_manager = _phase_managers.get(project_id)
    phases = list(phase_manager.phases if phase_manager else [])
    if not phases:
        raise HTTPException(
            status_code=409,
            detail="项目没有 authoritative Supervisor 阶段",
        )
    from api.routes_phases import start_auto_repair
    triggered = []
    for phase in phases:
        phase_id = str(phase.get("phase_id") or "")
        if not phase_id:
            continue
        try:
            delegated = await start_auto_repair(project_id, phase_id)
            triggered.append({
                "phase_id": phase_id,
                "authoritative_gate": "supervisor_quality_run",
                "run": delegated,
            })
        except HTTPException as exc:
            triggered.append({
                "phase_id": phase_id,
                "status": "blocked",
                "status_code": exc.status_code,
                "error": str(exc.detail),
            })
    return {
        "success": True,
        "message": f"已为 {len(triggered)} 个阶段登记 authoritative Supervisor 质检",
        "triggered": triggered,
    }

@router.get("/projects/{project_id}/qc/results")
async def get_all_qc_results(project_id: str):
    """
    获取项目所有质检结果（前端 QAReport 页面使用）
    
    返回格式：
    {
      "subprojects": [
        {
          "id": "SP-001",
          "name": "用户认证模块",
          "checks": {
            "qa":   { "passed": true, "score": 80, "issues": [...] },
            "perf": { "passed": true, "score": 100, ... },
            "sec":  { ... },
            "uxo":  { ... }
          }
        }
      ],
      "summary": { "total": 4, "passed": 4, "failed": 0, "overall_score": 95 }
    }
    """
    ctx = _get_project(project_id)
    result_list = []
    total_checks = 0
    passed_checks = 0
    score_sum = 0

    for sp in ctx.subprojects:
        sp_id = sp["id"]
        sp_checks = ctx.qc_results.get(sp_id, {})
        result_list.append({
            "id": sp_id,
            "name": sp.get("name", sp_id),
            "status": sp.get("status", "pending"),
            "checks": sp_checks,
        })
        # sp_checks 是单层 dict（非嵌套 {qa:{}, perf:{}}），直接当做一个 check 统计
        if isinstance(sp_checks, dict) and "passed" in sp_checks:
            total_checks += 1
            if sp_checks.get("passed", True):
                passed_checks += 1
            score_sum += sp_checks.get("score", 100)
        # 兼容旧嵌套格式 {qa:{passed:bool, score:int}, perf:{...}}
        elif isinstance(sp_checks, dict):
            for check in sp_checks.values():
                if isinstance(check, dict) and "passed" in check:
                    total_checks += 1
                    if check.get("passed", True):
                        passed_checks += 1
                    score_sum += check.get("score", 100)

    overall_score = round(score_sum / total_checks) if total_checks > 0 else 0
    return {
        "project_id": project_id,
        "subprojects": result_list,
        "summary": {
            "total": total_checks,
            "passed": passed_checks,
            "failed": total_checks - passed_checks,
            "overall_score": overall_score,
            "triggered": total_checks > 0,
        }
    }

@router.get("/projects/{project_id}/qc/results/{subproject_id}")
async def get_qc_results(project_id: str, subproject_id: str):
    """获取单个子项目质检结果"""
    ctx = _get_project(project_id)
    sp_checks = ctx.qc_results.get(subproject_id, {})
    return {"subproject_id": subproject_id, "checks": sp_checks}


@router.get(
    "/projects/{project_id}/signoff/status",
    response_model=SignoffResponse,
)
async def get_signoff_status(project_id: str):
    ctx = _get_project(project_id)
    phase_manager = _phase_managers.get(project_id)
    phases = phase_manager.phases if phase_manager else []
    blockers, manifest = _validate_signoff_readiness(
        ctx, phases, project_id=project_id
    )
    if blockers:
        return _signoff_payload(
            passed=False,
            status="blocked",
            manifest=None,
            blockers=blockers,
        )
    return _signoff_payload(
        passed=True,
        status="completed" if ctx.status == "completed" else "ready",
        manifest=manifest,
        blockers=[],
        receipt=getattr(ctx, "signoff_receipt", None),
    )


@router.post(
    "/projects/{project_id}/signoff",
    response_model=SignoffResponse,
    responses={409: {"model": SignoffResponse}},
)
async def sign_off(project_id: str):
    ctx = _get_project(project_id)
    previous_status = ctx.status
    previous_receipt = copy.deepcopy(getattr(ctx, "signoff_receipt", None))
    decision_store = getattr(
        getattr(ctx.supervisor, "dispatcher", None),
        "context",
        None,
    )
    previous_decisions = copy.deepcopy(
        getattr(decision_store, "decisions", None)
    )
    committed_receipt = None
    replayed = False
    try:
        with project_write_guard(project_id, Path(ctx.workspace)):
            phase_manager = _phase_managers.get(project_id)
            phases = phase_manager.phases if phase_manager else []
            blockers, manifest = _validate_signoff_readiness(
                ctx, phases, project_id=project_id
            )
            if blockers:
                return JSONResponse(
                    status_code=409,
                    content=_signoff_payload(
                        passed=False,
                        status="blocked",
                        manifest=None,
                        blockers=blockers,
                    ),
                )
            binding = _signoff_binding(ctx, manifest)
            existing_receipt = getattr(ctx, "signoff_receipt", None)
            if isinstance(existing_receipt, dict):
                if not _signoff_receipt_matches(existing_receipt, binding):
                    return JSONResponse(
                        status_code=409,
                        content=_signoff_payload(
                            passed=False,
                            status="blocked",
                            manifest=None,
                            blockers=[_normalize_signoff_blocker({
                                "code": "SIGNOFF_RECEIPT_STALE",
                                "scope": "project",
                                "message": "Persisted signoff receipt does not match current release evidence",
                                "action": "Restore the signed artifact or perform a new audited release",
                            })],
                            receipt=existing_receipt,
                        ),
                    )
                committed_receipt = copy.deepcopy(existing_receipt)
                replayed = True
                return _signoff_payload(
                    passed=True,
                    status="completed",
                    manifest=manifest,
                    blockers=[],
                    receipt=committed_receipt,
                )
            committed_receipt = _new_signoff_receipt(ctx, manifest)
            result = _signoff_payload(
                passed=True,
                status="completed",
                manifest=manifest,
                blockers=[],
                receipt=committed_receipt,
            )
            add_decision = getattr(decision_store, "add_decision", None)
            if callable(add_decision):
                add_decision(
                    "项目签核",
                    f"项目 {project_id} 全部完成，签核通过",
                )
            ctx.status = "completed"
            ctx.signoff_receipt = copy.deepcopy(committed_receipt)
            try:
                await _persist_all_async()
            except Exception:
                ctx.status = previous_status
                ctx.signoff_receipt = copy.deepcopy(previous_receipt)
                if (
                    decision_store is not None
                    and previous_decisions is not None
                ):
                    decision_store.decisions = previous_decisions
                raise
    except ProjectWriteFenceConflict as exc:
        return JSONResponse(
            status_code=409,
            content=_signoff_payload(
                passed=False,
                status="blocked",
                manifest=None,
                blockers=[_normalize_signoff_blocker({
                    "code": "SIGNOFF_CONFLICT",
                    "scope": "project",
                    "message": str(exc),
                    "action": "等待当前写入完成后重试 Signoff",
                })],
            ),
        )
    if committed_receipt is not None and not replayed:
        try:
            from api import websocket
            await websocket.manager.broadcast(
                project_id,
                "project.signoff.completed",
                copy.deepcopy(committed_receipt),
            )
        except Exception:
            logger.exception(
                "Signoff committed but advisory broadcast failed project=%s receipt=%s",
                project_id,
                committed_receipt.get("receipt_id"),
            )
    return result

@router.post("/projects/{project_id}/supervisor/sync-pm-plan")
async def sync_pm_plan_to_supervisor(project_id: str):
    """
    将 PM Agent 的规划方案同步给 Supervisor Agent。
    调用时机：PM 完成规划后、项目启动后。
    Supervisor 收到方案后，后续对话中会知晓整体项目结构。
    """
    ctx = _get_project(project_id)

    # 收集 PM 规划内容
    plan_parts = []
    if ctx.pm.current_plan:
        plan_parts.append(f"【规划书】\n{str(ctx.pm.current_plan)[:2000]}")
    if ctx.pm.subprojects:
        sp_lines = []
        for sp in ctx.pm.subprojects:
            sp_lines.append(
                f"- {sp.get('id','')}: {sp.get('name','')} — {sp.get('description','')[:100]}"
                f"（技术栈：{', '.join(sp.get('tech_stack',[]))}）"
            )
        plan_parts.append("【子项目列表】\n" + "\n".join(sp_lines))
    if ctx.pm.context_summary:
        plan_parts.append(f"【PM 对话摘要】\n{ctx.pm.context_summary[:1000]}")

    if not plan_parts:
        return {"success": False, "message": "PM Agent 尚未生成规划，请先与 PM Agent 完成需求分析"}

    plan_summary = "\n\n".join(plan_parts)
    ctx.supervisor.set_pm_plan(plan_summary)

    return {
        "success": True,
        "message": "PM 规划方案已同步给 Supervisor Agent",
        "plan_length": len(plan_summary),
    }
