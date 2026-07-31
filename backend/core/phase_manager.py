"""
阶段管理器
负责项目阶段的生命周期管理：
- 阶段划分（基于PM方案）
- 阶段启动（HR按阶段创建Agent）
- 阶段审查（触发监督Agent审查）
- 阶段推进（检查通过后进入下一阶段）
- 文件追责（记录每个文件由哪个Agent在哪个阶段产生）
"""

import time
import uuid
from typing import Dict, List, Optional, Any
from pathlib import Path
import json

from core.project_contract import (
    PHASE_PLAN_VERSION,
    artifact_metadata,
    deterministic_phase_fallback,
    phase_task_contract,
    validate_phase_plan_layers,
)
from core.role_mapping import canonical_expert_type


_EXECUTION_ROLE_TYPES = {
    "frontend", "backend", "database", "qa", "devops", "security",
    "architecture", "data", "fullstack_engineer",
}


class UnsupportedExecutionRoleError(ValueError):
    """Declared phase executors must map to one runnable expert type."""

    def __init__(self, roles: List[str]):
        self.roles = tuple(dict.fromkeys(roles))
        super().__init__(
            "unsupported or ambiguous execution roles: " + ", ".join(self.roles)
        )


def _role_values(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _declared_execution_roles(phase: Dict[str, Any]) -> List[str]:
    declared: List[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in declared:
            declared.append(text)

    for role in _role_values(phase.get("roles_needed") or phase.get("roles")):
        add(role)
    for task in phase.get("task_contract") or phase.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        add(task.get("required_role"))
        for role in _role_values(task.get("roles_needed") or task.get("roles")):
            add(role)
    return declared


def _execution_roles_for_phase(phase: Dict[str, Any], contract: Dict[str, Any]) -> List[str]:
    roles: List[str] = []

    def add(role_type: Optional[str]) -> None:
        if role_type in _EXECUTION_ROLE_TYPES and role_type not in roles:
            roles.append(role_type)

    for role in _role_values(phase.get("roles_needed") or phase.get("roles")):
        add(canonical_expert_type(str(role)))
    has_explicit_engineering_roles = bool(roles)
    if has_explicit_engineering_roles:
        return roles
    phase_id = str(phase.get("phase_id") or "")
    for item in contract.get("required_files") or []:
        if isinstance(item, dict) and str(item.get("phase_id") or "") == phase_id:
            add(str(item.get("owner_type") or "").strip().lower())
    for task in phase.get("task_contract") or phase.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        text = json.dumps(task, ensure_ascii=False).lower()
        add(canonical_expert_type(text))
        if any(token in text for token in ("/api/", " endpoint", "jwt", "auth", "middleware")):
            add("backend")
        if any(token in text for token in ("dockerfile", ".env", "deployment", "root package.json", "readme")):
            add("devops")
    return roles or ["backend"]


class PhaseManager:
    """
    项目阶段管理器
    每个 ProjectContext 持有一个 PhaseManager 实例
    """

    def __init__(self, project_id: str, workspace: Path):
        self.project_id = project_id
        self.workspace = workspace
        self.phases: List[Dict] = []           # 阶段列表
        self.current_phase_index: int = -1     # 当前阶段索引（-1 表示未启动）
        self.phase_agents: Dict[str, List[str]] = {}   # phase_id -> [agent_id, ...]
        self.file_registry: Dict[str, Dict] = {}       # file_path -> {agent_id, phase_id, created_at, ...}
        self.project_contract: Dict[str, Any] = {}

    # ─── 阶段初始化 ───────────────────────────────────────────────────────────

    def init_phases_from_plan(self, plan: Dict) -> List[Dict]:
        """
        从 PM 方案中初始化阶段列表
        plan 结构来自 PMLeaderAgent.get_final_plan_for_hr()
        """
        raw_phases = plan.get("phases", [])
        if not raw_phases:
            # 没有阶段信息时，创建默认阶段
            raw_phases = [
                {
                    "phase_id": "phase-1",
                    "name": "开发阶段",
                    "description": "核心功能开发",
                    "duration": "待定",
                    "deliverables": [],
                    "roles_needed": ["开发工程师"],
                    "agent_count": 1,
                }
            ]

        unsupported_roles = [
            role
            for phase in raw_phases
            for role in _declared_execution_roles(phase)
            if canonical_expert_type(role) is None
        ]
        if unsupported_roles:
            raise UnsupportedExecutionRoleError(unsupported_roles)

        project_contract = plan.get("project_contract") or {}
        new_phases: List[Dict] = []
        for i, p in enumerate(raw_phases):
            raw_phase_id = p.get("phase_id")
            phase = {
                # Route parameters are strings. Normalize model-produced numeric
                # IDs at the persistence boundary so phase lookup remains stable.
                "phase_id": str(raw_phase_id if raw_phase_id is not None else f"phase-{i+1}"),
                "name": p.get("name", f"阶段 {i+1}"),
                "description": p.get("objective") or p.get("description", ""),
                "objective": p.get("objective") or p.get("description", ""),
                "work_items": list(p.get("work_items") or []),
                "technical_requirements": list(
                    p.get("technical_requirements")
                    or p.get("tech_stack")
                    or p.get("technology_stack")
                    or []
                ),
                "source_requirement_ids": list(
                    p.get("source_requirement_ids") or []
                ),
                "duration": p.get("duration", "待定"),
                "deliverables": p.get("deliverables", []),
                "roles_needed": p.get("roles_needed") or p.get("roles") or [],
                "tech_stack": p.get("tech_stack", []),
                "acceptance_criteria": list(p.get("acceptance_criteria") or []),
                "dependencies": list(p.get("dependencies") or []),
                "source_constraints": list(p.get("source_constraints") or []),
                "project_contract": project_contract,
                "task_contract": p.get("task_contract") or p.get("tasks") or [],
                "agent_count": p.get("agent_count", 1),
                "status": "pending",       # pending / active / reviewing / completed
                "started_at": None,
                "completed_at": None,
                "reviewed": False,
                "review_passed": False,
                "agents": [],              # 本阶段的 agent_id 列表
                "subprojects": [],         # 本阶段的子项目 id 列表
                "expert_requirements": [], # 阶段 PM 生成的结构化专家规划（需持久化，刷新后恢复）
                "plan_generated": False,
                "plan_reviews": [],        # v3.0: PlanReview 审阅链记录
                "review_chain_status": "pending",  # v3.0: pending / in_progress / resolved
                "order": i,
            }
            phase["roles_needed"] = (
                p.get("roles_needed") or p.get("roles") or []
            )
            phase["tech_stack"] = (
                p.get("technical_requirements")
                or p.get("tech_stack")
                or p.get("technology_stack")
                or phase["tech_stack"]
            )
            phase["task_contract"] = phase_task_contract(phase)
            # ``roles_needed`` is part of the immutable ProjectContract.  Keep
            # the user/model label byte-for-byte and derive executor types into
            # a separate field.  File ownership must never rewrite task roles.
            phase["execution_roles"] = _execution_roles_for_phase(
                phase, project_contract
            )
            if not phase["tech_stack"]:
                phase["tech_stack"] = list(dict.fromkeys(
                    str(tech)
                    for task in phase["task_contract"]
                    if isinstance(task, dict)
                    for tech in (task.get("technology_stack") or [])
                    if str(tech).strip()
                ))
            if project_contract.get("locked") and phase["task_contract"]:
                requirements = deterministic_phase_fallback(
                    phase, project_contract
                )
                validation = validate_phase_plan_layers(
                    phase, requirements, project_contract
                )
                if requirements and validation.valid:
                    phase.update({
                        "expert_requirements": requirements,
                        "phase_plan_version": PHASE_PLAN_VERSION,
                        "plan_status": "saved",
                        "plan_generated": True,
                        "plan_contract_validated": True,
                        "plan_generation_mode": "confirmed_contract",
                        "plan_generated_at": time.time(),
                        "plan_validation": validation.to_dict(),
                        "plan_artifact_metadata": artifact_metadata(
                            "phase_plan",
                            PHASE_PLAN_VERSION,
                            "confirmed_contract",
                            validation,
                        ),
                    })
            new_phases.append(phase)

        self.project_contract = project_contract
        self.phases = new_phases
        return self.phases

    def get_current_phase(self) -> Optional[Dict]:
        """获取当前活跃阶段"""
        if self.current_phase_index < 0 or self.current_phase_index >= len(self.phases):
            return None
        return self.phases[self.current_phase_index]

    def get_phase_by_id(self, phase_id: str) -> Optional[Dict]:
        # Tolerate legacy persisted projects that stored numeric phase IDs.
        wanted = str(phase_id)
        return next((p for p in self.phases if str(p.get("phase_id")) == wanted), None)

    # ─── 阶段推进 ─────────────────────────────────────────────────────────────

    def start_next_phase(self) -> Dict[str, Any]:
        """
        启动下一个阶段
        - 如果是第一次，启动第一个阶段
        - 否则检查当前阶段是否已通过审查
        """
        if not self.phases:
            return {"success": False, "message": "尚未初始化阶段，请先确认 PM 方案"}

        # 检查当前阶段是否已完成审查
        if self.current_phase_index >= 0:
            current = self.phases[self.current_phase_index]
            if not current.get("review_passed"):
                return {
                    "success": False,
                    "message": f"当前阶段「{current['name']}」尚未通过审查，请先完成审查",
                    "current_phase": current,
                }
            # 标记当前阶段完成
            current["status"] = "completed"
            current["completed_at"] = time.time()

        next_index = self.current_phase_index + 1
        if next_index >= len(self.phases):
            return {
                "success": False,
                "message": "所有阶段已完成",
                "all_completed": True,
            }

        # 启动下一阶段
        self.current_phase_index = next_index
        next_phase = self.phases[next_index]
        next_phase["status"] = "active"
        next_phase["started_at"] = time.time()

        return {
            "success": True,
            "phase": next_phase,
            "phase_index": next_index,
            "total_phases": len(self.phases),
            "message": f"阶段「{next_phase['name']}」已启动",
        }


    # ─── Agent 管理 ───────────────────────────────────────────────────────────

    def assign_agents_to_phase(self, phase_id: str, agent_ids: List[str]) -> None:
        """将 Agent 分配到指定阶段"""
        phase = self.get_phase_by_id(phase_id)
        if phase:
            phase["agents"] = agent_ids
        self.phase_agents[phase_id] = agent_ids

    def assign_subprojects_to_phase(self, phase_id: str, subproject_ids: List[str]) -> None:
        """将子项目分配到指定阶段"""
        phase = self.get_phase_by_id(phase_id)
        if phase:
            phase["subprojects"] = subproject_ids

    def get_current_phase_agents(self) -> List[str]:
        """获取当前阶段的 Agent 列表"""
        current = self.get_current_phase()
        if not current:
            return []
        return current.get("agents", [])

    # ─── 文件追责 ─────────────────────────────────────────────────────────────

    def register_file(
        self,
        file_path: str,
        agent_id: str,
        agent_role: str,
        phase_id: str,
        subproject_id: str = "",
        task_id: str = "",
    ) -> None:
        """注册文件产出记录（追责用）"""
        self.file_registry[file_path] = {
            "file_path": file_path,
            "agent_id": agent_id,
            "agent_role": agent_role,
            "phase_id": phase_id,
            "subproject_id": subproject_id,
            "task_id": task_id,
            "created_at": time.time(),
        }

    def get_file_owner(self, file_path: str) -> Optional[Dict]:
        """获取文件的负责 Agent 信息"""
        return self.file_registry.get(file_path)

    def get_files_by_phase(self, phase_id: str) -> List[Dict]:
        """获取某阶段产生的所有文件"""
        return [
            info for info in self.file_registry.values()
            if info.get("phase_id") == phase_id
        ]

    def get_files_by_agent(self, agent_id: str) -> List[Dict]:
        """获取某 Agent 产生的所有文件"""
        return [
            info for info in self.file_registry.values()
            if info.get("agent_id") == agent_id
        ]

    def scan_workspace_files(self, phase_id: str, agent_id: str, agent_role: str) -> List[str]:
        """
        扫描工作区文件，自动注册新文件
        返回新发现的文件列表
        """
        new_files = []
        src_dir = self.workspace / "src"
        if src_dir.exists():
            for f in src_dir.rglob("*"):
                if f.is_file():
                    rel_path = str(f.relative_to(self.workspace)).replace("\\", "/")
                    if rel_path not in self.file_registry:
                        self.register_file(rel_path, agent_id, agent_role, phase_id)
                        new_files.append(rel_path)
        return new_files

    # ─── 序列化 ───────────────────────────────────────────────────────────────

    def to_dict(self) -> Dict:
        return {
            "project_id": self.project_id,
            "phases": self.phases,
            "current_phase_index": self.current_phase_index,
            "phase_agents": self.phase_agents,
            "file_registry": self.file_registry,
            "project_contract": self.project_contract,
        }

    def from_dict(self, data: Dict) -> None:
        """从持久化数据恢复"""
        self.phases = data.get("phases", [])
        self.current_phase_index = data.get("current_phase_index", -1)
        self.phase_agents = data.get("phase_agents", {})
        self.file_registry = data.get("file_registry", {})
        self.project_contract = data.get("project_contract", {})

    def get_progress_summary(self) -> Dict:
        """获取阶段进度摘要"""
        total = len(self.phases)
        completed = sum(1 for p in self.phases if p.get("status") == "completed")
        current = self.get_current_phase()
        return {
            "total_phases": total,
            "completed_phases": completed,
            "current_phase": current,
            "current_phase_index": self.current_phase_index,
            "overall_progress": int(completed / total * 100) if total > 0 else 0,
            "phases": self.phases,
        }

    def get_phase(self, phase_id: str) -> Optional[Dict]:
        """get_phase 是 get_phase_by_id 的别名，供 main.py 调用"""
        return self.get_phase_by_id(phase_id)

    def update_phase_description(self, phase_id: str, description: str) -> bool:
        """修改阶段任务描述"""
        phase = self.get_phase_by_id(phase_id)
        if not phase:
            return False
        phase["description"] = description
        phase["updated_at"] = time.time()
        return True

    def force_start_phase(self, phase_id: str) -> Dict:
        """强制启动指定阶段（不检查前置条件，由调用方负责检查）"""
        phase = self.get_phase_by_id(phase_id)
        if not phase:
            return {"success": False, "message": f"阶段 {phase_id} 不存在"}
        if phase.get("status") == "completed":
            return {"success": False, "message": "该阶段已完成，无法重新启动"}
        if phase.get("status") == "active":
            return {"success": False, "message": "该阶段已在执行中，无需重复启动"}
        phase["status"] = "active"
        phase["started_at"] = time.time()
        # 更新当前阶段索引
        for i, p in enumerate(self.phases):
            if p["phase_id"] == phase_id:
                self.current_phase_index = i
                break
        return {"success": True, "phase": phase}

    def mark_phase_reviewed(self, phase_id: str, passed: bool, auto_confirm: bool = False) -> Dict[str, Any]:
        """
        标记阶段审查结果（质检通过/不通过）。
        auto_confirm=True 时质检通过后直接进入 completed，无需用户手动确认。
        auto_confirm=False（默认）时进入 reviewing，等待用户调用 confirm-complete。
        """
        phase = self.get_phase_by_id(phase_id)
        if not phase:
            return {"success": False, "message": f"阶段 {phase_id} 不存在"}
        phase["reviewed"] = True
        phase["review_passed"] = passed
        phase["reviewed_at"] = time.time()
        if passed:
            if auto_confirm:
                phase["status"] = "completed"
                phase["completed_at"] = time.time()
                phase["auto_confirmed"] = True
            else:
                phase["status"] = "reviewing"  # 质检通过，等待用户手动确认
        else:
            # 质检不通过，标记为 needs_rework 状态，便于后续返工流程识别
            phase["status"] = "needs_rework"
        return {"success": True, "phase_id": phase_id, "passed": passed, "status": phase.get("status")}

    def mark_phase_completed(self, phase_id: str) -> Dict[str, Any]:
        """
        用户手动确认阶段完成（必须由用户主动触发，不自动调用）。
        将阶段状态设为 completed，允许启动下一阶段。
        """
        phase = self.get_phase_by_id(phase_id)
        if not phase:
            return {"success": False, "message": f"阶段 {phase_id} 不存在"}
        phase["status"] = "completed"
        phase["user_confirmed"] = True
        phase["completed_at"] = time.time()
        return {"success": True, "phase_id": phase_id, "phase_name": phase.get("name", "")}
