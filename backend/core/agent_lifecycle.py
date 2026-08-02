"""Agent lifecycle helpers.

Centralizes status transitions so routes do not silently diverge.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional


TERMINAL_STATUSES = {
    "completed",
    "failed",
    "model_failed",
    "cancelled",
    "fix_limit_reached",
}
ACTIVE_STATUSES = {"queued", "working", "re_checking", "fixing"}

ALLOWED_TRANSITIONS = {
    "idle": {"queued", "working", "cancelled"},
    "queued": {"working", "failed", "cancelled"},
    "working": {
        "completed",
        "failed",
        "model_failed",
        "fix_required",
        "re_checking",
        "fix_limit_reached",
        "cancelled",
    },
    # A process restart may resume the underlying execution before repeating
    # the quality check, so re_checking -> working is a valid recovery edge.
    "re_checking": {"working", "completed", "fix_required", "failed", "cancelled"},
    "fix_required": {"queued", "working", "fixing", "fix_limit_reached", "cancelled"},
    "fixing": {"working", "re_checking", "completed", "failed", "fix_required", "fix_limit_reached"},
    "completed": {"queued", "working", "fix_required"},
    "failed": {"queued", "working", "fix_required"},
    "model_failed": {"queued", "working", "cancelled"},
    # A new user-triggered QA cycle may legitimately reopen an agent after the
    # previous cycle exhausted its repair budget.
    "fix_limit_reached": {"queued", "working", "fix_required", "cancelled"},
    "cancelled": {"queued", "working"},
}


def transition_agent(
    agent: Dict[str, Any],
    status: str,
    *,
    progress: Optional[int] = None,
    message: str = "",
) -> Dict[str, Any]:
    """Apply a guarded lifecycle transition to an agent dict."""
    current = agent.get("status", "idle")
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if status != current and allowed and status not in allowed:
        raise ValueError(f"Invalid agent status transition: {current} -> {status}")

    # Polling/recovery projections may observe the same terminal state many
    # times. A terminal transition is immutable: replaying it must not restart
    # its clock or evict the original lifecycle evidence.
    if status == current and status in TERMINAL_STATUSES:
        if progress is not None:
            agent["progress"] = max(0, min(100, int(progress)))
        return agent

    now = time.time()
    agent["status"] = status
    agent["updated_at"] = now
    if progress is not None:
        agent["progress"] = max(0, min(100, int(progress)))
    if status in ACTIVE_STATUSES:
        agent.setdefault("started_at", now)
        agent["heartbeat_at"] = now
    if status in TERMINAL_STATUSES:
        agent["finished_at"] = now
    if message:
        agent.setdefault("lifecycle_events", [])
        agent["lifecycle_events"] = agent["lifecycle_events"][-20:] + [{
            "status": status,
            "message": message,
            "at": now,
        }]
    return agent


def mark_stale_agents(projects: Dict[str, Any], timeout_seconds: int = 1800) -> int:
    """Mark long-running agents without heartbeat as failed."""
    now = time.time()
    changed = 0
    for ctx in projects.values():
        for agent in getattr(ctx, "agents", {}).values():
            if agent.get("status") in ACTIVE_STATUSES:
                heartbeat = agent.get("heartbeat_at") or agent.get("updated_at") or agent.get("created_at") or now
                if now - heartbeat > timeout_seconds:
                    transition_agent(agent, "failed", progress=0, message="Agent heartbeat timed out")
                    changed += 1
    return changed
