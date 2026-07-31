"""
专家池 (Expert Pool)

设计理念：
- 专家池是独立于项目的 Agent 模板库，类似"人才简历库"
- 每个专家有两层记忆：
    1. 角色记忆（长期）：角色定位、能力、做事风格、历史表现 —— 跨项目保留
    2. 项目记忆（短期）：当前/历史项目的上下文 —— 按项目隔离
- 新项目启动时：从专家池匹配合适专家 → 创建实例 → 注入角色记忆 + 空项目记忆
- 专家可以被多个项目复用，但每个项目的记忆互相隔离

数据结构：
    ExpertProfile  —— 专家档案（角色记忆的载体）
    ExpertPool     —— 专家池管理器
    ExpertMatcher  —— 任务-专家匹配器
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from core.role_mapping import canonical_expert_type
from core.workspace import metis_data_path


# ─── 四大原则常量（所有 Agent 必须遵守）────────────────────────────────────────

AGENT_CORE_PRINCIPLES = """
【Agent 核心工作原则 — 必须严格遵守】

原则一 Plan Before Execute（先规划后执行）
- 收到任务后，先在内部完整规划，再输出结果
- 不边想边写，不自相矛盾
- 需求审核与执行输出严格分离

原则二 Agent-First（需求优先）
- 先确认需求，再动手执行
- 思考过程不混入输出
- 收到任务时先明确三点（不明确直接提问）：
  1. 执行什么具体任务
  2. 对象是谁
  3. 执行标准是什么

原则三 Test-Driven（验证驱动）
- 输出前必须模拟验证：代码可运行，答案可验证
- 不确定不输出
- 有错误不打补丁，直接用新的完整版本替换旧状态（Immutability）

原则四 Security-First（安全优先）
- 安全是底线不是选项
- 不确定就说不确定，不用模糊答案充数

【输出规则】
- 字数：用户有明确要求则遵守，没有则按需
- 场景匹配：内容符合使用场景
- 格式：用户指定则用指定格式（表格/Markdown/流程图），未指定则按内容选最优格式
- 输出结构分明，层次清晰，突出重点
""".strip()


# ─── 专家档案（角色记忆）─────────────────────────────────────────────────────

class ExpertSkill(BaseModel):
    """专家技能条目"""
    skill_id: str = ""
    name: str
    level: Literal["beginner", "intermediate", "expert"] = "intermediate"
    description: str = ""


class TrainingSession(BaseModel):
    """单次训练/反馈记录"""
    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    created_at: float = Field(default_factory=time.time)
    # 训练类型：feedback=用户反馈, correction=纠错, style_tune=风格调整, logic_tune=逻辑调整
    session_type: Literal["feedback", "correction", "style_tune", "logic_tune", "output_tune"] = "feedback"
    user_input: str = ""          # 用户给的原始输入/示例
    agent_output: str = ""        # agent 当时的输出（可选，用于对比）
    feedback: str = ""            # 用户反馈内容
    correction: str = ""          # 期望的正确输出/行为
    applied: bool = False         # 是否已应用到 system prompt
    impact_score: float = 0.0     # 该训练对 agent 的影响程度（0-1）


class WorkMode(BaseModel):
    """工作模式配置"""
    mode_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:6])
    name: str = "默认模式"
    description: str = ""
    # 思考方式：chain_of_thought=链式思考, step_by_step=逐步推理, direct=直接输出
    thinking_style: Literal["chain_of_thought", "step_by_step", "direct", "socratic"] = "chain_of_thought"
    # 执行风格：conservative=保守, balanced=均衡, aggressive=激进
    execution_style: Literal["conservative", "balanced", "aggressive"] = "balanced"
    # 输出详细度：brief=简洁, normal=正常, detailed=详细
    verbosity: Literal["brief", "normal", "detailed"] = "normal"
    # 是否启用 CoT 推理标签
    enable_cot: bool = True
    # 是否在输出前强制自检
    enable_self_check: bool = True
    # 自定义 prompt 片段（追加到 system prompt 末尾）
    custom_prompt_addon: str = ""
    # 是否激活
    active: bool = True


class ExpertProfile(BaseModel):
    """
    专家档案 —— 角色记忆的载体（长期，跨项目保留）

    类比：一份个人简历 + 工作风格说明书 + 个人训练记录
    """
    expert_id: str = Field(default_factory=lambda: f"exp-{uuid.uuid4().hex[:8]}")
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    # 基本信息
    name: str                           # 专家名称（如"高级后端工程师-张三"）
    role: str                           # 角色标签（如"后端工程师"）
    agent_type: str = "pg"              # 对应 AgentType（pm/hr/pg/qa/sec/perf/uxo/supervisor/ccb）
    avatar: str = ""                    # 头像 emoji 或 URL

    # 角色定位（注入 System Prompt 的核心内容）
    role_description: str = ""          # 角色定位：我是谁，我负责什么
    working_style: str = ""             # 做事风格：我怎么做事（严谨/快速/保守/激进等）
    communication_style: str = ""       # 沟通风格：我怎么表达（简洁/详细/结构化等）
    decision_style: str = ""            # 决策风格：我怎么做决定（数据驱动/经验驱动等）

    # ── 新增：工作模式 & 思考方式 ──────────────────────────────────────────────
    work_mode: WorkMode = Field(default_factory=WorkMode)
    # 思考框架（注入 system prompt 的思考方式描述）
    thinking_framework: str = ""        # 如："先分解问题，再逐步推理，最后验证结论"
    # 项目背景（由 PM 组长传达，所有 agent 都需要知道）
    project_background: str = ""        # 当前项目的核心背景信息

    # 能力
    skills: List[ExpertSkill] = Field(default_factory=list)
    skill_ids: List[str] = Field(default_factory=list)  # 关联 SMAgent 的 skill_id
    domains: List[str] = Field(default_factory=list)    # 擅长领域（如 ["Python", "FastAPI", "PostgreSQL"]）
    certifications: List[str] = Field(default_factory=list)  # 认证/资质

    # 行为规范（直接注入 System Prompt）
    behavior_rules: List[str] = Field(default_factory=list)
    # 拒绝策略（什么情况下返回 BLOCKED）
    rejection_policy: str = ""
    # 输出格式要求
    output_format: str = ""

    # ── 新增：个人长期记忆（训练记录）──────────────────────────────────────────
    # 训练会话历史（跨项目保留，是专家的"个人成长记录"）
    training_sessions: List[TrainingSession] = Field(default_factory=list)
    # 从训练中提炼的长期记忆摘要（LLM 压缩后的精华）
    long_term_memory: str = ""
    # 用户对该专家的个性化偏好设置（如"回复要简短"、"代码要加注释"）
    user_preferences: List[str] = Field(default_factory=list)

    # 历史表现（跨项目积累）
    total_tasks_completed: int = 0
    total_tasks_failed: int = 0
    avg_quality_score: float = 0.0      # 历史平均质检得分（0-100）
    project_history: List[Dict] = Field(default_factory=list)  # 最近 10 个项目摘要

    # 状态
    status: Literal["available", "busy", "retired"] = "available"
    current_project_id: str = ""        # 当前所在项目（busy 时填写）

    # 自定义 API 配置（可覆盖全局默认）
    api_config: Dict[str, Any] = Field(default_factory=dict)

    def to_system_prompt_block(self) -> str:
        """
        生成注入 System Prompt 的角色记忆块。
        包含：四大原则 + 角色定位 + 工作模式 + 思考框架 + 技能 + 行为规范 + 长期记忆
        """
        lines = [
            AGENT_CORE_PRINCIPLES,
            "",
            f"【专家档案 — 角色记忆】",
            f"姓名：{self.name}  |  角色：{self.role}",
        ]

        if self.role_description:
            lines.append(f"\n【角色定位】\n{self.role_description}")

        if self.working_style:
            lines.append(f"\n【做事风格】\n{self.working_style}")

        if self.communication_style:
            lines.append(f"\n【沟通风格】\n{self.communication_style}")

        if self.decision_style:
            lines.append(f"\n【决策风格】\n{self.decision_style}")

        # 工作模式
        wm = self.work_mode
        thinking_labels = {
            "chain_of_thought": "链式思考（CoT）",
            "step_by_step": "逐步推理",
            "direct": "直接输出",
            "socratic": "苏格拉底式追问",
        }
        exec_labels = {
            "conservative": "保守稳健",
            "balanced": "均衡",
            "aggressive": "激进快速",
        }
        lines.append(
            f"\n【工作模式】{wm.name}"
            f"  思考方式：{thinking_labels.get(wm.thinking_style, wm.thinking_style)}"
            f"  执行风格：{exec_labels.get(wm.execution_style, wm.execution_style)}"
            f"  输出详细度：{wm.verbosity}"
        )
        if wm.enable_cot:
            lines.append("  → 启用 CoT：输出前用 <thinking> 标签完成内部推理，不将推理过程混入最终输出")
        if wm.enable_self_check:
            lines.append("  → 启用自检：输出前必须自我验证结果的正确性和完整性")
        if wm.custom_prompt_addon:
            lines.append(f"\n【自定义工作指令】\n{wm.custom_prompt_addon}")

        # 思考框架
        if self.thinking_framework:
            lines.append(f"\n【思考框架】\n{self.thinking_framework}")

        # 项目背景
        if self.project_background:
            lines.append(f"\n【项目背景（PM 传达）】\n{self.project_background}")

        if self.domains:
            lines.append(f"\n【擅长领域】{', '.join(self.domains)}")

        if self.skills:
            skill_strs = [f"{s.name}({s.level})" for s in self.skills[:8]]
            lines.append(f"【核心技能】{', '.join(skill_strs)}")

        # 注入 Skill 池中关联 skill 的完整工作框架内容
        if self.skill_ids:
            # 延迟导入避免循环依赖
            try:
                from pathlib import Path
                import json as _json
                _skills_file = Path(__file__).parent.parent / "data" / "skills.json"
                if _skills_file.exists():
                    _skill_pool = _json.loads(_skills_file.read_text(encoding="utf-8"))
                    _injected = []
                    for _sid in self.skill_ids:
                        _sk = _skill_pool.get(_sid)
                        if _sk and _sk.get("status") == "active" and _sk.get("content"):
                            _injected.append("--- " + _sk['name'] + " ---\n" + _sk['content'].strip())
                    if _injected:
                        lines.append("\n【已配置 Skill 工作框架（" + str(len(_injected)) + " 个）】")
                        for _block in _injected:
                            lines.append(_block)
            except Exception:
                pass  # skill 注入失败不影响主流程

        if self.behavior_rules:
            lines.append("\n【行为规范】")
            for i, rule in enumerate(self.behavior_rules, 1):
                lines.append(f"  {i}. {rule}")

        if self.user_preferences:
            lines.append("\n【用户个性化偏好】")
            for pref in self.user_preferences:
                lines.append(f"  • {pref}")

        if self.rejection_policy:
            lines.append(f"\n【拒绝策略】\n{self.rejection_policy}")

        if self.output_format:
            lines.append(f"\n【输出格式要求】\n{self.output_format}")

        # 长期记忆（训练提炼的精华）
        if self.long_term_memory:
            lines.append(f"\n【个人长期记忆（训练积累）】\n{self.long_term_memory}")

        if self.avg_quality_score > 0:
            lines.append(f"\n【历史表现】平均质检得分：{self.avg_quality_score:.1f}/100，"
                         f"完成任务：{self.total_tasks_completed}，失败：{self.total_tasks_failed}")

        return "\n".join(lines)

    def update_performance(self, score: float, success: bool, project_id: str, project_name: str) -> None:
        """更新历史表现（每次任务完成后调用）"""
        self.total_tasks_completed += 1 if success else 0
        self.total_tasks_failed += 0 if success else 1
        # 滑动平均
        n = self.total_tasks_completed + self.total_tasks_failed
        if n > 0:
            self.avg_quality_score = (self.avg_quality_score * (n - 1) + score) / n
        # 记录项目历史（最近 10 个）
        self.project_history.append({
            "project_id": project_id,
            "project_name": project_name,
            "score": score,
            "success": success,
            "at": time.time(),
        })
        self.project_history = self.project_history[-10:]
        self.updated_at = time.time()

    def to_dict(self) -> Dict:
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: Dict) -> "ExpertProfile":
        return cls(**data)


# ─── 项目记忆（短期，按项目隔离）────────────────────────────────────────────

class ProjectMemoryEntry(BaseModel):
    """单条项目记忆"""
    entry_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    created_at: float = Field(default_factory=time.time)
    memory_type: Literal["decision", "feedback", "context", "issue", "milestone"] = "context"
    content: str
    importance: float = 1.0  # 1.0 = 普通，2.0 = 重要，3.0 = 关键


class ExpertProjectMemory(BaseModel):
    """
    专家的项目记忆（短期，按项目隔离）

    类比：一个员工在某个项目上的工作日志 + 反馈记录
    - 新项目 = 空的项目记忆
    - 同一专家在不同项目有不同的项目记忆
    """
    expert_id: str
    project_id: str
    phase_id: str = ""          # 当前阶段（阶段 Agent 只有阶段记忆）
    scope: Literal["project", "phase"] = "project"  # project=整体记忆，phase=阶段记忆

    entries: List[ProjectMemoryEntry] = Field(default_factory=list)
    context_summary: str = ""   # 压缩后的上下文摘要（超过阈值后压缩）
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    def add_entry(
        self,
        content: str,
        memory_type: str = "context",
        importance: float = 1.0,
    ) -> None:
        self.entries.append(ProjectMemoryEntry(
            memory_type=memory_type,  # type: ignore
            content=content,
            importance=importance,
        ))
        self.updated_at = time.time()
        # 超过 50 条时保留重要的 + 最近 20 条
        if len(self.entries) > 50:
            important = [e for e in self.entries if e.importance >= 2.0]
            recent = self.entries[-20:]
            seen = {e.entry_id for e in important}
            merged = important + [e for e in recent if e.entry_id not in seen]
            self.entries = sorted(merged, key=lambda e: e.created_at)

    def to_context_str(self, max_entries: int = 15) -> str:
        """生成注入 System Prompt 的项目记忆块"""
        if not self.entries and not self.context_summary:
            return ""

        lines = [f"【项目记忆 — {self.scope}级别】"]
        if self.context_summary:
            lines.append(f"历史摘要：{self.context_summary}")

        # 按重要性 + 时间排序，取最近的
        sorted_entries = sorted(self.entries, key=lambda e: (e.importance, e.created_at), reverse=True)
        for entry in sorted_entries[:max_entries]:
            type_label = {
                "decision": "📌决策",
                "feedback": "💬反馈",
                "context": "📝上下文",
                "issue": "⚠️问题",
                "milestone": "🎯里程碑",
            }.get(entry.memory_type, "📝")
            lines.append(f"  {type_label} {entry.content[:200]}")

        return "\n".join(lines)

    def to_dict(self) -> Dict:
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: Dict) -> "ExpertProjectMemory":
        return cls(**data)


# ─── 专家池管理器 ─────────────────────────────────────────────────────────────

class ExpertPool:
    """
    专家池管理器

    职责：
    1. 管理专家档案（CRUD）
    2. 管理专家的项目记忆（按 expert_id + project_id 隔离）
    3. 提供任务-专家匹配（基于技能/领域/历史表现）
    4. 持久化到本地 JSON
    """

    POOL_FILE = "backend/data/expert_pool.json"
    MEMORY_DIR = "backend/data/expert_memories"

    def __init__(self, data_dir: Optional[str] = None):
        # Resolve the default from this module, not the process working
        # directory. In the Render image backend/ is copied to /app, so the
        # former relative "backend/data" attempted to create /app/backend as
        # the non-root runtime user and failed with EACCES.
        self._data_dir = (
            Path(data_dir)
            if data_dir is not None
            else metis_data_path(
                "pools",
                legacy=Path(__file__).resolve().parents[1] / "data",
            )
        )
        self._data_dir.mkdir(parents=True, exist_ok=True)
        (self._data_dir / "expert_memories").mkdir(exist_ok=True)

        self._pool_file = self._data_dir / "expert_pool.json"
        self._memory_dir = self._data_dir / "expert_memories"

        # 内存缓存
        self._experts: Dict[str, ExpertProfile] = {}
        self._memories: Dict[str, ExpertProjectMemory] = {}  # key: "{expert_id}:{project_id}:{scope}"

        self._load()
        # 首次启动时预置9类专家
        self._init_preset_experts()

    def _init_preset_experts(self) -> None:
        """首次启动时预置9类专家（已存在则跳过）"""
        presets = (
            ("frontend", "前端工程师", "前端专家", "🎨", ["React", "TypeScript", "响应式界面", "可访问性"]),
            ("backend", "后端工程师", "后端专家", "⚙️", ["Python", "FastAPI", "业务建模", "并发控制"]),
            ("database", "数据库工程师", "数据库专家", "🗄️", ["PostgreSQL", "数据建模", "迁移", "查询优化"]),
            ("api", "API 设计师", "API设计专家", "🔌", ["REST API", "接口契约", "鉴权", "错误处理"]),
            ("architect", "系统架构师", "架构师", "🏗️", ["系统设计", "模块边界", "可扩展性", "技术决策"]),
            ("devops", "DevOps 工程师", "DevOps专家", "🚀", ["Docker", "CI/CD", "部署", "可观测性"]),
            ("security", "安全工程师", "安全专家", "🔐", ["OWASP", "威胁建模", "权限控制", "安全审计"]),
            ("testing", "测试工程师", "测试专家", "🧪", ["自动化测试", "集成测试", "边界分析", "回归测试"]),
            ("data", "数据工程师", "数据专家", "📊", ["数据处理", "ETL", "质量校验", "指标设计"]),
        )
        changed = False
        for key, name, role, avatar, skills in presets:
            ep = {
                "expert_id": f"preset-{key}",
                "name": name,
                "role": role,
                "type": "pg",
                "avatar": avatar,
                "skills": skills,
                "domains": skills,
                "role_description": f"负责项目中的{role}工作，输出可验证、可维护的交付结果。",
                "working_style": "先明确边界和验收标准，再实施并完成自检",
                "behavior_rules": ["不编造执行结果", "交付必须包含可验证证据"],
            }
            if ep["expert_id"] not in self._experts:
                profile = ExpertProfile(
                    expert_id=ep["expert_id"],
                    name=ep["name"],
                    role=ep["role"],
                    agent_type=ep.get("type", "pg"),
                    avatar=ep.get("avatar", "🤖"),
                    domains=ep.get("domains", ep["skills"][:4]),
                    role_description=ep.get("role_description", ep.get("description", "")),
                    working_style=ep.get("working_style", ""),
                    thinking_framework=ep.get("thinking_framework", ""),
                    behavior_rules=ep.get("behavior_rules", []),
                    rejection_policy=ep.get("rejection_policy", ""),
                    output_format=ep.get("output_format", ""),
                    skills=[
                        ExpertSkill(name=s, level="expert")
                        for s in ep["skills"]
                    ],
                    status="available",
                )
                self._experts[profile.expert_id] = profile
                changed = True
        if changed:
            self.save()

    # ── 持久化 ────────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """从磁盘加载专家池"""
        if self._pool_file.exists():
            try:
                data = json.loads(self._pool_file.read_text(encoding="utf-8"))
                for expert_data in data.get("experts", []):
                    try:
                        profile = ExpertProfile.from_dict(expert_data)
                        self._experts[profile.expert_id] = profile
                    except Exception:
                        pass
            except Exception:
                pass

        # 加载项目记忆
        for mem_file in self._memory_dir.glob("*.json"):
            try:
                mem_data = json.loads(mem_file.read_text(encoding="utf-8"))
                mem = ExpertProjectMemory.from_dict(mem_data)
                key = self._mem_key(mem.expert_id, mem.project_id, mem.scope, mem.phase_id)
                self._memories[key] = mem
            except Exception:
                pass

    def save(self) -> None:
        """原子持久化到磁盘：先写临时文件，再重命名，防止写入中断导致数据损坏"""
        import tempfile as _tmp

        # 原子写入专家池文件
        pool_data = {"experts": [e.to_dict() for e in self._experts.values()]}
        tmp_fd, tmp_path = _tmp.mkstemp(
            suffix=".json", prefix=".expert_pool_", dir=str(self._pool_file.parent)
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(pool_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, str(self._pool_file))
        except Exception:
            os.unlink(tmp_path, missing_ok=True)
            raise

        # 原子写入项目记忆文件
        for key, mem in self._memories.items():
            safe_key = key.replace(":", "_")
            mem_file = self._memory_dir / f"{safe_key}.json"
            mem_tmp_fd, mem_tmp_path = _tmp.mkstemp(
                suffix=".json", prefix=".mem_", dir=str(self._memory_dir)
            )
            try:
                with os.fdopen(mem_tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(mem.to_dict(), f, ensure_ascii=False, indent=2)
                os.replace(mem_tmp_path, str(mem_file))
            except Exception:
                os.unlink(mem_tmp_path, missing_ok=True)

    @staticmethod
    def _mem_key(expert_id: str, project_id: str, scope: str = "project", phase_id: str = "") -> str:
        if scope == "phase" and phase_id:
            return f"{expert_id}:{project_id}:{scope}:{phase_id}"
        return f"{expert_id}:{project_id}:{scope}"

    # ── 专家 CRUD ─────────────────────────────────────────────────────────────

    def create_expert(self, profile: ExpertProfile) -> ExpertProfile:
        """创建专家档案"""
        self._experts[profile.expert_id] = profile
        self.save()
        return profile

    def get_expert(self, expert_id: str) -> Optional[ExpertProfile]:
        return self._experts.get(expert_id)

    def update_expert(self, expert_id: str, updates: Dict) -> Optional[ExpertProfile]:
        """更新专家档案"""
        profile = self._experts.get(expert_id)
        if not profile:
            return None
        for k, v in updates.items():
            if hasattr(profile, k):
                setattr(profile, k, v)
        profile.updated_at = time.time()
        self.save()
        return profile

    def delete_expert(self, expert_id: str) -> bool:
        if expert_id not in self._experts:
            return False
        del self._experts[expert_id]
        self.save()
        return True

    def list_experts(
        self,
        agent_type: Optional[str] = None,
        status: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> List[ExpertProfile]:
        """列出专家（支持过滤）"""
        result = list(self._experts.values())
        if agent_type:
            result = [e for e in result if e.agent_type == agent_type]
        if status:
            result = [e for e in result if e.status == status]
        if domain:
            result = [e for e in result if domain in e.domains]
        return sorted(result, key=lambda e: e.avg_quality_score, reverse=True)

    # ── 项目记忆 CRUD ─────────────────────────────────────────────────────────

    def get_project_memory(
        self,
        expert_id: str,
        project_id: str,
        scope: str = "project",
        phase_id: str = "",
    ) -> ExpertProjectMemory:
        """获取（或创建）专家的项目记忆"""
        key = self._mem_key(expert_id, project_id, scope, phase_id)
        if key not in self._memories:
            self._memories[key] = ExpertProjectMemory(
                expert_id=expert_id,
                project_id=project_id,
                phase_id=phase_id,
                scope=scope,  # type: ignore
            )
        return self._memories[key]

    def add_memory_entry(
        self,
        expert_id: str,
        project_id: str,
        content: str,
        memory_type: str = "context",
        importance: float = 1.0,
        scope: str = "project",
        phase_id: str = "",
    ) -> None:
        """向专家的项目记忆中添加条目"""
        mem = self.get_project_memory(expert_id, project_id, scope, phase_id)
        mem.add_entry(content, memory_type, importance)
        self.save()

    def get_memory_context(
        self,
        expert_id: str,
        project_id: str,
        scope: str = "project",
        phase_id: str = "",
    ) -> str:
        """获取注入 System Prompt 的记忆上下文字符串"""
        mem = self.get_project_memory(expert_id, project_id, scope, phase_id)
        return mem.to_context_str()

    def clear_project_memory(self, expert_id: str, project_id: str) -> None:
        """清空专家在某项目的所有记忆（项目结束时调用）"""
        keys_to_del = [
            k for k in self._memories
            if k.startswith(f"{expert_id}:{project_id}:")
        ]
        for k in keys_to_del:
            del self._memories[k]
        self.save()

    # ── 匹配 ──────────────────────────────────────────────────────────────────

    def match_experts(
        self,
        required_role: str,
        required_domains: Optional[List[str]] = None,
        required_skills: Optional[List[str]] = None,
        agent_type: Optional[str] = None,
        required_expert_type: Optional[str] = None,
        top_k: int = 3,
        exclude_busy: bool = False,
        exclude_expert_ids: Optional[List[str]] = None,
    ) -> List[Dict]:
        """
        任务-专家匹配。

        匹配逻辑（得分越高越优先）：
        1. 角色匹配：role 包含 required_role → +10
        2. 领域匹配：每个匹配的 domain → +3
        3. 技能匹配：每个匹配的 skill → +2
        4. 历史表现：avg_quality_score / 10 → 最多 +10
        5. 状态加分：available → +5，busy → 0
        """
        candidates = self.list_experts(agent_type=agent_type)
        requested_type = canonical_expert_type(
            required_expert_type or required_role
        )
        if required_expert_type and not requested_type:
            return []
        if requested_type:
            candidates = [
                expert for expert in candidates
                if canonical_expert_type(expert.role) == requested_type
            ]
        excluded_ids = set(exclude_expert_ids or [])
        if excluded_ids:
            candidates = [e for e in candidates if e.expert_id not in excluded_ids]
        if exclude_busy:
            candidates = [e for e in candidates if e.status == "available"]

        scored = []
        req_role_lower = required_role.lower()
        req_domains = [d.lower() for d in (required_domains or [])]
        req_skills = [s.lower() for s in (required_skills or [])]

        for expert in candidates:
            score = 0.0

            # 角色匹配
            if req_role_lower in expert.role.lower() or expert.role.lower() in req_role_lower:
                score += 10

            # 领域匹配
            expert_domains_lower = [d.lower() for d in expert.domains]
            for d in req_domains:
                if any(d in ed or ed in d for ed in expert_domains_lower):
                    score += 3

            # 技能匹配
            expert_skills_lower = [s.name.lower() for s in expert.skills]
            for s in req_skills:
                if any(s in es or es in s for es in expert_skills_lower):
                    score += 2

            # 历史表现
            score += expert.avg_quality_score / 10

            # 状态加分
            if expert.status == "available":
                score += 5

            scored.append({"expert": expert, "score": score})

        scored.sort(key=lambda x: x["score"], reverse=True)
        return [
            {
                "expert_id": item["expert"].expert_id,
                "name": item["expert"].name,
                "role": item["expert"].role,
                "expert_type": (
                    canonical_expert_type(item["expert"].role) or ""
                ),
                "score": round(item["score"], 1),
                "status": item["expert"].status,
                "avg_quality_score": item["expert"].avg_quality_score,
                "domains": item["expert"].domains,
            }
            for item in scored[:top_k]
        ]

    def build_system_prompt(
        self,
        expert_id: str,
        project_id: str,
        scope: str = "project",
        phase_id: str = "",
        extra_context: str = "",
    ) -> str:
        """
        为专家构建完整的 System Prompt。
        = 角色记忆块 + 项目记忆块 + 额外上下文
        """
        parts = []

        profile = self._experts.get(expert_id)
        if profile:
            parts.append(profile.to_system_prompt_block())

        mem_ctx = self.get_memory_context(expert_id, project_id, scope, phase_id)
        if mem_ctx:
            parts.append(mem_ctx)

        if extra_context:
            parts.append(extra_context)

        return "\n\n".join(parts)

    def update_expert_performance(
        self,
        expert_id: str,
        project_id: str,
        project_name: str,
        score: float,
        success: bool,
    ) -> None:
        """任务完成后更新专家历史表现"""
        profile = self._experts.get(expert_id)
        if profile:
            profile.update_performance(score, success, project_id, project_name)
            self.save()

    # ── 训练 & 长期记忆 ───────────────────────────────────────────────────────

    def add_training_session(
        self,
        expert_id: str,
        session_type: str,
        feedback: str,
        user_input: str = "",
        agent_output: str = "",
        correction: str = "",
    ) -> Optional[TrainingSession]:
        """
        添加训练会话记录。

        每次用户对专家的输出给出反馈/纠错/风格调整时调用。
        训练记录跨项目保留，是专家的"个人成长档案"。
        """
        profile = self._experts.get(expert_id)
        if not profile:
            return None

        session = TrainingSession(
            session_type=session_type,  # type: ignore
            user_input=user_input,
            agent_output=agent_output,
            feedback=feedback,
            correction=correction,
        )
        profile.training_sessions.append(session)
        # 最多保留 100 条训练记录
        if len(profile.training_sessions) > 100:
            profile.training_sessions = profile.training_sessions[-100:]

        profile.updated_at = time.time()
        self.save()
        return session

    def apply_training_to_memory(
        self,
        expert_id: str,
        hermes_client=None,
    ) -> str:
        """
        将训练记录提炼为长期记忆，注入 system prompt。

        策略：
        1. 收集所有未应用（applied=False）的训练记录
        2. 用 LLM 压缩提炼为结构化的行为指导
        3. 合并到 long_term_memory
        4. 标记训练记录为 applied=True
        5. 如果有明确的 user_preferences，自动提取并追加

        返回：新的 long_term_memory 字符串
        """
        profile = self._experts.get(expert_id)
        if not profile:
            return ""

        pending = [s for s in profile.training_sessions if not s.applied]
        if not pending:
            return profile.long_term_memory

        # 构建训练摘要文本
        session_lines = []
        for s in pending:
            parts_list = [f"[{s.session_type}]"]
            if s.user_input:
                parts_list.append(f"用户输入：{s.user_input[:200]}")
            if s.agent_output:
                parts_list.append(f"Agent输出：{s.agent_output[:200]}")
            if s.feedback:
                parts_list.append(f"反馈：{s.feedback[:300]}")
            if s.correction:
                parts_list.append(f"期望行为：{s.correction[:300]}")
            session_lines.append(" | ".join(parts_list))

        sessions_text = "\n".join(session_lines)
        existing_memory = profile.long_term_memory or "（暂无）"

        new_memory = existing_memory  # 默认不变

        if hermes_client:
            try:
                from core.hermes_client import Message, MessageRole
                resp = hermes_client.chat([
                    Message(role=MessageRole.SYSTEM, content=(
                        "你是一个 AI Agent 训练系统。请将以下训练记录提炼为简洁的行为指导，"
                        "合并到现有长期记忆中。\n\n"
                        "输出格式（纯文本，不要 JSON）：\n"
                        "【输出风格】...\n"
                        "【思考方式】...\n"
                        "【禁止行为】...\n"
                        "【用户偏好】...\n"
                        "【其他重要指导】...\n\n"
                        "规则：\n"
                        "- 只保留对 Agent 行为有实质影响的指导\n"
                        "- 合并重复内容，保持简洁\n"
                        "- 矛盾的指导以最新的为准"
                    )),
                    Message(role=MessageRole.USER, content=(
                        f"现有长期记忆：\n{existing_memory}\n\n"
                        f"新训练记录：\n{sessions_text}"
                    )),
                ])
                new_memory = resp.get("content", existing_memory) or existing_memory
            except Exception:
                # LLM 不可用时，直接拼接
                new_memory = existing_memory + "\n\n【新增训练记录】\n" + sessions_text

        # 自动提取 user_preferences（从 style_tune 类型的 correction 中提取）
        for s in pending:
            if s.session_type in ("style_tune", "output_tune") and s.correction:
                pref = s.correction.strip()
                if pref and pref not in profile.user_preferences:
                    profile.user_preferences.append(pref)
        # 最多保留 20 条偏好
        profile.user_preferences = profile.user_preferences[-20:]

        profile.long_term_memory = new_memory
        # 标记为已应用
        for s in pending:
            s.applied = True
        profile.updated_at = time.time()
        self.save()
        return new_memory

    def update_work_mode(
        self,
        expert_id: str,
        work_mode_updates: Dict,
    ) -> Optional[WorkMode]:
        """
        更新专家的工作模式配置。
        支持部分更新（只传需要修改的字段）。
        """
        profile = self._experts.get(expert_id)
        if not profile:
            return None

        wm = profile.work_mode
        for k, v in work_mode_updates.items():
            if hasattr(wm, k):
                setattr(wm, k, v)

        profile.updated_at = time.time()
        self.save()
        return wm

    def get_training_sessions(
        self,
        expert_id: str,
        limit: int = 20,
        only_pending: bool = False,
    ) -> List[Dict]:
        """获取专家的训练记录"""
        profile = self._experts.get(expert_id)
        if not profile:
            return []
        sessions = profile.training_sessions
        if only_pending:
            sessions = [s for s in sessions if not s.applied]
        return [s.model_dump() for s in sessions[-limit:]]

    def preview_system_prompt(self, expert_id: str) -> str:
        """
        预览专家当前的完整 System Prompt（不含项目记忆）。
        供前端训练面板实时展示效果。
        """
        profile = self._experts.get(expert_id)
        if not profile:
            return "专家不存在"
        return profile.to_system_prompt_block()

    # ── 对话训练 ──────────────────────────────────────────────────────────────

    def chat_train(
        self,
        expert_id: str,
        user_message: str,
        history: List[Dict],
        hermes_client=None,
    ) -> Dict:
        """
        与专家进行训练对话。

        流程：
        1. 用专家当前 system prompt 作为角色基础
        2. 专家回答用户问题
        3. 返回回答，用户可随时对回答给出反馈（调用 add_training_session）

        Args:
            expert_id: 专家 ID
            user_message: 用户当前消息
            history: 对话历史 [{"role": "user"/"assistant", "content": "..."}]
            hermes_client: LLM 客户端

        Returns:
            {"reply": str, "expert_id": str, "system_prompt_preview": str}
        """
        profile = self._experts.get(expert_id)
        if not profile:
            return {"reply": "专家不存在", "expert_id": expert_id}

        if not hermes_client:
            return {
                "reply": "⚠️ 未配置 API Key，无法进行对话训练。请先在设置页面配置 LLM API Key。",
                "expert_id": expert_id,
            }

        system_prompt = profile.to_system_prompt_block()

        try:
            from core.hermes_client import Message, MessageRole
            messages = [Message(role=MessageRole.SYSTEM, content=system_prompt)]
            for h in history[-12:]:  # 最近 12 条历史
                role = MessageRole.USER if h.get("role") == "user" else MessageRole.ASSISTANT
                if h.get("content"):
                    messages.append(Message(role=role, content=h["content"]))
            messages.append(Message(role=MessageRole.USER, content=user_message))

            resp = hermes_client.chat(messages)
            reply = resp.get("content", "")
        except Exception as e:
            reply = f"对话失败：{e}"

        return {
            "reply": reply,
            "expert_id": expert_id,
            "expert_name": profile.name,
        }

    # ── 知识文件上传 ──────────────────────────────────────────────────────────

    def add_knowledge(
        self,
        expert_id: str,
        content: str,
        source_name: str = "",
        knowledge_type: str = "document",
        hermes_client=None,
    ) -> Dict:
        """
        向专家喂入知识文件/文本。

        策略：
        1. 如果内容较短（<2000字），直接追加到 long_term_memory
        2. 如果内容较长，用 LLM 压缩提炼为关键知识点后追加
        3. 同时记录一条 training_session（类型=knowledge）

        Args:
            expert_id: 专家 ID
            content: 知识内容（文件文本或粘贴文本）
            source_name: 来源名称（文件名或标题）
            knowledge_type: document/rule/example/reference
            hermes_client: LLM 客户端（用于压缩长文本）

        Returns:
            {"success": bool, "summary": str, "memory_updated": bool}
        """
        profile = self._experts.get(expert_id)
        if not profile:
            return {"success": False, "error": "专家不存在"}

        label = f"【知识来源：{source_name}】" if source_name else "【知识文件】"
        summary = content

        # 长文本用 LLM 压缩
        if len(content) > 2000 and hermes_client:
            try:
                from core.hermes_client import Message, MessageRole
                resp = hermes_client.chat([
                    Message(role=MessageRole.SYSTEM, content=(
                        f"你是一个知识提炼助手。请将以下文档内容提炼为「{profile.role}」需要掌握的关键知识点。\n\n"
                        "输出格式：\n"
                        "- 每条知识点一行，以「• 」开头\n"
                        "- 只保留对该角色工作有直接帮助的内容\n"
                        "- 最多输出 20 条，每条不超过 100 字\n"
                        "- 不要输出无关的背景介绍"
                    )),
                    Message(role=MessageRole.USER, content=f"文档内容：\n{content[:8000]}"),
                ])
                extracted = resp.get("content", "")
                if extracted and not extracted.startswith("⚠️"):
                    summary = extracted
            except Exception:
                # LLM 失败时截断原文
                summary = content[:1500] + "...(已截断)"

        # 追加到 long_term_memory
        knowledge_block = f"\n\n{label}\n{summary}"
        profile.long_term_memory = (profile.long_term_memory or "") + knowledge_block
        # 长期记忆上限 10000 字
        if len(profile.long_term_memory) > 10000:
            profile.long_term_memory = profile.long_term_memory[-10000:]

        # 同时记录训练会话（方便追溯）
        session = TrainingSession(
            session_type="feedback",
            user_input=f"[知识上传] {source_name}",
            agent_output="",
            feedback=f"已上传知识文件：{source_name}",
            correction=summary[:500],
            applied=True,  # 直接标记为已应用
        )
        profile.training_sessions.append(session)
        if len(profile.training_sessions) > 100:
            profile.training_sessions = profile.training_sessions[-100:]

        profile.updated_at = time.time()
        self.save()

        return {
            "success": True,
            "expert_id": expert_id,
            "source_name": source_name,
            "original_length": len(content),
            "summary_length": len(summary),
            "compressed": len(summary) < len(content),
            "summary_preview": summary[:300],
        }


# ─── 全局单例（供 main.py 使用）──────────────────────────────────────────────

_user_expert_pools: Dict[str, ExpertPool] = {}


def get_expert_pool(user_id: str = "") -> ExpertPool:
    from core.user_scope import active_user_id, user_storage_key

    owner_id = active_user_id(user_id)
    key = user_storage_key(owner_id)
    pool = _user_expert_pools.get(key)
    if pool is None:
        base = metis_data_path(
            "pools",
            legacy=Path(__file__).resolve().parents[1] / "data",
        )
        data_dir = base if not owner_id else base / "users" / key / "experts"
        pool = ExpertPool(data_dir=str(data_dir))
        _user_expert_pools[key] = pool
    return pool
