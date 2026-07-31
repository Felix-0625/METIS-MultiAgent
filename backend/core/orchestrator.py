"""
三层协作编排器 (Orchestrator)

架构设计：
  顶层（层级式）：PM 持续感知 → Supervisor 守关 → HR 动态创建 → CCB 审批变更
  中层（平行式）：PG/Sec/Perf/UXO 并发执行，各自产出结构化 HandoffPayload
  底层（流水线）：QA 四层流水，不通过直接上报 Supervisor，由 Supervisor 决策

连接点：
  中层 → 底层：结构化 HandoffPayload（不是字符串）
  底层 → 顶层：QAVerdict 状态码（PASS/FAIL/BLOCKED），不是自然语言
  顶层 → 中层：Rubric 验收标准，任务分配时生成，QA 对照打分

动态重规划：
  PM 持续感知项目状态（阶段完成率、质检失败率、变更请求）
  任何时候用户或 Supervisor 触发重规划，PM 重新评估并输出调整方案
  调整方案经 CCB 审批后，HR 按新方案重新分配任务
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from core.handoff import HandoffPayload, QAVerdict, RubricItem, make_rubric_from_requirements


# ─── 状态码（三层之间的通信语言）────────────────────────────────────────────────

class PhaseStatus(str, Enum):
    PENDING   = "pending"    # 未开始
    RUNNING   = "running"    # 执行中
    QA_CHECK  = "qa_check"   # 质检中
    PASS      = "pass"       # 通过，可进入下一阶段
    FAIL      = "fail"       # 质检失败，Supervisor 决策中
    BLOCKED   = "blocked"    # 被阻塞（上游未完成）
    REPLANNING = "replanning" # 重规划中
    COMPLETED = "completed"  # 已完成


class SupervisorDecision(str, Enum):
    PASS          = "pass"           # 质检通过
    RETRY_LOCAL   = "retry_local"    # 让对应 Agent 局部修改
    RETRY_PHASE   = "retry_phase"    # 整个阶段推倒重来
    DEGRADE_PASS  = "degrade_pass"   # 降级通过（记录问题，继续推进）
    ESCALATE_PM   = "escalate_pm"    # 上报 PM，触发重规划
    BLOCK         = "block"          # 阻塞，等待人工介入


# ─── QA 上报事件（底层→顶层的通信）─────────────────────────────────────────────

class QAReport(BaseModel):
    """QA 流水线完成后向 Supervisor 上报的结构化报告"""
    report_id: str = Field(default_factory=lambda: f"qar-{uuid.uuid4().hex[:8]}")
    created_at: float = Field(default_factory=time.time)

    phase_id: str
    phase_name: str = ""
    subproject_id: str
    subproject_name: str = ""
    responsible_agent_id: str = ""
    responsible_agent_role: str = ""

    # QA 结果
    verdict: str  # PASS / FAIL / BLOCKED
    score: int = 0
    max_score: int = 0
    pass_rate: float = 0.0  # score / max_score

    # 失败详情（FAIL 时必填）
    failed_layers: List[str] = Field(default_factory=list)   # 哪几层没过
    critical_issues: List[str] = Field(default_factory=list) # 关键问题（必须修复）
    minor_issues: List[str] = Field(default_factory=list)    # 次要问题（可降级通过）
    fix_hints: List[str] = Field(default_factory=list)       # 修复建议

    # 重试信息
    retry_count: int = 0
    max_retries: int = 3

    def is_critical_failure(self) -> bool:
        """是否是严重失败（需要 Supervisor 介入）"""
        return self.verdict == "FAIL" and len(self.critical_issues) > 0

    def can_degrade(self) -> bool:
        """是否可以降级通过（只有次要问题）"""
        return self.verdict == "FAIL" and len(self.critical_issues) == 0 and len(self.minor_issues) > 0

    def to_supervisor_brief(self) -> str:
        """生成给 Supervisor 的简报（用于 LLM 决策）"""
        lines = [
            f"【QA 上报】阶段：{self.phase_name} | 子项目：{self.subproject_name}",
            f"结果：{self.verdict} | 得分：{self.score}/{self.max_score} ({self.pass_rate:.0%})",
            f"负责 Agent：{self.responsible_agent_role}（{self.responsible_agent_id}）",
        ]
        if self.failed_layers:
            lines.append(f"失败层：{', '.join(self.failed_layers)}")
        if self.critical_issues:
            lines.append("关键问题（必须修复）：")
            for i in self.critical_issues[:5]:
                lines.append(f"  ✗ {i}")
        if self.minor_issues:
            lines.append(f"次要问题（{len(self.minor_issues)} 条，可降级通过）")
        if self.retry_count > 0:
            lines.append(f"已重试 {self.retry_count}/{self.max_retries} 次")
        return "\n".join(lines)


# ─── 重规划请求（任何层都可以触发）──────────────────────────────────────────────

class ReplanRequest(BaseModel):
    """动态重规划请求"""
    request_id: str = Field(default_factory=lambda: f"rp-{uuid.uuid4().hex[:8]}")
    created_at: float = Field(default_factory=time.time)

    # 触发来源
    triggered_by: str  # "user" / "supervisor" / "pm" / "qa_failure"
    trigger_reason: str  # 触发原因描述

    # 当前项目状态快照
    project_id: str
    current_phase_id: str = ""
    completed_phases: List[str] = Field(default_factory=list)
    failed_phases: List[str] = Field(default_factory=list)
    qa_failure_count: int = 0

    # 用户/Supervisor 的调整意图
    adjustment_intent: str = ""  # 用户想改什么
    affected_scope: str = "phase"  # "task" / "phase" / "project"

    # 约束条件（不能改的东西）
    frozen_deliverables: List[str] = Field(default_factory=list)  # 已完成且不能回滚的产出


class ReplanResult(BaseModel):
    """PM 重规划输出"""
    request_id: str
    created_at: float = Field(default_factory=time.time)

    # 调整方案
    summary: str  # 一句话说明调整了什么
    changes: List[Dict] = Field(default_factory=list)  # 具体变更列表
    # 每条变更格式：{"type": "add/modify/remove/reorder", "target": "phase/task/agent", "id": "...", "detail": "..."}

    # 影响评估
    impact_level: str = "low"  # low / medium / high
    requires_ccb: bool = False  # 是否需要 CCB 审批
    estimated_delay: str = ""   # 预计延期

    # 新的阶段/任务列表（如果有结构性变化）
    new_phases: List[Dict] = Field(default_factory=list)
    modified_tasks: List[Dict] = Field(default_factory=list)

    approved: bool = False  # CCB/用户是否已批准
    applied: bool = False   # 是否已应用到执行层


# ─── 并行执行结果聚合器 ──────────────────────────────────────────────────────────

class ParallelExecutionResult(BaseModel):
    """中层并行执行的聚合结果"""
    phase_id: str
    phase_name: str = ""
    aggregated_at: float = Field(default_factory=time.time)

    # 各 Agent 的 HandoffPayload
    agent_results: Dict[str, HandoffPayload] = Field(default_factory=dict)
    # key: agent_id, value: HandoffPayload

    # 聚合状态
    all_passed: bool = False
    any_blocked: bool = False
    any_failed: bool = False

    # 聚合产出（合并所有 Agent 的 deliverable）
    merged_deliverable: Dict[str, Any] = Field(default_factory=dict)

    # 传递给 QA 的统一 HandoffPayload
    qa_handoff: Optional[HandoffPayload] = None

    def aggregate(self) -> None:
        """聚合所有 Agent 结果"""
        statuses = [p.status for p in self.agent_results.values()]
        self.any_blocked = "BLOCKED" in statuses
        self.any_failed = "FAILED" in statuses
        self.all_passed = all(s == "OK" for s in statuses)

        # 合并 deliverable
        for agent_id, payload in self.agent_results.items():
            for k, v in payload.deliverable.items():
                self.merged_deliverable[f"{agent_id}:{k}"] = v

    def build_qa_handoff(self, phase_id: str, requirements: List[str], rubric: List[RubricItem]) -> HandoffPayload:
        """构建传递给 QA 的统一 HandoffPayload"""
        self.qa_handoff = HandoffPayload(
            phase=phase_id,
            phase_name=self.phase_name,
            task_id=f"qa-{phase_id}",
            from_agent_id="parallel_aggregator",
            from_agent_role="执行层聚合器",
            to_agent_role="QA Agent",
            requirements=requirements,
            deliverable=self.merged_deliverable,
            rubric=rubric,
            status="OK" if self.all_passed else ("BLOCKED" if self.any_blocked else "PARTIAL"),
        )
        return self.qa_handoff


# ─── Orchestrator 核心 ────────────────────────────────────────────────────────

class Orchestrator:
    """
    三层协作编排器

    职责：
    1. 管理阶段状态机（PENDING → RUNNING → QA_CHECK → PASS/FAIL）
    2. 接收 QA 上报，触发 Supervisor 决策
    3. 接收重规划请求，协调 PM 重新规划
    4. 管理并行执行的聚合
    5. 维护 Rubric 的生命周期（PM 生成 → 执行层携带 → QA 对照打分）
    """

    def __init__(self, project_id: str):
        self.project_id = project_id

        # 阶段状态
        self._phase_status: Dict[str, PhaseStatus] = {}
        # 阶段 Rubric（PM 生成，贯穿整个阶段）
        self._phase_rubrics: Dict[str, List[RubricItem]] = {}
        # 阶段需求锚点
        self._phase_requirements: Dict[str, List[str]] = {}

        # QA 上报队列
        self._qa_reports: List[QAReport] = []
        # Supervisor 决策记录
        self._supervisor_decisions: List[Dict] = []
        # 重规划历史
        self._replan_history: List[ReplanResult] = []

        # 并行执行结果
        self._parallel_results: Dict[str, ParallelExecutionResult] = {}

        # 回调注册
        self._on_qa_fail_callbacks: List[Callable] = []
        self._on_replan_callbacks: List[Callable] = []

    # ── Rubric 管理 ───────────────────────────────────────────────────────────

    def set_phase_rubric(
        self,
        phase_id: str,
        requirements: List[str],
        rubric_items: Optional[List[Dict]] = None,
    ) -> List[RubricItem]:
        """
        为阶段设置 Rubric（PM 在任务分配时调用）。
        rubric_items 为 None 时从 requirements 自动生成。
        """
        self._phase_requirements[phase_id] = requirements
        if rubric_items:
            rubric = [RubricItem(**item) for item in rubric_items]
        else:
            rubric = make_rubric_from_requirements(requirements)
        self._phase_rubrics[phase_id] = rubric
        return rubric

    def get_phase_rubric(self, phase_id: str) -> List[RubricItem]:
        return self._phase_rubrics.get(phase_id, [])

    def get_phase_requirements(self, phase_id: str) -> List[str]:
        return self._phase_requirements.get(phase_id, [])

    # ── 阶段状态机 ────────────────────────────────────────────────────────────

    def get_phase_status(self, phase_id: str) -> PhaseStatus:
        return self._phase_status.get(phase_id, PhaseStatus.PENDING)

    def set_phase_status(self, phase_id: str, status: PhaseStatus) -> None:
        self._phase_status[phase_id] = status

    def transition_phase(self, phase_id: str, new_status: PhaseStatus) -> bool:
        """
        阶段状态转换（带合法性检查）。
        返回 True 表示转换成功。
        """
        current = self.get_phase_status(phase_id)
        # 合法转换表
        valid_transitions = {
            PhaseStatus.PENDING:    [PhaseStatus.RUNNING, PhaseStatus.BLOCKED],
            PhaseStatus.RUNNING:    [PhaseStatus.QA_CHECK, PhaseStatus.FAIL, PhaseStatus.BLOCKED, PhaseStatus.REPLANNING],
            PhaseStatus.QA_CHECK:   [PhaseStatus.PASS, PhaseStatus.FAIL, PhaseStatus.BLOCKED],
            PhaseStatus.FAIL:       [PhaseStatus.RUNNING, PhaseStatus.REPLANNING, PhaseStatus.COMPLETED],
            PhaseStatus.PASS:       [PhaseStatus.COMPLETED],
            PhaseStatus.REPLANNING: [PhaseStatus.RUNNING, PhaseStatus.BLOCKED],
            PhaseStatus.BLOCKED:    [PhaseStatus.RUNNING, PhaseStatus.REPLANNING],
            PhaseStatus.COMPLETED:  [],  # 终态
        }
        if new_status in valid_transitions.get(current, []):
            self._phase_status[phase_id] = new_status
            return True
        return False

    # ── QA 上报（底层→顶层）──────────────────────────────────────────────────

    def receive_qa_report(self, report: QAReport) -> SupervisorDecision:
        """
        接收 QA 上报，返回 Supervisor 决策。

        决策逻辑：
        - PASS → 阶段通过，进入 COMPLETED
        - FAIL + 可降级 → DEGRADE_PASS（记录问题，继续推进）
        - FAIL + 严重 + 可重试 → RETRY_LOCAL（让对应 Agent 修复）
        - FAIL + 严重 + 超重试 → ESCALATE_PM（触发重规划）
        - BLOCKED → BLOCK（等待人工介入）
        """
        self._qa_reports.append(report)

        if report.verdict == "PASS":
            self.transition_phase(report.phase_id, PhaseStatus.PASS)
            self._record_decision(report, "PASS", "质检通过，阶段完成")
            return SupervisorDecision.PASS

        if report.verdict == "BLOCKED":
            self.transition_phase(report.phase_id, PhaseStatus.BLOCKED)
            self._record_decision(report, "BLOCK", "QA 被阻塞，等待人工介入")
            return SupervisorDecision.BLOCK

        # FAIL 情况
        self.transition_phase(report.phase_id, PhaseStatus.FAIL)

        if report.can_degrade():
            # 只有次要问题，降级通过
            self._record_decision(report, "DEGRADE_PASS", f"降级通过：{len(report.minor_issues)} 条次要问题已记录")
            return SupervisorDecision.DEGRADE_PASS

        if report.retry_count < report.max_retries:
            # 还有重试机会，让 Agent 局部修复
            self._record_decision(report, "RETRY_LOCAL",
                f"第 {report.retry_count + 1} 次重试：{len(report.critical_issues)} 个关键问题需修复")
            return SupervisorDecision.RETRY_LOCAL

        # 超过重试次数，上报 PM 触发重规划
        self.transition_phase(report.phase_id, PhaseStatus.REPLANNING)
        self._record_decision(report, "ESCALATE_PM",
            f"已重试 {report.retry_count} 次仍未通过，上报 PM 重规划")
        for cb in self._on_qa_fail_callbacks:
            try:
                cb(report)
            except Exception:
                logging.getLogger("orchestrator").error("on_qa_fail 回调异常", exc_info=True)
        return SupervisorDecision.ESCALATE_PM

    def _record_decision(self, report: QAReport, decision: str, reason: str) -> None:
        self._supervisor_decisions.append({
            "at": time.time(),
            "phase_id": report.phase_id,
            "subproject_id": report.subproject_id,
            "qa_verdict": report.verdict,
            "decision": decision,
            "reason": reason,
            "retry_count": report.retry_count,
        })

    def get_supervisor_decisions(self, phase_id: str = "") -> List[Dict]:
        if phase_id:
            return [d for d in self._supervisor_decisions if d["phase_id"] == phase_id]
        return self._supervisor_decisions

    # ── 动态重规划 ────────────────────────────────────────────────────────────

    def create_replan_request(
        self,
        triggered_by: str,
        trigger_reason: str,
        adjustment_intent: str = "",
        affected_scope: str = "phase",
        current_phase_id: str = "",
    ) -> ReplanRequest:
        """
        创建重规划请求。

        触发场景：
        - 用户主动调整（triggered_by="user"）
        - QA 多次失败（triggered_by="qa_failure"）
        - Supervisor 判断需要重规划（triggered_by="supervisor"）
        """
        completed = [pid for pid, s in self._phase_status.items() if s == PhaseStatus.COMPLETED]
        failed = [pid for pid, s in self._phase_status.items() if s == PhaseStatus.FAIL]
        qa_failures = len([d for d in self._supervisor_decisions if d["decision"] == "ESCALATE_PM"])

        return ReplanRequest(
            triggered_by=triggered_by,
            trigger_reason=trigger_reason,
            project_id=self.project_id,
            current_phase_id=current_phase_id,
            completed_phases=completed,
            failed_phases=failed,
            qa_failure_count=qa_failures,
            adjustment_intent=adjustment_intent,
            affected_scope=affected_scope,
            frozen_deliverables=completed,  # 已完成的阶段产出不能回滚
        )

    def apply_replan_result(self, result: ReplanResult) -> bool:
        """
        应用重规划结果（CCB/用户批准后调用）。
        返回 True 表示应用成功。
        """
        if not result.approved:
            return False
        self._replan_history.append(result)
        result.applied = True

        # 重置受影响阶段的状态
        for change in result.changes:
            if change.get("type") == "modify" and change.get("target") == "phase":
                phase_id = change.get("id", "")
                if phase_id and self._phase_status.get(phase_id) not in (PhaseStatus.COMPLETED,):
                    self._phase_status[phase_id] = PhaseStatus.PENDING

        for cb in self._on_replan_callbacks:
            try:
                cb(result)
            except Exception as ex:
                logging.getLogger("orchestrator").error("on_replan 回调异常: %s", ex, exc_info=True)
        return True

    def get_replan_history(self) -> List[Dict]:
        return [r.model_dump() for r in self._replan_history]

    # ── 并行执行管理 ──────────────────────────────────────────────────────────

    def register_agent_result(
        self,
        phase_id: str,
        phase_name: str,
        agent_id: str,
        payload: HandoffPayload,
    ) -> None:
        """注册单个 Agent 的执行结果（并行执行时每个 Agent 完成后调用）"""
        if phase_id not in self._parallel_results:
            self._parallel_results[phase_id] = ParallelExecutionResult(
                phase_id=phase_id,
                phase_name=phase_name,
            )
        self._parallel_results[phase_id].agent_results[agent_id] = payload

    def get_parallel_result(self, phase_id: str) -> Optional[ParallelExecutionResult]:
        return self._parallel_results.get(phase_id)

    def build_qa_handoff_for_phase(self, phase_id: str) -> Optional[HandoffPayload]:
        """
        当阶段所有 Agent 完成后，聚合结果并构建传给 QA 的 HandoffPayload。
        """
        result = self._parallel_results.get(phase_id)
        if not result:
            return None
        result.aggregate()
        rubric = self.get_phase_rubric(phase_id)
        requirements = self.get_phase_requirements(phase_id)
        return result.build_qa_handoff(phase_id, requirements, rubric)

    # ── 回调注册 ──────────────────────────────────────────────────────────────

    def on_qa_fail(self, callback: Callable) -> None:
        """注册 QA 失败且需要重规划时的回调"""
        self._on_qa_fail_callbacks.append(callback)

    def on_replan(self, callback: Callable) -> None:
        """注册重规划应用时的回调"""
        self._on_replan_callbacks.append(callback)

    # ── 状态快照 ──────────────────────────────────────────────────────────────

    def get_status_snapshot(self) -> Dict:
        """获取当前编排状态快照（供 PM 持续感知）"""
        return {
            "project_id": self.project_id,
            "phase_statuses": {pid: s.value for pid, s in self._phase_status.items()},
            "qa_reports_count": len(self._qa_reports),
            "qa_fail_count": len([r for r in self._qa_reports if r.verdict == "FAIL"]),
            "qa_pass_count": len([r for r in self._qa_reports if r.verdict == "PASS"]),
            "supervisor_decisions": self._supervisor_decisions[-5:],  # 最近 5 条
            "replan_count": len(self._replan_history),
            "phases_completed": len([s for s in self._phase_status.values() if s == PhaseStatus.COMPLETED]),
            "phases_failed": len([s for s in self._phase_status.values() if s == PhaseStatus.FAIL]),
            "phases_running": len([s for s in self._phase_status.values() if s == PhaseStatus.RUNNING]),
        }

    def to_pm_context(self) -> str:
        """
        生成给 PM 的持续感知上下文（注入 PM 的 system prompt）。
        PM 通过这个上下文了解项目当前真实状态，决定是否需要重规划。
        """
        snap = self.get_status_snapshot()
        lines = [
            "【项目实时状态（PM 持续感知）】",
            f"已完成阶段：{snap['phases_completed']} | 执行中：{snap['phases_running']} | 失败：{snap['phases_failed']}",
            f"QA 通过：{snap['qa_pass_count']} | QA 失败：{snap['qa_fail_count']} | 重规划次数：{snap['replan_count']}",
        ]
        if snap["phases_failed"] > 0:
            failed_phases = [pid for pid, s in self._phase_status.items() if s == PhaseStatus.FAIL]
            lines.append(f"⚠️ 失败阶段：{', '.join(failed_phases)}")
        if snap["replan_count"] > 0:
            last_replan = self._replan_history[-1]
            lines.append(f"最近重规划：{last_replan.summary}")
        if snap["supervisor_decisions"]:
            lines.append("最近 Supervisor 决策：")
            for d in snap["supervisor_decisions"][-3:]:
                lines.append(f"  [{d['phase_id']}] {d['decision']} — {d['reason']}")
        return "\n".join(lines)


# ─── 全局 Orchestrator 注册表 ─────────────────────────────────────────────────

_orchestrators: Dict[str, Orchestrator] = {}


def get_orchestrator(project_id: str) -> Orchestrator:
    """获取（或创建）项目的 Orchestrator 单例"""
    if project_id not in _orchestrators:
        _orchestrators[project_id] = Orchestrator(project_id)
    return _orchestrators[project_id]


def remove_orchestrator(project_id: str) -> None:
    _orchestrators.pop(project_id, None)
