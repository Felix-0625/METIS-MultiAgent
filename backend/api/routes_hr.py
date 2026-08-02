"""HR 路由"""
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

router = APIRouter(tags=["hr"])


@router.post("/projects/{project_id}/team/build")
async def build_team(project_id: str):
    ctx = _get_project(project_id)
    # PMAgent 使用 current_plan 存储规划书，而非 plan
    if not ctx.pm.current_plan:
        raise HTTPException(status_code=400, detail="请先生成规划书")
    result = ctx.hr.build_team(ctx.pm.current_plan)
    for agent_id, info in ctx.hr.employees.items():
        ctx.agents[agent_id] = {**info, "source": "hr_employee"}
    for agent_id, info in ctx.hr.leads.items():
        ctx.agents[agent_id] = {**info, "source": "hr_lead"}
    ctx.status = "initializing"
    await _persist_all_async()
    return result

@router.post("/projects/{project_id}/agents")
async def create_agent(project_id: str, request: AgentCreateRequest):
    ctx = _get_project(project_id)
    agent_id = f"agent-{uuid.uuid4().hex[:6]}"
    authorized_skills = []
    for skill_name in request.skills:
        results = global_sm_agent.search_skills(skill_name)
        if results:
            authorized_skills.append(results[0]["id"])
    # api_config 在创建时为空，后续可通过 PUT /agents/{agent_id}/config 单独配置
    agent_info = {
        "id": agent_id,
        "role": request.role,
        "skills": authorized_skills,
        "skill_names": request.skills,
        "subproject_id": request.subproject_id,
        "status": "idle",
        "created_at": time.time(),
        "project_id": project_id,
    }
    ctx.agents[agent_id] = agent_info
    await _persist_all_async()
    return {"agent_id": agent_id, "agent": agent_info}

@router.get("/projects/{project_id}/agents")
async def list_agents(project_id: str):
    ctx = _get_project(project_id)
    from core.expert_pool import get_expert_pool
    expert_pool = get_expert_pool(str(getattr(ctx, "owner_user_id", "") or ""))
    # 附加 API 配置信息（隐藏 key）
    result = []
    for a in ctx.agents.values():
        a_copy = dict(a)
        if not a_copy.get("skill_names") and a_copy.get("expert_id"):
            profile = expert_pool.get_expert(str(a_copy["expert_id"]))
            if profile:
                a_copy["skill_names"] = [skill.name for skill in profile.skills]
                a_copy["skills"] = list(profile.skill_ids)
        cfg = agents_api_config.get(a["id"], {})
        a_copy["api_config"] = {k: ("*****" if k == "api_key" and v else v) for k, v in cfg.items()}
        a_copy["has_custom_api"] = bool(cfg)
        result.append(a_copy)
    return {"agents": result}

@router.get("/projects/{project_id}/team/status")
async def get_team_status(project_id: str):
    """获取团队状态 - P0修复：增加空值检查防止500错误"""
    ctx = _get_project(project_id)
    try:
        # P0修复：确保HR Agent存在且方法可用
        if not hasattr(ctx, 'hr') or ctx.hr is None:
            return {
                "status": "not_initialized",
                "message": "HR Agent尚未初始化",
                "employees": {},
                "leads": {},
                "team_size": 0
            }
        
        # 调用HR方法
        if hasattr(ctx.hr, 'get_team_status'):
            team_status = ctx.hr.get_team_status()
            # 确保返回值有效
            if team_status is None:
                return {
                    "status": "empty",
                    "message": "团队状态为空",
                    "employees": {},
                    "leads": {},
                    "team_size": 0
                }
            return team_status
        else:
            # HR Agent没有get_team_status方法，返回基础信息
            return {
                "status": "basic",
                "employees": getattr(ctx.hr, 'employees', {}),
                "leads": getattr(ctx.hr, 'leads', {}),
                "team_size": len(getattr(ctx.hr, 'employees', {})) + len(getattr(ctx.hr, 'leads', {}))
            }
    except Exception as e:
        logger.error(f"获取团队状态失败: {e}", exc_info=True)
        # 返回错误信息而非抛出500
        return {
            "status": "error",
            "message": f"获取团队状态失败: {str(e)}",
            "employees": {},
            "leads": {},
            "team_size": 0
        }

@router.post("/projects/{project_id}/hr/reassign")
async def hr_reassign_tasks(project_id: str, request: HRReassignRequest):
    """
    HR Agent 重新分工：
    - 根据变更描述，更新受影响子项目的任务描述
    - 将修复/新增任务分配给对应的执行 Agent
    - 更新 Agent 状态为 fix_required 或 working
    """
    ctx = _get_project(project_id)

    reassigned = []
    for sp_id in request.affected_subproject_ids:
        sp = next((s for s in ctx.subprojects if s["id"] == sp_id), None)
        if not sp:
            continue

        agent_id = sp.get("agent_id", "")
        if not agent_id or agent_id not in ctx.agents:
            continue

        # 更新子项目描述（追加变更说明）
        original_desc = sp.get("description", "")
        sp["description"] = (
            original_desc + f"\n\n【变更要求 {time.strftime('%H:%M:%S')}】\n{request.change_description}"
        )
        sp["status"] = "in_progress"
        sp["progress"] = 0

        # 更新 Agent 状态
        ctx.agents[agent_id]["status"] = "fix_required"
        ctx.agents[agent_id]["fix_task"] = (
            f"根据用户变更要求重新开发：\n{request.change_description}\n\n"
            f"原任务：{original_desc[:200]}"
        )
        ctx.agents[agent_id]["needs_rewrite"] = len(request.change_description) > 100  # 大改直接重写

        reassigned.append({
            "subproject_id": sp_id,
            "subproject_name": sp.get("name", ""),
            "agent_id": agent_id,
            "agent_role": ctx.agents[agent_id].get("role", ""),
            "needs_rewrite": ctx.agents[agent_id]["needs_rewrite"],
        })

    # 处理新增任务
    new_agents = []
    for new_task in (request.new_tasks or []):
        agent_id = f"agent-{uuid.uuid4().hex[:6]}"
        sp_id = new_task.get("subproject_id") or f"sp-new-{uuid.uuid4().hex[:4]}"
        agent_info = {
            "id": agent_id,
            "role": new_task.get("role", "开发工程师"),
            "skills": [],
            "skill_names": [],
            "subproject_id": sp_id,
            "subproject_name": new_task.get("name", "新增任务"),
            "status": "fix_required",
            "fix_task": new_task.get("description", ""),
            "created_at": time.time(),
            "project_id": project_id,
            "source": "hr_reassign",
        }
        ctx.agents[agent_id] = agent_info
        # 确保子项目存在
        if not any(s["id"] == sp_id for s in ctx.subprojects):
            ctx.subprojects.append({
                "id": sp_id,
                "name": new_task.get("name", "新增任务"),
                "description": new_task.get("description", ""),
                "agent_id": agent_id,
                "status": "in_progress",
                "progress": 0,
                "created_at": time.time(),
            })
        new_agents.append(agent_info)

    await _persist_all_async()

    return {
        "success": True,
        "message": f"HR 已重新分工：{len(reassigned)} 个子项目更新，{len(new_agents)} 个新 Agent 创建",
        "reassigned": reassigned,
        "new_agents": new_agents,
    }

