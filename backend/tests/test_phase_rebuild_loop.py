import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agents import quality_agents
from api import routes_adjustments, routes_phases, routes_supervisor
from core import runtime_acceptance
from core.hermes_client import current_user_api_config


class _PhaseManager:
    def __init__(self, phase):
        self.phases = [phase]
        self.file_registry = {}

    def get_phase(self, phase_id):
        return next((phase for phase in self.phases if phase["phase_id"] == phase_id), None)

    def get_files_by_phase(self, phase_id):
        return [
            item for item in self.file_registry.values()
            if item.get("phase_id") == phase_id
        ]

    def get_files_by_agent(self, agent_id):
        return [
            item for item in self.file_registry.values()
            if item.get("agent_id") == agent_id
        ]


@pytest.fixture(autouse=True)
def _pass_pre_qa_for_rebuild_loop_tests(monkeypatch):
    """Keep this module focused on QC/rebuild behavior, not deterministic pre-QA."""

    def passed_pre_qa(_ctx, _phase_id):
        return {
            "passed": True,
            "status": "passed",
            "failure_category": "",
            "failed_gate": "",
            "issues": [],
            "evidence": [],
            "consumes_business_qa_round": False,
        }

    monkeypatch.setattr(routes_phases, "_execute_phase_pre_qa", passed_pre_qa)


def test_issue_repair_prefers_agent_that_owns_reported_path():
    ctx = SimpleNamespace(agents={
        "qa": {
            "id": "qa",
            "phase_id": "phase-4",
            "role": "QA engineer",
            "allowed_path_prefixes": ["backend/tests/"],
        },
        "fullstack": {
            "id": "fullstack",
            "phase_id": "phase-4",
            "role": "Full-stack developer",
            "allowed_path_prefixes": ["backend/", "frontend/", "integration/"],
        },
    })

    selected = routes_phases._match_issue_agent(
        ctx,
        "phase-4",
        {
            "file_path": "backend/src/routes/auth.js",
            "responsible_agent_id": "qa",
            "responsible_agent_role": "QA engineer",
        },
    )

    assert selected["id"] == "fullstack"


def test_phase_qc_uses_only_phase_files_and_full_phase_contract(tmp_path, monkeypatch):
    captured = {}

    class FakeQAAgent:
        def __init__(self, **_kwargs):
            pass

        def inspect(self, **kwargs):
            captured.update(kwargs)
            return {
                "passed": True,
                "score": 100,
                "issues": [],
                "issues_detail": [],
                "layer_results": [],
                "user_report": "passed",
                "developer_report": "passed",
                "needs_rewrite": False,
            }

    project_id = "phase-qc-scope-project"
    phase_id = "phase-2"
    phase = {
        "phase_id": phase_id,
        "name": "Frontend",
        "description": "Implement the React TypeScript user interface",
    }
    pm = _PhaseManager(phase)
    pm.phases.insert(0, {"phase_id": "phase-1", "name": "Backend"})
    pm.phases.append({"phase_id": "phase-3", "name": "Integration"})
    pm.file_registry = {
        "backend/app/main.py": {
            "file_path": "backend/app/main.py",
            "phase_id": "phase-1",
            "agent_id": "backend-agent",
        },
    }
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "app" / "main.py").write_text(
        "from fastapi import FastAPI\n", encoding="utf-8"
    )
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    (tmp_path / "frontend" / "src" / "App.tsx").write_text(
        "export default function App(){ return <main /> }\n", encoding="utf-8"
    )
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        subprojects=[{
            # Legacy plan confirmation uses phase_id as the placeholder
            # subproject id; phase QC must not narrow to that one record.
            "id": phase_id,
            "phase_id": phase_id,
            "description": "Render todo input, list, filters and API interactions",
            "agent_id": "frontend-agent",
        }],
        agents={
            "frontend-agent": {
                "id": "frontend-agent",
                "phase_id": phase_id,
                "role": "Frontend Developer",
                "output_files": ["frontend/src/App.tsx"],
            }
        },
        qc_results={},
    )
    routes_supervisor._phase_managers[project_id] = pm
    monkeypatch.setattr(quality_agents, "QAAgent", FakeQAAgent)
    try:
        result = routes_supervisor._run_qc_for_subproject(
            ctx, phase_id, phase["name"]
        )
    finally:
        routes_supervisor._phase_managers.pop(project_id, None)

    assert result["passed"] is True, {
        key: result.get(key)
        for key in ("issues", "qc_execution_error", "qc_input_invalid", "runtime_acceptance")
    }
    assert captured["output_files"] == ["frontend/src/App.tsx"]
    assert "backend/app/main.py" not in captured["output_files"]
    assert captured["dependency_files"] == ["backend/app/main.py"]
    assert "React TypeScript" in captured["subproject_description"]
    assert "todo input, list, filters" in captured["subproject_description"]
    assert captured["agent_role"] == "Frontend Developer"


def test_phase_qc_passes_explicit_architecture_artifact_policy(tmp_path, monkeypatch):
    captured = {}

    class FakeQAAgent:
        def __init__(self, **_kwargs):
            pass

        def inspect(self, **kwargs):
            captured.update(kwargs)
            return {
                "passed": True, "score": 100, "issues": [],
                "layer_results": [], "user_report": "passed",
                "developer_report": "passed", "needs_rewrite": False,
            }

    project_id, phase_id = "architecture-policy-qc", "phase-1"
    phase = {"phase_id": phase_id, "name": "Architecture", "description": "Design contracts"}
    pm = _PhaseManager(phase)
    pm.phases.append({"phase_id": "phase-2", "name": "Implementation"})
    path = "docs/architecture/system-architecture.md"
    pm.file_registry[path] = {
        "file_path": path, "phase_id": phase_id, "agent_id": "architect",
    }
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_text("# Architecture\n", encoding="utf-8")
    ctx = SimpleNamespace(
        project_id=project_id, workspace=tmp_path,
        subprojects=[{"id": phase_id, "phase_id": phase_id}],
        agents={"architect": {
            "id": "architect", "phase_id": phase_id, "role": "Solution Architect",
            "artifact_policy": {"kind": "architecture_document"},
            "output_files": [path],
        }},
        qc_results={},
    )
    routes_supervisor._phase_managers[project_id] = pm
    monkeypatch.setattr(quality_agents, "QAAgent", FakeQAAgent)
    try:
        result = routes_supervisor._run_qc_for_subproject(ctx, phase_id, phase["name"])
    finally:
        routes_supervisor._phase_managers.pop(project_id, None)

    assert result["passed"] is True, {
        key: result.get(key)
        for key in ("issues", "qc_execution_error", "qc_input_invalid", "runtime_acceptance")
    }
    assert captured["artifact_kind"] == "architecture_document"


def test_phase_qc_closes_stale_reviewer_outage_after_layer_recovers(
    tmp_path, monkeypatch,
):
    class FakeQAAgent:
        def __init__(self, **_kwargs):
            pass

        def inspect(self, **_kwargs):
            return {
                "passed": True,
                "score": 100,
                "issues": [],
                "layer_results": [{
                    "layer": "functionality",
                    "passed": True,
                    "score": 100,
                    "issues": [],
                }],
                "user_report": "passed",
                "developer_report": "passed",
                "needs_rewrite": False,
            }

    project_id, phase_id = "reviewer-recovery-qc", "phase-1"
    phase = {"phase_id": phase_id, "name": "Frontend", "description": "Build UI"}
    pm = _PhaseManager(phase)
    pm.phases.append({"phase_id": "phase-2", "name": "Integration"})
    path = "frontend/index.html"
    pm.file_registry[path] = {
        "file_path": path,
        "phase_id": phase_id,
        "agent_id": "frontend-agent",
        "agent_role": "frontend",
    }
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_text("<main>ready</main>", encoding="utf-8")
    stale = {
        "message": "Functionality review could not produce valid acceptance evidence (ValueError)",
        "file_path": path,
        "severity": "error",
        "layer": "functionality",
        "status": "open",
        "requires_identity_review": True,
    }
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        subprojects=[{"id": phase_id, "phase_id": phase_id}],
        agents={"frontend-agent": {
            "id": "frontend-agent",
            "phase_id": phase_id,
            "role": "frontend",
            "output_files": [path],
        }},
        qc_results={phase_id: {"qa": {"issues_detail": [stale], "qc_round": 1}}},
    )
    routes_supervisor._phase_managers[project_id] = pm
    monkeypatch.setattr(quality_agents, "QAAgent", FakeQAAgent)
    try:
        result = routes_supervisor._run_qc_for_subproject(
            ctx, phase_id, phase["name"]
        )
    finally:
        routes_supervisor._phase_managers.pop(project_id, None)

    assert result["passed"] is True, result
    assert result["issues_detail"][0]["status"] == "fixed"
    assert result["issues_detail"][0]["verification_result"] == "reviewer_recovered"


def test_mixed_phase_does_not_inherit_architecture_artifact_policy(tmp_path, monkeypatch):
    captured = {}

    class FakeQAAgent:
        def __init__(self, **_kwargs):
            pass

        def inspect(self, **kwargs):
            captured.update(kwargs)
            return {
                "passed": True, "score": 100, "issues": [],
                "layer_results": [], "user_report": "passed",
                "developer_report": "passed", "needs_rewrite": False,
            }

    project_id, phase_id = "mixed-policy-qc", "phase-1"
    phase = {"phase_id": phase_id, "name": "Mixed", "description": "Design and build"}
    pm = _PhaseManager(phase)
    pm.phases.append({"phase_id": "phase-2", "name": "Integration"})
    paths = ["docs/architecture/system.md", "backend/app.py"]
    for path, agent_id in zip(paths, ("architect", "backend")):
        pm.file_registry[path] = {
            "file_path": path, "phase_id": phase_id, "agent_id": agent_id,
        }
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# design\n" if path.endswith(".md") else "print('ok')\n", encoding="utf-8")
    ctx = SimpleNamespace(
        project_id=project_id, workspace=tmp_path,
        subprojects=[{"id": phase_id, "phase_id": phase_id}],
        agents={
            "architect": {
                "id": "architect", "phase_id": phase_id, "role": "Architect",
                "artifact_policy": {"kind": "architecture_document"},
                "output_files": [paths[0]],
            },
            "backend": {
                "id": "backend", "phase_id": phase_id, "role": "Backend Developer",
                "output_files": [paths[1]],
            },
        },
        qc_results={},
    )
    routes_supervisor._phase_managers[project_id] = pm
    monkeypatch.setattr(quality_agents, "QAAgent", FakeQAAgent)
    monkeypatch.setattr(
        runtime_acceptance,
        "run_runtime_acceptance",
        lambda *_args, **_kwargs: {
            "enabled": True,
            "passed": True,
            "status": "passed",
            "summary": "fixture runtime acceptance passed",
            "logs": [],
        },
    )
    try:
        result = routes_supervisor._run_qc_for_subproject(ctx, phase_id, phase["name"])
    finally:
        routes_supervisor._phase_managers.pop(project_id, None)

    assert result["passed"] is True, result
    assert captured["artifact_kind"] == ""


def test_whole_project_qc_uses_registered_delivery_files(tmp_path, monkeypatch):
    captured = {}

    class FakeQAAgent:
        def __init__(self, **_kwargs):
            pass

        def inspect(self, **kwargs):
            captured.update(kwargs)
            return {
                "passed": True, "score": 100, "issues": [],
                "layer_results": [], "user_report": "passed",
                "developer_report": "passed", "needs_rewrite": False,
            }

    project_id = "whole-project-registry-qc"
    pm = _PhaseManager({"phase_id": "phase-1", "name": "Build"})
    paths = ["Dockerfile", "infra/main.tf", "docs/architecture/system.md"]
    for path in paths:
        pm.file_registry[path] = {"file_path": path, "phase_id": "phase-1"}
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("delivery\n", encoding="utf-8")
    ctx = SimpleNamespace(
        project_id=project_id, workspace=tmp_path,
        subprojects=[],
        agents={"legacy": {"output_files": ["backend/src/missing-legacy.js"]}},
        qc_results={},
    )
    routes_supervisor._phase_managers[project_id] = pm
    monkeypatch.setattr(quality_agents, "QAAgent", FakeQAAgent)
    try:
        result = routes_supervisor._run_qc_for_subproject(
            ctx, "__whole_project__", "Whole project", is_final_phase=True,
        )
    finally:
        routes_supervisor._phase_managers.pop(project_id, None)

    assert result["passed"] is True
    assert captured["output_files"] == paths


def test_unlocated_qc_failure_retries_before_finishing(monkeypatch, tmp_path):
    project_id, phase_id = "unlocated-qc-project", "phase-1"
    phase = {"phase_id": phase_id, "name": "Architecture", "status": "reviewing"}
    pm = _PhaseManager(phase)
    ctx = SimpleNamespace(project_id=project_id, workspace=tmp_path)
    key = f"{project_id}-{phase_id}"
    routes_phases._phase_managers[project_id] = pm
    routes_phases.projects[project_id] = ctx
    routes_phases._auto_repair_states[key] = {
        "running": True, "round": 0, "status": "starting",
        "messages": [], "phase_name": phase["name"],
    }

    qc_calls = []

    def unlocated_then_passes(*_args, **_kwargs):
        qc_calls.append(len(qc_calls) + 1)
        if len(qc_calls) == 1:
            return {
                "passed": False, "score": 90, "error_count": 1,
                "warning_count": 0, "issues": ["Quality provider timed out"],
                "issues_detail": [{
                    "file_path": "", "severity": "error", "status": "open",
                    "message": "Quality provider timed out",
                }],
            }
        return {
            "passed": True, "score": 100, "error_count": 0,
            "warning_count": 0, "issues": [], "issues_detail": [],
        }

    async def must_not_repair(*_args, **_kwargs):
        pytest.fail("an unlocated QA failure must not execute a delivery agent")

    async def no_persist():
        return None

    monkeypatch.setattr(routes_supervisor, "_run_qc_for_subproject", unlocated_then_passes)
    monkeypatch.setattr(routes_phases, "_repair_all_issue_owners", must_not_repair)
    monkeypatch.setattr(routes_phases, "_persist_all_async", no_persist)
    try:
        asyncio.run(routes_phases._run_auto_repair_loop(project_id, phase_id, False))
        state = routes_phases._auto_repair_states[key]
        assert qc_calls == [1, 2]
        assert state["status"] == "passed"
        assert state["running"] is False
        assert phase["review_passed"] is True
    finally:
        routes_phases._phase_managers.pop(project_id, None)
        routes_phases.projects.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)


def test_persistent_unlocated_qc_failure_blocks_after_retry(monkeypatch, tmp_path):
    project_id, phase_id = "persistent-unlocated-qc", "phase-1"
    phase = {"phase_id": phase_id, "name": "Architecture", "status": "reviewing"}
    pm = _PhaseManager(phase)
    ctx = SimpleNamespace(project_id=project_id, workspace=tmp_path)
    key = f"{project_id}-{phase_id}"
    routes_phases._phase_managers[project_id] = pm
    routes_phases.projects[project_id] = ctx
    routes_phases._auto_repair_states[key] = {
        "running": True, "round": 0, "status": "starting",
        "messages": [], "phase_name": phase["name"],
    }
    qc_calls = []

    def unlocated_qc(*_args, **_kwargs):
        qc_calls.append(len(qc_calls) + 1)
        return {
            "passed": False, "score": 90, "error_count": 1,
            "warning_count": 0, "issues": ["Quality provider timed out"],
            "issues_detail": [{
                "file_path": "", "severity": "error", "status": "open",
                "message": "Quality provider timed out",
            }],
        }

    async def must_not_repair(*_args, **_kwargs):
        pytest.fail("an unlocated QA failure must not execute a delivery agent")

    async def no_persist():
        return None

    monkeypatch.setattr(routes_supervisor, "_run_qc_for_subproject", unlocated_qc)
    monkeypatch.setattr(routes_phases, "_repair_all_issue_owners", must_not_repair)
    monkeypatch.setattr(routes_phases, "_persist_all_async", no_persist)
    try:
        asyncio.run(routes_phases._run_auto_repair_loop(project_id, phase_id, False))
        state = routes_phases._auto_repair_states[key]
        assert qc_calls == [1, 2]
        assert state["status"] == "qa_blocked"
        assert state["running"] is False
        assert "Quality provider timed out" in state["action_required"]["message"]
        assert phase["status"] == "qa_blocked"
    finally:
        routes_phases._phase_managers.pop(project_id, None)
        routes_phases.projects.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)


def test_final_phase_qc_uses_all_accepted_phase_files(tmp_path, monkeypatch):
    captured = {}

    class FakeQAAgent:
        def __init__(self, **_kwargs):
            pass

        def inspect(self, **kwargs):
            captured.update(kwargs)
            return {
                "passed": True, "score": 100, "issues": [],
                "layer_results": [], "user_report": "passed",
                "developer_report": "passed", "needs_rewrite": False,
            }

    project_id = "final-phase-full-registry"
    phase3 = {"phase_id": "phase-3", "name": "Integration", "description": "Verify full product"}
    pm = _PhaseManager(phase3)
    pm.phases = [
        {"phase_id": "phase-1", "name": "Backend"},
        {"phase_id": "phase-2", "name": "Frontend"},
        phase3,
    ]
    pm.file_registry = {
        "backend/app/main.py": {"file_path": "backend/app/main.py", "phase_id": "phase-1", "agent_id": "backend"},
        "frontend/src/App.tsx": {"file_path": "frontend/src/App.tsx", "phase_id": "phase-2", "agent_id": "frontend"},
        "integration/README.md": {"file_path": "integration/README.md", "phase_id": "phase-3", "agent_id": "fullstack"},
    }
    for path, content in {
        "backend/app/main.py": "print('backend')\n",
        "frontend/src/App.tsx": "export default function App(){ return <main /> }\n",
        "integration/README.md": "# Run\n",
        "integration/contract.json": "{}\n",
    }.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    ctx = SimpleNamespace(
        project_id=project_id, workspace=tmp_path,
        subprojects=[{"id": "phase-3", "phase_id": "phase-3"}],
        agents={"fullstack": {
            "id": "fullstack", "phase_id": "phase-3", "role": "Full-stack Developer",
            "required_delivery_files": ["integration/contract.json"],
        }},
        qc_results={},
    )
    routes_supervisor._phase_managers[project_id] = pm
    monkeypatch.setattr(quality_agents, "QAAgent", FakeQAAgent)
    monkeypatch.setattr(
        runtime_acceptance,
        "run_runtime_acceptance",
        lambda *_args, **_kwargs: {
            "enabled": True,
            "passed": True,
            "status": "passed",
            "summary": "fixture runtime acceptance passed",
            "logs": [],
        },
    )
    try:
        result = routes_supervisor._run_qc_for_subproject(ctx, "phase-3", "Integration")
    finally:
        routes_supervisor._phase_managers.pop(project_id, None)

    assert result["passed"] is True
    assert captured["is_final_phase"] is True
    assert captured["output_files"] == [
        "backend/app/main.py", "frontend/src/App.tsx", "integration/README.md",
        "integration/contract.json",
    ]


def test_final_phase_runtime_failure_returns_product_code_to_its_engineer(
    tmp_path, monkeypatch
):
    class FakeQAAgent:
        def __init__(self, **_kwargs):
            pass

        def inspect(self, **_kwargs):
            return {
                "passed": True, "score": 100, "issues": [],
                "layer_results": [], "user_report": "static passed",
                "developer_report": "static passed", "needs_rewrite": False,
            }

    project_id = "runtime-repair-routing"
    phase3 = {"phase_id": "phase-3", "name": "Integration", "description": "Run product"}
    pm = _PhaseManager(phase3)
    pm.phases = [
        {"phase_id": "phase-1", "name": "Backend"},
        {"phase_id": "phase-2", "name": "Frontend"},
        phase3,
    ]
    pm.file_registry = {
        "backend/src/routes/tickets.js": {
            "file_path": "backend/src/routes/tickets.js",
            "phase_id": "phase-1",
            "agent_id": "backend-agent",
            "agent_role": "Backend Developer",
        },
        "backend/tests/tickets.test.js": {
            "file_path": "backend/tests/tickets.test.js",
            "phase_id": "phase-1",
            "agent_id": "qa-agent",
            "agent_role": "QA Engineer",
        },
        "Dockerfile": {
            "file_path": "Dockerfile",
            "phase_id": "phase-3",
            "agent_id": "devops-agent",
            "agent_role": "DevOps Engineer",
        },
    }
    for path in pm.file_registry:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("// fixture\n", encoding="utf-8")
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        subprojects=[{"id": "phase-3", "phase_id": "phase-3"}],
        agents={
            "backend-agent": {"id": "backend-agent", "phase_id": "phase-1", "role": "Backend Developer"},
            "qa-agent": {"id": "qa-agent", "phase_id": "phase-1", "role": "QA Engineer"},
            "devops-agent": {"id": "devops-agent", "phase_id": "phase-3", "role": "DevOps Engineer"},
        },
        qc_results={},
    )
    monkeypatch.setattr(quality_agents, "QAAgent", FakeQAAgent)
    monkeypatch.setattr(
        runtime_acceptance,
        "run_runtime_acceptance",
        lambda *_args, **_kwargs: {
            "enabled": True,
            "passed": False,
            "status": "build_failed",
            "summary": "npm test exited with code 1",
            "logs": [
                "build: FAIL backend/tests/tickets.test.js",
                "build: at Object.<anonymous> (tests/tickets.test.js:42:7)",
                "build: Expected: 200",
                "build: Received: 403",
            ],
        },
    )
    monkeypatch.setattr(routes_adjustments, "_runtime_acceptance_required", lambda: True)
    routes_supervisor._phase_managers[project_id] = pm
    try:
        result = routes_supervisor._run_qc_for_subproject(
            ctx,
            "phase-3",
            "Integration",
            qa_context={"run_runtime_inside_qc": True},
        )
    finally:
        routes_supervisor._phase_managers.pop(project_id, None)

    issue = next(
        item for item in result["issues_detail"]
        if item.get("layer") == "runtime_acceptance"
    )
    assert result["passed"] is False
    assert result["runtime_acceptance"]["passed"] is False
    assert issue["file_path"] == "backend/src/routes/tickets.js"
    assert issue["responsible_agent_id"] == "backend-agent"
    assert "backend/tests/tickets.test.js" in issue["message"]
    assert "do not weaken or delete the failing test" in issue["fix_hint"]

    monkeypatch.setattr(
        runtime_acceptance,
        "run_runtime_acceptance",
        lambda *_args, **_kwargs: {
            "enabled": True,
            "passed": True,
            "status": "passed",
            "summary": "tests, startup and health checks passed",
            "logs": ["build: all tests passed"],
        },
    )
    routes_supervisor._phase_managers[project_id] = pm
    try:
            recheck = routes_supervisor._run_qc_for_subproject(
                ctx,
                "phase-3",
                "Integration",
                qa_context={"run_runtime_inside_qc": True},
            )
    finally:
        routes_supervisor._phase_managers.pop(project_id, None)

    assert recheck["passed"] is True
    assert recheck["runtime_acceptance"]["passed"] is True
    assert next(
        item for item in recheck["issues_detail"]
        if item.get("layer") == "runtime_acceptance"
    )["status"] == "fixed"
    assert (tmp_path / "backend/tests/tickets.test.js").read_text(
        encoding="utf-8"
    ) == "// fixture\n"
    assert routes_supervisor._runtime_repair_target(
        pm,
        "backend/tests/tickets.test.js",
        "Test suite failed to run: SyntaxError in the test file",
    ) == "backend/tests/tickets.test.js"


def test_runtime_repair_target_prefers_registered_source_from_stack(tmp_path):
    source = tmp_path / "backend/src/db.js"
    source.parent.mkdir(parents=True)
    source.write_text("export const db = true;\n", encoding="utf-8")
    package = tmp_path / "package.json"
    package.write_text("{}\n", encoding="utf-8")
    manager = SimpleNamespace(
        workspace=tmp_path,
        file_registry={
            "backend/src/db.js": {"agent_id": "backend"},
            "package.json": {"file_path": "package.json", "agent_id": "fullstack"},
        },
    )

    assert routes_supervisor._runtime_repair_target(
        manager,
        "package.json",
        "TypeError: Cannot open database\n    at file:///app/backend/src/db.js:12:10",
    ) == "backend/src/db.js"


def test_runtime_repair_target_does_not_guess_for_unlocated_syntax_error(tmp_path):
    for path in ("backend/src/db.js", "integration/start.js", "package.json"):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
    manager = SimpleNamespace(
        workspace=tmp_path,
        file_registry={path: {"file_path": path} for path in (
            "backend/src/db.js", "integration/start.js", "package.json"
        )},
    )

    assert routes_supervisor._runtime_repair_target(
        manager,
        "package.json",
        "SyntaxError: Unexpected end of input\n    at ModuleLoader.import",
    ) is None


def test_runtime_repair_target_recognizes_root_python_test(tmp_path):
    for path in ("tests/test_users.py", "backend/src/users.py"):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# fixture\n", encoding="utf-8")
    manager = SimpleNamespace(
        workspace=tmp_path,
        file_registry={path: {"file_path": path} for path in (
            "tests/test_users.py", "backend/src/users.py"
        )},
    )

    assert routes_supervisor._runtime_repair_target(
        manager,
        "tests/test_users.py",
        "assert response.status_code == 200; received 403",
    ) == "backend/src/users.py"


def test_final_phase_non_actionable_rejection_defers_to_runtime_gate(
    tmp_path, monkeypatch
):
    marker = "Functionality review rejected the delivery without a usable blocking finding"

    class InconclusiveQAAgent:
        def __init__(self, **_kwargs):
            pass

        def inspect(self, **_kwargs):
            issue = {
                "file": "backend/src/app.js",
                "layer": "functionality",
                "severity": "error",
                "message": marker,
                "fix_hint": "Rerun the functionality review",
            }
            return {
                "passed": False, "score": 90, "issues": [marker],
                "layer_results": [{"layer": "functionality", "issues": [issue]}],
                "user_report": "inconclusive", "developer_report": "inconclusive",
                "needs_rewrite": False,
            }

    project_id = "inconclusive-final-review"
    phase = {"phase_id": "phase-3", "name": "Integration"}
    pm = _PhaseManager(phase)
    pm.file_registry = {
        "backend/src/app.js": {
            "file_path": "backend/src/app.js",
            "phase_id": "phase-3",
            "agent_id": "backend-agent",
            "agent_role": "Backend Developer",
        },
    }
    app_file = tmp_path / "backend/src/app.js"
    app_file.parent.mkdir(parents=True)
    app_file.write_text("module.exports = {};\n", encoding="utf-8")
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        subprojects=[{"id": "phase-3", "phase_id": "phase-3"}],
        agents={
            "backend-agent": {
                "id": "backend-agent", "phase_id": "phase-3",
                "role": "Backend Developer",
            },
        },
        qc_results={},
    )
    monkeypatch.setattr(quality_agents, "QAAgent", InconclusiveQAAgent)
    monkeypatch.setattr(
        runtime_acceptance,
        "run_runtime_acceptance",
        lambda *_args, **_kwargs: {
            "enabled": True,
            "passed": True,
            "status": "passed",
            "summary": "tests, startup and health checks passed",
            "logs": ["build: all tests passed"],
        },
    )
    monkeypatch.setattr(routes_adjustments, "_runtime_acceptance_required", lambda: True)
    routes_supervisor._phase_managers[project_id] = pm
    try:
        result = routes_supervisor._run_qc_for_subproject(
            ctx,
            "phase-3",
            "Integration",
            qa_context={"run_runtime_inside_qc": True},
        )
    finally:
        routes_supervisor._phase_managers.pop(project_id, None)

    assert result["passed"] is True
    assert result["runtime_acceptance"]["passed"] is True
    assert result["error_count"] == 0
    assert result["warning_count"] == 1
    warning = next(
        item for item in result["issues_detail"]
        if item.get("layer") == "functionality"
    )
    assert warning["severity"] == "warning"
    assert "isolated runtime acceptance" in warning["message"]


def test_phase_qc_blocks_cross_phase_api_route_mismatch(tmp_path, monkeypatch):
    project_id = "cross-phase-contract-project"
    backend_route = tmp_path / "backend" / "app" / "api" / "todos.py"
    frontend_service = tmp_path / "frontend" / "src" / "todoService.ts"
    vite_config = tmp_path / "frontend" / "vite.config.ts"
    backend_route.parent.mkdir(parents=True)
    frontend_service.parent.mkdir(parents=True)
    backend_route.write_text(
        'from fastapi import APIRouter\nrouter = APIRouter()\n@router.get("/todos")\ndef list_todos(): return []\n',
        encoding="utf-8",
    )
    frontend_service.write_text(
        "import axios from 'axios';\nconst api = axios.create({ baseURL: '/api' });\napi.get('/todos');\n",
        encoding="utf-8",
    )
    vite_config.write_text(
        "export default { server: { proxy: { '/api': { target: 'http://localhost:8000' } } } };\n",
        encoding="utf-8",
    )

    phase2 = {"phase_id": "phase-2", "name": "Frontend", "description": "React frontend API"}
    pm = _PhaseManager(phase2)
    pm.phases.insert(0, {"phase_id": "phase-1", "name": "Backend"})
    pm.file_registry = {
        "backend/app/api/todos.py": {
            "file_path": "backend/app/api/todos.py", "phase_id": "phase-1", "agent_id": "backend-agent",
        },
        "frontend/src/todoService.ts": {
            "file_path": "frontend/src/todoService.ts", "phase_id": "phase-2", "agent_id": "frontend-agent",
        },
        "frontend/vite.config.ts": {
            "file_path": "frontend/vite.config.ts", "phase_id": "phase-2", "agent_id": "frontend-agent",
        },
    }
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        subprojects=[{"id": "phase-2", "phase_id": "phase-2", "description": "Call backend API"}],
        agents={"frontend-agent": {"id": "frontend-agent", "phase_id": "phase-2", "role": "Frontend Developer"}},
        qc_results={},
    )
    monkeypatch.setattr(
        quality_agents,
        "check_layer3_functionality",
        lambda *_args, **_kwargs: {"layer": "functionality", "passed": True, "score": 100, "issues": [], "issue_count": 0},
    )
    routes_supervisor._phase_managers[project_id] = pm
    try:
        result = routes_supervisor._run_qc_for_subproject(ctx, "phase-2", "Frontend")
    finally:
        routes_supervisor._phase_managers.pop(project_id, None)

    assert result["passed"] is False
    contract_layer = next(layer for layer in result["layer_results"] if layer["layer"] == "api_contract")
    assert contract_layer["passed"] is False
    assert contract_layer["issues"][0]["file"] == "frontend/vite.config.ts"


def test_phase_qc_without_deliverables_fails_without_scanning_other_phases(
    tmp_path, monkeypatch
):
    class UnexpectedQAAgent:
        def __init__(self, **_kwargs):
            raise AssertionError("QA must not scan the workspace for an empty phase")

    project_id = "empty-phase-qc-project"
    phase_id = "phase-2"
    phase = {
        "phase_id": phase_id,
        "name": "Frontend",
        "description": "Implement frontend",
    }
    pm = _PhaseManager(phase)
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "main.py").write_text("print('old')", encoding="utf-8")
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        subprojects=[{"id": phase_id, "phase_id": phase_id}],
        agents={},
        qc_results={},
    )
    routes_supervisor._phase_managers[project_id] = pm
    monkeypatch.setattr(quality_agents, "QAAgent", UnexpectedQAAgent)
    try:
        result = routes_supervisor._run_qc_for_subproject(
            ctx, phase_id, phase["name"]
        )
    finally:
        routes_supervisor._phase_managers.pop(project_id, None)

    assert result["passed"] is False
    assert result["error_count"] == 1
    assert "没有已登记的交付文件" in result["developer_report"]


def test_warning_is_reported_but_does_not_block_phase(tmp_path, monkeypatch):
    class FakeQAAgent:
        def __init__(self, **_kwargs):
            pass

        def inspect(self, **_kwargs):
            issue = {
                "message": "One advisory warning",
                "file": "src/app.py",
                "severity": "warning",
                "fix_hint": "Optional cleanup",
                "layer": "functionality",
            }
            return {
                "passed": False,
                "score": 90,
                "issues": [issue["message"]],
                "issues_detail": [issue],
                "layer_results": [{"layer": "functionality", "issues": [issue]}],
                "user_report": "warning",
                "developer_report": "warning",
                "needs_rewrite": False,
            }

    monkeypatch.setattr(quality_agents, "QAAgent", FakeQAAgent)
    ctx = SimpleNamespace(
        project_id="warning-only-project",
        workspace=tmp_path,
        subprojects=[{
            "id": "phase-1",
            "phase_id": "phase-1",
            "description": "test",
            "agent_id": "agent-1",
        }],
        agents={"agent-1": {"id": "agent-1", "role": "QA"}},
        qc_results={},
    )

    result = routes_supervisor._run_qc_for_subproject(ctx, "phase-1", "Phase 1")

    assert result["passed"] is True
    assert result["status"] == "passed"
    assert result["error_count"] == 0
    assert result["warning_count"] == 1
    assert result["score"] == 98


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/app.py", True),
        ("README.md", True),
        ("output/phase-1_execution.log", False),
        (".project/versions/1/commit.json", False),
        ("debug.log", False),
        ("../outside.py", False),
        ("C:/outside.py", False),
        ("", False),
    ],
)
def test_rebuild_manifest_excludes_runner_metadata(path, expected):
    assert routes_phases._is_rebuild_deliverable_path(path) is expected


def test_restarting_qc_after_manual_edit_preserves_cumulative_round(monkeypatch, tmp_path):
    project_id, phase_id = "restart-project", "phase-1"
    phase = {"phase_id": phase_id, "name": "Phase 1"}
    pm = _PhaseManager(phase)
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        agents={
            "restart-agent": {
                "id": "restart-agent",
                "phase_id": phase_id,
                "subproject_id": "restart-subproject",
                "status": "completed",
            }
        },
        subprojects=[{
            "id": "restart-subproject",
            "phase_id": phase_id,
            "agent_id": "restart-agent",
            "status": "completed",
        }],
    )
    key = f"{project_id}-{phase_id}"
    routes_phases._phase_managers[project_id] = pm
    routes_phases._auto_repair_states[key] = {
        "running": False,
        "round": 5,
        "status": "awaiting_manual_edit",
        "messages": [],
        "phase_name": "Phase 1",
        "action_required": {"options": ["manual_edit", "retry_cycle", "rebuild_phase"]},
        "needs_manual": True,
    }
    monkeypatch.setattr(routes_phases, "_get_project", lambda _project_id: ctx)

    def discard_task(coro, name=""):
        coro.close()

    monkeypatch.setattr(routes_phases, "_safe_create_task", discard_task)
    try:
        result = asyncio.run(routes_phases.start_auto_repair(project_id, phase_id))
        state = result["status"]
        assert state["running"] is True
        assert state["round"] == 5
        assert state["needs_manual"] is False
        assert state["action_required"] is None
    finally:
        routes_phases._phase_managers.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)
        routes_phases._auto_repair_api_configs.pop(key, None)


def test_rebuild_without_api_key_keeps_phase_untouched(monkeypatch, tmp_path):
    project_id, phase_id = "no-key-project", "phase-1"
    phase = {"phase_id": phase_id, "name": "Phase 1", "description": "original"}
    original = dict(phase)
    pm = _PhaseManager(phase)
    ctx = SimpleNamespace(project_id=project_id, workspace=tmp_path, agents={})
    key = f"{project_id}-{phase_id}"
    routes_phases._phase_managers[project_id] = pm
    routes_phases._auto_repair_states[key] = {
        "running": False,
        "round": 10,
        "repair_attempts": 10,
        "total_rounds": 14,
        "lifetime_qc_runs": 14,
        "status": "awaiting_decision",
        "messages": [],
        "phase_name": "Phase 1",
        "issue_report": {"src/app.py": [{"severity": "error"}]},
    }
    monkeypatch.setattr(routes_phases, "_get_project", lambda _project_id: ctx)
    monkeypatch.setattr(routes_phases.hermes_client, "api_key", "")
    token = current_user_api_config.set(None)
    try:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(routes_phases.start_auto_repair(project_id, phase_id, "rebuild_phase"))
        assert exc_info.value.status_code == 409
        assert phase == original
    finally:
        current_user_api_config.reset(token)
        routes_phases._phase_managers.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)
        routes_phases._auto_repair_api_configs.pop(key, None)
        routes_phases._auto_repair_api_configs.pop(key, None)


@pytest.mark.parametrize("placeholder", ["", "unknown", "unknown file", "未定位文件"])
def test_rebuild_rejects_unlocated_blocking_issue(monkeypatch, tmp_path, placeholder):
    project_id, phase_id = f"unlocated-{placeholder or 'empty'}", "phase-1"
    phase = {"phase_id": phase_id, "name": "Phase 1", "description": "original"}
    pm = _PhaseManager(phase)
    ctx = SimpleNamespace(project_id=project_id, workspace=tmp_path, agents={})
    key = f"{project_id}-{phase_id}"
    routes_phases._phase_managers[project_id] = pm
    routes_phases._auto_repair_states[key] = {
        "running": False,
        "round": 5,
        "status": "awaiting_decision",
        "messages": [],
        "phase_name": "Phase 1",
        "issue_report": {
            placeholder: [{
                "file_path": placeholder,
                "severity": "critical",
                "status": "open",
                "message": "must be localized before rebuild",
            }],
        },
    }
    monkeypatch.setattr(routes_phases, "_get_project", lambda _project_id: ctx)
    monkeypatch.setenv("METIS_TEST_MODE", "true")
    try:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(routes_phases.start_auto_repair(project_id, phase_id, "rebuild_phase"))
        assert exc_info.value.status_code == 409
        assert routes_phases._auto_repair_states[key]["status"] == "qa_blocked"
        assert phase["description"] == "original"
    finally:
        routes_phases._phase_managers.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)
        routes_phases._auto_repair_api_configs.pop(key, None)


def test_initial_phase_start_without_api_key_is_atomic(monkeypatch, tmp_path):
    project_id, phase_id = "initial-no-key-project", "phase-1"
    phase = {
        "phase_id": phase_id,
        "name": "Phase 1",
        "description": "Build it",
        "status": "pending",
        "agents": [],
    }
    original_phase = dict(phase)
    pm = _PhaseManager(phase)
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        status="planning",
        agents={},
        subprojects=[],
    )
    routes_phases._phase_managers[project_id] = pm
    monkeypatch.setattr(routes_phases, "_get_project", lambda _project_id: ctx)
    monkeypatch.setattr(routes_phases.hermes_client, "api_key", "")
    monkeypatch.delenv("METIS_TEST_MODE", raising=False)
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "create_lock",
        lambda **_kwargs: pytest.fail("lock must not be created without an API key"),
    )
    token = current_user_api_config.set(None)
    try:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(routes_phases.start_phase(project_id, phase_id))
        assert exc_info.value.status_code == 409
        assert phase == original_phase
        assert ctx.status == "planning"
        assert ctx.agents == {}
        assert ctx.subprojects == []
    finally:
        current_user_api_config.reset(token)
        routes_phases._phase_managers.pop(project_id, None)


def test_phase_start_key_guard_allows_explicit_test_mode(monkeypatch):
    monkeypatch.setenv("METIS_TEST_MODE", "true")
    monkeypatch.setattr(routes_phases.hermes_client, "api_key", "")
    token = current_user_api_config.set(None)
    try:
        assert routes_phases._phase_execution_has_api_key() is True
    finally:
        current_user_api_config.reset(token)


def test_warning_only_auto_repair_loop_finishes_without_rebuild(monkeypatch, tmp_path):
    project_id, phase_id = "warning-loop-project", "phase-1"
    phase = {"phase_id": phase_id, "name": "Phase 1", "status": "reviewing"}
    pm = _PhaseManager(phase)
    ctx = SimpleNamespace(project_id=project_id, workspace=tmp_path)
    key = f"{project_id}-{phase_id}"
    routes_phases._phase_managers[project_id] = pm
    routes_phases.projects[project_id] = ctx
    routes_phases._auto_repair_states[key] = {
        "running": True,
        "round": 0,
        "status": "starting",
        "messages": [],
        "phase_name": "Phase 1",
    }

    def warning_qc(_ctx, _phase_id, _phase_name, *_args, **_kwargs):
        return {
            "passed": True,
            "score": 98,
            "error_count": 0,
            "warning_count": 1,
            "issues": ["advisory"],
            "issues_detail": [{"severity": "warning", "status": "open", "message": "advisory"}],
        }

    async def no_persist():
        return None

    monkeypatch.setattr(routes_supervisor, "_run_qc_for_subproject", warning_qc)
    monkeypatch.setattr(routes_phases, "_persist_all_async", no_persist)
    try:
        asyncio.run(routes_phases._run_auto_repair_loop(project_id, phase_id, False))
        state = routes_phases._auto_repair_states[key]
        assert state["status"] == "passed"
        assert state["running"] is False
        assert state["round"] == 0
        assert state["lifetime_qc_runs"] == 1
        assert state["review_result"]["passed"] is True
        assert state["review_result"]["phase_id"] == phase_id
        assert phase["review_passed"] is True
    finally:
        routes_phases._phase_managers.pop(project_id, None)
        routes_phases.projects.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)


def test_failed_agent_repair_stops_loop_before_next_quality_check(monkeypatch, tmp_path):
    """A failed repair cannot be hidden by a later QC pass."""
    from api import routes_execution

    project_id, phase_id = "failed-repair-project", "phase-1"
    phase = {"phase_id": phase_id, "name": "Backend", "status": "reviewing"}
    pm = _PhaseManager(phase)
    agent = {
        "id": "agent-qa",
        "phase_id": phase_id,
        "subproject_id": "sp-tests",
        "subproject_name": "API tests",
        "role": "QA Engineer",
        "status": "completed",
        "output_files": ["backend/tests/test_todos.py"],
    }
    subproject = {
        "id": "sp-tests",
        "phase_id": phase_id,
        "agent_id": agent["id"],
        "status": "completed",
        "progress": 100,
    }
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        description="Todo API",
        pm=SimpleNamespace(context_summary="context"),
        agents={agent["id"]: agent},
        subprojects=[subproject],
    )
    key = f"{project_id}-{phase_id}"
    routes_phases._phase_managers[project_id] = pm
    routes_phases.projects[project_id] = ctx
    routes_phases._auto_repair_states[key] = {
        "running": True,
        "round": 0,
        "status": "starting",
        "messages": [],
        "phase_name": phase["name"],
    }

    qc_calls = []

    def qc_result(*_args, **_kwargs):
        qc_calls.append(len(qc_calls) + 1)
        if len(qc_calls) == 1:
            return {
                "passed": False,
                "score": 90,
                "error_count": 1,
                "warning_count": 0,
                "issues": ["Generated API test cannot run"],
                "issues_detail": [{
                    "file_path": "backend/tests/test_todos.py",
                    "severity": "error",
                    "message": "Generated API test cannot run",
                    "status": "open",
                    "responsible_agent_id": agent["id"],
                    "responsible_agent_role": agent["role"],
                }],
            }
        return {
            "passed": True,
            "score": 100,
            "error_count": 0,
            "warning_count": 0,
            "issues": [],
            "issues_detail": [],
        }

    async def failed_repair(**_kwargs):
        agent["status"] = "failed"
        subproject["status"] = "failed"
        return {
            "success": False,
            "status": "failed",
            "error": "repair delivery validation failed",
        }

    async def no_persist():
        return None

    monkeypatch.setattr(routes_supervisor, "_run_qc_for_subproject", qc_result)
    monkeypatch.setattr(routes_execution, "_run_agent_task", failed_repair)
    monkeypatch.setattr(routes_phases, "_persist_all_async", no_persist)
    try:
        asyncio.run(routes_phases._run_auto_repair_loop(project_id, phase_id, False))
        state = routes_phases._auto_repair_states[key]
        assert qc_calls == [1]
        assert state["running"] is False
        assert state["status"] == "error"
        assert state["status"] != "passed"
    finally:
        routes_phases._phase_managers.pop(project_id, None)
        routes_phases.projects.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)
        routes_phases._auto_repair_api_configs.pop(key, None)


def test_next_quality_check_waits_for_complete_repair_batch(monkeypatch, tmp_path):
    """The second QC run must not start while the engineer is still repairing."""
    from api import routes_execution

    project_id, phase_id = "repair-wait-project", "phase-1"
    phase = {"phase_id": phase_id, "name": "Backend", "status": "reviewing"}
    pm = _PhaseManager(phase)
    target = tmp_path / "backend" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("broken", encoding="utf-8")
    agent = {
        "id": "backend-agent", "phase_id": phase_id, "subproject_id": "sp-backend",
        "subproject_name": "Backend", "role": "Backend Developer",
        "status": "completed", "output_files": ["backend/app.py"],
        "allowed_path_prefixes": ["backend/app.py"],
    }
    ctx = SimpleNamespace(
        project_id=project_id, workspace=tmp_path, description="Backend",
        pm=SimpleNamespace(context_summary="context"), agents={agent["id"]: agent},
        subprojects=[{
            "id": "sp-backend", "phase_id": phase_id, "agent_id": agent["id"],
            "status": "completed", "progress": 100,
        }],
        qc_results={},
    )
    key = f"{project_id}-{phase_id}"
    routes_phases._phase_managers[project_id] = pm
    routes_phases.projects[project_id] = ctx
    routes_phases._auto_repair_states[key] = {
        "running": True, "round": 0, "status": "starting", "messages": [],
        "phase_name": phase["name"],
    }
    repair_started = asyncio.Event()
    allow_repair_to_finish = asyncio.Event()
    qc_calls = []

    def qc_result(*_args, **_kwargs):
        qc_calls.append(len(qc_calls) + 1)
        if len(qc_calls) == 1:
            return {
                "passed": False, "score": 90, "error_count": 1, "warning_count": 0,
                "issues": ["broken"], "issues_detail": [{
                    "file_path": "backend/app.py", "severity": "error",
                    "message": "broken", "status": "open",
                    "responsible_agent_id": agent["id"],
                }],
            }
        return {
            "passed": True, "score": 100, "error_count": 0, "warning_count": 0,
            "issues": [], "issues_detail": [],
        }

    async def delayed_repair(**_kwargs):
        repair_started.set()
        await allow_repair_to_finish.wait()
        target.write_text("fixed", encoding="utf-8")
        agent["status"] = "completed"
        return {"success": True, "status": "completed", "output_files": ["backend/app.py"]}

    async def no_persist():
        return None

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(routes_supervisor, "_run_qc_for_subproject", qc_result)
    monkeypatch.setattr(routes_execution, "_run_agent_task", delayed_repair)
    monkeypatch.setattr(routes_phases, "_persist_all_async", no_persist)
    monkeypatch.setattr(routes_phases.asyncio, "sleep", no_sleep)

    async def run_scenario():
        task = asyncio.create_task(
            routes_phases._run_auto_repair_loop(project_id, phase_id, False)
        )
        await repair_started.wait()
        assert qc_calls == [1]
        assert routes_phases._auto_repair_states[key]["repair_batch"]["status"] == "repairing"
        allow_repair_to_finish.set()
        await task

    try:
        asyncio.run(run_scenario())
        assert qc_calls == [1, 2]
        assert routes_phases._auto_repair_states[key]["repair_batch"]["status"] == "completed"
        assert routes_phases._auto_repair_states[key]["status"] == "passed"
    finally:
        routes_phases._phase_managers.pop(project_id, None)
        routes_phases.projects.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)


def test_equal_blocker_count_stops_after_one_non_converging_repair(monkeypatch, tmp_path):
    """Every completed repair round must strictly reduce blocking findings."""
    project_id, phase_id = "strict-convergence-project", "phase-1"
    phase = {"phase_id": phase_id, "name": "Backend", "status": "reviewing"}
    pm = _PhaseManager(phase)
    target = tmp_path / "backend" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("original", encoding="utf-8")
    ctx = SimpleNamespace(
        project_id=project_id, workspace=tmp_path, description="Backend",
        pm=SimpleNamespace(context_summary="context"),
        agents={"backend-agent": {
            "id": "backend-agent", "phase_id": phase_id, "subproject_id": "sp-backend",
            "role": "Backend Developer", "status": "completed",
            "allowed_path_prefixes": ["backend/app.py"],
        }},
        subprojects=[], qc_results={},
    )
    key = f"{project_id}-{phase_id}"
    routes_phases._phase_managers[project_id] = pm
    routes_phases.projects[project_id] = ctx
    routes_phases._auto_repair_states[key] = {
        "running": True, "round": 0, "status": "starting", "messages": [],
        "phase_name": phase["name"],
    }
    qc_calls = []

    def qc_result(*_args, **_kwargs):
        qc_calls.append(len(qc_calls) + 1)
        entry = {
            "passed": False, "score": 90, "error_count": 1, "warning_count": 0,
            "issues": ["still broken"], "issues_detail": [{
                "file_path": "backend/app.py", "severity": "error",
                "message": "still broken", "status": "open",
                "responsible_agent_id": "backend-agent",
            }],
        }
        ctx.qc_results[phase_id] = {"qa": entry}
        return entry

    async def completed_repair(*_args, **_kwargs):
        target.write_text("changed but not better", encoding="utf-8")
        return {"status": "completed", "total": 1, "completed": 1}

    async def no_persist():
        return None

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(routes_supervisor, "_run_qc_for_subproject", qc_result)
    monkeypatch.setattr(routes_phases, "_repair_all_issue_owners", completed_repair)
    monkeypatch.setattr(routes_phases, "_persist_all_async", no_persist)
    monkeypatch.setattr(routes_phases.asyncio, "sleep", no_sleep)
    try:
        asyncio.run(routes_phases._run_auto_repair_loop(project_id, phase_id, False))
        state = routes_phases._auto_repair_states[key]
        assert qc_calls == [1, 2]
        assert state["status"] == "no_progress"
        assert state["running"] is False
        assert target.read_text(encoding="utf-8") == "original"
        assert state["review_result"]["passed"] is False
        assert state["issue_report"]["backend/app.py"][0]["message"] == "still broken"
        assert [item["blocking_count"] for item in state["round_history"]] == [1, 1]
        assert state["round_history"][-1]["convergence"]["status"] == "no_progress"
    finally:
        routes_phases._phase_managers.pop(project_id, None)
        routes_phases.projects.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)


def test_fifth_business_qa_stops_after_five_repairs_without_dispatching_again(
    monkeypatch, tmp_path,
):
    project_id, phase_id = "five-qa-budget-project", "phase-1"
    phase = {"phase_id": phase_id, "name": "Backend", "status": "reviewing"}
    pm = _PhaseManager(phase)
    target = tmp_path / "backend" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("version-0", encoding="utf-8")
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        description="Backend",
        pm=SimpleNamespace(context_summary="context"),
        agents={"backend-agent": {
            "id": "backend-agent",
            "phase_id": phase_id,
            "subproject_id": "sp-backend",
            "role": "Backend Developer",
            "status": "completed",
            "allowed_path_prefixes": ["backend/app.py"],
        }},
        subprojects=[],
        qc_results={},
        supervisor_quality_runs={},
    )
    key = f"{project_id}-{phase_id}"
    routes_phases._phase_managers[project_id] = pm
    routes_phases.projects[project_id] = ctx
    routes_phases._auto_repair_states[key] = {
        "running": True,
        "round": 0,
        "status": "starting",
        "messages": [],
        "phase_name": phase["name"],
    }
    qc_calls = []
    repair_calls = []

    def qc_result(*_args, **_kwargs):
        qa_number = len(qc_calls) + 1
        qc_calls.append(qa_number)
        remaining = 6 - qa_number
        issues = [{
            "id": f"issue-{index}",
            "fingerprint": f"fp-{index}",
            "file_path": "backend/app.py",
            "severity": "error",
            "layer": "runtime",
            "message": f"blocker {index}",
            "status": "open",
            "responsible_agent_id": "backend-agent",
        } for index in range(1, remaining + 1)]
        entry = {
            "passed": False,
            "score": 50,
            "error_count": len(issues),
            "warning_count": 0,
            "issues": [item["message"] for item in issues],
            "issues_detail": issues,
            "observed_issues_detail": issues,
        }
        ctx.qc_results[phase_id] = {"qa": entry}
        return entry

    async def completed_repair(*_args, **_kwargs):
        repair_calls.append(len(repair_calls) + 1)
        target.write_text(f"version-{len(repair_calls)}", encoding="utf-8")
        return {
            "status": "completed",
            "total": 1,
            "completed": 1,
            "failed": 0,
            "files_changed": True,
        }

    async def no_persist():
        return None

    monkeypatch.setattr(routes_supervisor, "_run_qc_for_subproject", qc_result)
    monkeypatch.setattr(routes_phases, "_repair_all_issue_owners", completed_repair)
    monkeypatch.setattr(routes_phases, "_persist_all_async", no_persist)
    try:
        asyncio.run(routes_phases._run_auto_repair_loop(project_id, phase_id, False))
        state = routes_phases._auto_repair_states[key]
        assert qc_calls == [1, 2, 3, 4, 5, 6]
        assert repair_calls == [1, 2, 3, 4, 5]
        assert state["status"] in {"awaiting_decision", "passed"}
        assert state["running"] is False
        if state["status"] == "awaiting_decision":
            assert state["action_required"]["automatic_retry_allowed"] is False
        assert state["issue_report"].get("backend/app.py", [{"fingerprint": "fp-1"}])[0]["fingerprint"] == "fp-1"
    finally:
        routes_phases._phase_managers.pop(project_id, None)
        routes_phases.projects.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)


def test_auto_repair_issue_report_preserves_stable_identity_and_evidence():
    entry = {
        "issues_detail": [{
            "id": "legacy-id",
            "issue_id": "issue-stable",
            "fingerprint": "fingerprint-stable",
            "file_path": "backend/app.py",
            "line": 42,
            "layer": "runtime",
            "severity": "critical",
            "message": "request crashes",
            "fix_hint": "guard the input",
            "status": "open",
            "lifecycle": "repeated",
            "first_seen_round": 1,
            "last_seen_round": 5,
            "fix_rounds": 4,
            "evidence_step_id": "qa:runtime:request",
            "acceptance_criteria": "request returns 200",
        }],
    }

    issue = routes_phases._auto_repair_issue_report(entry)["backend/app.py"][0]

    assert issue["issue_id"] == "issue-stable"
    assert issue["fingerprint"] == "fingerprint-stable"
    assert issue["line"] == 42
    assert issue["layer"] == "runtime"
    assert issue["lifecycle"] == "repeated"
    assert issue["fix_rounds"] == 4
    assert issue["evidence"] == "qa:runtime:request"
    assert issue["acceptance_criteria"] == "request returns 200"


@pytest.mark.parametrize("field", ["line", "line_no", "line_number"])
def test_auto_repair_issue_report_preserves_all_supported_line_fields(field):
    entry = {
        "issues_detail": [{
            "issue_id": f"issue-{field}",
            "file_path": "backend/app.py",
            field: 42,
            "severity": "error",
            "status": "open",
            "message": "request crashes",
        }],
    }

    issue = routes_phases._auto_repair_issue_report(entry)["backend/app.py"][0]

    assert issue["line"] == 42


def test_new_blocker_is_regression_even_when_total_blockers_decrease(monkeypatch, tmp_path):
    """A repair may only shrink the original blocker set; replacement defects regress."""
    project_id, phase_id = "identity-convergence-project", "phase-1"
    phase = {"phase_id": phase_id, "name": "Backend", "status": "reviewing"}
    pm = _PhaseManager(phase)
    target = tmp_path / "backend" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("original", encoding="utf-8")
    ctx = SimpleNamespace(
        project_id=project_id, workspace=tmp_path, description="Backend",
        pm=SimpleNamespace(context_summary="context"),
        agents={"backend-agent": {
            "id": "backend-agent", "phase_id": phase_id, "subproject_id": "sp-backend",
            "role": "Backend Developer", "status": "completed",
            "allowed_path_prefixes": ["backend/app.py"],
        }},
        subprojects=[], qc_results={},
    )
    key = f"{project_id}-{phase_id}"
    routes_phases._phase_managers[project_id] = pm
    routes_phases.projects[project_id] = ctx
    routes_phases._auto_repair_states[key] = {
        "running": True, "round": 0, "status": "starting", "messages": [],
        "phase_name": phase["name"],
    }
    qc_calls = []

    def issue(issue_id, message):
        return {
            "id": issue_id, "fingerprint": issue_id,
            "file_path": "backend/app.py", "severity": "error",
            "message": message, "status": "open",
            "responsible_agent_id": "backend-agent",
        }

    results = iter([
        {
            "passed": False, "score": 70, "error_count": 3, "warning_count": 0,
            "issues": ["a", "b", "c"],
            "issues_detail": [issue("a", "a"), issue("b", "b"), issue("c", "c")],
        },
        {
            "passed": False, "score": 80, "error_count": 2, "warning_count": 0,
            "issues": ["a", "new"],
            "issues_detail": [issue("a", "a"), issue("new", "new")],
        },
    ])

    def qc_result(*_args, **_kwargs):
        qc_calls.append(len(qc_calls) + 1)
        entry = next(results)
        ctx.qc_results[phase_id] = {"qa": entry}
        return entry

    async def completed_repair(*_args, **_kwargs):
        target.write_text("changed with a new defect", encoding="utf-8")
        return {"status": "completed", "total": 1, "completed": 1, "failed": 0}

    async def no_persist():
        return None

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(routes_supervisor, "_run_qc_for_subproject", qc_result)
    monkeypatch.setattr(routes_phases, "_repair_all_issue_owners", completed_repair)
    monkeypatch.setattr(routes_phases, "_persist_all_async", no_persist)
    monkeypatch.setattr(routes_phases.asyncio, "sleep", no_sleep)
    try:
        asyncio.run(routes_phases._run_auto_repair_loop(project_id, phase_id, False))
        state = routes_phases._auto_repair_states[key]
        assert qc_calls == [1, 2]
        assert state["status"] == "quality_regressed"
        assert state["running"] is False
        assert target.read_text(encoding="utf-8") == "original"
        assert state["round_history"][-1]["convergence"]["new_blockers"] == ["new"]
        assert "new" in state["action_required"]["message"]
    finally:
        routes_phases._phase_managers.pop(project_id, None)
        routes_phases.projects.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)


def test_product_defect_routes_to_cross_phase_file_owner(monkeypatch, tmp_path):
    backend = {
        "id": "agent-backend",
        "phase_id": "phase-1",
        "expert_type": "backend",
        "output_files": ["backend/src/server.js"],
    }
    qa = {
        "id": "agent-qa",
        "phase_id": "phase-3",
        "expert_type": "qa",
        "output_files": ["tests/api.test.js"],
    }
    ctx = SimpleNamespace(
        project_id="cross-phase-owner",
        workspace=tmp_path,
        agents={backend["id"]: backend, qa["id"]: qa},
    )
    manager = SimpleNamespace(file_registry={
        "backend/src/server.js": {
            "agent_id": backend["id"],
            "phase_id": "phase-1",
        }
    })
    monkeypatch.setitem(
        routes_phases._phase_managers,
        ctx.project_id,
        manager,
    )

    owner = routes_phases._match_issue_agent(ctx, "phase-3", {
        "file_path": "backend/src/server.js",
        "responsible_agent_id": qa["id"],
        "responsible_agent_role": "QA Engineer",
    })

    assert owner["id"] == backend["id"]


def test_current_phase_owner_precedes_historical_file_owner(monkeypatch, tmp_path):
    historical = {
        "id": "agent-old",
        "phase_id": "phase-1",
        "expert_type": "backend",
        "output_files": ["backend/package.json"],
    }
    current = {
        "id": "agent-current",
        "phase_id": "phase-4",
        "expert_type": "fullstack_engineer",
        "allowed_path_prefixes": ["backend/", "package.json"],
    }
    ctx = SimpleNamespace(
        project_id="current-owner",
        workspace=tmp_path,
        agents={historical["id"]: historical, current["id"]: current},
    )
    monkeypatch.setitem(
        routes_phases._phase_managers,
        ctx.project_id,
        SimpleNamespace(file_registry={
            "backend/package.json": {"agent_id": historical["id"], "phase_id": "phase-1"}
        }),
    )

    owner = routes_phases._match_issue_agent(ctx, "phase-4", {
        "file_path": "backend/package.json",
        "responsible_agent_id": historical["id"],
    })

    assert owner["id"] == current["id"]


def test_unowned_readme_defect_routes_to_devops_contract_not_qa(tmp_path):
    devops = {
        "id": "agent-devops",
        "phase_id": "phase-3",
        "expert_type": "devops",
        "execution_contract": {"description": "Build Dockerfile and README.md"},
    }
    qa = {
        "id": "agent-qa",
        "phase_id": "phase-3",
        "expert_type": "qa",
        "execution_contract": {"description": "Test Dockerfile and README.md"},
    }
    ctx = SimpleNamespace(
        project_id="readme-owner",
        workspace=tmp_path,
        agents={devops["id"]: devops, qa["id"]: qa},
    )

    owner = routes_phases._match_issue_agent(ctx, "phase-3", {
        "file_path": "README.md",
        "responsible_agent_id": qa["id"],
        "responsible_agent_role": "QA Engineer",
    })

    assert owner["id"] == devops["id"]


def test_repair_claims_exact_file_before_extending_agent_scope(monkeypatch, tmp_path):
    agent = {
        "id": "agent-devops",
        "expert_id": "expert-devops",
        "subproject_id": "phase-3",
        "allowed_path_prefixes": ["Dockerfile"],
        "required_rebuild_files": [],
        "lock_id": "lock-devops",
    }
    ctx = SimpleNamespace(project_id="repair-scope", workspace=tmp_path)
    claims = []
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "renew_lock",
        lambda lock_id: {"success": True, "lock_id": lock_id},
    )
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "try_claim_file",
        lambda expert_id, lock_id, path: (
            claims.append((expert_id, lock_id, path))
            or {"success": True, "lock_id": lock_id}
        ),
    )

    lease = routes_phases._ensure_repair_file_scope(
        ctx,
        agent,
        "README.md",
        1,
    )

    assert claims == [("expert-devops", "lock-devops", "README.md")]
    assert "README.md" in agent["allowed_path_prefixes"]
    assert agent["required_rebuild_files"] == []
    assert lease["temporary"] is False


def test_repair_file_scope_preserves_dotfile_name(monkeypatch, tmp_path):
    agent = {
        "id": "agent-fullstack",
        "expert_id": "expert-fullstack",
        "subproject_id": "phase-1",
        "allowed_path_prefixes": [".gitignore"],
        "required_rebuild_files": [],
        "lock_id": "lock-fullstack",
    }
    ctx = SimpleNamespace(project_id="repair-dotfile", workspace=tmp_path)
    claims = []
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "renew_lock",
        lambda lock_id: {"success": True, "lock_id": lock_id},
    )
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "try_claim_file",
        lambda expert_id, lock_id, path: (
            claims.append((expert_id, lock_id, path))
            or {"success": True, "lock_id": lock_id}
        ),
    )

    lease = routes_phases._ensure_repair_file_scope(
        ctx,
        agent,
        ".gitignore",
        1,
    )

    assert lease["file_path"] == ".gitignore"
    assert claims == []
    assert agent["allowed_path_prefixes"] == [".gitignore"]


def test_repair_reclaims_released_cross_phase_lock(monkeypatch, tmp_path):
    agent = {
        "id": "agent-backend",
        "expert_id": "expert-backend",
        "subproject_id": "phase-1",
        "allowed_path_prefixes": ["backend/"],
        "required_rebuild_files": [],
        "lock_id": "released-lock",
    }
    ctx = SimpleNamespace(project_id="repair-reclaim", workspace=tmp_path)
    claims = []
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "renew_lock",
        lambda _lock_id: {"success": False},
    )
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "atomic_claim_lock",
        lambda **kwargs: (
            claims.append(kwargs)
            or {
                "success": True,
                "lock_id": "repair-lock",
                "leased_until": 9999999999,
            }
        ),
    )

    lease = routes_phases._ensure_repair_file_scope(
        ctx,
        agent,
        "backend/src/config/index.js",
        1,
    )

    assert claims[0]["file_scope"] == ["backend/src/config/index.js"]
    assert claims[0]["task_id"] == "phase-1"
    assert agent["lock_id"] == "repair-lock"
    assert lease == {
        "file_path": "backend/src/config/index.js",
        "lock_id": "repair-lock",
        "temporary": True,
        "round": 1,
    }


def test_repair_rejects_unlocated_finding_before_agent_execution(tmp_path):
    agent = {
        "id": "agent-backend",
        "expert_id": "expert-backend",
        "subproject_id": "phase-1",
        "allowed_path_prefixes": ["backend/"],
    }
    ctx = SimpleNamespace(project_id="repair-unlocated", workspace=tmp_path)

    with pytest.raises(RuntimeError, match="no actionable delivery file"):
        routes_phases._ensure_repair_file_scope(
            ctx, agent, "未定位文件", 1,
        )


def test_rebuild_uses_real_files_and_preserves_captured_api_config(monkeypatch, tmp_path):
    project_id, phase_id = "rebuild-project", "phase-1"
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('old')", encoding="utf-8")
    (tmp_path / "README.md").write_text("unregistered", encoding="utf-8")
    phase = {
        "phase_id": phase_id,
        "name": "Phase 1",
        "description": "Build the application",
        "roles_needed": [
            "Backend Developer", "Frontend Developer", "DevOps Engineer",
        ],
        "expert_requirements": [
            {"task_id": "backend-task", "required_role": "Backend Developer"},
            {"task_id": "frontend-task", "required_role": "Frontend Developer"},
            {"task_id": "devops-task", "required_role": "DevOps Engineer"},
        ],
    }
    pm = _PhaseManager(phase)
    required_rows = [
        ("src/app.py", "backend", "backend-task"),
        ("backend/package.json", "backend", "backend-task"),
        ("frontend/package.json", "frontend", "frontend-task"),
        ("package.json", "devops", "devops-task"),
        ("README.md", "devops", "devops-task"),
        (".env.example", "devops", "devops-task"),
        ("Dockerfile", "devops", "devops-task"),
    ]
    pm.project_contract = {
        "locked": True,
        "required_files": [
            {
                "path": path, "owner_type": owner_type,
                "phase_id": phase_id, "task_id": task_id, "required": True,
            }
            for path, owner_type, task_id in required_rows
        ],
    }
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        subprojects=[],
        agents={
            "agent-old": {
                "id": "agent-old",
                "phase_id": phase_id,
                "expert_type": "backend",
                "output_files": ["src/app.py", "output/phase-1_execution.log"],
            }
        },
    )
    key = f"{project_id}-{phase_id}"
    routes_phases._phase_managers[project_id] = pm
    routes_phases._auto_repair_states[key] = {
        "running": False,
        "round": 5,
        "status": "awaiting_decision",
        "messages": [],
        "phase_name": "Phase 1",
        "issue_report": {"src/app.py": [{"severity": "error"}]},
    }
    captured_config = {"api_key": "test-key", "model": "test-model"}
    routes_phases._auto_repair_api_configs[key] = captured_config
    monkeypatch.setattr(routes_phases, "_get_project", lambda _project_id: ctx)

    calls = {"reset": 0, "config": None}

    async def fake_reset(_project_id, _phase_id, preserve_rebuild_state=False):
        calls["reset"] += 1
        assert preserve_rebuild_state is True
        return {"success": True}

    async def fake_start(_project_id, _phase_id):
        calls["config"] = current_user_api_config.get()
        phase["execution_generation"] = "phase-1:rebuild-generation"
        phase["execution_contract_digest"] = "sha256:rebuild-contract"
        phase["execution_requirements_revision"] = 2
        return {"success": True, "created_agents": [{"id": "new-agent"}]}

    monkeypatch.setattr(routes_phases, "_reset_phase", fake_reset)
    monkeypatch.setattr(routes_phases, "start_phase", fake_start)
    token = current_user_api_config.set(None)
    try:
        result = asyncio.run(routes_phases.start_auto_repair(project_id, phase_id, "rebuild_phase"))
        manifest = phase["rebuild_file_manifest"]
        required = {
            "package.json", "backend/package.json", "frontend/package.json",
            "README.md", ".env.example", "Dockerfile", "src/app.py",
        }
        assert required == set(manifest["all"])
        assert required == set(manifest["required_from_project_contract"])
        assert required - {"README.md", "src/app.py"} == set(manifest["missing_before_rebuild"])
        assert manifest["unregistered_before_rebuild"] == ["README.md", "src/app.py"]
        assert set(manifest["by_expert_type"]["frontend"]) == {"frontend/package.json"}
        assert set(manifest["by_expert_type"]["backend"]) == {
            "backend/package.json", "src/app.py",
        }
        assert set(manifest["by_expert_type"]["devops"]) == {
            "package.json", "README.md", ".env.example", "Dockerfile",
        }
        assigned = {
            path
            for paths in manifest["by_task_id"].values()
            for path in paths
        }
        assert required == assigned
        assert manifest["preserve_verifier_paths"] == []
        assert calls == {"reset": 1, "config": captured_config}
        assert result["created_agents"] == 1
        assert result["status"]["status"] == "rebuild_started"
        assert result["status"]["round"] == 0
        assert result["status"]["repair_attempts"] == 0
        assert result["status"]["total_rounds"] == 0
        assert result["status"]["lifetime_qc_runs"] == 0
        assert result["status"]["issue_report"] == {}
        assert result["status"]["rebuild_comparison_pending"] is True
        assert result["status"]["execution_generation"] == phase["execution_generation"]
        assert result["status"]["contract_digest"] == phase["execution_contract_digest"]
        assert result["status"]["requirements_revision"] == 2
        internal_state = routes_phases._auto_repair_states[key]
        assert internal_state["pre_rebuild_issue_snapshot"][0]["file_path"] == "src/app.py"
        assert phase["pre_rebuild_issue_snapshot"] == internal_state["pre_rebuild_issue_snapshot"]
        assert "pre_rebuild_restore_point" not in result["status"]
        assert "pre_rebuild_issue_snapshot" not in result["status"]
        assert "src/app.py" not in phase["description"]
    finally:
        current_user_api_config.reset(token)
        routes_phases._phase_managers.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)


def test_rebuild_issues_are_partitioned_by_file_owner_and_not_broadcast():
    issues = routes_phases._flatten_rebuild_issue_report({
        "backend/api.py": [{"id": "api", "message": "修复 API", "responsible_agent_id": "old-api"}],
        "backend/auth.py": [{"id": "auth", "message": "修复认证", "responsible_agent_id": "old-auth"}],
        "未定位文件": [{"id": "unknown", "message": "需要人工定位"}],
    })
    assignments = routes_phases._partition_rebuild_issues(
        issues,
        {"sp-api": {"backend/api.py"}, "sp-auth": {"backend/auth.py"}},
        {
            "old-api": {"subproject_id": "sp-api"},
            "old-auth": {"subproject_id": "sp-auth"},
        },
    )

    assert [item["id"] for item in assignments["sp-api"]] == ["api"]
    assert [item["id"] for item in assignments["sp-auth"]] == ["auth"]
    assert [item["id"] for item in assignments["_unassigned"]] == ["unknown"]


def test_first_rebuild_qa_regresses_on_new_blocker_despite_lower_total():
    before = [
        {"id": "old-1", "severity": "error", "status": "open"},
        {"id": "old-2", "severity": "error", "status": "open"},
        {"id": "old-3", "severity": "error", "status": "open"},
    ]
    after = [
        {"id": "old-1", "severity": "error", "status": "open"},
        {"id": "new-1", "severity": "critical", "status": "open"},
    ]

    comparison = routes_phases._compare_rebuild_issue_snapshots(before, after)

    assert comparison["status"] == "rebuild_regressed"
    assert comparison["resolved"] == ["old-2", "old-3"]
    assert comparison["remaining"] == ["old-1"]
    assert comparison["new_blockers"] == ["new-1"]


def test_first_rebuild_qa_detects_no_progress():
    before = [{"id": "old-1", "severity": "error", "status": "open"}]
    comparison = routes_phases._compare_rebuild_issue_snapshots(before, before)

    assert comparison["status"] == "rebuild_no_progress"
    assert comparison["repeated"] == ["old-1"]


def test_legacy_completed_agent_without_progress_is_supervisor_success():
    assert routes_phases._supervisor_agent_status({"status": "completed"}) == "succeeded"
    assert routes_phases._supervisor_agent_status({
        "status": "completed", "progress": 100,
    }) == "succeeded"
    assert routes_phases._supervisor_agent_status({
        "status": "completed", "progress": 99,
    }) == "pending"
    assert routes_phases._supervisor_agent_status({"status": "failed"}) == "failed"


def test_legacy_scope_only_qc_can_enter_verification(monkeypatch, tmp_path):
    project_id, phase_id = "legacy-scope-only", "phase-1"
    pm = _PhaseManager({"phase_id": phase_id, "name": "Architecture"})
    ctx = SimpleNamespace(project_id=project_id, workspace=tmp_path)
    state = {"messages": []}
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)

    machine = routes_phases._ensure_supervisor_quality_run(ctx, phase_id, state)
    routes_phases._prepare_supervisor_verification(ctx, phase_id, machine)

    assert machine.state == "verifying"
    assert machine.to_dict()["agents"][f"legacy-scope:{phase_id}"]["status"] == "succeeded"


def test_completed_supervisor_agent_is_not_downgraded_by_later_legacy_failure(
    monkeypatch, tmp_path,
):
    project_id, phase_id = "durable-agent-success", "phase-1"
    pm = _PhaseManager({"phase_id": phase_id, "name": "Backend"})
    agent = {
        "id": "backend-agent",
        "phase_id": phase_id,
        "subproject_id": "backend-task",
        "status": "completed",
        "progress": 100,
    }
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        agents={agent["id"]: agent},
        supervisor_quality_runs={},
    )
    state = {"messages": []}
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)

    machine = routes_phases._ensure_supervisor_quality_run(ctx, phase_id, state)
    routes_phases._prepare_supervisor_verification(ctx, phase_id, machine)
    agent["status"] = "failed"
    agent["error"] = "later repair attempt was interrupted"

    routes_phases._prepare_supervisor_verification(ctx, phase_id, machine)

    recorded = machine.to_dict()["agents"][agent["id"]]
    assert recorded["status"] == "succeeded"
    assert recorded["error"] == ""


def test_interrupted_rebuild_cannot_create_duplicate_agents(monkeypatch, tmp_path):
    project_id, phase_id = "interrupted-rebuild", "phase-1"
    phase = {"phase_id": phase_id, "name": "后端"}
    routes_phases._phase_managers[project_id] = _PhaseManager(phase)
    ctx = SimpleNamespace(project_id=project_id, workspace=tmp_path, agents={}, subprojects=[])
    monkeypatch.setattr(routes_phases, "_get_project", lambda _project_id: ctx)
    monkeypatch.setattr(
        routes_phases, "_reset_phase",
        lambda *_args, **_kwargs: pytest.fail("interrupted rebuild must not reset again"),
    )
    key = f"{project_id}-{phase_id}"
    routes_phases._auto_repair_states[key] = {
        "running": False,
        "status": "interrupted",
        "rebuild_comparison_pending": True,
        "rebuild_run_id": "rebuild-existing",
        "pre_rebuild_snapshot_version": 3,
        "pre_rebuild_restore_point": {"private": "material"},
    }
    try:
        result = asyncio.run(
            routes_phases.start_auto_repair(project_id, phase_id, "rebuild_phase")
        )
        assert result["already_rebuilding"] is True
        assert result["status"]["status"] == "rebuild_recovery_required"
        assert result["status"]["action_required"]["options"] == [
            "recover_rebuild", "keep_previous_version", "manual_fix",
        ]
        assert "pre_rebuild_restore_point" not in result["status"]
    finally:
        routes_phases._phase_managers.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)


def test_python_only_contract_does_not_invent_node_or_frontend_deliverables():
    phase = {"phase_id": "phase-python", "deliverables": ["backend/requirements.txt"]}
    pm = _PhaseManager(phase)
    pm.project_contract = {
        "locked": True,
        "required_tech": ["Python", "FastAPI"],
        "source_requirements": "Python FastAPI backend service only.",
    }

    required = routes_phases._contract_required_rebuild_files(pm, phase)

    assert required == ["backend/requirements.txt"]
    assert "package.json" not in required
    assert "backend/package.json" not in required
    assert "frontend/package.json" not in required


def test_failed_rebuild_agent_rolls_back_and_requires_manual_action(monkeypatch, tmp_path):
    project_id, phase_id = "failed-rebuild", "phase-1"
    phase = {"phase_id": phase_id, "status": "in_progress"}
    pm = _PhaseManager(phase)
    routes_phases._phase_managers[project_id] = pm
    ctx = SimpleNamespace(project_id=project_id, workspace=tmp_path, agents={}, subprojects=[])
    key = f"{project_id}-{phase_id}"
    state = {
        "running": False,
        "status": "rebuild_started",
        "rebuild_comparison_pending": True,
        "rebuild_run_id": "rebuild-failed",
        "pre_rebuild_snapshot_version": 4,
        "pre_rebuild_issue_report": {"backend/app.py": [{"severity": "error"}]},
        "pre_rebuild_restore_point": {"captured": True},
    }
    routes_phases._auto_repair_states[key] = state
    monkeypatch.setattr(
        routes_phases, "_restore_phase_rebuild_snapshot",
        lambda *_args: {"snapshot_version": 4, "restored_files": ["backend/app.py"]},
    )

    async def no_persist():
        return None

    monkeypatch.setattr(routes_phases, "_persist_all_async", no_persist)
    try:
        changed = asyncio.run(routes_phases._fail_closed_rebuild_execution(
            ctx, phase_id, ["agent:new-agent:failed"],
        ))
        assert changed is True
        assert state["status"] == "rebuild_execution_failed"
        assert state["rebuild_comparison_pending"] is False
        assert state["action_required"]["automatic_retry_allowed"] is False
        assert phase["status"] == "needs_rework"
    finally:
        routes_phases._phase_managers.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)


def test_cleanup_keeps_terminal_qc_state_for_existing_phase():
    project_id, phase_id = "visible-terminal-state", "phase-1"
    pm = _PhaseManager({"phase_id": phase_id, "name": "Phase 1"})
    valid_key = f"{project_id}-{phase_id}"
    orphan_key = f"{project_id}-deleted-phase"
    routes_phases._phase_managers[project_id] = pm
    routes_phases._auto_repair_states[valid_key] = {
        "running": False, "status": "passed", "messages": [],
    }
    routes_phases._auto_repair_states[orphan_key] = {
        "running": False, "status": "error", "messages": [],
    }
    try:
        routes_phases._cleanup_auto_repair_states(project_id)
        assert valid_key in routes_phases._auto_repair_states
        assert orphan_key not in routes_phases._auto_repair_states
    finally:
        routes_phases._phase_managers.pop(project_id, None)
        routes_phases._auto_repair_states.pop(valid_key, None)
        routes_phases._auto_repair_states.pop(orphan_key, None)


def test_qc_status_recovers_visible_terminal_result_from_persisted_qc():
    project_id, phase_id = "recover-visible-qc", "phase-1"
    phase = {
        "phase_id": phase_id, "name": "Phase 1", "status": "reviewing",
    }
    pm = _PhaseManager(phase)
    ctx = SimpleNamespace(qc_results={phase_id: {"qa": {
        "passed": True, "qc_round": 2, "user_report": "Architecture accepted",
        "issues": [], "error_count": 0, "warning_count": 0,
    }}})
    routes_phases.projects[project_id] = ctx
    routes_phases._phase_managers[project_id] = pm
    routes_phases._auto_repair_states.pop(f"{project_id}-{phase_id}", None)
    try:
        result = asyncio.run(routes_phases.get_auto_repair_status(project_id, phase_id))
        assert result["status"] == "passed"
        assert result["round"] == 2
        assert result["review_result"]["report"] == "Architecture accepted"
        assert "质检已通过" in result["messages"][0]["content"]
    finally:
        routes_phases.projects.pop(project_id, None)
        routes_phases._phase_managers.pop(project_id, None)


def test_manual_typescript_config_is_included_with_registered_package(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"esModuleInterop":true}}', encoding="utf-8"
    )
    (tmp_path / "tsconfig.test.json").write_text("{}", encoding="utf-8")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "tsconfig.json").write_text("{}", encoding="utf-8")

    files = routes_supervisor._include_package_companion_configs(
        tmp_path, ["package.json", "tests/todos.test.ts"]
    )

    assert files == [
        "package.json", "tests/todos.test.ts", "tsconfig.json", "tsconfig.test.json"
    ]
