"""User dashboard aggregated by project."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from core.app_state import projects
from core.auth import UserModel, get_current_user
from core.llm_usage import get_user_llm_usage


router = APIRouter(tags=["dashboard"])


def _project_display_status(ctx, agents: list[dict]) -> str:
    raw_status = str(getattr(ctx, "status", "") or "").lower()
    if raw_status == "completed":
        return "completed"
    terminal = {"completed", "failed", "model_failed", "cancelled", "canceled"}
    if agents and all(str(agent.get("status") or "").lower() in terminal for agent in agents):
        return "completed"
    if any(str(agent.get("status") or "").lower() in {"queued", "working", "running", "fixing", "re_checking"} for agent in agents):
        return "running"
    return raw_status or "pending"


def _agent_execution_duration(agent: dict) -> float | None:
    """Return the immutable execution interval, excluding later phase bookkeeping."""
    if str(agent.get("status") or "").lower() not in {
        "completed", "failed", "cancelled", "canceled",
    }:
        return None

    events = agent.get("lifecycle_events") or []
    started_at = None
    for event in events:
        status = str(event.get("status") or "").lower()
        message = str(event.get("message") or "").lower()
        at = event.get("at")
        if at is None:
            continue
        if started_at is None and (
            status in {"working", "running"} or "execution started" in message
        ):
            started_at = float(at)
            continue
        if started_at is not None and (
            status in {"completed", "failed", "cancelled", "canceled"}
            or "execution finished" in message
        ):
            finished_at = float(at)
            return max(0.0, finished_at - started_at)

    # Existing data may have had its real start/finish events evicted by old
    # polling replays. Do not present the mutable timestamps as real duration.
    if events:
        return None

    started_at = agent.get("started_at")
    finished_at = agent.get("finished_at")
    if started_at is None or finished_at is None:
        return None
    duration = float(finished_at) - float(started_at)
    return duration if duration >= 0 else None


@router.get("/dashboard/overview")
async def dashboard_overview(current_user: UserModel = Depends(get_current_user)):
    usage = get_user_llm_usage(str(current_user.user_id))
    rows = []
    live_project_ids = set()
    for project_id, ctx in projects.items():
        if str(getattr(ctx, "owner_user_id", "") or "") != str(current_user.user_id):
            continue
        live_project_ids.add(project_id)
        project_usage = (usage.get("projects") or {}).get(project_id, {})
        agents = list(ctx.agents.values())
        durations = [
            duration
            for agent in agents
            if (duration := _agent_execution_duration(agent)) is not None
        ]
        files = {
            str(path)
            for agent in agents
            for path in (agent.get("output_files") or [])
            if path
        }
        requests = int(project_usage.get("requests") or 0)
        rows.append({
            "project_id": project_id,
            "project_name": ctx.name,
            "status": _project_display_status(ctx, agents),
            "total_tokens": int(project_usage.get("total_tokens") or 0),
            "prompt_tokens": int(project_usage.get("prompt_tokens") or 0),
            "completion_tokens": int(project_usage.get("completion_tokens") or 0),
            "requests": requests,
            "average_agent_duration_seconds": round(sum(durations) / len(durations), 2) if durations else None,
            "delivery_file_count": len(files),
            "agent_count": len(agents),
            "series": project_usage.get("series") or [],
        })

    token_rows = [
        {
            "project_id": project_id,
            "project_name": (
                getattr(projects.get(project_id), "name", None)
                or project_usage.get("project_name")
                or project_id
            ),
            "status": "deleted" if project_usage.get("deleted") else "active",
            "total_tokens": int(project_usage.get("total_tokens") or 0),
        }
        for project_id, project_usage in (usage.get("projects") or {}).items()
        if project_id in live_project_ids or project_usage.get("deleted")
    ]
    token_project_ids = {row["project_id"] for row in token_rows}
    token_rows.extend(
        {
            "project_id": row["project_id"],
            "project_name": row["project_name"],
            "status": "active",
            "total_tokens": 0,
        }
        for row in rows
        if row["project_id"] not in token_project_ids
    )

    def average(field: str):
        values = [float(row[field]) for row in rows if row.get(field) is not None]
        return round(sum(values) / len(values), 2) if values else None

    return {
        "aggregation": "project_mean",
        "project_count": len(rows),
        "active_project_count": len(live_project_ids),
        "summary": {
            "total_tokens": sum(int(row["total_tokens"]) for row in token_rows),
            "total_model_requests": sum(int(row["requests"]) for row in rows),
            "average_agent_duration_seconds": average("average_agent_duration_seconds"),
            "average_delivery_files_per_project": average("delivery_file_count"),
        },
        "projects": rows,
        "token_projects": token_rows,
        "updated_at": usage.get("updated_at"),
    }
