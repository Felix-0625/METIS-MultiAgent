"""Project context - each project gets an independent Agent team"""

import time
import logging
import copy
from pathlib import Path
from typing import Optional, List, Dict

from core.workspace import WORKSPACE_ROOT, _safe_name, _project_workspace
from agents.base.memory import HybridMemory
from core.state_schema import PROJECT_SCHEMA_VERSION

logger = logging.getLogger("project_context")


class ProjectContext:
    """每个项目的独立 Agent 上下文，与其他项目完全隔离"""

    def __init__(self, project_id: str, name: str, description: str,
                 created_at: Optional[float] = None,
                 status: str = "planning",
                 subprojects: Optional[List] = None,
                 agents: Optional[Dict] = None,
                 owner_user_id: str = "",
                 schema_version: int = PROJECT_SCHEMA_VERSION,
                 record_version: int = 1,
                 updated_at: Optional[float] = None,
                 record_scope: str = "production",
                 hermes_client=None, global_sm_agent=None):
        self.project_id = project_id
        self.name = name
        self.description = description
        self.owner_user_id = owner_user_id
        self.status = status
        self.created_at = created_at or time.time()
        self.updated_at = updated_at or self.created_at
        self.schema_version = schema_version
        self.record_version = max(1, int(record_version or 1))
        self.record_scope = record_scope or "production"
        self.subprojects: List[Dict] = subprojects or []
        self.skill_manager = global_sm_agent

        # 项目工作区：workspace/项目名_proj-xxxx/
        self.workspace: Path = _project_workspace(name, project_id)
        self.workspace.mkdir(parents=True, exist_ok=True)
        # 子目录结构
        for sub in ("docs", "src", "tests", "output", "uploads"):
            (self.workspace / sub).mkdir(exist_ok=True)
        # 写入项目说明文件（首次创建时）
        readme = self.workspace / "README.md"
        if not readme.exists():
            readme.write_text(
                f"# {name}\n\n{description}\n\n"
                f"- 项目 ID: {project_id}\n"
                f"- 创建时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
                encoding="utf-8"
            )

        from agents.pm_agent import PMAgent
        from agents.hr_agent import HRAgent
        from agents.pg_agent import PGAgent
        from agents.supervisor_agent import SupervisorAgent
        from agents.ccb_agent import CCBAgent

        memory = HybridMemory(f"memory/{project_id}")
        self.pm = PMAgent(hermes_client=hermes_client, memory_store=memory)
        self.hr = HRAgent(hermes_client=hermes_client, memory_store=memory, skill_manager=global_sm_agent)
        # PGAgent must share the canonical project root so project-wide write
        # fencing covers its staging, commit, rollback and direct-write paths.
        self.pg = PGAgent(
            hermes_client=hermes_client,
            workspace=str(self.workspace),
            workspace_is_project_root=True,
        )
        self.supervisor = SupervisorAgent(hermes_client=hermes_client, memory_store=memory, project_id=project_id)
        self.ccb = CCBAgent(hermes_client=hermes_client, memory_store=memory)

        # 项目内执行 Agent（由 HR 动态创建）
        self.agents: Dict[str, Dict] = agents or {}
        # 质检结果存储：{subproject_id: {qa: {...}, perf: {...}, sec: {...}, uxo: {...}}}
        self.qc_results: Dict[str, Dict] = {}
        # Supervisor QA runs are the durable transition/evidence authority.
        # qc_results remains only the latest reviewer read-model.
        self.supervisor_quality_runs: Dict[str, Dict] = {}
        self.signoff_receipt: Optional[Dict] = None

    def to_dict(self) -> Dict:
        merged_subprojects = copy.deepcopy(self.subprojects)
        phase_manager = None
        try:
            from core.app_state import _phase_managers
            phase_manager = _phase_managers.get(self.project_id)
            if phase_manager and phase_manager.phases:
                self._merge_phase_subprojects(merged_subprojects, phase_manager.phases)
                self._sync_agent_subproject_status(merged_subprojects)
                self._derive_phase_statuses(merged_subprojects, phase_manager.phases)
            else:
                self._sync_agent_subproject_status(merged_subprojects)
        except Exception:
            self._sync_agent_subproject_status(merged_subprojects)

        return {
            "id": self.project_id,
            "name": self.name,
            "description": self.description,
            "status": self._derive_project_status(
                merged_subprojects,
                phase_manager.phases if phase_manager else None,
            ),
            "owner_user_id": self.owner_user_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "schema_version": self.schema_version,
            "record_version": self.record_version,
            "record_scope": self.record_scope,
            "subprojects_count": len(merged_subprojects),
            "agents_count": len(self.agents),
            "subprojects": merged_subprojects,
            "agents": self.agents,
            "core_agents": self._core_agents_info(),
            "workspace": str(self.workspace.resolve()),
            "workspace_rel": str(self.workspace),
        }

    def _merge_phase_subprojects(self, subprojects: List[Dict], phases: List[Dict]) -> None:
        existing_ids = {sp.get("id") for sp in subprojects}
        for phase in phases:
            phase_id = phase.get("phase_id")
            # Keep a local phase row in the serialized rollup even when the
            # execution list only contains agent-owned subprojects.  This row
            # is the acceptance gate used by _derive_project_status.
            if phase_id and phase_id not in existing_ids:
                subprojects.append({
                    **copy.deepcopy(phase),
                    "id": phase_id,
                    "phase_id": phase_id,
                    "agent_id": "",
                })
                existing_ids.add(phase_id)
            for sp_id in phase.get("subprojects", []):
                if sp_id in existing_ids:
                    continue
                matching_agent = next(
                    (ainfo for ainfo in self.agents.values() if ainfo.get("subproject_id") == sp_id),
                    None,
                )
                subprojects.append({
                    "id": sp_id,
                    "name": matching_agent.get("subproject_name", sp_id) if matching_agent else sp_id,
                    "description": matching_agent.get("role", "") if matching_agent else "",
                    "phase_id": phase.get("phase_id", ""),
                    "agent_id": matching_agent.get("id", "") if matching_agent else "",
                    "status": matching_agent.get("status", "pending") if matching_agent else "pending",
                    "progress": matching_agent.get("progress", 0) if matching_agent else 0,
                })
                existing_ids.add(sp_id)

    def _sync_agent_subproject_status(self, subprojects: List[Dict]) -> None:
        for sp in subprojects:
            agent_id = sp.get("agent_id", "")
            if agent_id and agent_id in self.agents:
                agent = self.agents[agent_id]
                sp["status"] = agent.get("status", sp.get("status", "pending"))
                sp["progress"] = agent.get("progress", sp.get("progress", 0))

    def _derive_phase_statuses(self, subprojects: List[Dict], phases: List[Dict]) -> None:
        phase_entries = {
            sp.get("phase_id") or sp.get("id"): sp
            for sp in subprojects
            if (sp.get("phase_id") or sp.get("id")) and not sp.get("agent_id")
        }
        executable = [sp for sp in subprojects if sp.get("agent_id")]

        for phase in phases:
            phase_id = phase.get("phase_id")
            if not phase_id:
                continue
            child_ids = set(phase.get("subprojects") or [])
            children = [
                sp for sp in executable
                if sp.get("phase_id") == phase_id or sp.get("id") in child_ids
            ]
            if not children and len(phases) == 1:
                children = executable

            if children:
                statuses = [sp.get("status", "pending") for sp in children]
                progresses = [int(sp.get("progress") or 0) for sp in children]
                canonical_status = str(phase.get("status") or "pending").strip().lower()
                if phase.get("user_confirmed"):
                    status = "completed"
                elif any(status in {"failed", "fix_limit_reached"} for status in statuses):
                    status = "failed"
                elif all(status == "completed" for status in statuses):
                    # Agent completion only closes execution.  QA and explicit
                    # user acceptance are separate gates and must not be
                    # collapsed into a false phase/project completion.
                    if phase.get("review_passed"):
                        status = "reviewing"
                    elif canonical_status in {
                        "reviewing", "qa_pending", "needs_rework", "qa_blocked",
                        "failed", "error",
                    }:
                        status = canonical_status
                    else:
                        status = "qa_pending"
                elif any(status in {"working", "in_progress", "running", "re_checking", "fixing"} for status in statuses):
                    status = "in_progress"
                else:
                    status = "pending"
                progress = int(sum(progresses) / len(progresses)) if progresses else 0
            else:
                status = phase.get("status", "pending")
                progress = int(phase.get("progress") or (100 if status == "completed" else 0))

            entry = phase_entries.get(phase_id)
            if entry:
                entry["status"] = status
                entry["progress"] = progress
                entry["subprojects"] = [sp.get("id") for sp in children if sp.get("id")]

    def _derive_project_status(
        self,
        subprojects: List[Dict],
        canonical_phases: Optional[List[Dict]] = None,
    ) -> str:
        if not self.agents:
            return self.status
        statuses = [agent.get("status", "pending") for agent in self.agents.values()]
        if any(status in {"failed", "fix_limit_reached"} for status in statuses):
            return "failed"

        # PhaseManager is authoritative for acceptance.  An executable
        # subproject may legitimately have the same id as its phase, so the
        # serialized subproject list alone cannot reliably identify phase
        # rows.  Only explicit user acceptance completes a managed project.
        if canonical_phases:
            unconfirmed = [
                phase for phase in canonical_phases
                if not phase.get("user_confirmed")
            ]
            if any(
                str(phase.get("status") or "").lower()
                in {"failed", "fix_limit_reached"}
                for phase in unconfirmed
            ):
                return "failed"
            return "running" if unconfirmed else "completed"

        # Canonical phase rows have no agent_id. They represent the complete PM
        # plan, including phases that have not spawned an execution agent yet.
        # Do not mark a project completed merely because the currently-created
        # agents finished while later planned phases are still pending.
        phases = [sp for sp in subprojects if not sp.get("agent_id")]
        if phases:
            phase_statuses = [phase.get("status", "pending") for phase in phases]
            if any(status in {"failed", "fix_limit_reached"} for status in phase_statuses):
                return "failed"
            if all(status == "completed" for status in phase_statuses):
                return "completed"
            if any(status in {"working", "in_progress", "running", "re_checking", "fixing"}
                   for status in phase_statuses) or self.agents:
                return "running"

        if statuses and all(status == "completed" for status in statuses):
            return "completed"
        if any(status in {"working", "in_progress", "running", "queued", "re_checking", "fixing"} for status in statuses):
            return "running"
        executable = [sp for sp in subprojects if sp.get("agent_id")]
        if executable and all(sp.get("status") == "completed" for sp in executable):
            return "completed"
        return self.status

    def _legacy_to_dict(self) -> Dict:
        # 合并 PhaseManager 中的子项目数据，确保前端 ProgressBoard 能看到完整进度
        merged_subprojects = list(self.subprojects)
        try:
            from core.app_state import _phase_managers
            pm = _phase_managers.get(self.project_id)
            if pm and pm.phases:
                # 收集 PhaseManager 阶段中的 subproject 引用
                phase_sp_ids = set()
                for phase in pm.phases:
                    for sp_id in phase.get("subprojects", []):
                        phase_sp_ids.add(sp_id)
                # 对尚未在 ctx.subprojects 中的子项目，从 ctx.agents 中推导
                existing_ids = {sp["id"] for sp in merged_subprojects}
                for sp_id in phase_sp_ids:
                    if sp_id not in existing_ids:
                        # 从 agents 中找到负责该 subproject 的 agent
                        matching_agent = None
                        for aid, ainfo in self.agents.items():
                            if ainfo.get("subproject_id") == sp_id:
                                matching_agent = ainfo
                                break
                        sp_entry = {
                            "id": sp_id,
                            "name": matching_agent.get("subproject_name", sp_id) if matching_agent else sp_id,
                            "description": matching_agent.get("role", "") if matching_agent else "",
                            "agent_id": matching_agent.get("id", "") if matching_agent else "",
                            "status": matching_agent.get("status", "pending") if matching_agent else "pending",
                            "progress": matching_agent.get("progress", 0) if matching_agent else 0,
                        }
                        merged_subprojects.append(sp_entry)
                # 从 ctx.agents 同步状态和进度到 merged_subprojects
                for sp in merged_subprojects:
                    agent_id = sp.get("agent_id", "")
                    if agent_id and agent_id in self.agents:
                        ainfo = self.agents[agent_id]
                        sp["status"] = sp.get("status") or ainfo.get("status", "pending")
                        sp["progress"] = ainfo.get("progress", sp.get("progress", 0))
        except Exception:
            pass

        return {
            "id": self.project_id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "owner_user_id": self.owner_user_id,
            "created_at": self.created_at,
            "subprojects_count": len(merged_subprojects),
            "agents_count": len(self.agents),
            "subprojects": merged_subprojects,
            "agents": self.agents,
            "core_agents": self._core_agents_info(),
            "workspace": str(self.workspace.resolve()),
            "workspace_rel": str(self.workspace),
        }

    def _core_agents_info(self) -> List[Dict]:
        """返回本项目核心 Agent 的基本信息（含 Skill 列表）"""
        from agents.base.hermes_agent import AgentType
        skill_manager = self.skill_manager

        def _skill_names(agent_type: str) -> List[str]:
            skills = skill_manager.get_skills_for_agent_type(agent_type)
            return [s["name"] for s in skills]

        return [
            {"id": self.pm.agent_id, "role": "PM Agent", "type": "pm", "status": self.pm.state.value, "is_core": True, "skill_names": _skill_names("pm"), "skill_ids": skill_manager.get_skill_ids_for_agent_type("pm")},
            {"id": self.hr.agent_id, "role": "HR Agent", "type": "hr", "status": self.hr.state.value, "is_core": True, "skill_names": _skill_names("hr"), "skill_ids": skill_manager.get_skill_ids_for_agent_type("hr")},
            {"id": self.supervisor.agent_id, "role": "Supervisor Agent", "type": "supervisor", "status": self.supervisor.state.value, "is_core": True, "skill_names": _skill_names("supervisor"), "skill_ids": skill_manager.get_skill_ids_for_agent_type("supervisor")},
            {"id": self.pg.agent_id, "role": "PG Agent", "type": "pg", "status": self.pg.state.value, "is_core": True, "skill_names": _skill_names("pg"), "skill_ids": skill_manager.get_skill_ids_for_agent_type("pg")},
            {"id": self.ccb.agent_id, "role": "CCB Agent", "type": "ccb", "status": self.ccb.state.value, "is_core": True, "skill_names": _skill_names("ccb"), "skill_ids": skill_manager.get_skill_ids_for_agent_type("ccb")},
        ]

    def to_persist(self) -> Dict:
        """序列化为可持久化的字典（不含 Agent 实例）"""
        return {
            "project_id": self.project_id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "owner_user_id": self.owner_user_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "schema_version": self.schema_version,
            "record_version": self.record_version,
            "record_scope": self.record_scope,
            "subprojects": self.subprojects,
            "agents": self.agents,
            "qc_results": self.qc_results,
            "supervisor_quality_runs": self.supervisor_quality_runs,
            "signoff_receipt": copy.deepcopy(self.signoff_receipt),
        }
