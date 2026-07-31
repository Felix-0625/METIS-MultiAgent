"""团队管理路由"""
import asyncio
import time
import json
import logging
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
    ProjectTeamAssignRequest,
)

router = APIRouter(tags=["team"])


@router.post("/team/pm/add-member")
async def add_pm_member():
    """
    PM 团队新增成员。
    新成员能力与现有成员相同（system prompt 与组长一致），但记忆完全独立。
    """
    from agents.pm_team import PMTeam
    team: PMTeam = global_pm_team  # type: ignore
    new_member = team.add_member()
    # 同步到员工池（使用 create_employee + EmployeeProfile）
    from core.global_agent_pool import get_global_agent_pool, EmployeeProfile
    pool = get_global_agent_pool()
    profile = EmployeeProfile(
        employee_id=new_member.agent_id,
        name=new_member.name,
        role="PM 成员",
        agent_type="pm",
        department="pm_team",
        avatar="📋",
        role_description=(
            "PM 团队成员，负责阶段任务的细化规划，与用户追问确认，"
            "确定本阶段需要哪些专家实现哪些具体功能，输出可执行的阶段规划。"
        ),
        working_style="细致、追问到位、以验收标准为导向",
        communication_style="简洁直接，不超过 400 字",
        domains=["项目管理", "需求分析", "阶段规划"],
        skills=["需求分析", "阶段规划", "任务分解", "专家需求输出", "用户确认"],
        behavior_rules=[
            "不明确的需求直接追问，不猜测",
            "专家需求要具体（前端专家/后端专家/数据库专家等）",
            "每个任务必须有验收标准",
            "用户确认后在回复末尾加上【阶段规划已确认】",
        ],
        output_format="阶段规划（专家需求清单 + 任务描述 + 验收标准）",
        status="available",
    )
    pool.create_employee(profile)
    await _persist_all_async()
    return {"success": True, "name": new_member.name, "agent_id": new_member.agent_id}

@router.post("/team/supervisor/add-member")
async def add_supervisor_member():
    """
    Supervisor 团队新增成员。
    新成员能力与现有成员相同（system prompt 与组长一致），但记忆完全独立。
    """
    from agents.supervisor_team import SupervisorTeam
    team: SupervisorTeam = global_supervisor_team  # type: ignore
    new_member = team.add_member()
    from core.global_agent_pool import get_global_agent_pool, EmployeeProfile
    pool = get_global_agent_pool()
    profile = EmployeeProfile(
        employee_id=new_member.agent_id,
        name=new_member.name,
        role="Supervisor 成员",
        agent_type="supervisor",
        department="supervisor_team",
        avatar="🔍",
        role_description=(
            "Supervisor 团队成员，负责动态监督某阶段的代码质量。"
            "检查语法/逻辑问题，检查是否完成阶段任务，有问题直接反馈给对应专家（不生成建议）。"
        ),
        working_style="严格、客观、动态追踪，不放过任何问题",
        communication_style="问题导向、精确，只报告具体问题",
        domains=["质量保证", "代码审查", "阶段监督"],
        skills=["代码审查", "质量检查", "任务验收", "问题追踪", "阶段监督"],
        behavior_rules=[
            "只报告具体问题，不生成修改建议",
            "每个问题必须指明：文件路径、问题描述、严重程度",
            "有问题自动反馈给对应专家，不自行消化",
            "全部问题修复后输出「质检通过，可以进入下一阶段」",
        ],
        output_format="JSON 格式问题列表：{passed, issues:[{file_path, message, severity}]}",
        status="available",
    )
    pool.create_employee(profile)
    await _persist_all_async()
    return {"success": True, "name": new_member.name, "agent_id": new_member.agent_id}

@router.delete("/team/pm/member/{member_id}")
async def remove_pm_member(member_id: str):
    """PM 团队删除成员（需先通过 CCB 检查）"""
    from agents.pm_team import PMTeam
    team: PMTeam = global_pm_team  # type: ignore
    result = team.remove_member(member_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "成员不存在或无法删除"))
    # 同步从员工池删除
    from core.global_agent_pool import get_global_agent_pool
    get_global_agent_pool().delete_employee(member_id)
    return {"success": True, "message": result.get("message", "")}

@router.delete("/team/supervisor/member/{member_id}")
async def remove_supervisor_member(member_id: str):
    """Supervisor 团队删除成员（需先通过 CCB 检查）"""
    from agents.supervisor_team import SupervisorTeam
    team: SupervisorTeam = global_supervisor_team  # type: ignore
    result = team.remove_member(member_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "成员不存在或无法删除"))
    # 同步从员工池删除
    from core.global_agent_pool import get_global_agent_pool
    get_global_agent_pool().delete_employee(member_id)
    return {"success": True, "message": result.get("message", "")}

#
# /engineer/{project_id}/...
# 每个项目对应一个 FullStackEngineerAgent 实例（全局缓存）
# 职责：代码整改 / 文档生成 / 文件归档 / 项目问答

from agents.fullstack_engineer_agent import FullStackEngineerAgent

# 全局缓存：project_id → FullStackEngineerAgent
_engineer_agents: Dict[str, FullStackEngineerAgent] = {}

def _build_engineer_context(project_id: str, agent: "FullStackEngineerAgent") -> None:
    """
    向全能工程师注入完整项目知识：
      1. PM 组长 final_plan（完整规划 + 阶段任务）
      2. 子项目列表（名称/描述/技术栈/状态）
      3. 质检缺陷汇总（跨所有子项目的 qc_results）
      4. 阶段详细任务（来自 PhaseManager）
    后端重启后可通过 POST /engineer/{id}/load-context 重新注入。
    """
    ctx = _get_project(project_id)

    # ── 1. final_plan ────────────────────────────────────────────────────────
    plan: Optional[Dict] = None
    if project_id in _pm_teams:
        leader = _pm_teams[project_id]
        plan = getattr(leader, "final_plan", None) or getattr(leader, "draft_plan", None)

    # ── 2. 子项目列表（直接来自 ProjectContext，最新状态）────────────────────
    subprojects: List[Dict] = list(ctx.subprojects) if ctx.subprojects else []

    # ── 3. 阶段详细任务（来自 PhaseManager，含子任务和文件分配）──────────────
    phase_info: List[Dict] = []
    if project_id in _phase_managers:
        pm = _phase_managers[project_id]
        try:
            raw_phases = pm.get_all_phases() if hasattr(pm, "get_all_phases") else []
            for ph in raw_phases:
                phase_info.append({
                    "phase_id":    ph.get("id") or ph.get("phase_id", ""),
                    "name":        ph.get("name", ""),
                    "description": ph.get("description", ""),
                    "status":      ph.get("status", ""),
                    "tasks":       ph.get("tasks", []) or ph.get("subprojects", []),
                    "locked_tasks": ph.get("locked_tasks", []),
                    "acceptance_criteria": ph.get("acceptance_criteria", []),
                    "dependencies": ph.get("dependencies", []),
                    "source_ids": ph.get("source_ids", []),
                    "required_deliverables": ph.get("required_deliverables", []),
                })
        except Exception:
            pass
    # fallback：从 final_plan 里读
    if not phase_info and plan:
        phase_info = plan.get("phases", [])

    # ── 4. 质检缺陷汇总（跨所有子项目，聚合未解决问题）─────────────────────
    qc_results: Dict = {}
    subproject_names = {
        str(sp.get("id") or ""): str(sp.get("name") or sp.get("id") or "")
        for sp in subprojects
    }
    # Include every current ledger scope, especially __whole_project__ Final QA.
    for scope_id, stored_qc in ctx.qc_results.items():
        if not isinstance(stored_qc, dict):
            continue
        layers = (
            {"qa": stored_qc}
            if "issues_detail" in stored_qc
            else stored_qc
        )
        all_issues: List[Dict] = []
        latest_entry: Dict = {}
        for layer_val in layers.values():
            if not isinstance(layer_val, dict):
                continue
            all_issues.extend(
                item for item in layer_val.get("issues_detail", [])
                if isinstance(item, dict)
            )
            if float(layer_val.get("checked_at") or 0) >= float(
                latest_entry.get("checked_at") or 0
            ):
                latest_entry = layer_val
        if all_issues or latest_entry:
            qc_results[scope_id] = {
                **latest_entry,
                "subproject_id": scope_id,
                "subproject_name": (
                    "全项目 Final QA"
                    if scope_id == "__whole_project__"
                    else subproject_names.get(scope_id, scope_id)
                ),
                "issues_detail": all_issues,
            }

    # Refresh on every critical Engineer operation even if PM state has not
    # been rehydrated yet; stale Agent context is never safer than an explicit
    # minimal project snapshot.
    effective_plan = plan or {
        "project_overview": ctx.description or "",
        "core_features": [],
        "tech_stack": {},
        "phases": phase_info,
    }
    agent.load_project_context(
        final_plan=effective_plan,
        phase_info=phase_info,
        subprojects=subprojects,
        qc_results=qc_results,
    )

def _get_engineer(project_id: str) -> FullStackEngineerAgent:
    """获取或创建项目的全能工程师 Agent，首次创建时注入完整项目知识"""
    if project_id not in _engineer_agents:
        ctx = _get_project(project_id)
        agent = FullStackEngineerAgent(
            hermes_client=hermes_client,
            project_id=project_id,
            workspace=str(ctx.workspace),
        )
        _build_engineer_context(project_id, agent)
        _engineer_agents[project_id] = agent
    return _engineer_agents[project_id]

