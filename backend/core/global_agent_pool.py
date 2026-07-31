"""
全局员工池 (Global Agent Pool)

设计思路：
- 员工池是全局共享的，独立于任何项目
- 员工分为两类：
    1. 核心角色员工：PM组长/成员、Supervisor组长/成员、HR、CCB（系统预置，可扩展）
    2. 执行角色员工：PG（程序员）、Sec（安全）、Perf（性能）、UXO（交互）等（用户自行添加）
- 创建项目时，从员工池选人分配到项目，而不是凭空生成 Agent
- 员工可以同时参与多个项目（状态为 busy 时仍可分配，由用户决定）
- 员工档案包含：角色、技能、工作风格、API 配置等
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from core.workspace import metis_data_path


# ─── 员工档案 ─────────────────────────────────────────────────────────────────

class EmployeeProfile(BaseModel):
    """全局员工档案"""
    employee_id: str = Field(default_factory=lambda: f"emp-{uuid.uuid4().hex[:8]}")
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    # 基本信息
    name: str                           # 员工名称（如：PM 组长 · 张伟）
    role: str                           # 角色（如：PM 组长、后端工程师）
    agent_type: str                     # 系统类型：pm / supervisor / hr / pg / ccb / sec / perf / uxo
    avatar: str = "🤖"
    department: str = ""                # 部门：pm_team / supervisor_team / execution / management

    # 能力描述
    role_description: str = ""          # 角色职责描述
    working_style: str = ""             # 工作风格
    communication_style: str = ""       # 沟通风格
    domains: List[str] = Field(default_factory=list)   # 擅长领域
    skills: List[str] = Field(default_factory=list)    # 技能列表（名称）
    skill_ids: List[str] = Field(default_factory=list) # 技能 ID 列表

    # 工作配置
    behavior_rules: List[str] = Field(default_factory=list)  # 行为规范
    output_format: str = ""             # 输出格式要求
    api_config: Dict[str, Any] = Field(default_factory=dict)  # 独立 API 配置

    # 状态
    status: str = "available"           # available / busy / offline
    current_projects: List[str] = Field(default_factory=list)  # 当前参与的项目 ID 列表

    # 统计
    total_projects: int = 0             # 参与过的项目总数
    avg_quality_score: float = 0.0      # 平均质检得分

    def to_dict(self) -> Dict:
        d = self.model_dump()
        d["is_busy"] = len(self.current_projects) > 0
        d["project_count"] = len(self.current_projects)
        return d


# ─── 预置员工模板 ─────────────────────────────────────────────────────────────

PRESET_EMPLOYEES = [
    # ── PM 团队 ──────────────────────────────────────────────────────────────
    {
        "name": "PM 组长",
        "role": "PM 组长",
        "agent_type": "pm",
        "avatar": "👔",
        "department": "pm_team",
        "role_description": (
            "项目 PM 团队组长，负责全局需求分析、项目规划、阶段拆解、资源协调。"
            "是整个系统唯一的全局大脑，持续感知项目状态，在必要时触发重规划。"
        ),
        "working_style": "严谨、全局视角、以终为始",
        "communication_style": "结构化、简洁直接",
        "domains": ["项目管理", "需求分析", "风险管理", "资源规划"],
        "skills": ["需求分析", "项目规划", "风险评估", "团队协调", "文档撰写"],
        "behavior_rules": [
            "先明确需求再规划，不在需求模糊时强行输出方案",
            "规划必须包含验收标准，不输出无法验证的计划",
            "不确定时直接说不确定，不用模糊答案充数",
            "持续感知项目状态，主动识别风险并上报",
        ],
        "output_format": "结构化 Markdown，包含目标、阶段、验收标准",
    },
    # PM 成员 01-09（共9个，加上组长共10人）
    *[
        {
            "name": f"PM 成员{i:02d}",
            "role": "PM 成员",
            "agent_type": "pm",
            "avatar": "📋",
            "department": "pm_team",
            "employee_id": f"pm-member-preset-{i:02d}",
            "role_description": (
                "PM 团队成员，负责阶段任务的细化规划，与用户追问确认，"
                "确定本阶段需要哪些专家实现哪些具体功能，输出可执行的阶段规划。"
            ),
            "working_style": "细致、追问到位、以验收标准为导向",
            "communication_style": "简洁直接，不超过 400 字",
            "domains": ["项目管理", "需求分析", "阶段规划"],
            "skills": ["需求分析", "阶段规划", "任务分解", "专家需求输出", "用户确认"],
            "behavior_rules": [
                "不明确的需求直接追问，不猜测",
                "专家需求要具体（前端专家/后端专家/数据库专家等）",
                "每个任务必须有验收标准",
                "用户确认后在回复末尾加上【阶段规划已确认】",
            ],
            "output_format": "阶段规划（专家需求清单 + 任务描述 + 验收标准）",
        }
        for i in range(1, 10)
    ],

    # ── Supervisor 团队 ───────────────────────────────────────────────────────
    {
        "name": "Supervisor 组长",
        "role": "Supervisor 组长",
        "agent_type": "supervisor",
        "avatar": "🎯",
        "department": "supervisor_team",
        "role_description": (
            "总监督组长，负责阶段审查、质检触发、问题上报、进度监控。"
            "是 PM 在每个阶段的「现场代理」，守关每个阶段的交付质量。"
        ),
        "working_style": "严格、客观、以质量为底线",
        "communication_style": "直接、有据可查",
        "domains": ["质量管理", "进度监控", "风险控制"],
        "skills": ["质检触发", "问题追踪", "进度监控", "风险上报", "阶段审查"],
        "behavior_rules": [
            "质检不通过绝不放行，不降低标准",
            "问题必须有明确的责任人和修复期限",
            "不通过时直接上报 PM，不自行消化",
        ],
        "output_format": "审查报告：通过/不通过 + 问题列表 + 修复建议",
    },
    # Supervisor 成员 01-09（共9个，加上组长共10人）
    *[
        {
            "name": f"Supervisor 成员{i:02d}",
            "role": "Supervisor 成员",
            "agent_type": "supervisor",
            "avatar": "🔍",
            "department": "supervisor_team",
            "employee_id": f"sup-member-preset-{i:02d}",
            "role_description": (
                "Supervisor 团队成员，负责动态监督某阶段的代码质量。"
                "检查语法/逻辑问题，检查是否完成阶段任务，有问题直接反馈给对应专家（不生成建议）。"
            ),
            "working_style": "严格、客观、动态追踪，不放过任何问题",
            "communication_style": "问题导向、精确，只报告具体问题",
            "domains": ["质量保证", "代码审查", "阶段监督"],
            "skills": ["代码审查", "质量检查", "任务验收", "问题追踪", "阶段监督"],
            "behavior_rules": [
                "只报告具体问题，不生成修改建议",
                "每个问题必须指明：文件路径、问题描述、严重程度",
                "有问题自动反馈给对应专家，不自行消化",
                "全部问题修复后输出「质检通过，可以进入下一阶段」",
            ],
            "output_format": "JSON 格式问题列表：{passed, issues:[{file_path, message, severity}]}",
        }
        for i in range(1, 10)
    ],

    # ── HR ────────────────────────────────────────────────────────────────────
    {
        "name": "HR 负责人",
        "role": "HR 负责人",
        "agent_type": "hr",
        "avatar": "👥",
        "department": "management",
        "role_description": (
            "人力资源负责人，负责从员工池/专家池为项目阶段匹配合适人员，"
            "管理人员分配、技能授权、团队组建。"
        ),
        "working_style": "匹配导向、效率优先",
        "communication_style": "清晰、可执行",
        "domains": ["人员匹配", "团队组建", "技能管理"],
        "skills": ["人员匹配", "技能授权", "团队组建", "资源协调"],
        "behavior_rules": [
            "人员匹配必须基于技能和领域，不随机分配",
            "分配前确认人员状态（是否可用）",
            "分配结果必须经用户确认后才生效",
        ],
        "output_format": "分配方案：任务 → 匹配人员 → 备选人员 → 分配理由",
    },

    # ── CCB ───────────────────────────────────────────────────────────────────
    {
        "name": "CCB 审批员",
        "role": "CCB 审批员",
        "agent_type": "ccb",
        "avatar": "⚖️",
        "department": "management",
        "role_description": (
            "变更控制委员会成员，负责评估变更请求的风险和影响，"
            "决定是否批准变更、需要哪些补偿措施。"
        ),
        "working_style": "风险导向、谨慎",
        "communication_style": "正式、有据可查",
        "domains": ["变更管理", "风险评估", "合规审查"],
        "skills": ["变更评估", "风险分析", "影响评估", "审批决策"],
        "behavior_rules": [
            "高风险变更必须要求补偿措施",
            "不在信息不足时强行批准",
            "批准/拒绝必须给出明确理由",
        ],
        "output_format": "审批报告：变更描述 + 风险等级 + 决定 + 条件/补偿措施",
    },

    # 执行层专家由 HR 从专家池动态匹配，员工池不预置执行层成员
]


# ─── 全局员工池 ───────────────────────────────────────────────────────────────

class GlobalAgentPool:
    """
    全局员工池

    职责：
    1. 管理所有预置和自定义员工档案
    2. 为项目提供人员匹配和分配
    3. 跟踪员工的项目参与情况
    4. 持久化员工数据
    """

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = (
            Path(data_dir)
            if data_dir is not None
            else metis_data_path(
                "pools",
                legacy=Path(__file__).resolve().parents[1] / "data",
            )
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._data_file = self.data_dir / "global_agent_pool.json"
        self._employees: Dict[str, EmployeeProfile] = {}
        self._load()

    # ── 持久化 ────────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if self._data_file.exists():
            try:
                raw = json.loads(self._data_file.read_text(encoding="utf-8"))
                for emp_id, data in raw.items():
                    self._employees[emp_id] = EmployeeProfile(**data)
            except Exception:
                pass
        # 如果员工池为空，初始化预置员工
        if not self._employees:
            self._init_presets()
        else:
            # 员工池已有数据时，补充缺失的预置成员（升级兼容）
            self._patch_missing_presets()

    def _patch_missing_presets(self) -> None:
        """补充缺失的预置成员（用于已有持久化数据的用户升级）"""
        changed = False
        for preset in PRESET_EMPLOYEES:
            # 用固定 employee_id 的预置成员（PM/Supervisor 成员）
            fixed_id = preset.get("employee_id")
            if fixed_id:
                if fixed_id not in self._employees:
                    emp = EmployeeProfile(**preset)
                    self._employees[emp.employee_id] = emp
                    changed = True
            else:
                # 没有固定 ID 的预置成员（组长等），按名称查重
                name = preset.get("name", "")
                exists = any(e.name == name for e in self._employees.values())
                if not exists:
                    emp = EmployeeProfile(**preset)
                    self._employees[emp.employee_id] = emp
                    changed = True
        if changed:
            self.save()

    def save(self) -> None:
        """原子持久化：先写临时文件再重命名，防止断电/崩溃损坏数据"""
        import tempfile as _tmp

        data = {emp_id: emp.model_dump() for emp_id, emp in self._employees.items()}
        tmp_fd, tmp_path = _tmp.mkstemp(
            suffix=".json", prefix=".agent_pool_", dir=str(self._data_file.parent)
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, str(self._data_file))
        except Exception:
            os.unlink(tmp_path, missing_ok=True)
            raise

    def _init_presets(self) -> None:
        """初始化预置员工（首次启动时调用）"""
        for preset in PRESET_EMPLOYEES:
            emp = EmployeeProfile(**preset)
            self._employees[emp.employee_id] = emp
        self.save()

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def create_employee(self, profile: EmployeeProfile) -> EmployeeProfile:
        self._employees[profile.employee_id] = profile
        self.save()
        return profile

    def get_employee(self, employee_id: str) -> Optional[EmployeeProfile]:
        return self._employees.get(employee_id)

    def update_employee(self, employee_id: str, updates: Dict) -> Optional[EmployeeProfile]:
        emp = self._employees.get(employee_id)
        if not emp:
            return None
        for k, v in updates.items():
            if hasattr(emp, k) and v is not None:
                setattr(emp, k, v)
        emp.updated_at = time.time()
        self.save()
        return emp

    def delete_employee(self, employee_id: str) -> bool:
        if employee_id not in self._employees:
            return False
        del self._employees[employee_id]
        self.save()
        return True

    def list_employees(
        self,
        agent_type: Optional[str] = None,
        department: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[EmployeeProfile]:
        result = list(self._employees.values())
        if agent_type:
            result = [e for e in result if e.agent_type == agent_type]
        if department:
            result = [e for e in result if e.department == department]
        if status:
            result = [e for e in result if e.status == status]
        return result

    # ── 项目分配 ──────────────────────────────────────────────────────────────

    def assign_to_project(self, employee_id: str, project_id: str) -> bool:
        """将员工分配到项目"""
        emp = self._employees.get(employee_id)
        if not emp:
            return False
        if project_id not in emp.current_projects:
            emp.current_projects.append(project_id)
            emp.total_projects += 1
            emp.updated_at = time.time()
        self.save()
        return True

    def release_from_project(self, employee_id: str, project_id: str) -> bool:
        """从项目中释放员工"""
        emp = self._employees.get(employee_id)
        if not emp:
            return False
        if project_id in emp.current_projects:
            emp.current_projects.remove(project_id)
            emp.updated_at = time.time()
        self.save()
        return True

    def get_project_team(self, project_id: str) -> List[EmployeeProfile]:
        """获取项目当前团队成员"""
        return [e for e in self._employees.values() if project_id in e.current_projects]

    # ── 匹配 ──────────────────────────────────────────────────────────────────

    def match_for_role(
        self,
        agent_type: str,
        required_skills: Optional[List[str]] = None,
        required_domains: Optional[List[str]] = None,
        top_k: int = 3,
        exclude_busy: bool = False,
    ) -> List[Dict]:
        """
        为指定角色类型匹配员工。

        打分规则：
        - 技能匹配：每匹配一个 +2 分
        - 领域匹配：每匹配一个 +1 分
        - 空闲状态：+1 分
        """
        candidates = [e for e in self._employees.values() if e.agent_type == agent_type]
        if exclude_busy:
            candidates = [e for e in candidates if len(e.current_projects) == 0]

        scored = []
        for emp in candidates:
            score = 0
            emp_skills_lower = [s.lower() for s in emp.skills]
            emp_domains_lower = [d.lower() for d in emp.domains]

            for skill in (required_skills or []):
                if skill.lower() in emp_skills_lower:
                    score += 2
            for domain in (required_domains or []):
                if domain.lower() in emp_domains_lower:
                    score += 1
            if len(emp.current_projects) == 0:
                score += 1

            scored.append({
                "employee_id": emp.employee_id,
                "name": emp.name,
                "role": emp.role,
                "agent_type": emp.agent_type,
                "avatar": emp.avatar,
                "department": emp.department,
                "skills": emp.skills,
                "domains": emp.domains,
                "status": emp.status,
                "current_projects": emp.current_projects,
                "is_busy": len(emp.current_projects) > 0,
                "match_score": score,
            })

        scored.sort(key=lambda x: x["match_score"], reverse=True)
        return scored[:top_k]

    def get_default_project_team(self) -> Dict[str, List[EmployeeProfile]]:
        """
        获取创建项目时的默认团队建议（每个角色类型取最合适的一个）。
        返回按部门分组的员工列表。
        """
        result: Dict[str, List[EmployeeProfile]] = {
            "pm_team": [],
            "supervisor_team": [],
            "management": [],
            "execution": [],
        }
        for emp in self._employees.values():
            dept = emp.department or "execution"
            if dept in result:
                result[dept].append(emp)
        return result

    # ── 统计 ──────────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict:
        total = len(self._employees)
        by_type: Dict[str, int] = {}
        by_dept: Dict[str, int] = {}
        busy_count = 0
        for emp in self._employees.values():
            by_type[emp.agent_type] = by_type.get(emp.agent_type, 0) + 1
            by_dept[emp.department] = by_dept.get(emp.department, 0) + 1
            if emp.current_projects:
                busy_count += 1
        return {
            "total": total,
            "available": total - busy_count,
            "busy": busy_count,
            "by_type": by_type,
            "by_department": by_dept,
        }


# ─── 全局单例 ─────────────────────────────────────────────────────────────────

_global_pools: Dict[str, GlobalAgentPool] = {}


def get_global_agent_pool(user_id: str = "") -> GlobalAgentPool:
    from core.user_scope import active_user_id, user_storage_key

    owner_id = active_user_id(user_id)
    key = user_storage_key(owner_id)
    pool = _global_pools.get(key)
    if pool is None:
        base = metis_data_path(
            "pools",
            legacy=Path(__file__).resolve().parents[1] / "data",
        )
        data_dir = base if not owner_id else base / "users" / key / "employees"
        pool = GlobalAgentPool(data_dir=str(data_dir))
        _global_pools[key] = pool
    return pool
