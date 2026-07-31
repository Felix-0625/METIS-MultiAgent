"""专家池路由"""
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

router = APIRouter(tags=["experts"])


from core.expert_pool import ExpertPool, ExpertProfile, ExpertSkill, get_expert_pool

def _get_expert_pool() -> ExpertPool:
    # CRUD and phase planning must observe the same live pool instance.
    return get_expert_pool()

@router.get("/experts")
async def list_experts(
    agent_type: Optional[str] = None,
    status: Optional[str] = None,
    domain: Optional[str] = None,
):
    """列出专家池中的所有专家（支持过滤）"""
    pool = _get_expert_pool()
    experts = pool.list_experts(agent_type=agent_type, status=status, domain=domain)
    return {
        "experts": [e.to_dict() for e in experts],
        "total": len(experts),
    }

@router.post("/experts")
async def create_expert(request: ExpertCreateRequest):
    """创建专家档案（加入专家池）"""
    pool = _get_expert_pool()
    skills = [ExpertSkill(**s) for s in request.skills] if request.skills else []
    profile = ExpertProfile(
        name=request.name,
        role=request.role,
        agent_type=request.agent_type,
        avatar=request.avatar,
        role_description=request.role_description,
        working_style=request.working_style,
        communication_style=request.communication_style,
        decision_style=request.decision_style,
        domains=request.domains,
        skills=skills,
        skill_ids=request.skill_ids,
        behavior_rules=request.behavior_rules,
        rejection_policy=request.rejection_policy,
        output_format=request.output_format,
        api_config=request.api_config,
    )
    pool.create_expert(profile)
    return {"success": True, "expert": profile.to_dict()}

@router.get("/experts/{expert_id}")
async def get_expert(expert_id: str):
    """获取专家详情"""
    pool = _get_expert_pool()
    profile = pool.get_expert(expert_id)
    if not profile:
        raise HTTPException(status_code=404, detail=f"专家 {expert_id} 不存在")
    return profile.to_dict()

@router.patch("/experts/{expert_id}")
async def update_expert(expert_id: str, request: ExpertUpdateRequest):
    """更新专家档案"""
    pool = _get_expert_pool()
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    # skills 字段需要转换
    if "skills" in updates and updates["skills"]:
        updates["skills"] = [ExpertSkill(**s) for s in updates["skills"]]
    profile = pool.update_expert(expert_id, updates)
    if not profile:
        raise HTTPException(status_code=404, detail=f"专家 {expert_id} 不存在")
    return {"success": True, "expert": profile.to_dict()}

@router.delete("/experts/{expert_id}")
async def delete_expert(expert_id: str):
    """删除专家档案"""
    pool = _get_expert_pool()
    success = pool.delete_expert(expert_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"专家 {expert_id} 不存在")
    return {"success": True}

@router.post("/experts/match")
async def match_experts(request: ExpertMatchRequest):
    """任务-专家匹配：根据角色/领域/技能找最合适的专家"""
    pool = _get_expert_pool()
    results = pool.match_experts(
        required_role=request.required_role,
        required_domains=request.required_domains,
        required_skills=request.required_skills,
        agent_type=request.agent_type,
        top_k=request.top_k,
        exclude_busy=request.exclude_busy,
    )
    return {"matches": results, "count": len(results)}

@router.get("/experts/{expert_id}/memory/{project_id}")
async def get_expert_memory(expert_id: str, project_id: str, scope: str = "project", phase_id: str = ""):
    """获取专家在某项目的记忆"""
    pool = _get_expert_pool()
    mem = pool.get_project_memory(expert_id, project_id, scope, phase_id)
    return mem.to_dict()

@router.post("/experts/{expert_id}/memory/{project_id}")
async def add_expert_memory(expert_id: str, project_id: str, request: ExpertMemoryRequest):
    """向专家的项目记忆中添加条目"""
    pool = _get_expert_pool()
    pool.add_memory_entry(
        expert_id=expert_id,
        project_id=project_id,
        content=request.content,
        memory_type=request.memory_type,
        importance=request.importance,
        scope=request.scope,
        phase_id=request.phase_id,
    )
    return {"success": True}

@router.delete("/experts/{expert_id}/memory/{project_id}")
async def clear_expert_memory(expert_id: str, project_id: str):
    """清空专家在某项目的所有记忆"""
    pool = _get_expert_pool()
    pool.clear_project_memory(expert_id, project_id)
    return {"success": True}

@router.get("/experts/{expert_id}/system-prompt/{project_id}")
async def get_expert_system_prompt(
    expert_id: str,
    project_id: str,
    scope: str = "project",
    phase_id: str = "",
):
    """预览专家的完整 System Prompt（角色记忆 + 项目记忆）"""
    pool = _get_expert_pool()
    prompt = pool.build_system_prompt(expert_id, project_id, scope, phase_id)
    return {"expert_id": expert_id, "project_id": project_id, "system_prompt": prompt}

@router.get("/experts/{expert_id}/system-prompt")
async def preview_expert_system_prompt(expert_id: str):
    """预览专家当前 System Prompt（不含项目记忆，供训练面板实时展示）"""
    pool = _get_expert_pool()
    prompt = pool.preview_system_prompt(expert_id)
    return {"expert_id": expert_id, "system_prompt": prompt}

@router.get("/experts/{expert_id}/training")
async def get_expert_training(expert_id: str, limit: int = 20, only_pending: bool = False):
    """获取专家的训练记录列表"""
    pool = _get_expert_pool()
    sessions = pool.get_training_sessions(expert_id, limit=limit, only_pending=only_pending)
    expert = pool.get_expert(expert_id)
    if not expert:
        raise HTTPException(status_code=404, detail=f"专家 {expert_id} 不存在")
    return {
        "expert_id": expert_id,
        "sessions": sessions,
        "total": len(expert.training_sessions),
        "pending": len([s for s in expert.training_sessions if not s.applied]),
        "long_term_memory": expert.long_term_memory,
        "user_preferences": expert.user_preferences,
    }

@router.post("/experts/{expert_id}/training")
async def add_expert_training(expert_id: str, request: ExpertTrainingRequest):
    """
    添加训练会话记录。
    每次用户对专家输出给出反馈/纠错/风格调整时调用。
    训练记录跨项目保留，是专家的个人成长档案。
    """
    pool = _get_expert_pool()
    session = pool.add_training_session(
        expert_id=expert_id,
        session_type=request.session_type,
        feedback=request.feedback,
        user_input=request.user_input or "",
        agent_output=request.agent_output or "",
        correction=request.correction or "",
    )
    if not session:
        raise HTTPException(status_code=404, detail=f"专家 {expert_id} 不存在")
    return {"success": True, "session": session.model_dump()}

@router.post("/experts/{expert_id}/training/apply")
async def apply_expert_training(expert_id: str):
    """
    将待应用的训练记录提炼为长期记忆，注入 system prompt。
    调用后，专家的 long_term_memory 会更新，下次执行任务时自动生效。
    """
    pool = _get_expert_pool()
    expert = pool.get_expert(expert_id)
    if not expert:
        raise HTTPException(status_code=404, detail=f"专家 {expert_id} 不存在")
    new_memory = pool.apply_training_to_memory(expert_id, hermes_client=hermes_client)
    return {
        "success": True,
        "expert_id": expert_id,
        "long_term_memory": new_memory,
        "message": "训练记录已提炼并应用到长期记忆",
    }

@router.delete("/experts/{expert_id}/training")
async def clear_expert_training(expert_id: str):
    """清空专家的所有训练记录（保留长期记忆）"""
    pool = _get_expert_pool()
    expert = pool.get_expert(expert_id)
    if not expert:
        raise HTTPException(status_code=404, detail=f"专家 {expert_id} 不存在")
    expert.training_sessions = []
    expert.updated_at = time.time()
    pool.save()
    return {"success": True, "message": "训练记录已清空（长期记忆保留）"}

@router.patch("/experts/{expert_id}/work-mode")
async def update_expert_work_mode(expert_id: str, request: ExpertWorkModeRequest):
    """
    更新专家的工作模式配置（思考方式、执行风格、CoT 开关等）。
    支持部分更新，只传需要修改的字段。
    """
    pool = _get_expert_pool()
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    wm = pool.update_work_mode(expert_id, updates)
    if not wm:
        raise HTTPException(status_code=404, detail=f"专家 {expert_id} 不存在")
    return {"success": True, "work_mode": wm.model_dump()}

@router.patch("/experts/{expert_id}/config")
async def update_expert_config(expert_id: str, request: ExpertConfigRequest):
    """
    更新专家的高级配置：思考框架、项目背景、用户偏好、长期记忆。
    这些字段直接注入 system prompt，影响专家的思考和输出方式。
    """
    pool = _get_expert_pool()
    expert = pool.get_expert(expert_id)
    if not expert:
        raise HTTPException(status_code=404, detail=f"专家 {expert_id} 不存在")
    updates: Dict[str, Any] = {}
    if request.thinking_framework is not None:
        updates["thinking_framework"] = request.thinking_framework
    if request.project_background is not None:
        updates["project_background"] = request.project_background
    if request.user_preferences is not None:
        updates["user_preferences"] = request.user_preferences
    if request.long_term_memory is not None:
        updates["long_term_memory"] = request.long_term_memory
    profile = pool.update_expert(expert_id, updates)
    return {"success": True, "expert": profile.to_dict() if profile else {}}

@router.post("/experts/{expert_id}/chat-train")
async def expert_chat_train(expert_id: str, request: ExpertChatTrainRequest):
    """
    与专家进行训练对话。

    用法：
    1. 用户向专家提问（任何问题，测试专家的回答质量）
    2. 专家用当前 system prompt（含长期记忆）回答
    3. 用户对回答不满意时，调用 POST /experts/{id}/training/feedback 给出纠正
    4. 积累几条反馈后，调用 POST /experts/{id}/training/apply 提炼为长期记忆

    这样专家会越来越符合用户的期望。
    """
    pool = _get_expert_pool()
    history = [
        {"role": h.get("role"), "content": h.get("content", "")}
        for h in (request.history or [])
        if h.get("role") in ("user", "assistant") and h.get("content")
    ]
    result = pool.chat_train(
        expert_id=expert_id,
        user_message=request.message,
        history=history,
        hermes_client=hermes_client,
    )
    if "error" in result and not result.get("reply"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result

@router.post("/experts/{expert_id}/training/feedback")
async def add_expert_feedback(expert_id: str, request: ExpertFeedbackRequest):
    """
    对专家的某次回答给出即时反馈/纠正。

    在对话训练中，用户看到专家回答后，如果不满意可以立即纠正：
    - 告诉专家哪里错了（feedback）
    - 给出期望的正确回答（correction，可选）
    - 选择反馈类型（correction/style_tune/logic_tune/output_tune）

    auto_apply=True 时，反馈会立即提炼到长期记忆（适合单条重要纠正）。
    auto_apply=False 时，反馈先存入训练记录，等积累几条后统一 apply（适合批量调整）。
    """
    pool = _get_expert_pool()
    session = pool.add_training_session(
        expert_id=expert_id,
        session_type=request.session_type,
        feedback=request.feedback,
        user_input=request.user_input,
        agent_output=request.agent_output,
        correction=request.correction,
    )
    if not session:
        raise HTTPException(status_code=404, detail=f"专家 {expert_id} 不存在")

    new_memory = None
    if request.auto_apply:
        new_memory = pool.apply_training_to_memory(expert_id, hermes_client=hermes_client)

    return {
        "success": True,
        "session_id": session.session_id,
        "auto_applied": request.auto_apply,
        "long_term_memory": new_memory,
        "message": "反馈已记录并立即应用到长期记忆" if request.auto_apply else "反馈已记录，可稍后批量 apply",
    }

@router.post("/experts/{expert_id}/knowledge")
async def upload_expert_knowledge_text(expert_id: str, request: ExpertKnowledgeRequest):
    """
    向专家喂入知识文本（直接粘贴文本内容）。

    适合：
    - 粘贴技术文档、规范文档
    - 粘贴项目背景说明
    - 粘贴代码示例、最佳实践

    长文本（>2000字）会自动用 LLM 压缩提炼为关键知识点再存入。
    """
    pool = _get_expert_pool()
    result = pool.add_knowledge(
        expert_id=expert_id,
        content=request.content,
        source_name=request.source_name,
        knowledge_type=request.knowledge_type,
        hermes_client=hermes_client,
    )
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error", "上传失败"))
    return result

@router.post("/experts/{expert_id}/knowledge/upload")
async def upload_expert_knowledge_file(
    expert_id: str,
    source_name: str = Form(""),
    knowledge_type: str = Form("document"),
    file: UploadFile = File(...),
):
    """
    向专家上传知识文件（支持 txt/md/pdf/docx/json/csv）。

    文件内容会自动提取为纯文本，长文本自动压缩提炼。
    提炼后的知识点注入专家的长期记忆，下次执行任务时自动生效。
    """
    pool = _get_expert_pool()
    expert = pool.get_expert(expert_id)
    if not expert:
        raise HTTPException(status_code=404, detail=f"专家 {expert_id} 不存在")

    MAX_SIZE = 10 * 1024 * 1024  # 10MB
    raw = await file.read()
    if len(raw) > MAX_SIZE:
        raise HTTPException(status_code=400, detail=f"文件过大，最大支持 10MB")

    filename = file.filename or "unknown"
    name = source_name or filename

    # 复用已有的文件文本提取逻辑
    text = _extract_file_text(filename, raw)
    is_error = text.startswith("[") and ("解析失败" in text or "解析需要" in text or "不支持" in text)
    if is_error:
        raise HTTPException(status_code=400, detail=f"文件解析失败：{text}")

    result = pool.add_knowledge(
        expert_id=expert_id,
        content=text,
        source_name=name,
        knowledge_type=knowledge_type,
        hermes_client=hermes_client,
    )
    return result

