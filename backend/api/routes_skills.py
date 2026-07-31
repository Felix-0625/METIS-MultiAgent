"""Skill 池路由"""
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from typing import Optional, List, Dict
from pydantic import BaseModel
from core.app_state import (projects, hermes_client, global_sm_agent, _persist_all_async, logger)
from models.schemas import SkillImportRequest, SkillSearchRequest, SkillIngestRequest, SkillConfirmRequest

router = APIRouter(tags=["skills"])
# ─── Skill 池 ─────────────────────────────────────────────────────────────────

@router.get("/skills")
async def list_skills():
    return {"skills": global_sm_agent.get_skill_pool()}

@router.get("/skills/grouped")
async def list_skills_grouped():
    """按 Agent 类型分组返回 Skill 池，用于前端分类展示"""
    grouped = global_sm_agent.get_skills_grouped_by_category()
    total = sum(len(v) for v in grouped.values())
    return {
        "grouped": grouped,
        "total": total,
        "categories": {
            "pm":         {"label": "PM Agent",         "count": len(grouped["pm"])},
            "supervisor": {"label": "Supervisor Agent", "count": len(grouped["supervisor"])},
            "hr":         {"label": "HR Agent",         "count": len(grouped["hr"])},
            "pg":         {"label": "PG Agent",         "count": len(grouped["pg"])},
            "ccb":        {"label": "CCB Agent",        "count": len(grouped["ccb"])},
            "common":     {"label": "通用",              "count": len(grouped["common"])},
            "other":      {"label": "其他",              "count": len(grouped["other"])},
        }
    }

@router.get("/skills/grouped-by-domain")
async def list_skills_grouped_by_domain():
    """按专业领域分组返回 Skill 池"""
    grouped = global_sm_agent.get_skills_grouped_by_domain()
    total = sum(len(v) for v in grouped.values())
    return {
        "grouped": grouped,
        "total": total,
        "domains": [
            {"key": k, "count": len(v)}
            for k, v in grouped.items()
        ]
    }

@router.get("/skills/for-agent/{agent_type}")
async def list_skills_for_agent_type(agent_type: str):
    """获取指定 agent_type 的 Skill 列表（含通用 Skill）"""
    valid_types = [
        "pm", "supervisor", "hr", "pg", "ccb",
        "fullstack_engineer",
        "frontend", "backend", "database", "api", "architecture",
        "devops", "security", "qa", "data"
    ]
    if agent_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"agent_type 只支持：{valid_types}")
    skills = global_sm_agent.get_skills_for_agent_type(agent_type)
    return {"agent_type": agent_type, "skills": skills, "count": len(skills)}

@router.post("/skills/import")
async def import_skill(request: SkillImportRequest):
    result = global_sm_agent.import_skill(
        {"name": request.name, "description": request.description, "version": request.version, "content": request.content},
        source=request.source
    )
    await _persist_all_async()
    return result

@router.post("/skills/search")
async def search_skills(request: SkillSearchRequest):
    return {"results": global_sm_agent.search_skills(request.query, request.filters)}

@router.patch("/skills/{skill_id}")
async def update_skill(skill_id: str, body: dict):
    """Update a skill (partial update of editable fields)"""
    result = global_sm_agent.update_skill(skill_id, body)
    if not result.get("success"):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=result.get("error", "Skill not found"))
    await _persist_all_async()
    return result

@router.delete("/skills/{skill_id}")
async def delete_skill(skill_id: str):
    result = global_sm_agent.delete_skill(skill_id)
    await _persist_all_async()
    return result




@router.post("/skills/ingest")
async def ingest_skill(request: SkillIngestRequest):
    """
    Skill Agent 主入口：接收文件内容或 URL，自动解析、分类、入库。

    流程：
    1. 解析 SKILL.md 格式（YAML Frontmatter + Markdown 正文）
    2. 自动推断 for_agents / tags / capability_type
    3. 生成分类报告供用户确认
    4. auto_confirm=True 时直接入库

    支持来源：
    - 文件上传（raw_text）
    - GitHub URL（自动转换为 raw URL）
    - 任意 HTTP/HTTPS URL
    """
    if not request.raw_text and not request.url:
        raise HTTPException(status_code=400, detail="必须提供 raw_text 或 url")

    result = global_sm_agent.ingest_skill_from_raw(
        raw_text=request.raw_text or "",
        url=request.url or "",
        filename=request.filename or "",
        auto_confirm=request.auto_confirm,
    )

    if result.get("status") == "imported":
        await _persist_all_async()

    return result


@router.post("/skills/ingest/file")
async def ingest_skill_file(
    file: UploadFile = File(...),
    auto_confirm: bool = Form(False),
):
    """
    上传 Skill 文件（.md / .txt）并解析入库。

    multipart/form-data 格式：
    - file: 文件内容
    - auto_confirm: 是否直接入库（默认 False）
    """
    content_bytes = await file.read()
    try:
        raw_text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raw_text = content_bytes.decode("gbk", errors="replace")

    result = global_sm_agent.ingest_skill_from_raw(
        raw_text=raw_text,
        filename=file.filename or "",
        auto_confirm=auto_confirm,
    )

    if result.get("status") == "imported":
        await _persist_all_async()

    return result


@router.post("/skills/confirm")
async def confirm_skill_import(request: SkillConfirmRequest):
    """
    确认 Skill 导入（Step 2）
    
    用户在前端查看建议报告后点击「确认入库」时调用。
    可同时调整分类和标签。
    """
    result = global_sm_agent.confirm_skill_import(
        skill_id=request.skill_id,
        override_classification=request.override_classification,
        tags=request.tags,
    )
    if result.get("success"):
        await _persist_all_async()
    return result

@router.get("/skills/pending")
async def list_pending_skills():
    """列出所有待确认的 Skill（pending_confirmation 状态）"""
    pending = [
        s for s in global_sm_agent.skill_pool.values()
        if s.get("status") == "pending_confirmation"
    ]
    return {"skills": pending, "count": len(pending)}

@router.get("/skills/status")
async def skill_pool_status():
    return global_sm_agent.get_status()


# ─── 对话历史 ─────────────────────────────────────────────────────────────────
