"""Durable user/project-scoped LLM usage telemetry."""
from __future__ import annotations

import contextvars
import threading
import time
from typing import Any, Dict

from core.database import kv_get, kv_set


current_llm_scope: contextvars.ContextVar[Dict[str, str]] = contextvars.ContextVar(
    "current_llm_scope", default={}
)
_lock = threading.RLock()


def record_llm_usage(*, user_id: str, project_id: str, model: str,
                     usage: Dict[str, Any] | None = None,
                     cache_lookup: bool = False,
                     cache_hit: bool = False) -> None:
    if not user_id:
        return
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or prompt + completion)
    now = int(time.time())
    bucket = now - now % 300
    key = f"llm_usage:{user_id}"
    project_key = project_id or "unattributed"
    with _lock:
        data = kv_get(key, {"projects": {}})
        projects = data.setdefault("projects", {})
        project = projects.setdefault(project_key, {
            "requests": 0, "cache_lookups": 0, "cache_hits": 0,
            "cache_misses": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "total_tokens": 0, "models": {}, "series": [],
        })
        project["requests"] += 0 if cache_hit else 1
        project["cache_lookups"] = int(project.get("cache_lookups") or 0) + (1 if cache_lookup else 0)
        project["cache_hits"] += 1 if cache_hit else 0
        project["cache_misses"] = int(project.get("cache_misses") or 0) + (1 if cache_lookup and not cache_hit else 0)
        project["prompt_tokens"] += prompt
        project["completion_tokens"] += completion
        project["total_tokens"] += total
        if model and not cache_hit:
            project["models"][model] = project["models"].get(model, 0) + 1
        series = project["series"]
        point = next((item for item in series if item.get("timestamp") == bucket), None)
        if point is None:
            point = {"timestamp": bucket, "requests": 0, "cache_hits": 0, "tokens": 0}
            series.append(point)
        point["requests"] += 0 if cache_hit else 1
        point["cache_hits"] += 1 if cache_hit else 0
        point["tokens"] += total
        project["series"] = sorted(series, key=lambda item: item["timestamp"])[-576:]
        data["updated_at"] = now
        kv_set(key, data)


def get_user_llm_usage(user_id: str) -> Dict[str, Any]:
    return kv_get(f"llm_usage:{user_id}", {"projects": {}, "updated_at": None})


def archive_project_llm_usage(*, user_id: str, project_id: str, project_name: str) -> None:
    """Retain billable history while dropping deleted-project runtime metrics."""
    if not user_id or not project_id:
        return
    key = f"llm_usage:{user_id}"
    with _lock:
        data = kv_get(key, {"projects": {}})
        project = data.setdefault("projects", {}).setdefault(project_id, {
            "requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "models": {},
        })
        project["project_name"] = project_name or project_id
        project["deleted"] = True
        retained_tokens = int(project.get("total_tokens") or 0)
        project.clear()
        project.update({
            "project_name": project_name or project_id,
            "deleted": True,
            "total_tokens": retained_tokens,
        })
        data["updated_at"] = int(time.time())
        kv_set(key, data)
