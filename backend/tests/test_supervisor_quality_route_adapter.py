"""Integration contracts between phase routes and Supervisor QA authority."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api import routes_phases, routes_supervisor
from core.project_context import ProjectContext
from core.supervisor_quality_state import IllegalQualityTransition


def test_quality_background_postprocessing_failure_is_durable_terminal(
    monkeypatch,
) -> None:
    project_id = "project-qc-postprocess-failure"
    phase_id = "phase-1"
    key = f"{project_id}-{phase_id}"
    state = {
        "running": True,
        "status": "checking",
        "needs_manual": False,
    }
    routes_phases._auto_repair_states[key] = state
    monkeypatch.setattr(routes_phases, "_persist_all", lambda: None)

    @routes_phases._quality_background_terminal
    async def fail_after_qc(_project_id: str, _phase_id: str) -> None:
        raise RuntimeError("finish_verification fault")

    asyncio.run(fail_after_qc(project_id, phase_id))

    assert state["running"] is False
    assert state["status"] == "failed"
    assert state["needs_manual"] is True
    assert "finish_verification fault" in state["action_required"]["message"]


def test_background_quality_loop_advances_and_persists_authoritative_machine() -> None:
    """The legacy repair loop must not be able to pass beside the state machine."""
    source = inspect.getsource(routes_phases._run_auto_repair_loop)

    for required_call in (
        ".start_qa_round(",
        ".finish_verification(",
        ".mark_repair_required(",
        ".complete(",
        "_store_supervisor_quality_machine(",
    ):
        assert required_call in source


def test_status_endpoint_exposes_durable_supervisor_run_without_memory_loop(monkeypatch) -> None:
    project_id = "project-restored-supervisor"
    phase_id = "phase-1"
    durable = {
        "run_id": "run-restored",
        "status": "waiting_engineer",
        "state": "waiting_engineer",
        "active_qa_round_id": "qa-2",
        "business_rounds_used": 1,
        "waiting_for": ["engineer-1"],
        "next_action": {"type": "wait_for_engineer"},
        "rounds": [{"qa_round_id": "qa-1", "qa_snapshot_committed": True}],
    }
    ctx = SimpleNamespace(
        supervisor_quality_runs={phase_id: durable},
        qc_results={},
    )
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    routes_phases._auto_repair_states.pop(f"{project_id}-{phase_id}", None)

    result = asyncio.run(routes_phases.get_auto_repair_status(project_id, phase_id))

    assert result["supervisor_state"] == "waiting_engineer"
    assert result["waiting_for"] == ["engineer-1"]
    assert result["next_action"] == {"type": "wait_for_engineer"}
    assert result["supervisor_run"] == durable


def test_project_snapshot_serializes_supervisor_checkpoints() -> None:
    ctx = ProjectContext(
        project_id="project-supervisor-persist",
        name="Supervisor persistence",
        description="checkpoint",
        owner_user_id="user-1",
        hermes_client=SimpleNamespace(),
        global_sm_agent=SimpleNamespace(),
    )
    ctx.supervisor_quality_runs = {
        "phase-1": {
            "run_id": "run-1",
            "status": "verifying",
            "checkpoint": {"name": "verification_evidence_recorded"},
            "rounds": [],
        }
    }

    persisted = ctx.to_persist()

    assert persisted["supervisor_quality_runs"] == ctx.supervisor_quality_runs


def test_legacy_review_route_cannot_project_quality_pass(monkeypatch) -> None:
    project_id = "project-no-legacy-review"
    monkeypatch.setitem(routes_phases.projects, project_id, SimpleNamespace())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes_phases.review_phase(project_id, "phase-1"))

    assert exc_info.value.status_code == 410


def test_supervisor_scope_digest_binds_delivery_bytes(monkeypatch, tmp_path) -> None:
    project_id = "project-byte-bound-scope"
    phase_id = "phase-1"
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    phase_manager = SimpleNamespace(
        phases=[{"phase_id": phase_id, "started_at": 1.0}],
        get_phase=lambda requested: (
            {"phase_id": phase_id, "started_at": 1.0}
            if requested == phase_id else None
        ),
        get_files_by_phase=lambda requested: (
            [{"file_path": "src/app.py"}] if requested == phase_id else []
        ),
    )
    monkeypatch.setitem(routes_phases._phase_managers, project_id, phase_manager)
    ctx = SimpleNamespace(project_id=project_id, workspace=tmp_path)

    before = routes_phases._supervisor_scope_snapshot(ctx, phase_id)
    source.write_text("VALUE = 2\n", encoding="utf-8")
    after = routes_phases._supervisor_scope_snapshot(ctx, phase_id)

    assert before["artifact_digest"] != after["artifact_digest"]
    assert before["scope_digest"] != after["scope_digest"]
    assert before["delivery_manifest"]["rule_version"]


def test_supervisor_scope_excludes_missing_future_phase_artifacts(
    monkeypatch, tmp_path,
) -> None:
    project_id = "project-phase-scoped-delivery"
    phase_id = "phase-2"
    (tmp_path / "README.md").write_text("accepted\n", encoding="utf-8")
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    phases = [
        {"phase_id": "phase-1", "user_confirmed": True},
        {"phase_id": phase_id, "started_at": 2.0},
        {"phase_id": "phase-4"},
    ]
    phase_manager = SimpleNamespace(
        phases=phases,
        project_contract={"required_files": [
            {"path": "README.md", "phase_id": "phase-1", "required": True},
            {"path": "src/app.py", "phase_id": phase_id, "required": True},
            {"path": ".dockerignore", "phase_id": "phase-4", "required": True},
        ]},
        get_phase=lambda requested: next(
            (item for item in phases if item["phase_id"] == requested), None,
        ),
        get_files_by_phase=lambda _requested: [{"file_path": "src/app.py"}],
    )
    monkeypatch.setitem(routes_phases._phase_managers, project_id, phase_manager)
    ctx = SimpleNamespace(project_id=project_id, workspace=tmp_path)

    scope = routes_phases._supervisor_scope_snapshot(ctx, phase_id)

    assert scope["delivery_manifest"]["required_paths"] == [
        "README.md", "src/app.py",
    ]


def test_supervisor_completion_recomputes_and_rejects_concurrent_bytes(
    monkeypatch,
    tmp_path,
) -> None:
    project_id = "project-concurrent-supervisor"
    phase_id = "phase-1"
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    phase_manager = SimpleNamespace(
        phases=[{"phase_id": phase_id, "started_at": 1.0}],
        get_phase=lambda _phase_id: {"phase_id": phase_id, "started_at": 1.0},
        get_files_by_phase=lambda _phase_id: [{"file_path": "src/app.py"}],
    )
    monkeypatch.setitem(routes_phases._phase_managers, project_id, phase_manager)
    ctx = SimpleNamespace(project_id=project_id, workspace=tmp_path)
    scope = routes_phases._supervisor_scope_snapshot(ctx, phase_id)
    active_round = {
        "scope_snapshot": scope,
        "commit": f"artifact:{scope['artifact_digest']}",
        "evidence": [{"kind": "scope", "metadata": scope}],
    }
    machine = SimpleNamespace(
        active_round=active_round,
        to_dict=lambda: {"scope": scope},
    )

    source.write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(
        IllegalQualityTransition,
        match="Concurrent workspace change",
    ):
        routes_phases._assert_supervisor_artifact_current(
            ctx, phase_id, machine,
        )


def test_adhoc_qc_trigger_delegates_without_raw_ledger_mutation(
    monkeypatch,
) -> None:
    project_id = "project-delegated-qc"
    phase_id = "phase-1"
    ctx = SimpleNamespace(
        subprojects=[{"id": "sp-1", "phase_id": phase_id}],
    )
    phase_manager = SimpleNamespace(
        phases=[{"phase_id": phase_id}],
        get_phase=lambda requested: (
            {"phase_id": phase_id} if requested == phase_id else None
        ),
    )
    monkeypatch.setattr(routes_supervisor, "_get_project", lambda _project_id: ctx)
    monkeypatch.setitem(
        routes_supervisor._phase_managers, project_id, phase_manager,
    )
    monkeypatch.setattr(
        routes_supervisor,
        "_run_qc_for_subproject",
        lambda *_args, **_kwargs: pytest.fail("raw QC bypass was invoked"),
    )

    async def delegated(_project_id, requested_phase, _decision=None):
        return {"success": True, "phase_id": requested_phase}

    monkeypatch.setattr(routes_phases, "start_auto_repair", delegated)

    result = asyncio.run(
        routes_supervisor.trigger_quality_checks(project_id, "sp-1"),
    )

    assert result["delegated"] is True
    assert result["phase_id"] == phase_id
    assert result["run"]["success"] is True
