"""Operator-facing reliability APIs with explicit authorization boundaries."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from core.app_state import _can_user_access_project, projects
from core.auth import UserModel, get_current_user
from core.security_audit import query_audit_events


router = APIRouter(tags=["reliability"])


@router.get("/audit/events")
async def list_audit_events(
    actor_id: str = "",
    project_id: str = "",
    action: str = "",
    outcome: str = "",
    before: Optional[float] = None,
    limit: int = 100,
    current_user: UserModel = Depends(get_current_user),
):
    """Return integrity-checked audit records visible to the caller."""
    allowed_projects = [
        project.project_id
        for project in projects.values()
        if _can_user_access_project(current_user, project)
    ]
    try:
        events = query_audit_events(
            current_user.user_id,
            current_user.role,
            allowed_project_ids=allowed_projects,
            actor_id=actor_id,
            project_id=project_id,
            action=action,
            outcome=outcome,
            before=before,
            limit=limit,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"events": [event.to_dict() for event in events], "count": len(events)}
