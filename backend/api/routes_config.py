"""系统配置路由"""
import asyncio
import time
import json
import logging
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from core.app_state import (
    app, projects, hermes_client, global_sm_agent, gitee_sync,
    config_loader, agents_api_config, DEFAULT_API_CONFIG, user_api_configs,
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
from core.auth import get_current_user, UserModel
from core.security_audit import redact_text
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

router = APIRouter(tags=["config"])

_ROLE_TEMPERATURE_DEFAULTS = {"generator": 0.1, "reviewer": 0.0}


def _get_user_api_config(user_id: str) -> Dict[str, Any]:
    """获取用户的 API 配置，不存在则返回空字典"""
    return user_api_configs.get(user_id, {})


def _mask_api_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """对 API Key 进行掩码处理，防止泄露"""
    return {k: ("*****" if k == "api_key" and v else v) for k, v in cfg.items()}


def _normalize_api_config(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return a two-role config while preserving legacy model settings."""
    normalized = dict(DEFAULT_API_CONFIG)
    normalized.update(dict(cfg or {}))
    base_model = str(normalized.get("model") or DEFAULT_API_CONFIG["model"])
    for purpose, default_temperature in _ROLE_TEMPERATURE_DEFAULTS.items():
        raw_role = normalized.get(purpose)
        role = dict(raw_role) if isinstance(raw_role, dict) else {}
        role["model"] = str(role.get("model") or base_model)
        if role.get("temperature") is None:
            role["temperature"] = default_temperature
        normalized[purpose] = role
    return normalized


def _safe_api_message(value: Any, api_key: str = "") -> str:
    """Return bounded API-facing text without reflecting credentials."""
    text = str(value or "")
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    return redact_text(text, max_length=300)


@router.get("/config/api/default")
async def get_default_api_config(current_user: UserModel = Depends(get_current_user)):
    """
    获取当前用户的 API 配置（按 user_id 独立隔离）。
    如果没有配置则返回全局默认值（不含 Key）。
    """
    user_cfg = _get_user_api_config(current_user.user_id)
    if user_cfg:
        # 用户有自己的配置，返回用户的
        return _mask_api_config(_normalize_api_config(user_cfg))
    # 用户未配置，返回全局默认（模型和 URL，无 Key）
    return _mask_api_config(_normalize_api_config(DEFAULT_API_CONFIG))

@router.get("/config/cache/stats")
async def get_cache_stats():
    """
    获取 LLM 缓存命中率统计（Reasonix 风格三层缓存）。
    用于监控 API 调用效率，命中率越高 API 费用越低。
    """
    stats = hermes_client.get_cache_stats()
    api_stats = hermes_client.get_stats()
    return {
        "cache": stats,
        "api": {
            "total_requests": api_stats["total_requests"],
            "total_tokens": api_stats["total_tokens"],
            "model": api_stats["model"],
        },
        "tips": {
            "hit_rate_pct": stats["hit_rate_pct"],
            "level": (
                "优秀（>60%）" if stats["hit_rate_pct"] >= 60 else
                "良好（30-60%）" if stats["hit_rate_pct"] >= 30 else
                "待优化（<30%）"
            ),
            "advice": (
                "缓存命中率高，API 费用已大幅降低" if stats["hit_rate_pct"] >= 60 else
                "可通过固定 system prompt 内容、降低 temperature 进一步提升命中率"
            ),
        },
    }

@router.delete("/config/cache")
async def clear_cache(prefix: str = ""):
    """手动清除 LLM 缓存（prefix 为空时清除全部）"""
    count = hermes_client.invalidate_cache(prefix)
    return {"success": True, "cleared": count, "message": f"已清除 {count} 条缓存"}

@router.post("/config/api/test")
async def test_api_connection(current_user: UserModel = Depends(get_current_user)):
    """
    测试当前用户的 API 配置是否可用（发送一条极短的测试消息）。
    返回：{"success": bool, "latency_ms": int, "model": str, "message": str}
    """
    import time as _time
    from core.hermes_client import Message, MessageRole, current_user_api_config

    # 获取用户配置
    user_cfg = _normalize_api_config(_get_user_api_config(current_user.user_id))
    test_key = user_cfg.get("api_key", "")
    if not test_key:
        return {"success": False, "latency_ms": 0, "model": user_cfg.get("model", "") if user_cfg else "", "message": "未配置 API Key"}

    # 临时设置用户 API 配置到 contextvar，让测试使用用户的 Key
    token = current_user_api_config.set(user_cfg)
    try:
        t0 = _time.monotonic()
        resp = hermes_client.chat([
            Message(role=MessageRole.USER, content="reply with the single word: ok")
        ])
        latency = int((_time.monotonic() - t0) * 1000)
        content = _safe_api_message(resp.get("content", ""), test_key)
        if content.startswith("⚠️") or content.startswith("❌"):
            return {"success": False, "latency_ms": latency, "model": user_cfg.get("model", ""), "message": content}
        return {"success": True, "latency_ms": latency, "model": user_cfg.get("model", ""), "message": f"连接成功，响应：{content[:60]}"}
    except Exception as e:
        return {
            "success": False,
            "latency_ms": 0,
            "model": user_cfg.get("model", ""),
            "message": _safe_api_message(e, test_key),
        }
    finally:
        current_user_api_config.reset(token)

@router.put("/config/api/default")
async def update_default_api_config(
    request: DefaultApiConfigRequest,
    current_user: UserModel = Depends(get_current_user),
):
    """
    更新当前用户的 API 配置（按 user_id 独立隔离）。
    保存后只有当前用户的 Agent 调用使用这个配置，不影响其他用户。
    """
    user_id = current_user.user_id

    # 获取或创建用户配置
    user_cfg = dict(
        _get_user_api_config(user_id)
        or DEFAULT_API_CONFIG
    )

    if request.model is not None:
        user_cfg["model"] = request.model
    if request.api_base is not None:
        user_cfg["api_base"] = request.api_base
    if request.api_key is not None:
        user_cfg["api_key"] = request.api_key
    if request.max_tokens is not None:
        user_cfg["max_tokens"] = request.max_tokens
    if request.temperature is not None:
        user_cfg["temperature"] = request.temperature
    if request.thinking is not None:
        user_cfg["thinking"] = request.thinking.model_dump()

    for purpose in _ROLE_TEMPERATURE_DEFAULTS:
        requested = getattr(request, purpose)
        if requested is None:
            continue
        role = dict(user_cfg.get(purpose) or {})
        role.update(requested.model_dump(exclude_none=True))
        user_cfg[purpose] = role

    # 确保有默认值
    user_cfg.setdefault("model", DEFAULT_API_CONFIG["model"])
    user_cfg.setdefault("api_base", DEFAULT_API_CONFIG["api_base"])
    user_cfg.setdefault("max_tokens", DEFAULT_API_CONFIG["max_tokens"])
    user_cfg.setdefault("temperature", DEFAULT_API_CONFIG["temperature"])

    user_cfg = _normalize_api_config(user_cfg)

    user_api_configs[user_id] = user_cfg
    hermes_client.update_config(user_cfg)

    await _persist_all_async()
    return {
        "success": True,
        "model": user_cfg["model"],
        "api_base": user_cfg["api_base"],
        "generator": user_cfg["generator"],
        "reviewer": user_cfg["reviewer"],
        "message": f"你的 API 配置已更新为 {user_cfg['model']}，仅对你生效"
    }
