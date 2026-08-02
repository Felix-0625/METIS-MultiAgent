from pathlib import Path
from types import SimpleNamespace
import hashlib
import io
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from core.pre_qa_verifier import (
    ApiObservation,
    ApiProbe,
    CommandGate,
    CommandObservation,
    FAILURE_INFRASTRUCTURE,
    FAILURE_MODEL,
    FAILURE_PRE_QA,
    LocalCommandRunner,
    PreQAVerifier,
    _BoundedCommandOutput,
    _command_failure_issues,
    _run_bounded_command,
    _terminate_command_process_tree,
    api_probes_from_contract,
    classify_failure,
    default_api_probes,
    node_fullstack_command_gates,
    run_api_probes,
    scope_digest,
    verify_contract_artifacts,
    verify_jwt_invariants,
)


AUTH_SOURCE = """
const jwt = require('jsonwebtoken');
const JWT_SECRET = process.env.JWT_SECRET;
if (!JWT_SECRET) { throw new Error('JWT_SECRET is required'); }
module.exports = { sign: value => jwt.sign(value, JWT_SECRET) };
"""


def _write(path: Path, content: str = "ok") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _snapshot(tmp_path: Path):
    files = {
        "package.json": '{"scripts":{"test":"x","build":"x"}}',
        "backend/package.json": '{"scripts":{"test":"x"}}',
        "frontend/package.json": '{"scripts":{"build":"x"}}',
        "README.md": "# Application\n",
        ".env.example": "JWT_SECRET=\n",
        "Dockerfile": "FROM node:20\n",
        "backend/src/auth.js": AUTH_SOURCE,
    }
    for path, content in files.items():
        _write(tmp_path / path, content)
    required = [
        {"path": path, "required": True, "owner_type": "backend" if path.startswith("backend/") else "devops"}
        for path in files
    ]
    registry = {
        path: {"agent_id": "backend" if path.startswith("backend/") else "devops"}
        for path in files
    }
    agents = [
        {"id": "backend", "expert_type": "backend", "allowed_path_prefixes": ["backend/"]},
        {"id": "devops", "expert_type": "devops", "allowed_path_prefixes": [
            "package.json", "frontend/package.json", "README.md", ".env.example", "Dockerfile"
        ]},
    ]
    return {"required_files": required}, registry, agents


def test_contract_artifact_gate_accepts_complete_owned_manifest(tmp_path):
    contract, registry, agents = _snapshot(tmp_path)

    assert verify_contract_artifacts(tmp_path, contract, registry, agents) == ()


def test_contract_artifact_gate_accepts_empty_package_marker(tmp_path):
    contract, registry, agents = _snapshot(tmp_path)
    _write(tmp_path / "backend" / "pkg" / "__init__.py", "")
    contract["required_files"].append({
        "path": "backend/pkg/__init__.py",
        "required": True,
        "owner_type": "backend",
    })
    registry["backend/pkg/__init__.py"] = {"agent_id": "backend"}

    assert verify_contract_artifacts(tmp_path, contract, registry, agents) == ()


def test_contract_artifact_gate_does_not_treat_todo_domain_text_as_placeholder(
    tmp_path,
):
    contract, registry, agents = _snapshot(tmp_path)
    (tmp_path / "README.md").write_text(
        "# TODO API\nThe TODO API returns `TODO not found` for an unknown item.\n",
        encoding="utf-8",
    )

    assert verify_contract_artifacts(tmp_path, contract, registry, agents) == ()


def test_contract_artifact_gate_does_not_treat_css_todo_selector_as_placeholder(
    tmp_path,
):
    contract, registry, agents = _snapshot(tmp_path)
    (tmp_path / "styles.css").write_text(
        "#todo-form { display: flex; }\n#todo-input { width: 100%; }\n",
        encoding="utf-8",
    )
    contract["required_files"].append(
        {"path": "styles.css", "required": True, "owner_type": "devops"}
    )
    registry["styles.css"] = {"agent_id": "devops"}
    agents[1]["allowed_path_prefixes"].append("styles.css")

    assert verify_contract_artifacts(tmp_path, contract, registry, agents) == ()


def test_fullstack_agent_satisfies_backend_and_integration_owner_types(tmp_path):
    contract, registry, _agents = _snapshot(tmp_path)
    for path in registry:
        registry[path] = {"agent_id": "fullstack"}
    agents = [{
        "id": "fullstack",
        "expert_type": "fullstack_engineer",
        "allowed_path_prefixes": list(registry),
    }]

    assert verify_contract_artifacts(tmp_path, contract, registry, agents) == ()


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing", "missing_required_file"),
        ("empty", "empty_required_file"),
        ("todo", "forbidden_placeholder"),
        ("pseudocode", "forbidden_pseudocode"),
        ("log", "forbidden_execution_log"),
        ("unregistered", "unregistered_required_file"),
        ("scope", "owner_scope_violation"),
    ],
)
def test_contract_artifact_gate_rejects_invalid_delivery(tmp_path, mutation, expected_code):
    contract, registry, agents = _snapshot(tmp_path)
    target = tmp_path / "README.md"
    if mutation == "missing":
        target.unlink()
    elif mutation == "empty":
        target.write_text("  ", encoding="utf-8")
    elif mutation == "todo":
        target.write_text("TODO ship this", encoding="utf-8")
    elif mutation == "pseudocode":
        target.write_text("not implemented", encoding="utf-8")
    elif mutation == "log":
        target.write_text("npm ERR! build failed", encoding="utf-8")
    elif mutation == "unregistered":
        registry.pop("README.md")
    elif mutation == "scope":
        agents[1]["allowed_path_prefixes"].remove("README.md")

    codes = {issue.code for issue in verify_contract_artifacts(tmp_path, contract, registry, agents)}

    assert expected_code in codes


def test_contract_artifact_gate_rejects_duplicate_and_wrong_owners(tmp_path):
    contract, registry, agents = _snapshot(tmp_path)
    contract["required_files"].append({
        "path": "README.md", "owner_id": "backend", "owner_type": "backend"
    })
    registry["README.md"] = [
        {"agent_id": "devops"},
        {"agent_id": "backend"},
    ]

    codes = {issue.code for issue in verify_contract_artifacts(tmp_path, contract, registry, agents)}

    assert "multiple_owner_types" in codes
    assert "non_unique_registry_owner" in codes


def test_contract_artifact_gate_accepts_fail_closed_preservation_owner(tmp_path):
    target = tmp_path / "README.md"
    _write(target, "# Preserved application\n")
    digest = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
    contract = {
        "required_files": [{
            "path": "README.md",
            "required": True,
            "owner_type": "devops",
        }],
    }
    registry = {"README.md": {"agent_id": "preserve-verifier"}}
    agent = {
        "id": "preserve-verifier",
        "expert_type": "devops",
        "verification_only": True,
        "verification_paths": ["README.md"],
        "allowed_path_prefixes": [],
        "preservation_receipts": {
            "README.md": {
                "status": "succeeded",
                "start_run_id": "preserve:phase-1:readme",
                "completion_run_id": "preserve:phase-1:readme",
                "baseline_digest": digest,
                "artifact_digest": digest,
                "evidence": [{
                    "producer": "metis.runner",
                    "status": "passed",
                    "payload": {
                        "validator": "phase.preserve_baseline_digest",
                        "artifact_digest": digest,
                        "agent_id": "preserve-verifier",
                    },
                }],
            },
        },
    }

    assert verify_contract_artifacts(
        tmp_path, contract, registry, [agent],
    ) == ()

    agent["preservation_receipts"]["README.md"]["artifact_digest"] = (
        "sha256:" + ("0" * 64)
    )
    codes = {
        issue.code
        for issue in verify_contract_artifacts(
            tmp_path, contract, registry, [agent],
        )
    }
    assert "owner_scope_violation" in codes


def test_jwt_gate_accepts_env_secret_and_fail_closed_startup(tmp_path):
    _write(tmp_path / "backend/auth.js", AUTH_SOURCE)

    assert verify_jwt_invariants(tmp_path, ["backend/auth.js"]) == ()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "const JWT_SECRET = process.env.JWT_SECRET || 'secret'; jwt.sign(x, JWT_SECRET);",
            "jwt_default_secret",
        ),
        ("jwt.sign(x, 'hard-coded-secret');", "jwt_hardcoded_secret"),
        (
            "const JWT_SECRET = process.env.JWT_SECRET; jwt.sign(x, JWT_SECRET);",
            "jwt_not_fail_closed",
        ),
        (
            "const JWT_SECRET = process.env.JWT_SECRET; if (!JWT_SECRET) throw Error(); console.log(token); jwt.sign(x, JWT_SECRET);",
            "sensitive_value_logged",
        ),
    ],
)
def test_jwt_gate_rejects_default_hardcoded_or_non_fail_closed_secret(tmp_path, source, expected):
    _write(tmp_path / "backend/auth.js", source)

    codes = {issue.code for issue in verify_jwt_invariants(tmp_path, ["backend/auth.js"])}

    assert expected in codes


def test_jwt_gate_requires_implementation_when_contract_requires_jwt(tmp_path):
    _write(tmp_path / "backend/app.js", "module.exports = {}")

    issues = verify_jwt_invariants(tmp_path, ["backend/app.js"], jwt_required=True)

    assert [issue.code for issue in issues] == ["jwt_implementation_missing"]
    assert verify_jwt_invariants(tmp_path, ["backend/app.js"], jwt_required=False) == ()


def test_node_fullstack_commands_have_required_deterministic_order():
    gates = node_fullstack_command_gates()

    assert [gate.gate_id for gate in gates] == [
        "install-root", "install-backend", "install-frontend", "test-backend",
        "build-frontend", "test-root", "build-root", "docker-daemon", "docker-build",
    ]
    assert gates[7].timeout_seconds == 30
    assert gates[8].timeout_seconds == 1200


def test_node_fullstack_commands_can_skip_docker_when_runtime_lacks_daemon():
    gates = node_fullstack_command_gates(include_docker=False)

    assert [gate.gate_id for gate in gates] == [
        "install-root", "install-backend", "install-frontend", "test-backend",
        "build-frontend", "test-root", "build-root",
    ]


def test_root_install_failure_targets_the_owned_package_manifest(tmp_path):
    issues = _command_failure_issues(
        tmp_path,
        CommandGate("install-root", "install", ("npm", "install")),
        CommandObservation(1, stderr="native dependency is incompatible"),
        "native dependency is incompatible",
    )

    assert len(issues) == 1
    assert issues[0].code == "command_gate_failed"
    assert issues[0].path == "package.json"
    assert issues[0].gate == "install-root"


def test_jest_failure_targets_the_reported_test_file(tmp_path):
    _write(tmp_path / "counter.test.js", "test('edge', () => {});\n")
    log = (
        "Expected: { count: 2 }\nReceived: { count: 3 }\n"
        "at Object.toThrow (counter.test.js:65:39)\nTests: 4 failed"
    )

    issues = _command_failure_issues(
        tmp_path,
        CommandGate("test-root", "test", ("npm", "test")),
        CommandObservation(1, stderr=log),
        log,
    )

    assert len(issues) == 1
    assert issues[0].code == "command_gate_failed"
    assert issues[0].path == "counter.test.js"
    assert "Expected: { count: 2 }" in issues[0].message
    assert "Received: { count: 3 }" in issues[0].message


def test_jest_failure_returns_all_reported_assertion_locations(tmp_path):
    _write(tmp_path / "counter.test.js", "test('edge', () => {});\n")
    log = (
        "Expected: 3\nReceived: 6\n"
        "at Object.toBe (counter.test.js:16:29)\n"
        "Expected: 0\nReceived: 3\n"
        "at Object.toBe (counter.test.js:74:29)\n"
    )

    issues = _command_failure_issues(
        tmp_path,
        CommandGate("test-root", "test", ("npm", "test")),
        CommandObservation(1, stderr=log),
        log,
    )

    assert [issue.path for issue in issues] == [
        "counter.test.js", "counter.test.js",
    ]
    assert "counter.test.js:16" in issues[0].message
    assert "counter.test.js:74" in issues[1].message


def test_pre_qa_skips_npm_gate_for_missing_workspace_directory(tmp_path):
    _write(tmp_path / "README.md", "# Application\n")
    contract = {"required_files": [{
        "path": "README.md",
        "required": True,
        "owner_type": "devops",
        "agent_id": "devops",
    }]}
    registry = {"README.md": {"agent_id": "devops"}}
    agents = [{
        "id": "devops",
        "expert_type": "devops",
        "allowed_path_prefixes": ["README.md"],
    }]

    def unexpected_runner(_gate, _workspace):
        raise AssertionError("missing npm workspace must not be executed")

    result = PreQAVerifier(
        tmp_path,
        command_runner=unexpected_runner,
    ).verify(
        contract=contract,
        file_registry=registry,
        agents=agents,
        commit_digest="artifact:test",
        command_gates=(
                CommandGate(
                    "install-backend",
                    "install",
                    ("npm", "install"),
                    "backend",
                    required=False,
                ),
        ),
        jwt_required=False,
    )

    assert result.passed is True
    assert result.evidence[0].passed is None
    assert result.evidence[0].applicable is False
    assert result.evidence[0].executed is False
    assert "does not exist" in result.evidence[0].log_excerpt


def test_api_probe_catalog_is_compiled_only_from_project_contract():
    probes = api_probes_from_contract([
        "GET /api/todos returns 200",
        {
            "criterion": "POST /api/todos returns 201",
            "evidence_spec": {"body": {"title": "acceptance"}},
        },
    ])

    assert default_api_probes() == ()
    assert [(probe.method, probe.path, probe.expected_statuses) for probe in probes] == [
        ("GET", "/api/todos", (200,)),
        ("POST", "/api/todos", (201,)),
    ]
    assert probes[1].body == {"title": "acceptance"}


def test_api_probe_compiles_inline_json_request_body():
    probes = api_probes_from_contract([
        'POST /api/todos with valid JSON {"title":"..."} returns 201',
    ])

    assert len(probes) == 1
    assert probes[0].body == {"title": "metis-runtime-check"}


def test_api_probe_rejects_non_json_400_and_stops_at_first_failure():
    calls = []
    probes = (
        ApiProbe("priority", "POST", "/api/tickets", (400,), invariant="priority"),
        ApiProbe("later", "GET", "/api/later", (200,)),
    )

    def runner(probe):
        calls.append(probe.probe_id)
        return ApiObservation(400, False, log="Express HTML error")

    records, issues, category = run_api_probes(
        probes, runner, scope="sha256:scope", commit_digest="abc1234"
    )

    assert calls == ["priority"]
    assert records[0].passed is False
    assert issues[0].code == "api_invariant_failed"
    assert category == FAILURE_PRE_QA


def test_api_probe_rejects_sensitive_resource_details_on_denial():
    probe = ApiProbe("isolation", "GET", "/api/assets/other", (403, 404))

    records, issues, category = run_api_probes(
        (probe,),
        lambda _probe: ApiObservation(404, True, {"owner_id": "other-user"}),
        scope="sha256:scope",
        commit_digest="abc1234",
    )

    assert records[0].passed is False
    assert issues[0].code == "api_invariant_failed"
    assert category == FAILURE_PRE_QA


def test_verifier_runs_commands_then_api_and_records_redacted_evidence(tmp_path):
    contract, registry, agents = _snapshot(tmp_path)
    calls = []

    def command_runner(gate, workspace):
        assert workspace == tmp_path.resolve()
        calls.append(gate.gate_id)
        return CommandObservation(0, stdout="API_KEY=super-secret\npassed")

    def api_runner(probe):
        calls.append(probe.probe_id)
        return ApiObservation(400, True, log="Bearer abcdefghijklmnopqrstuvwxyz")

    verifier = PreQAVerifier(tmp_path, command_runner=command_runner, api_probe_runner=api_runner)
    result = verifier.verify(
        contract=contract,
        file_registry=registry,
        agents=agents,
        commit_digest="abcdef1234567",
        command_gates=(CommandGate("tests", "test", ("npm", "test")),),
        api_probes=(ApiProbe("priority", "POST", "/api/tickets", (400,)),),
    )

    assert result.passed is True
    assert result.consumes_business_qa_round is False
    assert calls == ["tests", "priority"]
    assert [record.kind for record in result.evidence] == ["test", "api"]
    assert all(record.scope_digest.startswith("sha256:") for record in result.evidence)
    assert all(record.log_digest.startswith("sha256:") for record in result.evidence)
    assert "super-secret" not in result.evidence[0].log_excerpt
    assert "abcdefghijklmnopqrstuvwxyz" not in result.evidence[1].log_excerpt


def test_verifier_collects_batchable_command_failures_before_supervisor_qa(tmp_path):
    contract, registry, agents = _snapshot(tmp_path)
    calls = []

    def runner(gate, _workspace):
        calls.append(gate.gate_id)
        return CommandObservation(1, stderr="tests failed")

    def api_runner(_probe):
        raise AssertionError("API probes must not run after command gate failures")

    result = PreQAVerifier(tmp_path, command_runner=runner, api_probe_runner=api_runner).verify(
        contract=contract,
        file_registry=registry,
        agents=agents,
        commit_digest="abcdef1234567",
        command_gates=(
            CommandGate("backend-test", "test", ("npm", "test")),
            CommandGate("must-not-run", "build", ("npm", "run", "build")),
        ),
        api_probes=(ApiProbe("must-not-probe", "GET", "/api/health", (200,)),),
    )

    assert result.passed is False
    assert result.status == FAILURE_PRE_QA
    assert result.failure_category == FAILURE_PRE_QA
    assert result.failed_gate == "backend-test"
    assert result.consumes_business_qa_round is False
    assert calls == ["backend-test", "must-not-run"]
    assert [issue.gate for issue in result.issues] == ["backend-test", "must-not-run"]


def test_frontend_build_failure_reports_precise_missing_support_files(tmp_path):
    contract, registry, agents = _snapshot(tmp_path)
    _write(
        tmp_path / "frontend" / "package.json",
        '{"scripts":{"build":"tsc && vite build"}}',
    )

    result = PreQAVerifier(
        tmp_path,
        command_runner=lambda _gate, _workspace: CommandObservation(
            1,
            stderr="error TS5058: The specified path does not exist",
        ),
    ).verify(
        contract=contract,
        file_registry=registry,
        agents=agents,
        commit_digest="abcdef1234567",
        command_gates=(
            CommandGate(
                "build-frontend",
                "build",
                ("npm", "run", "build"),
                "frontend",
            ),
        ),
    )

    assert result.failure_category == FAILURE_PRE_QA
    assert [(issue.code, issue.path, issue.gate) for issue in result.issues] == [
        (
            "missing_typescript_config",
            "frontend/tsconfig.json",
            "build-frontend",
        ),
        ("missing_vite_entry", "frontend/index.html", "build-frontend"),
    ]


def test_exit_137_is_fail_closed_as_infrastructure_resource_exhaustion(tmp_path):
    contract, registry, agents = _snapshot(tmp_path)

    result = PreQAVerifier(
        tmp_path,
        command_runner=lambda _gate, _workspace: CommandObservation(
            137,
            stderr="Killed",
        ),
    ).verify(
        contract=contract,
        file_registry=registry,
        agents=agents,
        commit_digest="abcdef1234567",
        command_gates=(
            CommandGate(
                "build-frontend",
                "build",
                ("npm", "run", "build"),
                "frontend",
            ),
        ),
    )

    assert result.passed is False
    assert result.status == FAILURE_INFRASTRUCTURE
    assert result.failure_category == FAILURE_INFRASTRUCTURE
    assert result.consumes_business_qa_round is False
    assert result.failed_gate == "build-frontend"
    assert [(issue.code, issue.path, issue.gate) for issue in result.issues] == [
        (
            "command_resource_exhausted",
            "frontend",
            "build-frontend",
        ),
    ]


@pytest.mark.parametrize(
    "log",
    [
        "Killed",
        "npm ERR! signal SIGKILL",
        "npm ERR! command failed with exit code 137",
        "ENOMEM: not enough memory",
    ],
)
def test_wrapped_resource_failure_is_infrastructure_even_when_exit_is_one(
    tmp_path, log
):
    contract, registry, agents = _snapshot(tmp_path)

    result = PreQAVerifier(
        tmp_path,
        command_runner=lambda _gate, _workspace: CommandObservation(
            1,
            stderr=log,
            failure_category=FAILURE_PRE_QA,
        ),
    ).verify(
        contract=contract,
        file_registry=registry,
        agents=agents,
        commit_digest="abcdef1234567",
        command_gates=(
            CommandGate("build-root", "build", ("npm", "run", "build")),
        ),
    )

    assert result.failure_category == FAILURE_INFRASTRUCTURE
    assert result.consumes_business_qa_round is False
    assert [issue.code for issue in result.issues] == [
        "command_resource_exhausted",
    ]


def test_verifier_stops_on_non_batchable_command_failure(tmp_path):
    contract, registry, agents = _snapshot(tmp_path)
    calls = []

    def runner(gate, _workspace):
        calls.append(gate.gate_id)
        return CommandObservation(1, stderr="docker failed")

    result = PreQAVerifier(tmp_path, command_runner=runner).verify(
        contract=contract,
        file_registry=registry,
        agents=agents,
        commit_digest="abcdef1234567",
        command_gates=(
            CommandGate("docker-build", "docker_build", ("docker", "build", ".")),
            CommandGate("must-not-run", "build", ("npm", "run", "build")),
        ),
    )

    assert result.failed_gate == "docker-build"
    assert calls == ["docker-build"]
    assert [issue.gate for issue in result.issues] == ["docker-build"]


def test_verifier_classifies_runner_exception_as_infrastructure_failure(tmp_path):
    contract, registry, agents = _snapshot(tmp_path)

    def runner(_gate, _workspace):
        raise OSError("docker daemon is not running")

    result = PreQAVerifier(tmp_path, command_runner=runner).verify(
        contract=contract,
        file_registry=registry,
        agents=agents,
        commit_digest="abcdef1234567",
        command_gates=(CommandGate("docker-build", "docker_build", ("docker", "build", ".")),),
    )

    assert result.failure_category == FAILURE_INFRASTRUCTURE
    assert result.evidence[-1].exit_code == -1
    assert result.consumes_business_qa_round is False


def test_local_command_runner_uses_workspace_npm_cache(monkeypatch, tmp_path):
    captured = {}
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_tmp = tmp_path / "runtime"
    monkeypatch.setattr("core.pre_qa_verifier.tempfile.gettempdir", lambda: str(runtime_tmp))
    monkeypatch.setenv("NPM_CONFIG_CACHE", "/root/.npm")
    monkeypatch.setenv("npm_config_cache", "/root/.npm")
    monkeypatch.setenv("HOME", "/root")

    def fake_run(command, *, cwd, env, timeout):
        captured["command"] = command
        captured["env"] = env
        captured["cwd"] = cwd
        captured["timeout"] = timeout
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("core.pre_qa_verifier._run_bounded_command", fake_run)

    observation = LocalCommandRunner()(CommandGate("install", "install", ("npm", "install")), workspace)

    npm_runtime_root = runtime_tmp / "metis-preqa-npm"
    npm_cache = npm_runtime_root / "cache"
    workspace_runtime = npm_runtime_root / "workspaces" / hashlib.sha256(
        str(workspace.resolve()).encode("utf-8")
    ).hexdigest()[:16]
    npm_home = workspace_runtime / "home"
    npmrc = workspace_runtime / "npmrc"

    assert observation.exit_code == 0
    assert captured["command"] == (
        "npm",
        "--cache",
        str(npm_cache),
        "--userconfig",
        str(npmrc),
        "install",
        "--package-lock=false",
        "--legacy-peer-deps",
    )
    assert captured["cwd"] == workspace.resolve()
    assert captured["env"]["NPM_CONFIG_CACHE"] == str(npm_cache)
    assert captured["env"]["npm_config_cache"] == str(npm_cache)
    assert captured["env"]["HOME"] == str(npm_home)
    assert captured["env"]["npm_config_update_notifier"] == "false"
    assert captured["env"]["NODE_OPTIONS"] == (
        "--max-old-space-size=192 --max-semi-space-size=4"
    )
    assert captured["env"]["UV_THREADPOOL_SIZE"] == "1"
    assert captured["env"]["npm_config_audit"] == "false"
    assert captured["env"]["npm_config_fund"] == "false"
    assert captured["env"]["npm_config_jobs"] == "1"
    assert captured["env"]["npm_config_maxsockets"] == "1"
    assert captured["env"]["npm_config_progress"] == "false"
    assert npm_cache.is_dir()
    assert npm_home.is_dir()
    assert npmrc.read_text(encoding="utf-8").startswith("cache=")
    assert not (workspace / ".npm-cache").exists()
    assert not (workspace / "package-lock.json").exists()


def test_local_command_runner_reuses_content_addressed_npm_cache_across_workspaces(
    monkeypatch, tmp_path
):
    runtime_tmp = tmp_path / "runtime"
    captured = []
    monkeypatch.setattr(
        "core.pre_qa_verifier.tempfile.gettempdir", lambda: str(runtime_tmp)
    )
    monkeypatch.setattr(
        "core.pre_qa_verifier._run_bounded_command",
        lambda command, **kwargs: (
            captured.append((command, kwargs["env"]))
            or SimpleNamespace(returncode=0, stdout="ok", stderr="")
        ),
    )

    for name in ("one", "two"):
        workspace = tmp_path / name
        workspace.mkdir()
        LocalCommandRunner()(
            CommandGate("install", "install", ("npm", "install")), workspace
        )

    assert captured[0][1]["NPM_CONFIG_CACHE"] == captured[1][1]["NPM_CONFIG_CACHE"]
    assert "metis-preqa-npm\\cache" in captured[0][1]["NPM_CONFIG_CACHE"]
    assert captured[0][1]["HOME"] != captured[1][1]["HOME"]


def test_local_command_runner_retries_transient_npm_download_once(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls = []

    def fake_run(_command, **_kwargs):
        calls.append(True)
        if len(calls) == 1:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="prebuild-install warn install Request timed out",
            )
        return SimpleNamespace(returncode=0, stdout="installed", stderr="")

    monkeypatch.setattr("core.pre_qa_verifier._run_bounded_command", fake_run)

    result = LocalCommandRunner()(
        CommandGate("install-root", "install", ("npm", "install")), workspace
    )

    assert result.exit_code == 0
    assert len(calls) == 2
    assert "transient download failure" in result.stdout


def test_local_command_runner_does_not_retry_code_install_failure(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls = []

    def fake_run(_command, **_kwargs):
        calls.append(True)
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="npm ERR! No matching version found for missing-package@99",
        )

    monkeypatch.setattr("core.pre_qa_verifier._run_bounded_command", fake_run)

    result = LocalCommandRunner()(
        CommandGate("install-root", "install", ("npm", "install")), workspace
    )

    assert result.exit_code == 1
    assert len(calls) == 1


def test_local_command_runner_uses_npm_ci_without_mutating_existing_lock(
    monkeypatch, tmp_path
):
    captured = {}
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lockfile = workspace / "package-lock.json"
    lockfile.write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    original = lockfile.read_bytes()

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("core.pre_qa_verifier._run_bounded_command", fake_run)

    result = LocalCommandRunner()(
        CommandGate("install", "install", ("npm", "install")),
        workspace,
    )

    assert result.exit_code == 0
    assert "ci" in captured["command"]
    assert "install" not in captured["command"]
    assert "--package-lock=false" not in captured["command"]
    assert captured["command"][-1] == "--legacy-peer-deps"
    assert lockfile.read_bytes() == original


def test_local_command_runner_releases_parent_memory_before_npm_spawn(
    monkeypatch, tmp_path
):
    events = []
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        "core.pre_qa_verifier._release_parent_memory_before_npm",
        lambda: events.append("release"),
    )
    monkeypatch.setattr(
        "core.pre_qa_verifier._run_bounded_command",
        lambda *_args, **_kwargs: (
            events.append("spawn")
            or SimpleNamespace(returncode=0, stdout="ok", stderr="")
        ),
    )

    result = LocalCommandRunner()(
        CommandGate("install", "install", ("npm", "install")),
        workspace,
    )

    assert result.exit_code == 0
    assert events == ["release", "spawn"]


def test_local_command_runner_does_not_expose_platform_secrets_to_npm(
    monkeypatch, tmp_path
):
    captured = {}
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secrets = {
        "DATABASE_URL": "postgresql://secret",
        "RENDER_API_KEY": "render-secret",
        "RUNTIME_ACCEPTANCE_GITHUB_TOKEN": "github-secret",
        "HERMES_API_KEY": "llm-secret",
        "OPENAI_API_KEY": "api-secret",
        "METIS_E2E_PASSWORD": "metis-secret",
        "JWT_SECRET": "production-jwt-secret",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)

    def fake_run(_command, **kwargs):
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("core.pre_qa_verifier._run_bounded_command", fake_run)

    result = LocalCommandRunner()(
        CommandGate("build-root", "build", ("npm", "run", "build")),
        workspace,
    )

    assert result.exit_code == 0
    environment = captured["env"]
    assert not (set(secrets) - {"JWT_SECRET"}) & set(environment)
    assert environment["CI"] == "true"
    assert environment["NODE_ENV"] == "test"
    assert environment["JWT_SECRET"] != secrets["JWT_SECRET"]
    assert len(environment["JWT_SECRET"]) >= 32
    allowed = {
        "PATH", "TEMP", "TMP", "SystemRoot", "ComSpec", "PATHEXT", "LANG", "LC_ALL",
        "CI", "NODE_ENV", "NODE_OPTIONS", "UV_THREADPOOL_SIZE", "JWT_SECRET",
        "HOME", "NPM_CONFIG_CACHE",
        "npm_config_cache", "npm_config_update_notifier",
        "npm_config_audit", "npm_config_fund", "npm_config_jobs",
        "npm_config_maxsockets", "npm_config_progress",
    }
    assert set(environment) <= allowed


def test_local_command_runner_serializes_npm_across_workspaces(
    monkeypatch, tmp_path
):
    active = 0
    maximum = 0
    guard = threading.Lock()
    barrier = threading.Barrier(2)
    observations = []

    def fake_run(_command, **_kwargs):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("core.pre_qa_verifier._run_bounded_command", fake_run)

    def execute(workspace):
        workspace.mkdir()
        barrier.wait()
        observations.append(
            LocalCommandRunner()(
                CommandGate("install-root", "install", ("npm", "install")),
                workspace,
            )
        )

    threads = [
        threading.Thread(target=execute, args=(tmp_path / f"workspace-{index}",))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert len(observations) == 2
    assert all(observation.exit_code == 0 for observation in observations)
    assert maximum == 1


def test_local_command_runner_bounds_runtime_output_and_preserves_failure(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        "core.pre_qa_verifier._COMMAND_OUTPUT_LIMIT_BYTES",
        64,
    )

    observation = LocalCommandRunner()(
        CommandGate(
            "test-python",
            "test",
            (
                sys.executable,
                "-c",
                (
                    "import sys;"
                    "sys.stdout.write('x'*96+'stdout-tail');"
                    "sys.stderr.write('y'*96+'stderr-tail');"
                    "raise SystemExit(137)"
                ),
            ),
        ),
        workspace,
    )

    assert observation.exit_code == 137
    assert observation.stdout.startswith("[command output truncated;")
    assert observation.stdout.endswith("stdout-tail")
    assert observation.stderr.startswith("[command output truncated;")
    assert observation.stderr.endswith("stderr-tail")


def test_command_output_buffer_never_exceeds_runtime_limit():
    output = _BoundedCommandOutput(64)

    for _ in range(10_000):
        output.append(b"x" * 1024)

    assert output.total_bytes == 10_240_000
    assert output.buffered_bytes == 64
    assert output.text().startswith("[command output truncated; kept last 64 bytes]")


def test_run_bounded_command_resolves_path_executable(monkeypatch, tmp_path):
    captured = {}

    class FakeProcess:
        pid = 4242
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        @staticmethod
        def wait(timeout):
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return FakeProcess()

    monkeypatch.setattr(
        "core.pre_qa_verifier.shutil.which",
        lambda executable, path=None: (
            str(tmp_path / "npm.cmd") if executable == "npm" else None
        ),
    )
    monkeypatch.setattr(
        "core.pre_qa_verifier.subprocess.Popen",
        fake_popen,
    )

    completed = _run_bounded_command(
        ("npm", "install"),
        cwd=tmp_path,
        env={"PATH": str(tmp_path)},
        timeout=1,
    )

    assert captured["command"] == (str(tmp_path / "npm.cmd"), "install")
    assert completed.returncode == 0


def test_run_bounded_command_isolates_group_and_cleans_on_timeout(
    monkeypatch, tmp_path
):
    captured = {}
    terminated = []

    class FakeProcess:
        pid = 4242
        stdout = io.BytesIO(b"x" * 96 + b"stdout-tail")
        stderr = io.BytesIO(b"stderr-tail")

        def wait(self, timeout):
            raise subprocess.TimeoutExpired(("fake-command",), timeout)

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("core.pre_qa_verifier.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "core.pre_qa_verifier._terminate_command_process_tree",
        lambda process: terminated.append(process.pid),
    )
    monkeypatch.setattr(
        "core.pre_qa_verifier._COMMAND_OUTPUT_LIMIT_BYTES",
        64,
    )

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        _run_bounded_command(
            ("fake-command",),
            cwd=tmp_path,
            env={},
            timeout=0.01,
        )

    assert terminated == [4242]
    assert raised.value.stdout.startswith("[command output truncated;")
    assert raised.value.stdout.endswith("stdout-tail")
    if os.name == "nt":
        assert (
            captured["kwargs"]["creationflags"]
            & subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        assert captured["kwargs"]["start_new_session"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_timeout_cleanup_sends_term_then_kill_to_entire_posix_group(monkeypatch):
    signals = []

    class FakeProcess:
        pid = 4242

        @staticmethod
        def wait(timeout):
            return 0

    monkeypatch.setattr(
        "core.pre_qa_verifier.os.killpg",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    _terminate_command_process_tree(FakeProcess())

    assert signals == [
        (4242, signal.SIGTERM),
        (4242, signal.SIGKILL),
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows process trees")
def test_timeout_cleanup_uses_taskkill_tree_on_windows(monkeypatch):
    captured = {}

    class FakeKiller:
        @staticmethod
        def wait(timeout):
            captured["timeout"] = timeout
            return 0

    class FakeProcess:
        pid = 4242

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeKiller()

    monkeypatch.setattr("core.pre_qa_verifier.subprocess.Popen", fake_popen)

    _terminate_command_process_tree(FakeProcess())

    assert captured["command"] == (
        "taskkill.exe",
        "/PID",
        "4242",
        "/T",
        "/F",
    )


def test_verifier_serializes_npm_gates_for_same_workspace(tmp_path):
    contract, registry, agents = _snapshot(tmp_path)
    active = 0
    maximum = 0
    guard = threading.Lock()
    barrier = threading.Barrier(2)
    results = []

    def runner(_gate, _workspace):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return CommandObservation(0, stdout="ok")

    def verify():
        barrier.wait()
        results.append(
            PreQAVerifier(tmp_path, command_runner=runner).verify(
                contract=contract,
                file_registry=registry,
                agents=agents,
                commit_digest="abcdef1234567",
                command_gates=(CommandGate("install", "install", ("npm", "install")),),
            )
        )

    threads = [threading.Thread(target=verify) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert len(results) == 2
    assert all(result.passed for result in results)
    assert maximum == 1


def test_verifier_reuses_backend_test_for_root_backend_only_alias(tmp_path):
    contract, registry, agents = _snapshot(tmp_path)
    _write(
        tmp_path / "package.json",
        '{"scripts":{"test":"npm --prefix backend test","build":"echo build"}}',
    )
    calls = []

    def runner(gate, _workspace):
        calls.append(gate.gate_id)
        return CommandObservation(0, stdout=f"{gate.gate_id} passed")

    result = PreQAVerifier(tmp_path, command_runner=runner).verify(
        contract=contract,
        file_registry=registry,
        agents=agents,
        commit_digest="abcdef1234567",
        command_gates=(
            CommandGate(
                "test-backend", "test", ("npm", "test", "--", "--runInBand"),
                "backend",
            ),
            CommandGate("test-root", "test", ("npm", "test")),
        ),
    )

    assert result.passed is True
    assert calls == ["test-backend"]
    assert [record.gate_id for record in result.evidence] == [
        "test-backend", "test-root",
    ]
    assert result.evidence[1].passed is True
    assert "reused test-backend evidence" in result.evidence[1].log_excerpt


def test_verifier_executes_independent_root_test_after_backend_test(tmp_path):
    contract, registry, agents = _snapshot(tmp_path)
    _write(
        tmp_path / "package.json",
        '{"scripts":{"test":"node root-contract.test.js","build":"echo build"}}',
    )
    calls = []

    def runner(gate, _workspace):
        calls.append(gate.gate_id)
        return CommandObservation(0, stdout=f"{gate.gate_id} passed")

    result = PreQAVerifier(tmp_path, command_runner=runner).verify(
        contract=contract,
        file_registry=registry,
        agents=agents,
        commit_digest="abcdef1234567",
        command_gates=(
            CommandGate(
                "test-backend", "test", ("npm", "test", "--", "--runInBand"),
                "backend",
            ),
            CommandGate("test-root", "test", ("npm", "test")),
        ),
    )

    assert result.passed is True
    assert calls == ["test-backend", "test-root"]


def test_local_command_runner_recovers_workspace_node_modules_once(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    node_modules = workspace / "node_modules"
    node_modules.mkdir(parents=True)
    (node_modules / "stale.txt").write_text("stale", encoding="utf-8")
    calls = []

    def fake_run(_command, **_kwargs):
        calls.append(True)
        if len(calls) == 1:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="npm ERR! code ENOTEMPTY\nAPI_KEY=super-secret",
            )
        return SimpleNamespace(returncode=0, stdout="installed", stderr="")

    monkeypatch.setattr("core.pre_qa_verifier._run_bounded_command", fake_run)

    result = LocalCommandRunner()(
        CommandGate("install-root", "install", ("npm", "install")),
        workspace,
    )

    assert result.exit_code == 0
    assert len(calls) == 2
    assert not node_modules.exists()
    assert "retried install once" in result.stdout
    assert "super-secret" not in result.stdout


def test_local_command_runner_never_removes_node_modules_outside_workspace(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    node_modules = outside / "node_modules"
    node_modules.mkdir(parents=True)
    monkeypatch.setattr(
        "core.pre_qa_verifier._run_bounded_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="npm ERR! code EPERM"
        ),
    )

    result = LocalCommandRunner()(
        CommandGate("install", "install", ("npm", "install"), "../outside"),
        workspace,
    )

    assert result.exit_code == 1
    assert node_modules.is_dir()


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("provider returned invalid JSON", FAILURE_MODEL),
        (
            "Jest assertion: expect(() => countElements(true)).toThrow('Invalid JSON')",
            FAILURE_PRE_QA,
        ),
        ("output was truncated due to context length", FAILURE_MODEL),
        ("rate limit: too many requests", FAILURE_MODEL),
        ("connection reset by peer", FAILURE_INFRASTRUCTURE),
        ("gyp ERR! find VS could not find any Visual Studio installation", FAILURE_INFRASTRUCTURE),
        (
            "FATAL ERROR: Reached heap limit Allocation failed",
            FAILURE_INFRASTRUCTURE,
        ),
        ("npm ERR! signal SIGKILL", FAILURE_INFRASTRUCTURE),
        ("npm ERR! command failed with exit code 137", FAILURE_INFRASTRUCTURE),
        ("ENOMEM: not enough memory", FAILURE_INFRASTRUCTURE),
        ("assertion expected 400 got 201", FAILURE_PRE_QA),
    ],
)
def test_failure_classification_does_not_charge_business_qa(message, expected):
    assert classify_failure(message) == expected


def test_scope_digest_changes_with_file_content(tmp_path):
    _write(tmp_path / "a.txt", "one")
    before = scope_digest(tmp_path, ["a.txt"])
    _write(tmp_path / "a.txt", "two")

    assert scope_digest(tmp_path, ["a.txt"]) != before
