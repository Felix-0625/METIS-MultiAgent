"""Deterministic gates that must pass before Supervisor business QA.

The verifier intentionally does not know about phase routes or QA round
accounting.  It accepts snapshots and injected runners, returns immutable
evidence, and always marks failures as non-consuming for business QA.  This
keeps model/provider failures, infrastructure failures and project defects out
of the five-round Supervisor budget.
"""

from __future__ import annotations

import gc
import hashlib
import json
import os
import re
import secrets
import signal
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from core.security_audit import redact_text


PRE_QA_EVIDENCE_VERSION = 1
FAILURE_PRE_QA = "pre_qa_failed"
FAILURE_INFRASTRUCTURE = "infrastructure_failed"
FAILURE_MODEL = "model_failed"


@dataclass(frozen=True)
class VerificationIssue:
    code: str
    message: str
    path: str = ""
    gate: str = "contract"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class CommandGate:
    gate_id: str
    kind: str
    command: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: float = 900
    environment: Mapping[str, str] = field(default_factory=dict)
    required: bool = True


@dataclass(frozen=True)
class CommandObservation:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    failure_category: str = ""


class CommandRunner(Protocol):
    def __call__(self, gate: CommandGate, workspace: Path) -> CommandObservation:
        ...


@dataclass(frozen=True)
class ApiProbe:
    probe_id: str
    method: str
    path: str
    expected_statuses: tuple[int, ...]
    body: Mapping[str, Any] = field(default_factory=dict)
    actor: str = "user"
    require_json: bool = True
    invariant: str = ""
    forbidden_response_keys: tuple[str, ...] = (
        "password", "api_key", "token", "jwt", "owner_id", "user_id"
    )


@dataclass(frozen=True)
class ApiObservation:
    status_code: int
    is_json: bool
    body: Any = None
    log: str = ""
    failure_category: str = ""


class ApiProbeRunner(Protocol):
    def __call__(self, probe: ApiProbe) -> ApiObservation:
        ...


@dataclass(frozen=True)
class PreQAEvidence:
    kind: str
    command: str
    exit_code: int
    passed: bool | None
    scope_digest: str
    commit_digest: str
    log_digest: str
    recorded_at: str
    gate_id: str
    schema_version: int = PRE_QA_EVIDENCE_VERSION
    log_excerpt: str = ""
    status_code: int | None = None
    endpoint: str = ""
    assertions: tuple[dict[str, Any], ...] = ()
    applicable: bool = True
    executed: bool = True

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["assertions"] = [
            dict(assertion) for assertion in self.assertions
        ]
        return value


@dataclass(frozen=True)
class PreQAResult:
    passed: bool
    status: str
    failure_category: str
    failed_gate: str
    issues: tuple[VerificationIssue, ...]
    evidence: tuple[PreQAEvidence, ...]
    consumes_business_qa_round: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["issues"] = [issue.to_dict() for issue in self.issues]
        value["evidence"] = [record.to_dict() for record in self.evidence]
        return value


_PLACEHOLDER_PATTERNS = (
    (
        "placeholder",
        re.compile(
            r"(?:^[ \t]*(?:#[ \t]*)?|//[ \t]*|/\*+[ \t]*|\*[ \t]*)"
            r"(?:TODO|FIXME|XXX)\b",
            re.MULTILINE,
        ),
    ),
    ("pseudocode", re.compile(r"\b(?:pseudocode|not\s+implemented|implement\s+me)\b", re.IGNORECASE)),
    ("execution_log", re.compile(r"^(?:npm\s+ERR!|Traceback \(most recent call last\)|\[command output\])", re.IGNORECASE | re.MULTILINE)),
)
_CSS_PLACEHOLDER_PATTERN = re.compile(
    r"(?:^[ \t]*|/\*+[ \t]*|\*[ \t]*)(?:TODO|FIXME|XXX)\b",
    re.MULTILINE,
)
_MARKDOWN_PLACEHOLDER_PATTERN = re.compile(
    r"^[ \t]*(?:<!--[ \t]*|//[ \t]*|\*[ \t]*)?"
    r"(?:TODO|FIXME|XXX)\b",
    re.MULTILINE,
)
_FULLSTACK_OWNER_TYPES = {
    "frontend",
    "backend",
    "database",
    "qa",
    "devops",
    "security",
    "architecture",
    "data",
    "fullstack_engineer",
}


def _agent_type_matches_owner(actual_type: str, expected_types: set[str]) -> bool:
    if actual_type in expected_types:
        return True
    return (
        actual_type == "fullstack_engineer"
        and bool(expected_types & _FULLSTACK_OWNER_TYPES)
    )
_JWT_FALLBACK = re.compile(
    r"(?:process\.env\.)?JWT_SECRET\s*(?:\|\||\?\?)\s*['\"][^'\"]+['\"]",
    re.IGNORECASE,
)
_JWT_HARDCODED_CALL = re.compile(
    r"\b(?:jwt\.)?(?:sign|verify)\s*\([^,]+,\s*['\"][^'\"]+['\"]",
    re.IGNORECASE | re.DOTALL,
)
_JWT_FAIL_CLOSED = re.compile(
    r"(?:if\s*\(\s*!\s*(?:process\.env\.)?JWT_SECRET\s*\)|"
    r"JWT_SECRET\s*=\s*(?:requiredEnv|requireEnv|getRequiredEnv|mustGetEnv)\s*\(|"
    r"(?:requiredEnv|requireEnv|getRequiredEnv|mustGetEnv)\s*\(\s*['\"]JWT_SECRET['\"])",
    re.IGNORECASE,
)
_SENSITIVE_LOG = re.compile(
    r"(?:console\.(?:log|info|warn|error)|logger\.(?:debug|info|warn|error)|"
    r"log\.(?:debug|info|warn|error))\s*\([^)]*\b(?:JWT_SECRET|password|api[_-]?key|token)\b",
    re.IGNORECASE | re.DOTALL,
)
_MODEL_FAILURE = re.compile(
    r"(?:invalid\s+api\s*key|(?:provider|model\s+output).{0,40}invalid\s+json|"
    r"output\s+(?:was\s+)?truncated|finish_reason.?length|"
    r"context\s+(?:length|window)|rate\s*limit|too\s+many\s+requests|provider.{0,20}\b5\d\d\b)",
    re.IGNORECASE,
)
_INFRA_FAILURE = re.compile(
    r"(?:timed?\s*out|timeout|connection\s+(?:reset|refused)|temporary\s+failure|"
    r"docker\s+(?:daemon|is not running)|no\s+space\s+left|network\s+unreachable|"
    r"infrastructure|environment\s+misconfigured|reached\s+heap\s+limit|"
    r"heap\s+out\s+of\s+memory|allocation\s+failed|"
    r"could\s+not\s+find\s+any\s+visual\s+studio\s+installation)",
    re.IGNORECASE,
)
_RESOURCE_FAILURE = re.compile(
    r"(?:^\s*(?:npm\s+err!\s*)?killed\s*$|"
    r"\bsigkill\b|\benomem\b|\boom(?:[-_\s]+killed|[-_\s]+killer)?\b|"
    r"cannot\s+allocate\s+memory|memory\s+limit|"
    r"(?:exit(?:ed)?|return(?:ed)?)\s+(?:(?:with|status|code)\s+)*137\b|"
    r"(?:exit|status|code)\s*[:=]?\s*137\b)",
    re.IGNORECASE | re.MULTILINE,
)


def _normalize_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe artifact path: {raw or '<empty>'}")
    return path.as_posix()


def _owner_id(value: Mapping[str, Any]) -> str:
    return str(
        value.get("agent_id")
        or value.get("owner_id")
        or value.get("responsible_agent_id")
        or ""
    ).strip()


def _required_file_rows(contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = contract.get("required_files") or []
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, str):
            normalized.append({"path": _normalize_path(row), "required": True})
        elif isinstance(row, Mapping) and row.get("required", True):
            normalized.append({**row, "path": _normalize_path(row.get("path"))})
    return normalized


def _path_is_allowed(path: str, prefixes: Iterable[Any]) -> bool:
    for raw_prefix in prefixes:
        try:
            prefix = _normalize_path(raw_prefix)
        except ValueError:
            continue
        if prefix.endswith("/") or str(raw_prefix).replace("\\", "/").endswith("/"):
            if path.startswith(prefix.rstrip("/") + "/"):
                return True
        elif path == prefix:
            return True
    return False


def _agent_map(agents: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {
        str(agent.get("id") or agent.get("agent_id") or "").strip(): agent
        for agent in agents
        if str(agent.get("id") or agent.get("agent_id") or "").strip()
    }


def verify_contract_artifacts(
    workspace: Path | str,
    contract: Mapping[str, Any],
    file_registry: Mapping[str, Mapping[str, Any]],
    agents: Iterable[Mapping[str, Any]],
) -> tuple[VerificationIssue, ...]:
    """Validate required files, registry ownership, scope and real content."""
    root = Path(workspace).resolve()
    issues: list[VerificationIssue] = []
    try:
        rows = _required_file_rows(contract)
    except ValueError as exc:
        return (VerificationIssue("unsafe_required_path", str(exc)),)
    owners_by_path: dict[str, set[str]] = {}
    owner_types_by_path: dict[str, set[str]] = {}
    for row in rows:
        path = row["path"]
        owner = str(row.get("agent_id") or row.get("owner_id") or "").strip()
        owner_type = str(row.get("owner_type") or "").strip()
        if owner:
            owners_by_path.setdefault(path, set()).add(owner)
        if owner_type:
            owner_types_by_path.setdefault(path, set()).add(owner_type)
    for path, owners in owners_by_path.items():
        if len(owners) > 1:
            issues.append(VerificationIssue("multiple_contract_owners", "required file has multiple contract owners", path))
    for path, owner_types in owner_types_by_path.items():
        if len(owner_types) > 1:
            issues.append(VerificationIssue("multiple_owner_types", "required file has multiple owner types", path))

    by_id = _agent_map(agents)
    registry_by_path: dict[str, list[Mapping[str, Any]]] = {}
    for raw_path, raw_entry in file_registry.items():
        entries = raw_entry if isinstance(raw_entry, list) else [raw_entry]
        try:
            normalized = _normalize_path(raw_path)
        except ValueError:
            continue
        registry_by_path[normalized] = [entry for entry in entries if isinstance(entry, Mapping)]

    seen: set[str] = set()
    for row in rows:
        path = row["path"]
        if path in seen:
            continue
        seen.add(path)
        target = (root / path).resolve()
        if root != target and root not in target.parents:
            issues.append(VerificationIssue("path_escape", "required file escapes workspace", path))
            continue
        if not target.is_file():
            issues.append(VerificationIssue("missing_required_file", "required file does not exist", path))
            continue
        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            issues.append(VerificationIssue("unreadable_required_file", "required file is not readable UTF-8 text", path))
            continue
        empty_marker_files = {"__init__.py", "__init__.pyi", ".gitkeep", "py.typed"}
        if not content.strip() and target.name not in empty_marker_files:
            issues.append(VerificationIssue("empty_required_file", "required file is empty", path))
        for label, pattern in _PLACEHOLDER_PATTERNS:
            effective_pattern = (
                _CSS_PLACEHOLDER_PATTERN
                if label == "placeholder"
                and target.suffix.lower() in {".css", ".less", ".sass", ".scss"}
                else _MARKDOWN_PLACEHOLDER_PATTERN
                if label == "placeholder"
                and target.suffix.lower() in {".md", ".markdown"}
                else pattern
            )
            if effective_pattern.search(content):
                issues.append(VerificationIssue(f"forbidden_{label}", f"required file contains {label}", path))

        entries = registry_by_path.get(path, [])
        if not entries:
            issues.append(VerificationIssue("unregistered_required_file", "required file is absent from file_registry", path))
            continue
        registry_owners = {_owner_id(entry) for entry in entries if _owner_id(entry)}
        if len(entries) != 1 or len(registry_owners) != 1:
            issues.append(VerificationIssue("non_unique_registry_owner", "required file must have exactly one registry owner", path))
            continue
        owner_id = next(iter(registry_owners))
        expected = owners_by_path.get(path)
        if expected and owner_id not in expected:
            issues.append(VerificationIssue("owner_mismatch", "registry owner differs from contract owner", path))
        agent = by_id.get(owner_id)
        if not agent:
            issues.append(VerificationIssue("unknown_file_owner", "registry owner is not an active or recorded agent", path))
            continue
        verification_paths: set[str] = set()
        for item in (agent.get("verification_paths") or []):
            try:
                verification_paths.add(_normalize_path(item))
            except ValueError:
                continue
        preservation_receipt = (
            (agent.get("preservation_receipts") or {}).get(path) or {}
        )
        preservation_evidence = preservation_receipt.get("evidence") or []
        current_digest = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
        valid_preservation_owner = (
            agent.get("verification_only") is True
            and path in verification_paths
            and preservation_receipt.get("status") == "succeeded"
            and bool(preservation_receipt.get("start_run_id"))
            and preservation_receipt.get("start_run_id")
            == preservation_receipt.get("completion_run_id")
            and preservation_receipt.get("baseline_digest") == current_digest
            and preservation_receipt.get("artifact_digest") == current_digest
            and any(
                isinstance(evidence, Mapping)
                and evidence.get("producer") == "metis.runner"
                and evidence.get("status") == "passed"
                and (evidence.get("payload") or {}).get("validator")
                == "phase.preserve_baseline_digest"
                and (evidence.get("payload") or {}).get("artifact_digest")
                == current_digest
                and (evidence.get("payload") or {}).get("agent_id") == owner_id
                for evidence in preservation_evidence
            )
        )
        if (
            not _path_is_allowed(path, agent.get("allowed_path_prefixes") or [])
            and not valid_preservation_owner
        ):
            issues.append(VerificationIssue("owner_scope_violation", "file path is outside its owner's allowed paths", path))
        expected_types = owner_types_by_path.get(path)
        actual_type = str(agent.get("owner_type") or agent.get("expert_type") or agent.get("agent_type") or "").strip()
        if expected_types and not _agent_type_matches_owner(
            actual_type, expected_types
        ):
            issues.append(VerificationIssue("owner_type_mismatch", "agent type differs from required file owner_type", path))
    return tuple(issues)


def verify_jwt_invariants(
    workspace: Path | str,
    paths: Iterable[str],
    *,
    jwt_required: bool = True,
) -> tuple[VerificationIssue, ...]:
    """Fast static JWT precheck; runtime startup/API tests remain authoritative."""
    root = Path(workspace).resolve()
    combined: list[str] = []
    issues: list[VerificationIssue] = []
    jwt_paths: list[str] = []
    for raw_path in paths:
        try:
            path = _normalize_path(raw_path)
        except ValueError:
            continue
        target = (root / path).resolve()
        if not target.is_file() or target.suffix.lower() not in {".js", ".cjs", ".mjs", ".ts", ".tsx"}:
            continue
        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if "JWT_SECRET" not in content and not re.search(r"\bjwt\.(?:sign|verify)\b", content, re.I):
            continue
        jwt_paths.append(path)
        combined.append(content)
        if _JWT_FALLBACK.search(content):
            issues.append(VerificationIssue("jwt_default_secret", "JWT_SECRET must not have a fallback value", path, "security"))
        if _JWT_HARDCODED_CALL.search(content):
            issues.append(VerificationIssue("jwt_hardcoded_secret", "JWT sign/verify must not use a hard-coded secret", path, "security"))
        if _SENSITIVE_LOG.search(content):
            issues.append(VerificationIssue("sensitive_value_logged", "JWT, password, API key and token values must not be logged", path, "security"))
    source = "\n".join(combined)
    if jwt_required and not jwt_paths:
        issues.append(VerificationIssue("jwt_implementation_missing", "JWT is required but no JWT implementation was found", gate="security"))
    if jwt_paths and "process.env.JWT_SECRET" not in source:
        issues.append(VerificationIssue("jwt_env_missing", "JWT implementation must read process.env.JWT_SECRET", jwt_paths[0], "security"))
    if jwt_paths and not _JWT_FAIL_CLOSED.search(source):
        issues.append(VerificationIssue("jwt_not_fail_closed", "application must fail closed when JWT_SECRET is missing", jwt_paths[0], "security"))
    return tuple(issues)


_API_PROBE_CRITERION = re.compile(
    r"\b(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+"
    r"(?P<path>/[A-Za-z0-9_./{}:@?=&%-]+)"
    r"(?P<trailer>[\s\S]*?)"
    r"(?=\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+/|\Z)",
    re.IGNORECASE,
)


def _inline_api_request_body(text: str) -> dict[str, Any]:
    body: dict[str, Any] = {}
    object_match = re.search(r"\{([^{}]{1,300})\}", text)
    if object_match:
        for field in re.finditer(
            r"['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\s*:\s*"
            r"(?:['\"]([^'\"]*)['\"]|(-?\d+(?:\.\d+)?)|(true|false))",
            object_match.group(1),
            re.IGNORECASE,
        ):
            key = field.group(1)
            if field.group(2) is not None:
                value = field.group(2)
                body[key] = (
                    "metis-runtime-check"
                    if not value or set(value) == {"."}
                    else value
                )
            elif field.group(3) is not None:
                number = field.group(3)
                body[key] = float(number) if "." in number else int(number)
            else:
                body[key] = field.group(4).casefold() == "true"
    if not body and re.search(r"\btitle\b", text, re.IGNORECASE):
        body["title"] = "metis-runtime-check"
    return body


def api_probes_from_contract(
    criteria: Sequence[str | Mapping[str, Any]],
) -> tuple[ApiProbe, ...]:
    """Compile only API probes explicitly declared by locked criteria."""
    probes: list[ApiProbe] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(criteria, 1):
        if isinstance(raw, Mapping):
            text = str(raw.get("criterion") or raw.get("text") or "")
            spec = raw.get("evidence_spec")
            body = (
                dict(spec.get("body") or {})
                if isinstance(spec, Mapping)
                and isinstance(spec.get("body"), Mapping)
                else {}
            )
        else:
            text = str(raw or "")
            body = {}
        for match in _API_PROBE_CRITERION.finditer(text):
            method = match.group("method").upper()
            path = match.group("path")
            key = (method, path)
            if key in seen:
                continue
            seen.add(key)
            status_match = re.search(
                r"\b([1-5]\d{2})\b", match.group("trailer") or ""
            )
            statuses = (
                (int(status_match.group(1)),)
                if status_match
                else (200,)
            )
            request_body = body
            if (
                not request_body
                and method in {"POST", "PUT", "PATCH"}
                and any(200 <= status < 300 for status in statuses)
            ):
                request_body = _inline_api_request_body(text)
            probes.append(ApiProbe(
                probe_id=f"criterion-api-{index}-{len(probes) + 1}",
                method=method,
                path=path,
                expected_statuses=statuses,
                body=request_body,
                invariant=text.strip() or f"{method} {path}",
            ))
    return tuple(probes)


def default_api_probes() -> tuple[ApiProbe, ...]:
    """No project-independent business API exists."""
    return ()


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def scope_digest(workspace: Path | str, paths: Iterable[str]) -> str:
    root = Path(workspace).resolve()
    digest = hashlib.sha256()
    for raw_path in sorted(set(paths)):
        path = _normalize_path(raw_path)
        target = (root / path).resolve()
        digest.update(path.encode("utf-8"))
        if target.is_file():
            digest.update(target.read_bytes())
        else:
            digest.update(b"\0missing")
    return "sha256:" + digest.hexdigest()


def _command_text(command: Sequence[str]) -> str:
    return redact_text(" ".join(str(part) for part in command), max_length=2048)


_PACKAGE_MANAGER_EXECUTABLES = {
    "npm",
    "npm.cmd",
    "npx",
    "npx.cmd",
    "pnpm",
    "pnpm.cmd",
    "yarn",
    "yarn.cmd",
}


def _package_script_name(gate: CommandGate) -> str:
    if not gate.command:
        return ""
    command = tuple(str(part) for part in gate.command)
    executable = Path(command[0]).name.casefold()
    args = command[1:]
    if executable in {"npx", "npx.cmd"}:
        return ""
    if not args or args[0] in {
        "ci",
        "install",
        "add",
        "exec",
        "dlx",
    }:
        return ""
    if args[0] == "run":
        return args[1] if len(args) > 1 else ""
    if args[0] == "test":
        return "test"
    if executable in {"yarn", "yarn.cmd", "pnpm", "pnpm.cmd"}:
        return args[0]
    return ""


def _package_gate_precondition_error(workspace: Path, gate: CommandGate) -> str:
    if not _is_package_manager_gate(gate):
        return ""
    cwd = (workspace / gate.cwd).resolve()
    try:
        cwd.relative_to(workspace)
    except ValueError:
        return f"workspace directory escapes project root: {gate.cwd}"
    package_path = cwd / "package.json"
    if not cwd.is_dir():
        return f"workspace directory does not exist: {gate.cwd}"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return f"package.json does not exist: {gate.cwd}"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    script_name = _package_script_name(gate)
    if not script_name:
        return ""
    scripts = package.get("scripts") if isinstance(package, Mapping) else {}
    if not isinstance(scripts, Mapping) or not str(scripts.get(script_name) or "").strip():
        return f"package script does not exist: {script_name}"
    return ""


def _test_import_side_effect_issue(
    workspace: Path,
    gate: CommandGate,
) -> VerificationIssue | None:
    if not str(gate.gate_id).startswith("test-"):
        return None
    package_root = (workspace / gate.cwd).resolve()
    try:
        package = json.loads(
            (package_root / "package.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return None
    scripts = package.get("scripts") if isinstance(package, Mapping) else {}
    script = (
        str(scripts.get(_package_script_name(gate)) or "")
        if isinstance(scripts, Mapping)
        else ""
    )
    test_paths = {
        path.resolve()
        for token in re.findall(r"[A-Za-z0-9_./\\-]+\.js\b", script)
        if (path := package_root / token).is_file()
    }
    if not test_paths:
        test_paths = {
            path.resolve()
            for folder in ("test", "tests")
            if (package_root / folder).is_dir()
            for path in (package_root / folder).rglob("*.js")
        }
    for test_path in sorted(test_paths, key=str):
        try:
            test_source = test_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        imports = re.findall(
            r"""(?:require\s*\(\s*|from\s+)['"](\.\.?/[^'"]+)['"]""",
            test_source,
        )
        for imported in imports:
            raw_target = (test_path.parent / imported).resolve()
            candidates = (
                raw_target,
                raw_target.with_suffix(".js"),
                raw_target / "index.js",
            )
            target = next((item for item in candidates if item.is_file()), None)
            if target is None:
                continue
            try:
                source = target.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            starts_server = bool(re.search(r"\.\s*listen\s*\(", source))
            guarded = bool(re.search(
                r"require\s*\.\s*main\s*===\s*module",
                source,
            ))
            if starts_server and not guarded:
                relative = test_path.relative_to(workspace).as_posix()
                return VerificationIssue(
                    "test_import_starts_server",
                    (
                        "test imports an entrypoint that starts listening on "
                        "import; spawn the production entrypoint or guard "
                        "listen() with require.main === module"
                    ),
                    relative,
                    gate.gate_id,
                )
    return None


def _evidence(
    *, gate_id: str, kind: str, command: str, exit_code: int,
    passed: bool | None,
    scope: str, commit_digest: str, log: str, status_code: int | None = None,
    endpoint: str = "", assertions: Sequence[Mapping[str, Any]] = (),
    applicable: bool = True, executed: bool = True,
) -> PreQAEvidence:
    safe_log = redact_text(log, max_length=4096)
    return PreQAEvidence(
        kind=kind,
        command=redact_text(command, max_length=2048),
        exit_code=exit_code,
        passed=passed,
        scope_digest=scope,
        commit_digest=commit_digest,
        log_digest=_sha256(safe_log.encode("utf-8")),
        recorded_at=datetime.now(timezone.utc).isoformat(),
        gate_id=gate_id,
        log_excerpt=safe_log,
        status_code=status_code,
        endpoint=endpoint,
        assertions=tuple(dict(item) for item in assertions),
        applicable=applicable,
        executed=executed,
    )


def classify_failure(message: str, *, declared: str = "") -> str:
    safe = redact_text(message)
    # A wrapper such as npm can return 1 even when its child was OOM-killed.
    # Resource evidence in the command log is therefore authoritative over a
    # generic declared category or wrapper exit code.
    if _RESOURCE_FAILURE.search(safe):
        return FAILURE_INFRASTRUCTURE
    if declared in {FAILURE_PRE_QA, FAILURE_INFRASTRUCTURE, FAILURE_MODEL}:
        return declared
    if _MODEL_FAILURE.search(safe):
        return FAILURE_MODEL
    if _INFRA_FAILURE.search(safe):
        return FAILURE_INFRASTRUCTURE
    return FAILURE_PRE_QA


def _command_failure_issues(
    workspace: Path,
    gate: CommandGate,
    observed: CommandObservation,
    log: str,
) -> tuple[VerificationIssue, ...]:
    """Turn deterministic build preconditions into repairable, scoped issues."""
    if (
        observed.exit_code in {137, -9}
        or _RESOURCE_FAILURE.search(redact_text(log))
    ):
        return (
            VerificationIssue(
                "command_resource_exhausted",
                f"command was terminated by a resource limit (exit {observed.exit_code})",
                gate.cwd,
                gate.gate_id,
            ),
        )

    if gate.gate_id == "build-frontend":
        frontend = (workspace / gate.cwd).resolve()
        try:
            frontend.relative_to(workspace)
        except ValueError:
            frontend = workspace
        package_path = frontend / "package.json"
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            package = {}
        scripts = package.get("scripts") if isinstance(package, Mapping) else {}
        build_script = (
            str(scripts.get("build") or "")
            if isinstance(scripts, Mapping)
            else ""
        )
        precise: list[VerificationIssue] = []

        tsc_invoked = re.search(
            r"(?:^|[;&|])\s*(?:npx\s+)?tsc\b",
            build_script,
        )
        if tsc_invoked:
            project_match = re.search(
                r"(?:^|[;&|])\s*(?:npx\s+)?tsc\b[^;&|]*?"
                r"(?:-p|--project)\s+([^\s;&|]+)",
                build_script,
            )
            config_value = str(
                project_match.group(1) if project_match else "tsconfig.json"
            ).strip("'\"")
            config_path = PurePosixPath(config_value.replace("\\", "/"))
            if (
                config_value
                and not config_path.is_absolute()
                and ".." not in config_path.parts
            ):
                if config_path.suffix.lower() != ".json":
                    config_path /= "tsconfig.json"
                target = frontend.joinpath(*config_path.parts)
                if not target.is_file():
                    relative = PurePosixPath(gate.cwd).joinpath(config_path).as_posix()
                    precise.append(VerificationIssue(
                        "missing_typescript_config",
                        "frontend build invokes TypeScript but its project config is missing",
                        relative,
                        gate.gate_id,
                    ))

        vite_invoked = bool(re.search(
            r"(?:^|[;&|])\s*(?:npx\s+)?vite(?:\s+build)?\b",
            build_script,
        ))
        vite_configs = (
            "vite.config.ts",
            "vite.config.js",
            "vite.config.mjs",
            "vite.config.cjs",
        )
        if (
            vite_invoked
            and not (frontend / "index.html").is_file()
            and not any((frontend / name).is_file() for name in vite_configs)
        ):
            relative = PurePosixPath(gate.cwd).joinpath("index.html").as_posix()
            precise.append(VerificationIssue(
                "missing_vite_entry",
                "frontend build invokes Vite but no HTML entry or Vite config exists",
                relative,
                gate.gate_id,
            ))

        if precise:
            return tuple(precise)

    if gate.kind == "test":
        # Jest/Pytest stack traces normally identify the failing source even
        # when the command itself ran at the project root. Route repair to the
        # concrete owned file instead of reopening every phase Agent for ".".
        safe_test_log = redact_text(log)
        test_issues: list[VerificationIssue] = []
        seen_test_locations: set[tuple[str, str]] = set()
        for match in re.finditer(
            r"(?<![A-Za-z0-9_./\\-])"
            r"([A-Za-z0-9_./\\-]+(?:\.test|\.spec)\.[cm]?[jt]sx?"
            r"|[A-Za-z0-9_./\\-]+test_[A-Za-z0-9_.-]+\.py)"
            r":(\d+)(?::\d+)?",
            safe_test_log,
        ):
            raw_path = match.group(1).replace("\\", "/")
            candidates = [workspace / gate.cwd / raw_path, workspace / raw_path]
            for candidate in candidates:
                resolved = candidate.resolve()
                try:
                    relative = resolved.relative_to(workspace).as_posix()
                except ValueError:
                    continue
                if resolved.is_file():
                    location_key = (relative.casefold(), match.group(2))
                    if location_key in seen_test_locations:
                        break
                    seen_test_locations.add(location_key)
                    diagnostic_start = max(0, match.start() - 900)
                    diagnostic_end = min(len(safe_test_log), match.end() + 300)
                    diagnostic = " ".join(
                        safe_test_log[diagnostic_start:diagnostic_end].split()
                    )[-1200:]
                    test_issues.append(
                        VerificationIssue(
                            "command_gate_failed",
                            f"command exited with {observed.exit_code}; "
                            f"test failure at {relative}:{match.group(2)}; "
                            f"{diagnostic}",
                            relative,
                            gate.gate_id,
                        )
                    )
                    break
        if test_issues:
            return tuple(test_issues[:20])

    diagnostic = redact_text(log).strip().replace("\r", " ").replace("\n", " ")
    message = f"command exited with {observed.exit_code}"
    if diagnostic:
        message += f"; {diagnostic[-512:]}"
    issue_path = "package.json" if gate.gate_id == "install-root" else gate.cwd
    return (
        VerificationIssue(
            "command_gate_failed",
            message,
            issue_path,
            gate.gate_id,
        ),
    )


def node_fullstack_command_gates(
    *,
    image_tag: str = "metis-generated-acceptance",
    include_docker: bool = True,
) -> tuple[CommandGate, ...]:
    """The required gate order.  Runners execute argv without a shell."""
    base_gates = (
        CommandGate("install-root", "install", ("npm", "install"), required=False),
        CommandGate(
            "install-backend", "install", ("npm", "install"), "backend",
            required=False,
        ),
        CommandGate(
            "install-frontend", "install", ("npm", "install"), "frontend",
            required=False,
        ),
        CommandGate(
            "test-backend", "test",
            ("npm", "test", "--", "--runInBand"), "backend",
            timeout_seconds=180,
            required=False,
        ),
        CommandGate(
            "build-frontend", "build", ("npm", "run", "build"), "frontend",
            required=False,
        ),
        CommandGate(
            "test-root",
            "test",
            ("npm", "test"),
            timeout_seconds=180,
            required=False,
        ),
        CommandGate(
            "build-root", "build", ("npm", "run", "build"), required=False,
        ),
    )
    if not include_docker:
        return base_gates
    return base_gates + (
        CommandGate(
            "docker-daemon",
            "infrastructure",
            ("docker", "version", "--format", "{{.Server.Version}}"),
            timeout_seconds=30,
        ),
        CommandGate("docker-build", "docker_build", ("docker", "build", "-t", image_tag, "."), timeout_seconds=1200),
    )


_BATCHABLE_COMMAND_KINDS = {"install", "test", "build"}
_NPM_WORKSPACE_LOCKS: dict[str, threading.Lock] = {}
_NPM_WORKSPACE_LOCKS_GUARD = threading.Lock()
_NPM_PROCESS_RESOURCE_LOCK = threading.Lock()
_NPM_INSTALL_RECOVERABLE = re.compile(r"\b(?:ENOTEMPTY|EPERM)\b", re.IGNORECASE)
_NPM_TRANSIENT_DOWNLOAD_FAILURE = re.compile(
    r"(?:request\s+timed?\s*out|network\s+timeout|"
    r"\b(?:ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN)\b|"
    r"socket\s+hang\s+up|temporary\s+failure)",
    re.IGNORECASE,
)
# 128 MB is below npm's observed dependency-tree parsing floor on the Render
# runtime (npm aborted at ~128.2 MB).  Keep Node bounded well below the service
# limit, but leave enough headroom for npm/tsc/vite to complete.
_NPM_NODE_OPTIONS = "--max-old-space-size=192 --max-semi-space-size=4"
_NPM_INSTALL_NODE_OPTIONS = "--max-old-space-size=192 --max-semi-space-size=4"
_COMMAND_OUTPUT_LIMIT_BYTES = 256 * 1024
_SUBPROCESS_ENV_ALLOWLIST = (
    "PATH",
    "TEMP",
    "TMP",
    "SystemRoot",
    "ComSpec",
    "PATHEXT",
    "LANG",
    "LC_ALL",
)


def _isolated_command_environment() -> dict[str, str]:
    """Build generated-code command environments from an explicit allowlist."""
    return {
        name: value
        for name in _SUBPROCESS_ENV_ALLOWLIST
        if (value := os.environ.get(name)) is not None
    }


def _release_parent_memory_before_npm() -> None:
    """Return unused parent-process memory before spawning a bounded npm task."""
    gc.collect()
    if os.name != "posix":
        return
    try:
        import ctypes

        malloc_trim = getattr(ctypes.CDLL(None), "malloc_trim")
        malloc_trim.argtypes = [ctypes.c_size_t]
        malloc_trim.restype = ctypes.c_int
        malloc_trim(0)
    except (AttributeError, OSError, TypeError, ValueError):
        # malloc_trim is a glibc extension and may not exist on other POSIX C runtimes.
        pass


def _npm_workspace_lock(workspace: Path) -> threading.Lock:
    key = os.path.normcase(str(workspace.resolve()))
    with _NPM_WORKSPACE_LOCKS_GUARD:
        return _NPM_WORKSPACE_LOCKS.setdefault(key, threading.Lock())


def _is_package_manager_gate(gate: CommandGate) -> bool:
    return bool(
        gate.command
        and Path(str(gate.command[0])).name.casefold()
        in _PACKAGE_MANAGER_EXECUTABLES
    )


def _is_npm_gate(gate: CommandGate) -> bool:
    return bool(
        gate.command
        and Path(str(gate.command[0])).name.casefold()
        in {"npm", "npm.cmd", "npx", "npx.cmd"}
    )


_STRUCTURED_COMMAND_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"(?P<command>"
    r"(?:npm|pnpm|yarn)\s+run\s+[A-Za-z0-9_.:@/-]+"
    r"|npm\s+(?:ci|install|test)"
    r"|pnpm\s+(?:install|test|exec\s+(?:cypress|playwright)\s+"
    r"(?:run|test))"
    r"|yarn\s+(?:install|test|(?:cypress|playwright)\s+(?:run|test))"
    r"|npx\s+(?:cypress|playwright)\s+(?:run|test)"
    r")",
    re.IGNORECASE,
)


def structured_command_gates(
    workspace: Path | str,
    criteria: Sequence[str | Mapping[str, Any]],
) -> tuple[CommandGate, ...]:
    """Compile exact package-manager commands from locked criteria.

    Only explicit argv is accepted.  No shell syntax, chained command, or
    prose-only ``test/build`` word is converted into executable evidence.
    """
    root = Path(workspace).resolve()
    gates: list[CommandGate] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for index, raw in enumerate(criteria, 1):
        if isinstance(raw, Mapping):
            text = str(raw.get("criterion") or raw.get("text") or "")
            spec = raw.get("evidence_spec")
            cwd = str(spec.get("cwd") or ".") if isinstance(spec, Mapping) else "."
            explicit_argv = (
                spec.get("argv")
                if isinstance(spec, Mapping)
                else None
            )
            if isinstance(explicit_argv, Sequence) and not isinstance(
                explicit_argv, (str, bytes)
            ):
                matches = [tuple(str(part) for part in explicit_argv if str(part))]
            else:
                matches = [
                    tuple(match.group("command").split())
                    for match in _STRUCTURED_COMMAND_PATTERN.finditer(text)
                ]
        else:
            text = str(raw or "")
            cwd = "."
            matches = [
                tuple(match.group("command").split())
                for match in _STRUCTURED_COMMAND_PATTERN.finditer(text)
            ]
        for argv in matches:
            if not argv or any(part in {"&&", "||", ";", "|"} for part in argv):
                continue
            target = (root / cwd).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            key = (cwd, tuple(part.casefold() for part in argv))
            if key in seen:
                continue
            seen.add(key)
            lowered = " ".join(argv).casefold()
            if re.match(
                r"^(?:npm|pnpm|yarn) (?:ci|install)\b", lowered
            ):
                kind = "install"
            elif re.search(r"(?:^|[\s:])build(?:$|\s)", lowered):
                kind = "build"
            else:
                kind = "test"
            slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
            gates.append(CommandGate(
                gate_id=f"criterion-{index}-{slug}",
                kind=kind,
                command=argv,
                cwd=cwd,
                timeout_seconds=180 if kind == "test" else 900,
                required=True,
            ))
    return tuple(gates)


class _BoundedCommandOutput:
    """In-memory diagnostic tail whose size stays bounded while a gate runs."""

    def __init__(self, limit: int) -> None:
        self.limit = max(0, int(limit))
        self.total_bytes = 0
        self._tail = bytearray()
        self._lock = threading.Lock()

    @property
    def buffered_bytes(self) -> int:
        with self._lock:
            return len(self._tail)

    def append(self, payload: bytes) -> None:
        if not payload:
            return
        with self._lock:
            self.total_bytes += len(payload)
            if not self.limit:
                return
            if len(payload) >= self.limit:
                self._tail[:] = payload[-self.limit:]
                return
            overflow = len(self._tail) + len(payload) - self.limit
            if overflow > 0:
                del self._tail[:overflow]
            self._tail.extend(payload)

    def text(self) -> str:
        with self._lock:
            payload = bytes(self._tail)
            total_bytes = self.total_bytes
        if total_bytes > len(payload):
            payload = (
                f"[command output truncated; kept last {self.limit} bytes]\n"
                .encode("utf-8")
                + payload
            )
        return payload.decode("utf-8", errors="replace")


def _drain_command_stream(stream: Any, output: _BoundedCommandOutput) -> None:
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            output.append(chunk)
    except (OSError, ValueError):
        return


def _terminate_command_process_tree(process: Any) -> None:
    """Terminate the isolated command group, including npm/node descendants."""
    if os.name == "nt":
        try:
            killer = subprocess.Popen(
                (
                    "taskkill.exe",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            killer.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                pass
        return

    # start_new_session=True makes the child PID the process-group ID.  Always
    # follow TERM with KILL: npm may exit while a node/jest descendant ignores
    # TERM and keeps the inherited output descriptors open.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass


def _finish_command_streams(
    process: Any,
    streams: Sequence[Any],
    readers: Sequence[threading.Thread],
) -> None:
    for reader in readers:
        reader.join(timeout=1)
    if any(reader.is_alive() for reader in readers):
        # A direct parent can exit while a background descendant still owns a
        # pipe.  Treat that descendant as part of the gate and clean the group.
        _terminate_command_process_tree(process)
    for stream in streams:
        try:
            stream.close()
        except (OSError, ValueError):
            pass
    for reader in readers:
        reader.join(timeout=1)


def _run_bounded_command(
    command: tuple[str, ...],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> Any:
    """Run one isolated process group with bounded, continuously drained logs."""
    resolved_command = command
    if command:
        executable = str(command[0])
        if not Path(executable).is_absolute() and not any(
            separator in executable for separator in ("/", "\\")
        ):
            resolved = shutil.which(executable, path=env.get("PATH"))
            if resolved:
                resolved_command = (resolved, *command[1:])
    popen_options: dict[str, Any] = {}
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True
    process = subprocess.Popen(
        resolved_command,
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        **popen_options,
    )
    stdout_output = _BoundedCommandOutput(_COMMAND_OUTPUT_LIMIT_BYTES)
    stderr_output = _BoundedCommandOutput(_COMMAND_OUTPUT_LIMIT_BYTES)
    streams = (process.stdout, process.stderr)
    readers = (
        threading.Thread(
            target=_drain_command_stream,
            args=(process.stdout, stdout_output),
            name=f"preqa-stdout-{process.pid}",
            daemon=True,
        ),
        threading.Thread(
            target=_drain_command_stream,
            args=(process.stderr, stderr_output),
            name=f"preqa-stderr-{process.pid}",
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_command_process_tree(process)
        _finish_command_streams(process, streams, readers)
        exc.stdout = stdout_output.text()
        exc.stderr = stderr_output.text()
        raise
    _finish_command_streams(process, streams, readers)
    return subprocess.CompletedProcess(
        resolved_command,
        returncode,
        stdout_output.text(),
        stderr_output.text(),
    )


def _root_test_delegates_only_to_backend(workspace: Path) -> bool:
    """Return true when root ``npm test`` is only a backend-test alias."""
    try:
        payload = json.loads((workspace / "package.json").read_text(encoding="utf-8"))
        script = str((payload.get("scripts") or {}).get("test") or "")
    except (OSError, UnicodeError, ValueError, TypeError):
        return False
    normalized = " ".join(script.strip().casefold().split())
    return bool(re.fullmatch(
        r"npm(?:\.cmd)?\s+--prefix(?:=|\s+)backend\s+(?:run\s+)?test"
        r"(?:\s+--(?:\s+)?[\w.-]+)*",
        normalized,
    ))


class LocalCommandRunner:
    """Run the deterministic Node/Docker gates without invoking a shell."""

    def __init__(self, *, base_url: str = "http://127.0.0.1:3000", image_tag: str = "metis-generated-acceptance") -> None:
        self.base_url = base_url.rstrip("/")
        self.image_tag = image_tag
        self.container_name = f"{image_tag}-runtime"

    def _cleanup(self) -> str:
        """Best-effort cleanup which must never mask the gate result."""
        try:
            completed = subprocess.run(
                ("docker", "rm", "-f", self.container_name),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return redact_text(str(exc))
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        if completed.returncode and "No such container" not in output:
            return redact_text(output or f"docker cleanup exited with {completed.returncode}")
        return ""

    @staticmethod
    def _json(response: Any) -> bool:
        try:
            response.json()
            return True
        except Exception:
            return False

    def __call__(self, gate: CommandGate, workspace: Path) -> CommandObservation:
        if gate.gate_id == "docker-run":
            cleanup_error = self._cleanup()
            if cleanup_error:
                return CommandObservation(
                    -1,
                    stderr=f"unable to prepare Docker runtime: {cleanup_error}",
                    failure_category=FAILURE_INFRASTRUCTURE,
                )
        env = _isolated_command_environment()
        if gate.gate_id == "docker-run":
            env["JWT_SECRET"] = secrets.token_urlsafe(48)
        cwd = (workspace / gate.cwd).resolve()
        command = tuple(gate.command)
        if _is_package_manager_gate(gate):
            env.update({
                "CI": "true",
                "NODE_ENV": "test",
                "NODE_OPTIONS": _NPM_NODE_OPTIONS,
            })
        if _is_npm_gate(gate):
            env.update({
                "NODE_OPTIONS": (
                    _NPM_INSTALL_NODE_OPTIONS
                    if gate.kind == "install"
                    else _NPM_NODE_OPTIONS
                ),
                "UV_THREADPOOL_SIZE": "1",
                "JWT_SECRET": secrets.token_urlsafe(48),
                "npm_config_audit": "false",
                "npm_config_fund": "false",
                "npm_config_jobs": "1",
                "npm_config_maxsockets": "1",
                "npm_config_progress": "false",
            })
            npm_runtime_root = (
                Path(tempfile.gettempdir()) / "metis-preqa-npm"
            ).resolve()
            # npm's cache is content-addressed and verifies package integrity,
            # so it is safe and useful to share downloaded artifacts between
            # isolated project workspaces. User config and HOME remain scoped.
            npm_cache = npm_runtime_root / "cache"
            npm_cache.mkdir(parents=True, exist_ok=True)
            workspace_runtime = (
                npm_runtime_root
                / "workspaces"
                / hashlib.sha256(
                    str(workspace.resolve()).encode("utf-8")
                ).hexdigest()[:16]
            )
            npm_home = workspace_runtime / "home"
            npm_home.mkdir(parents=True, exist_ok=True)
            npm_userconfig = workspace_runtime / "npmrc"
            npm_userconfig.write_text(f"cache={npm_cache}\n", encoding="utf-8")
            env["HOME"] = str(npm_home)
            env["NPM_CONFIG_CACHE"] = str(npm_cache)
            env["npm_config_cache"] = str(npm_cache)
            env["npm_config_update_notifier"] = "false"
            command = (
                command[0],
                "--cache",
                str(npm_cache),
                "--userconfig",
                str(npm_userconfig),
                *command[1:],
            )
            if gate.kind == "install":
                if (cwd / "package-lock.json").is_file():
                    command = tuple(
                        "ci" if part == "install" else part
                        for part in command
                    )
                else:
                    # Verification must not create a delivery artifact and
                    # invalidate the scope it locked before running.
                    command = (*command, "--package-lock=false")
                command = (*command, "--legacy-peer-deps")
        try:
            def run_once() -> Any:
                def execute() -> Any:
                    return _run_bounded_command(
                        command,
                        cwd=cwd,
                        env=env,
                        timeout=gate.timeout_seconds,
                    )

                if _is_npm_gate(gate):
                    with _NPM_PROCESS_RESOURCE_LOCK:
                        _release_parent_memory_before_npm()
                        return execute()
                return execute()

            completed = run_once()
            combined = "\n".join(
                part for part in (completed.stdout, completed.stderr) if part
            )
            if (
                completed.returncode
                and gate.kind == "install"
                and _is_npm_gate(gate)
                and _NPM_TRANSIENT_DOWNLOAD_FAILURE.search(combined)
            ):
                retried = run_once()
                retry_note = (
                    "npm recovery: transient download failure; retried "
                    "install once using the shared content cache"
                )
                stdout = "\n".join(
                    part for part in (retry_note, retried.stdout) if part
                )
                retry_log = "\n".join(
                    part for part in (retried.stdout, retried.stderr) if part
                )
                return CommandObservation(
                    retried.returncode,
                    stdout,
                    retried.stderr,
                    (
                        FAILURE_INFRASTRUCTURE
                        if retried.returncode
                        and _NPM_TRANSIENT_DOWNLOAD_FAILURE.search(retry_log)
                        else ""
                    ),
                )
            if (
                completed.returncode
                and gate.kind == "install"
                and _is_npm_gate(gate)
                and _NPM_INSTALL_RECOVERABLE.search(combined)
            ):
                root = workspace.resolve()
                node_modules = (cwd / "node_modules").resolve()
                try:
                    cwd.relative_to(root)
                    node_modules.relative_to(root)
                except ValueError:
                    pass
                else:
                    if node_modules != root and node_modules.parent == cwd and node_modules.is_dir():
                        shutil.rmtree(node_modules)
                        retried = run_once()
                        retry_note = (
                            "npm recovery: removed workspace-scoped node_modules "
                            "and retried install once"
                        )
                        stdout = "\n".join(
                            part for part in (retry_note, retried.stdout) if part
                        )
                        return CommandObservation(
                            retried.returncode,
                            stdout,
                            retried.stderr,
                        )
            return CommandObservation(completed.returncode, completed.stdout, completed.stderr)
        except subprocess.TimeoutExpired as exc:
            self._cleanup()
            return CommandObservation(-1, str(exc.stdout or ""), str(exc.stderr or exc), FAILURE_INFRASTRUCTURE)
        except OSError as exc:
            self._cleanup()
            return CommandObservation(-1, stderr=str(exc), failure_category=FAILURE_INFRASTRUCTURE)


def run_api_probes(
    probes: Sequence[ApiProbe],
    runner: ApiProbeRunner,
    *,
    scope: str,
    commit_digest: str,
) -> tuple[tuple[PreQAEvidence, ...], tuple[VerificationIssue, ...], str]:
    records: list[PreQAEvidence] = []
    issues: list[VerificationIssue] = []
    category = ""
    for probe in probes:
        try:
            observed = runner(probe)
        except Exception as exc:  # injected network runner boundary
            message = redact_text(exc)
            category = classify_failure(message, declared=FAILURE_INFRASTRUCTURE)
            records.append(_evidence(gate_id=probe.probe_id, kind="api", command=f"{probe.method} {probe.path}", exit_code=-1, passed=False, scope=scope, commit_digest=commit_digest, log=message))
            issues.append(VerificationIssue("api_probe_runner_failed", message, probe.path, "api"))
            break
        forbidden_keys = {key.casefold() for key in probe.forbidden_response_keys}

        def contains_forbidden_key(value: Any) -> bool:
            if isinstance(value, Mapping):
                return any(
                    str(key).casefold() in forbidden_keys or contains_forbidden_key(item)
                    for key, item in value.items()
                )
            if isinstance(value, (list, tuple)):
                return any(contains_forbidden_key(item) for item in value)
            return False

        passed = (
            observed.status_code in probe.expected_statuses
            and (observed.is_json or not probe.require_json)
            and not contains_forbidden_key(observed.body)
        )
        log = observed.log or json.dumps({"status_code": observed.status_code, "is_json": observed.is_json}, sort_keys=True)
        records.append(_evidence(
            gate_id=probe.probe_id,
            kind="api",
            command=f"{probe.method} {probe.path}",
            exit_code=0 if passed else 1,
            passed=passed,
            scope=scope,
            commit_digest=commit_digest,
            log=log,
            status_code=observed.status_code,
            endpoint=probe.path,
            assertions=[{
                "name": probe.invariant or f"{probe.method} {probe.path}",
                "passed": passed,
            }],
        ))
        if not passed:
            category = classify_failure(log, declared=observed.failure_category)
            issues.append(VerificationIssue("api_invariant_failed", probe.invariant or "API probe failed", probe.path, "api"))
            break
    return tuple(records), tuple(issues), category


class PreQAVerifier:
    def __init__(
        self,
        workspace: Path | str,
        *,
        command_runner: CommandRunner,
        api_probe_runner: ApiProbeRunner | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.command_runner = command_runner
        self.api_probe_runner = api_probe_runner

    def verify(
        self,
        *,
        contract: Mapping[str, Any],
        file_registry: Mapping[str, Mapping[str, Any]],
        agents: Iterable[Mapping[str, Any]],
        commit_digest: str,
        command_gates: Sequence[CommandGate],
        api_probes: Sequence[ApiProbe] = (),
        jwt_required: bool = True,
    ) -> PreQAResult:
        rows = _required_file_rows(contract)
        paths = tuple(row["path"] for row in rows)
        scope = scope_digest(self.workspace, paths)
        issues = list(verify_contract_artifacts(self.workspace, contract, file_registry, agents))
        jwt_paths = tuple(dict.fromkeys((*paths, *file_registry.keys())))
        issues.extend(
            verify_jwt_invariants(
                self.workspace, jwt_paths, jwt_required=jwt_required
            )
        )
        if issues:
            return PreQAResult(False, "pre_qa_failed", FAILURE_PRE_QA, issues[0].gate, tuple(issues), ())

        records: list[PreQAEvidence] = []
        command_issues: list[VerificationIssue] = []
        command_category = ""
        backend_test_record: PreQAEvidence | None = None
        root_test_is_backend_alias = _root_test_delegates_only_to_backend(
            self.workspace
        )
        for gate in command_gates:
            precondition_error = _package_gate_precondition_error(
                self.workspace, gate,
            )
            if precondition_error:
                record = _evidence(
                    gate_id=gate.gate_id,
                    kind=gate.kind,
                    command=_command_text(gate.command),
                    exit_code=-1,
                    passed=False if gate.required else None,
                    scope=scope,
                    commit_digest=commit_digest,
                    log=precondition_error,
                    applicable=gate.required,
                    executed=False,
                )
                records.append(record)
                if gate.required:
                    issue = VerificationIssue(
                        "command_precondition_missing",
                        precondition_error,
                        gate.cwd,
                        gate.gate_id,
                    )
                    return PreQAResult(
                        False,
                        FAILURE_PRE_QA,
                        FAILURE_PRE_QA,
                        gate.gate_id,
                        (issue,),
                        tuple(records),
                    )
                continue
            side_effect_issue = _test_import_side_effect_issue(
                self.workspace,
                gate,
            )
            if side_effect_issue is not None:
                record = _evidence(
                    gate_id=gate.gate_id,
                    kind=gate.kind,
                    command=_command_text(gate.command),
                    exit_code=-1,
                    passed=False,
                    scope=scope,
                    commit_digest=commit_digest,
                    log=side_effect_issue.message,
                    executed=False,
                )
                return PreQAResult(
                    False,
                    FAILURE_PRE_QA,
                    FAILURE_PRE_QA,
                    gate.gate_id,
                    (side_effect_issue,),
                    tuple(records + [record]),
                )
            if (
                gate.gate_id == "test-root"
                and root_test_is_backend_alias
                and backend_test_record is not None
            ):
                # Running the root alias would execute the same backend suite
                # against the same SQLite test database a second time. Reuse
                # the already recorded deterministic result instead.
                records.append(_evidence(
                    gate_id=gate.gate_id,
                    kind=gate.kind,
                    command=_command_text(gate.command),
                    exit_code=backend_test_record.exit_code,
                    passed=backend_test_record.passed,
                    scope=scope,
                    commit_digest=commit_digest,
                    log=(
                        "root test delegates only to backend test; reused "
                        f"{backend_test_record.gate_id} evidence "
                        f"{backend_test_record.log_digest}"
                    ),
                ))
                continue
            try:
                if _is_package_manager_gate(gate):
                    with _npm_workspace_lock(self.workspace):
                        observed = self.command_runner(gate, self.workspace)
                else:
                    observed = self.command_runner(gate, self.workspace)
            except Exception as exc:  # runner/process boundary
                message = redact_text(exc)
                record = _evidence(gate_id=gate.gate_id, kind=gate.kind, command=_command_text(gate.command), exit_code=-1, passed=False, scope=scope, commit_digest=commit_digest, log=message)
                return PreQAResult(False, "infrastructure_failed", FAILURE_INFRASTRUCTURE, gate.gate_id, (VerificationIssue("command_runner_failed", message, gate.cwd, gate.gate_id),), tuple(records + [record]))
            passed = observed.exit_code == 0
            log = "\n".join(part for part in (observed.stdout, observed.stderr) if part)
            record = _evidence(gate_id=gate.gate_id, kind=gate.kind, command=_command_text(gate.command), exit_code=observed.exit_code, passed=passed, scope=scope, commit_digest=commit_digest, log=log)
            records.append(record)
            if gate.gate_id == "test-backend":
                backend_test_record = record
            if not passed:
                category = (
                    FAILURE_INFRASTRUCTURE
                    if observed.exit_code in {137, -9}
                    else classify_failure(log, declared=observed.failure_category)
                )
                issues = _command_failure_issues(
                    self.workspace, gate, observed, log,
                )
                command_issues.extend(issues)
                command_category = (
                    category
                    if category in {FAILURE_INFRASTRUCTURE, FAILURE_MODEL}
                    else command_category or category
                )
                if category in {FAILURE_INFRASTRUCTURE, FAILURE_MODEL} or gate.kind not in _BATCHABLE_COMMAND_KINDS:
                    return PreQAResult(
                        False,
                        category,
                        category,
                        gate.gate_id,
                        issues,
                        tuple(records),
                    )
                continue

        if command_issues:
            failure = command_category or FAILURE_PRE_QA
            return PreQAResult(
                False,
                failure,
                failure,
                command_issues[0].gate,
                tuple(command_issues),
                tuple(records),
            )

        if api_probes:
            if self.api_probe_runner is None:
                issue = VerificationIssue("api_probe_runner_missing", "API probes require an injected runner", gate="api")
                return PreQAResult(False, FAILURE_INFRASTRUCTURE, FAILURE_INFRASTRUCTURE, "api", (issue,), tuple(records))
            api_records, api_issues, category = run_api_probes(api_probes, self.api_probe_runner, scope=scope, commit_digest=commit_digest)
            records.extend(api_records)
            if api_issues:
                failure = category or FAILURE_PRE_QA
                return PreQAResult(False, failure, failure, api_issues[0].gate, api_issues, tuple(records))
        return PreQAResult(True, "passed", "", "", (), tuple(records))
