"""员工池路由"""
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

router = APIRouter(tags=["employee"])


@router.get("/agents")
async def list_all_agents():
    """
    员工池：按项目列出所有 Agent
    每个项目包含：核心 Agent（PM/HR/Supervisor/PG/CCB）+ HR 创建的执行 Agent
    """
    result = []
    for project_id, ctx in projects.items():
        # 核心 Agent
        core = []
        for ca in ctx._core_agents_info():
            cfg = agents_api_config.get(ca["id"], {})
            ca["api_config"] = {k: ("*****" if k == "api_key" and v else v) for k, v in cfg.items()}
            ca["has_custom_api"] = bool(cfg)
            # skill_names 已由 _core_agents_info() 填充，不覆盖
            ca["project_id"] = project_id
            ca["project_name"] = ctx.name
            core.append(ca)

        # 执行 Agent（HR 创建）
        exec_agents = []
        for agent_id, agent_info in ctx.agents.items():
            a = dict(agent_info)
            a["project_id"] = project_id
            a["project_name"] = ctx.name
            a["is_core"] = False
            cfg = agents_api_config.get(agent_id, {})
            a["api_config"] = {k: ("*****" if k == "api_key" and v else v) for k, v in cfg.items()}
            a["has_custom_api"] = bool(cfg)
            exec_agents.append(a)

        result.append({
            "project_id": project_id,
            "project_name": ctx.name,
            "project_status": ctx.status,
            "core_agents": core,
            "exec_agents": exec_agents,
        })

    return {"projects": result, "total_projects": len(result)}

@router.get("/agents/{agent_id}/config")
async def get_agent_config(agent_id: str):
    """获取 Agent 的 API 配置（key 脱敏）"""
    cfg = agents_api_config.get(agent_id, {})
    display = {k: ("*****" if k == "api_key" and v else v) for k, v in cfg.items()}
    return {"agent_id": agent_id, "config": display, "using_default": not bool(cfg)}

@router.put("/agents/{agent_id}/config")
async def update_agent_config(agent_id: str, request: AgentApiConfigRequest):
    """
    配置 Agent 独立 API
    - 每个 Agent 可以使用不同的模型/endpoint/key
    - 记忆独立（通过 agent_id 隔离）
    - 不填则继承默认配置
    """
    # 找到该 Agent 所属项目（执行 Agent 或核心 Agent 均可）
    found = False
    for ctx in projects.values():
        core_ids = [a["id"] for a in ctx._core_agents_info()]
        if agent_id in ctx.agents or agent_id in core_ids:
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} 不存在")

    cfg = agents_api_config.get(agent_id, {})
    if request.model is not None:
        cfg["model"] = request.model
    if request.api_base is not None:
        cfg["api_base"] = request.api_base
    if request.api_key is not None:
        cfg["api_key"] = request.api_key
    if request.max_tokens is not None:
        cfg["max_tokens"] = request.max_tokens
    if request.temperature is not None:
        cfg["temperature"] = request.temperature

    agents_api_config[agent_id] = cfg
    await _persist_all_async()
    return {"success": True, "agent_id": agent_id, "config_keys": list(cfg.keys())}

@router.delete("/agents/{agent_id}/config")
async def reset_agent_config(agent_id: str):
    """重置 Agent API 配置为默认"""
    if agent_id in agents_api_config:
        del agents_api_config[agent_id]
        await _persist_all_async()
    return {"success": True, "message": "已重置为默认 API 配置"}

from core.global_agent_pool import GlobalAgentPool, EmployeeProfile, get_global_agent_pool

def _get_global_pool() -> GlobalAgentPool:
    return get_global_agent_pool()

@router.get("/employee-pool")
async def list_employees(
    agent_type: Optional[str] = None,
    department: Optional[str] = None,
    status: Optional[str] = None,
):
    pool = _get_global_pool()
    employees = pool.list_employees(agent_type=agent_type, department=department, status=status)
    return {"employees": [e.to_dict() for e in employees], "total": len(employees), "stats": pool.get_stats()}

@router.get("/employee-pool/stats")
async def get_employee_pool_stats():
    return _get_global_pool().get_stats()

@router.get("/employee-pool/default-team")
async def get_default_project_team():
    grouped = _get_global_pool().get_default_project_team()
    return {dept: [e.to_dict() for e in emps] for dept, emps in grouped.items()}

@router.post("/employee-pool")
async def create_employee(request: EmployeeCreateRequest):
    pool = _get_global_pool()
    profile = EmployeeProfile(**request.model_dump())
    pool.create_employee(profile)
    return {"success": True, "employee": profile.to_dict()}

@router.get("/employee-pool/{employee_id}")
async def get_employee(employee_id: str):
    emp = _get_global_pool().get_employee(employee_id)
    if not emp:
        raise HTTPException(status_code=404, detail=f"员工 {employee_id} 不存在")
    return emp.to_dict()

@router.patch("/employee-pool/{employee_id}")
async def update_employee(employee_id: str, request: EmployeeUpdateRequest):
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    emp = _get_global_pool().update_employee(employee_id, updates)
    if not emp:
        raise HTTPException(status_code=404, detail=f"员工 {employee_id} 不存在")
    return {"success": True, "employee": emp.to_dict()}

@router.delete("/employee-pool/{employee_id}")
async def delete_employee(employee_id: str):
    if not _get_global_pool().delete_employee(employee_id):
        raise HTTPException(status_code=404, detail=f"员工 {employee_id} 不存在")
    return {"success": True}

@router.post("/employee-pool/match")
async def match_employees(body: Dict):
    results = _get_global_pool().match_for_role(
        agent_type=body.get("agent_type", "pg"),
        required_skills=body.get("required_skills", []),
        required_domains=body.get("required_domains", []),
        top_k=body.get("top_k", 3),
        exclude_busy=body.get("exclude_busy", False),
    )
    return {"matches": results, "count": len(results)}

@router.get("/projects/{project_id}/team")
async def get_project_team(project_id: str):
    _get_project(project_id)
    team = _get_global_pool().get_project_team(project_id)
    return {"project_id": project_id, "team": [e.to_dict() for e in team], "count": len(team)}

@router.post("/projects/{project_id}/team/assign")
async def assign_project_team(project_id: str, request: ProjectTeamAssignRequest):
    _get_project(project_id)
    pool = _get_global_pool()
    assigned, not_found = [], []
    for emp_id in request.employee_ids:
        emp = pool.get_employee(emp_id)
        if not emp:
            not_found.append(emp_id)
            continue
        pool.assign_to_project(emp_id, project_id)
        assigned.append(emp.to_dict())
    await _persist_all_async()
    return {"success": True, "project_id": project_id, "assigned": assigned, "not_found": not_found, "count": len(assigned)}

@router.post("/projects/{project_id}/team/release")
async def release_project_team(project_id: str, body: Dict):
    _get_project(project_id)
    pool = _get_global_pool()
    emp_ids = body.get("employee_ids", [])
    if not emp_ids:
        emp_ids = [e.employee_id for e in pool.get_project_team(project_id)]
    released = [emp_id for emp_id in emp_ids if pool.release_from_project(emp_id, project_id)]
    await _persist_all_async()
    return {"success": True, "released": released, "count": len(released)}

@router.post("/employee-pool/reset-presets")
async def reset_employee_presets():
    pool = _get_global_pool()
    pool._employees.clear()
    pool._init_presets()
    return {"success": True, "count": len(pool._employees), "message": f"已重置，共 {len(pool._employees)} 名预置员工"}

