"""Canonical, lossless issue records shared by QA and repair workflows.

``defect_id`` identifies the defect across QA rounds.  ``observation_id``
identifies one concrete sighting of that defect and may therefore include the
raw diagnostic/evidence.  Keeping those identities separate prevents volatile
runtime output from changing (or defining) the durable defect identity.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, Mapping, Optional


ISSUE_SCHEMA_VERSION = "metis.issue.v2"
CANONICAL_ID_PROVENANCE = "canonical_ledger"


_DYNAMIC_LOG_LINE = re.compile(
    r"^\s*(?:"
    r"\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+|"
    r"\[[0-9:.+-]+\]|"
    r"(?:request|trace|span|run|pid)[ _-]?id\s*[:=]|"
    r"at\s+.+:\d+(?::\d+)?\s*$"
    r")",
    re.IGNORECASE,
)
_LINE_REFERENCE = re.compile(
    r"\b(?:at|on)?\s*lines?\s*[:#]?\s*\d+(?::\d+)?\b|第\s*\d+\s*行",
    re.IGNORECASE,
)


def canonical_path(path: Any) -> str:
    """Return a safe project-relative POSIX path, or an empty string."""
    value = str(path or "").strip().replace("\\", "/")
    value = re.sub(r"/+", "/", value)
    while value.startswith("./"):
        value = value[2:]
    if not value or value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
        return ""
    parts = [part for part in PurePosixPath(value).parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _identity_path(path: Any) -> str:
    # Project workspaces are currently Windows-backed.  Case-folding is used
    # only for identity/matching; the display path retains its original case.
    return canonical_path(path).casefold()


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def _stable_message_fragment(message: Any) -> str:
    """Compatibility identity for unstructured findings.

    This deliberately preserves token order and numbers.  It removes only
    recognised runtime-log lines and source line references.  A few equivalent
    diagnostic phrases are normalised without globally sorting tokens.
    """
    lines = [
        line for line in str(message or "").replace("\\", "/").splitlines()
        if line.strip() and not _DYNAMIC_LOG_LINE.match(line)
    ]
    value = " ".join(lines).casefold()
    value = _LINE_REFERENCE.sub(" ", value)
    value = re.sub(
        r"\b(?:is|are|was|were)?\s*not\s+(?:implemented|defined)\b|"
        r"\bunimplemented\b|\bundefined\b|"
        r"\b(?:implementation\s+is\s+missing|missing\s+implementation)\b",
        " missing ",
        value,
    )
    if re.search(r"\bsyntax\s+error\b", value) and re.search(
        r"\bunexpected\s+token\b", value
    ):
        value = re.sub(r"\bsyntax\s+error\b|\bunexpected\s+token\b", " ", value)
        value = f"syntax error unexpected token {value}"
    value = re.sub(r"\b(?:the|remains?)\b", " ", value)
    value = re.sub(r"[\s:;,.-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip() or "unstructured"


def _extract_http_identity(message: Any) -> tuple[str, str]:
    value = str(message or "")
    expected = re.search(
        r"\bexpected(?:\s+(?:status|http))?\s*[:=]?\s*(\d{3})\b",
        value,
        re.IGNORECASE,
    )
    actual = re.search(
        r"\b(?:actual|got|received)(?:\s+(?:status|http))?\s*[:=]?\s*(\d{3})\b",
        value,
        re.IGNORECASE,
    )
    return (
        expected.group(1) if expected else "",
        actual.group(1) if actual else "",
    )


def _stable_subject(issue: Mapping[str, Any]) -> str:
    explicit = (
        issue.get("symbol")
        or issue.get("symbol_name")
        or issue.get("subject")
        or issue.get("endpoint")
    )
    if explicit:
        return _text(explicit)
    location = str(
        issue.get("stable_location") or issue.get("location") or ""
    ).strip()
    if not location:
        return ""
    # Source coordinates are observation evidence.  A named symbol or endpoint
    # is stable identity; bare line/column locations are not.
    without_coordinates = re.sub(
        r"(?::\d+){1,2}$|\b(?:line|column|col)\s*[:#]?\s*\d+\b",
        "",
        location,
        flags=re.IGNORECASE,
    ).strip(" :#")
    if not without_coordinates or re.fullmatch(r"\d+(?::\d+)?", without_coordinates):
        return ""
    # A repeated file path plus a source coordinate is still only an
    # observation.  Treating ``app.py:41`` as a stable subject would
    # incorrectly upgrade a rule+path bucket to high-confidence identity.
    issue_path = canonical_path(issue.get("file_path") or issue.get("file"))
    location_path = canonical_path(without_coordinates)
    if issue_path and location_path.casefold() in {
        issue_path.casefold(),
        PurePosixPath(issue_path).name.casefold(),
    }:
        return ""
    return _text(without_coordinates)


def _has_high_confidence_identity(
    issue: Mapping[str, Any],
    *,
    subject: str,
    expected: str,
    actual: str,
) -> bool:
    # A rule and path only describe a bucket and can contain several defects.
    # A stable subject or directional expected/actual pair is required before
    # the ledger may drive automatic closure/rollback decisions.
    return bool(subject or expected or actual)


def defect_fingerprint(issue: Mapping[str, Any]) -> str:
    """Build the durable identity; raw message/evidence never lead identity."""
    rule = _text(issue.get("rule_id") or issue.get("rule") or issue.get("layer") or "unknown")
    path = _identity_path(issue.get("file_path") or issue.get("file"))
    symbol = _stable_subject(issue)
    expected = _text(issue.get("expected"))
    actual = _text(issue.get("actual"))
    if not expected and not actual:
        expected, actual = _extract_http_identity(issue.get("message"))
    structured = _has_high_confidence_identity(
        issue,
        subject=symbol,
        expected=expected,
        actual=actual,
    )
    identity = {
        "v": 2,
        "rule": rule,
        "path": path,
        "symbol_or_location": symbol,
        "expected": expected,
        "actual": actual,
    }
    if not structured:
        # Compatibility only.  Such records are explicitly low-confidence and
        # must not drive automatic rollback/closure decisions.
        identity["unstructured_message"] = _stable_message_fragment(issue.get("message"))
    return json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_defect_id(issue: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(defect_fingerprint(issue).encode("utf-8")).hexdigest()[:16]
    derived = f"issue-{digest}"
    if (
        issue.get("issue_schema_version") == ISSUE_SCHEMA_VERSION
        and issue.get("defect_id_provenance") == CANONICAL_ID_PROVENANCE
        and issue.get("defect_id")
        and str(issue.get("defect_id")) == derived
    ):
        return str(issue["defect_id"])
    return derived


def observation_id(
    issue: Mapping[str, Any],
    *,
    observation_context: Optional[Mapping[str, Any]] = None,
) -> str:
    payload = {
        "defect_id": canonical_defect_id(issue),
        "context": dict(observation_context or {}),
        "message": issue.get("message"),
        "evidence": issue.get("evidence"),
        "line": issue.get("line"),
        "line_no": issue.get("line_no"),
        "location": issue.get("location"),
        "stable_location": issue.get("stable_location"),
        "severity": issue.get("severity"),
        "actual": issue.get("actual"),
    }
    digest = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    ).hexdigest()[:20]
    return f"observation-{digest}"


def canonicalize_issue(
    issue: Mapping[str, Any],
    *,
    defaults: Optional[Mapping[str, Any]] = None,
    observation_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a canonical record without discarding any source fields."""
    record: Dict[str, Any] = dict(defaults or {})
    record.update(dict(issue))
    source_is_canonical = (
        record.get("issue_schema_version") == ISSUE_SCHEMA_VERSION
        and record.get("defect_id_provenance") == CANONICAL_ID_PROVENANCE
        and bool(record.get("defect_id"))
        and str(record.get("defect_id")) == canonical_defect_id(record)
    )
    if not source_is_canonical:
        legacy_id = record.get("id")
        source_issue_id = record.get("issue_id") or record.get("defect_id")
        if legacy_id:
            record["legacy_id"] = str(legacy_id)
        if source_issue_id:
            record["source_issue_id"] = str(source_issue_id)

    display_path = canonical_path(record.get("file_path") or record.get("file"))
    if display_path:
        record["file_path"] = display_path

    if "line" not in record and record.get("line_no") is not None:
        record["line"] = record["line_no"]
    if "line_no" not in record and record.get("line") is not None:
        record["line_no"] = record["line"]

    if "detected_phase" not in record:
        record["detected_phase"] = (
            record.get("detected_phase_id")
            or record.get("phase_id")
            or record.get("source_phase_id")
            or ""
        )

    owner = record.get("owner")
    if isinstance(owner, Mapping):
        record.setdefault(
            "responsible_agent_id",
            owner.get("agent_id") or owner.get("id") or "",
        )
        record.setdefault(
            "responsible_agent_role",
            owner.get("agent_role") or owner.get("role") or owner.get("owner_type") or "",
        )
    elif owner:
        record.setdefault("responsible_agent_id", str(owner))
    record.setdefault("responsible_agent_id", record.get("agent_id") or "")
    record.setdefault("responsible_agent_role", record.get("agent_role") or "")

    # These fields are first-class even when absent so every consumer sees one
    # schema.  Existing non-empty values retain their original type/content.
    record.setdefault("evidence", None)
    record.setdefault("acceptance_criteria", None)
    record.setdefault("detected_phase", "")

    fingerprint = defect_fingerprint(record)
    defect_id = canonical_defect_id(record)
    expected = _text(record.get("expected"))
    actual = _text(record.get("actual"))
    if not expected and not actual:
        expected, actual = _extract_http_identity(record.get("message"))
    structured = _has_high_confidence_identity(
        record,
        subject=_stable_subject(record),
        expected=expected,
        actual=actual,
    )
    record["fingerprint"] = fingerprint
    record["defect_id"] = defect_id
    record["issue_id"] = defect_id
    record["id"] = defect_id
    record["issue_schema_version"] = ISSUE_SCHEMA_VERSION
    record["defect_id_provenance"] = CANONICAL_ID_PROVENANCE
    record["identity_confidence"] = "high" if structured else "low"
    record["requires_identity_review"] = not structured
    record["observation_id"] = observation_id(
        record, observation_context=observation_context
    )
    return record


def mark_needs_manual(
    issue: Mapping[str, Any],
    reason: str,
    *,
    defaults: Optional[Mapping[str, Any]] = None,
    observation_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The sole conversion helper for a persisted manual defect."""
    record = canonicalize_issue(
        issue,
        defaults=defaults,
        observation_context=observation_context,
    )
    record["status"] = "needs_manual"
    record["needs_manual"] = True
    record["needs_manual_reason"] = str(reason or record.get("needs_manual_reason") or "")
    if record.get("manual_since") is None:
        record["manual_since"] = time.time()
    return record


def refresh_issue(
    previous: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> Dict[str, Any]:
    """Refresh display/routing data while preserving immutable defect history."""
    old = canonicalize_issue(previous)
    new = canonicalize_issue(observation)
    refreshed = dict(old)
    for field in (
        "file_path",
        "line",
        "line_no",
        "evidence",
        "acceptance_criteria",
        "responsible_agent_id",
        "responsible_agent_role",
        "fix_hint",
        "layer",
        "severity",
        "expected",
        "actual",
        "symbol",
        "location",
    ):
        if field in new and new.get(field) not in (None, ""):
            refreshed[field] = new[field]
    refreshed["latest_observation_id"] = new["observation_id"]
    refreshed["observation_id"] = new["observation_id"]
    # Preserve the original durable identity even if later presentation fields
    # differ; callers match by fingerprint before invoking this helper.
    for field in ("id", "issue_id", "defect_id", "fingerprint", "detected_at", "detected_phase"):
        if old.get(field) not in (None, ""):
            refreshed[field] = old[field]
    return refreshed


def find_owner_for_path(
    file_registry: Mapping[str, Mapping[str, Any]],
    path: Any,
) -> Dict[str, Any]:
    """Resolve ``./`` and slash variants; case-fold only when unambiguous."""
    target = canonical_path(path)
    if not target:
        return {}
    exact = [
        dict(owner)
        for key, owner in file_registry.items()
        if canonical_path(owner.get("file_path") or key) == target
    ]
    if len(exact) == 1:
        return exact[0]
    folded = [
        dict(owner)
        for key, owner in file_registry.items()
        if _identity_path(owner.get("file_path") or key) == target.casefold()
    ]
    return folded[0] if len(folded) == 1 else {}


def iter_canonical_issues(qc_results: Mapping[str, Any]) -> Iterable[Dict[str, Any]]:
    """Yield every persisted QA issue in the canonical projection."""
    for value in qc_results.values():
        if not isinstance(value, Mapping):
            continue
        qa = value.get("qa") if isinstance(value.get("qa"), Mapping) else value
        for issue in qa.get("issues_detail", []) or []:
            if isinstance(issue, Mapping):
                yield canonicalize_issue(issue)


def find_canonical_issue(qc_results: Mapping[str, Any], defect_id: str) -> Optional[Dict[str, Any]]:
    wanted = str(defect_id or "")
    issues = list(iter_canonical_issues(qc_results))
    canonical_matches = [issue for issue in issues if issue["defect_id"] == wanted]
    if len(canonical_matches) == 1:
        return canonical_matches[0]
    # Legacy ids are retained as migration evidence only.  They are neither
    # globally unique nor semantically directional, so accepting one as a
    # lookup key could select the wrong defect and authorize the wrong repair.
    return None
