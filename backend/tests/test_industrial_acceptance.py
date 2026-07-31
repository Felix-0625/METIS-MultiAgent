import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "industrial_acceptance.py"


@pytest.fixture
def acceptance(monkeypatch):
    spec = importlib.util.spec_from_file_location("industrial_acceptance_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_package(path: Path, scripts: dict[str, str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "package.json").write_text(
        json.dumps({"name": path.name or "root", "scripts": scripts}),
        encoding="utf-8",
    )


def _write_static_generated_project(path: Path) -> None:
    (path / "README.md").write_text("ready", encoding="utf-8")
    _write_package(path, {
        "start": "node server.js",
        "test": "npm --prefix backend test",
        "build": "npm --prefix frontend run build",
    })
    _write_package(path / "backend", {"test": "node --test"})
    _write_package(path / "frontend", {"build": "vite build"})
    tests = path / "backend" / "tests"
    tests.mkdir()
    (tests / "auth.test.js").write_text(
        "const { createApp } = require('../src/server');",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "evidence, message",
    [
        (None, "without isolated runtime acceptance evidence"),
        ({"enabled": False, "passed": True, "status": "passed"}, "was not enabled"),
        ({"enabled": True, "passed": False, "status": "passed"}, "did not pass"),
        ({"enabled": True, "passed": True, "status": "failed"}, "non-passed status"),
    ],
)
def test_runtime_acceptance_evidence_is_fail_closed(acceptance, evidence, message):
    with pytest.raises(RuntimeError, match=message):
        acceptance.validate_runtime_acceptance_evidence(
            {"runtime_acceptance": evidence}
        )


def test_pre_qa_failure_is_terminal_and_does_not_poll_forever(acceptance):
    assert (
        acceptance._qa_failure_code("pre_qa_failed")
        == acceptance.EXIT_PROJECT_FAILURE
    )


def test_acceptance_requirements_lock_complete_four_phase_contract(acceptance):
    from core.project_contract import parse_project_contract

    contract = parse_project_contract(acceptance.PROJECT_REQUIREMENTS)

    assert contract.phase_count == 4
    assert [phase.name for phase in contract.phases] == [
        "Foundation and runtime contract",
        "Backend domain and authorization",
        "Frontend workflows and integration",
        "Release hardening and deployment",
    ]
    assert all(phase.roles for phase in contract.phases)
    assert all(phase.tasks for phase in contract.phases)
    assert all(phase.acceptance_criteria for phase in contract.phases)
    assert contract.phases[0].tasks[1].acceptance_criteria == (
        "backend/package.json has a registry-backed byte digest",
    )
    assert contract.phases[0].tasks[2].acceptance_criteria == (
        "frontend/package.json has a registry-backed byte digest",
    )
    assert contract.source_requirements == acceptance.PROJECT_REQUIREMENTS


def test_acceptance_requirements_confirm_without_client_manifest(
    acceptance, monkeypatch,
):
    from agents.pm_team import PMLeaderAgent

    leader = PMLeaderAgent()

    def offline_model(*_args, **_kwargs):
        raise RuntimeError("offline deterministic fallback")

    monkeypatch.setattr(leader.hermes, "chat", offline_model)
    canonical = leader.record_user_requirements(
        acceptance.PROJECT_REQUIREMENTS,
        source="user",
    )
    synthesized = leader.synthesize_plan_fast(
        leader.canonical_requirements,
        requirements_revision=canonical["requirements_revision"],
        requirements_digest=canonical["requirements_digest"],
    )
    confirmed = leader.confirm_plan(
        expected_revision=canonical["requirements_revision"],
        expected_digest=canonical["requirements_digest"],
    )

    assert synthesized["success"] is True
    assert confirmed["success"] is True, confirmed.get("violations")
    assert confirmed["status"] == "confirmed"
    plan = confirmed["plan"]
    assert plan["schema_version"] == "total-plan/v1"
    assert "required_files" not in plan
    assert list(plan["project_contract"]["required_files"]) == []
    assert plan["phases"]


def test_qa_pending_resumes_directly_at_phase_qa(acceptance):
    assert acceptance._phase_resume_mode("qa_pending") == "qa_only"


def test_running_pre_qa_recovery_resumes_phase_qa_polling(acceptance):
    assert acceptance._phase_resume_mode(
        "pre_qa_verifying",
        {"status": "pre_qa_verifying", "running": True},
    ) == "qa_only"


def test_authoritative_qa_state_overrides_stale_phase_projection(acceptance):
    assert acceptance._phase_resume_mode(
        "reviewing",
        {"status": "pre_qa_verifying", "running": True},
    ) == "qa_only"
    assert acceptance._phase_resume_mode(
        "reviewing",
        {"status": "passed", "running": False},
    ) == "qa_only"


def test_phase_agent_ids_excludes_preserve_only_verifiers(acceptance):
    phase = {
        "agents": [
            {"id": "writer", "verification_only": False},
            {"id": "preserve-verifier", "verification_only": True},
        ],
    }

    assert acceptance._phase_agent_ids(phase) == ["writer"]


def test_scheduled_pre_qa_repair_polls_until_supervisor_passes(
    acceptance,
    monkeypatch,
):
    scheduled = {
        "status": "pre_qa_failed",
        "running": False,
        "needs_manual": False,
        "action_required": {
            "scheduled_runs": {"agent-1": "repair-run-1"},
        },
    }
    statuses = iter([
        scheduled,
        scheduled,
        {"status": "pre_qa_verifying", "running": True, "round": 0},
        {"status": "passed", "running": False, "round": 0},
    ])

    class Session:
        def get(self, *args, **kwargs):
            return _JsonResponse(next(statuses))

        def post(self, *args, **kwargs):
            raise AssertionError("a scheduled durable repair must not be restarted")

    monkeypatch.setattr(acceptance.time, "sleep", lambda *_: None)

    assert acceptance._phase_resume_mode(
        "pre_qa_failed",
        scheduled,
    ) == "qa_only"
    acceptance.run_phase_qa(Session(), "proj-test", "phase-1")


@pytest.mark.parametrize(
        ("phase_status", "qa_status", "exit_code"),
        [
            ("pre_qa_failed", {"status": "pre_qa_failed"}, 2),
            ("infrastructure_failed", {"status": "infrastructure_failed"}, 3),
        ],
)
def test_failed_phase_recovery_remains_fail_closed(
    acceptance, phase_status, qa_status, exit_code
):
    with pytest.raises(acceptance.AcceptanceFailure) as caught:
        acceptance._phase_resume_mode(phase_status, qa_status)
    assert caught.value.exit_code == exit_code


def test_model_failed_phase_resumes_same_fail_closed_qa_round(acceptance):
    assert (
        acceptance._phase_resume_mode(
            "model_failed", {"status": "model_failed", "running": False}
        )
        == "qa_only"
    )
    assert (
        acceptance._qa_failure_code("model_failed")
        == acceptance.EXIT_INFRASTRUCTURE_FAILURE
    )


def test_remote_runtime_validation_never_executes_local_node(
    acceptance, monkeypatch, tmp_path
):
    _write_static_generated_project(tmp_path)
    runtime_evidence = {
        "enabled": True,
        "passed": True,
        "status": "passed",
        "deploy_id": "dep-test",
        "commit_sha": "abc123",
        "service_url": "https://validator.example.test",
    }

    def forbidden(*args, **kwargs):
        raise AssertionError("local generated code execution is forbidden")

    monkeypatch.setattr(acceptance, "run_command", forbidden)
    monkeypatch.setattr(acceptance, "validate_runtime_environment", forbidden)
    monkeypatch.setattr(acceptance, "run_application_acceptance", forbidden)

    result = acceptance.validate_generated_project_with_remote_runtime(
        tmp_path, runtime_evidence
    )

    assert result["status"] == "passed"
    assert result["mode"] == "isolated_render_runtime"
    assert result["runtime_acceptance"] == runtime_evidence
    assert result["static"]["backend_tests"] == "imports_production_backend_src"


def test_remote_runtime_validation_rejects_mock_only_backend_tests(
    acceptance, tmp_path
):
    _write_static_generated_project(tmp_path)
    (tmp_path / "backend" / "tests" / "auth.test.js").write_text(
        "const assert = require('node:assert'); assert.equal(1, 1);",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="do not import production code"):
        acceptance.validate_generated_project_with_remote_runtime(
            tmp_path,
            {"enabled": True, "passed": True, "status": "passed"},
        )


def test_generated_project_installs_all_packages_before_tests(acceptance, monkeypatch, tmp_path):
    (tmp_path / "README.md").write_text("ready", encoding="utf-8")
    _write_package(tmp_path, {
        "start": "node server.js",
        "test": "root-test",
        "build": "root-build",
    })
    _write_package(tmp_path / "backend", {"test": "backend-test"})
    _write_package(tmp_path / "frontend", {"build": "frontend-build"})
    tests = tmp_path / "backend" / "tests"
    tests.mkdir()
    (tests / "auth.test.js").write_text(
        "const { createApp } = require('../src/server');",
        encoding="utf-8",
    )

    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_run(command, cwd, timeout=600, **kwargs):
        calls.append((Path(cwd).relative_to(tmp_path).as_posix(), tuple(command)))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(acceptance, "run_command", fake_run)
    monkeypatch.setattr(acceptance, "npm_command", lambda *args: list(args))
    monkeypatch.setattr(
        acceptance,
        "validate_runtime_environment",
        lambda *args, **kwargs: {"os": "Linux", "node": "v20.0.0", "npm": "10.0.0"},
    )
    monkeypatch.setattr(
        acceptance,
        "run_application_acceptance",
        lambda *args, **kwargs: {"checks": ["smoke"]},
    )
    monkeypatch.setattr(acceptance.shutil, "which", lambda name: name)

    summary = acceptance.validate_generated_project(tmp_path, tmp_path / "evidence")

    assert calls[:3] == [
        (".", ("install", "--no-audit", "--no-fund")),
        ("backend", ("install", "--no-audit", "--no-fund")),
        ("frontend", ("install", "--no-audit", "--no-fund")),
    ]
    assert calls[3:7] == [
        (".", ("test",)),
        ("backend", ("test",)),
        (".", ("run", "build")),
        ("frontend", ("run", "build")),
    ]
    assert len(calls) == 8
    assert calls[-1][0] == "backend"
    assert calls[-1][1][:2] == ("node", "-e")
    assert summary["status"] == "passed"


def test_generated_project_requires_root_test_and_build_scripts(acceptance, tmp_path):
    (tmp_path / "README.md").write_text("ready", encoding="utf-8")
    _write_package(tmp_path, {"start": "node server.js"})
    _write_package(tmp_path / "backend", {})
    _write_package(tmp_path / "frontend", {"build": "vite build"})

    with pytest.raises(RuntimeError, match="missing required scripts"):
        acceptance.validate_generated_project(tmp_path, tmp_path / "evidence")


def test_archive_members_reject_case_insensitive_conflicts(acceptance):
    with pytest.raises(RuntimeError, match="case-insensitive path conflicts"):
        acceptance.validate_archive_members([
            "Backend/package.json",
            "backend/package.json",
        ])


def test_archive_members_reject_case_insensitive_directory_conflicts(acceptance):
    with pytest.raises(RuntimeError, match="case-insensitive path conflicts"):
        acceptance.validate_archive_members([
            "Backend/src/a.js",
            "backend/src/b.js",
        ])


def test_archive_members_reject_path_traversal(acceptance):
    with pytest.raises(RuntimeError, match="unsafe path"):
        acceptance.validate_archive_members(["frontend/package.json", "../secret.txt"])


@pytest.mark.parametrize("path", ["/etc/passwd", "C:/Windows/test.txt", "\\\\server\\share.txt"])
def test_archive_members_reject_absolute_paths(acceptance, path):
    with pytest.raises(RuntimeError, match="unsafe path"):
        acceptance.validate_archive_members([path])


def test_archive_members_reject_duplicate_members(acceptance):
    with pytest.raises(RuntimeError, match="duplicate member"):
        acceptance.validate_archive_members(["backend/a.js", "backend/a.js"])


def test_archive_members_reject_oversized_entry(acceptance):
    member = zipfile.ZipInfo("backend/huge.bin")
    member.file_size = acceptance.MAX_ARCHIVE_FILE_SIZE + 1
    member.compress_size = member.file_size

    with pytest.raises(RuntimeError, match="size limits"):
        acceptance.validate_archive_members([member])


def test_archive_members_accept_portable_paths(acceptance):
    acceptance.validate_archive_members([
        "backend/package.json",
        "frontend/package.json",
        "README.md",
    ])


def test_default_generated_validation_uses_linux_node20_docker(acceptance, monkeypatch, tmp_path):
    project = tmp_path / "project"
    evidence = tmp_path / "evidence"
    project.mkdir()
    evidence.mkdir()
    calls = []

    monkeypatch.setattr(acceptance.shutil, "which", lambda name: "docker" if name == "docker" else name)

    def fake_run(command, cwd, timeout=600, **kwargs):
        calls.append(command)
        (evidence / "runtime-summary.json").write_text(
            json.dumps({"status": "passed"}), encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(acceptance, "run_command", fake_run)

    result = acceptance.validate_generated_project_in_docker(project, evidence)

    assert result["status"] == "passed"
    command = calls[0]
    assert "node:20-bookworm" in command
    assert "linux/amd64" in command
    assert "METIS_ACCEPTANCE_CONTAINER=1" in command
    assert "--validate-generated /tmp/generated-project" in command[-1]


def test_runtime_environment_rejects_non_node20(acceptance, monkeypatch, tmp_path):
    monkeypatch.setattr(acceptance.platform, "system", lambda: "Linux")
    monkeypatch.setattr(acceptance.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        acceptance,
        "run_command",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="v24.1.0\n", stderr=""),
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError, match="requires Node 20"):
        acceptance.validate_runtime_environment()


def test_ticket_asset_link_accepts_common_shapes(acceptance):
    assert acceptance._linked_asset_id({"assetId": 7}) == 7
    assert acceptance._linked_asset_id({"asset_id": "8"}) == "8"
    assert acceptance._linked_asset_id({"asset": {"id": 9}}) == 9


def test_application_acceptance_checks_isolation_links_uniqueness_and_restart(
    acceptance, monkeypatch, tmp_path
):
    class Response:
        def __init__(self, status, payload=None, *, html=None):
            self.status_code = status
            self._payload = payload
            self.text = html if html is not None else json.dumps(payload or {})
            self.headers = {
                "content-type": "text/html" if html is not None else "application/json"
            }

        @property
        def ok(self):
            return 200 <= self.status_code < 400

        def json(self):
            return self._payload

    users = {}
    assets = []
    tickets = []

    def owner(headers):
        return (headers or {}).get("Authorization", "").removeprefix("Bearer ")

    def post(url, json=None, headers=None, timeout=None):
        if url.endswith("/api/auth/register"):
            users[json["email"]] = json["password"]
            return Response(201, {"id": len(users)})
        if url.endswith("/api/auth/login"):
            if users.get(json["email"]) != json["password"]:
                return Response(401, {"error": "invalid"})
            return Response(200, {"token": json["email"]})
        if url.endswith("/api/assets"):
            if any(item["assetCode"] == json["assetCode"] for item in assets):
                return Response(409, {"error": "duplicate"})
            item = {**json, "id": len(assets) + 1, "owner": owner(headers)}
            assets.append(item)
            return Response(201, {"asset": item})
        if url.endswith("/api/tickets"):
            item = {**json, "id": len(tickets) + 1, "owner": owner(headers)}
            tickets.append(item)
            return Response(201, {"ticket": item})
        raise AssertionError(url)

    def get(url, headers=None, timeout=None):
        if url.endswith("/api/assets"):
            return Response(200, {"assets": [x for x in assets if x["owner"] == owner(headers)]})
        if url.endswith("/api/tickets"):
            return Response(200, {"tickets": [x for x in tickets if x["owner"] == owner(headers)]})
        if url.startswith("http://127.0.0.1:"):
            return Response(200, html='<html><div id="root"></div></html>')
        raise AssertionError(url)

    class Process:
        def poll(self):
            return None

    class Handle:
        def close(self):
            return None

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "application.log").write_text("started\n", encoding="utf-8")
    monkeypatch.setattr(acceptance.requests, "post", post)
    monkeypatch.setattr(acceptance.requests, "get", get)
    monkeypatch.setattr(acceptance, "_start_application", lambda *args: (Process(), Handle()))
    monkeypatch.setattr(acceptance, "_stop_application", lambda *args: None)
    monkeypatch.setattr(acceptance, "_wait_for_application", lambda *args: None)
    monkeypatch.setattr(acceptance.time, "sleep", lambda *_: None)

    result = acceptance.run_application_acceptance(tmp_path, evidence)

    assert {
        "asset_create_read",
        "asset_code_unique",
        "ticket_asset_link",
        "per_user_isolation",
        "sqlite_restart_persistence",
    }.issubset(result["checks"])


class _JsonResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def json(self):
        return self._payload


def test_phase_qa_reuses_passed_result_without_post(acceptance):
    class Session:
        def get(self, *args, **kwargs):
            return _JsonResponse({"status": "passed", "round": 2})

        def post(self, *args, **kwargs):
            raise AssertionError("passed QA must not be started again")

    acceptance.run_phase_qa(Session(), "proj-test", "phase-1")


def test_phase_qa_reuses_running_result_without_post(acceptance, monkeypatch):
    statuses = iter([
        {"status": "checking", "running": True, "round": 1},
        {"status": "passed", "running": False, "round": 1},
    ])

    class Session:
        def get(self, *args, **kwargs):
            return _JsonResponse(next(statuses))

        def post(self, *args, **kwargs):
            raise AssertionError("running QA must not be started again")

    monkeypatch.setattr(acceptance.time, "sleep", lambda *_: None)
    acceptance.run_phase_qa(Session(), "proj-test", "phase-1")


def test_phase_qa_restarts_rebuild_after_agents_completed(acceptance, monkeypatch):
    statuses = iter([
        {"status": "rebuild_started", "running": False, "round": 0},
        {"status": "passed", "running": False, "round": 1},
    ])
    posts = []

    class Session:
        def get(self, *args, **kwargs):
            return _JsonResponse(next(statuses))

        def post(self, *args, **kwargs):
            posts.append(args[0])
            return _JsonResponse({"success": True})

    monkeypatch.setattr(acceptance.time, "sleep", lambda *_: None)
    acceptance.run_phase_qa(Session(), "proj-test", "phase-4")
    assert len(posts) == 1


def test_phase_qa_resumes_interrupted_cycle_after_service_restart(
    acceptance, monkeypatch,
):
    statuses = iter([
        {
            "status": "interrupted",
            "retryable": True,
            "action_required": {"options": ["retry_cycle"]},
        },
        {"status": "pre_qa_verifying", "running": True, "round": 0},
        {"status": "passed", "running": False, "round": 0},
    ])
    posts = []

    class Session:
        def get(self, *args, **kwargs):
            return _JsonResponse(next(statuses))

        def post(self, url, **kwargs):
            posts.append((url, kwargs.get("params")))
            return _JsonResponse({"success": True})

    monkeypatch.setattr(acceptance.time, "sleep", lambda *_: None)
    acceptance.run_phase_qa(Session(), "proj-test", "phase-2")

    assert posts == [(
        f"{acceptance.BASE}/projects/proj-test/phases/phase-2/auto-repair",
        {"user_decision": "retry_cycle"},
    )]


def test_phase_qa_interrupted_without_retry_authority_remains_fail_closed(
    acceptance,
):
    class Session:
        def get(self, *args, **kwargs):
            return _JsonResponse({
                "status": "interrupted",
                "action_required": {"options": ["manual_edit"]},
            })

        def post(self, *args, **kwargs):
            raise AssertionError("unauthorized recovery must not be started")

    with pytest.raises(acceptance.AcceptanceFailure) as raised:
        acceptance.run_phase_qa(Session(), "proj-test", "phase-2")
    assert raised.value.exit_code == acceptance.EXIT_INFRASTRUCTURE_FAILURE


@pytest.mark.parametrize(
    "state, exit_code",
    [
        ("quality_regressed", 2),
        ("no_progress", 2),
        ("qa_blocked", 2),
        ("awaiting_manual_fix", 4),
        ("infrastructure_blocked", 3),
    ],
)
def test_phase_qa_terminal_states_exit_immediately(acceptance, state, exit_code):
    class Session:
        def get(self, *args, **kwargs):
            return _JsonResponse({"status": state, "issue_report": {"blocking": 1}})

        def post(self, *args, **kwargs):
            raise AssertionError("terminal QA must not be restarted")

    with pytest.raises(acceptance.AcceptanceFailure) as raised:
        acceptance.run_phase_qa(Session(), "proj-test", "phase-1")
    assert raised.value.exit_code == exit_code


def test_final_qa_reuses_passed_result_and_deploy_evidence(acceptance):
    payload = {
        "status": "passed",
        "all_passed": True,
        "runtime_acceptance": {
            "deploy_id": "dep-1",
            "commit_sha": "abc",
            "service_url": "https://validator.test",
            "status": "passed",
            "passed": True,
        },
    }

    class Session:
        def get(self, *args, **kwargs):
            return _JsonResponse(payload)

        def post(self, *args, **kwargs):
            raise AssertionError("passed final QA must not be started again")

    assert acceptance.run_final_qa(Session(), "proj-test") == payload
    assert acceptance._deployment_evidence(payload["runtime_acceptance"])["deploy_id"] == "dep-1"


def test_final_qa_resume_retries_recoverable_failed_run(acceptance, monkeypatch):
    statuses = iter([
        {
            "status": "failed",
            "failed_reason": "REWORK_LOCK_UNAVAILABLE",
            "retryable": True,
            "action_required": {"options": ["retry_acceptance"]},
        },
        {"status": "qc_running_round_1", "round": 1},
        {"status": "passed", "all_passed": True},
    ])
    posts = []

    class Session:
        def get(self, *args, **kwargs):
            return _JsonResponse(next(statuses))

        def post(self, *args, **kwargs):
            posts.append(args[0])
            return _JsonResponse({"success": True})

    monkeypatch.setattr(acceptance.time, "sleep", lambda *_: None)
    result = acceptance.run_final_qa(Session(), "proj-test")
    assert result["status"] == "passed"
    assert len(posts) == 1


def test_final_qa_resume_retries_legacy_rework_failure_without_action(acceptance, monkeypatch):
    statuses = iter([
        {"status": "failed", "failed_reason": "REWORK_FAILED"},
        {"status": "passed", "all_passed": True},
    ])
    posts = []

    class Session:
        def get(self, *args, **kwargs):
            return _JsonResponse(next(statuses))

        def post(self, *args, **kwargs):
            posts.append(args[0])
            return _JsonResponse({"success": True})

    monkeypatch.setattr(acceptance.time, "sleep", lambda *_: None)
    result = acceptance.run_final_qa(Session(), "proj-test")
    assert result["status"] == "passed"
    assert len(posts) == 1


def test_final_qa_dynamic_running_state_does_not_post(acceptance, monkeypatch):
    statuses = iter([
        {"status": "qc_running_round_2", "round": 2},
        {"status": "passed", "all_passed": True},
    ])

    class Session:
        def get(self, *args, **kwargs):
            return _JsonResponse(next(statuses))

        def post(self, *args, **kwargs):
            raise AssertionError("dynamic running state must not be restarted")

    monkeypatch.setattr(acceptance.time, "sleep", lambda *_: None)
    assert acceptance.run_final_qa(Session(), "proj-test")["status"] == "passed"


def test_existing_project_is_loaded_without_creation(acceptance):
    calls = []

    class Session:
        def get(self, url, **kwargs):
            calls.append(("GET", url))
            return _JsonResponse({"project_id": "proj-existing", "description": "resume"})

        def post(self, *args, **kwargs):
            raise AssertionError("existing project must not be recreated")

    project_id, project, created = acceptance._get_or_create_project(
        Session(), "proj-existing"
    )
    assert project_id == "proj-existing"
    assert project["description"] == "resume"
    assert created is False
    assert calls == [("GET", f"{acceptance.BASE}/projects/proj-existing")]


def test_model_failed_agent_is_terminal_but_not_successful(acceptance):
    assert "model_failed" in acceptance.TERMINAL_AGENT_STATES


def test_signoff_posts_then_verifies_completed_project(acceptance):
    calls = []

    class Session:
        def post(self, url, **kwargs):
            calls.append(("POST", url))
            return _JsonResponse({"success": True})

        def get(self, url, **kwargs):
            calls.append(("GET", url))
            return _JsonResponse({"project_id": "proj-test", "status": "completed"})

    result = acceptance.signoff_and_verify(Session(), "proj-test")

    assert result == {"status": "completed", "reused": False}
    assert calls == [
        ("POST", f"{acceptance.BASE}/projects/proj-test/signoff"),
        ("GET", f"{acceptance.BASE}/projects/proj-test"),
    ]


def test_signoff_completed_project_is_idempotent(acceptance):
    class Session:
        def post(self, *args, **kwargs):
            raise AssertionError("completed project must not be signed off again")

        def get(self, url, **kwargs):
            return _JsonResponse({"project_id": "proj-test", "status": "completed"})

    result = acceptance.signoff_and_verify(
        Session(), "proj-test", already_completed=True
    )

    assert result == {"status": "completed", "reused": True}


def test_signoff_requires_persisted_completed_status(acceptance):
    class Session:
        def post(self, url, **kwargs):
            return _JsonResponse({"success": True})

        def get(self, url, **kwargs):
            return _JsonResponse({"project_id": "proj-test", "status": "active"})

    with pytest.raises(
        acceptance.AcceptanceFailure,
        match="did not persist status=completed",
    ):
        acceptance.signoff_and_verify(Session(), "proj-test")


def test_acceptance_summary_is_atomic_and_preserves_resume_evidence(
    acceptance, monkeypatch, tmp_path
):
    script = tmp_path / "repo" / "scripts" / "industrial_acceptance.py"
    script.parent.mkdir(parents=True)
    monkeypatch.setattr(acceptance, "__file__", str(script))

    first = acceptance.AcceptanceSummary("proj-summary")
    first.update(
        current_step="final_qa",
        deployment={"deploy_id": "dep-1"},
        status="failed",
        error="old failure",
        exit_code=2,
        failed_at=123.0,
    )
    resumed = acceptance.AcceptanceSummary("proj-summary")
    resumed.update(current_step="download_archive")

    payload = json.loads(resumed.path.read_text(encoding="utf-8"))
    assert payload["project_id"] == "proj-summary"
    assert payload["current_step"] == "download_archive"
    assert payload["deployment"]["deploy_id"] == "dep-1"
    assert payload["status"] == "running"
    assert "error" not in payload
    assert "exit_code" not in payload
    assert "failed_at" not in payload
    assert not resumed.path.with_suffix(".json.tmp").exists()


@pytest.mark.parametrize("status", ["active", "in_progress", "running", "reviewing"])
def test_live_in_progress_phase_states_are_resumable(acceptance, status):
    assert status in acceptance.RESUMABLE_PHASE_STATES


def test_resilient_session_reauthenticates_and_continues_get(
    acceptance, monkeypatch
):
    calls = []

    def fake_request(session, method, url, **kwargs):
        calls.append((method, url))
        if len(calls) == 1:
            raise acceptance.requests.ConnectionError("deploy restart")
        if url.endswith("/auth/login"):
            return _JsonResponse({"token": "new-token"})
        return _JsonResponse({"status": "passed"})

    monkeypatch.setattr(acceptance.requests.Session, "request", fake_request)
    monkeypatch.setattr(acceptance.time, "sleep", lambda *_: None)
    session = acceptance.ResilientSession("user@example.test", "secret")

    response = session.get("https://example.test/status", timeout=1)

    assert response.json()["status"] == "passed"
    assert [method for method, _ in calls] == ["GET", "POST", "GET"]


def test_resilient_session_retries_safe_get_across_transient_502(
    acceptance, monkeypatch,
):
    calls = []

    def fake_request(session, method, url, **kwargs):
        calls.append((method, url))
        if len(calls) < 4:
            return _JsonResponse({"detail": "restarting"}, status_code=502)
        return _JsonResponse({"status": "passed"})

    monkeypatch.setattr(acceptance.requests.Session, "request", fake_request)
    monkeypatch.setattr(acceptance.time, "sleep", lambda *_: None)
    session = acceptance.ResilientSession("user@example.test", "secret")

    response = session.get("https://example.test/status", timeout=1)

    assert response.json()["status"] == "passed"
    assert [method for method, _ in calls] == ["GET", "GET", "GET", "GET"]


def test_resume_completed_project_does_not_create_or_restart_phases(
    acceptance, monkeypatch, tmp_path
):
    archive_buffer = __import__("io").BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as bundle:
        bundle.writestr("README.md", "ready")
    archive_bytes = archive_buffer.getvalue()
    runtime = {
        "enabled": True,
        "passed": True,
        "status": "passed",
        "deploy_id": "dep-resume",
        "commit_sha": "abc123",
    }
    phases = [
        {
            "phase_id": f"phase-{index}",
            "status": "completed",
            "user_confirmed": True,
        }
        for index in range(1, 5)
    ]

    class ArchiveResponse:
        ok = True
        status_code = 200
        text = ""
        content = archive_bytes

    class Session:
        def authenticate(self):
            return None

        def get(self, url, **kwargs):
            if url.endswith("/projects/proj-resume"):
                return _JsonResponse({"project_id": "proj-resume", "status": "completed"})
            if url.endswith("/phases"):
                return _JsonResponse({"phases": phases})
            if url.endswith("/final-qa/status"):
                return _JsonResponse(
                    {"status": "passed", "all_passed": True, "runtime_acceptance": runtime}
                )
            if url.endswith("/files"):
                return _JsonResponse({
                    "tree": [
                        {"type": "file", "key": f"file-{index}.txt"}
                        for index in range(15)
                    ]
                })
            if url.endswith("/archive/download"):
                return ArchiveResponse()
            raise AssertionError(url)

        def post(self, *args, **kwargs):
            raise AssertionError("resume must not create a project or restart completed work")

    script = tmp_path / "repo" / "scripts" / "industrial_acceptance.py"
    script.parent.mkdir(parents=True)
    monkeypatch.setattr(acceptance, "__file__", str(script))
    monkeypatch.setattr(acceptance, "ResilientSession", lambda *_: Session())
    monkeypatch.setattr(
        acceptance,
        "_get_or_create_project",
        lambda session, project_id: (
            project_id,
            {
                "project_id": project_id,
                "description": "existing",
                "status": "completed",
            },
            False,
        ),
    )
    monkeypatch.setattr(
        acceptance,
        "validate_generated_project_with_remote_runtime",
        lambda root, evidence: {"status": "passed", "runtime_acceptance": evidence},
    )
    monkeypatch.setenv("METIS_E2E_LOGIN", "user@example.test")
    monkeypatch.setenv("METIS_E2E_PASSWORD", "secret")
    monkeypatch.setenv("METIS_ACCEPTANCE_USE_DOCKER", "0")

    result = acceptance.main("proj-resume", resume=True)

    assert result["status"] == "passed"
    assert result["current_step"] == "completed"
    assert result["deployment"]["deploy_id"] == "dep-resume"
    assert result["signoff"] == {"status": "completed", "reused": True}
