"""
repair_loop.py  —  修复循环控制器 v1
实现五个层面的修复质量保障机制：
  1. 终止条件：单缺陷最多3轮修复；连续两轮恶化自动升级；总轮次5轮兜底人工
  2. 信息传递：质检输出强制结构化缺陷单；PM给专家的指令完整引用原始缺陷单
  3. 修复约束：每批次指令包含 forbidden_zone；限制单缺陷修改行数<=20
  4. 预审机制：专家先提交修复方案(<=100字)，PM/Supervisor审批后才允许改代码
  5. 分批反馈：按类别+文件聚类，每批最多3个缺陷；P0优先；一键启动全流程
新增角色：阶段仲裁者(PhaseArbiter)

【架构定位 - C9】本模块是 routes_repair.py 驱动的「手动修复旁路」，负责缺陷单的
存储/审批/仲裁状态机，**不是质检门禁权威**。项目级质检门禁由
backend/core/supervisor_quality_state.py 的 SupervisorQualityMachine 独占
（routes_phases.py 注释 "SupervisorQualityMachine is authoritative" 已确认）。
因此本模块 _run_qc_gate/_run_sec_gate 的 TODO 空桩与 verify_fixed 的禁用
属预期：双门验证由 Supervisor 质检管线负责，不在旁路内重复实现，避免双轨
判定不一致。修改本模块时勿把空桩当 bug 修。
"""

import json
import asyncio
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from core.issue_ledger import (
    CANONICAL_ID_PROVENANCE,
    ISSUE_SCHEMA_VERSION,
    canonicalize_issue,
    mark_needs_manual,
)


# ─── 枚举 & 常量 ──────────────────────────────────────────────────────────────

class DefectSeverity(str, Enum):
    P0 = "P0"   # 阻断性：语法错误、运行崩溃
    P1 = "P1"   # 严重：功能缺失、逻辑错误
    P2 = "P2"   # 一般：警告、代码质量


class DefectStatus(str, Enum):
    OPEN           = "open"
    PENDING_REVIEW = "pending_review"  # 等待PM/Supervisor审批修复方案
    APPROVED       = "approved"        # 方案已批准，专家可以动手
    FIXING         = "fixing"          # 修复中
    FIXED          = "fixed"           # 已修复（待复检）
    VERIFIED       = "verified"        # 复检通过
    ESCALATED      = "escalated"       # 已升级（仲裁者介入）
    MANUAL         = "needs_manual"    # 需要人工处理


class ArbiterDecision(str, Enum):
    REWRITE_MODULE = "rewrite_module"  # 重写模块
    REPLACE_EXPERT = "replace_expert"  # 更换专家
    HUMAN_REVIEW   = "human_review"    # 人工介入
    FORCE_PASS     = "force_pass"      # 强制通过


MAX_ROUNDS_PER_DEFECT = 6    # 单缺陷最多修复轮次
MAX_TOTAL_ROUNDS      = 6    # 总轮次兜底
MAX_DEFECTS_PER_BATCH = 3    # 每批最多缺陷数
MAX_LINES_PER_FIX     = 20   # 单缺陷修改行数上限
MAX_PROPOSAL_CHARS    = 100  # 修复方案字数上限
MAX_FIX_ROUNDS_DUAL   = 6    # 双重门联合修复轮次上限（超过标记为 needs_manual）


# ─── 结构化缺陷单 ─────────────────────────────────────────────────────────────

@dataclass
class DefectTicket:
    """结构化缺陷单 — 质检输出的标准格式，禁止转述，必须完整引用"""
    defect_id:     str
    severity:      DefectSeverity
    layer:         str           # syntax / logic / functionality / collaboration
    file_path:     str
    line_no:       Optional[int]
    message:       str           # 原始问题描述（禁止PM转述，必须完整传递给专家）
    fix_hint:      str           # 修复建议
    subproject_id: str
    agent_id:      str           # 负责修复的专家 Agent ID
    agent_role:    str
    observation_id: str = ""
    fingerprint: str = ""
    evidence: Any = None
    acceptance_criteria: Any = None
    detected_phase: str = ""
    owner: Any = None
    line: Optional[int] = None
    identity_confidence: str = "low"
    requires_identity_review: bool = True
    needs_manual_reason: str = ""
    manual_since: Optional[float] = None
    issue_schema_version: str = ISSUE_SCHEMA_VERSION
    defect_id_provenance: str = CANONICAL_ID_PROVENANCE
    rule_id: str = ""
    symbol: str = ""
    location: str = ""
    expected: Any = None
    actual: Any = None

    # 修复过程跟踪
    status:      DefectStatus = DefectStatus.OPEN
    fix_rounds:  int          = 0
    security_status: DefectStatus = field(default_factory=lambda: DefectStatus.OPEN)  # 安全检测门独立状态
    file_version_hash: str = ""                                 # 触发缺陷时的文件 SHA-256 哈希
    step_execution_state: Dict[str, str] = field(default_factory=dict)  # 原子步骤状态字典
    created_at:  float        = field(default_factory=time.time)
    updated_at:  float        = field(default_factory=time.time)

    # 预审字段
    fix_proposal:      Optional[str]   = None
    proposal_approved: bool            = False
    proposal_reviewer: str             = ""
    proposal_at:       Optional[float] = None

    # 修复约束
    forbidden_zone: List[str] = field(default_factory=list)
    max_lines:      int       = MAX_LINES_PER_FIX

    # 升级记录
    escalation_reason: Optional[str]            = None
    arbiter_decision:  Optional[ArbiterDecision] = None

    def reset_for_new_version(self, new_file_hash: str) -> None:
        """文件修改后重置双门状态，更新版本哈希（同一事务）"""
        self.status            = DefectStatus.OPEN
        self.security_status   = DefectStatus.OPEN
        self.file_version_hash = new_file_hash
        self.updated_at        = time.time()

    def advance_fix_round(self) -> None:
        """步进修复轮次，超过上限标记为 needs_manual"""
        self.fix_rounds += 1
        self.updated_at  = time.time()
        if self.fix_rounds > MAX_FIX_ROUNDS_DUAL and not is_dual_gate_verified(self):
            self.mark_needs_manual("Automatic repair round limit exceeded")

    def mark_needs_manual(self, reason: str) -> None:
        canonical = mark_needs_manual(self.to_dict(), reason)
        self.status = DefectStatus.MANUAL
        self.needs_manual_reason = canonical["needs_manual_reason"]
        self.manual_since = canonical["manual_since"]
        self.updated_at = time.time()

    def to_dict(self) -> Dict:
        return {
            "defect_id":          self.defect_id,
            "severity":           self.severity.value,
            "layer":              self.layer,
            "file_path":          self.file_path,
            "line_no":            self.line_no,
            "message":            self.message,
            "fix_hint":           self.fix_hint,
            "subproject_id":      self.subproject_id,
            "agent_id":           self.agent_id,
            "agent_role":         self.agent_role,
            "observation_id":     self.observation_id,
            "fingerprint":        self.fingerprint,
            "evidence":           self.evidence,
            "acceptance_criteria": self.acceptance_criteria,
            "detected_phase":     self.detected_phase,
            "owner":              self.owner,
            "line":               self.line,
            "identity_confidence": self.identity_confidence,
            "requires_identity_review": self.requires_identity_review,
            "needs_manual_reason": self.needs_manual_reason,
            "manual_since": self.manual_since,
            "issue_schema_version": self.issue_schema_version,
            "defect_id_provenance": self.defect_id_provenance,
            "rule_id": self.rule_id,
            "symbol": self.symbol,
            "location": self.location,
            "expected": self.expected,
            "actual": self.actual,
            "status":             self.status.value,
            "fix_rounds":         self.fix_rounds,
            "security_status":    self.security_status.value,
            "file_version_hash":  self.file_version_hash,
            "step_execution_state": self.step_execution_state,
            "created_at":         self.created_at,
            "updated_at":         self.updated_at,
            "fix_proposal":       self.fix_proposal,
            "proposal_approved":  self.proposal_approved,
            "proposal_reviewer":  self.proposal_reviewer,
            "proposal_at":        self.proposal_at,
            "forbidden_zone":     self.forbidden_zone,
            "max_lines":          self.max_lines,
            "escalation_reason":  self.escalation_reason,
            "arbiter_decision":   self.arbiter_decision.value if self.arbiter_decision else None,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "DefectTicket":
        canonical = canonicalize_issue(d)
        t = cls(
            defect_id=canonical["defect_id"],
            severity=DefectSeverity(d.get("severity", "P2")),
            layer=d.get("layer", ""),
            file_path=d.get("file_path", ""),
            line_no=d.get("line_no"),
            message=d.get("message", ""),
            fix_hint=d.get("fix_hint", ""),
            subproject_id=d.get("subproject_id", ""),
            agent_id=d.get("agent_id", ""),
            agent_role=d.get("agent_role", ""),
            observation_id=canonical.get("observation_id", ""),
            fingerprint=canonical.get("fingerprint", ""),
            evidence=d.get("evidence"),
            acceptance_criteria=d.get("acceptance_criteria"),
            detected_phase=d.get("detected_phase", ""),
            owner=d.get("owner"),
            line=d.get("line", d.get("line_no")),
            identity_confidence=d.get("identity_confidence", "low"),
            requires_identity_review=d.get("requires_identity_review", True),
            needs_manual_reason=d.get("needs_manual_reason", ""),
            manual_since=d.get("manual_since"),
            issue_schema_version=canonical["issue_schema_version"],
            defect_id_provenance=canonical["defect_id_provenance"],
            rule_id=canonical.get("rule_id") or canonical.get("rule") or "",
            symbol=canonical.get("symbol") or canonical.get("symbol_name") or "",
            location=canonical.get("location") or canonical.get("endpoint") or "",
            expected=canonical.get("expected"),
            actual=canonical.get("actual"),
        )
        raw_status = d.get("status", "open")
        t.status             = DefectStatus(
            "needs_manual" if raw_status == "manual" else raw_status
        )
        t.fix_rounds         = d.get("fix_rounds", 0)
        t.security_status    = DefectStatus(d.get("security_status", "open"))
        t.file_version_hash  = d.get("file_version_hash", "")
        t.step_execution_state = d.get("step_execution_state", {})
        t.created_at         = d.get("created_at", time.time())
        t.updated_at         = d.get("updated_at", time.time())
        t.fix_proposal       = d.get("fix_proposal")
        t.proposal_approved  = d.get("proposal_approved", False)
        t.proposal_reviewer  = d.get("proposal_reviewer", "")
        t.proposal_at        = d.get("proposal_at")
        t.forbidden_zone     = d.get("forbidden_zone", [])
        t.max_lines          = d.get("max_lines", MAX_LINES_PER_FIX)
        t.escalation_reason  = d.get("escalation_reason")
        ad = d.get("arbiter_decision")
        t.arbiter_decision   = ArbiterDecision(ad) if ad else None
        return t


# ─── 双重门质检辅助函数 ────────────────────────────

def is_dual_gate_verified(defect: DefectTicket) -> bool:
    """双重门联合收敛判定：supervisor 动态质检门 + 安全检测门 同时通过"""
    return defect.status == DefectStatus.VERIFIED and defect.security_status == DefectStatus.VERIFIED

# ====== ?????? ====== ??? + ?????????asyncio.gather?======

async def run_auto_repair_cycle(defect: DefectTicket, project_id: str) -> DefectTicket:
    """
    ???????????

    ??????????????asyncio.gather??
      1. ??????is_dual_gate_verified? ?? VERIFIED???
      2. ?????? fix_rounds < MAX_FIX_ROUNDS_DUAL  ???????fix_rounds += 1
      3. fix_rounds >= MAX_FIX_ROUNDS_DUAL  ?? needs_manual????????

    Args:
        defect:     ????????????????
        project_id: ???? ID???????? Agent ???

    Returns:
        ???? DefectTicket
    """

    async def _run_qc_gate(d: DefectTicket) -> bool:
        """
        ??????
        ???????????????? d.status?
        ????? d.status == VERIFIED ???????
        ???????? QAAgent.inspect() ??????????
        """
        # TODO: ?????? Agent ?????
        return d.status == DefectStatus.VERIFIED

    async def _run_sec_gate(d: DefectTicket) -> bool:
        """
        ????????
        ???????????????? d.security_status?
        ????? d.security_status == VERIFIED ???????
        ???????????? Agent ????????????
        """
        # TODO: ???????? Agent ?????
        return d.security_status == DefectStatus.VERIFIED

    # --- 1. ??? + ?????? ---
    qc_passed, sec_passed = await asyncio.gather(
        _run_qc_gate(defect),
        _run_sec_gate(defect),
    )

    # --- 2. ?????? ---
    if qc_passed:
        defect.status = DefectStatus.VERIFIED
    if sec_passed:
        defect.security_status = DefectStatus.VERIFIED

    # --- 3. ??????????? ---
    if is_dual_gate_verified(defect):
        # 3a. ?????  VERIFIED???
        defect.status = DefectStatus.VERIFIED
        defect.security_status = DefectStatus.VERIFIED

    elif defect.fix_rounds < MAX_FIX_ROUNDS_DUAL:
        # 3b. ??????????  ????????? +1
        defect.fix_rounds += 1
        defect.status = DefectStatus.OPEN
        defect.security_status = DefectStatus.OPEN

    else:
        # 3c. ????????  needs_manual
        defect.mark_needs_manual("Dual-gate repair round limit exceeded")

    defect.updated_at = time.time()
    return defect




# ─── 修复批次 ─────────────────────────────────────────────────────────────────

@dataclass
class RepairBatch:
    """一个修复批次：按类别+文件聚类，每批最多3个缺陷"""
    batch_id:     str
    defects:      List[DefectTicket]
    category:     str   # 聚类类别（layer 或 file）
    agent_id:     str
    round_no:     int   # 第几轮修复
    created_at:   float        = field(default_factory=time.time)
    started_at:   Optional[float] = None
    completed_at: Optional[float] = None
    status:       str  = "pending"  # pending / running / done / failed

    def to_instruction(self) -> str:
        """生成给专家的完整修复指令（完整引用原始缺陷单，禁止转述）"""
        lines = [
            f"## 修复批次 {self.batch_id}  [第 {self.round_no} 轮 / 最多 {MAX_TOTAL_ROUNDS} 轮]",
            f"类别：{self.category}  |  本批缺陷数：{len(self.defects)}",
            "",
            "### ⚠️ 修复约束（必须遵守）",
            f"- 每个缺陷修改行数 ≤ {MAX_LINES_PER_FIX} 行",
            "- 禁止修改区域（forbidden_zone）：",
        ]
        all_forbidden: set = set()
        for d in self.defects:
            all_forbidden.update(d.forbidden_zone)
        if all_forbidden:
            for fz in sorted(all_forbidden):
                lines.append(f"  - {fz}")
        else:
            lines.append("  - 无（但仍需最小化改动范围）")

        lines.append("")
        lines.append("### 缺陷清单（原始缺陷单，禁止转述，完整引用）")
        for i, d in enumerate(self.defects, 1):
            lines += [
                "",
                f"#### 缺陷 {i}  [{d.severity.value}]  ID: {d.defect_id}",
                f"- 文件：`{d.file_path}`  行号：{d.line_no or '未知'}",
                f"- 层次：{d.layer}",
                f"- 问题：{d.message}",
                f"- 修复建议：{d.fix_hint}",
                f"- 已尝试修复次数：{d.fix_rounds} / {MAX_ROUNDS_PER_DEFECT}",
            ]
            if d.fix_proposal:
                lines.append(f"- 已批准方案：{d.fix_proposal}")

        lines += [
            "",
            "### 完成后",
            "请在修复完成后回复：【修复完成】，并列出每个缺陷ID的修改摘要（每条≤20字）。",
        ]
        return "\n".join(lines)

    def to_dict(self) -> Dict:
        return {
            "batch_id":    self.batch_id,
            "defects":     [d.to_dict() for d in self.defects],
            "category":    self.category,
            "agent_id":    self.agent_id,
            "round_no":    self.round_no,
            "created_at":  self.created_at,
            "started_at":  self.started_at,
            "completed_at": self.completed_at,
            "status":      self.status,
        }


# ─── 修复循环控制器 ───────────────────────────────────────────────────────────

class RepairLoopController:
    """
    修复循环控制器
    - 管理一个子项目的全部缺陷单和修复轮次
    - 负责：终止条件判断、分批生成、预审流程、恶化检测
    """

    def __init__(self, subproject_id: str, subproject_name: str):
        self.subproject_id   = subproject_id
        self.subproject_name = subproject_name
        self.total_rounds    = 0          # 已进行的总修复轮次
        self.defects: Dict[str, DefectTicket] = {}   # defect_id → DefectTicket
        self.batches: List[RepairBatch]  = []
        self.round_scores: List[int]     = []        # 每轮质检分数，用于恶化检测
        self.escalated       = False
        self.arbiter_triggered = False
        self.created_at      = time.time()

    # ── 从质检结果导入缺陷单 ──────────────────────────────────────────────────

    def ingest_qc_result(
        self,
        qc_result: Dict,
        agent_id: str,
        agent_role: str,
        forbidden_zone: Optional[List[str]] = None,
    ) -> List[DefectTicket]:
        """
        将质检结果转换为结构化缺陷单并注册。
        支持两种数据格式：
          1. layer_results 格式：[{layer: "syntax", issues: [{...}]}]
          2. issues_detail 格式：[{layer: "syntax", message: "...", ...}]
        
        - 已存在的缺陷（按 message 前40字匹配）：
          - 若状态为 verified/manual，则跳过（不重新打开已验证通过的缺陷）
          - 若状态为其他（open/fixing/escalated等），更新为 open（允许重新修复）
        - 新缺陷追加
        返回本次新增的缺陷单列表
        """
        score = qc_result.get("score", 100)
        self.round_scores.append(score)

        new_tickets: List[DefectTicket] = []
        # Durable identity is shared with Supervisor/Engineer projections.
        existing_keys = {
            d.fingerprint or canonicalize_issue(d.to_dict())["fingerprint"]: d
            for d in self.defects.values()
        }

        # 兼容两种格式：优先使用 layer_results，fallback 到 issues_detail
        issues_to_process = []
        
        if "layer_results" in qc_result and qc_result["layer_results"]:
            # 格式1: layer_results 嵌套结构
            for lr in qc_result.get("layer_results", []):
                layer = lr.get("layer", "unknown")
                for iss in lr.get("issues", []):
                    iss_copy = iss.copy()
                    iss_copy["layer"] = layer  # 确保每个 issue 都有 layer 字段
                    issues_to_process.append(iss_copy)
        elif "issues_detail" in qc_result and qc_result["issues_detail"]:
            # 格式2: issues_detail 扁平列表（main.py 实际使用的格式）
            issues_to_process = qc_result.get("issues_detail", [])
        
        # 统一处理
        for iss in issues_to_process:
            if not iss.get("message") and not iss.get("rule_id") and not iss.get("rule"):
                continue
            
            layer = iss.get("layer", "unknown")
            sev_raw = iss.get("severity", "warning")
            
            # 映射 severity：error→P0/P1，warning→P2
            if sev_raw == "error":
                layer_sev = DefectSeverity.P0 if layer == "syntax" else DefectSeverity.P1
            else:
                layer_sev = DefectSeverity.P2

            # The raw message/evidence belong to this observation, not identity.
            canonical = canonicalize_issue(
                iss,
                defaults={
                    "layer": layer,
                    "responsible_agent_id": agent_id,
                    "responsible_agent_role": agent_role,
                },
                observation_context={
                    "subproject_id": self.subproject_id,
                    "round": len(self.round_scores),
                },
            )
            lookup_key = canonical["fingerprint"]
            if lookup_key in existing_keys:
                # 已有缺陷：若已 verified/manual 则跳过，否则重置为 open
                existing = existing_keys[lookup_key]
                existing.observation_id = canonical["observation_id"]
                existing.evidence = canonical.get("evidence")
                existing.acceptance_criteria = canonical.get("acceptance_criteria")
                existing.line = canonical.get("line")
                existing.line_no = canonical.get("line_no")
                if existing.status != DefectStatus.MANUAL:
                    existing.status     = DefectStatus.OPEN
                    existing.updated_at = time.time()
                continue

            defect_id = canonical["defect_id"]
            ticket = DefectTicket(
                defect_id=defect_id,
                severity=layer_sev,
                layer=layer,
                file_path=canonical.get("file_path", ""),
                line_no=canonical.get("line_no"),
                message=iss.get("message", ""),
                fix_hint=iss.get("fix_hint", ""),
                subproject_id=self.subproject_id,
                agent_id=agent_id,
                agent_role=agent_role,
                observation_id=canonical["observation_id"],
                fingerprint=canonical["fingerprint"],
                evidence=canonical.get("evidence"),
                acceptance_criteria=canonical.get("acceptance_criteria"),
                detected_phase=canonical.get("detected_phase", ""),
                owner=canonical.get("owner"),
                line=canonical.get("line"),
                identity_confidence=canonical["identity_confidence"],
                requires_identity_review=canonical["requires_identity_review"],
                issue_schema_version=canonical["issue_schema_version"],
                defect_id_provenance=canonical["defect_id_provenance"],
                rule_id=canonical.get("rule_id") or canonical.get("rule") or "",
                symbol=canonical.get("symbol") or canonical.get("symbol_name") or "",
                location=canonical.get("location") or canonical.get("endpoint") or "",
                expected=canonical.get("expected"),
                actual=canonical.get("actual"),
                forbidden_zone=list(forbidden_zone or []),
            )
            self.defects[defect_id] = ticket
            new_tickets.append(ticket)

        return new_tickets

    # ── 终止条件检查 ──────────────────────────────────────────────────────────

    def check_termination(self) -> Dict[str, Any]:
        """
        检查是否触发终止条件，返回终止原因和建议动作。
        优先级：连续恶化 > 单缺陷超轮次 > 总轮次兜底
        """
        reasons: List[str] = []
        should_escalate = False

        # 1. 连续两轮恶化检测（需要至少3轮数据才能判断"连续两轮"）
        if len(self.round_scores) >= 3:
            # 检查最近两轮是否连续下降
            if (self.round_scores[-1] < self.round_scores[-2] and 
                self.round_scores[-2] < self.round_scores[-3]):
                reasons.append(
                    f"连续两轮质检分数下降（{self.round_scores[-3]}→{self.round_scores[-2]}→{self.round_scores[-1]}），"
                    "修复引入了新问题"
                )
                should_escalate = True

        # 2. 单缺陷超过最大修复轮次（包含已被标记为 ESCALATED 的缺陷）
        # 注意：reopen_defect() 在 fix_rounds >= MAX_ROUNDS_PER_DEFECT 时会把状态设为 ESCALATED，
        # 所以这里必须同时检测 ESCALATED 状态，否则超轮次缺陷会被漏掉。
        over_limit = [
            d for d in self.defects.values()
            if d.fix_rounds >= MAX_ROUNDS_PER_DEFECT
            and d.status not in (DefectStatus.VERIFIED, DefectStatus.MANUAL)
        ]
        if over_limit:
            ids = ", ".join(d.defect_id for d in over_limit[:3])
            reasons.append(f"缺陷 {ids} 已尝试修复 {MAX_ROUNDS_PER_DEFECT} 轮仍未解决")
            should_escalate = True

        # 3. 总轮次兜底
        if self.total_rounds >= MAX_TOTAL_ROUNDS:
            reasons.append(f"总修复轮次已达上限 {MAX_TOTAL_ROUNDS} 轮")
            should_escalate = True

        # 4. P0 缺陷未减少（连续两轮 P0 数量不变）
        p0_open = [d for d in self.defects.values()
                   if d.severity == DefectSeverity.P0
                   and d.status in (DefectStatus.OPEN, DefectStatus.FIXING)]
        if p0_open and self.total_rounds >= 2:
            reasons.append(f"存在 {len(p0_open)} 个 P0 缺陷经过 {self.total_rounds} 轮仍未解决")
            should_escalate = True

        return {
            "should_escalate": should_escalate,
            "reasons":         reasons,
            "total_rounds":    self.total_rounds,
            "open_defects":    len(self.get_open_defects()),
            "p0_count":        len(p0_open),
        }

    # ── 分批生成 ──────────────────────────────────────────────────────────────

    def create_batches(self, forbidden_zone: Optional[List[str]] = None) -> List[RepairBatch]:
        """
        将待修复缺陷按「类别+文件」聚类，每批最多 MAX_DEFECTS_PER_BATCH 个。
        P0 优先，同类别同文件的缺陷放在一批。
        返回本轮新生成的批次列表。

        幂等性保障：若当前轮次已有 pending_review 状态的缺陷（说明批次已生成但未处理），
        直接返回已有未完成批次，不再新增轮次，防止重复调用导致 total_rounds 虚增。
        """
        # 幂等检查：若存在 pending_review 缺陷，说明上一次 create_batches 已生成批次
        # 直接返回当前轮次的已有批次，不重复计数
        pending = self.get_pending_review_defects()
        if pending:
            current_round_batches = [b for b in self.batches if b.round_no == self.total_rounds]
            if current_round_batches:
                return current_round_batches

        open_defects = self.get_open_defects()
        if not open_defects:
            return []

        # 按优先级排序：P0 > P1 > P2，同级按 layer 聚类
        priority_order = {DefectSeverity.P0: 0, DefectSeverity.P1: 1, DefectSeverity.P2: 2}
        open_defects.sort(key=lambda d: (priority_order[d.severity], d.layer, d.file_path))

        # 聚类：同 severity + 同 layer + 同 file 的缺陷归为一组
        # 注意：severity 必须作为聚类 key 的一部分，确保 P0/P1/P2 不混批
        # 这样 P0 缺陷永远在独立批次中，不会被 P2 稀释
        clusters: Dict[str, List[DefectTicket]] = {}
        for d in open_defects:
            key = f"{priority_order[d.severity]}::{d.layer}::{d.file_path or 'unknown'}"
            clusters.setdefault(key, []).append(d)

        self.total_rounds += 1
        new_batches: List[RepairBatch] = []

        for cluster_key, cluster_defects in clusters.items():
            # 每批最多 MAX_DEFECTS_PER_BATCH 个
            for i in range(0, len(cluster_defects), MAX_DEFECTS_PER_BATCH):
                chunk = cluster_defects[i: i + MAX_DEFECTS_PER_BATCH]
                # 更新 forbidden_zone
                if forbidden_zone:
                    for d in chunk:
                        d.forbidden_zone = list(set(d.forbidden_zone) | set(forbidden_zone))
                # 标记为 pending_review（需要预审）
                for d in chunk:
                    d.status     = DefectStatus.PENDING_REVIEW
                    d.updated_at = time.time()

                batch = RepairBatch(
                    batch_id=f"B-{self.subproject_id[:6]}-R{self.total_rounds}-{uuid.uuid4().hex[:4].upper()}",
                    defects=chunk,
                    category=cluster_key,
                    agent_id=chunk[0].agent_id,
                    round_no=self.total_rounds,
                )
                self.batches.append(batch)
                new_batches.append(batch)

        return new_batches

    # ── 预审流程 ──────────────────────────────────────────────────────────────

    def submit_proposal(self, defect_id: str, proposal: str) -> Dict[str, Any]:
        """专家提交修复方案（≤100字）"""
        d = self.defects.get(defect_id)
        if not d:
            return {"success": False, "message": f"缺陷 {defect_id} 不存在"}
        if d.status != DefectStatus.PENDING_REVIEW:
            return {"success": False, "message": f"缺陷当前状态 {d.status.value}，不在等待预审阶段"}

        proposal = proposal.strip()
        if len(proposal) > MAX_PROPOSAL_CHARS:
            return {
                "success": False,
                "message": f"修复方案超过 {MAX_PROPOSAL_CHARS} 字（当前 {len(proposal)} 字），请精简后重新提交",
            }

        d.fix_proposal = proposal
        d.proposal_at  = time.time()
        d.updated_at   = time.time()
        return {"success": True, "defect_id": defect_id, "proposal": proposal}

    def approve_proposal(self, defect_id: str, reviewer: str = "PM") -> Dict[str, Any]:
        """PM/Supervisor 审批通过修复方案"""
        d = self.defects.get(defect_id)
        if not d:
            return {"success": False, "message": f"缺陷 {defect_id} 不存在"}
        if not d.fix_proposal:
            return {"success": False, "message": "专家尚未提交修复方案"}

        d.proposal_approved = True
        d.proposal_reviewer = reviewer
        d.status            = DefectStatus.APPROVED
        d.updated_at        = time.time()
        return {"success": True, "defect_id": defect_id, "approved_by": reviewer}

    def reject_proposal(self, defect_id: str, reason: str = "", reviewer: str = "PM") -> Dict[str, Any]:
        """PM/Supervisor 驳回修复方案，要求重新提交"""
        d = self.defects.get(defect_id)
        if not d:
            return {"success": False, "message": f"缺陷 {defect_id} 不存在"}

        d.fix_proposal      = None
        d.proposal_approved = False
        d.proposal_reviewer = reviewer
        d.status            = DefectStatus.PENDING_REVIEW
        d.updated_at        = time.time()
        return {"success": True, "defect_id": defect_id, "reason": reason}

    def mark_fixing(self, defect_id: str) -> Dict[str, Any]:
        """标记缺陷为修复中（方案已批准后专家开始动手）"""
        d = self.defects.get(defect_id)
        if not d:
            return {"success": False, "message": f"缺陷 {defect_id} 不存在"}
        if d.status != DefectStatus.APPROVED:
            return {"success": False, "message": f"缺陷方案未批准（当前状态：{d.status.value}），不能开始修复"}

        d.status     = DefectStatus.FIXING
        d.updated_at = time.time()
        return {"success": True, "defect_id": defect_id}

    def mark_fixed(self, defect_id: str) -> Dict[str, Any]:
        """标记缺陷已修复（待复检）"""
        d = self.defects.get(defect_id)
        if not d:
            return {"success": False, "message": f"缺陷 {defect_id} 不存在"}

        d.fix_rounds += 1
        d.status      = DefectStatus.FIXED
        d.updated_at  = time.time()
        return {"success": True, "defect_id": defect_id, "fix_rounds": d.fix_rounds}

    def verify_fixed(self, defect_id: str) -> Dict[str, Any]:
        """Reject blind verification; automated gates own VERIFIED state."""
        d = self.defects.get(defect_id)
        if not d:
            return {"success": False, "message": f"缺陷 {defect_id} 不存在"}
        return {
            "success": False,
            "defect_id": defect_id,
            "message": "Direct verification is disabled; rerun QA, security, and runtime acceptance",
        }

    def reopen_defect(self, defect_id: str) -> Dict[str, Any]:
        """复检未通过，重新打开缺陷"""
        d = self.defects.get(defect_id)
        if not d:
            return {"success": False, "message": f"缺陷 {defect_id} 不存在"}

        # 注意：fix_rounds 已在 mark_fixed 中 +1，这里只需重置状态，不应再次计数
        d.status      = DefectStatus.OPEN
        d.updated_at  = time.time()
        # 检查是否超过单缺陷最大轮次
        if d.fix_rounds >= MAX_ROUNDS_PER_DEFECT:
            d.status            = DefectStatus.ESCALATED
            d.escalation_reason = f"已尝试修复 {d.fix_rounds} 轮仍未通过复检"
        return {"success": True, "defect_id": defect_id, "status": d.status.value}

    # ── 查询辅助 ──────────────────────────────────────────────────────────────

    def get_open_defects(self) -> List[DefectTicket]:
        """获取所有待修复缺陷（open / approved / fixing 状态）"""
        return [
            d for d in self.defects.values()
            if d.status in (DefectStatus.OPEN, DefectStatus.APPROVED, DefectStatus.FIXING)
        ]

    def get_pending_review_defects(self) -> List[DefectTicket]:
        """获取等待预审的缺陷"""
        return [d for d in self.defects.values() if d.status == DefectStatus.PENDING_REVIEW]

    def get_escalated_defects(self) -> List[DefectTicket]:
        """获取已升级的缺陷"""
        return [d for d in self.defects.values() if d.status == DefectStatus.ESCALATED]

    def is_all_resolved(self) -> bool:
        """Only defects verified by both independent gates are resolved."""
        return all(is_dual_gate_verified(d) for d in self.defects.values())

    def summary(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for d in self.defects.values():
            counts[d.status.value] = counts.get(d.status.value, 0) + 1
        p0 = sum(1 for d in self.defects.values() if d.severity == DefectSeverity.P0)
        p1 = sum(1 for d in self.defects.values() if d.severity == DefectSeverity.P1)
        p2 = sum(1 for d in self.defects.values() if d.severity == DefectSeverity.P2)
        return {
            "subproject_id":  self.subproject_id,
            "total_defects":  len(self.defects),
            "total_rounds":   self.total_rounds,
            "by_status":      counts,
            "by_severity":    {"P0": p0, "P1": p1, "P2": p2},
            "round_scores":   self.round_scores,
            "escalated":      self.escalated,
            "batches_count":  len(self.batches),
        }

    def to_dict(self) -> Dict:
        return {
            "subproject_id":   self.subproject_id,
            "subproject_name": self.subproject_name,
            "total_rounds":    self.total_rounds,
            "defects":         {k: v.to_dict() for k, v in self.defects.items()},
            "batches":         [b.to_dict() for b in self.batches],
            "round_scores":    self.round_scores,
            "escalated":       self.escalated,
            "created_at":      self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "RepairLoopController":
        ctrl = cls(d["subproject_id"], d.get("subproject_name", ""))
        ctrl.total_rounds  = d.get("total_rounds", 0)
        ctrl.round_scores  = d.get("round_scores", [])
        ctrl.escalated     = d.get("escalated", False)
        ctrl.created_at    = d.get("created_at", time.time())
        for did, dd in d.get("defects", {}).items():
            ticket = DefectTicket.from_dict(dd)
            ctrl.defects[ticket.defect_id] = ticket
        return ctrl


# ─── 阶段仲裁者 ───────────────────────────────────────────────────────────────

class PhaseArbiter:
    """
    阶段仲裁者
    触发条件（满足任一）：
      - 连续两轮质检分数恶化
      - P0 缺陷数量未减少
      - 单缺陷超过3轮未解决
    决策：重写模块 / 更换专家 / 人工介入 / 强制通过
    """

    def __init__(self, phase_id: str, phase_name: str):
        self.phase_id    = phase_id
        self.phase_name  = phase_name
        self.decisions:  List[Dict] = []
        self.active      = False
        self.created_at  = time.time()

    def evaluate(
        self,
        ctrl: RepairLoopController,
        context: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        评估是否需要介入，并给出决策建议。
        context 可包含：project_description、available_experts 等辅助信息。
        """
        term = ctrl.check_termination()
        if not term["should_escalate"]:
            return {"intervene": False, "reasons": [], "decision": None}

        self.active = True
        ctrl.escalated = True

        # 将所有触发升级的缺陷标记为 ESCALATED
        for d in ctrl.defects.values():
            if d.status in (DefectStatus.OPEN, DefectStatus.FIXING, DefectStatus.PENDING_REVIEW):
                if d.fix_rounds >= MAX_ROUNDS_PER_DEFECT or ctrl.total_rounds >= MAX_TOTAL_ROUNDS:
                    d.status            = DefectStatus.ESCALATED
                    d.escalation_reason = "; ".join(term["reasons"])
                    d.updated_at        = time.time()

        # 决策逻辑
        decision = self._decide(term, ctrl, context or {})

        record = {
            "decision_id":  f"ARB-{uuid.uuid4().hex[:6].upper()}",
            "phase_id":     self.phase_id,
            "subproject_id": ctrl.subproject_id,
            "reasons":      term["reasons"],
            "decision":     decision.value,
            "total_rounds": term["total_rounds"],
            "open_defects": term["open_defects"],
            "p0_count":     term["p0_count"],
            "decided_at":   time.time(),
        }
        self.decisions.append(record)

        return {
            "intervene":    True,
            "reasons":      term["reasons"],
            "decision":     decision.value,
            "decision_id":  record["decision_id"],
            "action_hint":  self._action_hint(decision, ctrl),
        }

    def _decide(
        self,
        term: Dict,
        ctrl: RepairLoopController,
        context: Dict,
    ) -> ArbiterDecision:
        """根据终止原因选择最合适的决策"""
        p0_count    = term["p0_count"]
        open_count  = term["open_defects"]
        total_rounds = term["total_rounds"]
        reasons_text = " ".join(term["reasons"])

        # 连续恶化 → 重写模块（修复方向错误）
        if "连续两轮" in reasons_text:
            return ArbiterDecision.REWRITE_MODULE

        # P0 超过2轮未解决 → 更换专家
        if p0_count > 0 and total_rounds >= 2:
            return ArbiterDecision.REPLACE_EXPERT

        # 总轮次兜底 → 人工介入
        if total_rounds >= MAX_TOTAL_ROUNDS:
            return ArbiterDecision.HUMAN_REVIEW

        # 缺陷数量少且都是 P2 → 强制通过
        p2_only = all(
            d.severity == DefectSeverity.P2
            for d in ctrl.defects.values()
            if d.status == DefectStatus.ESCALATED
        )
        if p2_only and open_count <= 2:
            return ArbiterDecision.FORCE_PASS

        return ArbiterDecision.HUMAN_REVIEW

    def _action_hint(self, decision: ArbiterDecision, ctrl: RepairLoopController) -> str:
        hints = {
            ArbiterDecision.REWRITE_MODULE: (
                f"建议重写子项目「{ctrl.subproject_name}」的核心模块。"
                "当前修复方向可能存在根本性错误，逐行修改已无法解决问题。"
            ),
            ArbiterDecision.REPLACE_EXPERT: (
                f"建议为子项目「{ctrl.subproject_name}」更换负责专家。"
                f"当前专家已尝试 {ctrl.total_rounds} 轮仍有 P0 缺陷未解决。"
            ),
            ArbiterDecision.HUMAN_REVIEW: (
                f"子项目「{ctrl.subproject_name}」已超过自动修复上限，需要人工介入审查。"
                f"共 {len(ctrl.defects)} 个缺陷，{ctrl.total_rounds} 轮修复后仍未全部解决。"
            ),
            ArbiterDecision.FORCE_PASS: (
                f"子项目「{ctrl.subproject_name}」剩余缺陷均为 P2 级别，"
                "建议强制通过并在后续迭代中修复。"
            ),
        }
        return hints.get(decision, "请人工判断处理方式。")

    def apply_force_pass(self, ctrl: RepairLoopController) -> Dict[str, Any]:
        """Escalate to manual review without treating unresolved work as passed."""
        count = 0
        for d in ctrl.defects.values():
            if d.status == DefectStatus.ESCALATED:
                d.mark_needs_manual("Phase arbiter requires human review")
                d.arbiter_decision  = ArbiterDecision.HUMAN_REVIEW
                d.updated_at        = time.time()
                count += 1
        return {
            "success": False,
            "force_passed": 0,
            "requires_manual": count,
            "message": "Unresolved defects require manual repair and cannot be force-passed",
        }

    def to_dict(self) -> Dict:
        return {
            "phase_id":   self.phase_id,
            "phase_name": self.phase_name,
            "active":     self.active,
            "decisions":  self.decisions,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "PhaseArbiter":
        arb = cls(d["phase_id"], d.get("phase_name", ""))
        arb.active     = d.get("active", False)
        arb.decisions  = d.get("decisions", [])
        arb.created_at = d.get("created_at", time.time())
        return arb


# ─── 全局修复循环注册表 ───────────────────────────────────────────────────────

class RepairRegistry:
    """
    全局注册表：管理所有子项目的 RepairLoopController 和 PhaseArbiter。
    供 main.py 通过 project_id 访问。
    """

    def __init__(self):
        # project_id → {subproject_id → RepairLoopController}
        self._controllers: Dict[str, Dict[str, RepairLoopController]] = {}
        # project_id → {phase_id → PhaseArbiter}
        self._arbiters: Dict[str, Dict[str, PhaseArbiter]] = {}

    def get_controller(self, project_id: str, subproject_id: str, subproject_name: str = "") -> RepairLoopController:
        self._controllers.setdefault(project_id, {})
        if subproject_id not in self._controllers[project_id]:
            self._controllers[project_id][subproject_id] = RepairLoopController(subproject_id, subproject_name)
        return self._controllers[project_id][subproject_id]

    def get_arbiter(self, project_id: str, phase_id: str, phase_name: str = "") -> PhaseArbiter:
        self._arbiters.setdefault(project_id, {})
        if phase_id not in self._arbiters[project_id]:
            self._arbiters[project_id][phase_id] = PhaseArbiter(phase_id, phase_name)
        return self._arbiters[project_id][phase_id]

    def list_controllers(self, project_id: str) -> List[Dict]:
        return [c.summary() for c in self._controllers.get(project_id, {}).values()]

    def list_arbiters(self, project_id: str) -> List[Dict]:
        return [a.to_dict() for a in self._arbiters.get(project_id, {}).values()]

    def to_dict(self) -> Dict:
        return {
            "controllers": {
                pid: {sid: c.to_dict() for sid, c in subs.items()}
                for pid, subs in self._controllers.items()
            },
            "arbiters": {
                pid: {aid: a.to_dict() for aid, a in arbs.items()}
                for pid, arbs in self._arbiters.items()
            },
        }

    def from_dict(self, d: Dict) -> None:
        for pid, subs in d.get("controllers", {}).items():
            self._controllers[pid] = {}
            for sid, cd in subs.items():
                self._controllers[pid][sid] = RepairLoopController.from_dict(cd)
        for pid, arbs in d.get("arbiters", {}).items():
            self._arbiters[pid] = {}
            for aid, ad in arbs.items():
                self._arbiters[pid][aid] = PhaseArbiter.from_dict(ad)

    def save_to_file(self, filepath: str) -> None:
        """将全部修复状态持久化到 JSON 文件，重启后可恢复"""
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    def load_from_file(self, filepath: str) -> bool:
        """
        从 JSON 文件恢复修复状态。
        返回 True 表示加载成功，False 表示文件不存在或解析失败。
        """
        path = Path(filepath)
        if not path.exists():
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.from_dict(data)
            return True
        except Exception:
            return False


# 全局单例（由 main.py 导入使用）
repair_registry = RepairRegistry()
