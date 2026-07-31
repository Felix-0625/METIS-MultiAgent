"""Isolated runtime acceptance by deploying a workspace snapshot to Render.

The provider is deliberately fail-closed when enabled.  It never shells out to
Git, never modifies the supplied workspace, and never logs credentials.  A
fresh Git tree is built through GitHub's Git Data API so files left by an older
acceptance run cannot leak into the next deployment.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlparse

import requests

from core.workspace_integrity import collect_delivery_artifact


_TERMINAL_DEPLOY_FAILURES = {
    "build_failed",
    "update_failed",
    "canceled",
    "cancelled",
    "deactivated",
    "failed",
}
# A passed artifact is not valid forever: Render may be redeployed or rolled
# back while the workspace bytes stay unchanged.  Keep reuse bounded and
# invalidate older acceptance records when runtime rules change.
_RULE_VERSION = "2026-07-26.1"
_CACHE_MAX_AGE_SECONDS = 15 * 60


class RuntimeAcceptanceError(RuntimeError):
    """A safe, credential-free runtime acceptance failure."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str = "project_defect",
        retryable: bool = False,
        file_path: str = "",
        fix_hint: str = "",
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.file_path = file_path
        self.fix_hint = fix_hint


def _http_failure_location(response_excerpt: str) -> tuple[str, int | None]:
    """Extract a safe workspace-relative source location from a runtime stack."""
    normalized = str(response_excerpt or "").replace("\\", "/")
    matches = re.findall(
        r"(?P<path>(?:[A-Za-z]:)?/[^<>\r\n:]+?\.(?:js|cjs|mjs|ts|py))"
        r":(?P<line>\d+)(?::\d+)?",
        normalized,
        flags=re.IGNORECASE,
    )
    for raw_path, raw_line in matches:
        path = raw_path.lstrip("/")
        lowered = path.casefold()
        if "/node_modules/" in f"/{lowered}":
            continue
        for marker in ("routes/", "src/", "server/", "backend/", "frontend/"):
            offset = lowered.rfind("/" + marker)
            if offset >= 0:
                path = path[offset + 1:]
                break
        else:
            path = Path(path).name
        return path, int(raw_line)
    return "", None


def _http_failure_fix_hint(response_excerpt: str, default: str) -> str:
    text = str(response_excerpt or "").casefold()
    if (
        "cannot destructure property" in text
        and "req.body" in text
    ) or "cannot read properties of undefined" in text and "body" in text:
        return (
            "Install JSON body parsing before this route handler "
            "(for Express, use express.json() before reading req.body), "
            "then rerun the exact failing request."
        )
    return default


@dataclass(frozen=True)
class _Config:
    github_token: str
    github_repository: str
    github_branch: str
    github_base_branch: str
    github_api_url: str
    render_api_key: str
    render_service_id: str
    render_service_url: str
    render_api_url: str
    max_files: int
    max_total_bytes: int
    request_timeout: float
    deploy_timeout: float
    health_timeout: float
    poll_interval: float


_FAILURE_MARKERS = (
    "error", "fail", "assert", "expect", "received", "✕", "✗",
    "traceback", "exception", "断言", "错误", "失败", "异常",
)


def _extract_failure_diagnostic(stdout: str, stderr: str) -> str:
    """Locate the real failure reason in command output.

    Prefer the first line containing a failure marker (Error/assert/fail…)
    over a blind tail truncation, so a long stdout of passing logs or stderr
    deprecation noise cannot mask the actual assertion failure.
    """
    combined = (stdout + "\n" + stderr)
    for line in combined.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()
        if not any(marker in low for marker in _FAILURE_MARKERS):
            continue
        # 排除“0 errors / 0 failing / no failures”这类零计数摘要噪声行，
        # 否则会抓到“Tests: 0 errors”误导 repair。但含 assert/expected/traceback
        # 的行是真失败，不跳过。
        is_assertion = any(w in low for w in ("assert", "expected", "traceback", "exception", "received"))
        is_zero_count = bool(re.search(r"\b0\s*(error|fail|failing|failure)s?\b", low)) or "no failure" in low
        if is_zero_count and not is_assertion:
            continue
        return stripped.replace("\r", " ")
    # 没有明确失败行：退回 stdout（Jest 断言在 stdout），再退 stderr
    return (stdout or stderr or "").strip().replace("\r", " ").replace("\n", " ")


def _result(*, enabled: bool, passed: bool, status: str, summary: str) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "passed": passed,
        "status": status,
        "summary": summary,
        "commit_sha": "",
        "deploy_id": "",
        "service_url": "",
        "logs": [],
        "artifact_sha256": "",
        "acceptance_key": "",
        "rule_version": _RULE_VERSION,
        "error_category": "",
        "retryable": False,
        "actionable": False,
        "source": "deterministic_runtime",
        "cached": False,
        "current_step": "",
        "started_at": time.time(),
        "updated_at": time.time(),
    }


def _acceptance_key(project_id: str, artifact_sha256: str) -> str:
    raw = f"{project_id}\0{artifact_sha256}\0{_RULE_VERSION}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _enabled() -> bool:
    return os.getenv("RUNTIME_ACCEPTANCE_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _positive_number(name: str, default: str, cast: type[int] | type[float]) -> int | float:
    raw = os.getenv(name, default).strip()
    try:
        value = cast(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeAcceptanceError(
            f"invalid configuration: {name}",
            error_type="environment_misconfigured",
        ) from exc
    if value <= 0:
        raise RuntimeAcceptanceError(
            f"invalid configuration: {name}",
            error_type="environment_misconfigured",
        )
    return value


def _nonnegative_number(name: str, default: str) -> float:
    raw = os.getenv(name, default).strip()
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeAcceptanceError(
            f"invalid configuration: {name}",
            error_type="environment_misconfigured",
        ) from exc
    if value < 0:
        raise RuntimeAcceptanceError(
            f"invalid configuration: {name}",
            error_type="environment_misconfigured",
        )
    return value


def _valid_branch(branch: str) -> bool:
    return bool(
        branch
        and not branch.startswith("/")
        and not branch.endswith(("/", ".", ".lock"))
        and "//" not in branch
        and ".." not in branch
        and not re.search(r"[\x00-\x20~^:?*\\\[]", branch)
    )


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.username


def _load_config() -> _Config:
    names = {
        "github_token": "RUNTIME_ACCEPTANCE_GITHUB_TOKEN",
        "github_repository": "RUNTIME_ACCEPTANCE_GITHUB_REPOSITORY",
        "render_api_key": "RUNTIME_ACCEPTANCE_RENDER_API_KEY",
        "render_service_id": "RUNTIME_ACCEPTANCE_RENDER_SERVICE_ID",
    }
    values = {key: os.getenv(env_name, "").strip() for key, env_name in names.items()}
    missing = [env_name for key, env_name in names.items() if not values[key]]
    if missing:
        raise RuntimeAcceptanceError(
            "missing configuration: " + ", ".join(sorted(missing)),
            error_type="environment_misconfigured",
        )

    repository = values["github_repository"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise RuntimeAcceptanceError(
            "invalid configuration: RUNTIME_ACCEPTANCE_GITHUB_REPOSITORY",
            error_type="environment_misconfigured",
        )
    branch = os.getenv(
        "RUNTIME_ACCEPTANCE_GITHUB_BRANCH", "metis-runtime-acceptance"
    ).strip()
    base_branch = os.getenv("RUNTIME_ACCEPTANCE_GITHUB_BASE_BRANCH", "develop").strip()
    if not _valid_branch(branch):
        raise RuntimeAcceptanceError(
            "invalid configuration: RUNTIME_ACCEPTANCE_GITHUB_BRANCH",
            error_type="environment_misconfigured",
        )
    if not _valid_branch(base_branch):
        raise RuntimeAcceptanceError(
            "invalid configuration: RUNTIME_ACCEPTANCE_GITHUB_BASE_BRANCH",
            error_type="environment_misconfigured",
        )

    github_api_url = os.getenv(
        "RUNTIME_ACCEPTANCE_GITHUB_API_URL", "https://api.github.com"
    ).strip().rstrip("/")
    render_api_url = os.getenv(
        "RUNTIME_ACCEPTANCE_RENDER_API_URL", "https://api.render.com/v1"
    ).strip().rstrip("/")
    service_url = os.getenv("RUNTIME_ACCEPTANCE_RENDER_SERVICE_URL", "").strip().rstrip("/")
    if not _valid_http_url(github_api_url):
        raise RuntimeAcceptanceError(
            "invalid configuration: RUNTIME_ACCEPTANCE_GITHUB_API_URL",
            error_type="environment_misconfigured",
        )
    if not _valid_http_url(render_api_url):
        raise RuntimeAcceptanceError(
            "invalid configuration: RUNTIME_ACCEPTANCE_RENDER_API_URL",
            error_type="environment_misconfigured",
        )
    if service_url and not _valid_http_url(service_url):
        raise RuntimeAcceptanceError(
            "invalid configuration: RUNTIME_ACCEPTANCE_RENDER_SERVICE_URL",
            error_type="environment_misconfigured",
        )

    return _Config(
        github_token=values["github_token"],
        github_repository=repository,
        github_branch=branch,
        github_base_branch=base_branch,
        github_api_url=github_api_url,
        render_api_key=values["render_api_key"],
        render_service_id=values["render_service_id"],
        render_service_url=service_url,
        render_api_url=render_api_url,
        max_files=int(_positive_number("RUNTIME_ACCEPTANCE_MAX_FILES", "800", int)),
        max_total_bytes=int(
            _positive_number("RUNTIME_ACCEPTANCE_MAX_TOTAL_BYTES", "15728640", int)
        ),
        request_timeout=float(
            _positive_number("RUNTIME_ACCEPTANCE_REQUEST_TIMEOUT", "30", float)
        ),
        deploy_timeout=float(
            _positive_number("RUNTIME_ACCEPTANCE_DEPLOY_TIMEOUT", "900", float)
        ),
        health_timeout=_nonnegative_number("RUNTIME_ACCEPTANCE_HEALTH_TIMEOUT", "120"),
        poll_interval=_nonnegative_number("RUNTIME_ACCEPTANCE_POLL_INTERVAL", "5"),
    )


_HTTP_CRITERION = re.compile(
    r"\b(?P<method>GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS)\s+"
    r"(?P<path>/[A-Za-z0-9_./{}:@?=&%-]*)"
    r"(?P<trailer>[\s\S]*?)"
    r"(?=\b(?:GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS)\s+/|\Z)",
    re.IGNORECASE,
)
_HTTP_STATUS = re.compile(r"\b([1-5]\d{2})\b")
_PORT_CUE = re.compile(
    r"(?:localhost|127\.0\.0\.1)\s*:\s*(\d{2,5})"
    r"|\bport\s*[:=]?\s*(\d{2,5})\b"
    r"|端口\s*[:：]?\s*(\d{2,5})",
    re.IGNORECASE,
)


def _contract_criteria(contract: Mapping[str, Any] | None) -> list[Any]:
    if not isinstance(contract, Mapping):
        return []
    rows: list[Any] = []
    rows.extend(contract.get("acceptance_criteria") or [])
    rows.append(str(contract.get("source_requirements") or ""))
    for unit in contract.get("requirement_units") or []:
        if isinstance(unit, Mapping):
            rows.append(str(unit.get("exact_text") or ""))
    for phase in contract.get("phases") or []:
        if not isinstance(phase, Mapping):
            continue
        rows.extend(phase.get("acceptance_criteria") or [])
        for task in phase.get("tasks") or []:
            if isinstance(task, Mapping):
                rows.extend(task.get("acceptance_criteria") or [])
    return [row for row in rows if str(row).strip()]


def _profile_command(
    raw: object,
    *,
    default_id: str,
    default_kind: str,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise RuntimeAcceptanceError(
            f"runtime profile command {default_id} is invalid",
            error_type="project_defect",
        )
    argv = raw.get("argv")
    if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)):
        raise RuntimeAcceptanceError(
            f"runtime profile command {default_id} must declare argv",
            error_type="project_defect",
        )
    normalized_argv = [str(part).strip() for part in argv]
    if (
        not normalized_argv
        or any(not part for part in normalized_argv)
        or any(part in {"&&", "||", ";", "|"} for part in normalized_argv)
    ):
        raise RuntimeAcceptanceError(
            f"runtime profile command {default_id} has unsafe argv",
            error_type="project_defect",
        )
    cwd = str(raw.get("cwd") or ".").strip().replace("\\", "/")
    path = Path(cwd)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeAcceptanceError(
            f"runtime profile command {default_id} has unsafe cwd",
            error_type="project_defect",
        )
    return {
        "id": str(raw.get("id") or default_id),
        "kind": str(raw.get("kind") or default_kind),
        "argv": normalized_argv,
        "cwd": cwd or ".",
        "timeout_seconds": max(
            1.0, min(float(raw.get("timeout_seconds") or 900), 3600.0)
        ),
    }


def _explicit_runtime_profile(
    contract: Mapping[str, Any],
) -> dict[str, Any] | None:
    raw = contract.get("runtime_profile") or contract.get(
        "runtime_acceptance_profile"
    )
    if not isinstance(raw, Mapping):
        return None
    commands = [
        _profile_command(
            command,
            default_id=f"command-{index}",
            default_kind="test",
        )
        for index, command in enumerate(raw.get("commands") or [], 1)
    ]
    e2e_commands = [
        _profile_command(
            command,
            default_id=f"e2e-{index}",
            default_kind="test",
        )
        for index, command in enumerate(raw.get("e2e_commands") or [], 1)
    ]
    start_raw = raw.get("start")
    start = (
        _profile_command(
            start_raw,
            default_id="start",
            default_kind="start",
        )
        if isinstance(start_raw, Mapping)
        else None
    )
    checks: list[dict[str, Any]] = []
    for index, check in enumerate(raw.get("http_checks") or [], 1):
        if not isinstance(check, Mapping):
            raise RuntimeAcceptanceError(
                f"runtime HTTP check {index} is invalid",
                error_type="project_defect",
            )
        method = str(check.get("method") or "GET").strip().upper()
        path = str(check.get("path") or "").strip()
        statuses = check.get("expected_statuses") or [200]
        if (
            method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
            or not path.startswith("/")
            or path.startswith("//")
            or not isinstance(statuses, Sequence)
        ):
            raise RuntimeAcceptanceError(
                f"runtime HTTP check {index} is invalid",
                error_type="project_defect",
            )
        checks.append({
            "method": method,
            "path": path,
            "expected_statuses": sorted({
                int(status) for status in statuses
                if isinstance(status, int) and 100 <= status <= 599
            }),
        })
        if not checks[-1]["expected_statuses"]:
            raise RuntimeAcceptanceError(
                f"runtime HTTP check {index} has no valid status",
                error_type="project_defect",
            )
    port = int(raw.get("port") or 0)
    if port and not 1 <= port <= 65535:
        raise RuntimeAcceptanceError(
            "runtime profile port is invalid",
            error_type="project_defect",
        )
    return {
        "source": "locked_project_contract",
        "commands": commands,
        "start": start,
        "http_checks": checks,
        "e2e_commands": e2e_commands,
        "port": port,
    }


def _package_manager(workspace: Path, cwd: str, package: Mapping[str, Any]) -> str:
    package_manager = str(package.get("packageManager") or "").casefold()
    if package_manager.startswith("pnpm@") or (workspace / cwd / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if package_manager.startswith("yarn@") or (workspace / cwd / "yarn.lock").is_file():
        return "yarn"
    return "npm"


def _manager_script_argv(manager: str, script: str) -> list[str]:
    if manager == "npm":
        return ["npm", "test"] if script == "test" else ["npm", "run", script]
    return [manager, "run", script]


def _manager_install_argv(workspace: Path, cwd: str, manager: str) -> list[str]:
    root = workspace / cwd
    if manager == "pnpm":
        return (
            ["pnpm", "install", "--frozen-lockfile"]
            if (root / "pnpm-lock.yaml").is_file()
            else ["pnpm", "install"]
        )
    if manager == "yarn":
        return (
            ["yarn", "install", "--frozen-lockfile"]
            if (root / "yarn.lock").is_file()
            else ["yarn", "install"]
        )
    return (
        ["npm", "ci"]
        if (root / "package-lock.json").is_file()
        else ["npm", "install"]
    )


def _contract_http_checks(criteria: Sequence[Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, tuple[int, ...], str]] = set()
    criterion_entries: list[tuple[str, Mapping[str, Any]]] = []
    endpoint_texts: dict[tuple[str, str], list[str]] = {}
    for raw in criteria:
        text = str(
            raw.get("criterion") or raw.get("text") or ""
            if isinstance(raw, Mapping)
            else raw
        )
        evidence_spec = (
            raw.get("evidence_spec")
            if isinstance(raw, Mapping)
            and isinstance(raw.get("evidence_spec"), Mapping)
            else {}
        )
        criterion_entries.append((text, evidence_spec))
        for match in _HTTP_CRITERION.finditer(text):
            endpoint_texts.setdefault(
                (match.group("method").upper(), match.group("path")),
                [],
            ).append(text)

    for text, evidence_spec in criterion_entries:
        for match in _HTTP_CRITERION.finditer(text):
            method = match.group("method").upper()
            path = match.group("path")
            endpoint_text = "\n".join(endpoint_texts.get((method, path), ()))
            status_match = _HTTP_STATUS.search(match.group("trailer") or "")
            statuses = [int(status_match.group(1))] if status_match else [200]
            check = {
                "method": method,
                "path": path,
                "expected_statuses": statuses,
            }
            path_params = re.findall(r":([A-Za-z_][A-Za-z0-9_]*)", path)
            if path_params:
                missing_resource = bool(re.search(
                    r"(?<![A-Za-z0-9_])(?:nonexistent|not[- ]found|missing)"
                    r"(?![A-Za-z0-9_])|不存在|未找到|找不到",
                    text,
                    re.IGNORECASE,
                ))
                check["path_params"] = {
                    parameter: (
                        {"literal": f"metis-missing-{parameter}"}
                        if missing_resource
                        else {"capture": parameter}
                    )
                    for parameter in path_params
                }
            check_id = str(evidence_spec.get("check_id") or "").strip()
            if check_id:
                check["check_id"] = check_id
            if (
                isinstance(evidence_spec.get("body"), Mapping)
                and evidence_spec.get("body")
            ):
                check["body"] = dict(evidence_spec["body"])
            elif method in {"POST", "PUT", "PATCH"} and any(
                200 <= status < 300 for status in statuses
            ):
                inline_body: dict[str, Any] = {}
                object_match = re.search(r"\{([^{}]{1,300})\}", text)
                if object_match:
                    for field in re.finditer(
                        r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"
                        r"(?:['\"]([^'\"]*)['\"]|(-?\d+(?:\.\d+)?)|"
                        r"(true|false))",
                        object_match.group(1),
                        re.IGNORECASE,
                    ):
                        key = field.group(1)
                        if field.group(2) is not None:
                            value = field.group(2)
                            inline_body[key] = (
                                "metis-runtime-check"
                                if not value or set(value) == {"."}
                                else value
                            )
                        elif field.group(3) is not None:
                            number = field.group(3)
                            inline_body[key] = (
                                float(number) if "." in number else int(number)
                            )
                        else:
                            inline_body[key] = (
                                field.group(4).casefold() == "true"
                            )
                if not inline_body and re.search(
                    r"\btitle\b",
                    endpoint_text,
                    re.IGNORECASE,
                ):
                    inline_body["title"] = "metis-runtime-check"
                if inline_body:
                    check["body"] = inline_body
            signature = (
                str(check.get("check_id") or ""),
                method,
                path,
                tuple(statuses),
                json.dumps(
                    {
                        "body": check.get("body") or {},
                        "path_params": check.get("path_params") or {},
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            if signature in seen:
                continue
            seen.add(signature)
            checks.append(check)
    return checks


def _declared_port(criteria: Sequence[Any]) -> int:
    for raw in criteria:
        text = str(
            raw.get("criterion") or raw.get("text") or ""
            if isinstance(raw, Mapping)
            else raw
        )
        match = _PORT_CUE.search(text)
        if not match:
            continue
        port = int(next(group for group in match.groups() if group))
        if 1 <= port <= 65535:
            return port
    return 0


def compile_runtime_profile(
    workspace: Path | str,
    project_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile a minimal runtime profile from immutable project evidence."""
    root = Path(workspace).resolve()
    if isinstance(project_contract, Mapping):
        if project_contract.get("locked") is not True:
            raise RuntimeAcceptanceError(
                "runtime profile requires a locked project contract",
                error_type="project_defect",
            )
        explicit = _explicit_runtime_profile(project_contract)
        if explicit is not None:
            profile = explicit
        else:
            profile = {}
        criteria = _contract_criteria(project_contract)
    else:
        # Legacy callers may not yet supply the contract.  Workspace docs are
        # artifact-bound, but Final QA passes the locked contract explicitly.
        criteria = []
        readme = next(
            (
                candidate for candidate in (root / "README.md", root / "readme.md")
                if candidate.is_file()
            ),
            None,
        )
        if readme:
            try:
                criteria.append(readme.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                pass
        profile = {}

    if profile:
        if not profile["http_checks"]:
            profile["http_checks"] = _contract_http_checks(criteria)
        if not profile["http_checks"]:
            raise RuntimeAcceptanceError(
                "runtime profile has no declared HTTP check",
                error_type="project_defect",
            )
        profile["profile_sha256"] = hashlib.sha256(
            json.dumps(
                profile, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return profile

    from core.pre_qa_verifier import structured_command_gates

    package_rows: list[tuple[str, Mapping[str, Any], str]] = []
    for manifest in sorted(root.rglob("package.json")):
        if "node_modules" in {part.casefold() for part in manifest.parts}:
            continue
        try:
            relative = manifest.parent.relative_to(root).as_posix() or "."
            package = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(package, Mapping):
            continue
        package_rows.append((
            relative,
            package,
            _package_manager(root, relative, package),
        ))
    if not package_rows:
        raise RuntimeAcceptanceError(
            "unsupported runtime profile: no structured runtime declaration",
            error_type="project_defect",
        )

    structured = structured_command_gates(root, criteria)
    commands: list[dict[str, Any]] = []
    e2e_commands: list[dict[str, Any]] = []
    used_roots: set[str] = set()
    for gate in structured:
        lowered = " ".join(gate.command).casefold()
        row = {
            "id": gate.gate_id,
            "kind": gate.kind,
            "argv": list(gate.command),
            "cwd": gate.cwd,
            "timeout_seconds": gate.timeout_seconds,
        }
        used_roots.add(gate.cwd)
        if any(token in lowered for token in ("cypress", "playwright", "e2e")):
            e2e_commands.append(row)
        else:
            commands.append(row)

    starts: list[dict[str, Any]] = []
    criteria_text = "\n".join(str(item) for item in criteria).casefold()
    for cwd, package, manager in package_rows:
        scripts = package.get("scripts")
        if not isinstance(scripts, Mapping):
            scripts = {}
        if str(scripts.get("start") or "").strip():
            starts.append({
                "id": f"start-{cwd.replace('/', '-')}",
                "kind": "start",
                "argv": _manager_script_argv(manager, "start"),
                "cwd": cwd,
                "timeout_seconds": 120.0,
            })
        for script, tool in (
            ("cypress", "cypress"),
            ("playwright", "playwright"),
            ("e2e", "e2e"),
            ("test:e2e", "e2e"),
        ):
            if (
                tool in criteria_text
                and str(scripts.get(script) or "").strip()
                and not any(
                    item["cwd"] == cwd
                    and script in " ".join(item["argv"])
                    for item in e2e_commands
                )
            ):
                e2e_commands.append({
                    "id": f"{tool}-{cwd.replace('/', '-')}",
                    "kind": "test",
                    "argv": _manager_script_argv(manager, script),
                    "cwd": cwd,
                    "timeout_seconds": 900.0,
                })

    if len(starts) != 1:
        raise RuntimeAcceptanceError(
            "unsupported runtime profile: expected one declared start script",
            error_type="project_defect",
        )
    start = starts[0]
    used_roots.add(start["cwd"])
    existing_installs = {
        (item["cwd"], tuple(item["argv"]))
        for item in commands if item["kind"] == "install"
    }
    install_commands: list[dict[str, Any]] = []
    for cwd, package, manager in package_rows:
        if cwd not in used_roots and len(package_rows) > 1:
            continue
        argv = _manager_install_argv(root, cwd, manager)
        if (cwd, tuple(argv)) in existing_installs:
            continue
        install_commands.append({
            "id": f"install-{cwd.replace('/', '-')}",
            "kind": "install",
            "argv": argv,
            "cwd": cwd,
            "timeout_seconds": 1200.0,
        })

    http_checks = _contract_http_checks(criteria)
    if not http_checks:
        raise RuntimeAcceptanceError(
            "unsupported runtime profile: no endpoint declared by the contract",
            error_type="project_defect",
        )
    profile = {
        "source": (
            "locked_project_contract"
            if isinstance(project_contract, Mapping)
            else "workspace_artifact"
        ),
        "commands": (
            install_commands
            + [item for item in commands if item["kind"] == "install"]
            + [item for item in commands if item["kind"] != "install"]
        ),
        "start": start,
        "http_checks": http_checks,
        "e2e_commands": e2e_commands,
        "port": _declared_port(criteria),
    }
    profile["profile_sha256"] = hashlib.sha256(
        json.dumps(
            profile, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return profile


def _workspace_snapshot(workspace: Path, config: _Config) -> dict[str, bytes]:
    try:
        _, files = collect_delivery_artifact(workspace)
    except (OSError, ValueError) as exc:
        raise RuntimeAcceptanceError(str(exc)) from exc
    _validate_workspace_snapshot(files, config)
    return dict(sorted(files.items()))


def _validate_workspace_snapshot(
    files: dict[str, bytes], config: _Config
) -> None:
    total_bytes = sum(len(content) for content in files.values())
    if len(files) > config.max_files:
        raise RuntimeAcceptanceError(
            f"workspace snapshot exceeds file limit ({config.max_files})"
        )
    if total_bytes > config.max_total_bytes:
        raise RuntimeAcceptanceError(
            f"workspace snapshot exceeds byte limit ({config.max_total_bytes})"
        )


def _request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    label: str,
    timeout: float,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    expected: tuple[int, ...] = (200,),
) -> tuple[int, Any]:
    try:
        response = session.request(
            method,
            url,
            headers=headers,
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RuntimeAcceptanceError(
            f"{label} request failed",
            error_type="infrastructure_transient",
            retryable=True,
        ) from exc
    if response.status_code not in expected:
        code = response.status_code
        if code in {408, 425, 429} or code >= 500:
            error_type, retryable = "infrastructure_transient", True
        elif code in {401, 403}:
            error_type, retryable = "environment_misconfigured", False
        else:
            error_type, retryable = "infrastructure_unavailable", False
        raise RuntimeAcceptanceError(
            f"{label} returned HTTP {code}",
            error_type=error_type,
            retryable=retryable,
        )
    if response.status_code == 204:
        return response.status_code, {}
    try:
        body = response.json()
    except ValueError as exc:
        if response.status_code == 202:
            return response.status_code, {}
        raise RuntimeAcceptanceError(
            f"{label} returned invalid JSON",
            error_type="infrastructure_provider_error",
            retryable=True,
        ) from exc
    if not isinstance(body, (dict, list)):
        raise RuntimeAcceptanceError(
            f"{label} returned an invalid response",
            error_type="infrastructure_provider_error",
            retryable=True,
        )
    return response.status_code, body


def _github_headers(config: _Config) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {config.github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _nested_text(body: dict[str, Any], *keys: str) -> str:
    value: Any = body
    for key in keys:
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    return value.strip() if isinstance(value, str) else ""


def _preflight(session: requests.Session, config: _Config) -> dict[str, Any]:
    """Validate external services without creating refs, commits, or deploys."""
    repo_api = f"{config.github_api_url}/repos/{config.github_repository}"
    _request_json(
        session,
        "GET",
        f"{repo_api}/git/ref/heads/{quote(config.github_base_branch, safe='')}",
        label="read base branch during preflight",
        timeout=config.request_timeout,
        headers=_github_headers(config),
    )
    _, service = _request_json(
        session,
        "GET",
        f"{config.render_api_url}/services/{quote(config.render_service_id, safe='')}",
        label="read Render service during preflight",
        timeout=config.request_timeout,
        headers=_render_headers(config),
    )
    owner_id = _nested_text(service, "ownerId")
    service_url = config.render_service_url or _nested_text(
        service, "serviceDetails", "url"
    ).rstrip("/")
    if not owner_id:
        raise RuntimeAcceptanceError(
            "Render service response did not include an owner",
            error_type="environment_misconfigured",
        )
    if not _valid_http_url(service_url):
        raise RuntimeAcceptanceError(
            "Render service response did not include a valid URL",
            error_type="environment_misconfigured",
        )
    return {"owner_id": owner_id, "service_url": service_url}


def preflight_runtime_acceptance() -> dict[str, Any]:
    """Return a non-mutating infrastructure readiness result."""
    if not _enabled():
        return {
            "enabled": False,
            "passed": True,
            "status": "disabled",
            "error_category": "",
            "retryable": False,
        }
    try:
        config = _load_config()
        with requests.Session() as session:
            evidence = _preflight(session, config)
        return {
            "enabled": True,
            "passed": True,
            "status": "ready",
            "error_category": "",
            "retryable": False,
            "evidence": evidence,
        }
    except RuntimeAcceptanceError as exc:
        return {
            "enabled": True,
            "passed": False,
            "status": "infrastructure_blocked",
            "summary": str(exc),
            "error_category": exc.error_type,
            "retryable": exc.retryable,
        }
    except Exception:
        return {
            "enabled": True,
            "passed": False,
            "status": "infrastructure_blocked",
            "summary": "Runtime acceptance preflight failed unexpectedly",
            "error_category": "infrastructure_provider_error",
            "retryable": True,
        }


def _push_snapshot(
    session: requests.Session,
    files: dict[str, bytes],
    config: _Config,
    project_id: str,
    logs: list[str],
) -> str:
    repo_api = f"{config.github_api_url}/repos/{config.github_repository}"
    headers = _github_headers(config)
    encoded_branch = quote(config.github_branch, safe="")
    ref_url = f"{repo_api}/git/ref/heads/{encoded_branch}"
    status, branch_ref = _request_json(
        session,
        "GET",
        ref_url,
        label="read acceptance branch",
        timeout=config.request_timeout,
        headers=headers,
        expected=(200, 404),
    )
    branch_exists = status == 200
    if branch_exists:
        parent_sha = _nested_text(branch_ref, "object", "sha")
    else:
        base_ref_url = (
            f"{repo_api}/git/ref/heads/{quote(config.github_base_branch, safe='')}"
        )
        _, base_ref = _request_json(
            session,
            "GET",
            base_ref_url,
            label="read base branch",
            timeout=config.request_timeout,
            headers=headers,
        )
        parent_sha = _nested_text(base_ref, "object", "sha")
    if not parent_sha:
        raise RuntimeAcceptanceError("GitHub branch response did not include a commit SHA")

    tree_items: list[dict[str, str]] = []
    for path, content in files.items():
        _, blob = _request_json(
            session,
            "POST",
            f"{repo_api}/git/blobs",
            label="create snapshot blob",
            timeout=config.request_timeout,
            headers=headers,
            payload={
                "content": base64.b64encode(content).decode("ascii"),
                "encoding": "base64",
            },
            expected=(201,),
        )
        blob_sha = _nested_text(blob, "sha")
        if not blob_sha:
            raise RuntimeAcceptanceError("GitHub blob response did not include a SHA")
        tree_items.append(
            {"path": path, "mode": "100644", "type": "blob", "sha": blob_sha}
        )

    _, tree = _request_json(
        session,
        "POST",
        f"{repo_api}/git/trees",
        label="create snapshot tree",
        timeout=config.request_timeout,
        headers=headers,
        payload={"tree": tree_items},
        expected=(201,),
    )
    tree_sha = _nested_text(tree, "sha")
    if not tree_sha:
        raise RuntimeAcceptanceError("GitHub tree response did not include a SHA")

    safe_project_id = re.sub(r"[^A-Za-z0-9_.-]", "-", project_id)[:80] or "project"
    _, commit = _request_json(
        session,
        "POST",
        f"{repo_api}/git/commits",
        label="create snapshot commit",
        timeout=config.request_timeout,
        headers=headers,
        payload={
            "message": f"Runtime acceptance snapshot for {safe_project_id}",
            "tree": tree_sha,
            "parents": [parent_sha],
        },
        expected=(201,),
    )
    commit_sha = _nested_text(commit, "sha")
    if not commit_sha:
        raise RuntimeAcceptanceError("GitHub commit response did not include a SHA")

    if branch_exists:
        _request_json(
            session,
            "PATCH",
            f"{repo_api}/git/refs/heads/{encoded_branch}",
            label="update acceptance branch",
            timeout=config.request_timeout,
            headers=headers,
            payload={"sha": commit_sha, "force": True},
        )
    else:
        _request_json(
            session,
            "POST",
            f"{repo_api}/git/refs",
            label="create acceptance branch",
            timeout=config.request_timeout,
            headers=headers,
            payload={"ref": f"refs/heads/{config.github_branch}", "sha": commit_sha},
            expected=(201,),
        )
    logs.append(f"snapshot pushed: {len(files)} files")
    return commit_sha


def _render_headers(config: _Config) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {config.render_api_key}",
        "Content-Type": "application/json",
    }


def _append_render_build_logs(
    session: requests.Session,
    config: _Config,
    logs: list[str],
    *,
    started_at: float,
    deploy_id: str = "",
) -> None:
    """Attach bounded, redacted build and startup output for automatic rework."""
    headers = _render_headers(config)
    service_id = quote(config.render_service_id, safe="")
    try:
        _, service = _request_json(
            session,
            "GET",
            f"{config.render_api_url}/services/{service_id}",
            label="read Render service for build logs",
            timeout=config.request_timeout,
            headers=headers,
        )
        owner_id = _nested_text(service, "ownerId")
        if not owner_id:
            return
        query = (
            f"ownerId={quote(owner_id, safe='')}&resource={service_id}"
            f"&startTime={quote(datetime.fromtimestamp(started_at, timezone.utc).isoformat(), safe='')}"
            "&direction=backward&limit=120"
        )
        _, body = _request_json(
            session,
            "GET",
            f"{config.render_api_url}/logs?{query}",
            label="read Render deploy logs",
            timeout=config.request_timeout,
            headers=headers,
        )
    except RuntimeAcceptanceError:
        return

    entries = body.get("logs")
    if not isinstance(entries, list):
        return
    sanitized: list[str] = []
    ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_deploy_id = _nested_text(entry, "deployId") or _nested_text(
            entry, "deploy", "id"
        )
        if deploy_id and entry_deploy_id and entry_deploy_id != deploy_id:
            continue
        message = ansi.sub("", str(entry.get("message") or "")).strip()
        if not message:
            continue
        message = message.replace(config.github_token, "[redacted]")
        message = message.replace(config.render_api_key, "[redacted]")
        sanitized.append(message[:700])
    # Test runners often print the actual Expected/Received block well before
    # Docker's export summary. Keep enough bounded context for deterministic
    # rework while still capping every line and redacting credentials.
    for message in reversed(sanitized[:100]):
        logs.append(f"build: {message}")


def _render_logs_show_project_failure(logs: list[str]) -> bool:
    evidence = "\n".join(logs[-120:])
    return bool(re.search(
        r"(?:cannot find module|module_not_found|syntaxerror|referenceerror|typeerror|"
        r"unexpected reserved word|npm err!|failed to compile|no open ports detected|"
        r"port scan timeout|exited with status [1-9]|file:///[^\s:]+:\d+)",
        evidence,
        re.IGNORECASE,
    ))


def _service_url(
    session: requests.Session, config: _Config, headers: dict[str, str]
) -> str:
    if config.render_service_url:
        return config.render_service_url
    _, service = _request_json(
        session,
        "GET",
        f"{config.render_api_url}/services/{quote(config.render_service_id, safe='')}",
        label="read Render service",
        timeout=config.request_timeout,
        headers=headers,
    )
    url = _nested_text(service, "serviceDetails", "url").rstrip("/")
    if not _valid_http_url(url):
        raise RuntimeAcceptanceError("Render service response did not include a valid URL")
    return url


def _deploy(
    session: requests.Session,
    config: _Config,
    commit_sha: str,
    logs: list[str],
) -> tuple[str, str, str]:
    headers = _render_headers(config)
    service_id = quote(config.render_service_id, safe="")
    deploys_url = f"{config.render_api_url}/services/{service_id}/deploys"
    status, deploy = _request_json(
        session,
        "POST",
        deploys_url,
        label="create Render deploy",
        timeout=config.request_timeout,
        headers=headers,
        payload={"commitId": commit_sha, "clearCache": "clear"},
        expected=(200, 201, 202),
    )
    deploy_id = _nested_text(deploy, "id")
    if not deploy_id and status == 202:
        # Render may acknowledge a queued deploy with an empty 202 response.
        # Recover the accepted deploy by the exact commit instead of creating
        # duplicates or treating an asynchronous success as a hard failure.
        for _ in range(10):
            _, recent = _request_json(
                session,
                "GET",
                f"{deploys_url}?limit=20",
                label="find accepted Render deploy",
                timeout=config.request_timeout,
                headers=headers,
            )
            entries = recent if isinstance(recent, list) else recent.get("deploys", [])
            for entry in entries if isinstance(entries, list) else []:
                candidate = entry.get("deploy", entry) if isinstance(entry, dict) else {}
                candidate_commit = _nested_text(candidate, "commit", "id") or _nested_text(
                    candidate, "commitId"
                )
                if candidate_commit == commit_sha:
                    deploy_id = _nested_text(candidate, "id")
                    if deploy_id:
                        logs.append(f"Render accepted deploy recovered: {deploy_id}")
                        break
            if deploy_id:
                break
            time.sleep(config.poll_interval)
    if not deploy_id:
        raise RuntimeAcceptanceError("Render deploy response did not include an ID")
    logs.append(f"Render deploy created: {deploy_id}")

    deadline = time.monotonic() + config.deploy_timeout
    latest_status = str(deploy.get("status", "created")).lower()
    while True:
        _, current = _request_json(
            session,
            "GET",
            f"{deploys_url}/{quote(deploy_id, safe='')}",
            label="read Render deploy",
            timeout=config.request_timeout,
            headers=headers,
        )
        latest_status = str(current.get("status", "unknown")).lower()
        if latest_status == "live":
            deployed_commit = _nested_text(current, "commit", "id") or _nested_text(
                current, "commitId"
            )
            if deployed_commit and deployed_commit != commit_sha:
                logs.append(
                    f"Render deploy artifact mismatch: expected {commit_sha}, got {deployed_commit}"
                )
                return deploy_id, "artifact_mismatch", ""
            logs.append("Render deploy is live")
            return deploy_id, latest_status, _service_url(session, config, headers)
        if latest_status in _TERMINAL_DEPLOY_FAILURES:
            logs.append(f"Render deploy stopped: {latest_status}")
            return deploy_id, latest_status, ""
        if time.monotonic() >= deadline:
            logs.append("Render deploy timed out")
            return deploy_id, "timeout", ""
        time.sleep(config.poll_interval)


def _health_check(
    session: requests.Session,
    service_url: str,
    config: _Config,
    logs: list[str],
) -> bool:
    deadline = time.monotonic() + config.health_timeout
    while True:
        for endpoint in ("/api/health", "/health"):
            try:
                response = session.request(
                    "GET",
                    f"{service_url}{endpoint}",
                    timeout=config.request_timeout,
                )
                if 200 <= response.status_code < 300:
                    logs.append(f"health check passed: {endpoint}")
                    return True
            except requests.RequestException:
                pass
        if time.monotonic() >= deadline:
            logs.append("health check failed")
            return False
        time.sleep(config.poll_interval)


def _frontend_check(
    session: requests.Session,
    service_url: str,
    config: _Config,
    logs: list[str],
) -> bool:
    """Require the public root to be served on the same port as the API."""
    deadline = time.monotonic() + config.health_timeout
    while True:
        try:
            response = session.request(
                "GET",
                f"{service_url}/",
                timeout=config.request_timeout,
            )
            if 200 <= response.status_code < 300:
                logs.append("frontend check passed: /")
                return True
        except requests.RequestException:
            pass
        if time.monotonic() >= deadline:
            logs.append("frontend check failed: public root did not return 2xx")
            return False
        time.sleep(config.poll_interval)


def _run_remote_profile_http_checks(
    session: requests.Session,
    service_url: str,
    config: _Config,
    profile: Mapping[str, Any],
    logs: list[str],
) -> dict[str, Any]:
    """Run only HTTP checks declared by the artifact-bound runtime profile."""
    checks = list(profile.get("http_checks") or [])
    if not checks:
        raise RuntimeAcceptanceError(
            "runtime profile has no declared HTTP check",
            error_type="project_defect",
    )
    passed_checks: list[str] = []
    observations: list[dict[str, Any]] = []
    captured_values: dict[str, Any] = {}
    for index, check in enumerate(checks):
        method = str(check.get("method") or "GET").upper()
        path = str(check.get("path") or "")
        missing_resource_scenario = False
        for parameter, source in (
            check.get("path_params") or {}
        ).items():
            if not isinstance(source, Mapping):
                continue
            if "literal" in source:
                value = source.get("literal")
                missing_resource_scenario = (
                    missing_resource_scenario
                    or str(value) == "metis-missing-id"
                )
            else:
                value = captured_values.get(
                    str(source.get("capture") or parameter)
                )
            if value is None:
                raise RuntimeAcceptanceError(
                    f"runtime HTTP check {method} {path} has no captured "
                    f"value for :{parameter}",
                    error_type="project_defect",
                )
            path = path.replace(
                f":{parameter}",
                quote(str(value), safe=""),
            )
        body = (
            dict(check.get("body") or {})
            if isinstance(check.get("body"), Mapping)
            else {}
        )
        expected = {
            int(status) for status in check.get("expected_statuses") or []
        }
        if (
            not body
            and method in {"PUT", "PATCH"}
            and (
                any(200 <= status < 300 for status in expected)
                or 404 in expected
            )
        ):
            body = {
                key: value
                for key, value in captured_values.items()
                if key not in {"id", "_id"}
            }
            if not body:
                body = {"title": "metis-runtime-check"}
        deadline = time.monotonic() + (
            config.health_timeout if index == 0 else 0
        )
        last_failure: dict[str, Any] | None = None
        while True:
            try:
                request_kwargs: dict[str, Any] = {
                    "timeout": config.request_timeout,
                }
                if body:
                    request_kwargs["json"] = body
                response = session.request(
                    method,
                    f"{service_url}{path}",
                    **request_kwargs,
                )
                if response.status_code in expected:
                    label = f"{method} {path}"
                    passed_checks.append(label)
                    logs.append(f"runtime HTTP check passed: {label}")
                    try:
                        response_body = response.json()
                        is_json = True
                    except (AttributeError, TypeError, ValueError):
                        response_body = None
                        is_json = False
                    if (
                        200 <= response.status_code < 300
                        and isinstance(response_body, Mapping)
                    ):
                        captured_values.update({
                            str(key): value
                            for key, value in response_body.items()
                            if isinstance(value, (str, int, float))
                        })
                    observations.append({
                        "check_id": str(check.get("check_id") or ""),
                        "method": method,
                        "path": path,
                        "status_code": int(response.status_code),
                        "is_json": is_json,
                        "body": response_body,
                        "expected_statuses": sorted(expected),
                    })
                    break
                try:
                    failure_body = json.dumps(
                        response.json(),
                        ensure_ascii=False,
                        sort_keys=True,
                    )[:1000]
                except (AttributeError, TypeError, ValueError):
                    failure_body = str(
                        getattr(response, "text", "") or ""
                    )[:1000]
                file_path, line = _http_failure_location(failure_body)
                last_failure = {
                    "status_code": int(response.status_code),
                    "response": failure_body,
                    "file_path": file_path,
                    "line": line,
                }
                if response.status_code < 500:
                    correction = ""
                    if (
                        400 in expected
                        and not body
                        and 200 <= response.status_code < 300
                    ):
                        correction = (
                            "; required_fix=reject an empty JSON request body "
                            "with status 400 before applying any update"
                        )
                    elif (
                        missing_resource_scenario
                        and 404 in expected
                        and response.status_code == 400
                    ):
                        correction = (
                            "; scenario=missing_resource"
                            "; root_cause=request body validation ran before "
                            "the resource existence check"
                            "; required_fix=check whether the resource exists "
                            "before validating the update body, then return "
                            "404 for an unknown id"
                        )
                    raise RuntimeAcceptanceError(
                        f"runtime HTTP check {method} {path} failed: "
                        f"expected {sorted(expected)}, got "
                        f"{response.status_code}; request_body_keys="
                        f"{sorted(body)}; response={failure_body}"
                        f"{correction}",
                        error_type="project_defect",
                        fix_hint=(
                            "Make the endpoint accept the contract-valid "
                            "request fields and return an expected status."
                        ),
                        file_path=file_path,
                    )
            except requests.RequestException:
                pass
            if time.monotonic() >= deadline:
                if last_failure is not None:
                    response_excerpt = str(last_failure["response"])
                    location = (
                        f"; source={last_failure['file_path']}:"
                        f"{last_failure['line']}"
                        if last_failure.get("file_path")
                        else ""
                    )
                    raise RuntimeAcceptanceError(
                        f"runtime HTTP check {method} {path} failed: "
                        f"expected {sorted(expected)}, got "
                        f"{last_failure['status_code']}; request_body_keys="
                        f"{sorted(body)}; response={response_excerpt}"
                        f"{location}",
                        error_type="project_defect",
                        file_path=str(last_failure.get("file_path") or ""),
                        fix_hint=_http_failure_fix_hint(
                            response_excerpt,
                            "Repair the reported source failure, then rerun "
                            "this exact HTTP request and expected status.",
                        ),
                    )
                raise RuntimeAcceptanceError(
                    f"runtime HTTP check {method} {path} failed",
                    error_type="project_defect",
                    fix_hint=(
                        "Make the endpoint declared by the locked project "
                        "contract return its required status."
                    ),
                )
            time.sleep(config.poll_interval)
    return {
        "passed": True,
        "checks": passed_checks,
        "observations": observations,
    }


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _run_local_runtime_profile(
    workspace: Path,
    profile: Mapping[str, Any],
    logs: list[str],
) -> dict[str, Any]:
    """Execute an artifact-bound profile without a shell or inherited secrets."""
    from core.pre_qa_verifier import (
        CommandGate,
        LocalCommandRunner,
        _isolated_command_environment,
        _terminate_command_process_tree,
    )

    runner = LocalCommandRunner()
    passed_checks: list[str] = []
    for raw in profile.get("commands") or []:
        gate = CommandGate(
            gate_id=str(raw.get("id") or "runtime-command"),
            kind=str(raw.get("kind") or "test"),
            command=tuple(str(part) for part in raw.get("argv") or []),
            cwd=str(raw.get("cwd") or "."),
            timeout_seconds=float(raw.get("timeout_seconds") or 900),
            required=True,
        )
        observation = runner(gate, workspace)
        if observation.exit_code != 0:
            # 失败时优先定位真正的错误行（Error/assert/fail/✕/FAIL/断言），
            # 避免长 stdout 通过日志尾部或 deprecation 噪声掩盖真因。
            diagnostic = _extract_failure_diagnostic(
                observation.stdout or "", observation.stderr or ""
            )
            raise RuntimeAcceptanceError(
                f"local runtime command {gate.gate_id} failed"
                + (f": {diagnostic[-512:]}" if diagnostic else ""),
                error_type="project_defect",
                file_path=(
                    f"{gate.cwd.rstrip('/')}/package.json"
                    if gate.cwd != "."
                    else "package.json"
                ),
            )
        passed_checks.append(gate.gate_id)
        logs.append(f"local runtime command passed: {gate.gate_id}")

    start = profile.get("start")
    if not isinstance(start, Mapping):
        raise RuntimeAcceptanceError(
            "runtime profile has no start command",
            error_type="project_defect",
        )
    argv = tuple(str(part) for part in start.get("argv") or [])
    if not argv:
        raise RuntimeAcceptanceError(
            "runtime profile start command is empty",
            error_type="project_defect",
        )
    cwd = (workspace / str(start.get("cwd") or ".")).resolve()
    try:
        cwd.relative_to(workspace)
    except ValueError as exc:
        raise RuntimeAcceptanceError(
            "runtime profile start cwd escapes the workspace",
            error_type="project_defect",
        ) from exc
    executable = argv[0]
    if not Path(executable).is_absolute():
        executable = shutil.which(
            executable,
            path=_isolated_command_environment().get("PATH"),
        ) or executable
    command = (executable, *argv[1:])
    # A declared production default (for example 3000) is part of the
    # delivery contract, but local acceptance must never bind or probe a
    # shared development port.  The generated app receives an isolated port
    # through PORT, while remote profiles retain their declared settings.
    port = _free_loopback_port()
    environment = _isolated_command_environment()
    environment["PORT"] = str(port)
    popen_options: dict[str, Any] = {}
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **popen_options,
        )
    except OSError as exc:
        raise RuntimeAcceptanceError(
            "local runtime start command could not be launched",
            error_type="project_defect",
            file_path=(
                f"{start.get('cwd')}/package.json"
                if start.get("cwd") not in {None, "", "."}
                else "package.json"
            ),
        ) from exc
    try:
        local_config = type(
            "_LocalRuntimeConfig",
            (),
            {
                "request_timeout": 3.0,
                "health_timeout": min(
                    float(start.get("timeout_seconds") or 120), 120.0
                ),
                "poll_interval": 0.25,
            },
        )()
        with requests.Session() as session:
            http_evidence = _run_remote_profile_http_checks(
                session,
                f"http://127.0.0.1:{port}",
                local_config,
                profile,
                logs,
            )
        passed_checks.extend(http_evidence["checks"])
        for raw in profile.get("e2e_commands") or []:
            gate = CommandGate(
                gate_id=str(raw.get("id") or "runtime-e2e"),
                kind="test",
                command=tuple(str(part) for part in raw.get("argv") or []),
                cwd=str(raw.get("cwd") or "."),
                timeout_seconds=float(raw.get("timeout_seconds") or 900),
                environment={"PORT": str(port)},
                required=True,
            )
            observation = runner(gate, workspace)
            if observation.exit_code != 0:
                raise RuntimeAcceptanceError(
                    f"local runtime E2E command {gate.gate_id} failed",
                    error_type="project_defect",
                    file_path=(
                        f"{gate.cwd.rstrip('/')}/package.json"
                        if gate.cwd != "."
                        else "package.json"
                    ),
                )
            passed_checks.append(gate.gate_id)
            logs.append(f"local runtime E2E passed: {gate.gate_id}")
    finally:
        _terminate_command_process_tree(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
    return {
        "passed": True,
        "checks": passed_checks,
        "http_observations": list(
            http_evidence.get("observations") or []
        ),
        "port": port,
    }


def run_runtime_acceptance(
    workspace: Path,
    project_id: str,
    previous_result: dict[str, Any] | None = None,
    required_paths: list[str] | None = None,
    project_contract: Mapping[str, Any] | None = None,
    force_local: bool = False,
) -> dict[str, Any]:
    """Execute a local or remote profile bound to one canonical artifact."""
    remote_enabled = _enabled() and not force_local
    result = _result(
        enabled=remote_enabled,
        passed=False,
        status="failed",
        summary="Runtime acceptance failed",
    )
    result["mode"] = "remote" if remote_enabled else "local"
    result["source"] = (
        "remote_deterministic_runtime"
        if remote_enabled
        else "local_deterministic_runtime"
    )
    logs: list[str] = result["logs"]
    try:
        config = _load_config() if remote_enabled else None
        manifest, files = collect_delivery_artifact(
            Path(workspace), required_paths=required_paths or ()
        )
        limits = config or type(
            "_LocalSnapshotLimits",
            (),
            {
                "max_files": int(
                    _positive_number(
                        "RUNTIME_ACCEPTANCE_MAX_FILES", "800", int
                    )
                ),
                "max_total_bytes": int(
                    _positive_number(
                        "RUNTIME_ACCEPTANCE_MAX_TOTAL_BYTES",
                        "15728640",
                        int,
                    )
                ),
            },
        )()
        _validate_workspace_snapshot(files, limits)
        profile = compile_runtime_profile(
            Path(workspace),
            project_contract,
        )
        artifact_sha = str(manifest["artifact_sha256"])
        profile_sha = str(profile["profile_sha256"])
        acceptance_key = _acceptance_key(
            project_id, f"{artifact_sha}:{profile_sha}"
        )
        result.update({
            "artifact_sha256": artifact_sha,
            "artifact_digest": artifact_sha,
            "artifact_manifest": manifest,
            "artifact_manifest_rule_version": manifest["rule_version"],
            "acceptance_key": acceptance_key,
            "profile_sha256": profile_sha,
            "profile_source": profile.get("source"),
            "current_step": (
                "preflight" if remote_enabled else "local_runtime"
            ),
            "steps": [],
        })
        expected_mode = result["mode"]
        if (
            isinstance(previous_result, dict)
            and previous_result.get("passed") is True
            and previous_result.get("status") == "passed"
            and previous_result.get("artifact_sha256") == artifact_sha
            and previous_result.get("rule_version") == _RULE_VERSION
            and previous_result.get("profile_sha256") == profile_sha
            and previous_result.get("mode") == expected_mode
            and (
                expected_mode == "local"
                or (
                    previous_result.get("deploy_id")
                    and previous_result.get("service_url")
                    and previous_result.get("commit_sha")
                )
            )
            and (time.time() - float(previous_result.get("updated_at") or 0))
            <= _CACHE_MAX_AGE_SECONDS
        ):
            cached = dict(previous_result)
            cached["logs"] = list(previous_result.get("logs") or [])
            cached["cached"] = True
            cached["current_step"] = "completed"
            cached["updated_at"] = time.time()
            return cached
        logs.append(f"workspace snapshot prepared: {len(files)} files")
        if not remote_enabled:
            with tempfile.TemporaryDirectory(
                prefix="metis-runtime-acceptance-"
            ) as temporary:
                isolated_workspace = Path(temporary).resolve()
                for relative, content in files.items():
                    target = isolated_workspace.joinpath(
                        *Path(relative).parts
                    )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                local_evidence = _run_local_runtime_profile(
                    isolated_workspace,
                    profile,
                    logs,
                )
            result["checks"] = local_evidence.get("checks") or []
            result["http_observations"] = list(
                local_evidence.get("http_observations") or []
            )
            result["steps"].append({
                "name": "local_runtime_profile",
                "status": "passed",
                "checks": list(result["checks"]),
            })
            result.update({
                "passed": True,
                "status": "passed",
                "summary": (
                    "Local runtime profile passed for the current artifact"
                ),
                "current_step": "completed",
                "updated_at": time.time(),
            })
            return result

        assert config is not None
        with requests.Session() as session:
            preflight = _preflight(session, config)
            result["steps"].append({
                "name": "infrastructure_preflight",
                "status": "passed",
                "evidence": preflight,
            })
            result["current_step"] = "snapshot_push"
            commit_sha = _push_snapshot(session, files, config, project_id, logs)
            result["commit_sha"] = commit_sha
            result["steps"].append({"name": "snapshot_push", "status": "passed"})
            result["current_step"] = "isolated_deploy"
            deploy_id, deploy_status, service_url = _deploy(
                session, config, commit_sha, logs
            )
            result["deploy_id"] = deploy_id
            result["service_url"] = service_url
            if deploy_status != "live":
                _append_render_build_logs(
                    session,
                    config,
                    logs,
                    started_at=float(result["started_at"]),
                    deploy_id=str(result.get("deploy_id") or ""),
                )
                is_project_failure = (
                    deploy_status == "build_failed"
                    or deploy_status == "update_failed"
                    and _render_logs_show_project_failure(logs)
                )
                result.update({
                    "status": deploy_status,
                    "summary": f"Runtime deploy did not become live ({deploy_status})",
                    "error_category": (
                        "project_defect" if is_project_failure else "infrastructure_transient"
                    ),
                    "retryable": not is_project_failure,
                    "actionable": is_project_failure,
                    "current_step": "completed",
                })
                result["steps"].append({
                    "name": "isolated_deploy",
                    "status": "failed",
                    "deploy_id": deploy_id,
                })
                return result
            result["steps"].append({
                "name": "isolated_deploy",
                "status": "passed",
                "deploy_id": deploy_id,
            })
            result["current_step"] = "contract_http_checks"
            http_evidence = _run_remote_profile_http_checks(
                session,
                service_url,
                config,
                profile,
                logs,
            )
            result["checks"] = http_evidence["checks"]
            result["http_observations"] = list(
                http_evidence.get("observations") or []
            )
            result["steps"].append({
                "name": "contract_http_checks",
                "status": "passed",
                "checks": http_evidence["checks"],
            })
    except RuntimeAcceptanceError as exc:
        # RuntimeAcceptanceError messages are intentionally authored without
        # response bodies, headers, or secret values.
        result["summary"] = str(exc) or "Runtime acceptance failed"
        if str(exc).startswith("runtime HTTP check") and ":" in str(exc):
            logs.append(str(exc).split(":", 1)[0])
        logs.append(result["summary"])
        result["error_category"] = exc.error_type
        result["retryable"] = exc.retryable
        if exc.file_path:
            result["file_path"] = exc.file_path
        if exc.fix_hint:
            result["fix_hint"] = exc.fix_hint
        result["actionable"] = exc.error_type == "project_defect"
        if str(exc).startswith("unsupported runtime profile"):
            result["status"] = "unsupported_runtime_profile"
        elif str(exc).startswith("runtime HTTP check"):
            result["status"] = "runtime_check_failed"
        elif (
            exc.error_type.startswith("infrastructure")
            or exc.error_type == "environment_misconfigured"
        ):
            result["status"] = "infrastructure_blocked"
        result["current_step"] = "completed"
        return result
    except OSError as exc:
        result["summary"] = str(exc) or "Runtime acceptance failed"
        logs.append(result["summary"])
        result["error_category"] = "project_defect"
        result["actionable"] = True
        result["current_step"] = "completed"
        return result
    except Exception:
        # A malformed provider response or other unexpected integration error
        # must never turn into an optimistic pass.  Do not include the raw
        # exception because third-party errors can echo request credentials.
        result["summary"] = "Runtime acceptance failed due to an unexpected provider error"
        logs.append(result["summary"])
        result["status"] = "infrastructure_blocked"
        result["error_category"] = "infrastructure_provider_error"
        result["retryable"] = True
        result["current_step"] = "completed"
        return result

    result["passed"] = True
    result["status"] = "passed"
    result["summary"] = (
        "Remote runtime profile passed for the current artifact"
    )
    result["current_step"] = "completed"
    result["updated_at"] = time.time()
    return result


__all__ = ["preflight_runtime_acceptance", "run_runtime_acceptance"]
