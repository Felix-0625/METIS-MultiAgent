from pathlib import Path
import asyncio
from types import SimpleNamespace

from agents.execution_agent import ExecutionAgent
from core.project_context import ProjectContext
from api import routes_execution, routes_phases
from core.app_state import _upgrade_default_token_limit, _can_user_access_project


def _agent() -> ExecutionAgent:
    agent = object.__new__(ExecutionAgent)
    agent.workspace = Path.cwd()
    agent.output_files = []
    agent.logs = []
    return agent


def test_rejects_readme_body_misidentified_as_path():
    agent = _agent()
    malformed = "/n/n## 快速开始/n/n```bash/npip install -r requirements.txt/n```"

    assert agent._sanitize_declared_path(malformed) is None
    assert agent._normalize_output_path(
        malformed, malformed, 0, "demo", "生成 Python 服务"
    ) == "src/demo_1.txt"


def test_keeps_valid_declared_file_path():
    agent = _agent()

    assert agent._sanitize_declared_path("src/main.py") == "src/main.py"
    assert agent._normalize_output_path(
        "src/main.py", "python", 0, "demo", "生成 Python 服务"
    ) == "src/main.py"


def test_rejects_traversal_and_absolute_paths():
    agent = _agent()

    assert agent._sanitize_declared_path("../secret.py") is None
    assert agent._sanitize_declared_path("/tmp/secret.py") is None


def test_command_example_is_not_treated_as_deliverable():
    agent = _agent()

    assert agent._is_command_block("bash", "pip install -r requirements.txt", "快速开始")
    assert not agent._is_command_block("bash", "#!/bin/sh\necho ok", "生成启动脚本")


def test_truncated_delivery_error_is_retryable():
    assert ExecutionAgent._is_retryable_delivery_error(
        ValueError("LLM output appears truncated or has unclosed code fences")
    )
    assert not ExecutionAgent._is_retryable_delivery_error(ValueError("permission denied"))


def test_only_python_package_markers_may_be_empty():
    assert ExecutionAgent._allows_empty_file("backend/app/__init__.py")


def test_delivery_evidence_records_real_dotfile_digest(tmp_path, monkeypatch):
    (tmp_path / ".env.example").write_text("JWT_SECRET=\n", encoding="utf-8")
    ctx = SimpleNamespace(
        workspace=tmp_path,
        agents={"agent-1": {}},
    )
    monkeypatch.setitem(routes_execution.execution_status, "agent-1", {"run_id": "run-1"})

    evidence = routes_execution._record_output_file_evidence(
        ctx, "agent-1", [".env.example", "../outside"],
    )

    assert evidence["run_id"] == "run-1"
    assert [item["path"] for item in evidence["files"]] == [".env.example"]
    assert len(evidence["files"][0]["sha256"]) == 64


def test_new_durable_run_reacquires_a_new_file_lease(monkeypatch):
    released = []
    claims = []
    agent = {
        "id": "agent-1",
        "expert_id": "expert-1",
        "subproject_id": "sp-1",
        "project_id": "proj-1",
        "allowed_path_prefixes": ["backend/"],
        "lock_id": "lock:old",
        "lock_run_id": "old-run",
    }
    ctx = SimpleNamespace(project_id="proj-1", agents={"agent-1": agent})
    monkeypatch.setattr(
        routes_execution.expert_lock,
        "release_lock",
        lambda lock_id: released.append(lock_id) or {"success": True},
    )
    monkeypatch.setattr(
        routes_execution.expert_lock,
        "get_active_locks",
        lambda **_kwargs: [],
    )

    def claim(expert_id, project_id, task_id, scopes):
        claims.append((expert_id, project_id, task_id, scopes))
        return {"success": True, "lock_id": f"lock:{task_id}", "leased_until": 123.0}

    monkeypatch.setattr(routes_execution.expert_lock, "atomic_claim_lock", claim)

    result = routes_execution._ensure_agent_run_file_lease(ctx, "agent-1", "new-run")

    assert released == ["lock:old"]
    assert claims[0][2] == "sp-1:run:new-run"
    assert claims[0][3] == ["*"]
    assert agent["lock_run_id"] == "new-run"
    assert agent["lock_id"] == result["lock_id"]
    assert not ExecutionAgent._allows_empty_file("backend/app/main.py")


def test_new_run_clears_only_exact_old_run_lineage(monkeypatch):
    released = []
    agent = {
        "id": "agent-1",
        "expert_id": "expert-1",
        "subproject_id": "sp-1",
        "project_id": "proj-1",
        "allowed_path_prefixes": ["backend/"],
    }
    ctx = SimpleNamespace(project_id="proj-1", agents={"agent-1": agent})
    monkeypatch.setattr(
        routes_execution.expert_lock,
        "get_active_locks",
        lambda **kwargs: [
            {"lock_id": "old-run", "expert_id": "expert-1", "project_id": "proj-1", "task_id": "sp-1:run:old"},
            {"lock_id": "planning", "expert_id": "expert-1", "project_id": "proj-1", "task_id": "sp-1"},
            {"lock_id": "sibling", "expert_id": "expert-1", "project_id": "proj-1", "task_id": "sp-2:run:old"},
            {"lock_id": "other-expert", "expert_id": "expert-2", "project_id": "proj-1", "task_id": "sp-1:run:old"},
        ],
    )
    monkeypatch.setattr(
        routes_execution.expert_lock,
        "release_lock",
        lambda lock_id: released.append(lock_id) or {"success": True},
    )
    monkeypatch.setattr(
        routes_execution.expert_lock,
        "atomic_claim_lock",
        lambda expert_id, project_id, task_id, scopes: {
            "success": True,
            "lock_id": "fresh-run",
            "leased_until": 456.0,
        },
    )

    result = routes_execution._ensure_agent_run_file_lease(
        ctx, "agent-1", "new-run",
    )

    assert released == ["old-run"]
    assert result["lock_id"] == "fresh-run"
    assert agent["lock_id"] == "fresh-run"
    assert agent["lock_run_id"] == "new-run"
    assert agent["locked_until"] == 456.0


def test_repair_contract_uses_existing_workspace_files(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend/package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "frontend/package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    missing = routes_execution._missing_workspace_delivery_files(
        tmp_path,
        ["backend/package.json", "frontend/package.json", "package.json"],
        ["backend/package.json", "frontend/package.json", "package.json"],
    )

    assert missing == []


def test_repair_contract_accepts_prose_capitalization_for_owned_file(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend/package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "frontend/package.json").write_text("{}", encoding="utf-8")

    missing = routes_execution._missing_workspace_delivery_files(
        tmp_path,
        ["Backend/package.json", "Frontend/package.json"],
        ["backend/package.json", "frontend/package.json"],
    )

    assert missing == []


def test_repair_contract_rejects_absent_and_unsafe_paths(tmp_path):
    assert routes_execution._missing_workspace_delivery_files(
        tmp_path,
        ["missing.json", "../outside.json", "C:/outside.json"],
        ["missing.json", "../outside.json", "C:/outside.json"],
    ) == ["missing.json", "../outside.json", "C:/outside.json"]


def test_repair_contract_does_not_borrow_a_sibling_agents_file(tmp_path):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend/package.json").write_text("{}", encoding="utf-8")

    assert routes_execution._missing_workspace_delivery_files(
        tmp_path,
        ["frontend/package.json"],
        ["backend/package.json"],
    ) == ["frontend/package.json"]


def test_repair_contract_rejects_directories_and_external_symlinks(tmp_path):
    (tmp_path / "directory.json").mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = tmp_path / "linked.json"
    try:
        link.symlink_to(outside)
    except OSError:
        link = None

    required = ["directory.json"] + (["linked.json"] if link is not None else [])
    assert routes_execution._missing_workspace_delivery_files(
        tmp_path,
        required,
        required,
    ) == required


def test_project_is_not_completed_while_planned_phases_are_pending():
    ctx = object.__new__(ProjectContext)
    ctx.status = "running"
    ctx.agents = {"agent-1": {"status": "completed"}}
    subprojects = [
        {"id": "phase-1", "phase_id": "phase-1", "status": "completed"},
        {"id": "phase-2", "phase_id": "phase-2", "status": "pending"},
        {"id": "sp-1", "phase_id": "phase-1", "agent_id": "agent-1", "status": "completed"},
    ]

    assert ctx._derive_project_status(subprojects) == "running"


def test_project_completes_only_after_all_planned_phases_complete():
    ctx = object.__new__(ProjectContext)
    ctx.status = "running"
    ctx.agents = {"agent-1": {"status": "completed"}}
    subprojects = [
        {"id": "phase-1", "phase_id": "phase-1", "status": "completed"},
        {"id": "phase-2", "phase_id": "phase-2", "status": "completed"},
    ]

    assert ctx._derive_project_status(subprojects) == "completed"


def test_serialized_rollup_does_not_false_complete_unaccepted_phase():
    ctx = object.__new__(ProjectContext)
    ctx.project_id = "qa-gate-rollup"
    ctx.status = "running"
    ctx.agents = {"agent-1": {"id": "agent-1", "status": "completed", "progress": 100}}
    ctx.subprojects = [{
        "id": "sp-1", "phase_id": "phase-1", "agent_id": "agent-1",
        "status": "completed", "progress": 100,
    }]
    # This reproduces the production false-pass state: an earlier read had
    # accidentally persisted completed although QA never passed or got user
    # acceptance.
    phase = {
        "phase_id": "phase-1", "subprojects": ["sp-1"],
        "status": "completed", "review_passed": False,
        "user_confirmed": False,
    }
    merged = [dict(item) for item in ctx.subprojects]

    ctx._merge_phase_subprojects(merged, [phase])
    ctx._sync_agent_subproject_status(merged)
    ctx._derive_phase_statuses(merged, [phase])

    phase_row = next(item for item in merged if item.get("id") == "phase-1")
    assert phase_row["status"] == "qa_pending"
    assert ctx._derive_project_status(merged) == "running"
    # Serialization must never rewrite PhaseManager's canonical state.
    assert phase["status"] == "completed"


def test_project_status_uses_canonical_phase_when_agent_id_matches_phase_id():
    ctx = object.__new__(ProjectContext)
    ctx.status = "running"
    ctx.agents = {"agent-1": {"status": "completed"}}
    executable = [{
        "id": "phase-1", "phase_id": "phase-1", "agent_id": "agent-1",
        "status": "completed", "progress": 100,
    }]
    phases = [{
        "phase_id": "phase-1", "status": "qa_pending",
        "review_passed": False, "user_confirmed": False,
    }]

    assert ctx._derive_project_status(executable, phases) == "running"


class _PhaseManagerStub:
    def __init__(self, phase):
        self.phases = [phase]


def _rollup_context():
    ctx = object.__new__(ProjectContext)
    ctx.project_id = "rollup-test"
    ctx.status = "running"
    ctx.agents = {"agent-1": {"status": "completed"}}
    ctx.subprojects = [{
        "id": "sp-1", "phase_id": "phase-1", "agent_id": "agent-1",
        "status": "completed", "progress": 100,
    }]
    return ctx


def test_execution_completion_waits_for_quality_review(monkeypatch):
    phase = {"phase_id": "phase-1", "subprojects": ["sp-1"], "status": "in_progress"}
    ctx = _rollup_context()
    monkeypatch.setitem(routes_execution._phase_managers, ctx.project_id, _PhaseManagerStub(phase))

    routes_execution._refresh_project_rollup(ctx)

    assert phase["status"] == "qa_pending"
    assert ctx.status == "running"


def test_quality_pass_waits_for_user_confirmation(monkeypatch):
    phase = {
        "phase_id": "phase-1", "subprojects": ["sp-1"],
        "status": "reviewing", "reviewed": True, "review_passed": True,
    }
    ctx = _rollup_context()
    monkeypatch.setitem(routes_execution._phase_managers, ctx.project_id, _PhaseManagerStub(phase))

    routes_execution._refresh_project_rollup(ctx)

    assert phase["status"] == "reviewing"
    assert ctx.status == "running"


def test_phase_completes_only_after_user_confirmation(monkeypatch):
    phase = {
        "phase_id": "phase-1", "subprojects": ["sp-1"],
        "status": "reviewing", "reviewed": True, "review_passed": True,
        "user_confirmed": True,
    }
    ctx = _rollup_context()
    monkeypatch.setitem(routes_execution._phase_managers, ctx.project_id, _PhaseManagerStub(phase))

    routes_execution._refresh_project_rollup(ctx)

    assert phase["status"] == "completed"
    assert ctx.status == "completed"


def test_confirmed_phase_is_not_downgraded_by_failed_rework_rollup(monkeypatch):
    phase = {
        "phase_id": "phase-1", "subprojects": ["sp-1"],
        "status": "completed", "reviewed": True, "review_passed": True,
        "user_confirmed": True,
    }
    ctx = _rollup_context()
    ctx.agents["agent-1"]["status"] = "failed"
    ctx.subprojects[0]["status"] = "failed"
    ctx.subprojects[0]["progress"] = 0
    monkeypatch.setitem(routes_execution._phase_managers, ctx.project_id, _PhaseManagerStub(phase))

    routes_execution._refresh_project_rollup(ctx)

    assert phase["status"] == "completed"
    assert phase["progress"] == 100


def test_failed_agent_retry_clears_phase_and_project_failed_projection(monkeypatch):
    phase = {
        "phase_id": "phase-1",
        "subprojects": ["sp-1", "sp-2", "sp-3"],
        "status": "failed",
        "started_at": 1.0,
    }
    ctx = _rollup_context()
    ctx.subprojects = [
        {
            "id": "sp-1", "phase_id": "phase-1", "agent_id": "agent-1",
            "status": "completed", "progress": 100,
        },
        {
            "id": "sp-2", "phase_id": "phase-1", "agent_id": "agent-2",
            "status": "completed", "progress": 100,
        },
        {
            "id": "sp-3", "phase_id": "phase-1", "agent_id": "agent-3",
            "status": "failed", "progress": 0, "error": "model_failed",
        },
    ]
    ctx.agents = {
        "agent-1": {"status": "completed"},
        "agent-2": {"status": "completed"},
        "agent-3": {"status": "failed"},
    }
    monkeypatch.setitem(
        routes_execution._phase_managers,
        ctx.project_id,
        _PhaseManagerStub(phase),
    )

    ctx.agents["agent-3"]["status"] = "working"
    ctx.subprojects[2].update(status="in_progress", progress=10)
    ctx.subprojects[2].pop("error", None)
    routes_execution._refresh_project_rollup(ctx)

    assert phase["status"] == "in_progress"
    assert ctx.status == "running"

    ctx.agents["agent-3"]["status"] = "completed"
    ctx.subprojects[2].update(status="completed", progress=100)
    routes_execution._refresh_project_rollup(ctx)

    assert phase["status"] == "qa_pending"
    assert ctx.status == "running"


def test_ready_phase_starts_quality_cycle_once(monkeypatch):
    phase = {"phase_id": "phase-1", "subprojects": ["sp-1"], "status": "qa_pending"}
    ctx = _rollup_context()
    monkeypatch.setitem(routes_execution._phase_managers, ctx.project_id, _PhaseManagerStub(phase))

    from api import routes_phases
    routes_phases._auto_repair_states.pop(f"{ctx.project_id}-phase-1", None)
    calls = []

    async def fake_start(project_id, phase_id, user_decision=None):
        calls.append((project_id, phase_id))
        routes_phases._auto_repair_states[f"{project_id}-{phase_id}"] = {"status": "starting"}

    monkeypatch.setattr(routes_phases, "start_auto_repair", fake_start)
    asyncio.run(routes_execution._start_phase_quality_cycle_if_ready(ctx))
    asyncio.run(routes_execution._start_phase_quality_cycle_if_ready(ctx))

    assert calls == [(ctx.project_id, "phase-1"), (ctx.project_id, "phase-1")]
    assert phase["status"] == "reviewing"


def test_legacy_default_output_limit_is_increased_fivefold():
    config = {"max_tokens": 4096}

    assert _upgrade_default_token_limit(config) is True
    assert config["max_tokens"] == 20480


def test_custom_output_limit_is_preserved():
    config = {"max_tokens": 8192}

    assert _upgrade_default_token_limit(config) is False
    assert config["max_tokens"] == 8192


def test_manual_agent_rerun_reuses_persisted_execution_contract(monkeypatch, tmp_path):
    project_id = "manual-contract-project"
    agent_id = "agent-frontend"
    captured = {}
    ctx = type("Ctx", (), {})()
    ctx.project_id = project_id
    ctx.workspace = tmp_path
    ctx.description = "fallback project description"
    ctx.pm = type("PM", (), {"context_summary": "fallback context"})()
    ctx.subprojects = [{
        "id": "phase-2",
        "name": "placeholder",
        "description": "placeholder description",
        "tech_stack": [],
    }]
    ctx.agents = {agent_id: {
        "id": agent_id,
        "status": "failed",
        "subproject_id": "phase-2",
        "execution_contract": {
            "subproject_id": "phase-2",
            "subproject_name": "React frontend",
            "description": "full merged React delivery and acceptance contract",
            "tech_stack": ["React", "TypeScript"],
            "project_context": "accepted FastAPI route and schema context",
            "defer_fix_qc": True,
        },
    }}

    async def fake_schedule(payload, *, client_idempotency_key=None):
        captured.update(payload)
        return {
            "run_id": "manual-contract-run",
            "status": "pending",
        }, True

    monkeypatch.setitem(routes_execution.projects, project_id, ctx)
    monkeypatch.setattr(routes_execution, "_schedule_durable_agent_run", fake_schedule)

    result = asyncio.run(routes_execution.execute_agent_task(project_id, agent_id))

    assert result["success"] is True
    assert captured["description"] == "full merged React delivery and acceptance contract"
    assert captured["tech_stack"] == ["React", "TypeScript"]
    assert captured["project_context"] == "accepted FastAPI route and schema context"
    assert captured["subproject_name"] == "React frontend"
    assert captured["defer_fix_qc"] is True


def test_qc_issue_routes_to_file_owner():
    class Ctx:
        agents = {
            "frontend": {"id": "frontend", "phase_id": "p1", "role": "前端", "output_files": ["frontend/App.tsx"]},
            "backend": {"id": "backend", "phase_id": "p1", "role": "后端", "output_files": ["backend/main.py"]},
        }

    owner = routes_phases._match_issue_agent(Ctx(), "p1", {"file_path": "backend/main.py"})

    assert owner["id"] == "backend"


def test_project_access_is_strictly_owner_scoped():
    class Obj:
        def __init__(self, **values):
            self.__dict__.update(values)

    owner = Obj(user_id="user-1", role="user")
    other = Obj(user_id="user-2", role="user")
    admin = Obj(user_id="admin", role="admin")
    project = Obj(owner_user_id="user-1")
    legacy_project = Obj(owner_user_id="")

    assert _can_user_access_project(owner, project)
    assert not _can_user_access_project(other, project)
    assert not _can_user_access_project(owner, legacy_project)
    assert _can_user_access_project(admin, project)
    assert _can_user_access_project(admin, legacy_project)
