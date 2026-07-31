"""杂项路由"""

import asyncio
import time
import json
import uuid
import os
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from starlette.responses import StreamingResponse
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from api.routes_execution import execution_status

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



router = APIRouter(tags=["misc"])





@router.post("/projects/{project_id}/changes")

async def process_change(project_id: str, request: ChangeRequest):

    ctx = _get_project(project_id)

    change = {"id": f"change-{uuid.uuid4().hex[:6]}", "type": request.change_type,

              "description": request.description, "affected_subprojects": request.affected_subprojects}

    return ctx.ccb.process_change(change)



@router.get("/projects/{project_id}/changes/pending")

async def get_pending_changes(project_id: str):

    ctx = _get_project(project_id)

    return {"pending": ctx.ccb.get_pending_decisions()}


# SSE 并发连接限制（防止连接耗尽，默认50）
_sse_connections: int = 0
_MAX_SSE_CONNECTIONS = int(os.environ.get("MAX_SSE_CONNECTIONS", "50"))


@router.get("/projects/{project_id}/progress/stream")

async def stream_project_progress(project_id: str):

    """

    SSE 实时进度流：每 2 秒推送一次项目整体进度快照。

    前端用 EventSource 订阅，界面自动更新。

    最大并发连接数限制：{_MAX_SSE_CONNECTIONS}

    事件格式：

      data: {"agents": [...], "subprojects": [...], "qc_results": {...}, "status": "running", "timestamp": 1234}

    """

    global _sse_connections
    if _sse_connections >= _MAX_SSE_CONNECTIONS:
        raise HTTPException(status_code=429, detail=f"SSE 连接数已达上限 {_MAX_SSE_CONNECTIONS}，请稍后重试")
    _sse_connections += 1

    ctx = _get_project(project_id)



    async def event_generator():
        global _sse_connections
        try:

            while True:

                try:

                    # 汇总所有 Agent 状态

                    agents_snapshot = []

                    for agent_id, info in ctx.agents.items():

                        exec_info = execution_status.get(agent_id, {})

                        agents_snapshot.append({

                            "id": agent_id,

                            "role": info.get("role", ""),

                            "subproject_id": info.get("subproject_id", ""),

                            "subproject_name": info.get("subproject_name", ""),

                            "status": exec_info.get("status") or info.get("status", "idle"),

                            "progress": exec_info.get("progress", 0),

                            "output_files": exec_info.get("output_files", info.get("output_files", [])),

                            "fix_required": info.get("status") == "fix_required",

                            "fix_task": info.get("fix_task", ""),

                            "needs_rewrite": info.get("needs_rewrite", False),

                            "last_log": exec_info.get("logs", [""])[-1] if exec_info.get("logs") else "",

                        })



                    # 子项目状态

                    sp_snapshot = [

                        {

                            "id": sp["id"],

                            "name": sp.get("name", ""),

                            "status": sp.get("status", "pending"),

                            "progress": sp.get("progress", 0),

                            "agent_id": sp.get("agent_id", ""),

                        }

                        for sp in ctx.subprojects

                    ]



                    # 质检摘要

                    qc_summary = {}

                    for sp_id, qc in ctx.qc_results.items():

                        qc_summary[sp_id] = {

                            "passed": qc.get("passed", False),

                            "score": qc.get("score", 0),

                            "status": qc.get("status", "pending"),

                            "error_count": qc.get("error_count", 0),

                            "warning_count": qc.get("warning_count", 0),

                            "responsible_agent_id": qc.get("responsible_agent_id", ""),

                            "needs_rewrite": qc.get("needs_rewrite", False),

                        }



                    payload = json.dumps({

                        "project_id": project_id,

                        "project_status": ctx.status,

                        "agents": agents_snapshot,

                        "subprojects": sp_snapshot,

                        "qc_summary": qc_summary,

                        "timestamp": time.time(),

                    }, ensure_ascii=False)



                    yield f"data: {payload}\n\n"

                except Exception as e:

                    yield f"data: {json.dumps({'error': str(e)})}\n\n"



                await asyncio.sleep(2)
        finally:
            _sse_connections -= 1



    return StreamingResponse(

        event_generator(),

        media_type="text/event-stream",

        headers={

            "Cache-Control": "no-cache",

            "X-Accel-Buffering": "no",

            "Connection": "keep-alive",

        },

    )



@router.post("/ccb/check-delete-member")

async def ccb_check_delete_member(request: CCBCheckDeleteMemberRequest):

    """

    CCB 删除成员保护检查。

    返回：

      - allowed=True：可以直接删除

      - allowed=False, require_confirm=True：需要二次确认（返回 confirm_token）

      - allowed=False, require_confirm=False：拒绝删除（返回原因）

    """

    from agents.ccb_agent import CCBAgent as _CCB

    ccb: _CCB = global_ccb_agent  # type: ignore

    result = ccb.check_delete_member(

        team_type=request.team_type,

        member_id=request.member_id,

        member_name=request.member_name,

        is_leader=request.is_leader,

        current_count=request.current_count,

        is_in_project=request.is_in_project,

    )

    return result



@router.post("/ccb/check-delete-expert")

async def ccb_check_delete_expert(request: CCBCheckDeleteExpertRequest):

    """CCB 删除专家保护检查"""

    from agents.ccb_agent import CCBAgent as _CCB

    ccb: _CCB = global_ccb_agent  # type: ignore

    result = ccb.check_delete_expert(

        expert_id=request.expert_id,

        expert_name=request.expert_name,

        is_in_project=request.is_in_project,

        current_project_name=request.current_project_name,
        current_count=request.current_count,

    )

    return result



@router.post("/ccb/confirm-delete")

async def ccb_confirm_delete(request: CCBConfirmDeleteRequest):

    """CCB 二次确认删除（用 confirm_token 验证）"""

    from agents.ccb_agent import CCBAgent as _CCB

    ccb: _CCB = global_ccb_agent  # type: ignore

    result = ccb.confirm_delete_member(confirm_token=request.confirm_token)

    return result