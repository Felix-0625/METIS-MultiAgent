"""Durable, fail-closed Supervisor quality convergence state machine.

The existing phase routes own execution, locks, and persistence.  This module
only owns the quality run contract: legal transitions, round accounting,
stable issue identity, checkpoint data, and the single completion gate.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional


MAX_BUSINESS_QA_ROUNDS = 6
SCHEMA_VERSION = 1

AGENT_STATUSES = {
    "pending", "running", "succeeded", "failed", "timeout", "blocked",
    "cancelled",
}
AGENT_FAILURE_STATUSES = {"failed", "timeout", "blocked", "cancelled"}
TERMINAL_STATES = {"blocked", "completed"}
FAILURE_STATES = {"infrastructure_failed", "model_failed"}
ACTIVE_STATES = {
    "waiting_engineer", "qa_running", "repair_required", "verifying",
    "infrastructure_failed", "model_failed",
}


class IllegalQualityTransition(ValueError):
    """Raised when a caller attempts to bypass a Supervisor quality gate."""


def _now() -> float:
    return time.time()


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _normalized_message(value: Any) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    # Line numbers are evidence, not identity.  Keeping them in fingerprints
    # would turn the same defect into a newly introduced blocker after edits.
    import re

    text = re.sub(r"\bline\s*\d+\b", "line#", text)
    text = re.sub(r"第\s*\d+\s*行", "第#行", text)
    text = re.sub(r":\d+(?=\D|$)", ":#", text)
    # 语义归一化：LLM 对同一缺陷每轮措辞不同（“函数 foo 未实现” vs
    # “foo 函数体为空”），若直接用原文做指纹会每轮产生新 blocker，导致
    # “越改越多”。压成关键 token 集合（去停用词/标点、排序）再 join，
    # 让措辞不同但语义相同的消息归并到同一指纹。
    return _semantic_fingerprint_key(text)


def _semantic_fingerprint_key(text: str) -> str:
    """Collapse a message to a canonical token bag so paraphrases match.

    LLM 描述同一缺陷措辞多变（“函数 foo 未实现” vs “foo 函数体为空”）。
    中文描述词（未/实/现/为/空/缺/失…）本身不带缺陷身份，真正区分缺陷
    的是英文标识符（函数名/变量名/路径）。因此指纹主要由英文标识符构成，
    中文描述词整体弱化（仅保留极少数可能是实体名的字符），让措辞不同但
    指向同一标识符的缺陷归并到同一指纹。
    """
    import re

    # 英文停用词：仅纯语法/高频通用词，不含缺陷身份词。
    # undefined/null/error/fail/missing 等带缺陷语义（undefined vs throws 是不同缺陷），
    # 必须保留进 bag 以区分同标识符的不同缺陷。
    _STOP = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "to", "of", "in", "on", "at", "for", "and", "or", "do", "does", "did",
        "has", "have", "had", "with", "this", "that", "it", "should", "must",
        "function", "func", "method", "var", "variable", "return", "returns",
        "issue", "problem", "code", "file", "line", "use", "used",
    }
    # 拆分 token：英文单词、$ 路径标识符、中文连续汉字块
    tokens = re.findall(r"[a-z_][a-z0-9_]*|\$[^ \t]*|[一-鿿]+", text)
    bag = set()
    cjk_chars: set = set()
    for tok in tokens:
        if "一" <= tok[0] <= "鿿":
            # 中文描述词不带缺陷身份，整体弱化：收集但不优先。
            # 仅当消息没有任何英文标识符时，用 CJK 字符做兜底指纹，
            # 避免纯中文缺陷塌缩成空串被静默丢弃。
            cjk_chars.update(tok)
            continue
        if tok not in _STOP:
            bag.add(tok)
    if not bag:
        # 纯中文/无标识符消息：用 CJK 字符集兜底，至少保证不同中文缺陷
        # 字集不同时区分；字集相同时由 file_path/layer（在 issue_fingerprint
        # payload 里）区分。绝不返回空串导致同 file 多缺陷塌缩。
        bag = cjk_chars
    return " ".join(sorted(bag))


def issue_fingerprint(issue: Dict[str, Any]) -> str:
    explicit = str(issue.get("fingerprint") or "").strip()
    if explicit:
        return explicit
    payload = "|".join((
        str(issue.get("layer") or "unknown").strip().lower(),
        str(issue.get("file_path") or issue.get("file") or "")
        .replace("\\", "/").strip().lower(),
        _normalized_message(issue.get("message")),
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_issue_id(issue: Dict[str, Any]) -> str:
    explicit = str(issue.get("issue_id") or issue.get("id") or "").strip()
    return explicit or f"issue-{issue_fingerprint(issue)[:16]}"


def _is_blocker(issue: Dict[str, Any]) -> bool:
    status = str(issue.get("status") or "open").strip().lower()
    severity = str(issue.get("severity") or "error").strip().lower()
    # "deferred" = 5 轮上限后转人工延后，不阻塞当前流程推进（人工队列后续处理）
    return status not in {
        "fixed", "verified", "resolved", "deferred", "needs_manual",
    } and severity in {
        "error", "critical", "blocker", "p0", "p1",
    }


class SupervisorQualityMachine:
    """Serializable Supervisor QA state with strict transition validation."""

    def __init__(
        self,
        data: Optional[Dict[str, Any]] = None,
        *,
        max_business_rounds: int = MAX_BUSINESS_QA_ROUNDS,
    ):
        if int(max_business_rounds) != MAX_BUSINESS_QA_ROUNDS:
            raise ValueError("Supervisor business QA rounds are fixed at exactly 6")
        self.data: Dict[str, Any] = copy.deepcopy(data) if isinstance(data, dict) else {}
        self._normalize()
        self._validate_persisted_state()
        self._persisted_data = _json_copy(self.data)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]] = None) -> "SupervisorQualityMachine":
        return cls(data)

    def to_dict(self) -> Dict[str, Any]:
        return _json_copy(self.data)

    def mark_persisted(self) -> None:
        """Advance the rollback baseline only after durable commit succeeds."""
        self._persisted_data = _json_copy(self.data)

    def rollback_unpersisted(self) -> Dict[str, Any]:
        """Restore the last durable state after a failed checkpoint write."""
        self.data.clear()
        self.data.update(_json_copy(self._persisted_data))
        return self.to_dict()

    @property
    def state(self) -> str:
        return str(self.data.get("status") or self.data.get("state") or "idle")

    @property
    def active_round(self) -> Optional[Dict[str, Any]]:
        round_id = self.data.get("active_round_id") or self.data.get("active_qa_round_id")
        return next(
            (item for item in self.data["rounds"] if item.get("qa_round_id") == round_id),
            None,
        )

    def _normalize(self) -> None:
        self.data.setdefault("schema_version", SCHEMA_VERSION)
        self.data.setdefault("run_id", None)
        self.data.setdefault("phase_id", "")
        self.data.setdefault("status", self.data.get("state") or "idle")
        self.data["state"] = self.data["status"]
        self.data.setdefault("active", False)
        self.data.setdefault("idempotency_key", "")
        self.data.setdefault("start_request", {})
        self.data.setdefault("artifact_generation_history", [])
        self.data.setdefault("dependencies_ready", False)
        self.data.setdefault("dependency_snapshot", {})
        self.data.setdefault("max_qa_rounds", MAX_BUSINESS_QA_ROUNDS)
        # This is deliberately fixed, not configurable per request.
        self.data["max_qa_rounds"] = MAX_BUSINESS_QA_ROUNDS
        self.data["max_business_rounds"] = MAX_BUSINESS_QA_ROUNDS
        self.data.setdefault("business_rounds_used", 0)
        self.data.setdefault("active_qa_round_id", None)
        self.data.setdefault("active_round_id", self.data.get("active_qa_round_id"))
        self.data.setdefault("rounds", [])
        self.data.setdefault("agents", {})
        self.data.setdefault("pending_evidence", [])
        self.data.setdefault("pending_commands", [])
        self.data.setdefault("pending_verification_log", [])
        self.data.setdefault("verification_commit", "")
        self.data.setdefault("waiting_for", [])
        next_action = self.data.setdefault("next_action", {"type": "start_run"})
        if isinstance(next_action, str):
            self.data["next_action"] = {"type": next_action}
        self.data.setdefault("failure_reason", "")
        self.data.setdefault("manual_items", [])
        self.data.setdefault("checkpoint", {})
        self.data.setdefault("required_evidence_kinds", ["qa"])
        self.data.setdefault(
            "required_pre_qa_evidence_kinds",
            list(self.data.get("required_evidence_kinds") or ["qa"]),
        )
        self.data.setdefault("started_at", None)
        self.data.setdefault("updated_at", None)
        self.data.setdefault("completed_at", None)
        self.data.setdefault("completion_gate", {
            "dependencies_ready": False,
            "critical_agents_succeeded": False,
            "no_blockers": False,
            "evidence_complete": False,
            "passed": False,
        })

    def _validate_persisted_state(self) -> None:
        if self.state != "completed":
            return
        current = self.active_round
        agents = [
            item for item in self.data.get("agents", {}).values()
            if item.get("critical", True)
        ]
        gate = self.data.get("completion_gate") or {}
        valid = bool(
            self.data.get("run_id")
            and current
            and current.get("qa_snapshot_committed")
            and current.get("consumes_business_round")
            and current.get("commit")
            and current.get("scope_snapshot", {}).get("scope_digest")
            and (
                current.get("scope_snapshot", {}).get("artifact_digest")
                or current.get("scope_snapshot", {}).get("workspace_digest")
            )
            and current.get("commit") == "artifact:" + str(
                current.get("scope_snapshot", {}).get("artifact_digest")
                or current.get("scope_snapshot", {}).get("workspace_digest")
                or ""
            )
            and not any(_is_blocker(item) for item in current.get("issues", []))
            and agents
            and all(item.get("status") == "succeeded" for item in agents)
            and self._evidence_complete(current)
            and gate.get("passed") is True
        )
        if not valid:
            raise IllegalQualityTransition(
                "Invalid completed Supervisor checkpoint or completion gate"
            )

    def _touch(self, checkpoint: str) -> None:
        now = _now()
        self.data["updated_at"] = now
        self.data["checkpoint"] = {
            "name": checkpoint,
            "state": self.state,
            "qa_round_id": self.data.get("active_qa_round_id"),
            "business_rounds_used": self.data.get("business_rounds_used", 0),
            "updated_at": now,
        }

    def _set_state(self, state: str) -> None:
        self.data["status"] = state
        self.data["state"] = state

    def _set_active_round_id(self, round_id: Optional[str]) -> None:
        self.data["active_round_id"] = round_id
        self.data["active_qa_round_id"] = round_id

    def _set_next_action(self, action_type: str, **details: Any) -> None:
        self.data["next_action"] = {"type": action_type, **_json_copy(details)}

    def _require_state(self, *allowed: str) -> None:
        if self.state not in allowed:
            raise IllegalQualityTransition(
                f"Illegal Supervisor QA transition from {self.state}; "
                f"expected one of {sorted(allowed)}"
            )

    def _critical_agent_failures(self) -> List[str]:
        return [
            agent_id for agent_id, record in self.data["agents"].items()
            if record.get("critical", True)
            and record.get("status") in AGENT_FAILURE_STATUSES
        ]

    def _critical_agents_pending(self) -> List[str]:
        return [
            agent_id for agent_id, record in self.data["agents"].items()
            if record.get("critical", True) and record.get("status") != "succeeded"
        ]

    def start_run(
        self,
        scope: Dict[str, Any],
        idempotency_key: str,
        dependencies_ready: bool,
        run_id: Optional[str] = None,
        agents: Optional[Iterable[Dict[str, Any]]] = None,
        required_evidence_kinds: Optional[Iterable[str]] = None,
        required_pre_qa_evidence_kinds: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        materialized_agents = list(agents or [])
        requested_evidence = sorted(set(
            required_evidence_kinds
            or scope.get("required_evidence")
            or ["qa"]
        ))
        requested_pre_qa_evidence = sorted(set(
            required_pre_qa_evidence_kinds
            or required_evidence_kinds
            or scope.get("required_evidence")
            or ["qa"]
        ))
        start_request = {
            "scope": _json_copy(scope),
            "dependencies_ready": bool(dependencies_ready),
            "agents": sorted(
                [
                    {
                        "agent_id": str(
                            item.get("agent_id") or item.get("id") or ""
                        ).strip(),
                        "status": str(item.get("status") or "pending"),
                        "critical": bool(item.get("critical", True)),
                        "error": str(item.get("error") or ""),
                    }
                    for item in materialized_agents
                    if str(item.get("agent_id") or item.get("id") or "").strip()
                ],
                key=lambda item: item["agent_id"],
            ),
            "required_evidence_kinds": requested_evidence,
            "required_pre_qa_evidence_kinds": requested_pre_qa_evidence,
        }
        key = str(idempotency_key or "").strip()
        if not key:
            raise IllegalQualityTransition("A quality run requires an idempotency_key")
        if self.data.get("run_id"):
            if self.data.get("idempotency_key") == key:
                if self.data.get("start_request") != start_request:
                    raise IllegalQualityTransition(
                        "Conflicting replay for Supervisor quality run idempotency_key"
                    )
                return self.to_dict()
            raise IllegalQualityTransition(
                "An existing Supervisor quality run cannot be replaced; "
                "start a separately authorized phase generation"
            )
        if not dependencies_ready:
            raise IllegalQualityTransition("Supervisor quality dependencies are not satisfied")
        artifact_digest = str(
            scope.get("artifact_digest")
            or scope.get("workspace_digest")
            or ""
        ).strip()
        if not artifact_digest:
            raise IllegalQualityTransition(
                "Supervisor quality scope must bind immutable workspace bytes"
            )

        now = _now()
        phase_id = str(scope.get("phase_id") or self.data.get("phase_id") or "")
        self.data = {
            "schema_version": SCHEMA_VERSION,
            "run_id": str(run_id or f"qa-run-{uuid.uuid4().hex}"),
            "phase_id": phase_id,
            "state": "waiting_engineer",
            "status": "waiting_engineer",
            "active": True,
            "idempotency_key": key,
            "start_request": start_request,
            "artifact_generation_history": [],
            "dependencies_ready": True,
            "dependency_snapshot": _json_copy(scope.get("dependencies") or {}),
            "scope": _json_copy(scope),
            "max_qa_rounds": MAX_BUSINESS_QA_ROUNDS,
            "max_business_rounds": MAX_BUSINESS_QA_ROUNDS,
            "business_rounds_used": 0,
            "active_qa_round_id": None,
            "active_round_id": None,
            "rounds": [],
            "agents": {},
            "pending_evidence": [],
            "pending_commands": [],
            "pending_verification_log": [],
            "verification_commit": "",
            "waiting_for": [],
            "next_action": {"type": "start_qa_round"},
            "failure_reason": "",
            "manual_items": [],
            "checkpoint": {},
            "required_evidence_kinds": requested_evidence,
            "required_pre_qa_evidence_kinds": requested_pre_qa_evidence,
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
            "completion_gate": {},
        }
        for agent in materialized_agents:
            agent_id = str(agent.get("agent_id") or agent.get("id") or "").strip()
            if not agent_id:
                continue
            self.record_agent(
                agent_id,
                str(agent.get("status") or "pending"),
                critical=bool(agent.get("critical", True)),
                error=str(agent.get("error") or ""),
            )
        self.data["waiting_for"] = self._critical_agents_pending()
        self._refresh_completion_gate([])
        self._touch("run_started")
        return self.to_dict()

    def bind_artifact_generation(
        self,
        *,
        artifact_digest: str,
        scope_digest: str,
        repair_commit: str,
        scope_snapshot: Optional[Dict[str, Any]] = None,
        transition_reason: str = "engineer_repair",
        expected_previous_artifact_digest: str = "",
    ) -> Dict[str, Any]:
        """Authorize a new immutable byte generation before verification."""
        reason = str(transition_reason or "").strip().lower()
        allowed_reason_by_state = {
            "waiting_engineer": {"engineer_repair", "deterministic_pre_qa_repair"},
            "verifying": {"deterministic_pre_qa_repair"},
            "blocked": {"manual_fix"},
        }
        if reason not in allowed_reason_by_state.get(self.state, set()):
            raise IllegalQualityTransition(
                f"Artifact generation transition {reason or '<missing>'} "
                f"is not allowed from {self.state}"
            )
        artifact = str(artifact_digest or "").strip()
        scope = str(scope_digest or "").strip()
        commit = str(repair_commit or "").strip()
        if not artifact or not scope or not commit:
            raise IllegalQualityTransition(
                "Artifact generation binding requires artifact, scope, and repair commit"
            )
        previous = _json_copy(self.data.get("scope") or {})
        previous_artifact = str(
            previous.get("artifact_digest")
            or previous.get("workspace_digest")
            or ""
        )
        expected_previous = str(expected_previous_artifact_digest or "").strip()
        if self.state != "waiting_engineer":
            if not expected_previous or expected_previous != previous_artifact:
                raise IllegalQualityTransition(
                    "Artifact generation binding lost its previous-generation CAS"
                )
        requested_scope = _json_copy(scope_snapshot or {})
        if requested_scope and (
            str(requested_scope.get("artifact_digest") or "") != artifact
            or str(requested_scope.get("scope_digest") or "") != scope
        ):
            raise IllegalQualityTransition(
                "Artifact generation snapshot does not match its digests"
            )
        self.data["artifact_generation_history"].append({
            "previous_artifact_digest": previous_artifact,
            "previous_scope_digest": str(previous.get("scope_digest") or ""),
            "artifact_digest": artifact,
            "scope_digest": scope,
            "repair_commit": commit,
            "transition_reason": reason,
            "bound_at": _now(),
        })
        self.data["scope"] = requested_scope or {
            **previous,
            "artifact_digest": artifact,
            "scope_digest": scope,
        }
        self.data["verification_commit"] = f"artifact:{artifact}"
        if self.state in {"verifying", "blocked"}:
            # Evidence recorded for the previous immutable generation cannot
            # satisfy verification of the newly authorized bytes.
            self.data["pending_evidence"] = []
            self.data["pending_commands"] = []
            self.data["pending_verification_log"] = []
        self._touch("artifact_generation_bound")
        return self.to_dict()

    def record_agent(
        self,
        agent_id: str,
        status: str,
        *,
        critical: bool = True,
        error: str = "",
        task_id: str = "",
    ) -> Dict[str, Any]:
        normalized = str(status or "").strip().lower()
        if normalized not in AGENT_STATUSES:
            raise IllegalQualityTransition(f"Unsupported Agent status: {status}")
        if self.state in TERMINAL_STATES:
            raise IllegalQualityTransition("Terminal quality runs cannot mutate Agent state")
        existing = self.data["agents"].get(agent_id, {})
        previous_status = str(existing.get("status") or "pending")
        allowed = {
            "pending": AGENT_STATUSES,
            "running": {"running", "succeeded", "failed", "timeout", "blocked", "cancelled"},
            "succeeded": {"succeeded"},
            "failed": {"failed"},
            "timeout": {"timeout"},
            "blocked": {"blocked"},
            "cancelled": {"cancelled"},
        }
        explicit_retry = bool(
            existing
            and previous_status in AGENT_FAILURE_STATUSES
            and normalized == "pending"
            and task_id
            and task_id != existing.get("task_id")
        )
        if existing and normalized not in allowed.get(previous_status, set()) and not explicit_retry:
            raise IllegalQualityTransition(
                f"Illegal Agent transition {previous_status} -> {normalized} for {agent_id}"
            )
        history = list(existing.get("attempt_history") or [])
        if explicit_retry:
            history.append({
                key: copy.deepcopy(value)
                for key, value in existing.items()
                if key != "attempt_history"
            })
        self.data["agents"][agent_id] = {
            **existing,
            "agent_id": agent_id,
            "task_id": task_id or existing.get("task_id", ""),
            "critical": bool(critical),
            "status": normalized,
            "error": str(error or ""),
            "attempt_history": history,
            "updated_at": _now(),
        }
        current = self.active_round
        if current is not None:
            by_id = {item.get("agent_id"): item for item in current["agent_tasks"]}
            by_id[agent_id] = _json_copy(self.data["agents"][agent_id])
            current["agent_tasks"] = list(by_id.values())
        self.data["waiting_for"] = self._critical_agents_pending()
        self._touch("agent_state_recorded")
        return self.to_dict()

    def start_qa_round(
        self,
        scope_snapshot: Dict[str, Any],
        issue_snapshot: Optional[Iterable[Dict[str, Any]]] = None,
        qa_round_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        current = self.active_round
        requested_round_id = str(qa_round_id or "").strip()
        if self.state == "qa_running" and current is not None:
            if not requested_round_id or requested_round_id == current.get("qa_round_id"):
                normalized_replay = self._normalize_issues(
                    issue_snapshot or [], current["round_number"]
                )
                if (
                    current.get("scope_snapshot") != _json_copy(scope_snapshot)
                    or current.get("issue_snapshot") != normalized_replay
                ):
                    raise IllegalQualityTransition(
                        "Conflicting replay for active qa_round_id"
                    )
                return self.to_dict()
            raise IllegalQualityTransition("A QA round is already active")
        self._require_state("verifying", "infrastructure_failed", "model_failed")
        failures = self._critical_agent_failures()
        pending = self._critical_agents_pending()
        if failures:
            raise IllegalQualityTransition(
                "Critical Agent failure prevents QA: " + ", ".join(failures)
            )
        if pending:
            raise IllegalQualityTransition(
                "Engineering work is incomplete: " + ", ".join(pending)
            )
        if not self.data.get("dependencies_ready"):
            raise IllegalQualityTransition("Dependencies are not satisfied")
        locked_scope_digest = str(
            (self.data.get("scope") or {}).get("scope_digest") or ""
        )
        requested_scope_digest = str(scope_snapshot.get("scope_digest") or "")
        if not requested_scope_digest or requested_scope_digest != locked_scope_digest:
            raise IllegalQualityTransition(
                "QA scope digest does not match the scope locked at run start"
            )
        locked_artifact_digest = str(
            (self.data.get("scope") or {}).get("artifact_digest")
            or (self.data.get("scope") or {}).get("workspace_digest")
            or ""
        )
        requested_artifact_digest = str(
            scope_snapshot.get("artifact_digest")
            or scope_snapshot.get("workspace_digest")
            or ""
        )
        if (
            not requested_artifact_digest
            or requested_artifact_digest != locked_artifact_digest
        ):
            raise IllegalQualityTransition(
                "QA artifact digest does not match the workspace bytes locked at run start"
            )

        if self.state in FAILURE_STATES and current is not None:
            if requested_round_id and requested_round_id != current["qa_round_id"]:
                raise IllegalQualityTransition("Failed QA retry must reuse qa_round_id")
            current["state"] = "qa_running"
            current["failure"] = None
            self._set_state("qa_running")
            self.data["failure_reason"] = ""
            self._set_next_action("record_verification_evidence")
            self._touch("qa_round_resumed")
            return self.to_dict()

        if (
            int(self.data.get("business_rounds_used", 0)) >= MAX_BUSINESS_QA_ROUNDS
            and not self.data.get("manual_verification_override")
        ):
            return self._block("The five business QA rounds are exhausted", [])
        if not self._evidence_complete_list(
            self.data.get("pending_evidence") or [],
            self.data.get("required_pre_qa_evidence_kinds") or [],
        ) or not self._pre_qa_evidence_matches_locked_scope():
            raise IllegalQualityTransition(
                "Complete scoped verification evidence is required before QA"
            )

        round_number = int(self.data.get("business_rounds_used", 0)) + 1
        round_id = requested_round_id or f"qa-round-{self.data['run_id']}-{round_number}"
        normalized_issues = self._normalize_issues(issue_snapshot or [], round_number)
        round_record = {
            "qa_round_id": round_id,
            "run_id": self.data["run_id"],
            "round_number": round_number,
            "state": "qa_running",
            "scope_snapshot": _json_copy(scope_snapshot),
            "issue_snapshot": _json_copy(normalized_issues),
            "issues": _json_copy(normalized_issues),
            "counts": {
                "total": len(normalized_issues), "blocking": sum(map(_is_blocker, normalized_issues)),
                "fixed": 0, "remaining": 0, "new": 0, "repeated": 0,
            },
            "commit": str(self.data.get("verification_commit") or ""),
            "commands": _json_copy(self.data.get("pending_commands") or []),
            "evidence": _json_copy(self.data.get("pending_evidence") or []),
            "evidence_by_kind": {},
            "verification_log": _json_copy(self.data.get("pending_verification_log") or []),
            "agent_tasks": [],
            "failure": None,
            "consumes_business_round": False,
            "qa_snapshot_committed": False,
            "started_at": _now(),
            "finished_at": None,
        }
        for evidence in round_record["evidence"]:
            round_record["evidence_by_kind"].setdefault(evidence.get("kind", "other"), []).append(evidence)
        self.data["rounds"].append(round_record)
        self._set_active_round_id(round_id)
        self._set_state("qa_running")
        self.data["waiting_for"] = []
        self._set_next_action("record_verification_evidence")
        self.data["pending_evidence"] = []
        self.data["pending_commands"] = []
        self.data["pending_verification_log"] = []
        self._touch("qa_round_started")
        return self.to_dict()

    def mark_repair_required(
        self,
        issues: Optional[Iterable[Dict[str, Any]]] = None,
        agent_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        self._require_state("repair_required")
        current = self.active_round
        if current is None:
            raise IllegalQualityTransition("No active QA round")
        if issues is not None:
            candidate = self._normalize_issues(issues, current["round_number"])
            if candidate != current.get("issues", []):
                raise IllegalQualityTransition("Committed issue snapshots are immutable")
        repair_agents = [str(item) for item in (agent_ids or []) if str(item)]
        if not repair_agents:
            repair_agents = sorted({
                str(item.get("responsible_agent_id") or "")
                for item in current.get("issues", []) if item.get("responsible_agent_id")
            })
        if not repair_agents:
            raise IllegalQualityTransition("Repair requires an explicit responsible Agent")
        for agent_id in repair_agents:
            existing = self.data["agents"].get(agent_id, {})
            self.data["agents"][agent_id] = {
                **existing,
                "agent_id": agent_id,
                "critical": True,
                "status": "pending",
                "error": "",
                "attempt": int(existing.get("attempt", 0)) + 1,
                "updated_at": _now(),
            }
        current["state"] = "repair_required"
        self._set_state("waiting_engineer")
        self.data["waiting_for"] = repair_agents
        self._set_next_action("wait_for_engineer")
        self._touch("repair_required")
        return self.to_dict()

    def engineer_completed(self, commit: str) -> Dict[str, Any]:
        self._require_state("waiting_engineer", "repair_required")
        current = self.active_round
        failures = self._critical_agent_failures()
        pending = self._critical_agents_pending()
        critical_agents = [
            record for record in self.data["agents"].values()
            if record.get("critical", True)
        ]
        if not critical_agents:
            raise IllegalQualityTransition("At least one critical Agent must succeed")
        if failures:
            raise IllegalQualityTransition(
                "Critical Agent failed during repair: " + ", ".join(failures)
            )
        if pending:
            raise IllegalQualityTransition(
                "Cannot verify before every critical Agent succeeds: " + ", ".join(pending)
            )
        commit_value = str(commit or "").strip()
        if not commit_value:
            raise IllegalQualityTransition("Repair completion requires an associated commit")
        expected_commit = "artifact:" + str(
            (self.data.get("scope") or {}).get("artifact_digest")
            or (self.data.get("scope") or {}).get("workspace_digest")
            or ""
        )
        if commit_value != expected_commit:
            raise IllegalQualityTransition(
                "Repair completion commit does not bind the locked artifact generation"
            )
        if current is not None:
            current["commit"] = commit_value
        self.data["verification_commit"] = commit_value
        self.data["waiting_for"] = []
        self._set_next_action("start_verification")
        self._touch("engineer_completed")
        return self.to_dict()

    def start_verification(self) -> Dict[str, Any]:
        self._require_state("waiting_engineer")
        if not self.data.get("verification_commit"):
            raise IllegalQualityTransition("Repair commit is missing")
        pending = self._critical_agents_pending()
        if pending:
            raise IllegalQualityTransition(
                "Cannot verify before every critical Agent succeeds: " + ", ".join(pending)
            )
        self._set_state("verifying")
        self.data["pending_evidence"] = []
        self.data["pending_commands"] = []
        self.data["pending_verification_log"] = []
        self._set_next_action("record_verification_evidence")
        self._touch("verification_started")
        return self.to_dict()

    def fail_pre_qa(
        self,
        issues: Iterable[Dict[str, Any]],
        *,
        agent_ids: Iterable[str],
    ) -> Dict[str, Any]:
        """Return to engineering without consuming a Supervisor QA round."""
        self._require_state("verifying", "qa_running")
        normalized_issues = [
            copy.deepcopy(item) for item in issues if isinstance(item, dict)
        ]
        owners = sorted({str(item) for item in agent_ids if str(item)})
        if not owners:
            raise IllegalQualityTransition("Pre-QA failure requires a responsible Agent")
        for agent_id in owners:
            existing = self.data["agents"].get(agent_id, {})
            self.data["agents"][agent_id] = {
                **existing,
                "agent_id": agent_id,
                "critical": True,
                "status": "pending",
                "error": "pre-QA verification failed",
                "updated_at": _now(),
            }
        self.data["pre_qa_failure"] = {
            "issues": normalized_issues,
            "recorded_at": _now(),
            "consumed_business_round": False,
        }
        current = self.active_round
        if current is not None:
            current["state"] = "pre_qa_failed"
            current["consumes_business_round"] = False
            current["qa_snapshot_committed"] = False
        # pre-QA 失败计数 + 上限：pre_qa 失败不消耗业务轮次，若无上限会无限
        # 循环重跑同一 agent（每轮 pre_qa 都失败、永不收敛）。达上限转 blocked
        # 让 routes 停止重试并转人工裁决。
        self.data["pre_qa_failure_count"] = int(self.data.get("pre_qa_failure_count", 0)) + 1
        if self.data["pre_qa_failure_count"] >= MAX_BUSINESS_QA_ROUNDS:
            self.data["pre_qa_failure"] = {
                "issues": normalized_issues,
                "recorded_at": _now(),
                "consumed_business_round": False,
                "exhausted": True,
            }
            return self._block(
                "Pre-QA failures exhausted the retry budget; manual review required",
                list(normalized_issues),
            )
        self.data["waiting_for"] = owners
        self._set_state("waiting_engineer")
        self._set_next_action("repair_pre_qa_failures", agent_ids=owners)
        self._touch("pre_qa_failed")
        return self.to_dict()

    def record_evidence(
        self,
        kind: str,
        command: str,
        exit_code: int,
        *,
        passed: Optional[bool] = None,
        log: str,
        step_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._require_state("qa_running", "verifying")
        current = self.active_round
        if current is None and self.state == "qa_running":
            raise IllegalQualityTransition("No active QA round")
        command_value = str(command or "").strip()
        log_value = str(log or "").strip()
        if not command_value or not isinstance(exit_code, int) or not log_value:
            raise IllegalQualityTransition(
                "Verification evidence requires command, integer exit_code, and log"
            )
        normalized_kind = str(kind or "other").strip().lower()
        evidence_id = str(step_id or hashlib.sha256(
            f"{normalized_kind}|{command_value}|{exit_code}|{log_value}".encode("utf-8")
        ).hexdigest()[:20])
        evidence_target = (
            current["evidence"]
            if current is not None and self.state == "qa_running"
            else self.data["pending_evidence"]
        )
        existing = next(
            (item for item in evidence_target if item.get("step_id") == evidence_id),
            None,
        )
        if existing:
            candidate = {
                "kind": normalized_kind,
                "command": command_value,
                "exit_code": exit_code,
                "passed": bool(exit_code == 0 if passed is None else passed),
                "log": log_value,
            }
            if all(existing.get(key) == value for key, value in candidate.items()):
                return self.to_dict()
            raise IllegalQualityTransition(
                f"Evidence step {evidence_id} already exists with different content"
            )
        if bool(exit_code == 0) != bool(exit_code == 0 if passed is None else passed):
            raise IllegalQualityTransition("Evidence passed flag must agree with exit_code")
        item = {
            "step_id": evidence_id,
            "kind": normalized_kind,
            "command": command_value,
            "exit_code": exit_code,
            "passed": bool(exit_code == 0 if passed is None else passed),
            "log": log_value,
            "metadata": _json_copy(metadata or {}),
            "recorded_at": _now(),
        }
        target_evidence = current["evidence"] if current is not None and self.state == "qa_running" else self.data["pending_evidence"]
        target_commands = current["commands"] if current is not None and self.state == "qa_running" else self.data["pending_commands"]
        target_logs = current["verification_log"] if current is not None and self.state == "qa_running" else self.data["pending_verification_log"]
        target_evidence.append(item)
        if current is not None and self.state == "qa_running":
            current["evidence_by_kind"].setdefault(normalized_kind, []).append(item)
        target_commands.append({
            "command": command_value,
            "exit_code": exit_code,
            "kind": normalized_kind,
            "step_id": evidence_id,
        })
        target_logs.append(log_value)
        self._touch("verification_evidence_recorded")
        return self.to_dict()

    def finish_verification(self, issues: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        self._require_state("qa_running")
        current = self.active_round
        if current is None:
            raise IllegalQualityTransition("No active QA round")
        normalized = self._normalize_issues(issues, current["round_number"])
        if current.get("consumes_business_round"):
            if current.get("issues") != normalized:
                raise IllegalQualityTransition(
                    "Conflicting verification replay for qa_round_id"
                )
            return self.to_dict()
        evidence_complete = self._evidence_complete(current)
        if not evidence_complete:
            raise IllegalQualityTransition("Complete real verification evidence is required")
        if any(not item.get("passed") for item in current["evidence"]):
            raise IllegalQualityTransition("Failed verification evidence cannot complete a QA round")

        previous = self._previous_committed_round(current["qa_round_id"])
        baseline_by_fp = {
            item["fingerprint"]: item
            for item in current.get("issue_snapshot", [])
            if _is_blocker(item)
        }
        previous_by_fp = {
            item["fingerprint"]: item
            for item in (previous.get("issues", []) if previous else [])
            if _is_blocker(item)
        }
        current_by_fp = {item["fingerprint"]: item for item in normalized if _is_blocker(item)}
        comparison_by_fp = baseline_by_fp or previous_by_fp
        new_fps = set(current_by_fp) - set(comparison_by_fp)
        repeated_fps = set(current_by_fp) & set(comparison_by_fp)
        fixed_fps = set(comparison_by_fp) - set(current_by_fp)
        for fingerprint in repeated_fps:
            old = comparison_by_fp[fingerprint]
            current_by_fp[fingerprint]["issue_id"] = old.get("issue_id") or current_by_fp[fingerprint]["issue_id"]
            current_by_fp[fingerprint]["first_seen_round"] = old.get("first_seen_round", 1)
            current_by_fp[fingerprint]["lifecycle"] = "repeated"
        for fingerprint in new_fps:
            current_by_fp[fingerprint]["lifecycle"] = "new"

        current["issues"] = normalized
        current["issue_snapshot"] = _json_copy(normalized)
        current["counts"] = {
            "total": len(normalized),
            "blocking": len(current_by_fp),
            "fixed": len(fixed_fps),
            "remaining": len(current_by_fp),
            "new": len(new_fps),
            "repeated": len(repeated_fps),
        }
        current["new_blocking"] = len(new_fps)
        current["consumes_business_round"] = True
        current["qa_snapshot_committed"] = True
        current["finished_at"] = _now()

        # 先校验 critical agent failure，再提交轮次预算。否则抛错后
        # business_rounds_used 已 +1，留下半提交状态消耗预算。
        failures = self._critical_agent_failures()
        if failures:
            raise IllegalQualityTransition(
                "Critical Agent failure prevents completion: " + ", ".join(failures)
            )
        self.data["business_rounds_used"] = int(self.data.get("business_rounds_used", 0)) + 1
        # 新指纹不再立即封锁：LLM 措辞波动会产生新指纹，立即 block 会导致
        # 「越改越多、卡死」。新指纹 blocker 并入 repair 流程收敛，由 5 轮上限兜底。
        if current_by_fp:
            if self.data["business_rounds_used"] >= MAX_BUSINESS_QA_ROUNDS:
                # 逃生口：5 轮上限后转人工延后。剩余 blocker 标记 deferred（进 manual_items
                # 供人工处理；人工介入重验时不再阻塞 completion gate），保留 blocked 状态
                # 让 routes 停止 auto repair 并转人工决策（awaiting_decision）。
                deferred_items = _json_copy(list(current_by_fp.values()))
                for item in deferred_items:
                    item["status"] = "deferred"
                for item in normalized:
                    if _is_blocker(item):
                        item["status"] = "deferred"
                return self._block(
                    "Blockers remain after the fifth business QA round",
                    deferred_items,
                )
            else:
                current["state"] = "repair_required"
                self._set_state("repair_required")
                self.data["waiting_for"] = ["repair_plan"]
                self._set_next_action("dispatch_repair")
                self._refresh_completion_gate(normalized)
                self._touch("verification_requires_repair")
                return self.to_dict()

        current["state"] = "verified"
        self._refresh_completion_gate(normalized)
        self._set_state("qa_running")
        self._set_next_action("complete")
        self._touch("verification_passed")
        return self.to_dict()

    def fail(self, kind: str, reason: str) -> Dict[str, Any]:
        self._require_state("qa_running", "verifying")
        normalized = str(kind or "").strip().lower()
        if normalized not in {"infrastructure", "model"}:
            raise IllegalQualityTransition("Failure kind must be infrastructure or model")
        current = self.active_round
        if current is None and self.state == "qa_running":
            raise IllegalQualityTransition("No active QA round")
        state = f"{normalized}_failed"
        failure = {
            "kind": normalized,
            "reason": str(reason or "Unknown external failure"),
            "recorded_at": _now(),
            "consumed_business_round": False,
            "resume_state": self.state,
        }
        if current is not None:
            current["state"] = state
            current["failure"] = failure
        self.data["failure"] = failure
        self._set_state(state)
        self.data["failure_reason"] = failure["reason"]
        self.data["waiting_for"] = [f"{normalized}_recovery"]
        self._set_next_action("retry_same_qa_round")
        self._touch(state)
        return self.to_dict()

    def resume(self, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        if idempotency_key and idempotency_key != self.data.get("idempotency_key"):
            raise IllegalQualityTransition("Resume idempotency key does not match the active run")
        if self.state in FAILURE_STATES:
            resume_state = str((self.data.get("failure") or {}).get("resume_state") or "verifying")
            if resume_state == "qa_running" and self.active_round is not None:
                current = self.active_round
                current["state"] = "qa_running"
                current["failure"] = None
                self._set_state("qa_running")
            else:
                self._set_state("verifying")
            self.data["failure_reason"] = ""
            self._set_next_action("retry_failed_step")
            self._touch("failure_resumed")
            return self.to_dict()
        if self.state in ACTIVE_STATES:
            return self.to_dict()
        raise IllegalQualityTransition(f"Quality run in {self.state} cannot be resumed")

    def resume_after_manual_fix(self) -> Dict[str, Any]:
        """Verify an explicit human correction without resetting QA budget."""
        self._require_state("blocked")
        # 人工修复后必须可重验：5 轮上限是自动收敛预算，不应阻塞人工介入后的
        # 复核。routes 给 blocked 状态的选项就是 manual_fix，此处拒绝会死路。
        self._set_state("verifying")
        self.data["active"] = True
        self.data["failure_reason"] = ""
        self.data["manual_items"] = []
        self.data["waiting_for"] = []
        self.data["completed_at"] = None
        self.data["manual_verification_override"] = True
        self.data["pending_evidence"] = []
        self.data["pending_commands"] = []
        self.data["pending_verification_log"] = []
        self.acknowledge_manual_fix_agents()
        self._set_next_action("record_verification_evidence")
        self._touch("manual_fix_verification_started")
        return self.to_dict()

    def acknowledge_manual_fix_agents(self) -> Dict[str, Any]:
        """Record explicit human takeover for pending repair tasks only."""
        self._require_state("verifying")
        for agent_id, record in list(self.data.get("agents", {}).items()):
            if record.get("critical", True) and record.get("status") == "pending":
                self.record_agent(
                    agent_id,
                    "succeeded",
                    critical=True,
                    task_id=str(record.get("task_id") or agent_id),
                )
        self._touch("manual_fix_agents_acknowledged")
        return self.to_dict()

    def complete(self) -> Dict[str, Any]:
        self._require_state("verifying", "qa_running")
        current = self.active_round
        issues = current.get("issues", []) if current else []
        if not current or not current.get("qa_snapshot_committed"):
            raise IllegalQualityTransition("A committed QA issue snapshot is required")
        scope_snapshot = current.get("scope_snapshot", {})
        if (
            not current.get("commit")
            or not scope_snapshot.get("scope_digest")
            or not (
                scope_snapshot.get("artifact_digest")
                or scope_snapshot.get("workspace_digest")
            )
        ):
            raise IllegalQualityTransition("Completion requires commit and locked scope digest")
        expected_commit = "artifact:" + str(
            scope_snapshot.get("artifact_digest")
            or scope_snapshot.get("workspace_digest")
            or ""
        )
        if current.get("commit") != expected_commit:
            raise IllegalQualityTransition(
                "Completion commit does not match the locked artifact generation"
            )
        self._refresh_completion_gate(issues)
        gate = self.data["completion_gate"]
        if not all(gate.get(item) for item in (
            "dependencies_ready", "critical_agents_succeeded", "no_blockers",
            "evidence_complete",
        )):
            raise IllegalQualityTransition(
                "Supervisor completion gate rejected the run: "
                + json.dumps(gate, ensure_ascii=False, sort_keys=True)
            )
        self._set_state("completed")
        self.data["active"] = False
        self.data["waiting_for"] = []
        self._set_next_action("confirm_phase_completion")
        self.data["failure_reason"] = ""
        self.data["completed_at"] = _now()
        self.data["completion_gate"]["passed"] = True
        if current is not None:
            current["state"] = "verified"
            current["finished_at"] = current.get("finished_at") or _now()
        self._touch("completed")
        return self.to_dict()

    def block(self, reason: str, manual_items: Iterable[Any]) -> Dict[str, Any]:
        """Fail closed from an active run without exposing internal helpers."""
        if self.state not in ACTIVE_STATES:
            raise IllegalQualityTransition(
                f"Quality run in {self.state} cannot be manually blocked"
            )
        return self._block(reason, manual_items)

    def _block(self, reason: str, manual_items: Iterable[Any]) -> Dict[str, Any]:
        materialized_items = list(manual_items)
        self._set_state("blocked")
        self.data["active"] = False
        self.data["failure_reason"] = str(reason)
        self.data["manual_items"] = _json_copy(materialized_items)
        self.data["waiting_for"] = ["human"]
        self._set_next_action("manual_intervention", issues=materialized_items)
        self.data["completed_at"] = _now()
        current = self.active_round
        if current is not None:
            current["state"] = "blocked"
            current["finished_at"] = current.get("finished_at") or _now()
        self._refresh_completion_gate(current.get("issues", []) if current else [])
        self._touch("blocked")
        return self.to_dict()

    def _normalize_issues(
        self, issues: Iterable[Dict[str, Any]], round_number: int,
    ) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        seen = set()
        for raw in issues or []:
            if not isinstance(raw, dict):
                continue
            item = _json_copy(raw)
            fingerprint = issue_fingerprint(item)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            item["fingerprint"] = fingerprint
            item["issue_id"] = stable_issue_id(item)
            item.setdefault("id", item["issue_id"])
            item.setdefault("status", "open")
            item.setdefault("severity", "error")
            item.setdefault("first_seen_round", round_number)
            item["last_seen_round"] = round_number
            item.setdefault("lifecycle", "new")
            normalized.append(item)
        return normalized

    def _previous_committed_round(self, current_round_id: str) -> Optional[Dict[str, Any]]:
        prior = [
            item for item in self.data["rounds"]
            if item.get("qa_round_id") != current_round_id
            and item.get("consumes_business_round")
        ]
        return prior[-1] if prior else None

    def _evidence_complete(self, round_record: Optional[Dict[str, Any]]) -> bool:
        if not round_record:
            return False
        evidence = round_record.get("evidence") or []
        required = self.data.get("required_evidence_kinds") or ["qa"]
        return all(
            any(
                item.get("command")
                and isinstance(item.get("exit_code"), int)
                and str(item.get("log") or "").strip()
                and item.get("kind") == kind
                and item.get("passed") is True
                and item.get("exit_code") == 0
                for item in evidence
            )
            for kind in required
        )

    def _evidence_complete_list(
        self,
        evidence: List[Dict[str, Any]],
        required: Optional[Iterable[str]] = None,
    ) -> bool:
        record = {"evidence": evidence}
        if required is None:
            return self._evidence_complete(record)
        original = self.data.get("required_evidence_kinds")
        try:
            self.data["required_evidence_kinds"] = list(required)
            return self._evidence_complete(record)
        finally:
            self.data["required_evidence_kinds"] = original

    def _pre_qa_evidence_matches_locked_scope(self) -> bool:
        """Require every pre-QA gate record to bind the immutable run scope."""
        locked = self.data.get("scope") or {}
        required = set(self.data.get("required_pre_qa_evidence_kinds") or [])
        if "pre_qa" not in required:
            return True
        evidence = self.data.get("pending_evidence") or []
        binding_keys = (
            "project_id",
            "phase_id",
            "phase_generation_id",
            "scope_digest",
            "artifact_digest",
        )
        for kind in required:
            matching = [item for item in evidence if item.get("kind") == kind]
            if not matching:
                return False
            if not any(
                all(
                    not locked.get(key)
                    or str((item.get("metadata") or {}).get(key) or "")
                    == str(locked.get(key) or "")
                    for key in binding_keys
                )
                for item in matching
            ):
                return False
        return True

    def _refresh_completion_gate(self, issues: Iterable[Dict[str, Any]]) -> None:
        current = self.active_round
        critical_agents = [
            record for record in self.data.get("agents", {}).values()
            if record.get("critical", True)
        ]
        gate = {
            "dependencies_ready": bool(self.data.get("dependencies_ready")),
            "critical_agents_succeeded": bool(critical_agents) and all(
                record.get("status") == "succeeded" for record in critical_agents
            ),
            "no_blockers": not any(_is_blocker(item) for item in issues or []),
            "evidence_complete": self._evidence_complete(current),
            "passed": False,
        }
        self.data["completion_gate"] = gate


__all__ = [
    "AGENT_STATUSES",
    "IllegalQualityTransition",
    "MAX_BUSINESS_QA_ROUNDS",
    "SupervisorQualityMachine",
    "issue_fingerprint",
    "stable_issue_id",
]
