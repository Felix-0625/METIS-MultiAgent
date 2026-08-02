"""MCP 路由 — 需要认证的 API（除 tools/list 外）"""
import asyncio
import time
import json
import logging
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from core.auth import (
    get_current_user, get_mcp_user, create_mcp_token, revoke_mcp_token,
    get_mcp_token_status, create_named_mcp_token, list_mcp_tokens,
    reveal_mcp_token, revoke_named_mcp_token, UserModel,
)
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

router = APIRouter(tags=["mcp"])


class MCPTokenCreateRequest(BaseModel):
    name: str


class MCPTokenRevealRequest(BaseModel):
    password: str


@router.post("/mcp")
async def mcp_endpoint(
    body: Dict,
    current_user: UserModel = Depends(get_mcp_user),
):
    """
    MCP JSON-RPC 2.0 端点（需要认证）

    支持 Trae / Cursor / Claude Code / OpenAI / Hermes 等 MCP 客户端接入。

    支持的方法：
    - initialize       协议握手
    - ping             心跳检测
    - tools/list       获取工具列表
    - tools/call       调用工具

    工具：
    - task_execute     任务执行（创建项目/分析需求/生成规划/触发质检/签核）
    - file_operate     文件操作（读/写/列出）
    - agent_query      状态查询（项目/Agent/进度/Skill）
    """
    handler = MCPToolHandler(projects, global_sm_agent, current_user.user_id)
    result = handle_mcp_request(body, handler)
    if result is None:
        # notifications 类消息无需响应，返回 204
        from fastapi import Response
        return Response(status_code=204)
    return result


@router.get("/mcp/tools")
async def mcp_tools(current_user: UserModel = Depends(get_current_user)):
    """获取 MCP 工具定义（需要认证）"""
    return {"tools": TOOL_DEFINITIONS, "count": len(TOOL_DEFINITIONS)}


@router.get("/mcp/token")
async def mcp_token_status(current_user: UserModel = Depends(get_current_user)):
    return get_mcp_token_status(current_user.user_id)


@router.post("/mcp/token")
async def issue_mcp_token(current_user: UserModel = Depends(get_current_user)):
    return {"token": create_mcp_token(current_user), "token_type": "Bearer"}


@router.delete("/mcp/token")
async def delete_mcp_token(current_user: UserModel = Depends(get_current_user)):
    return {"revoked": revoke_mcp_token(current_user.user_id)}


@router.get("/mcp/tokens")
async def get_mcp_tokens(current_user: UserModel = Depends(get_current_user)):
    return {"tokens": list_mcp_tokens(current_user.user_id)}


@router.post("/mcp/tokens")
async def issue_named_mcp_token(
    request: MCPTokenCreateRequest,
    current_user: UserModel = Depends(get_current_user),
):
    try:
        return create_named_mcp_token(current_user, request.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/mcp/tokens/{token_id}/reveal")
async def reveal_named_mcp_token(
    token_id: str,
    request: MCPTokenRevealRequest,
    current_user: UserModel = Depends(get_current_user),
):
    try:
        return {"token": reveal_mcp_token(current_user, token_id, request.password)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Token 不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.delete("/mcp/tokens/{token_id}")
async def delete_named_mcp_token(
    token_id: str,
    current_user: UserModel = Depends(get_current_user),
):
    if not revoke_named_mcp_token(current_user.user_id, token_id):
        raise HTTPException(status_code=404, detail="Token 不存在")
    return {"revoked": True}
