"""Security primitives for secret redaction and durable, scoped audit events.

This module deliberately has no dependency on the HTTP layer.  Callers pass
already-authorized actor and project identifiers, while query helpers enforce
that a non-privileged caller can only see its own events in projects it may
access.  Audit payloads are recursively redacted before either persistence or
logging.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from core.database import kv_get, kv_keys_prefix, kv_set


REDACTED = "[REDACTED]"
AUDIT_KEY_PREFIX = "audit:v1:"
AUDIT_SCHEMA_VERSION = 1
_MAX_TEXT_LENGTH = 8_192
_MAX_DETAILS_BYTES = 32_768
_MAX_REDACTION_DEPTH = 12
_PRIVILEGED_AUDIT_ROLES = frozenset({"admin", "auditor"})

_SENSITIVE_KEY = re.compile(
    r"(?i)(?:^|[_-])(?:"
    r"api[_-]?key|authorization|cookie|credential|database[_-]?url|dsn|"
    r"jwt|pass(?:word|phrase)?|private[_-]?key|refresh[_-]?token|"
    r"secret|session|smtp[_-]?password|token"
    r")(?:$|[_-])"
)
_ACTION = re.compile(r"^[A-Z][A-Z0-9_.:-]{1,79}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}$")
_JWT = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_VENDOR_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk-[A-Za-z0-9_-]{16,}|"
    r"gh[opsu]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}"
    r")"
)
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|cookie|database[_-]?url|dsn|jwt|"
    r"password|passphrase|private[_-]?key|refresh[_-]?token|secret|"
    r"session[_-]?token|smtp[_-]?password|token)"
    r"(\s*[:=]\s*)(?!\[REDACTED\])([^\s,;]+|\"[^\"]*\"|'[^']*')"
)
_URL_WITH_CREDENTIALS = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|https?)://"
    r"[^\s/@:]+:[^\s/@]+@[^\s]+",
    re.IGNORECASE,
)


def _is_sensitive_key(key: str) -> bool:
    if _SENSITIVE_KEY.search(key):
        return True
    collapsed = re.sub(r"[^a-z0-9]", "", key.casefold())
    return any(
        marker in collapsed
        for marker in (
            "accesstoken",
            "apikey",
            "authorization",
            "clientsecret",
            "credential",
            "databaseurl",
            "jwtsecret",
            "password",
            "privatekey",
            "refreshtoken",
            "sessiontoken",
            "smtppassword",
        )
    )


def _redact_url(match: re.Match[str]) -> str:
    value = match.group(0)
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit((parsed.scheme, f"{REDACTED}@{hostname}{port}", parsed.path, parsed.query, parsed.fragment))
    except (TypeError, ValueError):
        scheme = value.split("://", 1)[0]
        return f"{scheme}://{REDACTED}"


def redact_text(value: Any, *, max_length: int = _MAX_TEXT_LENGTH) -> str:
    """Redact common credentials embedded in free-form text.

    The return value is bounded so an upstream provider cannot turn an error
    message into an unbounded log or audit record.
    """
    text = str(value or "")
    text = _URL_WITH_CREDENTIALS.sub(_redact_url, text)
    text = _BEARER.sub(lambda match: f"{match.group(1)} {REDACTED}", text)
    text = _JWT.sub(REDACTED, text)
    text = _VENDOR_TOKEN.sub(REDACTED, text)
    text = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text)
    if len(text) > max_length:
        text = text[:max_length] + "...[TRUNCATED]"
    return text


def redact_value(value: Any, *, _depth: int = 0, _seen: set[int] | None = None) -> Any:
    """Return a JSON-safe, recursively redacted copy of ``value``."""
    if _depth > _MAX_REDACTION_DEPTH:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"[BINARY {len(value)} bytes]"
    if isinstance(value, str):
        return redact_text(value)

    seen = _seen if _seen is not None else set()
    trackable = isinstance(value, (Mapping, list, tuple, set, frozenset))
    if trackable:
        identity = id(value)
        if identity in seen:
            return "[CYCLE]"
        seen.add(identity)
    try:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for raw_key, item in value.items():
                key = redact_text(raw_key, max_length=256)
                result[key] = REDACTED if _is_sensitive_key(key) else redact_value(
                    item, _depth=_depth + 1, _seen=seen
                )
            return result
        if isinstance(value, (list, tuple, set, frozenset)):
            return [redact_value(item, _depth=_depth + 1, _seen=seen) for item in value]
        return redact_text(value)
    finally:
        if trackable:
            seen.discard(id(value))


class RedactingFormatter(logging.Formatter):
    """Logging formatter that redacts message arguments and tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        copied = logging.makeLogRecord(record.__dict__.copy())
        copied.msg = redact_text(record.getMessage())
        copied.args = ()
        copied.exc_text = None
        return redact_text(super().format(copied))

    def formatException(self, exc_info: Any) -> str:  # noqa: N802 - logging API
        return redact_text(super().formatException(exc_info))


def install_secret_redaction(
    logger: logging.Logger | None = None,
    *,
    format_string: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt: str = "%Y-%m-%d %H:%M:%S",
) -> None:
    """Install redaction on existing handlers without changing log levels."""
    target = logger or logging.getLogger()
    formatter = RedactingFormatter(format_string, datefmt=datefmt)
    for handler in target.handlers:
        handler.setFormatter(formatter)


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    action: str
    actor_id: str
    actor_role: str
    outcome: str
    occurred_at: float
    project_id: str = ""
    resource_type: str = ""
    resource_id: str = ""
    source_ip: str = "unknown"
    request_id: str = ""
    details: Any = None
    schema_version: int = AUDIT_SCHEMA_VERSION
    integrity_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validate_identifier(name: str, value: str, *, required: bool = False) -> str:
    normalized = str(value or "").strip()
    if required and not normalized:
        raise ValueError(f"{name} is required")
    if normalized and (
        not _IDENTIFIER.fullmatch(normalized)
        or ".." in normalized
        or "//" in normalized
        or normalized.endswith("/")
    ):
        raise ValueError(f"invalid {name}")
    return normalized


def _normalize_ip(value: str) -> str:
    normalized = str(value or "unknown").strip()
    if normalized == "unknown":
        return normalized
    try:
        return str(ipaddress.ip_address(normalized))
    except ValueError as exc:
        raise ValueError("invalid source_ip") from exc


def _event_hash(payload: Mapping[str, Any]) -> str:
    canonical = {key: value for key, value in payload.items() if key != "integrity_sha256"}
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_details(details: Any) -> Any:
    safe = redact_value(details if details is not None else {})
    encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > _MAX_DETAILS_BYTES:
        return {"summary": redact_text(encoded[:_MAX_DETAILS_BYTES].decode("utf-8", "ignore")) + "...[TRUNCATED]"}
    return safe


def record_audit_event(
    action: str,
    actor_id: str,
    *,
    actor_role: str = "user",
    outcome: str = "success",
    project_id: str = "",
    resource_type: str = "",
    resource_id: str = "",
    source_ip: str = "unknown",
    request_id: str = "",
    details: Any = None,
    occurred_at: float | None = None,
) -> AuditEvent:
    """Persist a redacted audit event under a collision-resistant key."""
    normalized_action = str(action or "").strip().upper()
    if not _ACTION.fullmatch(normalized_action):
        raise ValueError("invalid audit action")
    normalized_outcome = str(outcome or "").strip().lower()
    if normalized_outcome not in {"success", "failure", "denied", "cancelled"}:
        raise ValueError("invalid audit outcome")
    timestamp = float(time.time() if occurred_at is None else occurred_at)
    if timestamp <= 0:
        raise ValueError("occurred_at must be positive")

    payload: dict[str, Any] = {
        "event_id": uuid.uuid4().hex,
        "action": normalized_action,
        "actor_id": _validate_identifier("actor_id", actor_id, required=True),
        "actor_role": _validate_identifier("actor_role", actor_role, required=True).lower(),
        "outcome": normalized_outcome,
        "occurred_at": timestamp,
        "project_id": _validate_identifier("project_id", project_id),
        "resource_type": _validate_identifier("resource_type", resource_type),
        "resource_id": _validate_identifier("resource_id", resource_id),
        "source_ip": _normalize_ip(source_ip),
        "request_id": _validate_identifier("request_id", request_id),
        "details": _bounded_details(details),
        "schema_version": AUDIT_SCHEMA_VERSION,
    }
    payload["integrity_sha256"] = _event_hash(payload)
    event = AuditEvent(**payload)
    kv_set(f"{AUDIT_KEY_PREFIX}{event.event_id}", event.to_dict())
    return event


def _load_verified_event(key: str) -> AuditEvent | None:
    payload = kv_get(key, None)
    if not isinstance(payload, dict) or payload.get("schema_version") != AUDIT_SCHEMA_VERSION:
        return None
    if not isinstance(payload.get("integrity_sha256"), str) or payload["integrity_sha256"] != _event_hash(payload):
        return None
    try:
        return AuditEvent(**payload)
    except (TypeError, ValueError):
        return None


def query_audit_events(
    requester_actor_id: str,
    requester_role: str,
    *,
    allowed_project_ids: Iterable[str] = (),
    actor_id: str = "",
    project_id: str = "",
    action: str = "",
    outcome: str = "",
    before: float | None = None,
    limit: int = 100,
) -> list[AuditEvent]:
    """Query audit events with fail-closed actor and project isolation."""
    requester = _validate_identifier("requester_actor_id", requester_actor_id, required=True)
    role = _validate_identifier("requester_role", requester_role, required=True).lower()
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    cutoff = float("inf") if before is None else float(before)
    if cutoff <= 0:
        raise ValueError("before must be positive")
    requested_actor = _validate_identifier("actor_id", actor_id)
    requested_project = _validate_identifier("project_id", project_id)
    allowed = {_validate_identifier("allowed_project_id", item, required=True) for item in allowed_project_ids}
    privileged = role in _PRIVILEGED_AUDIT_ROLES
    if not privileged:
        if requested_actor and requested_actor != requester:
            raise PermissionError("cannot query another actor's audit events")
        if requested_project and requested_project not in allowed:
            raise PermissionError("cannot query audit events for an unauthorized project")
        requested_actor = requester

    requested_action = str(action or "").strip().upper()
    if requested_action and not _ACTION.fullmatch(requested_action):
        raise ValueError("invalid audit action")
    requested_outcome = str(outcome or "").strip().lower()
    if requested_outcome and requested_outcome not in {"success", "failure", "denied", "cancelled"}:
        raise ValueError("invalid audit outcome")

    events: list[AuditEvent] = []
    for key in kv_keys_prefix(AUDIT_KEY_PREFIX):
        event = _load_verified_event(key)
        if event is None or event.occurred_at >= cutoff:
            continue
        if requested_actor and event.actor_id != requested_actor:
            continue
        if requested_project and event.project_id != requested_project:
            continue
        if requested_action and event.action != requested_action:
            continue
        if requested_outcome and event.outcome != requested_outcome:
            continue
        if not privileged and event.project_id and event.project_id not in allowed:
            continue
        events.append(event)
    events.sort(key=lambda item: (item.occurred_at, item.event_id), reverse=True)
    return events[:limit]


def safe_audit_log_line(event: AuditEvent) -> str:
    """Return a bounded redacted line suitable for the application log."""
    return redact_text(
        f"AUDIT action={event.action} actor={event.actor_id} outcome={event.outcome} "
        f"project={event.project_id or '-'} event_id={event.event_id}"
    )
