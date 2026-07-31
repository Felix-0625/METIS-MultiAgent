"""Typed, fail-closed execution evidence and release gate primitives.

Evidence represents observations made by runner-controlled producers.  An
agent narrative and the existence of a file are useful diagnostics, but are
not independently sufficient proof that a command, test, service or deploy
succeeded.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from core.security_audit import redact_value


EVIDENCE_SCHEMA_VERSION = 1
TRUSTED_EVIDENCE_PRODUCERS = frozenset(
    {
        "metis.runner", "metis.ci", "metis.runtime_acceptance",
        "metis.deployment", "metis.supervisor", "metis.user_confirmation",
    }
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}$")
_COMMIT_HASH = re.compile(r"^[0-9a-fA-F]{7,64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-fA-F]{64}$")


class EvidenceKind(str, Enum):
    COMMAND = "command"
    TEST = "test"
    BUILD = "build"
    SERVICE_HEALTH = "service_health"
    API = "api"
    DOCKER = "docker"
    DEPLOYMENT = "deployment"
    COMMIT = "commit"
    ARTIFACT_VALIDATION = "artifact_validation"
    SUPERVISOR_OBSERVATION = "supervisor_observation"
    HUMAN_CONFIRMATION = "human_confirmation"


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    run_id: str
    kind: str
    producer: str
    status: str
    observed_at: float
    payload: dict[str, Any]
    schema_version: int = EVIDENCE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvidenceRecord":
        payload = value.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("evidence payload must be an object")
        return cls(
            evidence_id=str(value.get("evidence_id") or ""),
            run_id=str(value.get("run_id") or ""),
            kind=str(value.get("kind") or ""),
            producer=str(value.get("producer") or ""),
            status=str(value.get("status") or ""),
            observed_at=float(value.get("observed_at") or 0),
            payload=payload,
            schema_version=int(value.get("schema_version") or 0),
        )


@dataclass(frozen=True)
class EvidenceGateResult:
    passed: bool
    required_kinds: tuple[str, ...]
    verified_evidence_ids: tuple[str, ...]
    missing_kinds: tuple[str, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _kind(value: EvidenceKind | str) -> EvidenceKind:
    return value if isinstance(value, EvidenceKind) else EvidenceKind(str(value))


def _valid_identifier(value: str) -> bool:
    return bool(
        _IDENTIFIER.fullmatch(value)
        and ".." not in value
        and "//" not in value
        and not value.endswith("/")
    )


def _valid_http_target(value: Any) -> bool:
    text = str(value or "").strip()
    if text.startswith("/") and not text.startswith("//"):
        return True
    parsed = urlparse(text)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.username


def _integer(payload: Mapping[str, Any], name: str) -> int | None:
    value = payload.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _nonempty(payload: Mapping[str, Any], name: str) -> bool:
    return bool(str(payload.get(name) or "").strip())


def _has_output_reference(payload: Mapping[str, Any]) -> bool:
    return _nonempty(payload, "output_summary") or _nonempty(payload, "log_ref")


def _artifact_checks(payload: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    checks = payload.get("checks")
    if not isinstance(checks, list) or not checks:
        return None
    normalized: list[dict[str, Any]] = []
    for check in checks:
        if not isinstance(check, dict) or not _nonempty(check, "name") or not isinstance(check.get("passed"), bool):
            return None
        label = " ".join(
            str(check.get(field) or "") for field in ("name", "kind", "type")
        ).casefold()
        # Presence alone is not execution evidence. A deterministic validator
        # must inspect syntax, content, schema, checksum, policy or a contract.
        if re.search(r"\b(?:file|path|artifact)?\s*(?:exists?|existence|present|presence)\b", label):
            return None
        normalized.append(check)
    return normalized


def _api_status_matches_contract(
    payload: Mapping[str, Any],
    status_code: int,
) -> bool:
    expected = payload.get("expected_statuses")
    if expected is None:
        return 200 <= status_code < 400
    if not isinstance(expected, (list, tuple)) or not expected:
        return False
    return all(
        isinstance(item, int)
        and not isinstance(item, bool)
        and 100 <= item < 600
        for item in expected
    ) and status_code in expected


def _derive_passed(kind: EvidenceKind, payload: Mapping[str, Any]) -> bool:
    if kind in {EvidenceKind.COMMAND, EvidenceKind.BUILD}:
        return _integer(payload, "exit_code") == 0 and _nonempty(payload, "command") and _has_output_reference(payload)
    if kind == EvidenceKind.TEST:
        total = _integer(payload, "tests_total")
        failed = _integer(payload, "tests_failed")
        return (
            _integer(payload, "exit_code") == 0
            and _nonempty(payload, "command")
            and _has_output_reference(payload)
            and total is not None
            and total > 0
            and failed == 0
        )
    if kind == EvidenceKind.SERVICE_HEALTH:
        code = _integer(payload, "status_code")
        return bool(code is not None and 200 <= code < 300 and payload.get("healthy") is True and _valid_http_target(payload.get("endpoint")))
    if kind == EvidenceKind.API:
        code = _integer(payload, "status_code")
        assertions = payload.get("assertions")
        return bool(
            code is not None
            and _api_status_matches_contract(payload, code)
            and _valid_http_target(payload.get("endpoint"))
            and isinstance(assertions, list)
            and assertions
            and all(isinstance(item, dict) and item.get("passed") is True and _nonempty(item, "name") for item in assertions)
        )
    if kind == EvidenceKind.DOCKER:
        return bool(
            _integer(payload, "exit_code") == 0
            and _nonempty(payload, "command")
            and _IMAGE_DIGEST.fullmatch(str(payload.get("image_digest") or ""))
            and _has_output_reference(payload)
        )
    if kind == EvidenceKind.DEPLOYMENT:
        return bool(
            str(payload.get("provider_status") or "").lower() in {"deployed", "live", "ready", "succeeded"}
            and _nonempty(payload, "deployment_id")
            and _valid_http_target(payload.get("service_url"))
            and _COMMIT_HASH.fullmatch(str(payload.get("commit_hash") or ""))
            and _nonempty(payload, "log_ref")
        )
    if kind == EvidenceKind.COMMIT:
        return bool(_COMMIT_HASH.fullmatch(str(payload.get("commit_hash") or "")))
    if kind == EvidenceKind.ARTIFACT_VALIDATION:
        checks = _artifact_checks(payload)
        return bool(
            _valid_identifier(str(payload.get("validator") or ""))
            and checks
            and all(check["passed"] is True for check in checks)
        )
    if kind in {
        EvidenceKind.SUPERVISOR_OBSERVATION,
        EvidenceKind.HUMAN_CONFIRMATION,
    }:
        return bool(
            payload.get("passed") is True
            and _nonempty(payload, "actor_id")
            and _nonempty(payload, "observation_id")
            and _IMAGE_DIGEST.fullmatch(str(payload.get("scope_digest") or ""))
        )
    return False


def _derive_trusted_passed(
    kind: EvidenceKind,
    payload: Mapping[str, Any],
    producer: str,
    trusted_producers: Iterable[str],
) -> bool:
    trusted = frozenset(trusted_producers)
    if producer not in trusted:
        return False
    if kind == EvidenceKind.ARTIFACT_VALIDATION and producer != "metis.runner":
        return False
    if (
        kind == EvidenceKind.SUPERVISOR_OBSERVATION
        and producer != "metis.supervisor"
    ):
        return False
    if (
        kind == EvidenceKind.HUMAN_CONFIRMATION
        and producer != "metis.user_confirmation"
    ):
        return False
    return _derive_passed(kind, payload)


def create_evidence(
    kind: EvidenceKind | str,
    run_id: str,
    producer: str,
    payload: Mapping[str, Any],
    *,
    observed_at: float | None = None,
) -> EvidenceRecord:
    """Create evidence whose status is derived from observed values."""
    try:
        normalized_kind = _kind(kind)
    except ValueError as exc:
        raise ValueError("unsupported evidence kind") from exc
    normalized_run = str(run_id or "").strip()
    normalized_producer = str(producer or "").strip()
    if not _valid_identifier(normalized_run):
        raise ValueError("invalid run_id")
    if not _valid_identifier(normalized_producer):
        raise ValueError("invalid producer")
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be an object")
    timestamp = float(time.time() if observed_at is None else observed_at)
    if timestamp <= 0:
        raise ValueError("observed_at must be positive")
    safe_payload = redact_value(dict(payload))
    if not isinstance(safe_payload, dict):
        raise ValueError("payload must be an object")
    status = "passed" if _derive_trusted_passed(
        normalized_kind,
        safe_payload,
        normalized_producer,
        TRUSTED_EVIDENCE_PRODUCERS,
    ) else "failed"
    return EvidenceRecord(
        evidence_id=uuid.uuid4().hex,
        run_id=normalized_run,
        kind=normalized_kind.value,
        producer=normalized_producer,
        status=status,
        observed_at=timestamp,
        payload=safe_payload,
    )


def validate_evidence_record(
    record: EvidenceRecord | Mapping[str, Any],
    *,
    trusted_producers: Iterable[str] = TRUSTED_EVIDENCE_PRODUCERS,
) -> tuple[str, ...]:
    """Return validation errors; an empty tuple means structurally trustworthy."""
    try:
        item = record if isinstance(record, EvidenceRecord) else EvidenceRecord.from_dict(record)
    except (TypeError, ValueError) as exc:
        return (str(exc),)
    errors: list[str] = []
    if item.schema_version != EVIDENCE_SCHEMA_VERSION:
        errors.append("unsupported evidence schema")
    if not _valid_identifier(item.evidence_id):
        errors.append("invalid evidence_id")
    if not _valid_identifier(item.run_id):
        errors.append("invalid run_id")
    trusted = frozenset(trusted_producers)
    if item.producer not in trusted:
        errors.append("untrusted evidence producer")
    try:
        kind = EvidenceKind(item.kind)
    except ValueError:
        errors.append("unsupported evidence kind")
        return tuple(errors)
    if item.status not in {"passed", "failed"}:
        errors.append("invalid evidence status")
    if item.observed_at <= 0:
        errors.append("invalid observed_at")
    derived = "passed" if _derive_trusted_passed(
        kind, item.payload, item.producer, trusted
    ) else "failed"
    if item.status != derived:
        errors.append("evidence status contradicts observed values")
    if kind in {EvidenceKind.COMMAND, EvidenceKind.BUILD, EvidenceKind.TEST, EvidenceKind.DOCKER}:
        if _integer(item.payload, "exit_code") is None:
            errors.append("missing integer exit_code")
    if kind == EvidenceKind.TEST:
        if _integer(item.payload, "tests_total") is None or _integer(item.payload, "tests_failed") is None:
            errors.append("missing test counts")
    if kind in {EvidenceKind.SERVICE_HEALTH, EvidenceKind.API} and _integer(item.payload, "status_code") is None:
        errors.append("missing integer status_code")
    if kind == EvidenceKind.ARTIFACT_VALIDATION:
        if item.producer != "metis.runner":
            errors.append("artifact validation must be produced by metis.runner")
        if not _valid_identifier(str(item.payload.get("validator") or "")):
            errors.append("invalid artifact validator")
        if _artifact_checks(item.payload) is None:
            errors.append("artifact checks must contain deterministic name/passed results and not existence-only checks")
    if kind in {
        EvidenceKind.SUPERVISOR_OBSERVATION,
        EvidenceKind.HUMAN_CONFIRMATION,
    }:
        expected_producer = (
            "metis.supervisor"
            if kind == EvidenceKind.SUPERVISOR_OBSERVATION
            else "metis.user_confirmation"
        )
        if item.producer != expected_producer:
            errors.append(f"{kind.value} must be produced by {expected_producer}")
        if not _nonempty(item.payload, "actor_id"):
            errors.append("observation actor_id is required")
        if not _nonempty(item.payload, "observation_id"):
            errors.append("observation_id is required")
        if not _IMAGE_DIGEST.fullmatch(str(item.payload.get("scope_digest") or "")):
            errors.append("observation scope_digest must be sha256")
    return tuple(dict.fromkeys(errors))


def evaluate_evidence_gate(
    records: Sequence[EvidenceRecord | Mapping[str, Any]],
    required_kinds: Iterable[EvidenceKind | str],
    *,
    run_id: str = "",
    trusted_producers: Iterable[str] = TRUSTED_EVIDENCE_PRODUCERS,
) -> EvidenceGateResult:
    """Require current-run, valid, successful evidence for every requested kind."""
    normalized_required: list[str] = []
    for value in required_kinds:
        try:
            kind = _kind(value).value
        except ValueError as exc:
            raise ValueError(f"unsupported required evidence kind: {value}") from exc
        if kind not in normalized_required:
            normalized_required.append(kind)
    if not normalized_required:
        raise ValueError("at least one required evidence kind is required")
    expected_run = str(run_id or "").strip()
    if expected_run and not _valid_identifier(expected_run):
        raise ValueError("invalid run_id")

    successful_by_kind: dict[str, str] = {}
    reasons: list[str] = []
    for index, raw in enumerate(records):
        errors = validate_evidence_record(raw, trusted_producers=trusted_producers)
        if errors:
            reasons.append(f"evidence[{index}]: " + "; ".join(errors))
            continue
        item = raw if isinstance(raw, EvidenceRecord) else EvidenceRecord.from_dict(raw)
        if expected_run and item.run_id != expected_run:
            reasons.append(f"evidence[{index}]: evidence belongs to another run")
            continue
        if item.status == "passed" and item.kind in normalized_required:
            successful_by_kind.setdefault(item.kind, item.evidence_id)

    missing = tuple(kind for kind in normalized_required if kind not in successful_by_kind)
    if missing:
        reasons.append("missing successful evidence: " + ", ".join(missing))
    verified = tuple(successful_by_kind[kind] for kind in normalized_required if kind in successful_by_kind)
    return EvidenceGateResult(
        passed=not missing,
        required_kinds=tuple(normalized_required),
        verified_evidence_ids=verified,
        missing_kinds=missing,
        reasons=tuple(reasons),
    )
