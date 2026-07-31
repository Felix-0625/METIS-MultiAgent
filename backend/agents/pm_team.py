"""
PM 团队 Agent 实现 v2
职责重新设计：
  PM 组长：与用户对话，完善总体规划，划分阶段，传达给阶段PM和Supervisor
  PM 成员：每人负责一个阶段的细化规划，与用户追问确认，输出专家需求
  预置10个PM成员，记忆独立，能力与组长相同
"""

import copy
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import replace
from typing import Dict, List, Optional, Any
from .base.hermes_agent import AgentBase, AgentType, Task, AGENT_PRINCIPLES
from core.hermes_client import Message, MessageRole, chat_for_json
from core.project_contract import (
    PLAN_VERSION,
    ValidationIssue,
    ValidationResult,
    artifact_metadata,
    extract_project_contract,
    finalize_project_contract,
    freeze_confirmed_project_contract,
    hydrate_plan_required_files,
    validate_plan_for_confirmation,
    validate_plan_layers,
)


MAX_PLAN_MODEL_ATTEMPTS = 3
TOTAL_PLAN_SCHEMA_VERSION = "total-plan/v1"
logger = logging.getLogger(__name__)


def _total_pm_contract(contract: Dict[str, Any]) -> Dict[str, Any]:
    """Keep source constraints while leaving task/personnel ownership to phase PMs."""
    boundary = copy.deepcopy(contract)
    boundary.update({
        "locked": False,
        "phase_count": None,
        "required_phase_count": None,
        "minimum_phase_count": None,
        "phases": [],
        "phase_task_counts": {},
        "roles": [],
        "required_files": [],
    })
    return boundary


def _finalize_total_pm_contract(
    contract: Dict[str, Any],
    plan: Dict[str, Any],
):
    locked = finalize_project_contract(_total_pm_contract(contract), plan)
    return replace(locked, required_files=())


def _total_plan_validation(
    plan: Any,
    contract: Dict[str, Any],
    *,
    allow_internal: bool = False,
) -> ValidationResult:
    """Validate only the total-PM boundary; execution details belong to phase PMs."""
    issues: List[ValidationIssue] = []

    def add(code: str, path: str, message: str, expected: Any = None, actual: Any = None) -> None:
        issues.append(ValidationIssue(
            layer="total_plan_protocol",
            code=code,
            path=path,
            message=message,
            expected=expected,
            actual=actual,
        ))

    if not isinstance(plan, dict):
        add("invalid_type", "$", "total plan must be a JSON object", "object", type(plan).__name__)
        return ValidationResult(False, "total_plan", tuple(issues), str(contract.get("contract_version") or ""), TOTAL_PLAN_SCHEMA_VERSION)

    root_fields = {"schema_version", "project_name", "summary", "phases"}
    internal_fields = {
        "project_contract", "contract_version", "plan_version", "source",
        "artifact_metadata", "status", "requirements_revision",
        "requirements_digest",
    }
    extra = sorted(set(plan) - root_fields - (internal_fields if allow_internal else set()))
    missing = sorted(root_fields - set(plan))
    if extra:
        add("unexpected_fields", "$", "total PM must not output execution-detail fields", sorted(root_fields), extra)
    if missing:
        add("missing_fields", "$", "total plan is missing required fields", sorted(root_fields), missing)
    if plan.get("schema_version") != TOTAL_PLAN_SCHEMA_VERSION:
        add("schema_version", "$.schema_version", "unsupported total-plan schema", TOTAL_PLAN_SCHEMA_VERSION, plan.get("schema_version"))
    for field in ("project_name", "summary"):
        if not isinstance(plan.get(field), str) or not plan[field].strip():
            add("non_empty_string", f"$.{field}", f"{field} must be a non-empty string")

    phases = plan.get("phases")
    if not isinstance(phases, list) or not phases:
        add("phases_empty", "$.phases", "total plan must contain at least one phase")
        phases = []
    required_count = contract.get("required_phase_count") or contract.get("phase_count")
    minimum_count = contract.get("minimum_phase_count")
    if required_count and len(phases) != int(required_count):
        add(
            "phase_count_mismatch",
            "$.phases",
            "phase count must match the confirmed user requirement",
            int(required_count),
            len(phases),
        )
    if minimum_count and len(phases) < int(minimum_count):
        add(
            "minimum_phase_count",
            "$.phases",
            "phase count is below the confirmed minimum",
            int(minimum_count),
            len(phases),
        )
    phase_fields = {
        "phase_id", "name", "objective", "work_items",
        "technical_requirements", "dependencies", "source_requirement_ids",
    }
    known_phase_ids: List[str] = []
    covered: set[str] = set()
    expected_units = {
        str(item.get("unit_id"))
        for item in (contract.get("requirement_units") or [])
        if isinstance(item, dict) and item.get("unit_id")
    }
    for index, phase in enumerate(phases):
        path = f"$.phases[{index}]"
        if not isinstance(phase, dict):
            add("invalid_type", path, "phase must be an object")
            continue
        extra = sorted(set(phase) - phase_fields)
        missing = sorted(phase_fields - set(phase))
        if extra:
            add("unexpected_fields", path, "phase contains fields owned by phase PM", sorted(phase_fields), extra)
        if missing:
            add("missing_fields", path, "phase is missing required fields", sorted(phase_fields), missing)
        expected_id = f"phase-{index + 1}"
        phase_id = str(phase.get("phase_id") or "")
        if phase_id != expected_id:
            add("phase_id_sequence", f"{path}.phase_id", "phase IDs must be sequential", expected_id, phase_id)
        for field in ("name", "objective"):
            if not isinstance(phase.get(field), str) or not phase[field].strip():
                add("non_empty_string", f"{path}.{field}", f"{field} must be a non-empty string")
        work_items = phase.get("work_items")
        if not isinstance(work_items, list) or not work_items or any(
            not isinstance(item, str) or not item.strip() for item in work_items
        ):
            add("work_items_invalid", f"{path}.work_items", "work_items must be a non-empty string array")
        for field in ("technical_requirements", "dependencies", "source_requirement_ids"):
            value = phase.get(field)
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in (value if isinstance(value, list) else [])
            ):
                add("string_array", f"{path}.{field}", f"{field} must be a string array")
        dependencies = phase.get("dependencies") if isinstance(phase.get("dependencies"), list) else []
        invalid_dependencies = [item for item in dependencies if item not in known_phase_ids]
        if invalid_dependencies:
            add("phase_dependency_order", f"{path}.dependencies", "phase dependencies may only reference earlier phases", known_phase_ids, invalid_dependencies)
        source_ids = phase.get("source_requirement_ids") if isinstance(phase.get("source_requirement_ids"), list) else []
        unknown = sorted(set(source_ids) - expected_units)
        if unknown:
            add("unknown_requirement_ids", f"{path}.source_requirement_ids", "unknown requirement unit IDs", sorted(expected_units), unknown)
        covered.update(source_ids)
        known_phase_ids.append(phase_id)
    uncovered = sorted(expected_units - covered)
    if uncovered:
        add("requirement_units_uncovered", "$.phases", "every canonical requirement unit must be assigned to a phase", sorted(expected_units), sorted(covered))
    boundary_codes = {
        "technology_missing",
        "technology_conflict",
        "forbidden_scope",
        "unresolved_option",
    }
    boundary_validation = validate_plan_layers(
        plan,
        _total_pm_contract(contract),
    )
    issues.extend(
        issue
        for issue in boundary_validation.issues
        if issue.code in boundary_codes
    )
    return ValidationResult(
        not issues,
        "total_plan",
        tuple(issues),
        str(contract.get("contract_version") or ""),
        TOTAL_PLAN_SCHEMA_VERSION,
    )


def _total_plan_fallback(contract: Dict[str, Any]) -> Dict[str, Any]:
    units = [
        item for item in (contract.get("requirement_units") or [])
        if isinstance(item, dict)
    ]
    contract_phases = [
        item for item in (contract.get("phases") or [])
        if isinstance(item, dict)
    ]
    count = max(1, int(contract.get("phase_count") or len(contract_phases) or 1))
    buckets: List[List[Dict[str, Any]]] = [[] for _ in range(count)]
    for index, unit in enumerate(units):
        buckets[min(index * count // max(len(units), 1), count - 1)].append(unit)
    phases = []
    global_tech = [
        str(item) for item in (contract.get("technology_stack") or [])
        if str(item).strip()
    ]
    for index in range(count):
        declared = contract_phases[index] if index < len(contract_phases) else {}
        assigned = buckets[index]
        work_items = [
            str(item.get("exact_text") or "").strip()
            for item in assigned
            if str(item.get("exact_text") or "").strip()
        ] or [f"完成第 {index + 1} 阶段目标"]
        phases.append({
            "phase_id": f"phase-{index + 1}",
            "name": str(declared.get("name") or f"阶段 {index + 1}"),
            "objective": "；".join(work_items),
            "work_items": work_items,
            "technical_requirements": global_tech,
            "dependencies": [] if index == 0 else [f"phase-{index}"],
            "source_requirement_ids": [
                str(item.get("unit_id")) for item in assigned if item.get("unit_id")
            ],
        })
    return {
        "schema_version": TOTAL_PLAN_SCHEMA_VERSION,
        "project_name": "项目总规划",
        "summary": str(contract.get("requirements_summary") or "依据已确认需求生成的阶段规划"),
        "phases": phases,
    }


def _model_error_record(exc: Exception) -> Dict[str, str]:
    """Classify model failures without persisting provider payloads or secrets."""
    message = str(exc).lower()
    if any(token in message for token in ("quota", "insufficient", "rate limit", "429")):
        code = "quota_or_rate_limit"
    elif any(token in message for token in ("unauthorized", "forbidden", "api key", "401", "403")):
        code = "authentication_failed"
    elif "timeout" in message:
        code = "timeout"
    else:
        code = "model_call_failed"
    return {"code": code, "error_type": type(exc).__name__}


def _extract_first_json_object(content: str) -> Optional[Dict[str, Any]]:
    """Return the first standalone decodable JSON object in model text."""
    if not isinstance(content, str):
        return None

    decoder = json.JSONDecoder()
    cursor = 0
    while cursor < len(content):
        object_start = content.find("{", cursor)
        array_start = content.find("[", cursor)
        starts = [start for start in (object_start, array_start) if start >= 0]
        if not starts:
            return None

        start = min(starts)
        try:
            value, end = decoder.raw_decode(content, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue

        if isinstance(value, dict):
            return value
        # Do not reinterpret an object nested inside a valid top-level array.
        cursor = max(end, start + 1)

    return None


# ─── PM 成员 Agent ────────────────────────────────────────────────────────────

class PMMemberAgent:
    """
    PM 成员 Agent
    职责：负责一个阶段的细化规划，与用户追问确认，输出专家需求清单
    记忆独立（每个成员有自己的 conversation_history）
    能力与 PM 组长相同
    """

    def __init__(self, member_id: str, name: str, hermes_client, memory_store=None):
        self.member_id = member_id
        self.name = name
        self.hermes = hermes_client
        self.memory = memory_store
        self.agent_id = f"pm-member-{member_id}"
        # 角色描述（与组长能力一致）
        self.role_description = (
            "PM 团队成员，负责阶段任务的细化规划，与用户追问确认，"
            "确定本阶段需要哪些专家实现哪些具体功能，输出可执行的阶段规划。"
        )
        # 独立记忆
        self.conversation_history: List[Dict] = []
        self.context_summary: str = ""
        # 当前负责的阶段
        self.assigned_phase_id: Optional[str] = None
        self.assigned_phase_name: Optional[str] = None
        self.phase_plan: Optional[Dict] = None   # 阶段细化规划
        self.phase_confirmed: bool = False
        self.status: str = "available"  # available / working / completed

        COMPRESS_THRESHOLD = 10
        KEEP_RECENT = 6
        self._compress_threshold = COMPRESS_THRESHOLD
        self._keep_recent = KEEP_RECENT

    def assign_phase(self, phase_id: str, phase_name: str, phase_description: str = "") -> None:
        """分配阶段任务"""
        self.assigned_phase_id = phase_id
        self.assigned_phase_name = phase_name
        self.phase_confirmed = False
        self.phase_plan = None
        self.status = "working"
        # 重置对话历史（新阶段新记忆）
        self.conversation_history = []
        self.context_summary = ""

    def chat(
        self,
        user_input: str,
        history: Optional[List[Dict]] = None,
        context_summary: Optional[str] = None,
        project_background: str = "",
    ) -> Dict[str, Any]:
        """
        与用户对话，细化阶段规划
        - 了解阶段具体需求
        - 追问不明确的地方
        - 确定需要哪些专家实现哪些功能
        - 用户确认后输出阶段规划
        """
        hist = history or self.conversation_history
        new_summary = context_summary or self.context_summary

        if len(hist) > self._compress_threshold:
            to_compress = hist[:-self._keep_recent]
            recent_raw = hist[-self._keep_recent:]
            # 简单压缩
            lines = [f"{'用户' if h.get('role')=='user' else 'PM'}：{h.get('content','')[:200]}" for h in to_compress]
            new_summary = (new_summary + "\n" + "\n".join(lines[-10:])) if new_summary else "\n".join(lines[-10:])
        else:
            recent_raw = hist

        # 消毒用户可控输入，防御 Prompt 注入（防止换行/JSON 注入到 system prompt）
        safe_name = self.name.replace("\n", " ").replace("{", "").replace("}", "")[:64]
        safe_phase = (self.assigned_phase_name or "").replace("\n", " ")[:128]
        # Phase PMs must receive the authoritative project/phase snapshot in
        # full; truncation can silently remove later functional constraints.
        safe_bg = project_background or ""

        phase_ctx = ""
        if self.assigned_phase_name:
            phase_ctx = f"\n\n【你负责的阶段】：{safe_phase}"
            if self.phase_plan:
                phase_ctx += f"\n【当前阶段规划草稿】：{str(self.phase_plan)[:300]}"

        system_prompt = (
            f"{AGENT_PRINCIPLES}\n\n---\n\n"
            f"你是 PM 团队成员【{safe_name}】，负责阶段任务的细化规划。\n\n"
            "【你的职责】：\n"
            "1. 根据阶段任务与用户深入沟通，追问不明确的地方\n"
            "2. 确定本阶段需要哪些专家、实现哪些具体功能\n"
            "3. 输出可执行的阶段规划（专家需求清单 + 任务描述 + 验收标准）\n"
            "4. 用户确认后在回复末尾加上【阶段规划已确认】\n\n"
            "【规则】：\n"
            "- 不明确的需求直接追问，不猜测\n"
            "- 专家需求要具体（前端专家/后端专家/数据库专家等）\n"
            "- 每个任务必须有验收标准\n"
            "- 回复简洁，不超过 400 字"
            + phase_ctx
        )
        if project_background:
            system_prompt += f"\n\n【项目背景（PM组长传达）】：\n{safe_bg}"

        messages = [Message(role=MessageRole.SYSTEM, content=system_prompt)]
        if new_summary:
            messages.append(Message(role=MessageRole.SYSTEM, content=f"对话摘要：\n{new_summary}"))
        for h in recent_raw:
            role = MessageRole.USER if h.get("role") == "user" else MessageRole.ASSISTANT
            if h.get("content"):
                messages.append(Message(role=role, content=h["content"]))
        messages.append(Message(role=MessageRole.USER, content=user_input))

        try:
            resp = self.hermes.chat(messages)
            reply = resp.get("content", "")
        except Exception:
            reply = (
                f"【{self.name} 离线模式】\n"
                f"阶段：{self.assigned_phase_name or '未分配'}\n"
                f"已收到：{user_input[:200]}\n"
                "请配置 API Key 获取智能规划对话。"
            )

        if "【阶段规划已确认】" in reply:
            self.phase_confirmed = True
            self.status = "completed"

        self.conversation_history.append({"role": "user", "content": user_input})
        self.conversation_history.append({"role": "assistant", "content": reply})
        if new_summary:
            self.context_summary = new_summary

        return {
            "reply": reply,
            "member_id": self.member_id,
            "name": self.name,
            "phase_id": self.assigned_phase_id,
            "phase_confirmed": self.phase_confirmed,
            "success": True,
        }

    def generate_expert_requirements(self, phase_description: str) -> Dict[str, Any]:
        """
        根据阶段任务生成专家需求清单（供 HR 使用）
        """
        system_prompt = (
            f"{AGENT_PRINCIPLES}\n\n---\n\n"
            "你是阶段 PM，请根据阶段任务生成专家需求清单。\n\n"
            "输出严格 JSON 格式：\n"
            "{\n"
            '  "phase_id": "...",\n'
            '  "phase_name": "...",\n'
            '  "expert_requirements": [\n'
            '    {\n'
            '      "task_id": "task-001",\n'
            '      "task_name": "任务名称",\n'
            '      "task_description": "详细描述",\n'
            '      "required_expert_type": "前端专家|后端专家|数据库专家|API设计专家|架构师|DevOps专家|安全专家|测试专家|数据专家",\n'
            '      "acceptance_criteria": ["验收标准1", "验收标准2"],\n'
            '      "priority": "high|normal|low"\n'
            "    }\n"
            "  ]\n"
            "}"
        )
        messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(role=MessageRole.USER, content=(
                f"阶段：{self.assigned_phase_name}\n"
                f"阶段描述：{phase_description}\n"
                f"对话历史摘要：{self.context_summary or '无'}"
            )),
        ]
        try:
            resp = chat_for_json(self.hermes, messages, purpose="generator")
            content = resp.get("content", "")
            parsed = _extract_first_json_object(content)
            if parsed:
                return parsed
        except Exception:
            pass
        # fallback
        return {
            "phase_id": self.assigned_phase_id or "",
            "phase_name": self.assigned_phase_name or "",
            "expert_requirements": [{
                "task_id": f"task-{self.assigned_phase_id}-001",
                "task_name": self.assigned_phase_name or "阶段任务",
                "task_description": phase_description,
                "required_expert_type": "后端专家",
                "acceptance_criteria": ["功能实现完整", "代码通过质检"],
                "priority": "normal",
            }],
        }

    def to_dict(self) -> Dict:
        return {
            "member_id": self.member_id,
            "agent_id": self.agent_id,
            "name": self.name,
            "type": "pm_member",
            "status": self.status,
            "assigned_phase_id": self.assigned_phase_id,
            "assigned_phase_name": self.assigned_phase_name,
            "phase_confirmed": self.phase_confirmed,
            "conversation_turns": len(self.conversation_history) // 2,
        }

    def to_persist(self) -> Dict:
        return {
            "member_id": self.member_id,
            "name": self.name,
            "agent_id": self.agent_id,
            "conversation_history": self.conversation_history[-30:],
            "context_summary": self.context_summary,
            "assigned_phase_id": self.assigned_phase_id,
            "assigned_phase_name": self.assigned_phase_name,
            "phase_plan": self.phase_plan,
            "phase_confirmed": self.phase_confirmed,
            "status": self.status,
        }

    def from_persist(self, data: Dict) -> None:
        self.conversation_history = data.get("conversation_history", [])
        self.context_summary = data.get("context_summary", "")
        self.assigned_phase_id = data.get("assigned_phase_id")
        self.assigned_phase_name = data.get("assigned_phase_name")
        self.phase_plan = data.get("phase_plan")
        self.phase_confirmed = data.get("phase_confirmed", False)
        self.status = data.get("status", "available")


# ─── PM 组长 Agent ────────────────────────────────────────────────────────────

class PMLeaderAgent(AgentBase):
    """
    PM 组长 Agent
    职责：
    1. 与用户对话，根据需求文档追问，完善总体规划
    2. 设计开发阶段（阶段划分）
    3. 传达阶段任务给对应的阶段PM成员和Supervisor成员
    4. 用户确认总规划后宣布"项目可以启动"
    """

    ESSENTIAL_CAPABILITIES = ["需求分析", "阶段规划", "团队协调", "用户确认"]
    COMPRESS_THRESHOLD = 10
    KEEP_RECENT = 6
    # 预置成员数量
    PRESET_MEMBER_COUNT = 10

    def __init__(self, *args, **kwargs):
        self._capabilities = list(self.ESSENTIAL_CAPABILITIES)
        self.members: Dict[str, PMMemberAgent] = {}
        self.draft_plan: Optional[Dict] = None
        self.final_plan: Optional[Dict] = None
        self.plan_confirmed: bool = False
        self.conversation_history: List[Dict] = []
        self.context_summary: str = ""
        self.member_analyses: Dict[str, Dict] = {}
        self.project_contract: Dict[str, Any] = {}
        self.last_plan_violations: List[str] = []
        self.plan_status: Optional[str] = None
        self.plan_generation: Dict[str, Any] = {}
        self.draft_blocked_reason: Optional[str] = None
        self.blocked_draft: Optional[Dict[str, Any]] = None
        self.canonical_requirements: str = ""
        self.requirements_revision: int = 0
        self.requirements_digest: str = ""
        self.requirement_events: List[Dict[str, Any]] = []
        super().__init__(*args, agent_type=AgentType.PM, **kwargs)
        # 初始化预置成员
        self._init_preset_members()

    def record_user_requirements(
        self, text: str, *, source: str = "chat",
        expected_revision: Optional[int] = None,
        expected_digest: Optional[str] = None, supersedes: Optional[List[str]] = None,
        replace: bool = False,
    ) -> Dict[str, Any]:
        """Record an explicit user revision event and rebuild its snapshot."""
        normalized = str(text or "").strip()
        if not normalized:
            raise ValueError("canonical requirements cannot be empty")
        if expected_revision is not None and expected_revision != self.requirements_revision:
            raise ValueError("requirements revision conflict")
        if expected_digest is not None and expected_digest != self.requirements_digest:
            raise ValueError("requirements digest conflict")
        normalized_source = source if source in {"chat", "attachment", "user"} else "user"
        active_ids = [
            str(event["event_id"])
            for event in self.requirement_events
            if not event.get("superseded")
        ]
        requested_supersedes = list(dict.fromkeys(str(item) for item in (supersedes or [])))
        unknown_ids = sorted(set(requested_supersedes).difference(active_ids))
        if unknown_ids:
            raise ValueError(
                "unknown or inactive requirement event ids: " + ", ".join(unknown_ids)
            )
        superseded = set(active_ids if replace else requested_supersedes)
        content_digest = "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        duplicate_active = any(
            event.get("content_digest") == content_digest
            and not event.get("superseded")
            for event in self.requirement_events
        )
        # A plain duplicate is a true no-op.  An explicit replace/supersede is
        # still a lineage change even when the replacement text already exists.
        if duplicate_active and not superseded:
            return {
                "requirements_revision": self.requirements_revision,
                "requirements_digest": self.requirements_digest,
            }
        for event in self.requirement_events:
            if event.get("event_id") in superseded:
                event["superseded"] = True
        next_revision = self.requirements_revision + 1
        event = {
            "event_id": f"reqevt-{uuid.uuid4().hex}",
            "content": normalized,
            "content_digest": content_digest,
            "source": normalized_source,
            "supersedes": sorted(superseded),
            "superseded": False,
            "revision": next_revision,
        }
        self.requirement_events.append(event)
        self.requirements_revision = next_revision
        self.canonical_requirements = "\n\n".join(
            item["content"] for item in self.requirement_events if not item.get("superseded")
        )
        self.requirements_digest = "sha256:" + hashlib.sha256(
            self.canonical_requirements.encode("utf-8")
        ).hexdigest()
        stale_plan = self.final_plan or self.draft_plan
        had_plan_artifact = (
            stale_plan is not None
            or bool(self.project_contract)
            or bool(self.plan_generation)
        )
        if stale_plan is not None:
            self.blocked_draft = copy.deepcopy(stale_plan)
        self.plan_confirmed = False
        self.final_plan = None
        self.draft_plan = None
        self.project_contract = {}
        self.plan_generation = {}
        if had_plan_artifact:
            self.plan_status = "validation_failed"
            self.draft_blocked_reason = "requirements_revision_changed"
        return {
            "requirements_revision": self.requirements_revision,
            "requirements_digest": self.requirements_digest,
        }

    @staticmethod
    def _rebuild_requirements_snapshot(
        raw_events: Any,
    ) -> tuple[List[Dict[str, Any]], str, int, str]:
        if not isinstance(raw_events, list):
            raise ValueError("persisted requirement_events must be a list")
        events = copy.deepcopy(raw_events)
        seen: set[str] = set()
        superseded_ids: set[str] = set()
        for index, event in enumerate(events, start=1):
            if not isinstance(event, dict):
                raise ValueError("persisted requirement event must be an object")
            event_id = str(event.get("event_id") or "")
            content = str(event.get("content") or "").strip()
            if not event_id or event_id in seen or not content:
                raise ValueError("persisted requirement event identity/content is invalid")
            expected_content_digest = "sha256:" + hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest()
            if str(event.get("content_digest") or "") != expected_content_digest:
                raise ValueError("persisted requirement event content digest mismatch")
            if event.get("source") not in {"chat", "attachment", "user"}:
                raise ValueError("persisted requirement event source is invalid")
            references = event.get("supersedes") or []
            if (
                not isinstance(references, list)
                or len(references) != len(set(map(str, references)))
            ):
                raise ValueError("persisted requirement event supersedes is invalid")
            normalized_references = [str(item) for item in references]
            if any(item not in seen or item in superseded_ids for item in normalized_references):
                raise ValueError("persisted requirement event supersedes unknown/inactive id")
            stored_revision = event.get("revision")
            if stored_revision is not None and int(stored_revision) != index:
                raise ValueError("persisted requirement event revision sequence mismatch")
            event["revision"] = index
            event["content"] = content
            event["supersedes"] = normalized_references
            seen.add(event_id)
            superseded_ids.update(normalized_references)
        for event in events:
            derived = str(event["event_id"]) in superseded_ids
            if "superseded" in event and bool(event["superseded"]) != derived:
                raise ValueError("persisted requirement event active state mismatch")
            event["superseded"] = derived
        canonical = "\n\n".join(
            str(event["content"]) for event in events if not event["superseded"]
        )
        revision = len(events)
        digest = (
            "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if canonical else ""
        )
        return events, canonical, revision, digest

    def _init_preset_members(self) -> None:
        """初始化10个预置PM成员（如果还没有的话）"""
        if self.members:
            return
        for i in range(1, self.PRESET_MEMBER_COUNT + 1):
            mid = f"preset-{i:02d}"
            self.members[mid] = PMMemberAgent(
                member_id=mid,
                name=f"PM成员{i:02d}",
                hermes_client=self.hermes,
            )

    def add_member(self) -> PMMemberAgent:
        """新增一个PM成员（能力与其他成员相同，记忆独立）"""
        new_id = f"custom-{uuid.uuid4().hex[:6]}"
        # 用当前自定义成员数量计算编号，避免与预置成员编号冲突
        custom_count = sum(1 for mid in self.members if mid.startswith("custom-"))
        idx = custom_count + 1
        member = PMMemberAgent(
            member_id=new_id,
            name=f"PM成员-新增{idx:02d}",
            hermes_client=self.hermes,
        )
        self.members[new_id] = member
        return member

    def remove_member(self, member_id: str) -> Dict[str, Any]:
        """
        删除PM成员（CCB保护：组长不可删，成员数不得少于4）
        """
        if member_id not in self.members:
            return {"success": False, "message": f"成员 {member_id} 不存在"}
        if len(self.members) <= 4:
            return {"success": False, "message": "PM团队成员数不得少于4人，无法删除"}
        del self.members[member_id]
        return {"success": True, "message": f"成员 {member_id} 已删除"}

    def get_members_info(self) -> List[Dict]:
        return [m.to_dict() for m in self.members.values()]

    def assign_phase_to_member(self, phase_id: str, phase_name: str, phase_description: str = "") -> Optional[PMMemberAgent]:
        """将阶段任务分配给一个空闲的PM成员"""
        existing = self.get_member_for_phase(phase_id)
        if existing:
            return existing
        # 优先找空闲成员
        for member in self.members.values():
            if member.status == "available":
                member.assign_phase(phase_id, phase_name, phase_description)
                return member
        # 没有空闲成员，找已完成的成员
        for member in self.members.values():
            if member.status == "completed":
                member.assign_phase(phase_id, phase_name, phase_description)
                return member
        # 每个阶段必须拥有独立 PM；成员不足时扩容，禁止复用正在负责其他阶段的成员。
        member = self.add_member()
        member.assign_phase(phase_id, phase_name, phase_description)
        return member

    def get_member_for_phase(self, phase_id: str) -> Optional[PMMemberAgent]:
        """获取负责某阶段的PM成员"""
        for member in self.members.values():
            if member.assigned_phase_id == phase_id:
                return member
        return None

    def chat_with_user(
        self,
        user_input: str,
        history: Optional[List[Dict]] = None,
        context_summary: Optional[str] = None,
    ) -> Dict[str, Any]:
        """PM组长与用户对话，完善总体规划"""
        hist = history or self.conversation_history
        new_summary = context_summary or self.context_summary

        if len(hist) > self.COMPRESS_THRESHOLD:
            to_compress = hist[:-self.KEEP_RECENT]
            recent_raw = hist[-self.KEEP_RECENT:]
            compressed = self._compress_history(to_compress)
            new_summary = (new_summary + "\n\n【新增摘要】\n" + compressed) if new_summary else compressed
        else:
            recent_raw = hist

        plan_ctx = ""
        if self.draft_plan:
            phases = self.draft_plan.get("phases", [])
            phases_text = "\n".join(f"  - {p.get('name','')}: {p.get('description','')[:60]}" for p in phases)
            plan_ctx = f"\n\n【当前总规划草稿】\n概述：{self.draft_plan.get('project_overview','')[:200]}\n阶段：\n{phases_text}"

        system_prompt = (
            f"{AGENT_PRINCIPLES}\n\n---\n\n"
            "你是 PM 团队组长，负责与用户沟通，完善项目总体规划并划分开发阶段。\n\n"
            "【你的职责】：\n"
            "1. 根据用户提供的需求文档，追问不明确的地方，完善总体规划\n"
            "2. 设计合理的开发阶段（每个阶段有明确目标和交付物）\n"
            "3. 用户确认总规划后，传达阶段任务给各阶段PM成员\n"
            "4. 用户确认后在回复末尾加上【总规划已确认】\n\n"
            "【规则】：\n"
            "- 所有阶段工期填'待定'，不估算具体时间\n"
            "- 不明确的需求直接追问\n"
            "- 用户说'确认''好的''可以'等时，加上【总规划已确认】\n"
            "- 回复简洁，不超过 500 字"
            + plan_ctx
        )

        messages = [Message(role=MessageRole.SYSTEM, content=system_prompt)]
        if new_summary:
            messages.append(Message(role=MessageRole.SYSTEM, content=f"对话摘要：\n{new_summary}"))
        for h in recent_raw:
            role = MessageRole.USER if h.get("role") == "user" else MessageRole.ASSISTANT
            if h.get("content"):
                messages.append(Message(role=role, content=h["content"]))
        messages.append(Message(role=MessageRole.USER, content=user_input))

        llm_error = None
        try:
            resp = self.hermes.chat(messages)
            reply = resp.get("content", "")
        except Exception as e:
            llm_error = str(e)
            reply = (
                f"【PM组长 离线模式】\n已收到：{user_input[:200]}\n"
                "请配置 API Key 获取智能规划对话。"
            )

        # LLM 调用失败时，不应标记为成功，也不应更新 plan_confirmed
        if llm_error:
            self.conversation_history.append({"role": "user", "content": user_input})
            return {
                "reply": reply,
                "analysis": "",
                "success": False,
                "summary": new_summary or "",
                "plan_confirmed": False,
                "has_draft_plan": self.draft_plan is not None,
                "error": llm_error,
            }

        self.conversation_history.append({"role": "user", "content": user_input})
        self.conversation_history.append({"role": "assistant", "content": reply})
        if new_summary:
            self.context_summary = new_summary
        if len(self.conversation_history) > self.KEEP_RECENT * 2:
            self.conversation_history = self.conversation_history[-(self.KEEP_RECENT * 2):]

        return {
            "reply": reply,
            "analysis": reply,
            "success": True,
            "summary": new_summary or "",
            "plan_confirmed": self.plan_confirmed,
            "has_draft_plan": self.draft_plan is not None,
        }

    def synthesize_plan_fast(
        self,
        requirements: str,
        requirements_revision: Optional[int] = None,
        requirements_digest: Optional[str] = None,
    ) -> Dict[str, Any]:
        bound_revision = (
            self.requirements_revision
            if requirements_revision is None
            else requirements_revision
        )
        bound_digest = (
            self.requirements_digest
            if requirements_digest is None
            else requirements_digest
        )
        """Generate and save a contract-valid plan with bounded model attempts."""
        contract = dict(extract_project_contract(requirements))
        lineage = [
            {
                "event_id": str(event.get("event_id") or ""),
                "content_digest": str(event.get("content_digest") or ""),
                "source": str(event.get("source") or ""),
                "supersedes": list(event.get("supersedes") or []),
                "revision": int(event.get("revision") or 0),
            }
            for event in self.requirement_events
        ]
        contract.update({
            "requirements_revision": bound_revision,
            "requirements_digest": bound_digest,
            "requirement_event_ids": [
                item["event_id"] for item in lineage if item["event_id"]
            ],
            "requirement_lineage_digest": (
                "sha256:" + hashlib.sha256(
                    json.dumps(
                        lineage,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            ),
        })
        prompt_contract = dict(contract)
        prompt_contract.pop("source_requirements", None)
        prompt_contract["requirement_units"] = [
            {
                "unit_id": item.get("unit_id"),
                "exact_text": item.get("exact_text"),
                "kind": item.get("kind"),
                "binding": item.get("binding"),
            }
            for item in (contract.get("requirement_units") or [])
        ]
        self.plan_status = "generating"
        self.draft_blocked_reason = None
        started_at = time.time()
        attempts: List[Dict[str, Any]] = []
        system_prompt = (
            "你是 MeTis 总 PM。你只负责把已确认需求划分为一个或多个阶段，"
            "说明每个阶段的目标、要完成的工作和阶段级技术要求。"
            "不要生成具体任务 ID、实现方式、交付文件、文件路径、角色、人员、工期、"
            "验收命令、依赖包或代码细节；这些由阶段 PM 后续与用户确认。\n"
            "阶段数量必须由需求决定：用户明确指定时严格遵守，否则选择合理数量；"
            "phases 是动态数组，可包含任意正整数个阶段。"
            "technical_requirements 只写用户已确认或阶段必须继承的技术约束，"
            "没有要求时必须输出空数组 []，不得编造技术选型。\n"
            "每个 requirement_units.unit_id 必须原样写入某个阶段的 "
            "source_requirement_ids；work_items 应覆盖对应 exact_text。"
            "dependencies 只能引用排在当前阶段之前的 phase_id。\n"
            f"已确认需求文件：{json.dumps(prompt_contract, ensure_ascii=False)}\n"
            "只允许输出下列固定 JSON 字段，不得增加字段，不要 Markdown 或解释：\n"
            '{"schema_version":"total-plan/v1","project_name":"项目名",'
            '"summary":"总规划摘要","phases":[{"phase_id":"phase-1",'
            '"name":"阶段名","objective":"阶段目标",'
            '"work_items":["本阶段要完成的工作"],'
            '"technical_requirements":["阶段技术要求；无要求时为 []"],'
            '"dependencies":[],"source_requirement_ids":["req-..."]}]}'
        )
        base_messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(
                role=MessageRole.USER,
                content="依据上述已确认需求文件生成总规划。",
            ),
        ]
        plan_data: Optional[Dict[str, Any]] = None
        validation = None
        previous_issues: List[Dict[str, Any]] = []
        previous_candidate: Optional[Dict[str, Any]] = None
        model_failed = False

        for attempt in range(MAX_PLAN_MODEL_ATTEMPTS):
            messages = list(base_messages)
            if previous_issues:
                if previous_candidate is not None:
                    messages.append(Message(
                        role=MessageRole.ASSISTANT,
                        content=json.dumps(previous_candidate, ensure_ascii=False),
                    ))
                messages.append(Message(
                    role=MessageRole.USER,
                    content=(
                        "上一结果不符合 total-plan/v1。只修正以下协议问题并重新输出完整 JSON："
                        + json.dumps(previous_issues, ensure_ascii=False)
                    ),
                ))
            try:
                chat_kwargs = {
                    "use_cache": False,
                    "request_timeout": 30,
                    "max_tokens": 4000,
                    "temperature": 0.1,
                }
                try:
                    response = self.hermes.chat(messages, **chat_kwargs)
                except TypeError as exc:
                    error_text = str(exc)
                    unsupported_kwarg = (
                        "unexpected keyword argument" in error_text
                        and any(name in error_text for name in chat_kwargs)
                    )
                    if not unsupported_kwarg:
                        raise
                    response = self.hermes.chat(messages)
                content = str(response.get("content", ""))
            except Exception as exc:
                model_failed = True
                logger.warning(
                    "PM plan model call failed attempt=%s code=%s error_type=%s",
                    attempt + 1,
                    _model_error_record(exc).get("code", "model_call_failed"),
                    type(exc).__name__,
                )
                attempts.append({
                    "attempt": attempt + 1, "status": "model_failed", **_model_error_record(exc),
                })
                break

            candidate = _extract_first_json_object(content)
            if not isinstance(candidate, dict):
                previous_issues = [{
                    "layer": "json_schema", "code": "invalid_json", "path": "$",
                    "message": "model response did not contain a valid JSON object",
                }]
                attempts.append({"attempt": attempt + 1, "status": "validation_failed", "issues": previous_issues})
                continue

            validation = _total_plan_validation(candidate, contract)
            if not validation.valid:
                previous_candidate = copy.deepcopy(candidate)
                previous_issues = [
                    {
                        key: value
                        for key, value in issue.items()
                        if key in {"code", "path", "message", "expected", "actual"}
                        and value is not None
                    }
                    for issue in validation.to_dict()["issues"]
                ]
                attempts.append({"attempt": attempt + 1, "status": "validation_failed", "issues": previous_issues})
                continue

            source = "model" if attempt == 0 else "model_repaired"
            attempts.append({"attempt": attempt + 1, "status": "generated", "validation": validation.to_dict()})
            candidate_contract = _finalize_total_pm_contract(contract, candidate)
            candidate["project_contract"] = candidate_contract.as_mapping()
            candidate["contract_version"] = candidate_contract.contract_version
            candidate["plan_version"] = PLAN_VERSION
            candidate["source"] = source
            candidate["artifact_metadata"] = artifact_metadata(
                "plan", PLAN_VERSION, source, validation,
                [{"type": "model_retry", "attempt": attempt + 1}] if attempt else (),
            )
            candidate["artifact_metadata"].update({"created_at": started_at, "saved_at": time.time()})
            candidate["status"] = "saved"
            plan_data = candidate
            break

        if plan_data is None:
            fallback = _total_plan_fallback(contract)
            validation = _total_plan_validation(fallback, contract)
            fallback_contract = _finalize_total_pm_contract(contract, fallback)
            fallback["project_contract"] = fallback_contract.as_mapping()
            fallback["contract_version"] = fallback_contract.contract_version
            fallback["plan_version"] = PLAN_VERSION
            fallback["source"] = "deterministic_contract_fallback"
            if validation.valid:
                correction = {
                    "type": "deterministic_contract_fallback",
                    "reason": "model_failed" if model_failed else "validation_failed",
                    "rejected_attempts": attempts,
                }
                fallback["artifact_metadata"] = artifact_metadata(
                    "plan", PLAN_VERSION, "deterministic_contract_fallback", validation, [correction],
                )
                fallback["artifact_metadata"].update({"created_at": started_at, "saved_at": time.time()})
                fallback["status"] = "saved"
                plan_data = fallback

        self.last_plan_violations = validation.violations if validation else ["plan validation did not run"]
        generated_by_model = bool(
            plan_data
            and plan_data.get("source") in {"model", "model_repaired"}
        )
        self.plan_generation = {
            "status": "saved" if plan_data is not None else ("model_failed" if model_failed else "validation_failed"),
            "model_status": (
                "generated"
                if generated_by_model
                else ("model_failed" if model_failed else "validation_failed")
            ),
            "attempts": attempts,
            "validation": validation.to_dict() if validation else None,
            "requirements_revision": bound_revision,
            "requirements_digest": bound_digest,
            "updated_at": time.time(),
        }
        if plan_data is None:
            self.plan_status = self.plan_generation["status"]
            return {
                "success": False, "status": self.plan_status,
                "message": "Planning output failed deterministic contract validation",
                "draft_plan": self.draft_plan, "member_analyses": {},
                "violations": self.last_plan_violations,
                "validation": self.plan_generation["validation"], "generation": self.plan_generation,
                "can_confirm": False, "fast_mode": True,
            }

        self.project_contract = _total_pm_contract(contract)
        plan_data.setdefault("artifact_metadata", {})
        plan_data["artifact_metadata"].update({
            "requirements_revision": bound_revision,
            "requirements_digest": bound_digest,
        })
        plan_data["requirements_revision"] = bound_revision
        plan_data["requirements_digest"] = bound_digest
        self.draft_plan = plan_data
        self.final_plan = None
        self.plan_confirmed = False
        self.plan_status = "saved"
        return {
            "success": True, "status": "saved", "draft_plan": plan_data,
            "member_analyses": {}, "validation": validation.to_dict(),
            "generation": self.plan_generation, "can_confirm": True, "fast_mode": True,
        }

    def synthesize_plan(
        self,
        requirements: str,
        requirements_revision: Optional[int] = None,
        requirements_digest: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.synthesize_plan_fast(
            requirements,
            requirements_revision,
            requirements_digest,
        )

    def collect_member_analyses(self, requirements: str, context: str = "") -> Dict[str, Any]:
        return {"success": True, "analyses": {}, "members": self.get_members_info()}

    def _sanitize_durations(self, plan_data: Dict) -> Dict:
        if not isinstance(plan_data, dict):
            return plan_data
        plan_data["total_duration"] = "待定"
        phases = [
            phase for phase in plan_data.get("phases", [])
            if isinstance(phase, dict)
        ]
        unique_task_by_phase: Dict[str, str] = {}
        for phase in phases:
            tasks = phase.get("tasks") or phase.get("task_contract") or []
            task_ids = [
                str(task.get("task_id") or "").strip()
                for task in tasks
                if isinstance(task, dict)
                and str(task.get("task_id") or "").strip()
            ]
            phase_id = str(phase.get("phase_id") or "").strip()
            if phase_id and len(task_ids) == 1:
                unique_task_by_phase[phase_id] = task_ids[0]
        for phase in phases:
            if isinstance(phase, dict):
                phase["duration"] = "待定"
                for task in (
                    phase.get("tasks")
                    or phase.get("task_contract")
                    or []
                ):
                    if not isinstance(task, dict):
                        continue
                    dependencies = task.get("dependencies")
                    if not isinstance(dependencies, list):
                        continue
                    task["dependencies"] = list(dict.fromkeys(
                        unique_task_by_phase.get(
                            str(dependency),
                            dependency,
                        )
                        for dependency in dependencies
                    ))
        task_phase_by_id = {
            str(task.get("task_id") or "").strip(): str(
                phase.get("phase_id") or ""
            ).strip()
            for phase in phases
            for task in (
                phase.get("tasks")
                or phase.get("task_contract")
                or []
            )
            if isinstance(task, dict)
            and str(task.get("task_id") or "").strip()
        }
        for phase in phases:
            phase_id = str(phase.get("phase_id") or "").strip()
            inferred = [
                dependency_phase
                for task in (
                    phase.get("tasks")
                    or phase.get("task_contract")
                    or []
                )
                if isinstance(task, dict)
                for dependency in (task.get("dependencies") or [])
                if (
                    (dependency_phase := task_phase_by_id.get(
                        str(dependency),
                        "",
                    ))
                    and dependency_phase != phase_id
                )
            ]
            phase["dependencies"] = list(dict.fromkeys([
                *(phase.get("dependencies") or []),
                *inferred,
            ]))
        return plan_data

    def _fallback_plan(self, requirements: str) -> Dict:
        return {
            "project_overview": f"基于需求「{requirements[:100]}」的项目方案",
            "core_features": ["用户管理", "核心业务功能", "数据管理"],
            "tech_stack": {"frontend": "待确认", "backend": "待确认", "database": "待确认", "deploy": "待确认"},
            "phases": [
                {"phase_id": "phase-1", "name": "基础架构阶段", "description": "搭建项目基础框架", "duration": "待定", "deliverables": ["项目框架", "数据库Schema"], "roles_needed": ["后端专家", "架构师"], "agent_count": 2},
                {"phase_id": "phase-2", "name": "核心功能开发阶段", "description": "实现核心业务功能", "duration": "待定", "deliverables": ["核心功能代码", "API文档"], "roles_needed": ["前端专家", "后端专家"], "agent_count": 3},
                {"phase_id": "phase-3", "name": "测试部署阶段", "description": "集成测试和部署", "duration": "待定", "deliverables": ["测试报告", "部署文档"], "roles_needed": ["测试专家", "DevOps专家"], "agent_count": 2},
            ],
            "subprojects": [
                {"id": "sp-001", "name": "基础架构搭建", "description": "项目框架、数据库", "phase_id": "phase-1", "roles_needed": ["后端专家"], "tech_stack": [], "priority": "high"},
                {"id": "sp-002", "name": "前端界面开发", "description": "用户界面", "phase_id": "phase-2", "roles_needed": ["前端专家"], "tech_stack": [], "priority": "high"},
                {"id": "sp-003", "name": "后端API开发", "description": "业务逻辑API", "phase_id": "phase-2", "roles_needed": ["后端专家"], "tech_stack": [], "priority": "high"},
            ],
            "risks": ["需求变更风险", "技术难度风险"],
            "total_duration": "待定",
        }

    def confirm_plan(
        self,
        modifications: str = "",
        required_files: Optional[List[Dict[str, Any]]] = None,
        expected_revision: Optional[int] = None,
        expected_digest: Optional[str] = None,
    ) -> Dict[str, Any]:
        if (
            expected_revision is not None
            and expected_revision != self.requirements_revision
        ) or (
            expected_digest is not None and expected_digest != self.requirements_digest
        ):
            return {
                "success": False,
                "status": "requirements_revision_conflict",
                "message": "Canonical requirements changed; regenerate the plan",
                "plan": None,
                "validation": {"valid": False, "issues": [{
                    "layer": "project_contract",
                    "code": "requirements_revision_conflict",
                    "path": "$.requirements_revision",
                    "message": "Draft confirmation CAS does not match canonical requirements",
                    "expected": {
                        "requirements_revision": self.requirements_revision,
                        "requirements_digest": self.requirements_digest,
                    },
                    "actual": {
                        "requirements_revision": expected_revision,
                        "requirements_digest": expected_digest,
                    },
                }]},
                "can_launch": False,
            }
        generated_revision = self.plan_generation.get("requirements_revision")
        generated_digest = self.plan_generation.get("requirements_digest")
        if self.requirements_revision and (
            generated_revision != self.requirements_revision
            or generated_digest != self.requirements_digest
        ):
            self.plan_confirmed = False
            self.final_plan = None
            self.plan_status = "validation_failed"
            return {
                "success": False,
                "status": "requirements_revision_conflict",
                "message": "Draft was generated from a stale requirements revision",
                "plan": None,
                "validation": {"valid": False, "issues": [{
                    "layer": "project_contract",
                    "code": "stale_requirements_revision",
                    "path": "$.plan_generation",
                    "message": "Regenerate the draft from the current canonical requirements",
                    "expected": {
                        "requirements_revision": self.requirements_revision,
                        "requirements_digest": self.requirements_digest,
                    },
                    "actual": {
                        "requirements_revision": generated_revision,
                        "requirements_digest": generated_digest,
                    },
                }]},
                "can_launch": False,
            }
        if self.plan_confirmed and self.final_plan:
            return {
                "success": False,
                "status": "contract_locked",
                "message": "Confirmed ProjectContract is immutable; create a new contract version",
                "plan": self.final_plan,
                "validation": {"valid": False, "issues": [{
                    "layer": "project_contract", "code": "contract_version_locked", "path": "$.project_contract",
                    "message": "Confirmed ProjectContract cannot be overwritten in place",
                    "expected": (self.final_plan.get("project_contract") or {}).get("contract_version"),
                }]},
                "can_launch": True,
            }
        if not self.draft_plan:
            self.plan_confirmed = False
            self.final_plan = None
            self.plan_status = "validation_failed"
            return {
                "success": False,
                "status": "validation_failed",
                "message": "No draft plan available to confirm",
                "plan": None,
                "validation": {"valid": False, "issues": [{
                    "layer": "json_schema", "code": "missing_draft", "path": "$",
                    "message": "No draft plan available to confirm",
                }]},
                "can_launch": False,
            }
        if modifications:
            # Free-form modifications cannot silently mutate the executable
            # contract.  The caller must regenerate a structured draft first.
            self.plan_confirmed = False
            self.final_plan = None
            self.plan_status = "validation_failed"
            return {
                "success": False,
                "status": "validation_failed",
                "message": "Structured plan must be regenerated after modifications",
                "plan": self.draft_plan,
                "validation": {"valid": False, "issues": [{
                    "layer": "project_contract", "code": "unstructured_modification", "path": "$",
                    "message": "Free-form modifications require a newly validated structured draft",
                }]},
                "can_launch": False,
            }
        candidate = copy.deepcopy(self.draft_plan)
        # The contract captured from the user's requirements is authoritative.
        # A draft/model response may not replace or omit that source boundary.
        base_contract = self.project_contract or candidate.get("project_contract")
        expected_source_requirements = str(
            (base_contract or {}).get("source_requirements") or ""
        )
        if candidate.get("schema_version") == TOTAL_PLAN_SCHEMA_VERSION:
            locked_contract = _finalize_total_pm_contract(
                dict(base_contract or {}),
                candidate,
            )
            candidate["project_contract"] = locked_contract.as_mapping()
            candidate["contract_version"] = locked_contract.contract_version
            validation = _total_plan_validation(
                candidate,
                dict(base_contract or {}),
                allow_internal=True,
            )
        else:
            locked_contract = finalize_project_contract(
                base_contract or {},
                candidate,
                required_files,
            )
            candidate["project_contract"] = locked_contract.as_mapping()
            candidate["contract_version"] = locked_contract.contract_version
            candidate["required_files"] = [
                item.to_dict() for item in locked_contract.required_files
            ]
            hydrate_plan_required_files(candidate, locked_contract)
            validation = validate_plan_for_confirmation(
                candidate,
                locked_contract,
                expected_source_requirements=expected_source_requirements,
            )
        self.last_plan_violations = validation.violations
        if not validation.valid:
            self.plan_confirmed = False
            self.final_plan = None
            self.plan_status = "validation_failed"
            return {
                "success": False,
                "status": "validation_failed",
                "message": "Plan does not satisfy the confirmed project contract",
                "plan": self.draft_plan,
                "violations": validation.violations,
                "validation": validation.to_dict(),
                "can_launch": False,
            }
        self.draft_plan = candidate
        self.project_contract = candidate["project_contract"]
        self.plan_confirmed = True
        self.plan_status = "confirmed"
        self.draft_plan["status"] = "confirmed"
        self.draft_plan.setdefault("artifact_metadata", artifact_metadata(
            "plan", PLAN_VERSION, str(self.draft_plan.get("source") or "legacy_validated"), validation,
        ))
        self.draft_plan["artifact_metadata"]["validation"] = validation.to_dict()
        self.draft_plan["artifact_metadata"]["confirmed_at"] = time.time()
        self.final_plan = self.draft_plan
        return {
            "success": True, "status": "confirmed", "message": "方案已确认！项目可以启动了",
            "plan": self.final_plan, "validation": validation.to_dict(), "can_launch": True,
        }

    def get_final_plan_for_hr(self) -> Optional[Dict]:
        if not self.plan_confirmed or not self.final_plan:
            return None
        return {
            "schema_version": self.final_plan.get("schema_version"),
            "project_name": self.final_plan.get("project_name", ""),
            "summary": self.final_plan.get("summary", ""),
            "phases": self.final_plan.get("phases", []),
            "subprojects": self.final_plan.get("subprojects", []),
            "tech_stack": self.final_plan.get("tech_stack", {}),
            "total_duration": self.final_plan.get("total_duration", ""),
            "project_contract": self.final_plan.get("project_contract", self.project_contract),
            "plan_version": self.final_plan.get("plan_version", PLAN_VERSION),
            "source": self.final_plan.get("source", "legacy_validated"),
            "artifact_metadata": self.final_plan.get("artifact_metadata", {}),
            "confirmed_at": time.time(),
        }

    def get_status(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "type": "pm_leader",
            "state": self.state.value,
            "members_count": len(self.members),
            "has_draft_plan": self.draft_plan is not None,
            "plan_confirmed": self.plan_confirmed,
            "plan_status": self.plan_status,
            "plan_generation": self.plan_generation,
            "draft_blocked_reason": self.draft_blocked_reason,
            "conversation_turns": len(self.conversation_history) // 2,
        }

    def to_persist(self) -> Dict:
        return {
            "draft_plan": self.draft_plan,
            "final_plan": self.final_plan,
            "plan_confirmed": self.plan_confirmed,
            "member_analyses": self.member_analyses,
            "conversation_history": self.conversation_history[-40:],
            "context_summary": self.context_summary,
            "project_contract": self.project_contract,
            "last_plan_violations": self.last_plan_violations,
            "plan_status": self.plan_status,
            "plan_generation": self.plan_generation,
            "draft_blocked_reason": self.draft_blocked_reason,
            "blocked_draft": self.blocked_draft,
            "canonical_requirements": self.canonical_requirements,
            "requirements_revision": self.requirements_revision,
            "requirements_digest": self.requirements_digest,
            "requirement_events": self.requirement_events,
            "members": {mid: m.to_persist() for mid, m in self.members.items()},
        }

    def from_persist(self, data: Dict) -> None:
        loaded_draft = data.get("draft_plan")
        loaded_final = data.get("final_plan")
        self.draft_plan = loaded_draft
        self.final_plan = loaded_final
        self.plan_confirmed = bool(data.get("plan_confirmed", False))
        self.member_analyses = data.get("member_analyses", {})
        self.conversation_history = data.get("conversation_history", [])
        self.context_summary = data.get("context_summary", "")
        self.project_contract = data.get("project_contract") or (self.final_plan or {}).get("project_contract", {})
        self.last_plan_violations = data.get("last_plan_violations", [])
        self.plan_generation = data.get("plan_generation", {})
        self.draft_blocked_reason = data.get("draft_blocked_reason")
        self.blocked_draft = data.get("blocked_draft")
        (
            rebuilt_events,
            rebuilt_canonical,
            rebuilt_revision,
            rebuilt_digest,
        ) = self._rebuild_requirements_snapshot(data.get("requirement_events") or [])
        stored_canonical = str(data.get("canonical_requirements") or "")
        stored_revision = int(data.get("requirements_revision") or 0)
        stored_digest = str(data.get("requirements_digest") or "")
        if (
            stored_canonical != rebuilt_canonical
            or stored_revision != rebuilt_revision
            or stored_digest != rebuilt_digest
        ):
            raise ValueError("persisted canonical requirements snapshot mismatch")
        self.requirement_events = rebuilt_events
        self.canonical_requirements = rebuilt_canonical
        self.requirements_revision = rebuilt_revision
        self.requirements_digest = rebuilt_digest
        self.plan_status = data.get("plan_status") or ("confirmed" if self.plan_confirmed else "saved")

        requirements_bound_plan = (
            self.final_plan if self.plan_confirmed and self.final_plan else self.draft_plan
        )
        if self.requirements_revision and isinstance(requirements_bound_plan, dict):
            metadata = requirements_bound_plan.get("artifact_metadata") or {}
            bound_revision = requirements_bound_plan.get(
                "requirements_revision", metadata.get("requirements_revision")
            )
            bound_digest = requirements_bound_plan.get(
                "requirements_digest", metadata.get("requirements_digest")
            )
            if (
                bound_revision != self.requirements_revision
                or bound_digest != self.requirements_digest
            ):
                self.blocked_draft = requirements_bound_plan
                self.draft_plan = None
                self.final_plan = None
                self.project_contract = {}
                self.plan_generation = {}
                self.plan_confirmed = False
                self.plan_status = "validation_failed"
                self.draft_blocked_reason = "persisted_plan_requirements_binding_mismatch"

        # Legacy artifacts are never trusted merely because they were loaded
        # from disk. Valid artifacts receive explicit migration metadata;
        # invalid ones are retained as blocked history but removed from the
        # active plan pointers so they cannot contaminate a new project run.
        candidate = self.final_plan if self.plan_confirmed and self.final_plan else self.draft_plan
        if isinstance(candidate, dict):
            contract_version = int(
                (self.project_contract or candidate.get("project_contract") or {}).get(
                    "contract_version", 2
                )
            )
            if self.plan_confirmed and contract_version < 3:
                legacy_contract = (
                    self.project_contract
                    or candidate.get("project_contract")
                    or {}
                )
                source_requirements = str(
                    legacy_contract.get("source_requirements") or ""
                ).strip()
                migrated_candidate: Optional[Dict[str, Any]] = None
                if source_requirements:
                    content_digest = "sha256:" + hashlib.sha256(
                        source_requirements.encode("utf-8")
                    ).hexdigest()
                    event = {
                        "event_id": "reqevt-legacy-" + content_digest[7:39],
                        "content": source_requirements,
                        "content_digest": content_digest,
                        "source": "user",
                        "supersedes": [],
                        "superseded": False,
                        "revision": 1,
                    }
                    lineage = [{
                        "event_id": event["event_id"],
                        "content_digest": content_digest,
                        "source": "user",
                        "supersedes": [],
                        "revision": 1,
                    }]
                    upgraded_contract = dict(
                        extract_project_contract(source_requirements)
                    )
                    upgraded_contract.update({
                        "requirements_revision": 1,
                        "requirements_digest": content_digest,
                        "requirement_event_ids": [event["event_id"]],
                        "requirement_lineage_digest": (
                            "sha256:" + hashlib.sha256(
                                json.dumps(
                                    lineage,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                            ).hexdigest()
                        ),
                    })
                    proposal = copy.deepcopy(candidate)
                    proposal["project_contract"] = upgraded_contract
                    proposal["contract_version"] = 3
                    proposal["requirements_revision"] = 1
                    proposal["requirements_digest"] = content_digest
                    proposal.setdefault("artifact_metadata", {}).update({
                        "requirements_revision": 1,
                        "requirements_digest": content_digest,
                        "legacy_contract_migration": "trusted_source_requirements",
                    })
                    migration_validation = validate_plan_layers(
                        proposal, upgraded_contract
                    )
                    if migration_validation.valid:
                        migrated_candidate = proposal
                        self.requirement_events = [event]
                        self.canonical_requirements = source_requirements
                        self.requirements_revision = 1
                        self.requirements_digest = content_digest
                        self.project_contract = upgraded_contract
                        self.final_plan = proposal
                        if self.draft_plan is not None:
                            self.draft_plan = proposal
                        self.plan_generation = {
                            "status": "confirmed",
                            "source": "legacy_trusted_migration",
                            "requirements_revision": 1,
                            "requirements_digest": content_digest,
                            "validation": migration_validation.to_dict(),
                            "updated_at": time.time(),
                        }
                if migrated_candidate is not None:
                    candidate = migrated_candidate
                    contract_version = 3
                else:
                    self.blocked_draft = copy.deepcopy(candidate)
                    self.draft_plan = None
                    self.final_plan = None
                    self.project_contract = {}
                    self.plan_confirmed = False
                    self.plan_status = "validation_failed"
                    self.draft_blocked_reason = (
                        "legacy_confirmed_requires_regeneration"
                    )
                    self.last_plan_violations = [
                        "confirmed legacy plan lacks a trusted, current "
                        "ProjectContract v3 requirements lineage"
                    ]
                    self.plan_generation = {
                        "status": "validation_failed",
                        "source": "legacy_blocked",
                        "updated_at": time.time(),
                    }
                    candidate = None
            if (
                isinstance(candidate, dict)
                and not self.plan_confirmed
                and contract_version < 3
            ):
                self.blocked_draft = candidate
                self.draft_plan = None
                self.final_plan = None
                self.plan_status = "validation_failed"
                self.draft_blocked_reason = "legacy_draft_requires_regeneration"
                self.last_plan_violations = [
                    "unconfirmed legacy draft must be regenerated with ProjectContract v3"
                ]
                self.plan_generation = {
                    "status": "validation_failed",
                    "source": "legacy_blocked",
                    "updated_at": time.time(),
                }
                candidate = None
        if isinstance(candidate, dict):
            if self.plan_confirmed:
                frozen_contract = freeze_confirmed_project_contract(
                    self.project_contract or candidate.get("project_contract") or {}
                )
                candidate["project_contract"] = frozen_contract
                candidate["contract_version"] = frozen_contract["contract_version"]
                self.project_contract = frozen_contract
            validation = validate_plan_layers(candidate, self.project_contract or candidate.get("project_contract"))
            if not validation.valid:
                self.blocked_draft = candidate
                self.draft_plan = None
                self.final_plan = None
                self.plan_confirmed = False
                self.plan_status = "validation_failed"
                self.last_plan_violations = validation.violations
                self.draft_blocked_reason = "legacy_artifact_failed_current_contract_validation"
                self.plan_generation = {
                    "status": "validation_failed", "source": "legacy_blocked",
                    "validation": validation.to_dict(), "updated_at": time.time(),
                }
            elif not candidate.get("artifact_metadata") or not candidate.get("plan_version"):
                candidate["plan_version"] = PLAN_VERSION
                candidate["source"] = candidate.get("source") or "legacy_migrated"
                candidate["artifact_metadata"] = artifact_metadata(
                    "plan", PLAN_VERSION, "legacy_migrated", validation,
                    [{"type": "legacy_metadata_migration", "preserved_executable_fields": True}],
                )
                candidate["artifact_metadata"]["migrated_at"] = time.time()
                candidate["status"] = "confirmed" if self.plan_confirmed else "saved"
                self.plan_status = candidate["status"]
        # 恢复成员状态
        for mid, mdata in data.get("members", {}).items():
            if mid in self.members:
                self.members[mid].from_persist(mdata)
            else:
                m = PMMemberAgent(member_id=mid, name=mdata.get("name", mid), hermes_client=self.hermes)
                m.from_persist(mdata)
                self.members[mid] = m

    def _do_execute(self, task: Task) -> Any:
        if task.title == "analyze_requirement":
            return self.chat_with_user(task.description)
        elif task.title == "synthesize_plan":
            return self.synthesize_plan(task.description)
        elif task.title == "confirm_plan":
            return self.confirm_plan(task.metadata.get("modifications", ""))
        return {"error": f"Unknown task: {task.title}"}


# ─── PM 团队（容器） ──────────────────────────────────────────────────────────

class PMTeam:
    """PM 团队容器，持有组长并代理成员管理操作"""

    def assign_phase_to_member(self, phase_id: str, phase_name: str, phase_description: str = "") -> "Optional[PMMemberAgent]":
        """将阶段任务分配给一个空闲的PM成员（代理到组长）"""
        return self.leader.assign_phase_to_member(phase_id, phase_name, phase_description)

    def __init__(self, hermes_client):
        from agents.base.memory import HybridMemory
        memory = HybridMemory("memory/global_pm_team")
        self.leader = PMLeaderAgent(hermes_client=hermes_client, memory_store=memory)

    def add_member(self) -> PMMemberAgent:
        return self.leader.add_member()

    def remove_member(self, member_id: str) -> Dict[str, Any]:
        return self.leader.remove_member(member_id)

    def get_members_info(self) -> List[Dict]:
        return self.leader.get_members_info()
