"""Industrial end-to-end acceptance against a deployed MeTis instance.

This test intentionally stops on the first platform or generated-deliverable
failure.  It does not retry failed agents or reinterpret partial output as a
pass.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import requests


BASE = os.getenv("METIS_E2E_BASE", "https://metis-cho3.onrender.com/api").rstrip("/")
HTTP_TIMEOUT = 180
AGENT_TIMEOUT = int(os.getenv("METIS_AGENT_TIMEOUT", "1800"))
QA_TIMEOUT = int(os.getenv("METIS_QA_TIMEOUT", "1800"))
TERMINAL_AGENT_STATES = {
    "completed",
    "failed",
    "model_failed",
    "fix_limit_reached",
    "cancelled",
}
DOCKER_IMAGE = os.getenv("METIS_ACCEPTANCE_DOCKER_IMAGE", "node:20-bookworm")
MAX_ARCHIVE_MEMBERS = int(os.getenv("METIS_ACCEPTANCE_MAX_FILES", "5000"))
MAX_ARCHIVE_FILE_SIZE = int(os.getenv("METIS_ACCEPTANCE_MAX_FILE_BYTES", str(100 * 1024 * 1024)))
MAX_ARCHIVE_TOTAL_SIZE = int(os.getenv("METIS_ACCEPTANCE_MAX_TOTAL_BYTES", str(500 * 1024 * 1024)))
MAX_ARCHIVE_RATIO = int(os.getenv("METIS_ACCEPTANCE_MAX_COMPRESSION_RATIO", "200"))

EXIT_PROJECT_FAILURE = 2
EXIT_INFRASTRUCTURE_FAILURE = 3
EXIT_USER_ACTION_REQUIRED = 4
EXIT_SCRIPT_ERROR = 5
PROJECT_FAILURE_STATES = {
    "quality_regressed", "no_progress", "qa_blocked", "pre_qa_failed",
    "rebuild_regressed", "rebuild_no_progress", "blocked", "failed", "error",
}
USER_ACTION_STATES = {
    "awaiting_manual_fix",
    "awaiting_decision",
    "needs_manual",
    "manual_fix_required",
}
INFRASTRUCTURE_FAILURE_STATES = {
    "infrastructure_blocked",
    "infrastructure_unavailable",
    "infrastructure_failed",
    "model_failed",
}
RUNNING_QA_STATES = {
    "checking",
    "repairing",
    "rebuild_started",
    "running",
    "queued",
    "starting",
}
RESUMABLE_PHASE_STATES = {
    "active",
    "in_progress",
    "running",
    "reviewing",
    "needs_rework",
    "qa_blocked",
}
QA_ONLY_PHASE_STATES = {"qa_pending"}
RUNNING_RECOVERY_PHASE_STATES = {"pre_qa_verifying", "continuing"}
FAIL_CLOSED_PHASE_STATES = {
    "waiting_engineer",
    "pre_qa_failed",
    "model_failed",
    "infrastructure_failed",
}
FINAL_QA_RECOVERABLE_FAILURES = {
    "REWORK_LOCK_UNAVAILABLE",
    "REWORK_FAILED",
    "REWORK_TIMEOUT",
    "STALE_WORKSPACE",
    "QC_TIMEOUT",
    "QC_EXECUTION_ERROR",
}


class AcceptanceFailure(RuntimeError):
    def __init__(self, message: str, exit_code: int = EXIT_PROJECT_FAILURE):
        super().__init__(message)
        self.exit_code = exit_code


class ResilientSession(requests.Session):
    """Authenticated session that recovers safe polling after auth/network loss."""

    def __init__(self, login_name: str, login_password: str):
        super().__init__()
        self.trust_env = False
        self._login_name = login_name
        self._login_password = login_password

    def authenticate(self) -> None:
        last_error: requests.RequestException | None = None
        response: requests.Response | None = None
        for attempt in range(3):
            try:
                response = super().request(
                    "POST",
                    f"{BASE}/auth/login",
                    json={"login": self._login_name, "password": self._login_password},
                    timeout=HTTP_TIMEOUT,
                )
                if response.status_code != 429 and response.status_code < 500:
                    break
            except requests.RequestException as exc:
                last_error = exc
            if attempt < 2:
                time.sleep(min(2 ** attempt, 4))
        if response is None:
            raise AcceptanceFailure(
                f"login connection failed: {last_error}", EXIT_INFRASTRUCTURE_FAILURE
            ) from last_error
        auth = require(response, "login")
        if not auth.get("token") and not self.cookies:
            raise AcceptanceFailure(
                "login response did not establish authentication",
                EXIT_USER_ACTION_REQUIRED,
            )

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        method = method.upper()
        # Status polling is safe to retry across a Render instance restart.
        # Keep state-changing requests single-shot so acceptance never repeats
        # an ambiguous mutation.
        attempts = 8 if method in {"GET", "HEAD"} else 1
        last_error: requests.RequestException | None = None
        for attempt in range(attempts):
            try:
                response = super().request(method, url, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                self.authenticate()
                time.sleep(min(2 ** attempt, 4))
                continue
            if response.status_code == 401 and not url.endswith("/auth/login"):
                self.authenticate()
                return super().request(method, url, **kwargs)
            if (
                method in {"GET", "HEAD"}
                and (response.status_code == 429 or response.status_code >= 500)
                and attempt + 1 < attempts
            ):
                time.sleep(min(2 ** attempt, 8))
                continue
            return response
        raise AcceptanceFailure(
            f"request connection failed after {attempts} attempts: {last_error}",
            EXIT_INFRASTRUCTURE_FAILURE,
        )


class AcceptanceSummary:
    def __init__(self, project_id: str):
        repository = Path(__file__).resolve().parents[1]
        self.evidence_dir = repository / "work" / "acceptance" / project_id
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.evidence_dir / "acceptance-summary.json"
        existing: dict[str, Any] = {}
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("project_id") == project_id:
                    existing = loaded
            except (OSError, json.JSONDecodeError):
                existing = {}
        self.data: dict[str, Any] = {
            **existing,
            "status": "running",
            "project_id": project_id,
            "platform_base": BASE,
            "expected_commit": os.getenv("METIS_EXPECTED_COMMIT"),
            "updated_at": time.time(),
        }
        for terminal_key in ("error", "exit_code", "failed_at", "completed_at"):
            self.data.pop(terminal_key, None)
        self.write()

    def update(self, **values: Any) -> None:
        self.data.update(values)
        self.data["updated_at"] = time.time()
        self.write()

    def write(self) -> None:
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)


def fail(message: str, exit_code: int = EXIT_PROJECT_FAILURE) -> None:
    raise AcceptanceFailure(message, exit_code)


def require(response: requests.Response, step: str) -> Any:
    if not response.ok:
        exit_code = EXIT_PROJECT_FAILURE
        if response.status_code in {401, 403}:
            exit_code = EXIT_USER_ACTION_REQUIRED
        elif response.status_code == 429 or response.status_code >= 500:
            exit_code = EXIT_INFRASTRUCTURE_FAILURE
        fail(
            f"{step}: HTTP {response.status_code} {response.text[:1000]}",
            exit_code,
        )
    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type:
        fail(f"{step}: expected JSON, received {content_type!r}")
    return response.json()


def flatten_tree(entries: list[dict[str, Any]]) -> list[str]:
    files: list[str] = []
    for entry in entries:
        if entry.get("type") == "folder":
            files.extend(flatten_tree(entry.get("children") or []))
        elif entry.get("type") == "file" and entry.get("key"):
            files.append(str(entry["key"]).replace("\\", "/"))
    return files


def poll_agents(session: requests.Session, project_id: str, agent_ids: list[str]) -> None:
    deadline = time.monotonic() + AGENT_TIMEOUT
    last_states: dict[str, str | None] = {}
    while time.monotonic() < deadline:
        records = {
            agent_id: require(
                session.get(
                    f"{BASE}/projects/{project_id}/agents/{agent_id}/status",
                    timeout=HTTP_TIMEOUT,
                ),
                f"agent status {agent_id}",
            )
            for agent_id in agent_ids
        }
        states = {agent_id: record.get("status") for agent_id, record in records.items()}
        if states != last_states:
            print(f"AGENTS {json.dumps(states, ensure_ascii=False)}", flush=True)
            last_states = states
        if all(state in TERMINAL_AGENT_STATES for state in states.values()):
            failures = {
                agent_id: {
                    "status": record.get("status"),
                    "error": record.get("error"),
                    "message": record.get("message"),
                }
                for agent_id, record in records.items()
                if record.get("status") != "completed"
            }
            if failures:
                fail(f"agent execution failed: {json.dumps(failures, ensure_ascii=False)}")
            missing = [
                agent_id
                for agent_id, record in records.items()
                if not (record.get("output_files") or record.get("files_written"))
            ]
            if missing:
                fail(f"completed agents produced no files: {missing}")
            return
        time.sleep(8)
    fail(f"agent execution timed out: {last_states}")


def _qa_failure_code(state: str) -> int | None:
    if state in INFRASTRUCTURE_FAILURE_STATES or state == "interrupted":
        return EXIT_INFRASTRUCTURE_FAILURE
    if state in USER_ACTION_STATES:
        return EXIT_USER_ACTION_REQUIRED
    if state in PROJECT_FAILURE_STATES:
        return EXIT_PROJECT_FAILURE
    return None


def _pre_qa_repair_is_scheduled(status: dict[str, Any]) -> bool:
    scheduled_runs = (
        (status.get("action_required") or {}).get("scheduled_runs") or {}
    )
    return (
        str(status.get("status") or "") == "pre_qa_failed"
        and status.get("needs_manual") is False
        and isinstance(scheduled_runs, dict)
        and bool(scheduled_runs)
    )


def _qa_is_running(status: dict[str, Any]) -> bool:
    state = str(status.get("status") or "")
    if _pre_qa_repair_is_scheduled(status):
        return True
    if state == "rebuild_started":
        return bool(status.get("running"))
    return bool(status.get("running")) or state in RUNNING_QA_STATES or state.startswith(
        ("qc_running_round_", "rework_round_")
    )


def _phase_resume_mode(
    phase_status: str,
    auto_repair_status: dict[str, Any] | None = None,
) -> str:
    """Return normal/qa_only or fail closed for non-runnable phase states."""
    if auto_repair_status and (
        _qa_is_running(auto_repair_status)
        or str(auto_repair_status.get("status") or "") == "passed"
    ):
        return "qa_only"
    if phase_status in QA_ONLY_PHASE_STATES:
        return "qa_only"
    if phase_status in RUNNING_RECOVERY_PHASE_STATES:
        if auto_repair_status and _qa_is_running(auto_repair_status):
            return "qa_only"
        state = str((auto_repair_status or {}).get("status") or phase_status)
        fail(
            f"phase recovery is not running: status={state}",
            _qa_failure_code(state) or EXIT_PROJECT_FAILURE,
        )
    if phase_status == "model_failed":
        # Provider failures do not invalidate completed agent output. Resume the
        # same fail-closed QA round; run_phase_qa applies the bounded retry cap.
        return "qa_only"
    if phase_status in FAIL_CLOSED_PHASE_STATES:
        if (
            auto_repair_status
            and _pre_qa_repair_is_scheduled(auto_repair_status)
        ):
            return "qa_only"
        state = str((auto_repair_status or {}).get("status") or phase_status)
        fail(
            f"phase cannot resume automatically: status={state}",
            _qa_failure_code(state) or EXIT_USER_ACTION_REQUIRED,
        )
    return "normal"


def _final_qa_is_recoverable(status: dict[str, Any]) -> bool:
    state = str(status.get("status") or "")
    options = (status.get("action_required") or {}).get("options") or []
    return (
        state == "interrupted"
        or str(status.get("failed_reason") or "") in FINAL_QA_RECOVERABLE_FAILURES
        or ("retry_acceptance" in options and status.get("retryable") is not False)
    )


def _phase_qa_is_recoverable(status: dict[str, Any]) -> bool:
    """Allow the same durable QA cycle to resume after a service restart."""
    options = (status.get("action_required") or {}).get("options") or []
    return (
        str(status.get("status") or "") == "interrupted"
        and "retry_cycle" in options
        and status.get("retryable") is not False
    )


def _raise_for_qa_terminal(
    status: dict[str, Any], *, label: str, report_key: str = "issue_report"
) -> None:
    if _pre_qa_repair_is_scheduled(status):
        return
    state = str(status.get("status") or "")
    exit_code = _qa_failure_code(state)
    if exit_code is None:
        return
    report = status.get(report_key) or status.get("message") or status
    fail(
        f"{label} did not pass: status={state} "
        f"report={json.dumps(report, ensure_ascii=False)[:4000]}",
        exit_code,
    )


def run_phase_qa(session: requests.Session, project_id: str, phase_id: str) -> None:
    status_url = f"{BASE}/projects/{project_id}/phases/{phase_id}/auto-repair/status"
    repair_url = f"{BASE}/projects/{project_id}/phases/{phase_id}/auto-repair"
    provider_retries_remaining = 2
    interruption_retries_remaining = 2
    status = require(
        session.get(status_url, timeout=HTTP_TIMEOUT),
        f"phase QA status {phase_id}",
    )
    state = str(status.get("status") or "idle")
    if state == "passed":
        print(f"PHASE_QA phase={phase_id} status=passed reused=true", flush=True)
        return
    cycle_started = False
    if _phase_qa_is_recoverable(status) and interruption_retries_remaining:
        interruption_retries_remaining -= 1
        require(
            session.post(
                repair_url,
                params={"user_decision": "retry_cycle"},
                timeout=HTTP_TIMEOUT,
            ),
            f"resume interrupted phase QA {phase_id}",
        )
        cycle_started = True
    elif state == "model_failed" and provider_retries_remaining:
        provider_retries_remaining -= 1
        require(
            session.post(
                repair_url,
                params={"user_decision": "retry_cycle"},
                timeout=HTTP_TIMEOUT,
            ),
            f"retry phase QA provider {phase_id}",
        )
        cycle_started = True
    else:
        _raise_for_qa_terminal(status, label=f"phase QA {phase_id}")
    if not cycle_started and not _qa_is_running(status):
        require(
            session.post(repair_url, timeout=HTTP_TIMEOUT),
            f"start phase QA {phase_id}",
        )
    deadline = time.monotonic() + QA_TIMEOUT
    last_marker: tuple[Any, ...] | None = None
    while time.monotonic() < deadline:
        status = require(
            session.get(status_url, timeout=HTTP_TIMEOUT),
            f"phase QA status {phase_id}",
        )
        marker = (status.get("status"), status.get("round"), len(status.get("messages") or []))
        if marker != last_marker:
            print(f"PHASE_QA phase={phase_id} status={marker[0]} round={marker[1]}", flush=True)
            last_marker = marker
        state = status.get("status")
        if state == "passed":
            return
        if _phase_qa_is_recoverable(status) and interruption_retries_remaining:
            interruption_retries_remaining -= 1
            print(
                f"PHASE_QA phase={phase_id} interruption_retry="
                f"{2 - interruption_retries_remaining}/2",
                flush=True,
            )
            require(
                session.post(
                    repair_url,
                    params={"user_decision": "retry_cycle"},
                    timeout=HTTP_TIMEOUT,
                ),
                f"resume interrupted phase QA {phase_id}",
            )
            time.sleep(8)
            continue
        if state == "model_failed" and provider_retries_remaining:
            provider_retries_remaining -= 1
            print(
                f"PHASE_QA phase={phase_id} provider_retry="
                f"{2 - provider_retries_remaining}/2",
                flush=True,
            )
            require(
                session.post(
                    repair_url,
                    params={"user_decision": "retry_cycle"},
                    timeout=HTTP_TIMEOUT,
                ),
                f"retry phase QA provider {phase_id}",
            )
            time.sleep(8)
            continue
        _raise_for_qa_terminal(status, label=f"phase QA {phase_id}")
        time.sleep(8)
    fail(f"phase QA timed out: {phase_id}")


def run_final_qa(session: requests.Session, project_id: str) -> dict[str, Any]:
    status_url = f"{BASE}/projects/{project_id}/final-qa/status"
    status = require(
        session.get(status_url, timeout=HTTP_TIMEOUT),
        "final QA status",
    )
    state = str(status.get("status") or "not_started")
    if state == "passed" and status.get("all_passed") is not False:
        print("FINAL_QA status=passed reused=true", flush=True)
        return status
    recoverable = _final_qa_is_recoverable(status)
    if not recoverable:
        _raise_for_qa_terminal(status, label="final QA", report_key="qc_summary")
    if state == "not_started" or recoverable:
        require(
            session.post(f"{BASE}/projects/{project_id}/final-qa", timeout=HTTP_TIMEOUT),
            "start final QA",
        )
    deadline = time.monotonic() + QA_TIMEOUT
    last_marker: tuple[Any, ...] | None = None
    while time.monotonic() < deadline:
        status = require(
            session.get(status_url, timeout=HTTP_TIMEOUT),
            "final QA status",
        )
        marker = (
            status.get("status"),
            status.get("round"),
            status.get("completed_items"),
            status.get("total_items"),
        )
        if marker != last_marker:
            print(
                f"FINAL_QA status={marker[0]} round={marker[1]} "
                f"progress={marker[2]}/{marker[3]}",
                flush=True,
            )
            last_marker = marker
        if status.get("status") == "passed" and status.get("all_passed") is not False:
            return status
        _raise_for_qa_terminal(status, label="final QA", report_key="qc_summary")
        time.sleep(8)
    fail("final QA timed out")


def validate_runtime_acceptance_evidence(final_qa: dict[str, Any]) -> dict[str, Any]:
    """Require proof that the platform's isolated runtime gate actually passed."""
    evidence = final_qa.get("runtime_acceptance")
    if not isinstance(evidence, dict):
        fail("final QA passed without isolated runtime acceptance evidence")
    if evidence.get("enabled") is not True:
        fail("isolated runtime acceptance was not enabled")
    if evidence.get("passed") is not True:
        fail("isolated runtime acceptance did not pass")
    if str(evidence.get("status") or "").strip().lower() != "passed":
        fail(
            "isolated runtime acceptance has a non-passed status: "
            f"{evidence.get('status')!r}"
        )
    return dict(evidence)


def _append_log(log_path: Path | None, text: str) -> None:
    if log_path is None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")


def run_command(
    command: list[str],
    cwd: Path,
    timeout: int = 600,
    *,
    log_path: Path | None = None,
    records: list[dict[str, Any]] | None = None,
) -> subprocess.CompletedProcess[str]:
    rendered = " ".join(command)
    marker = f"RUN cwd={cwd} command={rendered}"
    print(marker, flush=True)
    _append_log(log_path, marker)
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.stdout:
        print(completed.stdout.rstrip(), flush=True)
        _append_log(log_path, completed.stdout)
    if completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr, flush=True)
        _append_log(log_path, completed.stderr)
    if records is not None:
        records.append({"cwd": str(cwd), "command": rendered, "returncode": completed.returncode})
    if completed.returncode:
        fail(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{completed.stdout[-4000:]}\nstderr:\n{completed.stderr[-4000:]}"
        )
    return completed


def npm_command(*args: str) -> list[str]:
    executable = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not executable:
        fail("npm executable is not available")
    if os.name == "nt":
        # Execute npm's JavaScript entrypoint directly.  Routing a spaced
        # npm.cmd path through cmd.exe is quoting-sensitive under Popen and can
        # truncate it at ``C:\\Program``.
        node = shutil.which("node.exe") or shutil.which("node")
        npm_cli = Path(executable).parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if node and npm_cli.is_file():
            return [node, str(npm_cli), *args]
        return ["cmd.exe", "/d", "/c", f'"{executable}" ' + subprocess.list2cmdline(list(args))]
    return [executable, *args]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def validate_runtime_environment(
    log_path: Path | None = None,
    records: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    if platform.system() != "Linux":
        fail(f"generated runtime acceptance requires Linux, received {platform.system()}")
    node = shutil.which("node")
    if not node:
        fail("node executable is not available")
    node_result = run_command([node, "--version"], Path.cwd(), log_path=log_path, records=records)
    node_version = node_result.stdout.strip()
    match = re.fullmatch(r"v?(\d+)(?:\..*)?", node_version)
    if not match or int(match.group(1)) != 20:
        fail(f"generated runtime acceptance requires Node 20, received {node_version!r}")
    npm_result = run_command(npm_command("--version"), Path.cwd(), log_path=log_path, records=records)
    return {
        "os": platform.platform(),
        "node": node_version,
        "npm": npm_result.stdout.strip(),
    }


def _portable_member_name(value: str) -> tuple[str, tuple[str, ...]]:
    raw = value
    if (
        not raw
        or "\x00" in raw
        or raw.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:", raw)
    ):
        fail(f"generated archive contains unsafe path: {raw}")
    normalized = raw.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        fail(f"generated archive contains unsafe path: {raw}")
    return "/".join(path.parts), path.parts


def validate_archive_members(members: list[Any]) -> None:
    """Reject archives that cannot be extracted consistently on all targets."""
    prefix_case: dict[str, str] = {}
    exact_members: set[str] = set()
    node_kinds: dict[str, str] = {}
    conflicts: list[tuple[str, str]] = []
    total_size = 0
    if len(members) > MAX_ARCHIVE_MEMBERS:
        fail(f"generated archive contains too many entries: {len(members)}")
    for member in members:
        raw = str(getattr(member, "filename", member))
        normalized, path_parts = _portable_member_name(raw.rstrip("/"))
        if normalized in exact_members:
            fail(f"generated archive contains duplicate member: {normalized}")
        exact_members.add(normalized)

        is_directory = raw.endswith(("/", "\\")) or bool(
            getattr(member, "is_dir", lambda: False)()
        )
        member_kind = "directory" if is_directory else "file"
        for index in range(1, len(path_parts) + 1):
            prefix = "/".join(path_parts[:index])
            folded = prefix.casefold()
            previous = prefix_case.get(folded)
            if previous is not None and previous != prefix:
                conflicts.append((previous, prefix))
            else:
                prefix_case[folded] = prefix
            expected_kind = member_kind if index == len(path_parts) else "directory"
            previous_kind = node_kinds.get(folded)
            if previous_kind is not None and previous_kind != expected_kind:
                fail(f"generated archive path is both file and directory: {prefix}")
            node_kinds[folded] = expected_kind

        file_size = int(getattr(member, "file_size", 0) or 0)
        compressed_size = int(getattr(member, "compress_size", 0) or 0)
        total_size += file_size
        if file_size > MAX_ARCHIVE_FILE_SIZE or total_size > MAX_ARCHIVE_TOTAL_SIZE:
            fail("generated archive exceeds safe extraction size limits")
        if file_size and compressed_size == 0:
            fail(f"generated archive entry has invalid compressed size: {raw}")
        if file_size > 1024 * 1024 and file_size / compressed_size > MAX_ARCHIVE_RATIO:
            fail(f"generated archive entry has suspicious compression ratio: {raw}")
        if hasattr(member, "external_attr"):
            mode = int(member.external_attr) >> 16
            if stat.S_ISLNK(mode):
                fail(f"generated archive contains symbolic link: {raw}")
            if int(getattr(member, "flag_bits", 0)) & 1:
                fail(f"generated archive contains encrypted member: {raw}")
    if conflicts:
        rendered = ", ".join(f"{left} <-> {right}" for left, right in conflicts[:20])
        fail(f"generated archive contains case-insensitive path conflicts: {rendered}")


def safe_extract_archive(bundle: zipfile.ZipFile, target: Path) -> None:
    members = bundle.infolist()
    validate_archive_members(members)
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    for member in members:
        normalized, _ = _portable_member_name(member.filename.rstrip("/"))
        destination = (root / normalized).resolve()
        if destination != root and root not in destination.parents:
            fail(f"generated archive member escapes extraction root: {member.filename}")
        if member.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with bundle.open(member) as source, destination.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)


def _json_response(response: requests.Response, step: str) -> Any:
    if not response.ok:
        fail(f"{step}: HTTP {response.status_code} {response.text[:500]}")
    try:
        return response.json()
    except ValueError:
        fail(f"{step}: expected JSON, received {response.text[:500]}")


def _deep_find(data: Any, keys: set[str]) -> Any:
    if isinstance(data, dict):
        for key, value in data.items():
            if key.casefold() in keys and value not in (None, ""):
                return value
        for value in data.values():
            found = _deep_find(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for value in data:
            found = _deep_find(value, keys)
            if found not in (None, ""):
                return found
    return None


def _extract_token(data: Any) -> str:
    token = _deep_find(data, {"token", "accesstoken", "access_token"})
    if not isinstance(token, str) or not token:
        fail("generated login response did not contain a token")
    return token


def _payload_contains(data: Any, value: Any) -> bool:
    needle = str(value)
    if isinstance(data, dict):
        return any(_payload_contains(item, value) for item in data.values())
    if isinstance(data, list):
        return any(_payload_contains(item, value) for item in data)
    return str(data) == needle


def _find_record(data: Any, marker: str) -> dict[str, Any] | None:
    if isinstance(data, dict):
        if _payload_contains(data, marker):
            return data
        for value in data.values():
            record = _find_record(value, marker)
            if record is not None:
                return record
    elif isinstance(data, list):
        for value in data:
            record = _find_record(value, marker)
            if record is not None:
                return record
    return None


def _extract_entity_id(data: Any, entity: str) -> Any:
    if isinstance(data, dict):
        nested = data.get(entity)
        if isinstance(nested, dict) and nested.get("id") not in (None, ""):
            return nested["id"]
        nested = data.get("data")
        if isinstance(nested, dict):
            result = _extract_entity_id(nested, entity)
            if result not in (None, ""):
                return result
        if data.get("id") not in (None, ""):
            return data["id"]
    return None


def _linked_asset_id(ticket: dict[str, Any]) -> Any:
    direct = _deep_find(ticket, {"assetid", "asset_id"})
    if direct not in (None, ""):
        return direct
    asset = ticket.get("asset")
    if isinstance(asset, dict):
        return asset.get("id")
    return None


def _start_application(root: Path, port: int, app_log: Path) -> tuple[subprocess.Popen[str], Any]:
    env = os.environ.copy()
    env.update({"PORT": str(port), "NODE_ENV": "test", "JWT_SECRET": "industrial-acceptance-secret"})
    handle = app_log.open("a", encoding="utf-8")
    handle.write(f"\nSTART port={port}\n")
    handle.flush()
    process = subprocess.Popen(
        npm_command("start"),
        cwd=root,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=os.name != "nt",
    )
    return process, handle


def _stop_application(process: subprocess.Popen[str], handle: Any) -> None:
    try:
        if process.poll() is None:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=5)
    finally:
        handle.close()


def _wait_for_application(process: subprocess.Popen[str], base: str, app_log: Path) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = app_log.read_text(encoding="utf-8", errors="replace") if app_log.exists() else ""
            fail(f"generated application exited during startup:\n{output[-4000:]}")
        try:
            health = requests.get(f"{base}/api/health", timeout=3)
            if health.status_code == 200:
                _json_response(health, "generated health check")
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    fail("generated application did not expose GET /api/health with HTTP 200 within 90 seconds")


def _login(base: str, email: str, password: str) -> dict[str, str]:
    response = requests.post(
        f"{base}/api/auth/login",
        json={"email": email, "password": password},
        timeout=10,
    )
    token = _extract_token(_json_response(response, "generated login"))
    return {"Authorization": f"Bearer {token}"}


def run_application_acceptance(root: Path, evidence_dir: Path) -> dict[str, Any]:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    app_log = evidence_dir / "application.log"
    stamp = f"{int(time.time())}-{os.getpid()}"
    admin_email = f"admin-{stamp}@test.local"
    second_email = f"employee-{stamp}@test.local"
    password = "Acceptance-Only-123!"
    asset_code = f"ASSET-{stamp}"
    ticket_title = f"Industrial acceptance ticket {stamp}"
    asset_id: Any = None
    checks: list[str] = []

    process, handle = _start_application(root, port, app_log)
    try:
        _wait_for_application(process, base, app_log)
        checks.append("health")
        frontend = requests.get(base, timeout=10)
        content_type = frontend.headers.get("content-type", "").lower()
        if frontend.status_code != 200 or "text/html" not in content_type or 'id="root"' not in frontend.text:
            fail(
                "generated application did not serve the built frontend on the backend port: "
                f"{frontend.status_code} {content_type} {frontend.text[:500]}"
            )
        checks.append("single_port_frontend")

        registered = requests.post(
            f"{base}/api/auth/register",
            json={
                "email": admin_email,
                "password": password,
                "name": "Acceptance Admin",
                "role": "admin",
            },
            timeout=10,
        )
        if registered.status_code not in {200, 201}:
            fail(f"generated register failed: {registered.status_code} {registered.text[:500]}")
        admin_headers = _login(base, admin_email, password)
        checks.extend(["register", "login"])

        asset_payload = {
            "assetCode": asset_code,
            "code": asset_code,
            "name": "Acceptance Laptop",
            "type": "computer",
        }
        asset = requests.post(f"{base}/api/assets", json=asset_payload, headers=admin_headers, timeout=10)
        if asset.status_code not in {200, 201}:
            fail(f"generated asset creation failed: {asset.status_code} {asset.text[:500]}")
        asset_data = _json_response(asset, "generated asset creation")
        asset_id = _extract_entity_id(asset_data, "asset")
        if asset_id in (None, ""):
            fail("generated asset creation response did not contain an asset id")
        assets = _json_response(
            requests.get(f"{base}/api/assets", headers=admin_headers, timeout=10),
            "generated asset listing",
        )
        if _find_record(assets, asset_code) is None:
            fail("generated asset listing did not contain the created asset")
        duplicate = requests.post(f"{base}/api/assets", json=asset_payload, headers=admin_headers, timeout=10)
        if duplicate.ok:
            fail("generated asset code uniqueness constraint accepted a duplicate")
        checks.extend(["asset_create_read", "asset_code_unique"])

        ticket = requests.post(
            f"{base}/api/tickets",
            json={
                "title": ticket_title,
                "description": "Verify complete generated project runtime",
                "priority": "high",
                "assetId": asset_id,
                "asset_id": asset_id,
            },
            headers=admin_headers,
            timeout=10,
        )
        if ticket.status_code not in {200, 201}:
            fail(f"generated ticket creation failed: {ticket.status_code} {ticket.text[:500]}")
        tickets = _json_response(
            requests.get(f"{base}/api/tickets", headers=admin_headers, timeout=10),
            "generated ticket listing",
        )
        ticket_record = _find_record(tickets, ticket_title)
        if ticket_record is None:
            fail("generated ticket listing did not contain the created ticket")
        linked_id = _linked_asset_id(ticket_record)
        if linked_id is None or str(linked_id) != str(asset_id):
            fail(f"generated ticket did not preserve its asset association: {linked_id!r} != {asset_id!r}")
        checks.extend(["ticket_create_read", "ticket_asset_link"])

        second = requests.post(
            f"{base}/api/auth/register",
            json={
                "email": second_email,
                "password": password,
                "name": "Acceptance Employee",
                "role": "employee",
            },
            timeout=10,
        )
        if second.status_code not in {200, 201}:
            fail(f"generated second-user registration failed: {second.status_code} {second.text[:500]}")
        second_headers = _login(base, second_email, password)
        second_assets = _json_response(
            requests.get(f"{base}/api/assets", headers=second_headers, timeout=10),
            "generated second-user asset listing",
        )
        second_tickets = _json_response(
            requests.get(f"{base}/api/tickets", headers=second_headers, timeout=10),
            "generated second-user ticket listing",
        )
        if _payload_contains(second_assets, asset_code) or _payload_contains(second_tickets, ticket_title):
            fail("generated application leaked one user's records to another user")
        checks.append("per_user_isolation")
    finally:
        _stop_application(process, handle)

    process, handle = _start_application(root, port, app_log)
    try:
        _wait_for_application(process, base, app_log)
        admin_headers = _login(base, admin_email, password)
        assets = _json_response(
            requests.get(f"{base}/api/assets", headers=admin_headers, timeout=10),
            "persisted asset listing",
        )
        tickets = _json_response(
            requests.get(f"{base}/api/tickets", headers=admin_headers, timeout=10),
            "persisted ticket listing",
        )
        ticket_record = _find_record(tickets, ticket_title)
        if _find_record(assets, asset_code) is None or ticket_record is None:
            fail("generated application lost SQLite data after restart")
        if str(_linked_asset_id(ticket_record)) != str(asset_id):
            fail("generated application lost the ticket/asset association after restart")
        time.sleep(2)
        if process.poll() is not None:
            fail("generated application exited after persistence smoke tests")
        checks.append("sqlite_restart_persistence")
    finally:
        _stop_application(process, handle)

    log_text = app_log.read_text(encoding="utf-8", errors="replace")
    if re.search(r"uncaught\s+(?:exception|error)|unhandled\s+(?:promise\s+)?rejection", log_text, re.I):
        fail("generated application log contains an uncaught exception or unhandled rejection")
    checks.append("no_uncaught_exception")
    return {"checks": checks, "application_log": str(app_log)}


def validate_backend_tests(root: Path) -> None:
    candidates = [
        path for path in (root / "backend").rglob("*")
        if path.is_file() and (
            re.search(r"(?:^|[._-])test(?:[._-]|$)", path.name, re.I)
            or "tests" in {part.casefold() for part in path.parts}
        ) and path.suffix.lower() in {".js", ".cjs", ".mjs", ".ts"}
    ]
    if not candidates:
        fail("generated backend contains no executable JavaScript/TypeScript tests")
    if not any(
        re.search(r"(?:require\s*\(|from\s+)[^\n;]*[\"'][^\"']*src/", path.read_text(encoding="utf-8", errors="replace"))
        for path in candidates
    ):
        fail("generated backend tests do not import production code from backend/src")


def validate_generated_project_structure(root: Path) -> dict[str, Any]:
    """Validate the downloaded deliverable without executing generated code."""
    root = root.resolve()
    required = ["README.md", "package.json", "backend/package.json", "frontend/package.json"]
    missing = [path for path in required if not (root / path).is_file()]
    if missing:
        fail(f"generated archive missing required delivery files: {missing}")

    required_scripts = {
        ".": ("start", "test", "build"),
        "backend": ("test",),
        "frontend": ("build",),
    }
    verified_scripts: dict[str, list[str]] = {}
    for relative, script_names in required_scripts.items():
        package_root = root if relative == "." else root / relative
        package = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
        scripts = package.get("scripts") or {}
        missing_scripts = [name for name in script_names if not scripts.get(name)]
        if missing_scripts:
            fail(
                f"{relative} package.json is missing required scripts: "
                f"{missing_scripts}"
            )
        verified_scripts[relative] = list(script_names)

    validate_backend_tests(root)
    return {
        "required_files": required,
        "required_scripts": verified_scripts,
        "backend_tests": "imports_production_backend_src",
    }


def validate_generated_project_with_remote_runtime(
    root: Path, runtime_acceptance: dict[str, Any]
) -> dict[str, Any]:
    """Use platform-controlled Render evidence instead of running local Node/Docker."""
    structure = validate_generated_project_structure(root)
    evidence = validate_runtime_acceptance_evidence(
        {"runtime_acceptance": runtime_acceptance}
    )
    return {
        "status": "passed",
        "mode": "isolated_render_runtime",
        "static": structure,
        "runtime_acceptance": evidence,
    }


def validate_generated_project(root: Path, evidence_dir: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    evidence_dir = (evidence_dir or root / ".acceptance-evidence").resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    command_log = evidence_dir / "commands.log"
    command_records: list[dict[str, Any]] = []
    validate_generated_project_structure(root)
    package_roots = [root, root / "backend", root / "frontend"]
    environment = validate_runtime_environment(command_log, command_records)
    for package_root in package_roots:
        run_command(
            npm_command("install", "--no-audit", "--no-fund"),
            package_root,
            timeout=1200,
            log_path=command_log,
            records=command_records,
        )
    run_command(npm_command("test"), root, timeout=1200, log_path=command_log, records=command_records)
    run_command(
        npm_command("test"),
        root / "backend",
        timeout=1200,
        log_path=command_log,
        records=command_records,
    )
    run_command(npm_command("run", "build"), root, timeout=1200, log_path=command_log, records=command_records)
    run_command(
        npm_command("run", "build"),
        root / "frontend",
        timeout=1200,
        log_path=command_log,
        records=command_records,
    )

    sqlite_probe = """
const fs = require('fs');
const Database = require('better-sqlite3');
const path = '.acceptance-native-probe.sqlite';
try {
  const db = new Database(path);
  db.exec('CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT NOT NULL)');
  db.prepare('INSERT INTO probe(value) VALUES (?)').run('ok');
  const row = db.prepare('SELECT value FROM probe WHERE id = 1').get();
  db.close();
  if (!row || row.value !== 'ok') process.exit(3);
} finally {
  if (fs.existsSync(path)) fs.unlinkSync(path);
}
""".strip()
    run_command(
        [shutil.which("node") or "node", "-e", sqlite_probe],
        root / "backend",
        timeout=120,
        log_path=command_log,
        records=command_records,
    )
    api = run_application_acceptance(root, evidence_dir)
    summary = {
        "status": "passed",
        "environment": environment,
        "commands": command_records,
        "api": api,
    }
    (evidence_dir / "runtime-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def validate_generated_project_in_docker(root: Path, evidence_dir: Path) -> dict[str, Any]:
    docker = shutil.which("docker")
    if not docker:
        fail("Docker is required for default Linux/Node 20 generated-project acceptance")
    repository = Path(__file__).resolve().parents[1]
    docker_log = evidence_dir / "docker-runtime.log"
    inner = (
        "set -euo pipefail; "
        "test \"$(node -p 'process.versions.node.split(\".\")[0]')\" = 20; "
        "apt-get update >/dev/null; "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
        "python3 python3-requests make g++ ca-certificates >/dev/null; "
        "rm -rf /tmp/generated-project; mkdir -p /tmp/generated-project; "
        "cp -a /project-source/. /tmp/generated-project/; "
        "python3 /runner/scripts/industrial_acceptance.py "
        "--validate-generated /tmp/generated-project --evidence-dir /evidence"
    )
    command = [
        docker,
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "-e",
        "METIS_ACCEPTANCE_CONTAINER=1",
        "-v",
        f"{repository}:/runner:ro",
        "-v",
        f"{root.resolve()}:/project-source:ro",
        "-v",
        f"{evidence_dir.resolve()}:/evidence",
        DOCKER_IMAGE,
        "bash",
        "-lc",
        inner,
    ]
    run_command(command, repository, timeout=7200, log_path=docker_log)
    runtime_summary = evidence_dir / "runtime-summary.json"
    if not runtime_summary.is_file():
        fail("Docker acceptance completed without runtime-summary.json")
    return json.loads(runtime_summary.read_text(encoding="utf-8"))


PROJECT_REQUIREMENTS = """
Build a production-ready team ticket and asset management web application.
严格划分为四个阶段。
The delivery must contain exactly four implementation phases; preserve
their names, responsibilities and order:

阶段一：Foundation and runtime contract
角色集合：DevOps Engineer, Backend Developer, Frontend Developer
任务一：Create repository manifests and runtime delivery with root npm start plus npm test plus npm run build scripts so root start serves the built frontend and backend on one PORT | 角色：DevOps Engineer | 验收：package.json and README.md and .env.example and Dockerfile have registry-backed byte digests
任务二：Create authentication and backend foundation | 角色：Backend Developer | 文件：backend/package.json | 验收：backend/package.json has a registry-backed byte digest
任务三：Create the frontend shell | 角色：Frontend Developer | 文件：frontend/package.json | 验收：frontend/package.json has a registry-backed byte digest
验收标准：package.json and README.md and .env.example and Dockerfile and backend/package.json and frontend/package.json have registry-backed byte digests

阶段二：Backend domain and authorization
角色集合：Backend Developer
任务一：Implement users, JWT authentication, assets, tickets, comments, dashboard, validation and authorization including uniqueness and per-user isolation | 角色：Backend Developer | 文件：backend/src/database.js, backend/src/auth.js, backend/src/server.js, backend/src/routes/assets.js, backend/src/routes/tickets.js, backend/src/routes/comments.js, backend/src/routes/dashboard.js | 验收：npm test in backend exits 0
验收标准：npm test in backend exits 0

阶段三：Frontend workflows and integration
角色集合：Frontend Developer, Backend Developer
任务一：Implement authentication plus authenticated POST and GET asset and ticket workflows plus dashboard workflows against the real API with loading plus error plus empty states and no mock-only workflow | 角色：Frontend Developer | 文件：frontend/src/main.tsx, frontend/src/App.tsx, frontend/src/api.ts, frontend/src/pages/Dashboard.tsx, frontend/src/pages/Assets.tsx, frontend/src/pages/Tickets.tsx | 验收：npm run build in frontend exits 0
任务二：Implement and stabilize the frontend API contract without rewriting frontend-owned files | 角色：Backend Developer | 文件：backend/src/frontend-contract.js | 验收：backend/src/frontend-contract.js has a registry-backed byte digest
验收标准：npm run build in frontend exits 0

阶段四：Release hardening and deployment
角色集合：DevOps Engineer, Backend Developer, Frontend Developer
任务一：Create release orchestration without overwriting earlier phase files | 角色：DevOps Engineer | 文件：deploy/start.js, .dockerignore, docker-compose.yml | 验收：deploy/start.js and .dockerignore and docker-compose.yml have registry-backed byte digests
任务二：Implement backend runtime acceptance support without overwriting backend implementation files | 角色：Backend Developer | 文件：backend/src/runtime-acceptance.js | 验收：npm test in backend exits 0
任务三：Implement the production frontend release acceptance entry without overwriting frontend implementation files | 角色：Frontend Developer | 文件：frontend/src/release-acceptance.tsx | 验收：npm run build in frontend exits 0
验收标准：npm test at repository root exits 0; npm run build at repository root exits 0

Produce a complete, integrated runnable project, not samples or design
documents. Use Node.js 20, Express, SQLite, React, TypeScript and Vite.
Required repository contract:
The root package.json defines npm start plus npm test plus npm run build.
backend/package.json and frontend/package.json are required.
README.md plus .env.example plus Dockerfile are required.
npm install must work in the root plus backend plus frontend package roots.
The root start command serves the built frontend and backend on one PORT.
Implement GET /api/health.
Implement POST /api/auth/register.
Implement POST /api/auth/login returning a token.
Implement authenticated POST and GET /api/assets.
Implement authenticated POST and GET /api/tickets.
Asset code must be unique and tickets can link an asset.
Implement roles admin plus employee plus technician with JWT authentication.
Enforce strict per-user data isolation.
Implement ticket status transitions plus comments plus asset deletion protection.
Implement filters plus search plus pagination plus dashboard statistics.
Implement a responsive frontend with loading plus error plus empty states.
Automated backend tests must pass.
The frontend production build must pass.
No TODO or mock-only endpoint or omitted file or pseudo-code or external database
or manual setup step is allowed.
""".strip()


def _phase_agent_ids(phase: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for agent in (phase.get("agent_details") or phase.get("agents") or []):
        if isinstance(agent, dict) and agent.get("verification_only") is True:
            continue
        agent_id = agent.get("id") if isinstance(agent, dict) else agent
        if agent_id and str(agent_id) not in ids:
            ids.append(str(agent_id))
    return ids


def _get_or_create_project(
    session: requests.Session, project_id: str | None
) -> tuple[str, dict[str, Any], bool]:
    if project_id:
        if not re.fullmatch(r"proj-[A-Za-z0-9_-]+", project_id):
            fail(f"unsafe project id: {project_id!r}", EXIT_SCRIPT_ERROR)
        project = require(
            session.get(f"{BASE}/projects/{project_id}", timeout=HTTP_TIMEOUT),
            "load existing project",
        )
        print(f"PROJECT {project_id} resumed=true", flush=True)
        return project_id, project, False

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    project = require(
        session.post(
            f"{BASE}/projects",
            json={
                "name": f"Industrial-Acceptance-{stamp}",
                "description": PROJECT_REQUIREMENTS,
            },
            timeout=HTTP_TIMEOUT,
        ),
        "create project",
    )
    project_id = project["project_id"]
    if not re.fullmatch(r"proj-[A-Za-z0-9_-]+", str(project_id)):
        fail(f"platform returned an unsafe project id: {project_id!r}", EXIT_SCRIPT_ERROR)
    print(f"PROJECT {project_id}", flush=True)
    return str(project_id), project, True


def _deployment_evidence(runtime_acceptance: dict[str, Any]) -> dict[str, Any]:
    keys = ("deploy_id", "commit_sha", "service_url", "status", "passed")
    return {key: runtime_acceptance.get(key) for key in keys if key in runtime_acceptance}


def signoff_and_verify(
    session: requests.Session,
    project_id: str,
    *,
    already_completed: bool = False,
) -> dict[str, Any]:
    """Sign off a verified project and prove the persisted terminal state."""
    if not already_completed:
        require(
            session.post(
                f"{BASE}/projects/{project_id}/signoff",
                timeout=HTTP_TIMEOUT,
            ),
            "sign off project",
        )
    project = require(
        session.get(f"{BASE}/projects/{project_id}", timeout=HTTP_TIMEOUT),
        "verify project signoff",
    )
    if str(project.get("status") or "").strip().lower() != "completed":
        fail(
            "project signoff did not persist status=completed: "
            f"{project.get('status')!r}"
        )
    print(
        f"PASS project signoff reused={str(already_completed).lower()}",
        flush=True,
    )
    return {
        "status": "completed",
        "reused": already_completed,
    }


def _failure_status(exit_code: int) -> str:
    return {
        EXIT_PROJECT_FAILURE: "failed",
        EXIT_INFRASTRUCTURE_FAILURE: "infrastructure_failed",
        EXIT_USER_ACTION_REQUIRED: "awaiting_user_action",
        EXIT_SCRIPT_ERROR: "script_error",
    }.get(exit_code, "script_error")


def main(project_id: str | None = None, *, resume: bool = False) -> dict[str, Any]:
    del resume  # Project-aware execution is resumable whenever --project-id is supplied.
    try:
        login_name = os.environ["METIS_E2E_LOGIN"]
        login_password = os.environ["METIS_E2E_PASSWORD"]
    except KeyError as exc:
        fail(f"missing required environment variable: {exc.args[0]}", EXIT_SCRIPT_ERROR)
    if project_id and not re.fullmatch(r"proj-[A-Za-z0-9_-]+", project_id):
        fail(f"unsafe project id: {project_id!r}", EXIT_SCRIPT_ERROR)
    summary = AcceptanceSummary(project_id) if project_id else None
    if summary is not None:
        summary.update(current_step="login", resumed=True)
    session = ResilientSession(login_name, login_password)
    try:
        session.authenticate()
        print("PASS platform login", flush=True)
        project_id, project, created = _get_or_create_project(session, project_id)
    except AcceptanceFailure as exc:
        if summary is not None:
            summary.update(
                status=_failure_status(exc.exit_code),
                exit_code=exc.exit_code,
                error=str(exc)[:4000],
                failed_at=time.time(),
            )
        raise
    summary = summary or AcceptanceSummary(project_id)
    summary.update(current_step="planning", resumed=not created)
    try:
        phase_data = require(
            session.get(f"{BASE}/projects/{project_id}/phases", timeout=HTTP_TIMEOUT),
            "list phases",
        )
        phases = phase_data.get("phases") or []
        if not phases:
            requirements_state = require(
                session.get(
                    f"{BASE}/projects/{project_id}/pm-team/requirements/revisions",
                    timeout=HTTP_TIMEOUT,
                ),
                "load canonical requirements metadata",
            )
            synthesized = require(
                session.post(
                    f"{BASE}/projects/{project_id}/pm-team/synthesize",
                    json={
                        "requirements_revision": requirements_state.get(
                            "requirements_revision"
                        ),
                        "requirements_digest": requirements_state.get(
                            "requirements_digest"
                        ),
                        "fast_mode": True,
                    },
                    timeout=600,
                ),
                "synthesize plan",
            )
            if synthesized.get("success") is not True:
                fail(
                    "plan synthesis did not produce a confirmable draft: "
                    f"{synthesized.get('status')!r}"
                )
            confirmed = require(
                session.post(
                    f"{BASE}/projects/{project_id}/pm-team/confirm-plan",
                    json={
                        "modifications": "",
                        "requirements_revision": synthesized.get(
                            "requirements_revision"
                        ),
                        "requirements_digest": synthesized.get(
                            "requirements_digest"
                        ),
                    },
                    timeout=600,
                ),
                "confirm plan",
            )
            if confirmed.get("success") is not True:
                fail(
                    "plan confirmation did not lock a launchable contract: "
                    f"{confirmed.get('status')!r}"
                )
            phase_data = require(
                session.get(f"{BASE}/projects/{project_id}/phases", timeout=HTTP_TIMEOUT),
                "list phases",
            )
            phases = phase_data.get("phases") or []
        if len(phases) != 4:
            fail(f"planning contract violated: expected 4 phases, got {len(phases)}")

        for phase in phases:
            phase_id = str(phase["phase_id"])
            summary.update(current_step=f"phase:{phase_id}")
            phase_status = str(phase.get("status") or "pending")
            if phase.get("user_confirmed"):
                print(f"PASS phase {phase_id} reused=true", flush=True)
                continue

            if phase_status == "completed":
                require(
                    session.post(
                        f"{BASE}/projects/{project_id}/phases/{phase_id}/confirm-complete",
                        timeout=HTTP_TIMEOUT,
                    ),
                    f"confirm phase {phase_id}",
                )
                print(f"PASS phase {phase_id} reused=true", flush=True)
                continue

            agent_ids = _phase_agent_ids(phase)
            auto_repair_status = None
            if phase_status not in {"pending", "planned", "ready", "completed"}:
                auto_repair_status = require(
                    session.get(
                        f"{BASE}/projects/{project_id}/phases/{phase_id}/auto-repair/status",
                        timeout=HTTP_TIMEOUT,
                    ),
                    f"phase QA status {phase_id}",
                )
            resume_mode = _phase_resume_mode(phase_status, auto_repair_status)
            if phase_status in {"pending", "planned", "ready"}:
                started = require(
                    session.post(
                        f"{BASE}/projects/{project_id}/phases/{phase_id}/start",
                        timeout=HTTP_TIMEOUT,
                    ),
                    f"start phase {phase_id}",
                )
                agent_ids = [
                    str(agent["id"])
                    for agent in started.get("created_agents") or []
                    if agent.get("id")
                ]
                if not agent_ids:
                    fail(f"phase {phase_id} created no agents")
            elif phase_status in {"active", "in_progress", "running"} and not agent_ids:
                current_phase = require(
                    session.get(
                        f"{BASE}/projects/{project_id}/phases/{phase_id}",
                        timeout=HTTP_TIMEOUT,
                    ),
                    f"load phase {phase_id}",
                )
                agent_ids = _phase_agent_ids(current_phase)
                if not agent_ids:
                    fail(
                        f"in-progress phase {phase_id} has no resumable agents"
                    )
            elif resume_mode != "qa_only" and phase_status not in RESUMABLE_PHASE_STATES:
                fail(
                    f"phase {phase_id} cannot be resumed from status {phase_status!r}",
                    EXIT_USER_ACTION_REQUIRED,
                )

            if agent_ids and resume_mode != "qa_only":
                poll_agents(session, project_id, agent_ids)
            run_phase_qa(session, project_id, phase_id)
            require(
                session.post(
                    f"{BASE}/projects/{project_id}/phases/{phase_id}/confirm-complete",
                    timeout=HTTP_TIMEOUT,
                ),
                f"confirm phase {phase_id}",
            )
            print(f"PASS phase {phase_id}", flush=True)

        summary.update(current_step="final_qa")
        final_qa = run_final_qa(session, project_id)
        runtime_acceptance = validate_runtime_acceptance_evidence(final_qa)
        summary.update(
            runtime_acceptance=runtime_acceptance,
            deployment=_deployment_evidence(runtime_acceptance),
            current_step="download_archive",
        )
        file_data = require(
            session.get(f"{BASE}/projects/{project_id}/files", timeout=HTTP_TIMEOUT),
            "list generated files",
        )
        files = flatten_tree(file_data.get("tree") or [])
        if len(files) < 15:
            fail(f"deliverable is incomplete: only {len(files)} files")
        archive = session.get(
            f"{BASE}/projects/{project_id}/archive/download",
            timeout=HTTP_TIMEOUT,
        )
        if not archive.ok or not archive.content.startswith(b"PK"):
            fail(f"archive download failed: HTTP {archive.status_code} {archive.text[:500]}")

        archive_path = summary.evidence_dir / "generated-project.zip"
        archive_path.write_bytes(archive.content)
        archive_sha256 = hashlib.sha256(archive.content).hexdigest()
        target = summary.evidence_dir / "project"
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
            validate_archive_members(bundle.infolist())
            corrupt_member = bundle.testzip()
            if corrupt_member:
                fail(f"generated archive failed CRC validation: {corrupt_member}")
            safe_extract_archive(bundle, target)

        summary.update(
            current_step="runtime_validation",
            archive={
                "path": str(archive_path),
                "sha256": archive_sha256,
                "bytes": len(archive.content),
                "reported_files": len(files),
            },
        )
        use_docker = os.getenv("METIS_ACCEPTANCE_USE_DOCKER", "1").strip().lower() not in {
            "0", "false", "no"
        }
        if use_docker:
            runtime = validate_generated_project_in_docker(target, summary.evidence_dir)
        else:
            runtime = validate_generated_project_with_remote_runtime(
                target, runtime_acceptance
            )
        summary.update(current_step="signoff", runtime=runtime)
        signoff = signoff_and_verify(
            session,
            project_id,
            already_completed=(
                str(project.get("status") or "").strip().lower() == "completed"
            ),
        )
        summary.update(
            status="passed",
            current_step="completed",
            exit_code=0,
            signoff=signoff,
            completed_at=time.time(),
        )
        print(
            f"INDUSTRIAL_ACCEPTANCE_PASSED project={project_id} files={len(files)} "
            f"artifact={target} evidence={summary.evidence_dir} sha256={archive_sha256}",
            flush=True,
        )
        return summary.data
    except AcceptanceFailure as exc:
        summary.update(
            status=_failure_status(exc.exit_code),
            exit_code=exc.exit_code,
            error=str(exc)[:4000],
            failed_at=time.time(),
        )
        raise
    except Exception as exc:
        summary.update(
            status="script_error",
            exit_code=EXIT_SCRIPT_ERROR,
            error=str(exc)[:4000],
            failed_at=time.time(),
        )
        raise AcceptanceFailure(str(exc), EXIT_SCRIPT_ERROR) from exc


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-generated", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--project-id", help="resume an existing platform project")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="continue the project supplied by --project-id without recreating it",
    )
    args = parser.parse_args()
    if args.validate_generated:
        if os.getenv("METIS_ACCEPTANCE_CONTAINER") != "1":
            fail("--validate-generated is reserved for the isolated acceptance container")
        if args.evidence_dir is None:
            fail("--evidence-dir is required with --validate-generated")
        validate_generated_project(args.validate_generated, args.evidence_dir)
        return
    if args.resume and not args.project_id:
        fail("--resume requires --project-id", EXIT_SCRIPT_ERROR)
    main(args.project_id, resume=args.resume)


if __name__ == "__main__":
    try:
        cli()
    except AcceptanceFailure as exc:
        print(f"INDUSTRIAL_ACCEPTANCE_FAILED {exc}", file=sys.stderr, flush=True)
        raise SystemExit(exc.exit_code)
    except Exception as exc:
        print(f"INDUSTRIAL_ACCEPTANCE_FAILED {exc}", file=sys.stderr, flush=True)
        raise SystemExit(EXIT_SCRIPT_ERROR)
