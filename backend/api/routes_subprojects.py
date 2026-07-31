"""子项目管理路由"""
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

router = APIRouter(tags=["subprojects"])


@router.post("/projects/{project_id}/subprojects/assign")
async def assign_subproject(project_id: str, request: SubprojectRequest):
    ctx = _get_project(project_id)
    if request.agent_id and request.agent_id not in ctx.agents:
        raise HTTPException(status_code=400, detail=f"Agent {request.agent_id} 不属于本项目")
    if request.agent_id:
        for sp in ctx.subprojects:
            if sp.get("agent_id") == request.agent_id and sp["id"] != request.id:
                raise HTTPException(status_code=400, detail=f"Agent {request.agent_id} 已是子项目 {sp['id']} 的负责人")
    existing = next((sp for sp in ctx.subprojects if sp["id"] == request.id), None)
    if existing:
        existing.update({"agent_id": request.agent_id, "name": request.name, "description": request.description})
    else:
        ctx.subprojects.append({
            "id": request.id, "name": request.name, "description": request.description,
            "agent_id": request.agent_id, "status": "pending", "progress": 0, "created_at": time.time(),
        })
    await _persist_all_async()
    return {"success": True, "subprojects": ctx.subprojects}

@router.get("/projects/{project_id}/subprojects/list")
async def list_subprojects(project_id: str):
    ctx = _get_project(project_id)
    result = []
    for sp in ctx.subprojects:
        sp_info = dict(sp)
        if sp.get("agent_id") and sp["agent_id"] in ctx.agents:
            sp_info["agent"] = ctx.agents[sp["agent_id"]]
        result.append(sp_info)
    return {"subprojects": result}

