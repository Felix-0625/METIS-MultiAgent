"""
Agent 间结构化交接协议 (Handoff Protocol)

设计原则：
- 每次 Agent 交接都携带完整的 HandoffPayload，不丢失原始需求锚点
- Rubric 由 Orchestrator/PM 在生成任务时同步生成，QA Agent 对照逐条打分
- status 字段强制 Agent 表达自身状态，不允许"猜测式"输出
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


# ─── Rubric（验收标准条目）────────────────────────────────────────────────────

class RubricItem(BaseModel):
    """单条验收标准"""
    id: str = Field(default_factory=lambda: f"r-{uuid.uuid4().hex[:6]}")
    criterion: str          # 验收标准描述（一句话，可验证）
    weight: int = 2         # 权重 1-5，越高越重要
    category: str = "functional"  # functional / security / performance / ux


class RubricResult(BaseModel):
    """QA Agent 对单条 Rubric 的打分结果"""
    id: str                 # 对应 RubricItem.id
    passed: bool
    evidence: str = ""      # 证据（代码行号、文件名、截图描述等）
    note: str = ""          # 补充说明


# ─── HandoffPayload（Agent 间交接物）─────────────────────────────────────────

class HandoffPayload(BaseModel):
    """
    Agent 间通用交接协议。

    贯穿全流程的字段：
    - requirements: 原始需求锚点，每个阶段都带着，QA 对照此验收
    - acceptance_criteria: 下一个 Agent 要对照的标准（由当前 Agent 填写）
    - rubric: 由 PM/Orchestrator 生成，QA 对照逐条打分

    状态语义：
    - OK       : 本阶段产出完整，可以交接
    - BLOCKED  : 收到的输入不足以完成任务，需要上游补充信息
    - PARTIAL  : 部分完成，已完成的部分可以交接，未完成部分需说明
    - FAILED   : 执行失败，附带错误原因
    """

    # 元信息
    handoff_id: str = Field(default_factory=lambda: f"ho-{uuid.uuid4().hex[:8]}")
    created_at: float = Field(default_factory=time.time)

    # 流程定位
    phase: str                          # 当前阶段 ID（如 "phase-1"）
    phase_name: str = ""                # 阶段名称（可读）
    task_id: str                        # 任务 ID
    from_agent_id: str = ""             # 发出方 Agent ID
    from_agent_role: str = ""           # 发出方角色
    to_agent_id: str = ""               # 接收方 Agent ID（可为空，由 Orchestrator 路由）
    to_agent_role: str = ""             # 接收方角色

    # 需求锚点（全程不变）
    requirements: List[str] = Field(default_factory=list)
    # 本阶段产出
    deliverable: Dict[str, Any] = Field(default_factory=dict)
    # 下一个 Agent 的验收标准（由当前 Agent 填写）
    acceptance_criteria: List[str] = Field(default_factory=list)
    # 验收 Rubric（由 PM/Orchestrator 生成）
    rubric: List[RubricItem] = Field(default_factory=list)

    # 状态
    status: Literal["OK", "BLOCKED", "PARTIAL", "FAILED"] = "OK"
    blocked_reason: str = ""            # BLOCKED 时必填
    partial_done: List[str] = Field(default_factory=list)   # PARTIAL 时已完成的部分
    partial_todo: List[str] = Field(default_factory=list)   # PARTIAL 时未完成的部分
    error: str = ""                     # FAILED 时必填

    # 重试控制
    retry_count: int = 0
    max_retries: int = 3

    # 附加元数据（自由扩展）
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def can_retry(self) -> bool:
        return self.retry_count < self.max_retries

    def to_context_str(self) -> str:
        """
        生成注入 Agent System Prompt 的上下文字符串。
        让 Agent 知道：原始需求是什么、上一步做了什么、自己要验收什么。
        """
        lines = [
            f"【交接协议 {self.handoff_id}】",
            f"阶段：{self.phase_name or self.phase} | 任务：{self.task_id}",
            "",
            "【原始需求锚点（全程不变，必须对照）】",
        ]
        for i, req in enumerate(self.requirements, 1):
            lines.append(f"  {i}. {req}")

        if self.deliverable:
            lines.append("")
            lines.append("【上一阶段产出摘要】")
            for k, v in self.deliverable.items():
                v_str = str(v)[:200] if not isinstance(v, list) else ", ".join(str(x) for x in v[:5])
                lines.append(f"  {k}: {v_str}")

        if self.acceptance_criteria:
            lines.append("")
            lines.append("【本阶段验收标准（你必须逐条满足）】")
            for i, ac in enumerate(self.acceptance_criteria, 1):
                lines.append(f"  {i}. {ac}")

        if self.rubric:
            lines.append("")
            lines.append("【Rubric 评分标准（QA 将对照此逐条打分）】")
            for item in self.rubric:
                lines.append(f"  [{item.id}] (权重{item.weight}) {item.criterion}")

        if self.status == "BLOCKED":
            lines.append("")
            lines.append(f"⚠️ 上游状态：BLOCKED — {self.blocked_reason}")
        elif self.status == "PARTIAL":
            lines.append("")
            lines.append(f"⚠️ 上游状态：PARTIAL")
            if self.partial_todo:
                lines.append(f"  未完成：{', '.join(self.partial_todo)}")

        return "\n".join(lines)


# ─── QA 打分结果（QA Agent 输出）────────────────────────────────────────────

class QAVerdict(BaseModel):
    """QA Agent 对一次交接物的完整打分结果"""

    handoff_id: str
    task_id: str
    qa_agent_id: str = ""
    evaluated_at: float = Field(default_factory=time.time)

    # 逐条 Rubric 打分
    results: List[RubricResult] = Field(default_factory=list)

    # 汇总
    score: int = 0          # 实际得分（通过的 Rubric 权重之和）
    max_score: int = 0      # 满分（所有 Rubric 权重之和）
    passed: bool = False

    # 可操作的反馈（执行 Agent 收到后知道精确改哪里）
    actionable_feedback: List[str] = Field(default_factory=list)

    # 整体状态
    status: Literal["PASS", "FAIL", "BLOCKED"] = "FAIL"
    blocked_reason: str = ""

    def compute_score(self, rubric: List[RubricItem]) -> None:
        """根据 rubric 和 results 计算得分"""
        rubric_map = {item.id: item for item in rubric}
        self.max_score = sum(item.weight for item in rubric)
        self.score = sum(
            rubric_map[r.id].weight
            for r in self.results
            if r.passed and r.id in rubric_map
        )
        self.passed = self.score >= self.max_score * 0.6  # 60% 通过线
        self.status = "PASS" if self.passed else "FAIL"

    def to_developer_feedback(self) -> str:
        """生成给执行 Agent 的可操作反馈"""
        lines = ["【QA 打分结果】", f"得分：{self.score}/{self.max_score}  状态：{self.status}", ""]
        failed = [r for r in self.results if not r.passed]
        if failed:
            lines.append("❌ 未通过项（必须修复）：")
            for r in failed:
                lines.append(f"  [{r.id}] {r.note or '未满足验收标准'}")
                if r.evidence:
                    lines.append(f"    证据：{r.evidence}")
        if self.actionable_feedback:
            lines.append("")
            lines.append("🔧 修复指令：")
            for fb in self.actionable_feedback:
                lines.append(f"  - {fb}")
        return "\n".join(lines)


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

def make_rubric_from_requirements(requirements: List[str]) -> List[RubricItem]:
    """
    从需求列表快速生成 Rubric（无 LLM 版本，用于降级场景）。
    每条需求对应一条 Rubric，权重默认 2。
    """
    return [
        RubricItem(criterion=req, weight=2)
        for req in requirements
        if req.strip()
    ]


def parse_qa_verdict_from_llm(
    content: str,
    handoff: HandoffPayload,
    qa_agent_id: str = "",
) -> QAVerdict:
    """
    从 LLM 输出中解析 QAVerdict。
    期望 LLM 输出严格 JSON，格式见 QAVerdict schema。
    解析失败时返回 BLOCKED 状态。
    """
    import json as _json
    import re as _re

    verdict = QAVerdict(
        handoff_id=handoff.handoff_id,
        task_id=handoff.task_id,
        qa_agent_id=qa_agent_id,
    )

    try:
        match = _re.search(r'\{.*\}', content, _re.DOTALL)
        if not match:
            raise ValueError("LLM 输出中未找到 JSON")
        data = _json.loads(match.group())

        verdict.results = [RubricResult(**r) for r in data.get("results", [])]
        verdict.actionable_feedback = data.get("actionable_feedback", [])
        verdict.compute_score(handoff.rubric)

    except Exception as e:
        verdict.status = "BLOCKED"
        verdict.blocked_reason = f"QA 输出解析失败：{e}，原始内容：{content[:200]}"

    return verdict
