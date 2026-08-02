"""项目管理系统路由"""
import asyncio
import time
import uuid
from fastapi import APIRouter, HTTPException, Depends, Header
from typing import Optional, Dict, Any
from pydantic import BaseModel
from core.app_state import (
    projects, hermes_client, global_sm_agent, get_user_sm_agent,
    config_loader, agents_api_config, DEFAULT_API_CONFIG,
    _get_project, _persist_all_async, _can_user_access_project,
    ProjectContext, logger,
)
from models.schemas import (
    ProjectRequest,
)
from core.auth import get_current_user, UserModel
from core.execution_runs import IdempotencyConflict, IdempotencyStore
from core.database import commit_project_with_idempotency, delete_idempotency_reservation, delete_project_files

router = APIRouter(tags=["projects"])
_idempotency = IdempotencyStore()

async def _create_project_once(request: ProjectRequest, current_user: UserModel):
    project_id = f"proj-{uuid.uuid4().hex[:6]}"
    ctx = ProjectContext(
        project_id, request.name, request.description,
        owner_user_id=current_user.user_id,
        hermes_client=hermes_client,
        global_sm_agent=get_user_sm_agent(current_user.user_id),
    )
    projects[project_id] = ctx
    try:
        await _persist_all_async()
    except Exception:
        if projects.get(project_id) is ctx:
            projects.pop(project_id, None)
        raise
    return {
        **ctx.to_dict(),
        "project_id": project_id,
        "agents": {
            "pm": ctx.pm.agent_id,
            "hr": ctx.hr.agent_id,
            "pg": ctx.pg.agent_id,
            "supervisor": ctx.supervisor.agent_id,
            "ccb": ctx.ccb.agent_id,
        },
        "message": f"项目 {request.name} 已创建，独立 Agent 团队已就绪"
    }


def _new_project_response(request: ProjectRequest, current_user: UserModel):
    project_id = f"proj-{uuid.uuid4().hex[:6]}"
    ctx = ProjectContext(
        project_id, request.name, request.description,
        owner_user_id=current_user.user_id, hermes_client=hermes_client,
        global_sm_agent=get_user_sm_agent(current_user.user_id),
    )
    response = {
        **ctx.to_dict(), "project_id": project_id,
        "agents": {"pm": ctx.pm.agent_id, "hr": ctx.hr.agent_id, "pg": ctx.pg.agent_id,
                   "supervisor": ctx.supervisor.agent_id, "ccb": ctx.ccb.agent_id},
        "message": f"项目 {request.name} 已创建，独立 Agent 团队已就绪",
    }
    return project_id, ctx, response

@router.post("/projects")
async def create_project(
    request: ProjectRequest,
    current_user: UserModel = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key", max_length=256),
):
    """Create one project; an optional idempotency key makes retries safe."""
    if not isinstance(idempotency_key, str):
        idempotency_key = None
    if not idempotency_key:
        return await _create_project_once(request, current_user)

    payload = request.model_dump()
    try:
        reservation = _idempotency.reserve(
            "projects.create", current_user.user_id, idempotency_key, payload,
        )
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not reservation["acquired"]:
        response = reservation.get("response")
        if reservation["status"] == "completed" and isinstance(response, dict):
            return {**response, "idempotent_replay": True}
        raise HTTPException(
            status_code=409,
            detail=f"Idempotent project creation is {reservation['status']}",
        )

    try:
        project_id, ctx, response = _new_project_response(request, current_user)
        await asyncio.to_thread(
            commit_project_with_idempotency,
            project_id, ctx.to_persist(), scope="projects.create",
            actor_id=current_user.user_id, key=idempotency_key,
            request_hash=_idempotency.hash_request(payload), response=response,
        )
        projects[project_id] = ctx
        return response
    except Exception:
        try:
            delete_idempotency_reservation(
                "projects.create", current_user.user_id, idempotency_key,
            )
        except Exception:
            logger.exception("Failed to release project idempotency reservation")
        raise


@router.get("/projects")
async def list_projects(current_user: UserModel = Depends(get_current_user)):
    visible_projects = []
    for ctx in projects.values():
        if _can_user_access_project(current_user, ctx):
            visible_projects.append(ctx.to_dict())
    return {"projects": visible_projects}

@router.get("/projects/{project_id}")
async def get_project(project_id: str):
    return _get_project(project_id).to_dict()

@router.patch("/projects/{project_id}")
async def update_project(project_id: str, request: ProjectRequest):
    ctx = _get_project(project_id)
    ctx.name = request.name
    ctx.description = request.description
    await _persist_all_async()
    return {"success": True, "project": ctx.to_dict()}

@router.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    if project_id not in projects:
        raise HTTPException(status_code=404, detail="项目不存在")
    ctx = projects[project_id]  # 先拿到上下文，获取 agent ID 列表
    from core.llm_usage import archive_project_llm_usage
    archive_project_llm_usage(
        user_id=str(getattr(ctx, "owner_user_id", "") or ""),
        project_id=project_id,
        project_name=ctx.name,
    )
    del projects[project_id]
    # 清理关联的全局状态（防内存泄漏）
    from core.orchestrator import remove_orchestrator
    remove_orchestrator(project_id)
    # 清理自动修复循环状态
    from api.routes_phases import _cleanup_auto_repair_states
    _cleanup_auto_repair_states(project_id)
    # 清理全栈工程师 Agent 缓存
    from api.routes_team import _engineer_agents
    _engineer_agents.pop(project_id, None)
    # 清理全局执行状态中属于此项目的 agent 条目（按 project_id 过滤）
    from api.routes_execution import execution_status, _fix_attempt_counts
    project_agent_ids = [aid for aid in ctx.agents.keys() if aid.startswith("agent-")]
    for aid in project_agent_ids:
        execution_status.pop(aid, None)
        _fix_attempt_counts.pop(aid, None)
    delete_project_files(project_id)
    await _persist_all_async()
    return {"success": True}
