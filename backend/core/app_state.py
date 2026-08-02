"""应用全局状态与核心基础设施"""

import time
import uuid
import atexit
import os
import re
import json
import base64
import copy
import logging
import threading
import time as time_module
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from core.security_audit import install_secret_redaction

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
install_secret_redaction()
logger = logging.getLogger("app_state")
from core.workspace import WORKSPACE_ROOT, _safe_name, _project_workspace
from core.project_context import ProjectContext
from core.version_manager import VersionManager

import asyncio
from agents.pm_agent import PMAgent
from agents.hr_agent import HRAgent
from agents.sm_agent import SMAgent
from agents.pg_agent import PGAgent
from agents.supervisor_agent import SupervisorAgent
from agents.ccb_agent import CCBAgent
from agents.execution_agent import ExecutionAgent
from agents.pm_team import PMLeaderAgent, PMMemberAgent
from agents.supervisor_team import SupervisorLeaderAgent, PhaseSupervisionAgent
from agents.base.hermes_agent import AgentType
from core.hermes_client import HermesClient
from core.config_loader import ConfigLoader
from agents.base.memory import HybridMemory
from core.phase_manager import PhaseManager
from core.persistence import (
    save_projects, load_projects,
    save_agents_config, load_agents_config,
    save_skills, load_skills,
    save_gitee_config, load_gitee_config,
    export_snapshot,
    save_default_api_config, load_default_api_config,
    save_user_api_configs, load_user_api_configs,
    save_chat_history, load_chat_history, clear_chat_history,
    save_pm_teams, load_pm_teams,
    save_phase_managers, load_phase_managers,
    save_supervisor_leaders, load_supervisor_leaders,
    save_idea_landing, load_idea_landing, migrate_legacy_idea_landing_to_user,
    save_application_state,
)
from agents.idea_landing_agent import IdeaLandingAgent
from core.gitee_sync import GiteeSync
from core.mcp_server import MCPToolHandler, handle_mcp_request, TOOL_DEFINITIONS
from core.repair_loop import repair_registry, DefectStatus, ArbiterDecision
from core.expert_pool import ExpertPool, get_expert_pool
from core.global_agent_pool import get_global_agent_pool
from core.agent_lifecycle import mark_stale_agents
from core.state_schema import migrate_project_record, version_phase_manager_record
from core.workspace_integrity import (
    WORKSPACE_PERSIST_EXCLUDED_PARTS,
    WORKSPACE_PERSIST_FILE_LIMIT,
    WORKSPACE_PERSIST_PROJECT_LIMIT,
)
from core.database import load_project_files

# ─── 全局单例 ─────────────────────────────────────────────────────────────────
config_loader = ConfigLoader("config")
hermes_client = HermesClient()
_legacy_sm_agent = SMAgent(hermes_client=hermes_client)
_user_sm_agents: Dict[str, SMAgent] = {}
_persisted_user_skills: Dict[str, Dict[str, Any]] = {}


def get_user_sm_agent(user_id: str = "") -> SMAgent:
    from core.user_scope import active_user_id

    owner_id = active_user_id(user_id)
    if not owner_id:
        return _legacy_sm_agent
    agent = _user_sm_agents.get(owner_id)
    if agent is None:
        agent = SMAgent(hermes_client=hermes_client)
        source = _persisted_user_skills.get(owner_id)
        if not isinstance(source, dict):
            source = _legacy_sm_agent.skill_pool
        agent.skill_pool.update(copy.deepcopy(source))
        _user_sm_agents[owner_id] = agent
    return agent


class _UserScopedSkillAgent:
    def __getattr__(self, name: str):
        return getattr(get_user_sm_agent(), name)


global_sm_agent = _UserScopedSkillAgent()
gitee_sync = GiteeSync()
global_ccb_agent = CCBAgent(hermes_client=hermes_client)
from agents.pm_team import PMTeam as _PMTeam
from agents.supervisor_team import SupervisorTeam as _SupervisorTeam
global_pm_team = _PMTeam(hermes_client=hermes_client)
global_supervisor_team = _SupervisorTeam(hermes_client=hermes_client)
agents_api_config: Dict[str, Dict] = {}
DEFAULT_API_CONFIG = {
    "model": "deepseek-v4-flash",
    "api_base": "https://api.deepseek.com/v1",
    "api_key": "",
    "max_tokens": 20480,
    "temperature": 0.7,
    "thinking": {"type": "disabled"},
    "generator": {"model": "", "temperature": 0.1},
    "reviewer": {"model": "", "temperature": 0.0},
}

# ── 用户级 API 配置（按 user_id 隔离，支持多用户独立 Key）────────────────────
# 结构：{user_id: {model, api_base, api_key, max_tokens, temperature}}
# 由 routes_config.py 读写，persistence.py 持久化
# hermes_client.chat() 通过 contextvars 读取当前用户的配置
user_api_configs: Dict[str, Dict[str, Any]] = {}

_LEGACY_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_MAX_TOKENS = 20480


def _upgrade_default_token_limit(config: Dict[str, Any]) -> bool:
    """Migrate the previous default without overwriting custom limits."""
    if config.get("max_tokens") in (None, _LEGACY_DEFAULT_MAX_TOKENS):
        config["max_tokens"] = _DEFAULT_MAX_TOKENS
        return True
    return False

projects: Dict[str, ProjectContext] = {}

_WORKSPACE_EXCLUDED_PARTS = WORKSPACE_PERSIST_EXCLUDED_PARTS
_WORKSPACE_FILE_LIMIT = WORKSPACE_PERSIST_FILE_LIMIT
_WORKSPACE_PROJECT_LIMIT = WORKSPACE_PERSIST_PROJECT_LIMIT


def _snapshot_workspace(ctx: ProjectContext) -> Dict[str, str]:
    """Serialize project files into database-safe base64 so Render redeploys cannot erase them."""
    snapshot: Dict[str, str] = {}
    total_size = 0
    if not ctx.workspace.exists():
        return snapshot
    for path in sorted(ctx.workspace.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(ctx.workspace)
        if any(part in _WORKSPACE_EXCLUDED_PARTS for part in relative.parts):
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if len(content) > _WORKSPACE_FILE_LIMIT or total_size + len(content) > _WORKSPACE_PROJECT_LIMIT:
            logger.warning("Skip oversized workspace file during persistence: %s", relative)
            continue
        snapshot[relative.as_posix()] = base64.b64encode(content).decode("ascii")
        total_size += len(content)
    return snapshot


def _restore_workspace(ctx: ProjectContext, snapshot: Dict[str, str]) -> None:
    """Restore only paths contained by this project's workspace."""
    workspace = ctx.workspace.resolve()
    for relative, encoded in (snapshot or {}).items():
        try:
            if not isinstance(relative, str) or not relative:
                raise ValueError("workspace path must be a non-empty string")
            target = (workspace / relative).resolve()
            if target == workspace or workspace not in target.parents:
                logger.warning("Ignore unsafe persisted workspace path: %s", relative)
                continue
            content = base64.b64decode(encoded, validate=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Failed to restore workspace file %s: %s", relative, exc)


def _authoritative_workspace_snapshot(
    snapshot: Dict[str, str],
    authoritative_files: Dict[str, Dict[str, Any]],
) -> Dict[str, str]:
    """Prefer legacy CRLF delivery-doc bytes only when LF bytes are identical."""
    restored: Dict[str, str] = {}
    generated_paths = {"docs/metis/file-responsibility.json"}
    generated_prefix = "docs/metis/phase-deliveries/"
    for path, record in authoritative_files.items():
        content = bytes(record["content"])
        legacy_encoded = snapshot.get(path)
        is_delivery_document = (
            path in generated_paths
            or (path.startswith(generated_prefix) and path.endswith(".json"))
        )
        if is_delivery_document and isinstance(legacy_encoded, str):
            try:
                legacy = base64.b64decode(legacy_encoded, validate=True)
            except (ValueError, TypeError):
                legacy = b""
            if legacy != content and legacy.replace(b"\r\n", b"\n") == content:
                content = legacy
        restored[path] = base64.b64encode(content).decode("ascii")
    return restored


# ─── 持久化工具 ───────────────────────────────────────────────────────────────

# 尾缘合并：限制写入频率，但每次已等待完成的状态变更都必须落库。
_last_persist_time: float = 0.0
_PERSIST_DEBOUNCE_SEC: float = 1.0   # 最小写盘间隔（秒）
_persist_lock = asyncio.Lock()        # 防止并发写盘撕裂文件
_persist_thread_lock = threading.RLock()
_persist_revision: int = 0
_persisted_revision: int = 0


def _persist_all():
    """
    保存所有数据到本地文件（同步版，供 atexit / lifespan 关闭时调用）。
    关闭流程不能受防抖影响，始终强制保存最新状态。
    """
    global _last_persist_time, _persist_revision, _persisted_revision
    _persist_revision += 1
    _do_persist()
    _last_persist_time = time.monotonic()
    _persisted_revision = _persist_revision


async def _persist_all_async():
    """
    异步版持久化（供路由 handler 调用）。
    使用 asyncio.Lock 合并并发写入，并保证最后一次变更不会丢失。
    """
    global _last_persist_time, _persist_revision, _persisted_revision
    _persist_revision += 1
    requested_revision = _persist_revision
    async with _persist_lock:
        # A previous waiter may already have persisted this mutation.  Unlike
        # the old leading-edge debounce, every awaited mutation is guaranteed
        # to reach durable storage; rapid calls are coalesced, never dropped.
        if _persisted_revision >= requested_revision:
            return
        remaining = _PERSIST_DEBOUNCE_SEC - (time.monotonic() - _last_persist_time)
        if remaining > 0:
            await asyncio.sleep(remaining)
        target_revision = _persist_revision
        _last_persist_time = time.monotonic()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _do_persist)
        _last_persist_time = time.monotonic()
        _persisted_revision = target_revision


def _do_persist():
    """Serialize sync and async persistence through the same process lock."""
    with _persist_thread_lock:
        _do_persist_unlocked()


def _persistable_auto_repair_states() -> Dict[str, Dict[str, Any]]:
    """Return repair-loop state without the separate in-memory API configs."""
    try:
        from api.routes_phases import _auto_repair_states
    except ImportError as exc:
        raise RuntimeError("auto repair state registry is unavailable") from exc
    try:
        # Snapshot the top-level mapping first: the repair task can append a
        # message while persistence is running in a worker thread.  Copy the
        # mutable collections explicitly so JSON encoding never iterates a
        # dictionary whose size is changing.
        snapshot: Dict[str, Dict[str, Any]] = {}
        for key, raw_state in list(_auto_repair_states.items()):
            if not isinstance(raw_state, dict):
                continue
            state = dict(raw_state)
            if isinstance(state.get("messages"), list):
                state["messages"] = list(state["messages"])
            if isinstance(state.get("issue_report"), dict):
                state["issue_report"] = dict(state["issue_report"])
            snapshot[str(key)] = state
        return json.loads(json.dumps(snapshot, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        logger.exception("Failed to serialize auto repair states")
        raise RuntimeError("auto repair state is not serializable") from exc


def _restore_auto_repair_states(saved_states: Any) -> int:
    """Restore loop history and expose interrupted work as an explicit retry."""
    if not isinstance(saved_states, dict):
        return 0
    try:
        from api.routes_phases import _auto_repair_states
    except ImportError:
        return 0

    restored = 0
    for key, value in saved_states.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        if not any(key.startswith(f"{project_id}-") for project_id in projects):
            continue
        state = dict(value)
        if state.get("running"):
            # The coroutine itself cannot survive a process restart.  Never
            # report it as still running; retain the history and let the user
            # resume the same QC cycle or deliberately rebuild the phase.
            interrupted_status = str(state.get("status") or "")
            state["running"] = False
            state["status"] = "interrupted"
            state["interrupted_from_status"] = interrupted_status
            state["needs_manual"] = True
            state["action_required"] = {
                "message": "The quality loop was interrupted by a service restart.",
                "options": ["manual_edit", "retry_cycle", "rebuild_phase"],
            }
        _auto_repair_states[key] = state
        restored += 1
    return restored


def _persistable_adjustments() -> Dict[str, List[Dict[str, Any]]]:
    """Snapshot adjustment runs, recovery bytes, and acceptance evidence."""
    try:
        from api.routes_engineer import _adjustments
    except ImportError as exc:
        raise RuntimeError("adjustment state registry is unavailable") from exc
    snapshot: Dict[str, List[Dict[str, Any]]] = {}
    try:
        for project_id, raw_adjustments in list(_adjustments.items()):
            if not isinstance(raw_adjustments, list):
                continue
            snapshot[str(project_id)] = [
                dict(item)
                for item in list(raw_adjustments)
                if isinstance(item, dict)
            ]
        return json.loads(json.dumps(snapshot, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        logger.exception("Failed to serialize adjustment state")
        raise RuntimeError("adjustment state is not serializable") from exc


def _restore_adjustments(saved_adjustments: Any) -> int:
    """Restore durable adjustment state without reviving dead coroutines."""
    try:
        from api.routes_engineer import _adjustments
    except ImportError:
        return 0
    _adjustments.clear()
    if not isinstance(saved_adjustments, dict):
        return 0

    restored = 0
    for raw_project_id, raw_items in saved_adjustments.items():
        project_id = str(raw_project_id)
        if project_id not in projects or not isinstance(raw_items, list):
            continue
        project_items: List[Dict[str, Any]] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            try:
                item = json.loads(json.dumps(raw_item, ensure_ascii=False))
            except (TypeError, ValueError):
                logger.warning(
                    "Ignore non-serializable adjustment state [project=%s]",
                    project_id,
                )
                continue
            active_run = item.get("active_run")
            if isinstance(active_run, dict) and active_run.get("status") in {
                "queued",
                "executing",
            }:
                interrupted_from = str(active_run.get("status") or "")
                active_run["status"] = "interrupted"
                active_run["interrupted_from_status"] = interrupted_from
                active_run["interrupted_at"] = time.time()
                item["status"] = "recovery_required"
                item["recovery_required"] = True
                item["recovery_reason"] = (
                    "Adjustment worker was interrupted by a service restart"
                )
            elif item.get("status") in {"queued", "executing"}:
                item["status"] = "recovery_required"
                item["recovery_required"] = True
                item["recovery_reason"] = (
                    "Persisted adjustment has no recoverable active-run identity"
                )
            project_items.append(item)
            restored += 1
        _adjustments[project_id] = project_items
    return restored


def _persistable_engineer_agents() -> Dict[str, Dict[str, Any]]:
    """Snapshot repair proposals and confirmations owned by each project."""
    try:
        from api.routes_team import _engineer_agents
    except ImportError as exc:
        raise RuntimeError("Engineer state registry is unavailable") from exc
    snapshot: Dict[str, Dict[str, Any]] = {}
    for project_id, agent in list(_engineer_agents.items()):
        if project_id not in projects:
            continue
        try:
            payload = agent.to_persist()
            snapshot[str(project_id)] = json.loads(
                json.dumps(payload, ensure_ascii=False)
            )
        except (AttributeError, TypeError, ValueError) as exc:
            logger.exception(
                "Failed to serialize Engineer state [project=%s]",
                project_id,
            )
            raise RuntimeError(
                f"Engineer state is not serializable [project={project_id}]"
            ) from exc
    return snapshot


def _restore_engineer_agents(saved_agents: Any) -> int:
    """Rehydrate durable repair authority without reviving any coroutine."""
    try:
        from api.routes_team import (
            _build_engineer_context,
            _engineer_agents,
            _get_engineer,
        )
    except ImportError:
        return 0
    _engineer_agents.clear()
    if not isinstance(saved_agents, dict):
        return 0
    restored = 0
    for raw_project_id, raw_payload in saved_agents.items():
        project_id = str(raw_project_id)
        if project_id not in projects or not isinstance(raw_payload, dict):
            continue
        try:
            payload = json.loads(json.dumps(raw_payload, ensure_ascii=False))
            agent = _get_engineer(project_id)
            agent.from_persist(payload)
            # Planning/QC context can have advanced after the Agent snapshot;
            # repair authority is restored, while descriptive context is fresh.
            _build_engineer_context(project_id, agent)
            restored += 1
        except Exception as exc:
            _engineer_agents.pop(project_id, None)
            logger.warning(
                "Failed to restore Engineer state [project=%s]: %s",
                project_id,
                exc,
            )
    return restored


def _do_persist_unlocked():
    """实际执行写盘（同步，可在线程池中运行）"""
    # 保存 PM 团队状态（draft_plan / final_plan / plan_confirmed / conversation_history）
    pm_teams_data = {}
    for pid, leader in _pm_teams.items():
        if pid not in projects:
            continue
        try:
            pm_teams_data[pid] = leader.to_persist()
        except Exception as exc:
            raise RuntimeError(
                f"PM team state is not serializable [project={pid}]"
            ) from exc
    # 保存阶段管理器状态（phases / current_phase_index / file_registry）
    phase_managers_data = {}
    for pid, pm in _phase_managers.items():
        if pid not in projects:
            continue
        try:
            phase_managers_data[pid] = version_phase_manager_record(pm.to_dict())
        except Exception as exc:
            raise RuntimeError(
                f"phase manager state is not serializable [project={pid}]"
            ) from exc
    # 保存监督 Leader 状态（phase_supervisors / conversation_history）
    sup_leaders_data = {}
    for pid, leader in _supervisor_leaders.items():
        if pid not in projects:
            continue
        try:
            sup_leaders_data[pid] = leader.to_persist()
        except Exception as exc:
            raise RuntimeError(
                f"Supervisor state is not serializable [project={pid}]"
            ) from exc
    snapshot = {
        "projects": {pid: ctx.to_persist() for pid, ctx in projects.items()},
        "agents_config": agents_api_config,
        "skills": _legacy_sm_agent.skill_pool,
        "user_skills": {
            user_id: copy.deepcopy(agent.skill_pool)
            for user_id, agent in _user_sm_agents.items()
        },
        "default_api_config": DEFAULT_API_CONFIG,
        "user_api_configs": user_api_configs,
        "pm_teams": pm_teams_data,
        "phase_managers": phase_managers_data,
        "supervisor_leaders": sup_leaders_data,
        "engineer_agents": _persistable_engineer_agents(),
        "auto_repair_states": _persistable_auto_repair_states(),
        "adjustments": _persistable_adjustments(),
        "workspace_files": {pid: _snapshot_workspace(ctx) for pid, ctx in projects.items()},
    }
    try:
        json.dumps(snapshot, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("application state snapshot is not serializable") from exc
    save_application_state(snapshot)


def _restore_from_disk():
    """启动时从本地文件恢复数据"""
    # 恢复默认 API 配置（最先恢复，后续 Agent 调用需要）
    saved_default = load_default_api_config()
    if saved_default and not isinstance(saved_default, dict):
        logger.error("Ignore invalid persisted default API config payload")
    elif saved_default:
        default_tokens_upgraded = _upgrade_default_token_limit(saved_default)
        DEFAULT_API_CONFIG.update(saved_default)
        hermes_client.update_config(DEFAULT_API_CONFIG)
        if default_tokens_upgraded:
            save_default_api_config(DEFAULT_API_CONFIG)

    # 恢复用户级 API 配置（按 user_id 隔离）
    saved_user_configs = load_user_api_configs()
    user_api_configs.clear()
    if saved_user_configs and not isinstance(saved_user_configs, dict):
        logger.error("Ignore invalid persisted user API config payload")
    elif saved_user_configs:
        user_tokens_upgraded = False
        for config in saved_user_configs.values():
            if isinstance(config, dict):
                user_tokens_upgraded = _upgrade_default_token_limit(config) or user_tokens_upgraded
        user_api_configs.update(saved_user_configs)
        if user_tokens_upgraded:
            save_user_api_configs(user_api_configs)
        logger.info("恢复了 %d 个用户的 API 配置", len(saved_user_configs))

    # 恢复项目
    saved = load_projects()
    from core.database import kv_get
    saved_workspace_files = kv_get("workspace_files", {})
    if not isinstance(saved, dict):
        logger.error("Ignore invalid persisted projects payload: expected object, got %s", type(saved).__name__)
        saved = {}
    if not isinstance(saved_workspace_files, dict):
        logger.error("Ignore invalid persisted workspace payload: expected object, got %s", type(saved_workspace_files).__name__)
        saved_workspace_files = {}

    # Restore into a fresh map so an explicit reload (for example after a
    # remote pull) cannot leave deleted projects in memory.
    restored_projects: Dict[str, ProjectContext] = {}
    for raw_pid, data in saved.items():
        pid = str(raw_pid)
        if not isinstance(data, dict):
            logger.error("Skip invalid persisted project %s: expected object", pid)
            continue
        try:
            data = migrate_project_record(data)
            persisted_pid = str(data.get("project_id") or pid)
            if persisted_pid != pid:
                logger.warning(
                    "Normalize mismatched persisted project id: key=%s payload=%s",
                    pid, persisted_pid,
                )
            ctx = ProjectContext(
                project_id=pid,
                name=str(data.get("name") or "Untitled project"),
                description=str(data.get("description") or ""),
                status=str(data.get("status") or "planning"),
                created_at=data.get("created_at"),
                subprojects=data.get("subprojects") if isinstance(data.get("subprojects"), list) else [],
                agents=data.get("agents") if isinstance(data.get("agents"), dict) else {},
                owner_user_id=str(data.get("owner_user_id") or ""),
                schema_version=int(data.get("schema_version") or 1),
                record_version=int(data.get("record_version") or 1),
                updated_at=data.get("updated_at"),
                record_scope=str(data.get("record_scope") or "production"),
                hermes_client=hermes_client,
                global_sm_agent=get_user_sm_agent(str(data.get("owner_user_id") or "")),
            )
            qc_results = data.get("qc_results", {})
            ctx.qc_results = qc_results if isinstance(qc_results, dict) else {}
            supervisor_quality_runs = data.get("supervisor_quality_runs", {})
            ctx.supervisor_quality_runs = (
                supervisor_quality_runs
                if isinstance(supervisor_quality_runs, dict)
                else {}
            )
            signoff_receipt = data.get("signoff_receipt")
            ctx.signoff_receipt = (
                copy.deepcopy(signoff_receipt)
                if isinstance(signoff_receipt, dict)
                else None
            )
            restored_projects[pid] = ctx
            snapshot = saved_workspace_files.get(pid, {})
            _restore_workspace(ctx, snapshot if isinstance(snapshot, dict) else {})
            authoritative_files = load_project_files(pid)
            _restore_workspace(
                ctx,
                _authoritative_workspace_snapshot(
                    snapshot if isinstance(snapshot, dict) else {},
                    authoritative_files,
                ),
            )
        except Exception as exc:
            logger.exception("Skip project that failed to restore [project=%s]: %s", pid, exc)

    projects.clear()
    projects.update(restored_projects)

    restored_adjustments = _restore_adjustments(kv_get("adjustments", {}))
    if restored_adjustments:
        logger.info("Restored %d adjustment records", restored_adjustments)

    # 恢复 Agent API 配置
    saved_cfg = load_agents_config()
    agents_api_config.clear()
    if isinstance(saved_cfg, dict):
        agents_api_config.update(saved_cfg)
    else:
        logger.error("Ignore invalid persisted agent config payload")

    stale_count = mark_stale_agents(projects)
    if stale_count:
        logger.warning("Marked %d stale agents as failed during restore", stale_count)

    # 恢复 Skill 池
    saved_skills = load_skills()
    if not saved_skills:
        # 兜底：从 skills.json 文件加载（init_skills.py 写入的数据）
        skills_file = Path(__file__).resolve().parents[1] / "data" / "skills.json"
        if skills_file.exists():
            try:
                saved_skills = json.loads(skills_file.read_text(encoding="utf-8"))
                logger.info("从 skills.json 加载了 %d 个 Skill", len(saved_skills))
            except Exception as e:
                logger.warning("从 skills.json 加载 Skill 失败: %s", e)
    _legacy_sm_agent.skill_pool.clear()
    if isinstance(saved_skills, dict):
        _legacy_sm_agent.skill_pool.update(saved_skills)
    else:
        logger.error("Ignore invalid persisted skill payload")

    saved_user_skills = kv_get("user_skills", {})
    _persisted_user_skills.clear()
    _user_sm_agents.clear()
    if isinstance(saved_user_skills, dict):
        _persisted_user_skills.update({
            str(user_id): skills
            for user_id, skills in saved_user_skills.items()
            if isinstance(skills, dict)
        })

    # 恢复 PM 团队状态
    saved_pm_teams = load_pm_teams()
    _pm_teams.clear()
    if not isinstance(saved_pm_teams, dict):
        logger.error("Ignore invalid persisted PM team payload")
        saved_pm_teams = {}
    for pid, data in saved_pm_teams.items():
        if pid not in projects:
            continue  # 项目已删除，跳过
        try:
            memory = HybridMemory(f"memory/{pid}_pm_team")
            leader = PMLeaderAgent(hermes_client=hermes_client, memory_store=memory)
            leader.from_persist(data)
            _pm_teams[pid] = leader
        except Exception as e:
            logger.warning("恢复 PM 团队状态失败 [project=%s]: %s", pid, e)

    # 恢复阶段管理器状态
    saved_phase_managers = load_phase_managers()
    _phase_managers.clear()
    if not isinstance(saved_phase_managers, dict):
        logger.error("Ignore invalid persisted phase manager payload")
        saved_phase_managers = {}
    for pid, data in saved_phase_managers.items():
        if pid not in projects:
            continue
        try:
            ctx = projects[pid]
            pm = PhaseManager(pid, ctx.workspace)
            pm.from_dict(data)
            _phase_managers[pid] = pm
        except Exception as e:
            logger.warning("恢复阶段管理器失败 [project=%s]: %s", pid, e)

    restored_engineers = _restore_engineer_agents(kv_get("engineer_agents", {}))
    if restored_engineers:
        logger.info("Restored %d Engineer agents", restored_engineers)

    restored_repair_states = _restore_auto_repair_states(kv_get("auto_repair_states", {}))
    if restored_repair_states:
        logger.info("Restored %d auto repair loop states", restored_repair_states)

    # 恢复监督 Leader 状态
    saved_sup_leaders = load_supervisor_leaders()
    _supervisor_leaders.clear()
    if not isinstance(saved_sup_leaders, dict):
        logger.error("Ignore invalid persisted supervisor payload")
        saved_sup_leaders = {}
    for pid, data in saved_sup_leaders.items():
        if pid not in projects:
            continue
        try:
            memory = HybridMemory(f"memory/{pid}_sup_leader")
            leader = SupervisorLeaderAgent(
                hermes_client=hermes_client,
                memory_store=memory,
                project_id=pid,
            )
            leader.from_persist(data)
            _supervisor_leaders[pid] = leader
        except Exception as e:
            logger.warning("恢复 SupervisorLeader 失败 [project=%s]: %s", pid, e)

    # 恢复 RepairRegistry 状态
    from core.repair_persistence import load_repair_registry
    try:
        load_repair_registry()
    except Exception as e:
        logger.warning("恢复 RepairRegistry 失败: %s", e)


# ─── 安全配置 ─────────────────────────────────────────────────────────────────

# CORS 默认值：开发阶段允许 localhost，生产环境必须通过环境变量显式设置域名
# 容器内不暴露 3000/1420/5173 等开发端口，默认仅允许同源和 127.0.0.1 调试
# 端口 3000 = React 前端，端口 8080 = 宣传网页，端口 5173 = Vite dev server
# 注意：不包含 "null" —— 允许 null Origin 会使 CORS 形同虚设（file:// 协议等）
ALLOWED_ORIGINS = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000,http://localhost:8080,http://127.0.0.1:8080,http://localhost:5173,http://127.0.0.1:5173"
).split(",")
ALLOWED_ORIGINS = [o.strip() for o in ALLOWED_ORIGINS if o.strip() and o.strip() != "null"]

# 请求体大小限制（10MB）
MAX_BODY_SIZE = int(os.environ.get("MAX_BODY_SIZE", 10 * 1024 * 1024))


# ─── 简易账号限流器 ───────────────────────────────────────────────────────────

_rate_records: Dict[str, list] = defaultdict(list)
# 限流配置：读请求和写请求分桶。
# 前端登录后会同时加载项目详情、阶段、Agent、指标、质检、聊天历史等多组 GET 请求，
# 如果读/写共用同一个 120 RPM 桶，页面自动刷新/轮询可能会消耗掉“开始阶段”等关键 POST 操作额度。
RATE_LIMIT_READ_RPM = int(os.environ.get("RATE_LIMIT_READ_RPM", "600"))  # 页面加载/轮询类 GET 请求额度
RATE_LIMIT_WRITE_RPM = int(os.environ.get("RATE_LIMIT_WRITE_RPM", os.environ.get("RATE_LIMIT_RPM", "120")))  # 写操作额度
RATE_LIMIT_LOGIN_RPM = int(os.environ.get("RATE_LIMIT_LOGIN_RPM", "30"))  # 登录接口每分钟请求数
RATE_LIMIT_WINDOW = 60  # 窗口秒数


@lru_cache(maxsize=1)
def _declared_openapi_routes():
    routes = []
    for template, path_item in app.openapi().get("paths", {}).items():
        segments = template.strip("/").split("/")
        static = [segment for segment in segments if not segment.startswith("{")]
        pattern = re.compile(
            "^/"
            + "/".join(
                "[^/]+"
                if segment.startswith("{") and segment.endswith("}")
                else re.escape(segment)
                for segment in segments
            )
            + "$"
        )
        methods = {
            method.upper()
            for method in path_item
            if method.lower() in {
                "get", "post", "put", "patch", "delete", "options", "head", "trace"
            }
        }
        routes.append((
            (len(static), sum(map(len, static))),
            pattern,
            methods,
        ))
    return routes


def _declared_route_resolution(request: Request) -> tuple[str, set[str]]:
    path = str(request.scope.get("path") or request.url.path)
    candidates = [
        (score, methods)
        for score, pattern, methods in _declared_openapi_routes()
        if pattern.fullmatch(path)
    ]
    if not candidates:
        return "not_found", set()
    best_score = max(score for score, _methods in candidates)
    allowed = {
        method
        for score, methods in candidates
        if score == best_score
        for method in methods
    }
    return (
        ("full" if request.method in allowed else "method_not_allowed"),
        allowed,
    )


async def rate_limit_middleware(request: Request, call_next):
    """
    基于账号的内存限流中间件。
    - 已登录用户：按 user_id 限流（从Cookie/Header提取Token）
    - 未登录用户（如登录接口）：按 IP 限流
    - 读请求和写请求分桶，避免自动刷新/轮询挤占关键操作额度
    
    注意：此中间件在认证中间件之前执行，需要自己解析Token获取user_id
    """
    if os.environ.get("METIS_TEST_MODE", "").strip().lower() in {"1", "true", "yes"}:
        return await call_next(request)

    path = request.url.path

    # 健康检查、预检请求不限流
    if path in ("/health", "/nginx-health") or request.method == "OPTIONS":
        return await call_next(request)

    # Do not let an exhausted rate-limit bucket mask the router's authoritative
    # 404/405 response for a path/method that has no matching operation.
    if _declared_route_resolution(request)[0] != "full":
        return await call_next(request)

    bucket = "read" if request.method in {"GET", "HEAD"} else "write"

    # 确定限流键：尝试从Token中提取user_id，否则使用IP
    rate_limit_key = None
    limit = RATE_LIMIT_READ_RPM if bucket == "read" else RATE_LIMIT_WRITE_RPM
    
    # 尝试从Token中提取user_id（限流中间件先于认证中间件执行）
    from core.auth import AUTH_COOKIE_NAME, decode_token
    token = None
    
    # 1. Bearer Token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    # 2. HttpOnly Cookie
    if not token:
        token = request.cookies.get(AUTH_COOKIE_NAME)
    
    if token:
        try:
            payload = decode_token(token)
            user_id = payload.get("sub", "")
            if user_id:
                # 已登录：按账号限流
                rate_limit_key = f"user:{user_id}"
        except Exception:
            pass  # Token无效，fallback到IP限流
    
    if not rate_limit_key:
        # 未登录或Token无效：按IP限流
        client_ip = request.client.host if request.client else "unknown"
        rate_limit_key = f"ip:{client_ip}"
        # 登录接口保持严格限制
        if path == "/auth/login":
            limit = RATE_LIMIT_LOGIN_RPM

    # 将读/写请求放入不同限流桶，登录接口单独使用 login 桶。
    bucket_key = "login" if path == "/auth/login" else bucket
    rate_limit_key = f"{rate_limit_key}:{bucket_key}"

    now = time_module.monotonic()
    records = _rate_records[rate_limit_key]
    # 清除过期记录
    _rate_records[rate_limit_key] = [t for t in records if now - t < RATE_LIMIT_WINDOW]

    if len(_rate_records[rate_limit_key]) >= limit:
        logger.warning("限流触发: key=%s method=%s path=%s requests=%d limit=%d",
                       rate_limit_key, request.method, path, len(_rate_records[rate_limit_key]), limit)
        return JSONResponse(
            status_code=429,
            content={"detail": f"请求过于频繁，每分钟最多 {limit} 次请求"},
        )

    _rate_records[rate_limit_key].append(now)
    return await call_next(request)


# ─── 请求体大小限制中间件 ─────────────────────────────────────────────────────

async def body_size_limit_middleware(request: Request, call_next):
    """限制请求体大小，防止内存溢出"""
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_SIZE:
        return JSONResponse(
            status_code=413,
            content={"detail": f"请求体过大，最大允许 {MAX_BODY_SIZE // 1024 // 1024}MB"},
        )
    return await call_next(request)


# ─── 生命周期 ─────────────────────────────────────────────────────────────────


def _uses_durable_locked_phase_recovery(
    project_id: str,
    agent_id: str,
    agent: Dict[str, Any],
    run_registry: Any = None,
    execution_record: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether startup recovery must leave this Agent to a durable run.

    The legacy recovery loop predates both manual durable executions and locked
    phase coordinators. Replaying one directly would bypass its run lease (and,
    for a phase task, the parent coordinator lease and child receipt), allowing
    two attempts to write concurrently.
    """
    durable_run_id = str((execution_record or {}).get("run_id") or "")
    if durable_run_id:
        # The ordinary manual Agent endpoint is durable too. Startup already
        # schedules its pending run before entering this legacy loop, so a
        # second direct replay would race the same Agent and workspace.
        if run_registry is None:
            return True
        try:
            durable_run = run_registry.get(durable_run_id)
        except Exception:
            # An indeterminate persisted run is not permission to execute it a
            # second time. Leave operator/recovery reconciliation fail-closed.
            return True
        durable_payload = durable_run.get("payload") or {}
        if (
            str(durable_run.get("run_type") or "") == "agent.execute"
            and str(
                durable_payload.get("project_id")
                or durable_run.get("project_id")
                or ""
            ) == project_id
            and str(durable_payload.get("agent_id") or "") == agent_id
        ):
            return True

    phase_manager = _phase_managers.get(project_id)
    if phase_manager is None:
        return False
    project_locked = bool(
        (getattr(phase_manager, "project_contract", {}) or {}).get("locked")
    )
    agent_phase_id = str(agent.get("phase_id") or "")

    for phase in getattr(phase_manager, "phases", []) or []:
        if not isinstance(phase, dict):
            continue
        phase_id = str(phase.get("phase_id") or phase.get("id") or "")
        plan = phase.get("execution_dispatch_plan") or {}
        plan_agent_ids = {
            str(task.get("agent_id") or "")
            for wave in (plan.get("waves") or [])
            for task in (wave or [])
            if isinstance(task, dict) and str(task.get("agent_id") or "")
        }
        spec_agent_ids = {
            str(spec.get("agent_id") or "")
            for spec in (phase.get("execution_run_specs") or [])
            if isinstance(spec, dict) and str(spec.get("agent_id") or "")
        }
        belongs_to_phase = (
            bool(agent_phase_id and phase_id and agent_phase_id == phase_id)
            or agent_id in {
                str(item) for item in (phase.get("agents") or []) if str(item)
            }
            or agent_id in plan_agent_ids
            or agent_id in spec_agent_ids
        )
        if not belongs_to_phase:
            continue
        generation = str(phase.get("execution_generation") or "")
        coordinator = phase.get("execution_coordinator") or {}
        coordinator_id = str(coordinator.get("durable_run_id") or "")
        coordinator_generation = str(
            coordinator.get("execution_generation") or ""
        )
        coordinator_is_current = bool(
            coordinator_id
            and (
                not coordinator_generation
                or coordinator_generation == generation
            )
        )

        # Phase metadata such as execution_generation, run specs and even
        # locked_tasks also exists on legacy/non-locked plans.  Only a locked
        # project whose matching phase has a current durable coordinator is
        # owned by the DAG recovery path.
        if project_locked and coordinator_is_current:
            return True

        receipts: List[Dict[str, Any]] = [
            receipt
            for receipt in (agent.get("task_execution_receipts") or {}).values()
            if isinstance(receipt, dict)
        ]
        for attempt in (
            agent.get("pre_qa_repair_attempt_receipts") or {}
        ).values():
            if not isinstance(attempt, dict):
                continue
            receipts.extend(
                receipt
                for receipt in attempt.values()
                if isinstance(receipt, dict)
            )

        candidate_payloads: List[Dict[str, Any]] = []
        if agent.get("phase_coordinator_run_id"):
            candidate_payloads.append(agent)
        for receipt in receipts:
            if (
                receipt.get("phase_coordinator_run_id")
                or receipt.get("repair_coordinator_run_id")
            ):
                candidate_payloads.append(receipt)
            run_id = str(
                receipt.get("completion_run_id")
                or receipt.get("start_run_id")
                or ""
            )
            if not run_id or run_registry is None:
                continue
            try:
                run = run_registry.get(run_id)
            except Exception:
                continue
            payload = run.get("payload") or {}
            if isinstance(payload, dict):
                candidate_payloads.append(payload)

        for payload in candidate_payloads:
            payload_parent = str(
                payload.get("phase_coordinator_run_id")
                or payload.get("repair_coordinator_run_id")
                or ""
            )
            payload_phase = str(payload.get("phase_id") or "")
            payload_generation = str(
                payload.get("execution_generation") or ""
            )
            payload_agent = str(payload.get("agent_id") or "")
            if (
                coordinator_is_current
                and payload_parent == coordinator_id
                and payload_phase == phase_id
                and payload_generation == generation
                and (not payload_agent or payload_agent == agent_id)
            ):
                return True
    return False


async def _resume_interrupted_execution_tasks() -> int:
    """Resume durable Agent jobs whose asyncio tasks were lost on restart."""
    if os.environ.get("METIS_TEST_MODE", "").strip().lower() in {"1", "true", "yes"}:
        return 0

    from api.routes_execution import (
        _run_registry,
        _run_agent_task,
        _safe_create_task,
        _start_phase_quality_cycle_if_ready,
        execution_status,
    )
    from core.agent_lifecycle import ACTIVE_STATUSES, transition_agent
    from core.hermes_client import current_user_api_config

    resumed = 0
    failed = 0

    async def _resume_with_user_config(kwargs: Dict[str, Any], api_config: Optional[Dict[str, Any]]) -> None:
        token = current_user_api_config.set(api_config)
        try:
            await _run_agent_task(**kwargs, user_api_config=api_config)
        finally:
            current_user_api_config.reset(token)

    for project_id, ctx in projects.items():
        owner_user_id = str(getattr(ctx, "owner_user_id", "") or "")
        owner_config = (
            user_api_configs.get(owner_user_id)
            or DEFAULT_API_CONFIG
        )
        has_api_key = bool(
            (owner_config or {}).get("api_key")
            or getattr(hermes_client, "api_key", "")
        )
        for agent_id, agent in list(ctx.agents.items()):
            if agent.get("status") not in ACTIVE_STATUSES:
                continue
            # Durable locked phases were already recovered by
            # resume_pending_execution_runs().  Never fall through to the old
            # direct Agent replay, including when the durable child is blocked
            # and the read-model still says "working".
            if _uses_durable_locked_phase_recovery(
                project_id,
                str(agent_id),
                agent,
                _run_registry,
                execution_status.get(agent_id),
            ):
                continue
            subproject_id = agent.get("subproject_id", "")
            subproject = next(
                (item for item in ctx.subprojects if item.get("id") == subproject_id),
                None,
            )
            if not subproject or not has_api_key:
                reason = (
                    "Interrupted execution cannot resume because its subproject is missing"
                    if not subproject
                    else "Interrupted execution requires a configured model API key"
                )
                transition_agent(agent, "failed", progress=0, message=reason)
                execution_status[agent_id] = {
                    "status": "failed",
                    "progress": 0,
                    "output_files": agent.get("output_files", []),
                    "logs": [],
                    "error": reason,
                }
                failed += 1
                continue

            description = subproject.get("description", "")
            if agent.get("status") in {"fixing", "re_checking"} and agent.get("fix_task"):
                description = (
                    f"{description}\n\n[QUALITY REPAIR TASK]\n{agent['fix_task']}"
                )
            kwargs = {
                "project_id": project_id,
                "agent_id": agent_id,
                "subproject_id": subproject_id,
                "subproject_name": subproject.get("name", agent.get("subproject_name", "Task")),
                "description": description,
                "tech_stack": subproject.get("tech_stack", []),
                "project_context": ctx.pm.context_summary or ctx.description or "",
            }
            _safe_create_task(
                _resume_with_user_config(kwargs, owner_config),
                name=f"resume-agent-{agent_id}",
            )
            resumed += 1
        # Preflight failures and persisted terminal Agent states must close a
        # rebuild before any user can accidentally create a duplicate run.
        try:
            await _start_phase_quality_cycle_if_ready(ctx)
        except Exception:
            logger.exception(
                "Skipping invalid project quality recovery during startup: %s",
                project_id,
            )

    if resumed or failed:
        logger.warning(
            "Recovered interrupted Agent jobs: resumed=%d failed_preflight=%d",
            resumed,
            failed,
        )
        await _persist_all_async()
    return resumed


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化数据库（建表，幂等）
    from core.database import init_db
    try:
        init_db()
    except Exception as e:
        # A configured PostgreSQL deployment must not come up against an
        # accidental local SQLite database.  Starting without durable state is
        # more dangerous than failing the health check clearly.
        logger.exception("数据库初始化失败，服务拒绝启动: %s", e)
        raise

    # Reconcile only expired durable leases. Active leases remain untouched,
    # while abandoned work is retried with backoff or made explicitly blocked.
    from core.execution_runs import DurableRunRegistry
    recovered_runs = DurableRunRegistry().recover_startup()
    if recovered_runs:
        logger.warning(
            "Recovered expired durable execution leases: count=%d statuses=%s",
            len(recovered_runs),
            {run["status"] for run in recovered_runs},
        )
    # `python main.py` is the documented local startup path and does not pass
    # through startup.sh. Seed the built-in skills here as well so local and
    # container startup expose the same product capabilities.
    from core.database import kv_get
    if not kv_get("skills", {}):
        logger.info("Skill 池为空，正在初始化内置 Skills")
        try:
            import contextlib
            import io
            from init_skills import main as initialize_builtin_skills
            # init_skills is also a CLI and prints Unicode status icons. Capture
            # that output so Windows services using a GBK console do not fail
            # product initialization on an otherwise harmless log character.
            init_output = io.StringIO()
            with contextlib.redirect_stdout(init_output), contextlib.redirect_stderr(init_output):
                initialize_builtin_skills()
            logger.info("内置 Skill 初始化完成")
        except Exception as exc:
            logger.exception("内置 Skill 初始化失败，服务拒绝启动: %s", exc)
            raise RuntimeError("Built-in skill initialization failed") from exc
    # 启动时恢复数据
    _restore_from_disk()
    from api.routes_adjustments import reconcile_interrupted_final_qa_runs
    recovered_final_qa = await reconcile_interrupted_final_qa_runs()
    if recovered_final_qa:
        logger.warning(
            "Reclaimed interrupted Final QA generations: count=%d",
            recovered_final_qa,
        )
    from api.routes_execution import resume_pending_execution_runs
    resumed_runs = await resume_pending_execution_runs()
    if resumed_runs:
        logger.info(
            "Resumed durable execution runs after project restore: count=%d",
            resumed_runs,
        )
    await _resume_interrupted_execution_tasks()
    # 确保管理员账号存在
    try:
        from core.auth import ensure_admin_user
        ensure_admin_user()
    except Exception as e:
        logger.warning("管理员账号初始化失败: %s", e)
    # 修复用户索引（仅启动时调用一次，处理历史遗留或异常崩溃后的不一致）
    try:
        from core.auth import repair_user_indexes
        repair_user_indexes()
    except Exception as e:
        logger.warning("用户索引修复失败: %s", e)
    # 清理超时未验证账号（24小时 TTL）
    try:
        from core.auth import cleanup_unverified_accounts
        cleanup_unverified_accounts()
    except Exception as e:
        logger.warning("未验证账号清理失败: %s", e)
    # 启动定时清理任务（每小时执行一次）
    _cleanup_task = None
    _execution_recovery_task = None
    try:
        async def _periodic_cleanup():
            while True:
                await asyncio.sleep(3600)
                try:
                    cleanup_unverified_accounts()
                except Exception as e:
                    logger.warning("定时清理未验证账号失败: %s", e)
        _cleanup_task = asyncio.create_task(_periodic_cleanup())
    except Exception as e:
        logger.warning("启动定时清理任务失败: %s", e)
    try:
        async def _periodic_execution_recovery():
            # A process can restart while a durable lease is still valid. The
            # one-shot startup recovery must therefore be followed by a small
            # watchdog that reclaims it immediately after lease expiry.
            while True:
                await asyncio.sleep(15)
                try:
                    await resume_pending_execution_runs()
                except Exception as e:
                    logger.exception("定时恢复中断执行任务失败: %s", e)
        _execution_recovery_task = asyncio.create_task(
            _periodic_execution_recovery()
        )
    except Exception as e:
        logger.warning("启动执行恢复任务失败: %s", e)
    yield
    # 关闭时取消定时任务
    if _cleanup_task:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
    if _execution_recovery_task:
        _execution_recovery_task.cancel()
        try:
            await _execution_recovery_task
        except asyncio.CancelledError:
            pass
    # 关闭时保存数据
    await _persist_all_async()


app = FastAPI(title="Metis - Multi-Agent System", version="0.4.0", lifespan=lifespan)

# CORS 中间件（生产环境通过 CORS_ORIGINS 环境变量限定域名）
# 本地开发默认允许 localhost:* 和 file:// 协议（Origin=null）
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "X-Requested-With",
    ],
)

# 请求体大小限制中间件
app.middleware("http")(body_size_limit_middleware)


# ─── 全局认证中间件 ───────────────────────────────────────────────────────────
# 默认所有端点需要登录，白名单除外
AUTH_WHITELIST = {
    "/", "/health", "/nginx-health",
    "/auth/login",
    "/mcp",  # dedicated MCP bearer authentication is enforced by the route
    "/openapi.json", "/docs", "/redoc",
    "/favicon.ico",
}

AUTH_WHITELIST_PREFIXES = (
    "/auth",         # /auth/login, /auth/logout
    "/ws/",          # WebSocket 独立认证（直连后端）
    "/api/ws/",      # WebSocket 独立认证（通过Nginx代理）
)


async def global_auth_middleware(request: Request, call_next):
    """全局认证中间件：白名单放行，其余需要有效 Token（Cookie/Header），并设置用户级 API 配置"""
    path = request.url.path

    # Legacy localhost integration tests exercise routes without auth headers.
    # Keep this impossible to trigger unless the server process explicitly opts in.
    from core.auth import is_test_auth_bypass_enabled, get_test_user
    if is_test_auth_bypass_enabled():
        request.state.current_user = get_test_user()
        return await call_next(request)

    # Authentication must not hide the router's 404/405 contract for unknown
    # paths or unsupported methods.
    if request.method != "OPTIONS":
        route_status, allowed_methods = _declared_route_resolution(request)
        if route_status == "method_not_allowed":
            return JSONResponse(
                status_code=405,
                content={"detail": "Method Not Allowed"},
                headers={"Allow": ", ".join(sorted(allowed_methods))},
            )
        if route_status == "not_found":
            return await call_next(request)

    # 白名单精确匹配
    if path in AUTH_WHITELIST:
        return await call_next(request)

    # 白名单前缀匹配
    for prefix in AUTH_WHITELIST_PREFIXES:
        if path.startswith(prefix):
            return await call_next(request)

    # OPTIONS 预检请求放行（浏览器 CORS 预检不带 Cookie）
    if request.method == "OPTIONS":
        return await call_next(request)

    # 其余所有端点：强制认证
    from core.auth import AUTH_COOKIE_NAME, authenticate_token
    from core.hermes_client import current_user_api_config

    token = None
    # 1. Bearer Token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    # 2. HttpOnly Cookie
    if not token:
        token = request.cookies.get(AUTH_COOKIE_NAME)

    if not token:
        logger.warning("认证失败: 未提供 Token path=%s ip=%s", path,
                       request.client.host if request.client else "unknown")
        return JSONResponse(status_code=401, content={"detail": "请先登录"})

    try:
        _payload, user = authenticate_token(token)
        if not user:
            raise ValueError("用户不存在")
    except Exception as e:
        logger.warning("认证失败: Token无效 path=%s error=%s", path, str(e))
        return JSONResponse(status_code=401, content={"detail": "令牌无效或已过期，请重新登录"})

    request.state.current_user = user

    if _requires_admin(path, request.method) and user.role != "admin":
        return JSONResponse(status_code=403, content={"detail": "需要管理员权限"})

    # P0修复：项目权限检查 - 修复owner_user_id为空时的403错误
    protected_project_id = _extract_project_id(path)
    if protected_project_id:
        project = projects.get(protected_project_id)
        if project:
            owner_user_id = getattr(project, "owner_user_id", "")
            if not _can_user_access_project(user, project):
                logger.warning("项目权限检查失败: user=%s project=%s owner=%s", 
                             user.user_id, protected_project_id, owner_user_id)
                return JSONResponse(status_code=403, content={"detail": "无权访问此项目"})

    # ── 设置用户级 API 配置到 contextvar ──────────────────────────────────
    # 后续 hermes_client.chat() 调用会自动使用此用户的 Key
    from core.user_scope import current_user_id
    from core.llm_usage import current_llm_scope

    user_config = user_api_configs.get(user.user_id) or DEFAULT_API_CONFIG
    user_scope_token = current_user_id.set(str(user.user_id))
    token_ctx = current_user_api_config.set(user_config)
    llm_scope_token = current_llm_scope.set({
        "user_id": str(user.user_id),
        "project_id": protected_project_id or "",
    })
    try:
        response = await call_next(request)
        return response
    finally:
        current_user_api_config.reset(token_ctx)
        current_llm_scope.reset(llm_scope_token)
        current_user_id.reset(user_scope_token)


app.middleware("http")(global_auth_middleware)

# 限流中间件（必须在认证中间件之后注册，这样才能先执行并读取到 current_user）
# FastAPI 中间件是反向执行的：后注册的先执行
# 执行顺序：限流中间件 → 认证中间件 → 请求体大小限制 → 路由处理
app.middleware("http")(rate_limit_middleware)


def _extract_project_id(path: str) -> Optional[str]:
    """Extract project id from project-scoped HTTP routes."""
    match = re.match(r"^/projects/([^/]+)(?:/|$)", path)
    if match:
        return match.group(1)
    match = re.match(r"^/engineer/([^/]+)(?:/|$)", path)
    if match:
        return match.group(1)
    return None


def _can_user_access_project(user, project) -> bool:
    """Admins can inspect all projects; users can access only owned projects."""
    if getattr(user, "role", "") == "admin":
        return True
    owner_user_id = getattr(project, "owner_user_id", "")
    return bool(owner_user_id) and owner_user_id == getattr(user, "user_id", "")


def _requires_admin(path: str, method: str) -> bool:
    """Admin-only guard for global management surfaces."""
    method = method.upper()
    safe_read = method in {"GET", "HEAD", "OPTIONS"}
    if path == "/config/cache" and method == "DELETE":
        return True
    # 员工池、专家池和 Skill 池均按当前登录用户隔离，用户可管理自己的资源。
    # /agents 会聚合所有项目，普通用户读取会泄露其他用户的项目信息，因此仍仅限管理员。
    if path.startswith(("/gitee", "/agents", "/team/", "/ccb/", "/debug/")):
        return True
    return False


# ─── 请求模型 ─────────────────────────────────────────────────────────────────


# ─── 工具函数 ─────────────────────────────────────────────────────────────────
def _get_project(project_id: str):
    if project_id not in projects:
        raise HTTPException(status_code=404, detail=f"项目 {project_id} 不存在")
    return projects[project_id]

def _get_hermes(project_id: str) -> HermesClient:
    return hermes_client

# PM 团队 & Supervisor 团队状态（延迟初始化，持久化恢复时创建）
_pm_teams: Dict[str, PMLeaderAgent] = {}
_supervisor_leaders: Dict[str, SupervisorLeaderAgent] = {}
_phase_managers: Dict[str, PhaseManager] = {}

_idea_landing_agents: Dict[str, IdeaLandingAgent] = {}


def _get_idea_landing(user_id: str = "") -> IdeaLandingAgent:
    """返回当前用户独立的 IdeaLanding，并在首次访问时恢复持久化状态。"""
    from core.user_scope import active_user_id, user_storage_key

    owner_id = active_user_id(user_id)
    key = user_storage_key(owner_id)
    existing = _idea_landing_agents.get(key)
    if existing is not None:
        return existing

    saved = load_idea_landing(owner_id)
    if owner_id and not saved:
        # 历史版本只有一份全局记录，无法证明普通用户的所有权。仅允许部署
        # 管理员在首次访问时接管，杜绝新注册用户抢占旧数据。
        try:
            from core.auth import get_user_by_id
            user = get_user_by_id(owner_id)
            if user and user.role == "admin":
                saved = migrate_legacy_idea_landing_to_user(owner_id)
        except Exception:
            logger.exception("Failed to migrate legacy IdeaLanding state")

    agent = IdeaLandingAgent(hermes_client=hermes_client)
    if saved:
        try:
            agent.from_persist(saved)
            logger.info("恢复用户想法落地记录 user=%s conversations=%d", owner_id, len(agent.conversations))
        except Exception:
            logger.exception("Ignore invalid persisted IdeaLanding state user=%s", owner_id)
    _idea_landing_agents[key] = agent
    return agent

async def _persist_idea_landing():
    """持久化当前用户的想法落地 Agent 完整状态。"""
    from core.user_scope import active_user_id

    loop = asyncio.get_running_loop()
    owner_id = active_user_id()
    agent = _get_idea_landing(owner_id)
    data = agent.to_persist()
    await loop.run_in_executor(None, lambda: save_idea_landing(data, owner_id))
version_manager = VersionManager()
