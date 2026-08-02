"""Project-scoped GitHub and Gitee repository synchronization routes."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException

from core.app_state import _get_project, _persist_all_async
from core.auth import UserModel, get_current_user
from core.gitee_sync import GiteeSync
from core.persistence import load_gitee_config, save_gitee_config
from models.schemas import GiteeConfigRequest, GiteePushRequest


router = APIRouter(tags=["git"])


def _project_for_user(project_id: str, current_user: UserModel):
    project = _get_project(project_id)
    owner_id = str(getattr(project, "owner_user_id", "") or "")
    if current_user.role != "admin" and owner_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="无权访问此项目")
    return project


def _all_bindings() -> dict:
    stored = load_gitee_config()
    projects = stored.get("projects") if isinstance(stored, dict) else None
    return dict(projects) if isinstance(projects, dict) else {}


def _binding(project_id: str) -> dict:
    value = _all_bindings().get(project_id, {})
    return dict(value) if isinstance(value, dict) else {}


def _save_binding(project_id: str, value: dict) -> None:
    bindings = _all_bindings()
    bindings[project_id] = value
    save_gitee_config({"schema_version": 2, "projects": bindings})


def _sync_for(project) -> GiteeSync:
    return GiteeSync(project.workspace)


@router.get("/projects/{project_id}/git/status")
async def git_status(project_id: str, current_user: UserModel = Depends(get_current_user)):
    project = _project_for_user(project_id, current_user)
    config = _binding(project_id)
    return _sync_for(project).get_status(
        provider=str(config.get("provider") or ""),
        repo_url=str(config.get("repo_url") or ""),
    )


@router.post("/projects/{project_id}/git/config")
async def configure_git(
    project_id: str,
    request: GiteeConfigRequest,
    current_user: UserModel = Depends(get_current_user),
):
    project = _project_for_user(project_id, current_user)
    result = _sync_for(project).set_remote(request.repo_url, request.token, request.provider)
    if not result.get("success"):
        return result
    _save_binding(project_id, {
        "provider": result["provider"],
        "repo_url": result["repo_url"],
        "token": request.token,
        "configured_at": time.time(),
        "owner_user_id": str(getattr(project, "owner_user_id", "") or current_user.user_id),
    })
    return result


@router.post("/projects/{project_id}/git/push")
async def push_to_git(
    project_id: str,
    request: GiteePushRequest,
    current_user: UserModel = Depends(get_current_user),
):
    project = _project_for_user(project_id, current_user)
    config = _binding(project_id)
    if not config.get("repo_url"):
        return {"success": False, "error": "请先为此项目绑定 Git 仓库"}
    return _sync_for(project).push(
        str(config.get("token") or ""),
        str(config.get("provider") or "Git"),
        request.commit_message or "",
    )


@router.post("/projects/{project_id}/git/pull")
async def pull_from_git(project_id: str, current_user: UserModel = Depends(get_current_user)):
    project = _project_for_user(project_id, current_user)
    config = _binding(project_id)
    if not config.get("repo_url"):
        return {"success": False, "error": "请先为此项目绑定 Git 仓库"}
    result = _sync_for(project).pull(
        str(config.get("token") or ""),
        str(config.get("provider") or "Git"),
    )
    if result.get("success"):
        await _persist_all_async()
    return result


@router.post("/projects/{project_id}/git/init")
async def init_git_repo(project_id: str, current_user: UserModel = Depends(get_current_user)):
    project = _project_for_user(project_id, current_user)
    return _sync_for(project).init_repo()


@router.api_route("/git/{operation:path}", methods=["GET", "POST"], include_in_schema=False)
@router.api_route("/gitee/{operation:path}", methods=["GET", "POST"], include_in_schema=False)
async def retired_global_git_route(operation: str):
    raise HTTPException(status_code=410, detail="全局仓库同步已停用，请先选择项目")
