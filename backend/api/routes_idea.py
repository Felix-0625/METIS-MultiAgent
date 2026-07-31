"""想法落地路由"""
import time
from fastapi import APIRouter, HTTPException, File, Form
import uvicorn
from core.app_state import (hermes_client, _get_idea_landing, _persist_idea_landing, logger)
from models.schemas import (IdeaChatRequest, IdeaNewConvRequest, IdeaConvMetaRequest, IdeaPinRequest, IdeaAdvancePhaseRequest, IdeaUserMemoryRequest)
from typing import Optional
from pydantic import BaseModel

router = APIRouter(tags=["idea_landing"])
@router.get("/idea-landing/status")
async def idea_landing_status():
    """获取 IdeaLanding Agent 状态"""
    agent = _get_idea_landing()
    return agent.get_status()


@router.get("/idea-landing/conversations")
async def idea_landing_list_conversations(
    category: Optional[str] = None,
    tag: Optional[str] = None,
):
    """
    获取对话列表。
    置顶对话排在最前，其余按更新时间倒序。
    支持按 category / tag 过滤。
    """
    agent = _get_idea_landing()
    convs = agent.list_conversations(category=category, tag=tag)
    return {
        "conversations": convs,
        "total": len(convs),
        "active_conv_id": agent.active_conv_id,
    }


@router.post("/idea-landing/conversations")
async def idea_landing_new_conversation(request: IdeaNewConvRequest):
    """开启一个新的隔离对话"""
    agent = _get_idea_landing()
    conv = agent.new_conversation(
        title=request.title,
        tags=request.tags,
        category=request.category,
    )
    await _persist_idea_landing()
    return {
        "conv_id": conv.conv_id,
        "title": conv.title,
        "current_phase": conv.current_phase,
        "phase_name": conv.get_phase_name(),
    }


@router.get("/idea-landing/conversations/{conv_id}")
async def idea_landing_get_conversation(conv_id: str):
    """获取单个对话详情（含完整消息列表）"""
    agent = _get_idea_landing()
    conv = agent.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail=f"对话 {conv_id} 不存在")
    return conv.to_dict()


@router.delete("/idea-landing/conversations/{conv_id}")
async def idea_landing_delete_conversation(conv_id: str):
    """删除对话（删除前自动保存 idea 摘要到个人记忆）"""
    agent = _get_idea_landing()
    success = agent.delete_conversation(conv_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"对话 {conv_id} 不存在")
    await _persist_idea_landing()
    return {"success": True}


@router.patch("/idea-landing/conversations/{conv_id}")
async def idea_landing_update_conversation(conv_id: str, request: IdeaConvMetaRequest):
    """更新对话标题/标签/分类"""
    agent = _get_idea_landing()
    success = agent.update_conversation_meta(
        conv_id=conv_id,
        title=request.title,
        tags=request.tags,
        category=request.category,
    )
    if not success:
        raise HTTPException(status_code=404, detail=f"对话 {conv_id} 不存在")
    await _persist_idea_landing()
    return {"success": True}


@router.post("/idea-landing/conversations/{conv_id}/pin")
async def idea_landing_pin_conversation(conv_id: str, request: IdeaPinRequest):
    """置顶/取消置顶对话"""
    agent = _get_idea_landing()
    success = agent.pin_conversation(conv_id, request.pinned)
    if not success:
        raise HTTPException(status_code=404, detail=f"对话 {conv_id} 不存在")
    await _persist_idea_landing()
    return {"success": True, "pinned": request.pinned}


@router.post("/idea-landing/conversations/{conv_id}/switch")
async def idea_landing_switch_conversation(conv_id: str):
    """切换激活对话"""
    agent = _get_idea_landing()
    success = agent.switch_conversation(conv_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"对话 {conv_id} 不存在")
    await _persist_idea_landing()
    return {"success": True, "active_conv_id": conv_id}


@router.post("/idea-landing/chat")
async def idea_landing_chat(request: IdeaChatRequest):
    """
    主要对话入口。

    流程：
    - 阶段1：压力测试（识别假设/盲区/矛盾）
    - 阶段2：需求文档生成
    - 阶段3：优化补充

    每个对话的上下文完全隔离，不会污染其他对话。
    """
    agent = _get_idea_landing()
    history = [
        {"role": m.get("role"), "content": m.get("content", "")}
        for m in (request.history or [])
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    result = agent.chat(
        user_input=request.message,
        conv_id=request.conv_id,
        history=history if history else None,
        context_summary=request.context_summary,
    )
    await _persist_idea_landing()
    return result


@router.post("/idea-landing/conversations/{conv_id}/advance-phase")
async def idea_landing_advance_phase(conv_id: str):
    """手动推进阶段（用户确认当前阶段完成）"""
    agent = _get_idea_landing()
    result = agent.advance_phase(conv_id)
    if result.get("success"):
        await _persist_idea_landing()
    return result


@router.post("/idea-landing/conversations/{conv_id}/generate-doc")
async def idea_landing_generate_doc(conv_id: str):
    """手动触发生成需求文档（阶段1完成后可调用）"""
    agent = _get_idea_landing()
    conv = agent.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail=f"对话 {conv_id} 不存在")
    if conv.current_phase < 2:
        raise HTTPException(status_code=400, detail="当前阶段不允许生成需求文档，请先完成压力测试并进入阶段 2")
    result = agent.generate_requirements_doc(conv_id)
    if result.get("success"):
        await _persist_idea_landing()
    return result

@router.get("/idea-landing/conversations/{conv_id}/phase")
async def idea_landing_get_current_phase(conv_id: str):
    """获取对话的当前阶段信息"""
    agent = _get_idea_landing()
    result = agent.get_current_phase(conv_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error", "对话不存在"))
    return result

@router.get("/idea-landing/conversations/{conv_id}/requirements-doc")
async def idea_landing_get_requirements_doc(conv_id: str):
    """获取对话的需求文档"""
    agent = _get_idea_landing()
    conv = agent.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail=f"对话 {conv_id} 不存在")
    return {
        "conv_id": conv_id,
        "requirements_doc": conv.requirements_doc,
        "has_doc": bool(conv.requirements_doc),
        "current_phase": conv.current_phase,
    }


@router.get("/idea-landing/user-memory")
async def idea_landing_get_user_memory():
    """获取用户个人记忆"""
    agent = _get_idea_landing()
    return agent.user_memory.to_dict()


@router.patch("/idea-landing/user-memory")
async def idea_landing_update_user_memory(request: IdeaUserMemoryRequest):
    """更新用户个人记忆（背景、偏好）"""
    agent = _get_idea_landing()
    agent.update_user_memory(
        background=request.background or "",
        preferences=request.preferences,
    )
    await _persist_idea_landing()
    return {"success": True}


# ─── 启动 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
