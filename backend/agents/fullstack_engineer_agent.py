"""
全能工程师 Agent (FullStackEngineerAgent) v1
职责：
  1. 代码整改   — 接收 needs_manual 缺陷单，与用户对话确认后执行整改，整改后触发质检
  2. 文档撰写   — 根据项目结构 + 阶段规划自动生成完整使用手册
  3. 文件归档   — 扫描 workspace，分类标注 source/test/doc/config/output，标注所属阶段
  4. 项目问答   — 独立上下文，回答用户关于项目的任意问题

遵守系统底线原则（AGENT_PRINCIPLES）：
  - Plan Before Execute：先给出整改方案，用户确认后才写文件
  - Immutability：不打补丁，写新的完整版本替换旧文件
  - Security-First：整改时主动检测安全问题
  - Agent-First：任务三要素不明确直接追问
  - 每套对话上下文完全隔离，互不污染
"""

import ast
import difflib
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

from .base.hermes_agent import AgentBase, AgentType, AgentState, Task, AGENT_PRINCIPLES
from core.hermes_client import Message, MessageRole

# 专家池（可选依赖，不存在时降级为无记忆模式）
try:
    from core.expert_pool import ExpertPool, ExpertProfile, ExpertSkill, get_expert_pool as _get_ep
    _EXPERT_POOL_AVAILABLE = True
except ImportError:
    _EXPERT_POOL_AVAILABLE = False

# 全能工程师在专家池中的固定 expert_id（跨项目复用同一个人格记忆）
ENGINEER_EXPERT_ID = "expert-fullstack-engineer-001"


# ── Skills 清单（9项核心能力）────────────────────────────────────────────────
ENGINEER_SKILLS = [
    "代码静态分析",       # skill-eng-01
    "代码整改执行",       # skill-eng-02
    "使用手册生成",       # skill-eng-03
    "文件归档分类",       # skill-eng-04
    "项目问答服务",       # skill-eng-05
    "阶段感知",           # skill-eng-06
    "质检验证",           # skill-eng-07
    "Defect管理",         # skill-eng-08
    "任务规划接收",       # skill-eng-09
]

# 文件分类规则
FILE_CATEGORY_RULES: List[Tuple[str, List[str], str]] = [
    # (类别, 路径关键词或后缀, 说明)
    ("test",   ["test", "tests", "spec", "_test.", ".test.", ".spec."],              "测试文件"),
    ("doc",    [".md", ".rst", ".txt", "docs/", "README", "CHANGELOG", "LICENSE"],  "文档文件"),
    ("config", [".env", ".yml", ".yaml", ".toml", ".ini", ".cfg", "config/",
                "Dockerfile", "docker-compose", "nginx.conf", "requirements",
                "package.json", "tsconfig", "vite.config", "pytest.ini"],           "配置文件"),
    ("output", ["output/", "dist/", "build/", ".pyc", "__pycache__", ".cache"],     "构建产出"),
    ("source", [],                                                                    "源代码"),  # 兜底
]

USELESS_PATTERNS = [
    ".pyc", "__pycache__", ".cache", ".DS_Store", "Thumbs.db",
    ".git/", "node_modules/", ".pytest_cache/", ".mypy_cache/",
]


def _display_values(value: Any) -> List[str]:
    """Normalize scalar/list/mapping planning fields for prompt rendering."""
    if value is None:
        return []
    if isinstance(value, dict):
        rendered: List[str] = []
        for key, item in value.items():
            values = _display_values(item)
            if values:
                rendered.append(f"{key}: {'、'.join(values)}")
        return rendered
    if isinstance(value, (list, tuple, set)):
        rendered = []
        for item in value:
            rendered.extend(_display_values(item))
        return rendered
    text = str(value).strip()
    return [text] if text else []


def _mapping_rows(value: Any) -> List[Dict[str, Any]]:
    """Accept a row list, one row mapping, or an id-to-row mapping."""
    if isinstance(value, dict):
        if any(
            key in value
            for key in ("id", "phase_id", "task_id", "name", "description")
        ):
            return [value]
        return [item for item in value.values() if isinstance(item, dict)]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, dict)]
    return []


def _classify_file(rel_path: str) -> Tuple[str, bool]:
    """
    根据路径判断文件类别和是否无用。
    返回 (category, is_useless)
    """
    lower = rel_path.lower().replace("\\", "/")

    # 先判断是否无用
    for pat in USELESS_PATTERNS:
        if pat in lower:
            return "output", True

    for category, patterns, _ in FILE_CATEGORY_RULES:
        for pat in patterns:
            if pat in lower:
                return category, False

    return "source", False


class FullStackEngineerAgent(AgentBase):
    """
    全能工程师 Agent
    - 承接 PM 组长的 final_plan，了解项目全貌
    - 处理 needs_manual 缺陷单（与用户对话 → 确认方案 → 写文件 → 质检）
    - 生成使用手册（保存到 workspace/docs/）
    - 文件归档（扫描 + 分类 + 阶段标注）
    - 项目问答（独立上下文）
    """

    ESSENTIAL_CAPABILITIES = ENGINEER_SKILLS

    def __init__(self, *args, project_id: str = "", workspace: str = "", **kwargs):
        self._capabilities = list(ENGINEER_SKILLS)
        self.project_id = project_id
        self.workspace = Path(workspace) if workspace else Path(".")

        # ── 四套完全隔离的对话上下文 ──────────────────────────────────────────
        # 整改对话：以 defect_id 为维度，每个缺陷独立上下文
        self.repair_history: Dict[str, List[Dict]] = {}       # defect_id → history
        # 项目问答：独立上下文
        self.qa_history: List[Dict] = []
        # 文档撰写：独立上下文
        self.doc_history: List[Dict] = []
        # 整改方案暂存：等待用户确认，确认后才写文件
        self.pending_fixes: Dict[str, Dict] = {}              # defect_id → fix_plan
        self.pending_fix_proposals: Dict[str, Dict[str, Any]] = {}
        # defect_id → immutable, server-generated confirmation record
        self.confirmed_fix_plans: Dict[str, Dict[str, Any]] = {}

        # 项目背景（由 PM 组长 final_plan 填充）
        self.project_background: str = ""
        self.final_plan: Optional[Dict] = None
        self.phase_info: List[Dict] = []   # [{phase_id, phase_name, description, ...}]

        # 归档结果缓存
        self.archive_result: Optional[Dict] = None

        # ── 文件修改计数器（保证每个文件修改次数均值 ≤3）─────────────────────
        # file_path → 修改次数
        self.file_edit_counter: Dict[str, int] = {}
        # file_path → 最后一次修改时的静态检查分数（用于 pre-flight 回归对比）
        self.file_last_score: Dict[str, int] = {}
        # 每个文件的修改上限（超过后强制 pre-flight 确认）
        self.FILE_EDIT_LIMIT = 3

        super().__init__(*args, agent_type=AgentType.PG, **kwargs)

    # ══════════════════════════════════════════════════════════════════════════
    # 一、项目背景注入（由 main.py 在创建后调用）
    # ══════════════════════════════════════════════════════════════════════════

    def load_project_context(
        self,
        final_plan: Dict,
        phase_info: Optional[List[Dict]] = None,
        subprojects: Optional[List[Dict]] = None,
        qc_results: Optional[Dict] = None,
    ) -> None:
        """
        从 PM 组长的 final_plan 中加载项目完整背景信息。
        新增：
          - 完整阶段任务（不截断每阶段描述）
          - 子项目列表（名称/描述/技术栈/状态）
          - 质检缺陷汇总（让 Agent 知道哪些文件有问题、问题是什么）
        """
        if not isinstance(final_plan, dict):
            raise TypeError("final_plan must be a mapping")
        self.final_plan = final_plan
        self.phase_info = _mapping_rows(
            phase_info if phase_info else final_plan.get("phases"),
        )
        self._subprojects = _mapping_rows(subprojects)
        self._qc_results = qc_results if isinstance(qc_results, dict) else {}

        # ── 基础信息 ──────────────────────────────────────────────────────────
        overview = str(
            final_plan.get("project_overview")
            or final_plan.get("summary")
            or ""
        )
        features = _display_values(final_plan.get("core_features"))
        features_text = (
            "\n".join(f"  - {feature}" for feature in features)
            if features else "（未指定）"
        )
        tech = final_plan.get("tech_stack")
        tech_values = _display_values(tech)
        tech_str = " | ".join(tech_values) if tech_values else "未指定"

        # ── 完整阶段任务（不截断，每条阶段完整保留）─────────────────────────
        phases_lines = []
        for p in self.phase_info:
            name = str(p.get("name") or "")
            desc = str(p.get("description") or "")
            raw_tasks = p.get("tasks") or p.get("task_contract")
            tasks = _mapping_rows(raw_tasks)
            tasks_text = ""
            if tasks:
                task_labels = [
                    str(
                        task.get("name")
                        or task.get("task_name")
                        or task.get("description")
                        or task.get("task_description")
                        or task.get("task_id")
                        or ""
                    )
                    for task in tasks
                ]
            else:
                task_labels = _display_values(raw_tasks)
            if task_labels:
                tasks_text = "\n    任务: " + "；".join(task_labels)
            locked = _display_values(p.get("locked_tasks"))
            acceptance = _display_values(p.get("acceptance_criteria"))
            dependencies = _display_values(p.get("dependencies"))
            source_ids = _display_values(
                p.get("source_ids") or p.get("source_constraints"),
            )
            deliverables = _display_values(
                p.get("required_deliverables") or p.get("deliverables"),
            )
            phases_lines.append(f"  [{name}] {desc}{tasks_text}")
            if locked:
                phases_lines.append(f"    锁定任务: {locked}")
            if acceptance:
                phases_lines.append(f"    验收标准: {acceptance}")
            if dependencies:
                phases_lines.append(f"    依赖: {dependencies}")
            if source_ids:
                phases_lines.append(f"    来源ID: {source_ids}")
            if deliverables:
                phases_lines.append(f"    必需交付: {deliverables}")
        phases_text = "\n".join(phases_lines) if phases_lines else "（尚未划分阶段）"

        # ── 子项目列表（含技术栈和完成状态）─────────────────────────────────
        sp_lines = []
        for sp in self._subprojects:
            sp_id = str(sp.get("id") or "")
            sp_name = str(sp.get("name") or "")
            sp_desc = str(sp.get("description") or "")
            sp_stat = str(sp.get("status") or "")
            sp_tech = "/".join(_display_values(sp.get("tech_stack")))
            sp_roles = "/".join(_display_values(sp.get("roles_needed")))
            sp_lines.append(
                f"  [{sp_id}] {sp_name}（{sp_stat}）\n"
                f"    描述: {sp_desc}\n"
                f"    技术栈: {sp_tech} | 角色: {sp_roles}\n"
                f"    锁定任务: {sp.get('locked_tasks', sp.get('locked_task', []))}\n"
                f"    验收标准: {sp.get('acceptance_criteria', [])}\n"
                f"    依赖: {sp.get('dependencies', [])}\n"
                f"    来源ID: {sp.get('source_ids', sp.get('source_id', []))}\n"
                f"    必需交付: {sp.get('required_deliverables', sp.get('required_delivery_files', []))}"
            )
        subprojects_text = "\n".join(sp_lines) if sp_lines else "（无子项目信息）"

        # ── 质检缺陷汇总（让 Agent 知道全局问题分布）────────────────────────
        defect_lines = []
        for sp_id, qc in self._qc_results.items():
            if not isinstance(qc, dict):
                continue
            issues = qc.get("issues_detail", [])
            if not isinstance(issues, list):
                issues = []
            if not issues:
                continue
            open_issues = [
                d for d in issues
                if isinstance(d, dict) and d.get("status") in (
                    "open", "needs_manual", "fixing", "pending_verification"
                )
            ]
            if not open_issues:
                continue
            defect_lines.append(f"  [{sp_id}] {qc.get('subproject_name', sp_id)}（{len(open_issues)} 个未解决问题）：")
            for d in open_issues[:5]:
                defect_lines.append(
                    f"    • [{d.get('severity','')}] {d.get('file_path','')} — {d.get('message','')[:80]}"
                )
        defects_summary = "\n".join(defect_lines) if defect_lines else "（暂无未解决的质检问题）"

        self.project_background = (
            f"【项目概述】\n{overview}\n\n"
            f"【核心功能】\n{features_text}\n\n"
            f"【技术栈】{tech_str}\n\n"
            f"【开发阶段（完整）】\n{phases_text}\n\n"
            f"【子项目列表】\n{subprojects_text}\n\n"
            f"【当前质检问题汇总】\n{defects_summary}"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # 二、代码整改（Skill-eng-01/02/07/08）
    # ══════════════════════════════════════════════════════════════════════════

    # ── 内部工具：读取文件内容（供整改时注入上下文）──────────────────────────
    def _resolve_within_workspace(self, file_path: str) -> Path:
        """解析 workspace 内文件路径，禁止绝对路径或 ../ 越界。"""
        workspace = self.workspace.resolve()
        target = Path(file_path)
        if not target.is_absolute():
            target = self.workspace / file_path
        target = target.resolve()
        try:
            target.relative_to(workspace)
        except ValueError:
            raise ValueError(f"路径越界：{file_path}")
        return target

    def _read_file_safe(self, file_path: str) -> str:
        """安全读取文件，路径可以是绝对或相对于 workspace"""
        try:
            target = self._resolve_within_workspace(file_path)
        except ValueError:
            return ""
        if target.exists() and target.is_file():
            try:
                return target.read_text(encoding="utf-8", errors="replace")
            except Exception:
                return ""
        return ""

    @staticmethod
    def _defect_source_window(
        content: str, defect_info: Dict[str, Any], *, radius: int = 60
    ) -> Tuple[str, int, int]:
        """Return a source window centered on the real defect line or symbol."""
        lines = content.splitlines()
        if not lines:
            return "", 0, 0
        raw_line = defect_info.get("line_no", defect_info.get("line"))
        try:
            center: Optional[int] = max(1, min(len(lines), int(raw_line)))
        except (TypeError, ValueError):
            center = None
        symbol = str(
            defect_info.get("symbol") or defect_info.get("symbol_name") or ""
        ).strip()
        if center is None and symbol:
            pattern = re.compile(rf"\b{re.escape(symbol)}\b")
            center = next(
                (index for index, line in enumerate(lines, 1) if pattern.search(line)),
                None,
            )
        if center is None:
            return content, 1, len(lines)
        start = max(1, center - max(1, int(radius)))
        end = min(len(lines), center + max(1, int(radius)))
        return "\n".join(lines[start - 1:end]), start, end

    # ── 内部工具：按文件聚合缺陷单（复用 repair_loop 的聚合策略）──────────
    @staticmethod
    def _group_defects_by_file(defects: List[Dict]) -> Dict[str, List[Dict]]:
        """
        按 file_path 聚合缺陷列表。
        P0（error）排在每个文件的最前面，P1/P2 依次排后。
        返回 {file_path: [defect, ...]}
        """
        file_map: Dict[str, List[Dict]] = {}
        severity_order = {"error": 0, "P0": 0, "warning": 1, "P1": 1, "P2": 2}
        for d in defects:
            key = d.get("file_path") or "（未知文件）"
            file_map.setdefault(key, []).append(d)
        # 每个文件内按严重级别排序
        for key in file_map:
            file_map[key].sort(key=lambda d: severity_order.get(d.get("severity", "P2"), 2))
        return file_map

    def chat_repair(
        self,
        defect_id: str,
        user_input: str,
        defect_info: Optional[Dict] = None,
        all_defects: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        """
        整改对话（每个 defect_id 完全独立上下文）。

        改进策略（对齐阶段质检逻辑）：
        1. 首轮：读取涉及文件的原始内容 → 自动按文件聚合相关缺陷 → 输出逐文件整改方案
        2. 方案格式对齐 RepairBatch.to_instruction()：指明文件、行号、问题、修改范围限制
        3. 对话只生成 proposal；用户必须通过独立 confirm API 明确确认
        4. apply_fix() 用 LLM 精准生成新文件内容（只改问题行，函数名/接口不变）
        """
        history = self.repair_history.setdefault(defect_id, [])

        # ── 构建缺陷上下文（首轮：注入文件原内容 + 聚合同文件其他缺陷）────────
        defect_ctx = ""
        file_content_ctx = ""
        related_defects_ctx = ""

        if defect_info:
            file_path = defect_info.get("file_path", "")
            fix_rounds = defect_info.get("fix_rounds", 0)
            edit_count = self.file_edit_counter.get(
                str((self.workspace / file_path).resolve()) if file_path else "", 0
            )

            defect_ctx = (
                f"\n\n【当前缺陷单】\n"
                f"- ID: {defect_info.get('defect_id', defect_id)}\n"
                f"- 严重级别: {defect_info.get('severity', '')}\n"
                f"- 文件: {file_path}  行号: {defect_info.get('line_no', '未知')}\n"
                f"- 层次: {defect_info.get('layer', '')}\n"
                f"- 问题: {defect_info.get('message', '')}\n"
                f"- 规则: {defect_info.get('rule_id', '')}\n"
                f"- 位置: symbol={defect_info.get('symbol') or defect_info.get('symbol_name') or ''}, "
                f"line={defect_info.get('line_no', defect_info.get('line', ''))}\n"
                f"- 期望: {defect_info.get('expected', '')}\n"
                f"- 实际: {defect_info.get('actual', '')}\n"
                f"- 验收标准: {defect_info.get('acceptance_criteria', '')}\n"
                f"- 证据: {defect_info.get('evidence', '')}\n"
                f"- 原问题复检规范: {defect_info.get('verification_spec', '')}\n"
                f"- 修复建议: {defect_info.get('fix_hint', '')}\n"
                f"- 已自动修复次数: {fix_rounds}（已超过系统上限，需人工判断）\n"
                f"- 全能工程师已修改此文件: {edit_count} 次（上限 {self.FILE_EDIT_LIMIT} 次）"
            )

            # 注入以真实 defect line/symbol 为中心的上下文，附 baseline。
            if file_path and not history:  # 只在首轮注入，避免重复
                raw = self._read_file_safe(file_path)
                if raw:
                    preview, window_start, window_end = self._defect_source_window(
                        raw, defect_info
                    )
                    baseline_sha256 = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                    file_content_ctx = (
                        f"\n\n【文件原始内容（{file_path}，真实窗口 "
                        f"{window_start}-{window_end} 行，"
                        f"baseline_sha256={baseline_sha256}）】\n"
                        f"```\n{preview}\n```"
                    )

            # 聚合同文件的其他 needs_manual 缺陷（复用 _group_defects_by_file 逻辑）
            if all_defects and file_path:
                same_file = [
                    d for d in all_defects
                    if d.get("file_path") == file_path
                    and d.get("id") != defect_info.get("id")
                    and d.get("status") == "needs_manual"
                ]
                if same_file:
                    lines_ctx = [f"\n\n【同文件其他待修缺陷（{len(same_file)} 条，请一并处理）】"]
                    for i, d in enumerate(same_file[:5], 1):
                        lines_ctx.append(
                            f"{i}. [{d.get('severity','')}] 行{d.get('line_no','?')} "
                            f"{d.get('message','')[:80]} — 建议：{d.get('fix_hint','')[:60]}"
                        )
                    related_defects_ctx = "\n".join(lines_ctx)

        # ── 首轮：扫描调用方文件（让 Agent 知道接口影响范围）──────────────
        callers_ctx = ""
        if defect_info and not history:
            file_path = defect_info.get("file_path", "")
            if file_path:
                callers = self._find_callers(file_path)
                if callers:
                    callers_ctx = (
                        f"\n\n【调用此文件的其他文件（共 {len(callers)} 个，修改接口名前必须一并更新）】\n"
                    )
                    for cf, snippet in callers[:6]:
                        callers_ctx += f"  - {cf}\n    引用片段: {snippet[:120]}\n"

        system_prompt = (
            f"{AGENT_PRINCIPLES}\n\n---\n\n"
            "你是全能工程师，已完整了解项目规划、阶段任务、子项目列表和当前质检问题分布。\n"
            "负责处理质检无法自动修复的代码缺陷（needs_manual 级别）。\n\n"
            "【工作流程（Plan Before Execute）】\n"
            "1. 先读取文件原始内容，结合项目背景分析问题根因\n"
            "2. 按文件聚合同文件的所有缺陷，一次性给出该文件的完整修改方案\n"
            "3. 方案必须包含：①涉及文件路径 ②问题位置（行号/函数名） "
            "③修改内容（具体改哪几行） ④为什么这样改 ⑤不会影响哪些接口/函数名\n"
            "4. 输出 proposal 后停止；任何聊天文字都不代表用户授权\n"
            "5. 不得声称方案已确认，确认只能由独立 confirm API 完成\n\n"
            "【整改约束（与阶段修复循环对齐）】\n"
            "- 每次只修改一个文件，并严格遵守服务端从已确认方案生成的 symbol/行范围/diff budget\n"
            "- 函数名、类名、公共接口名不能改（改了会导致其他文件引用报错）\n"
            "- 如果需要改接口名，必须先列出所有引用该接口的文件，确认后统一修改\n"
            "- Immutability：写新的完整文件版本，不打补丁\n"
            "- Security-First：发现安全问题必须主动指出\n"
            "- 不确定直接说不确定，不猜测"
            + defect_ctx + file_content_ctx + related_defects_ctx + callers_ctx
        )
        # 注入完整项目背景（不截断，让 Agent 真正了解项目全貌）
        if self.project_background:
            system_prompt += f"\n\n【项目完整背景（请基于此判断修改影响范围）】\n{self.project_background}"
        system_prompt += self._build_memory_system_prompt()

        messages = [Message(role=MessageRole.SYSTEM, content=system_prompt)]
        for h in history[-10:]:
            role = MessageRole.USER if h.get("role") == "user" else MessageRole.ASSISTANT
            messages.append(Message(role=role, content=h.get("content", "")))
        messages.append(Message(role=MessageRole.USER, content=user_input))

        try:
            resp = self.hermes.chat(messages)
            reply = resp.get("content", "")
        except Exception:
            reply = f"【全能工程师 离线模式】已收到缺陷 {defect_id}，请配置 API Key 获取整改建议。"

        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": reply})

        proposal_digest = ""
        proposal_confirmable = False
        proposal_error = ""
        if defect_info:
            authorization = self._build_fix_authorization(defect_info)
            proposal_error = str(authorization.get("error") or "")
            proposal_confirmable = bool(authorization.get("valid", True))
            normalized_path = str(
                defect_info.get("file_path") or ""
            ).replace("\\", "/").lstrip("./")
            baseline_sha256 = ""
            try:
                target = self._resolve_within_workspace(normalized_path)
                if target.is_file():
                    baseline_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
            except (OSError, ValueError):
                proposal_confirmable = False
                proposal_error = "缺陷文件不存在或路径不安全"
            proposal_payload = {
                "defect_id": defect_id,
                "file_path": normalized_path,
                "issue_version": str(
                    defect_info.get("observation_id")
                    or defect_info.get("fingerprint")
                    or ""
                ),
                "baseline_sha256": baseline_sha256,
                "reply": reply,
                "authorization": authorization,
            }
            proposal_digest = hashlib.sha256(
                json.dumps(
                    proposal_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            self.pending_fix_proposals[proposal_digest] = {
                **proposal_payload,
                "proposal_digest": proposal_digest,
                "confirmable": proposal_confirmable,
                "proposal_error": proposal_error,
                "created_at": time.time(),
            }

        return {
            "reply": reply,
            "defect_id": defect_id,
            "confirmed": False,
            "proposal_digest": proposal_digest,
            "proposal_confirmable": proposal_confirmable,
            "proposal_error": proposal_error,
            "requires_whole_file_authorization": bool(
                defect_info
                and self.pending_fix_proposals.get(proposal_digest, {})
                    .get("authorization", {})
                    .get("mode") == "whole_file"
            ),
            "success": True,
        }

    def confirm_fix_proposal(
        self,
        *,
        defect_id: str,
        proposal_digest: str,
        issue_version: str,
        file_path: str,
        baseline_sha256: str,
        allow_whole_file: bool = False,
    ) -> Dict[str, Any]:
        proposal = self.pending_fix_proposals.get(str(proposal_digest or ""))
        if not proposal or proposal.get("defect_id") != defect_id:
            return {"success": False, "error": "整改 proposal 不存在或不属于该缺陷"}
        if not proposal.get("confirmable", False):
            return {
                "success": False,
                "error": proposal.get("proposal_error") or "整改 proposal 缺少安全变更范围",
            }
        expected_path = str(proposal.get("file_path") or "").replace("\\", "/").lstrip("./")
        actual_path = str(file_path or "").replace("\\", "/").lstrip("./")
        if expected_path != actual_path:
            return {"success": False, "error": "proposal 绑定文件与 canonical defect 不匹配"}
        if str(proposal.get("issue_version") or "") != str(issue_version or ""):
            return {"success": False, "error": "proposal 绑定的 observation 已过期"}
        if str(proposal.get("baseline_sha256") or "") != str(baseline_sha256 or ""):
            return {"success": False, "error": "proposal 绑定的文件 baseline 已变化"}
        authorization = dict(proposal.get("authorization") or {})
        if authorization.get("mode") == "whole_file" and not allow_whole_file:
            return {
                "success": False,
                "error": "whole-file 整改必须显式确认高风险授权",
            }
        confirmation_payload = {
            **proposal,
            "allow_whole_file": bool(allow_whole_file),
        }
        plan_version = hashlib.sha256(
            json.dumps(
                confirmation_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        self.confirmed_fix_plans[defect_id] = {
            **confirmation_payload,
            "version": plan_version,
            "confirmation_digest": plan_version,
            "confirmation_generation": plan_version,
            "target_baseline_file_sha256": baseline_sha256,
            "scope": dict(authorization),
            "confirmed_at": time.time(),
        }
        self.add_project_memory(
            f"整改方案已独立确认：{expected_path} — proposal {proposal_digest[:12]}",
            memory_type="issue",
        )
        return {
            "success": True,
            "defect_id": defect_id,
            "confirmed_plan_version": plan_version,
            "baseline_sha256": baseline_sha256,
        }

    def validate_confirmed_fix_plan(
        self,
        defect_id: str,
        file_path: str,
        plan_version: str,
        issue_version: str = "",
    ) -> Tuple[bool, str]:
        """Validate the exact server-issued plan before a workspace mutation."""
        record = self.confirmed_fix_plans.get(defect_id)
        if not record:
            return False, "缺陷尚无已确认的整改方案"
        if not plan_version or plan_version != record.get("version"):
            return False, "整改方案版本缺失、过期或不匹配"
        expected = str(record.get("file_path") or "").replace("\\", "/").lstrip("./")
        actual = str(file_path or "").replace("\\", "/").lstrip("./")
        if expected != actual:
            return False, "整改方案绑定文件与请求文件不匹配"
        if str(record.get("issue_version") or "") != str(issue_version or ""):
            return False, "整改方案绑定的缺陷快照已变化，请重新确认方案"
        return True, ""

    def get_confirmed_fix_plan(
        self, defect_id: str, plan_version: str
    ) -> Dict[str, Any]:
        record = self.confirmed_fix_plans.get(defect_id) or {}
        if not plan_version or plan_version != record.get("version"):
            return {}
        return dict(record)

    def get_confirmed_fix_authorization(
        self, defect_id: str, plan_version: str
    ) -> Dict[str, Any]:
        """Return only the authorization bound into the confirmed plan hash."""
        record = self.confirmed_fix_plans.get(defect_id) or {}
        if not plan_version or plan_version != record.get("version"):
            return {}
        authorization = record.get("authorization")
        return dict(authorization) if isinstance(authorization, dict) else {}

    @staticmethod
    def _build_fix_authorization(defect_info: Dict[str, Any]) -> Dict[str, Any]:
        """Derive a server-owned mutation scope from the canonical defect."""
        symbol = str(
            defect_info.get("symbol") or defect_info.get("symbol_name") or ""
        ).strip()
        location = str(defect_info.get("location") or "").strip()
        if not symbol and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", location):
            symbol = location
        raw_start = defect_info.get("line_no", defect_info.get("line"))
        raw_end = defect_info.get("end_line", defect_info.get("line_end", raw_start))
        try:
            line_start = max(1, int(raw_start)) if raw_start is not None else None
            line_end = max(line_start or 1, int(raw_end)) if raw_end is not None else line_start
        except (TypeError, ValueError):
            line_start = None
            line_end = None
        requested_budget = defect_info.get("repair_diff_budget")
        try:
            diff_budget = max(1, int(requested_budget)) if requested_budget is not None else None
        except (TypeError, ValueError):
            diff_budget = None
        if diff_budget is None and line_start is not None and line_end is not None:
            # The budget is derived from the confirmed location span, not file
            # length or a global "completion" proxy.
            diff_budget = max(1, (line_end - line_start + 1) * 3)
        return {
            "mode": "scoped" if symbol or line_start is not None else "whole_file",
            "symbol": symbol,
            "line_start": line_start,
            "line_end": line_end,
            "diff_budget": diff_budget,
            "valid": not (
                Path(str(defect_info.get("file_path") or "")).suffix.lower()
                in {".js", ".jsx", ".ts", ".tsx"}
                and bool(symbol)
                and line_start is None
                and diff_budget is None
            ),
            "error": (
                "JS/TS symbol 缺陷必须提供 line range 或 diff budget 后才能确认"
                if (
                    Path(str(defect_info.get("file_path") or "")).suffix.lower()
                    in {".js", ".jsx", ".ts", ".tsx"}
                    and bool(symbol)
                    and line_start is None
                    and diff_budget is None
                )
                else ""
            ),
        }

    def prepare_fix_plan(
        self,
        defect_id: str,
        file_path: str,
        new_content: str,
        summary: str,
    ) -> Dict[str, Any]:
        """暂存整改方案，等待用户确认后才写文件。"""
        self.pending_fixes[defect_id] = {
            "defect_id": defect_id,
            "file_path": file_path,
            "new_content": new_content,
            "summary": summary,
            "prepared_at": time.time(),
        }
        return {"success": True, "defect_id": defect_id, "summary": summary}

    def apply_fix(
        self,
        defect_id: str,
        file_path: str,
        new_content: str,
        run_qa: bool = True,
        defect_info: Optional[Dict] = None,
        record_memory: bool = True,
        fix_authorization: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        执行写文件（Immutability：新版本替换旧文件）。

        改进策略（对齐阶段修复循环）：
        1. 先读取原文件内容，用 LLM 精准生成「只改问题行」的新文件版本
           （若调用方已提供 new_content，则跳过 LLM 生成，直接使用）
        2. 写前做 layer1+2 静态验证，验证不通过则拒绝写入（防止越改越坏）
        3. 记录修改次数（每文件 ≤ FILE_EDIT_LIMIT 次），超限记录在状态里
        4. 每次写入前备份旧文件（带次数后缀，保留完整历史）
        """
        try:
            target = self._resolve_within_workspace(file_path)
        except ValueError as exc:
            return {"success": False, "defect_id": defect_id, "error": str(exc)}

        rel_key = str(target.resolve())
        edit_count = self.file_edit_counter.get(rel_key, 0)
        exceeded = edit_count >= self.FILE_EDIT_LIMIT

        # ── 如果调用方传了 new_content=""，尝试用 LLM 精准生成 ──────────────
        final_content = new_content
        llm_generated = False
        if not new_content.strip() and defect_info and target.exists():
            old_content = target.read_text(encoding="utf-8", errors="replace")
            final_content = self._llm_generate_fixed_content(
                file_path=file_path,
                old_content=old_content,
                defect_info=defect_info,
                repair_history=self.repair_history.get(defect_id, []),
                confirmed_plan=str(
                    (self.confirmed_fix_plans.get(defect_id) or {}).get("reply") or ""
                ),
            )
            llm_generated = True

        if not final_content.strip():
            return {"success": False, "defect_id": defect_id,
                    "error": "整改内容为空，请先在对话中确认整改方案"}

        old_content = (
            target.read_text(encoding="utf-8", errors="replace")
            if target.exists()
            else ""
        )
        contract_check = self._validate_replacement_contract(
            str(target), old_content, final_content, fix_authorization=fix_authorization
        )
        if not contract_check["passed"]:
            return {
                "success": False,
                "defect_id": defect_id,
                "file_path": str(target),
                "preflight_failed": True,
                "qa_result": contract_check,
                "edit_count": edit_count,
                "message": f"完整文件/最小变更校验失败：{contract_check['issues']}",
            }

        # ── 写前静态验证（防止越改越坏）───────────────────────────────────────
        pre_qa = self._quick_static_check(str(target), final_content)
        if not pre_qa.get("passed", True):
            # 有语法错误，直接拒绝写入
            return {
                "success": False,
                "defect_id": defect_id,
                "file_path": str(target),
                "preflight_failed": True,
                "qa_result": pre_qa,
                "edit_count": edit_count,
                "message": f"新内容存在语法错误，写入被拒绝：{pre_qa.get('issues', [])}",
            }

        # ── 对比新旧得分（超过上限后额外检查）──────────────────────────────────
        if exceeded and target.exists():
            old_content = target.read_text(encoding="utf-8", errors="replace")
            old_qa = self._quick_static_check(str(target), old_content)
            if pre_qa.get("score", 0) < old_qa.get("score", 0):
                return {
                    "success": False,
                    "defect_id": defect_id,
                    "file_path": str(target),
                    "preflight_failed": True,
                    "qa_result": pre_qa,
                    "edit_count": edit_count,
                    "message": (
                        f"新内容得分（{pre_qa.get('score',0)}）低于旧版本（{old_qa.get('score',0)}），"
                        f"已超过 {self.FILE_EDIT_LIMIT} 次修改上限且质量下降，写入被拒绝"
                    ),
                }

        backup_path: Optional[str] = None
        try:
            # 备份旧文件（带次数后缀）
            if target.exists():
                relative = target.relative_to(self.workspace.resolve())
                backup = (
                    self.workspace.resolve()
                    / ".project"
                    / "backups"
                    / relative.parent
                    / f"{relative.name}.bak{edit_count + 1}"
                )
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(target.read_bytes())
                backup_path = str(backup)

            self._atomic_write_text(target, final_content)

            # 更新修改计数器
            self.file_edit_counter[rel_key] = edit_count + 1
            self.file_last_score[rel_key] = pre_qa.get("score", 0)

            self.pending_fixes.pop(defect_id, None)

            if record_memory:
                self.add_project_memory(
                    f"[第{edit_count+1}次整改{'⚠️超限' if exceeded else ''}] "
                    f"{target.name} — {(defect_info or {}).get('message','')[:60]}",
                    memory_type="issue",
                )

            return {
                "success": True,
                "defect_id": defect_id,
                "file_path": str(target),
                "backup_path": backup_path,
                "qa_result": pre_qa,
                "replacement_validation": contract_check,
                "llm_generated": llm_generated,
                "edit_count": edit_count + 1,
                "edit_limit": self.FILE_EDIT_LIMIT,
                "exceeded": exceeded,
                "message": (
                    f"整改完成（第 {edit_count+1} 次）：{target.name}"
                    + (f" ⚠️ 已达 {self.FILE_EDIT_LIMIT} 次上限，建议重新梳理根因" if exceeded else "")
                ),
            }
        except Exception as e:
            return {"success": False, "defect_id": defect_id, "error": str(e)}

    def _llm_generate_fixed_content(
        self,
        file_path: str,
        old_content: str,
        defect_info: Dict,
        repair_history: List[Dict],
        confirmed_plan: str = "",
    ) -> str:
        """
        用 LLM 精准生成修复后的完整文件内容。
        策略与阶段修复循环对齐：
        - 完整引用原始缺陷单（禁止转述）
        - 修改范围必须服从已确认方案绑定的 symbol/行范围/diff budget
        - 函数名/类名/公共接口不变
        - 输出完整文件内容（Immutability 原则）
        """
        system_prompt = (
            "你是全能工程师，负责精准修复代码缺陷。\n\n"
            "【修复约束（必须严格遵守）】\n"
            "- 只修改已确认方案中的 symbol/行范围，其余字节保持不变\n"
            "- 函数名、类名、公共接口名不能改（改了会导致其他文件引用报错）\n"
            "- 只修问题所在的最小必要范围\n"
            "- 输出完整文件内容（包含所有未修改的部分）\n"
            "- 只输出文件内容，不输出任何解释文字\n\n"
            "【缺陷信息（原始，禁止转述）】\n"
            f"- 文件：{file_path}\n"
            f"- 行号：{defect_info.get('line_no', '未知')}\n"
            f"- 层次：{defect_info.get('layer', '')}\n"
            f"- 问题：{defect_info.get('message', '')}\n"
            f"- 修复建议：{defect_info.get('fix_hint', '')}\n"
        )
        if confirmed_plan:
            system_prompt += f"\n【已确认的整改方案】\n{confirmed_plan[:600]}\n"
        if self.project_background:
            system_prompt += f"\n【项目背景（用于保证接口一致性）】\n{self.project_background[:300]}\n"

        messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(
                role=MessageRole.USER,
                content=(
                    f"原始文件内容（完整）：\n```\n{old_content}\n```\n\n"
                    "请输出修复后的完整文件内容（只改问题行，其余保持不变）："
                ),
            ),
        ]
        try:
            resp = self.hermes.chat(messages)
            content = resp.get("content", "").strip()
            # 去掉 LLM 可能包的 markdown 代码块
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            return content
        except Exception:
            return ""

    @staticmethod
    def _atomic_write_text(target: Path, content: str) -> None:
        """Replace a delivery file atomically; readers see old or complete new bytes."""
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=target.parent, prefix=f".{target.name}.",
                suffix=".tmp", delete=False
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(content.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    @staticmethod
    def _python_public_symbols(content: str) -> set[str]:
        tree = ast.parse(content)
        return {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and not node.name.startswith("_")
        }

    @staticmethod
    def _python_symbol_range(content: str, symbol: str) -> Optional[Tuple[int, int]]:
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ) and node.name == symbol:
                return int(node.lineno), int(getattr(node, "end_lineno", node.lineno))
        return None

    def _validate_replacement_contract(
        self,
        file_path: str,
        old_content: str,
        new_content: str,
        *,
        fix_authorization: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Validate full-file integrity and the exact confirmed diff scope."""
        issues: List[str] = []
        warnings: List[str] = []
        old_lines = old_content.splitlines()
        new_lines = new_content.splitlines()
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        changed_opcodes = [
            (tag, i1, i2, j1, j2)
            for tag, i1, i2, j1, j2 in matcher.get_opcodes()
            if tag != "equal"
        ]
        changed_lines = sum(
            max(i2 - i1, j2 - j1) for _, i1, i2, j1, j2 in changed_opcodes
        )
        if old_lines and len(old_lines) >= 40 and len(new_lines) < len(old_lines) // 2:
            issues.append(
                f"疑似截断：原文件 {len(old_lines)} 行，新文件仅 {len(new_lines)} 行"
            )
        authorization = dict(fix_authorization or {})
        authorized_start = authorization.get("line_start")
        authorized_end = authorization.get("line_end")
        symbol = str(authorization.get("symbol") or "").strip()
        if symbol and Path(file_path).suffix.lower() == ".py" and old_content:
            try:
                symbol_range = self._python_symbol_range(old_content, symbol)
            except SyntaxError as exc:
                symbol_range = None
                issues.append(f"授权 symbol 无法从原 Python 文件解析：{exc}")
            if symbol_range is None and not any("无法从原 Python" in item for item in issues):
                issues.append(f"已确认方案绑定的 symbol 不存在：{symbol}")
            elif symbol_range is not None and authorized_start is None:
                authorized_start, authorized_end = symbol_range
        if authorized_start is not None:
            authorized_start = int(authorized_start)
            authorized_end = int(authorized_end or authorized_start)
            outside_scope = []
            for _, i1, i2, _, _ in changed_opcodes:
                first_line = i1 + 1
                last_line = max(first_line, i2)
                if first_line < authorized_start or last_line > authorized_end:
                    outside_scope.append((first_line, last_line))
            if outside_scope:
                issues.append(
                    "变更超出已确认的行范围 "
                    f"{authorized_start}-{authorized_end}：{outside_scope}"
                )
        diff_budget = authorization.get("diff_budget")
        if diff_budget is not None and changed_lines > int(diff_budget):
            issues.append(
                f"变更 {changed_lines} 行，超过已确认方案授权的 {int(diff_budget)} 行"
            )
        if old_lines and changed_lines / max(1, len(old_lines)) > 0.35:
            warnings.append(
                f"变更覆盖原文件 {changed_lines}/{len(old_lines)} 行，需由 authoritative QA 复检"
            )
        if Path(file_path).suffix.lower() == ".py" and old_content:
            try:
                removed = self._python_public_symbols(old_content) - self._python_public_symbols(new_content)
                if removed:
                    issues.append(f"公共接口被删除或重命名：{', '.join(sorted(removed))}")
            except SyntaxError as exc:
                issues.append(f"Python 接口校验失败：{exc}")
        return {
            "passed": not issues,
            "issues": issues,
            "warnings": warnings,
            "old_line_count": len(old_lines),
            "new_line_count": len(new_lines),
            "changed_line_count": changed_lines,
            "authorized_line_start": authorized_start,
            "authorized_line_end": authorized_end,
            "authorized_diff_budget": diff_budget,
        }

    def get_file_edit_stats(self) -> Dict[str, Any]:
        """返回所有文件的修改次数统计，供前端展示"""
        stats = []
        for rel_key, count in self.file_edit_counter.items():
            try:
                rel_path = str(Path(rel_key).relative_to(self.workspace.resolve()))
            except ValueError:
                rel_path = rel_key
            stats.append({
                "file_path": rel_path.replace("\\", "/"),
                "edit_count": count,
                "last_score": self.file_last_score.get(rel_key, 0),
                "exceeded": count >= self.FILE_EDIT_LIMIT,
            })
        total = len(stats)
        avg = round(sum(s["edit_count"] for s in stats) / total, 2) if total else 0
        return {
            "files": sorted(stats, key=lambda s: s["edit_count"], reverse=True),
            "total_files_edited": total,
            "avg_edit_count": avg,
            "limit": self.FILE_EDIT_LIMIT,
            "files_exceeded": sum(1 for s in stats if s["exceeded"]),
        }

    def _quick_static_check(self, file_path: str, content: str) -> Dict[str, Any]:
        """layer1 + layer2 静态验证（整改后立即运行）"""
        from .quality_agents import check_layer1_syntax, check_layer2_logic
        files = [(file_path, content)]
        l1 = check_layer1_syntax(files)
        l2 = check_layer2_logic(files)
        passed = l1["passed"] and l2["passed"]
        issues = l1["issues"] + l2["issues"]
        return {
            "passed": passed,
            "score": (l1["score"] + l2["score"]) // 2,
            "issues": issues,
            "layer1": l1,
            "layer2": l2,
        }

    def _find_callers(self, file_path: str, max_callers: int = 10) -> List[Tuple[str, str]]:
        """
        扫描 workspace/src 下所有源码文件，找出 import 或 require 了 file_path 的文件。
        返回 [(caller_rel_path, import_snippet), ...]
        只扫描 .py / .ts / .tsx / .js / .jsx 文件，跳过无用目录。
        """
        target_name = Path(file_path).stem  # 不含扩展名的模块名
        target_name_lower = target_name.lower()
        callers: List[Tuple[str, str]] = []

        skip_dirs = {"__pycache__", ".git", "node_modules", ".pytest_cache", "dist", "build"}
        workspace = self.workspace.resolve()
        src_root = workspace / "src"
        scan_root = src_root if src_root.exists() else self.workspace

        try:
            for p in sorted(scan_root.rglob("*")):
                if not p.is_file():
                    continue
                # 跳过无用目录
                if any(skip in p.parts for skip in skip_dirs):
                    continue
                # 只扫描源码文件
                if p.suffix not in {".py", ".ts", ".tsx", ".js", ".jsx"}:
                    continue
                # 跳过自身
                try:
                    resolved_path = p.resolve()
                    resolved_path.relative_to(workspace)
                    if resolved_path == self._resolve_within_workspace(file_path):
                        continue
                except Exception:
                    continue

                try:
                    text = resolved_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue

                # 搜索 import/from/require 行中是否包含目标模块名
                matched_lines = []
                for line in text.splitlines():
                    line_lower = line.lower().strip()
                    if not line_lower:
                        continue
                    if any(kw in line_lower for kw in ("import", "from", "require")):
                        if target_name_lower in line_lower:
                            matched_lines.append(line.strip())
                            if len(matched_lines) >= 2:
                                break

                if matched_lines:
                    try:
                        rel = str(resolved_path.relative_to(workspace)).replace("\\", "/")
                    except ValueError:
                        rel = str(p)
                    snippet = " | ".join(matched_lines[:2])
                    callers.append((rel, snippet))
                    if len(callers) >= max_callers:
                        break
        except Exception:
            pass

        return callers

    # ══════════════════════════════════════════════════════════════════════════
    # 三、使用手册生成（Skill-eng-03）
    # ══════════════════════════════════════════════════════════════════════════

    def generate_manual(
        self,
        manual_type: str = "user",
        extra_instruction: str = "",
    ) -> Dict[str, Any]:
        """
        生成使用手册并保存到 workspace/docs/。
        manual_type: user（用户手册）/ api（API文档）/ deploy（部署手册）
        """
        type_labels = {
            "user": "用户使用手册",
            "api": "API 接口文档",
            "deploy": "部署运维手册",
        }
        label = type_labels.get(manual_type, "使用手册")

        # 扫描 workspace 文件结构
        file_tree = self._scan_file_tree()

        system_prompt = (
            f"{AGENT_PRINCIPLES}\n\n---\n\n"
            f"你是全能工程师，请根据项目信息生成完整的【{label}】。\n\n"
            "【输出要求】\n"
            "- 使用 Markdown 格式，结构清晰，层次分明\n"
            "- 中文撰写，语言专业但易懂\n"
            "- 包含目录、各章节内容完整\n"
            "- 代码示例使用 ``` 代码块\n"
            "- 不要输出占位符，所有内容必须有实际内容\n"
            f"- 手册类型：{label}\n"
        )
        if extra_instruction:
            system_prompt += f"\n【额外要求】{extra_instruction}\n"

        user_content = (
            f"{self.project_background}\n\n"
            f"【项目文件结构】\n{file_tree[:1500]}\n\n"
            f"请生成完整的{label}。"
        )

        try:
            messages = [
                Message(role=MessageRole.SYSTEM, content=system_prompt),
                Message(role=MessageRole.USER, content=user_content),
            ]
            resp = self.hermes.chat(messages)
            manual_content = resp.get("content", "")
        except Exception as e:
            manual_content = f"# {label}\n\n> 生成失败：{e}\n\n请配置 API Key 后重试。"

        # 保存到 docs/
        docs_dir = self.workspace / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        filename_map = {"user": "USER_MANUAL.md", "api": "API_DOCS.md", "deploy": "DEPLOY.md"}
        filename = filename_map.get(manual_type, "MANUAL.md")
        output_path = docs_dir / filename
        self._atomic_write_text(output_path, manual_content)

        return {
            "success": True,
            "manual_type": manual_type,
            "label": label,
            "content": manual_content,
            "file_path": str(output_path),
            "char_count": len(manual_content),
        }

    def _scan_file_tree(self, max_files: int = 80) -> str:
        """扫描 workspace 生成文件树摘要"""
        lines = []
        count = 0
        for p in sorted(self.workspace.rglob("*")):
            if count >= max_files:
                lines.append("... (更多文件已省略)")
                break
            rel = p.relative_to(self.workspace)
            parts = rel.parts
            # 跳过无用目录
            if any(skip in parts for skip in ["__pycache__", ".git", "node_modules", ".pytest_cache"]):
                continue
            indent = "  " * (len(parts) - 1)
            if p.is_file():
                lines.append(f"{indent}{'└─ ' if p.is_file() else '├─ '}{p.name}")
                count += 1
        return "\n".join(lines) if lines else "(工作区为空)"

    # ══════════════════════════════════════════════════════════════════════════
    # 四、文件归档（Skill-eng-04/06）
    # ══════════════════════════════════════════════════════════════════════════

    def archive_files(
        self,
        phase_registry: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        扫描 workspace，对每个文件进行分类和阶段标注。
        phase_registry: {file_path → phase_id}，来自 PhaseManager.file_registry
        返回归档结果，包含分类统计和详细列表。
        """
        phase_reg = phase_registry or {}
        results: List[Dict] = []
        stats: Dict[str, int] = {"source": 0, "test": 0, "doc": 0, "config": 0, "output": 0, "useless": 0}

        for p in sorted(self.workspace.rglob("*")):
            if not p.is_file():
                continue
            try:
                rel = str(p.relative_to(self.workspace)).replace("\\", "/")
            except ValueError:
                rel = str(p)

            category, is_useless = _classify_file(rel)
            size = p.stat().st_size
            phase_id = phase_reg.get(rel) or phase_reg.get(str(p), "")

            # 查找阶段名称
            phase_name = ""
            if phase_id and self.phase_info:
                ph = next((ph for ph in self.phase_info if ph.get("phase_id") == phase_id), None)
                if ph:
                    phase_name = ph.get("name", "")

            entry = {
                "file_path": rel,
                "abs_path": str(p),
                "category": "useless" if is_useless else category,
                "is_useless": is_useless,
                "phase_id": phase_id,
                "phase_name": phase_name,
                "size_bytes": size,
                "extension": p.suffix,
                "modified_at": p.stat().st_mtime,
            }
            results.append(entry)

            if is_useless:
                stats["useless"] += 1
            else:
                stats[category] = stats.get(category, 0) + 1

        self.archive_result = {
            "files": results,
            "stats": stats,
            "total": len(results),
            "scanned_at": time.time(),
            "workspace": str(self.workspace),
        }
        return self.archive_result

    def get_archive_result(self) -> Optional[Dict]:
        return self.archive_result

    # ══════════════════════════════════════════════════════════════════════════
    # 五、项目问答（Skill-eng-05）— 独立上下文
    # ══════════════════════════════════════════════════════════════════════════

    def chat_qa(self, user_input: str) -> Dict[str, Any]:
        """
        项目问答对话，上下文与整改/文档完全隔离。
        RAG 式：把项目背景 + 文件结构注入 system prompt，回答用户问题。
        """
        system_prompt = (
            f"{AGENT_PRINCIPLES}\n\n---\n\n"
            "你是全能工程师，负责回答用户关于当前项目的任意问题。\n\n"
            "【工作规范】\n"
            "- 基于项目背景信息回答，不猜测，不编造\n"
            "- 不确定的内容直接说不确定，并告知用户可以查看哪个文件\n"
            "- 回答简洁准确，引用具体文件/模块/阶段名称\n"
            "- 可以回答：技术方案、功能说明、文件位置、阶段规划、API 用法等\n"
        )
        if self.project_background:
            system_prompt += f"\n\n【项目背景】\n{self.project_background}"
        # 注入个人长期记忆 + 项目记忆（与整改上下文完全隔离）
        system_prompt += self._build_memory_system_prompt()

        messages = [Message(role=MessageRole.SYSTEM, content=system_prompt)]
        for h in self.qa_history[-12:]:
            role = MessageRole.USER if h.get("role") == "user" else MessageRole.ASSISTANT
            messages.append(Message(role=role, content=h.get("content", "")))
        messages.append(Message(role=MessageRole.USER, content=user_input))

        try:
            resp = self.hermes.chat(messages)
            reply = resp.get("content", "")
        except Exception:
            reply = "【离线模式】请配置 API Key 后使用项目问答功能。"

        self.qa_history.append({"role": "user", "content": user_input})
        self.qa_history.append({"role": "assistant", "content": reply})

        # 写入项目记忆：记录用户关心的问题
        if len(user_input) > 10:
            self.add_project_memory(
                f"用户问答：{user_input[:100]}",
                memory_type="context",
            )

        return {"reply": reply, "success": True}

    # ══════════════════════════════════════════════════════════════════════════
    # 六、质检触发（Skill-eng-07）
    # ══════════════════════════════════════════════════════════════════════════

    def run_qa_inspection(
        self,
        subproject_id: str,
        output_files: Optional[List[str]] = None,
        subproject_description: str = "",
        subproject_name: str = "",
        is_final_phase: bool = False,
    ) -> Dict[str, Any]:
        """
        触发 QAAgent 对指定子项目进行质检（整改后复检用）。
        """
        from .quality_agents import QAAgent
        qa = QAAgent(hermes_client=self.hermes)
        return qa.inspect(
            subproject_id=subproject_id,
            workspace_path=str(self.workspace),
            output_files=output_files,
            subproject_description=subproject_description,
            subproject_name=subproject_name,
            is_final_phase=is_final_phase,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # 七、持久化
    # ══════════════════════════════════════════════════════════════════════════

    def to_persist(self) -> Dict:
        return {
            "project_id": self.project_id,
            "workspace": str(self.workspace),
            "project_background": self.project_background,
            "final_plan": self.final_plan,
            "phase_info": self.phase_info,
            "qa_history": self.qa_history[-30:],
            "doc_history": self.doc_history[-20:],
            # repair_history 以 defect_id 为 key，只保留最近 20 条/每缺陷
            "repair_history": {
                did: hist[-20:]
                for did, hist in self.repair_history.items()
            },
            "pending_fix_proposals": self.pending_fix_proposals,
            "confirmed_fix_plans": self.confirmed_fix_plans,
            "archive_result": self.archive_result,
        }

    def from_persist(self, data: Dict) -> None:
        self.project_id = data.get("project_id", self.project_id)
        if data.get("workspace"):
            self.workspace = Path(data["workspace"])
        self.project_background = data.get("project_background", "")
        self.final_plan = data.get("final_plan")
        self.phase_info = data.get("phase_info", [])
        self.qa_history = data.get("qa_history", [])
        self.doc_history = data.get("doc_history", [])
        self.repair_history = data.get("repair_history", {})
        pending = data.get("pending_fix_proposals", {})
        confirmed = data.get("confirmed_fix_plans", {})
        self.pending_fix_proposals = {
            str(digest): dict(record)
            for digest, record in pending.items()
            if (
                isinstance(digest, str)
                and isinstance(record, dict)
                and record.get("proposal_digest") == digest
            )
        } if isinstance(pending, dict) else {}
        self.confirmed_fix_plans = {
            str(defect_id): dict(record)
            for defect_id, record in confirmed.items()
            if (
                isinstance(defect_id, str)
                and isinstance(record, dict)
                and record.get("version")
                and record.get("confirmation_digest") == record.get("version")
            )
        } if isinstance(confirmed, dict) else {}
        self.archive_result = data.get("archive_result")

    # ══════════════════════════════════════════════════════════════════════════
    # 八、AgentBase 接口
    # ══════════════════════════════════════════════════════════════════════════

    def _do_execute(self, task: Task) -> Any:
        title = task.title
        meta = task.metadata or {}
        if title == "chat_repair":
            return self.chat_repair(
                defect_id=meta.get("defect_id", ""),
                user_input=task.description,
                defect_info=meta.get("defect_info"),
            )
        elif title == "apply_fix":
            return self.apply_fix(
                defect_id=meta.get("defect_id", ""),
                file_path=meta.get("file_path", ""),
                new_content=meta.get("new_content", ""),
                run_qa=meta.get("run_qa", True),
            )
        elif title == "generate_manual":
            return self.generate_manual(
                manual_type=meta.get("manual_type", "user"),
                extra_instruction=meta.get("extra_instruction", ""),
            )
        elif title == "archive_files":
            return self.archive_files(phase_registry=meta.get("phase_registry"))
        elif title == "chat_qa":
            return self.chat_qa(user_input=task.description)
        elif title == "run_qa":
            return self.run_qa_inspection(
                subproject_id=meta.get("subproject_id", ""),
                output_files=meta.get("output_files"),
                subproject_description=meta.get("description", ""),
                subproject_name=meta.get("subproject_name", ""),
                is_final_phase=meta.get("is_final_phase", False),
            )
        return {"error": f"Unknown task: {title}"}

    # ══════════════════════════════════════════════════════════════════════════
    # 九、专家池记忆体系（个人长期记忆 + 项目隔离记忆）
    # ══════════════════════════════════════════════════════════════════════════

    def _get_expert_pool(self) -> Optional[Any]:
        """获取专家池单例（懒加载，不可用时返回 None）"""
        if not _EXPERT_POOL_AVAILABLE:
            return None
        try:
            return _get_ep()
        except Exception:
            return None

    def _ensure_expert_profile(self) -> None:
        """确保全能工程师在专家池中有档案（首次调用时自动注册）"""
        pool = self._get_expert_pool()
        if not pool:
            return
        if pool.get_expert(ENGINEER_EXPERT_ID):
            return
        try:
            profile = ExpertProfile(
                expert_id=ENGINEER_EXPERT_ID,
                name="全能工程师",
                role="全能工程师",
                agent_type="pg",
                avatar="🛠",
                role_description=(
                    "全能工程师，负责项目收尾阶段的人工整改、使用手册撰写、"
                    "文件归档分类和项目问答服务。遵守 Plan Before Execute / Immutability / Security-First 原则。"
                ),
                working_style="严谨、系统、以结果为导向，确认方案再执行",
                communication_style="简洁专业，引用具体文件和模块名称",
                decision_style="先规划后执行，不确定直接提问",
                domains=["代码审查", "技术文档", "软件工程", "项目管理", "质量保证"],
                skills=[
                    ExpertSkill(name="代码静态分析", level="expert"),
                    ExpertSkill(name="代码整改执行", level="expert"),
                    ExpertSkill(name="使用手册生成", level="expert"),
                    ExpertSkill(name="文件归档分类", level="expert"),
                    ExpertSkill(name="项目问答服务", level="expert"),
                    ExpertSkill(name="质检验证",     level="advanced"),
                    ExpertSkill(name="Defect管理",   level="advanced"),
                    ExpertSkill(name="阶段感知",     level="advanced"),
                    ExpertSkill(name="任务规划接收", level="advanced"),
                ],
                behavior_rules=[
                    "Plan Before Execute：先给出完整方案，等用户确认后才写文件",
                    "Immutability：有错误写新的完整版本替换，不打补丁",
                    "Security-First：发现安全问题（SQL注入/密钥泄露等）必须主动指出",
                    "Agent-First：任务三要素不明确直接追问，不猜测执行",
                    "Test-Driven：修改后的代码必须通过静态验证再输出",
                    "四套上下文完全隔离：整改/文档/归档/问答互不污染",
                ],
                output_format="Markdown 格式，结构分明，层次清晰，引用具体文件路径",
            )
            pool.create_expert(profile)
        except Exception:
            pass

    def _get_personal_memory_prompt(self) -> str:
        """获取个人长期记忆（跨项目积累的经验）"""
        pool = self._get_expert_pool()
        if not pool:
            return ""
        try:
            profile = pool.get_expert(ENGINEER_EXPERT_ID)
            if profile and profile.long_term_memory:
                return f"\n\n【个人长期记忆（跨项目积累的经验）】\n{profile.long_term_memory[:800]}"
        except Exception:
            pass
        return ""

    def _get_project_memory_prompt(self) -> str:
        """获取当前项目的专属记忆（只在当前项目内有效）"""
        if not self.project_id:
            return ""
        pool = self._get_expert_pool()
        if not pool:
            return ""
        try:
            mem = pool.get_project_memory(ENGINEER_EXPERT_ID, self.project_id)
            entries = mem.entries if hasattr(mem, "entries") else []
            if not entries:
                return ""
            lines = []
            for e in entries[-8:]:  # 最近 8 条
                content = e.get("content", "") if isinstance(e, dict) else getattr(e, "content", "")
                if content:
                    lines.append(f"- {content[:200]}")
            if lines:
                return f"\n\n【当前项目记忆（本项目专属上下文）】\n" + "\n".join(lines)
        except Exception:
            pass
        return ""

    def _build_memory_system_prompt(self) -> str:
        """个人记忆 + 项目记忆合并为 system prompt 附加内容"""
        self._ensure_expert_profile()
        personal = self._get_personal_memory_prompt()
        project = self._get_project_memory_prompt()
        return personal + project

    def add_project_memory(self, content: str, memory_type: str = "context") -> None:
        """向当前项目写入一条记忆（供整改/问答完成后调用）"""
        if not self.project_id:
            return
        pool = self._get_expert_pool()
        if not pool:
            return
        try:
            pool.add_memory_entry(
                expert_id=ENGINEER_EXPERT_ID,
                project_id=self.project_id,
                content=content,
                memory_type=memory_type,
                importance=1.0,
                scope="project",
            )
        except Exception:
            pass

    def get_expert_profile(self) -> Optional[Dict]:
        """获取全能工程师的专家档案（供 API 返回）"""
        pool = self._get_expert_pool()
        if not pool:
            return None
        try:
            profile = pool.get_expert(ENGINEER_EXPERT_ID)
            return profile.to_dict() if profile else None
        except Exception:
            return None

    def get_status(self) -> Dict[str, Any]:
        expert_profile = self.get_expert_profile()
        has_long_term_memory = bool(
            expert_profile and expert_profile.get("long_term_memory")
        ) if expert_profile else False
        return {
            "agent_id": self.agent_id,
            "expert_id": ENGINEER_EXPERT_ID,
            "type": "fullstack_engineer",
            "state": self.state.value,
            "project_id": self.project_id,
            "workspace": str(self.workspace),
            "has_project_background": bool(self.project_background),
            "qa_turns": len(self.qa_history) // 2,
            "repair_defects": len(self.repair_history),
            "archive_done": self.archive_result is not None,
            "has_long_term_memory": has_long_term_memory,
            "expert_pool_available": _EXPERT_POOL_AVAILABLE,
            "skills": ENGINEER_SKILLS,
        }
