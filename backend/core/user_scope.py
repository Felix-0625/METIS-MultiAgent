from __future__ import annotations

import contextvars
import hashlib


current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_user_id", default=""
)


def active_user_id(explicit_user_id: str = "") -> str:
    return str(explicit_user_id or current_user_id.get() or "").strip()


def user_storage_key(user_id: str) -> str:
    normalized = active_user_id(user_id)
    if not normalized:
        return "legacy-global"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
