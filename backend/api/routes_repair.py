"""修复循环路由"""
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

router = APIRouter(tags=["repair"])



@router.post("/projects/{project_id}/repair/start")
async def start_repair_loop(project_id: str, request: RepairStartRequest):
    """
    启动修复循环：
    1. 从最新质检结果导入缺陷单
    2. 检查终止条件（是否需要仲裁者介入）
    3. 按类别+文件聚类，生成分批修复指令（每批≤3个缺陷，P0优先）
    4. 返回批次列表和修复指令（用户点击「启动修复」触发全流程）
    """
    ctx = _get_project(project_id)
    sp_id = request.subproject_id
    sp_info = next((s for s in ctx.subprojects if s["id"] == sp_id), {})
    sp_name = sp_info.get("name", sp_id)
    agent_id = sp_info.get("agent_id", "")
    agent_role = ctx.agents.get(agent_id, {}).get("role", "开发工程师") if agent_id else "开发工程师"

    # 获取或创建控制器
    ctrl = repair_registry.get_controller(project_id, sp_id, sp_name)

    # 防止重复启动：若存在正在修复或等待审批的缺陷，不允许重新导入质检结果
    # 这会重置这些缺陷的状态，破坏进行中的修复流程
    in_progress = [
        d for d in ctrl.defects.values()
        if d.status in (
            DefectStatus.FIXING,
            DefectStatus.PENDING_REVIEW,
            DefectStatus.APPROVED,
        )
    ]
    if in_progress:
        in_progress_ids = [d.defect_id for d in in_progress[:5]]
        raise HTTPException(
            status_code=409,
            detail=(
                f"存在 {len(in_progress)} 个缺陷正在修复/等待审批中 "
                f"（{', '.join(in_progress_ids)}），"
                "请完成当前修复批次后再启动新一轮修复循环"
            ),
        )

    # 从最新质检结果导入缺陷单
    qc_entry = ctx.qc_results.get(sp_id, {})
    if not qc_entry:
        raise HTTPException(status_code=400, detail="该子项目尚未进行质检，请先触发质检")

    new_tickets = ctrl.ingest_qc_result(
        qc_result=qc_entry,
        agent_id=agent_id,
        agent_role=agent_role,
        forbidden_zone=request.forbidden_zone,
    )

    # 检查终止条件
    term = ctrl.check_termination()
    if term["should_escalate"]:
        phase_id = sp_info.get("phase_id", "default")
        phase_name = sp_info.get("phase_name", "")
        arbiter = repair_registry.get_arbiter(project_id, phase_id, phase_name)
        arb_result = arbiter.evaluate(ctrl)
        await _persist_all_async()
        return {
            "success": False,
            "escalated": True,
            "arbiter_result": arb_result,
            "summary": ctrl.summary(),
            "message": f"修复循环已触发仲裁者介入：{'; '.join(term['reasons'])}",
        }

    # 生成分批修复指令
    batches = ctrl.create_batches(forbidden_zone=request.forbidden_zone)
    await _persist_all_async()

    return {
        "success": True,
        "escalated": False,
        "new_defects": len(new_tickets),
        "batches": [b.to_dict() for b in batches],
        "batch_instructions": [b.to_instruction() for b in batches],
        "summary": ctrl.summary(),
        "message": f"已生成 {len(batches)} 个修复批次，共 {sum(len(b.defects) for b in batches)} 个缺陷待修复",
    }

@router.get("/projects/{project_id}/repair/{subproject_id}/status")
async def get_repair_status(project_id: str, subproject_id: str):
    """获取子项目修复循环状态（缺陷单列表、轮次、分数趋势）"""
    _get_project(project_id)
    ctrl = repair_registry.get_controller(project_id, subproject_id)
    term = ctrl.check_termination()
    return {
        "subproject_id": subproject_id,
        "summary": ctrl.summary(),
        "termination_check": term,
        "defects": [d.to_dict() for d in ctrl.defects.values()],
        "batches": [b.to_dict() for b in ctrl.batches],
        "pending_review": [d.to_dict() for d in ctrl.get_pending_review_defects()],
        "escalated": [d.to_dict() for d in ctrl.get_escalated_defects()],
    }

@router.post("/projects/{project_id}/repair/{subproject_id}/defects/{defect_id}/proposal")
async def submit_fix_proposal(project_id: str, subproject_id: str, defect_id: str, request: ProposalSubmitRequest):
    """专家提交修复方案（≤100字），等待PM/Supervisor审批"""
    _get_project(project_id)
    ctrl = repair_registry.get_controller(project_id, subproject_id)
    result = ctrl.submit_proposal(defect_id, request.proposal)
    if result["success"]:
        await _persist_all_async()
    return result

@router.post("/projects/{project_id}/repair/{subproject_id}/defects/{defect_id}/review")
async def review_fix_proposal(project_id: str, subproject_id: str, defect_id: str, request: ProposalReviewRequest):
    """PM/Supervisor 审批修复方案（通过/驳回）"""
    _get_project(project_id)
    ctrl = repair_registry.get_controller(project_id, subproject_id)
    if request.approved:
        result = ctrl.approve_proposal(defect_id, reviewer=request.reviewer)
    else:
        result = ctrl.reject_proposal(defect_id, reason=request.reason or "", reviewer=request.reviewer)
    if result["success"]:
        await _persist_all_async()
    return result

@router.post("/projects/{project_id}/repair/{subproject_id}/defects/{defect_id}/mark-fixed")
async def mark_defect_fixed(project_id: str, subproject_id: str, defect_id: str):
    """标记缺陷已修复（待复检）"""
    _get_project(project_id)
    ctrl = repair_registry.get_controller(project_id, subproject_id)
    result = ctrl.mark_fixed(defect_id)
    if result["success"]:
        await _persist_all_async()
    return result

@router.post("/projects/{project_id}/repair/{subproject_id}/defects/{defect_id}/verify")
async def verify_defect_fixed(project_id: str, subproject_id: str, defect_id: str):
    """复检通过，标记缺陷为 verified"""
    _get_project(project_id)
    ctrl = repair_registry.get_controller(project_id, subproject_id)
    result = ctrl.verify_fixed(defect_id)
    if result["success"]:
        await _persist_all_async()
    return result

@router.post("/projects/{project_id}/repair/{subproject_id}/defects/{defect_id}/reopen")
async def reopen_defect(project_id: str, subproject_id: str, defect_id: str):
    """复检未通过，重新打开缺陷（fix_rounds+1，超限自动升级）"""
    _get_project(project_id)
    ctrl = repair_registry.get_controller(project_id, subproject_id)
    result = ctrl.reopen_defect(defect_id)
    if result["success"]:
        await _persist_all_async()
    return result

@router.get("/projects/{project_id}/repair/summary")
async def get_project_repair_summary(project_id: str):
    """获取项目所有子项目的修复循环汇总"""
    _get_project(project_id)
    return {
        "project_id": project_id,
        "controllers": repair_registry.list_controllers(project_id),
        "arbiters": repair_registry.list_arbiters(project_id),
    }

@router.post("/projects/{project_id}/repair/arbiter/force-pass")
async def arbiter_force_pass(project_id: str, request: ArbiterForcePassRequest):
    """仲裁者执行强制通过（将所有 ESCALATED 缺陷标记为 MANUAL）"""
    ctx = _get_project(project_id)
    sp_id = request.subproject_id
    sp_info = next((s for s in ctx.subprojects if s["id"] == sp_id), {})
    phase_id = sp_info.get("phase_id", "default")
    phase_name = sp_info.get("phase_name", "")

    ctrl = repair_registry.get_controller(project_id, sp_id)
    arbiter = repair_registry.get_arbiter(project_id, phase_id, phase_name)
    result = arbiter.apply_force_pass(ctrl)
    await _persist_all_async()
    return result

