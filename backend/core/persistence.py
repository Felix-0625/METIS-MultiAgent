"""
数据持久化模块 v2 — 数据库版
将所有数据存入 PostgreSQL（生产）或 SQLite（本地开发回退）。
对外接口与 v1 完全一致，main.py 无需改动。

KV key 约定：
  projects                → 所有项目数据 dict
  agents_config           → agent API 配置 dict
  skills                  → skill 池 dict
  gitee_config            → gitee 配置 dict
  default_api_config      → 默认 API 配置 dict
  pm_teams                → PM 团队状态 dict
  phase_managers          → 阶段管理器状态 dict
  supervisor_leaders      → 监督 leader 状态 dict
  chat:{project_id}:{agent_type}  → 对话历史
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List

from core.database import kv_set, kv_get, kv_delete, kv_keys_prefix, kv_many_set
from core.secret_storage import clear_sensitive_values, protect_config, restore_config

logger = logging.getLogger(__name__)

# 保留 DATA_DIR 供 export_snapshot 写文件快照使用
DATA_DIR = Path("data")


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


# ─── 项目持久化 ───────────────────────────────────────────────────────────────

def save_projects(projects_data: Dict[str, Any]) -> None:
    kv_set("projects", projects_data)


def load_projects() -> Dict[str, Any]:
    return kv_get("projects", {})


# ─── Agent 配置持久化 ─────────────────────────────────────────────────────────

def save_agents_config(config: Dict[str, Any]) -> None:
    kv_set("agents_config", protect_config(config, context="agent API configuration"))


def load_agents_config() -> Dict[str, Any]:
    restored = restore_config(kv_get("agents_config", {}), context="agent API configuration")
    if restored.replacement is not None:
        kv_set("agents_config", restored.replacement)
    return restored.value if isinstance(restored.value, dict) else {}


# ─── Skill 池持久化 ───────────────────────────────────────────────────────────

def save_skills(skills: Dict[str, Any]) -> None:
    kv_set("skills", skills)


def load_skills() -> Dict[str, Any]:
    return kv_get("skills", {})


# ─── Gitee 配置持久化 ─────────────────────────────────────────────────────────

def save_gitee_config(config: Dict[str, Any]) -> None:
    kv_set("gitee_config", config)


def load_gitee_config() -> Dict[str, Any]:
    return kv_get("gitee_config", {})


# ─── 默认 API 配置持久化 ──────────────────────────────────────────────────────

def save_default_api_config(config: Dict[str, Any]) -> None:
    kv_set("default_api_config", protect_config(config, context="default API configuration"))


def load_default_api_config() -> Dict[str, Any]:
    restored = restore_config(kv_get("default_api_config", {}), context="default API configuration")
    if restored.replacement is not None:
        kv_set("default_api_config", restored.replacement)
    return restored.value if isinstance(restored.value, dict) else {}


# ─── 用户级 API 配置持久化（按 user_id 隔离）──────────────────────────────────

def save_user_api_configs(configs: Dict[str, Dict[str, Any]]) -> None:
    """保存所有用户的 API 配置"""
    kv_set("user_api_configs", protect_config(configs, context="user API configuration"))


def load_user_api_configs() -> Dict[str, Dict[str, Any]]:
    """加载所有用户的 API 配置"""
    restored = restore_config(kv_get("user_api_configs", {}), context="user API configuration")
    if restored.replacement is not None:
        kv_set("user_api_configs", restored.replacement)
    return restored.value if isinstance(restored.value, dict) else {}


def save_application_state(data: Dict[str, Any]) -> None:
    """Persist the main application state in one database transaction."""
    allowed = {
        "projects",
        "agents_config",
        "skills",
        "default_api_config",
        "user_api_configs",
        "user_skills",
        "pm_teams",
        "phase_managers",
        "supervisor_leaders",
        "engineer_agents",
        "idea_landing",
        "adjustments",
        "execution_status",
        "fix_attempt_counts",
        "auto_repair_states",
        "workspace_files",
    }
    persisted = {k: v for k, v in data.items() if k in allowed}
    for key in ("agents_config", "default_api_config", "user_api_configs"):
        if key in persisted:
            persisted[key] = protect_config(persisted[key], context=key.replace("_", " "))
    kv_many_set(persisted)


# ─── 快照工具（仍写本地文件，便于离线备份）──────────────────────────────────

def export_snapshot(projects_data: Dict, agents_config: Dict, skills: Dict) -> str:
    ensure_data_dir()
    ts = time.strftime("%Y%m%d_%H%M%S")
    snapshot_path = DATA_DIR / f"snapshot_{ts}.json"
    snapshot = {
        "exported_at": time.time(),
        "exported_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "projects": projects_data,
        # Offline snapshots must never become a plaintext credential export.
        "agents_config": clear_sensitive_values(agents_config),
        "skills": skills,
    }
    tmp = snapshot_path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        tmp.replace(snapshot_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return str(snapshot_path)


# ─── 对话历史持久化 ───────────────────────────────────────────────────────────

def _chat_key(project_id: str, agent_type: str) -> str:
    return f"chat:{project_id}:{agent_type}"


def save_chat_history(project_id: str, agent_type: str, messages: list) -> None:
    kept = messages[-200:] if len(messages) > 200 else messages
    kv_set(_chat_key(project_id, agent_type), {
        "project_id": project_id,
        "agent_type": agent_type,
        "updated_at": time.time(),
        "messages": kept,
    })


def load_chat_history(project_id: str, agent_type: str) -> list:
    data = kv_get(_chat_key(project_id, agent_type), {})
    return data.get("messages", [])


def clear_chat_history(project_id: str, agent_type: str) -> None:
    kv_delete(_chat_key(project_id, agent_type))


# ─── PM 团队状态持久化 ────────────────────────────────────────────────────────

def save_pm_teams(data: Dict[str, Any]) -> None:
    kv_set("pm_teams", data)


def load_pm_teams() -> Dict[str, Any]:
    return kv_get("pm_teams", {})


# ─── 阶段管理器状态持久化 ─────────────────────────────────────────────────────

def save_phase_managers(data: Dict[str, Any]) -> None:
    kv_set("phase_managers", data)


def load_phase_managers() -> Dict[str, Any]:
    return kv_get("phase_managers", {})


# ─── 监督 Leader 状态持久化 ───────────────────────────────────────────────────

def save_supervisor_leaders(data: Dict[str, Any]) -> None:
    kv_set("supervisor_leaders", data)


def load_supervisor_leaders() -> Dict[str, Any]:
    return kv_get("supervisor_leaders", {})


# ─── IdeaLanding Agent 持久化 ────────────────────────────────────────────────

def _idea_landing_key(user_id: str = "") -> str:
    if not str(user_id or "").strip():
        return "idea_landing"
    from core.user_scope import user_storage_key
    return f"idea_landing:user:{user_storage_key(user_id)}"


def save_idea_landing(data: Dict[str, Any], user_id: str = "") -> None:
    """保存按用户隔离的 IdeaLanding 完整状态。"""
    kv_set(_idea_landing_key(user_id), data)


def load_idea_landing(user_id: str = "") -> Dict[str, Any]:
    value = kv_get(_idea_landing_key(user_id), {})
    return value if isinstance(value, dict) else {}


def migrate_legacy_idea_landing_to_user(user_id: str) -> Dict[str, Any]:
    """将历史全局记录一次性归属给部署管理员，避免跨用户泄露。"""
    user_id = str(user_id or "").strip()
    if not user_id or load_idea_landing(user_id):
        return {}
    legacy = load_idea_landing()
    if not legacy:
        return {}
    save_idea_landing(legacy, user_id)
    kv_delete("idea_landing")
    return legacy

# --- execution_status persistence ---
def save_execution_status(data: dict) -> None:
    kv_set("execution_status", data)


def load_execution_status() -> dict:
    return kv_get("execution_status", {})


def save_fix_attempt_counts(data: dict) -> None:
    kv_set("fix_attempt_counts", data)


def load_fix_attempt_counts() -> dict:
    return kv_get("fix_attempt_counts", {})

