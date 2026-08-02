import asyncio
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api import routes_execution, routes_phases
from core.agent_lifecycle import transition_agent
from core.execution_runs import LeaseConflict
from core.project_write_fence import ProjectExecutionGuard, RevokedExecutionGuard


def _test_workspace(project_id: str) -> Path:
    return Path(tempfile.gettempdir()) / "metis-tests" / project_id


class _PhaseManager:
    def __init__(self, phases):
        self.phases = phases
        self.current_phase_index = 0

    def get_phase(self, phase_id):
        return next(
            (phase for phase in self.phases if phase.get("phase_id") == phase_id),
            None,
        )

    def get_current_phase(self):
        if 0 <= self.current_phase_index < len(self.phases):
            return self.phases[self.current_phase_index]
        return None

    def mark_phase_completed(self, phase_id):
        phase = self.get_phase(phase_id)
        phase.update(status="completed", user_confirmed=True)
        return {"success": True}


def test_working_agent_can_record_retryable_model_failure():
    agent = {"status": "working"}

    transition_agent(
        agent,
        "model_failed",
        progress=0,
        message="structured model output retries exhausted",
    )

    assert agent["status"] == "model_failed"
    assert agent["finished_at"]
    transition_agent(agent, "working")
    assert agent["status"] == "working"


def _phase_context(project_id, phase_id, agent_status="completed", subproject_status="completed"):
    return SimpleNamespace(
        project_id=project_id,
        workspace=_test_workspace(project_id),
        status="executing",
        description="project",
        pm=SimpleNamespace(context_summary="context"),
        agents={
            "agent-1": {
                "id": "agent-1",
                "phase_id": phase_id,
                "subproject_id": "sp-1",
                "status": agent_status,
            }
        },
        subprojects=[{
            "id": "sp-1",
            "phase_id": phase_id,
            "agent_id": "agent-1",
            "status": subproject_status,
            "progress": 100 if subproject_status == "completed" else 0,
        }],
        qc_results={},
    )


@pytest.mark.parametrize(
    ("agent_status", "subproject_status"),
    [("working", "in_progress"), ("completed", "failed")],
)
def test_review_rejects_incomplete_phase_execution(
    monkeypatch, agent_status, subproject_status
):
    project_id = f"review-guard-{agent_status}-{subproject_status}"
    phase_id = "phase-1"
    phase = {"phase_id": phase_id, "status": "active"}
    ctx = _phase_context(project_id, phase_id, agent_status, subproject_status)
    pm = _PhaseManager([phase])
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_phases.review_phase(project_id, phase_id))

    assert exc.value.status_code == 410
    assert "Legacy phase review is retired" in str(exc.value.detail)
    assert phase["status"] == "active"


def test_review_rejects_an_active_repair_loop(monkeypatch):
    project_id = "review-active-repair"
    phase_id = "phase-1"
    phase = {"phase_id": phase_id, "status": "reviewing"}
    ctx = _phase_context(project_id, phase_id)
    pm = _PhaseManager([phase])
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)
    monkeypatch.setitem(
        routes_phases._auto_repair_states,
        f"{project_id}-{phase_id}",
        {"running": True, "status": "running"},
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_phases.review_phase(project_id, phase_id))

    assert exc.value.status_code == 410


def test_initial_auto_repair_rejects_incomplete_phase(monkeypatch):
    project_id = "auto-repair-guard"
    phase_id = "phase-1"
    phase = {"phase_id": phase_id, "status": "active"}
    ctx = _phase_context(project_id, phase_id, "failed", "failed")
    pm = _PhaseManager([phase])
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_phases.start_auto_repair(project_id, phase_id))

    assert exc.value.status_code == 409
    assert f"{project_id}-{phase_id}" not in routes_phases._auto_repair_states


def test_confirm_rechecks_current_execution_state_without_mutating_failure(monkeypatch):
    project_id = "confirm-current-state"
    phase_id = "phase-1"
    phase = {
        "phase_id": phase_id,
        "status": "reviewing",
        "reviewed": True,
        "review_passed": True,
    }
    ctx = _phase_context(project_id, phase_id, "failed", "failed")
    pm = _PhaseManager([phase])
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_phases.confirm_phase_complete(project_id, phase_id))

    assert exc.value.status_code == 409
    assert phase["status"] == "reviewing"
    assert ctx.agents["agent-1"]["status"] == "failed"
    assert ctx.subprojects[0]["status"] == "failed"


def test_confirm_holds_project_write_guard_through_receipt_persistence(
    monkeypatch,
    tmp_path,
):
    project_id = "confirm-artifact-transaction"
    phase_id = "phase-1"
    phase = {
        "phase_id": phase_id,
        "name": "Phase 1",
        "status": "reviewing",
        "reviewed": True,
        "review_passed": True,
        "execution_generation": "generation-1",
        "execution_contract_digest": "contract-1",
        "execution_requirements_revision": 1,
        "execution_artifact_baseline_digest": "baseline-1",
    }
    ctx = _phase_context(project_id, phase_id)
    ctx.workspace = tmp_path
    ctx.supervisor_quality_runs = {
        phase_id: {
            "status": "completed",
            "completion_gate": {"passed": True},
        },
    }
    pm = _PhaseManager([phase])
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)
    monkeypatch.setattr(
        routes_phases.SupervisorQualityMachine,
        "from_dict",
        lambda _payload: SimpleNamespace(),
    )

    checkpoints = []

    def competing_writer_outcome():
        outcome = []

        def writer():
            try:
                with routes_phases.project_write_guard(
                    project_id, ctx.workspace,
                ):
                    outcome.append("acquired")
            except routes_phases.ProjectWriteFenceConflict:
                outcome.append("conflict")

        thread = threading.Thread(target=writer)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        return outcome

    def assert_current_artifact(*_args):
        checkpoints.append(("artifact", competing_writer_outcome()))
        return {}

    monkeypatch.setattr(
        routes_phases,
        "_assert_supervisor_artifact_current",
        assert_current_artifact,
    )
    monkeypatch.setattr(
        routes_phases,
        "_record_server_acceptance_evidence",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        routes_phases,
        "_record_user_confirmation_evidence",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        routes_phases,
        "_assert_phase_execution_completed",
        lambda *_args, **_kwargs: {
            "tasks": [{"task_id": "phase-1-task-1"}],
        },
    )

    async def persist():
        assert phase["validated_completion_receipt"]["execution_generation"] == (
            "generation-1"
        )
        checkpoints.append(("persist", competing_writer_outcome()))

    monkeypatch.setattr(routes_phases, "_persist_all_async", persist)

    result = asyncio.run(
        routes_phases.confirm_phase_complete(project_id, phase_id),
    )

    assert result["success"] is True
    assert checkpoints == [
        ("artifact", ["conflict"]),
        ("persist", ["conflict"]),
    ]
    with routes_phases.project_write_guard(project_id, ctx.workspace):
        pass


def test_start_phase_requires_previous_phase_user_confirmation(monkeypatch):
    monkeypatch.setenv("METIS_TEST_MODE", "1")
    project_id = "start-order-guard"
    previous = {
        "phase_id": "phase-1",
        "status": "reviewing",
        "review_passed": True,
        "user_confirmed": False,
    }
    current = {"phase_id": "phase-2", "status": "pending"}
    pm = _PhaseManager([previous, current])
    pm.current_phase_index = 1
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=_test_workspace(project_id),
        status="planning",
        agents={},
        subprojects=[],
    )
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_phases.start_phase(project_id, "phase-2"))

    assert exc.value.status_code == 409
    assert current["status"] == "pending"


@pytest.mark.parametrize("status", ["active", "reviewing", "completed"])
def test_start_phase_rejects_a_non_startable_current_state(monkeypatch, status):
    monkeypatch.setenv("METIS_TEST_MODE", "1")
    project_id = f"start-state-{status}"
    phase = {"phase_id": "phase-1", "status": status}
    pm = _PhaseManager([phase])
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=_test_workspace(project_id),
        status="planning",
        agents={},
        subprojects=[],
    )
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_phases.start_phase(project_id, "phase-1"))

    assert exc.value.status_code == 409
    assert phase["status"] == status


@pytest.mark.parametrize(
    ("status", "response_flag"),
    [("working", "already_running"), ("completed", "already_completed")],
)
def test_single_agent_execute_is_idempotent_for_active_or_completed(
    monkeypatch, status, response_flag
):
    project_id = f"single-agent-{status}"
    agent_id = "agent-1"
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=_test_workspace(project_id),
        agents={agent_id: {
            "id": agent_id,
            "status": status,
            "subproject_id": "sp-1",
        }},
        subprojects=[{"id": "sp-1", "name": "task", "description": "task"}],
        pm=SimpleNamespace(context_summary="context"),
        description="project",
    )
    scheduled = []
    monkeypatch.setitem(routes_execution.projects, project_id, ctx)
    monkeypatch.setattr(
        routes_execution,
        "_safe_create_task",
        lambda coro, name="": scheduled.append((coro, name)),
    )

    result = asyncio.run(routes_execution.execute_agent_task(project_id, agent_id))

    assert result["success"] is True
    assert result[response_flag] is True
    assert scheduled == []


def test_locked_agent_status_waits_for_current_receipt_and_projects_failure(
    monkeypatch,
) -> None:
    project_id = "locked-receipt-status"
    phase_id = "phase-1"
    agent_id = "agent-1"
    generation = "generation-1"
    contract_digest = "sha256:" + ("a" * 64)
    baseline_digest = "sha256:" + ("b" * 64)
    phase = {
        "phase_id": phase_id,
        "execution_generation": generation,
        "execution_contract_digest": contract_digest,
        "execution_requirements_revision": 3,
        "execution_artifact_baseline_digest": baseline_digest,
        "execution_coordinator": {
            "status": "running",
            "durable_run_id": "coordinator-status-run",
        },
        "execution_dispatch_result": {},
    }
    receipt = {
        "task_id": "task-1",
        "agent_id": agent_id,
        "phase_id": phase_id,
        "status": "pending",
        "execution_generation": generation,
        "contract_digest": contract_digest,
        "requirements_revision": 3,
        "artifact_baseline_digest": baseline_digest,
    }
    agent = {
        "id": agent_id,
        "phase_id": phase_id,
        "status": "completed",
        "progress": 100,
        "locked_tasks": [{"task_id": "task-1"}],
        "task_execution_receipts": {"task-1": receipt},
    }
    ctx = SimpleNamespace(project_id=project_id, agents={agent_id: agent})
    pm = _PhaseManager([phase])
    monkeypatch.setitem(routes_execution.projects, project_id, ctx)
    monkeypatch.setitem(routes_execution._phase_managers, project_id, pm)
    monkeypatch.setitem(routes_execution.execution_status, agent_id, {
        "status": "completed", "progress": 100, "output_files": ["README.md"],
    })
    monkeypatch.setattr(
        routes_execution._run_registry,
        "get",
        lambda run_id: {
            "run_id": run_id,
            "run_type": "phase.dispatch",
            "status": "succeeded",
            "payload": {
                "project_id": project_id,
                "phase_id": phase_id,
                "execution_generation": generation,
                "contract_digest": contract_digest,
                "requirements_revision": 3,
                "artifact_baseline_digest": baseline_digest,
                "dispatch_attempt_digest": "",
            },
        },
    )

    agent["task_execution_receipts"] = {}
    routes_execution.execution_status[agent_id].update({
        "status": "failed",
        "error": "Model output format retries exhausted",
    })
    failed_without_receipt = asyncio.run(
        routes_execution.get_agent_execution_status(project_id, agent_id)
    )
    assert failed_without_receipt["status"] == "failed"
    assert "retries exhausted" in failed_without_receipt["error"]

    agent["task_execution_receipts"] = {"task-1": receipt}
    routes_execution.execution_status[agent_id].update({
        "status": "completed",
        "progress": 100,
        "error": "",
    })
    pending = asyncio.run(
        routes_execution.get_agent_execution_status(project_id, agent_id)
    )
    assert (pending["status"], pending["progress"]) == ("working", 99)

    receipt.update(status="failed", error_code="FileNotFoundError")
    failed = asyncio.run(
        routes_execution.get_agent_execution_status(project_id, agent_id)
    )
    assert failed["status"] == "failed"
    assert failed["error"] == "FileNotFoundError"

    receipt.update(status="succeeded", completion_run_id="run-1")
    still_finalizing = asyncio.run(
        routes_execution.get_agent_execution_status(project_id, agent_id)
    )
    assert (still_finalizing["status"], still_finalizing["progress"]) == (
        "working", 99,
    )

    phase["execution_coordinator"]["status"] = "completed"
    phase["execution_dispatch_result"] = {"success": True}
    completed = asyncio.run(
        routes_execution.get_agent_execution_status(project_id, agent_id)
    )
    assert (completed["status"], completed["progress"]) == ("completed", 100)

    routes_execution.execution_status[agent_id].update({
        "status": "failed",
        "error": "stale transient failure",
    })
    completed_over_stale_status = asyncio.run(
        routes_execution.get_agent_execution_status(project_id, agent_id)
    )
    assert (
        completed_over_stale_status["status"],
        completed_over_stale_status["progress"],
        completed_over_stale_status["error"],
    ) == ("completed", 100, "")


def test_locked_agent_status_projects_terminal_durable_task_failure(
    monkeypatch,
) -> None:
    project_id = "locked-durable-task-failure"
    phase_id = "phase-1"
    agent_id = "agent-1"
    generation = "generation-1"
    contract_digest = "sha256:" + ("a" * 64)
    baseline_digest = "sha256:" + ("b" * 64)
    phase = {
        "phase_id": phase_id,
        "execution_generation": generation,
        "execution_contract_digest": contract_digest,
        "execution_requirements_revision": 3,
        "execution_artifact_baseline_digest": baseline_digest,
        "execution_coordinator": {
            "status": "running",
            "durable_run_id": "coordinator-run",
        },
    }
    receipt = {
        "task_id": "task-1",
        "agent_id": agent_id,
        "phase_id": phase_id,
        "status": "running",
        "start_run_id": "task-run-1",
        "execution_generation": generation,
        "contract_digest": contract_digest,
        "requirements_revision": 3,
        "artifact_baseline_digest": baseline_digest,
    }
    agent = {
        "id": agent_id,
        "phase_id": phase_id,
        "locked_tasks": [{"task_id": "task-1"}],
        "task_execution_receipts": {"task-1": receipt},
    }
    ctx = SimpleNamespace(project_id=project_id, agents={agent_id: agent})
    monkeypatch.setitem(routes_execution.projects, project_id, ctx)
    monkeypatch.setitem(
        routes_execution._phase_managers,
        project_id,
        _PhaseManager([phase]),
    )
    monkeypatch.setitem(routes_execution.execution_status, agent_id, {
        "status": "working",
        "progress": 99,
    })
    monkeypatch.setattr(
        routes_execution._run_registry,
        "get",
        lambda run_id: {
            "run_id": run_id,
            "run_type": "agent.execute",
            "status": "failed",
            "last_error": "route contract validation failed",
        },
    )

    result = asyncio.run(
        routes_execution.get_agent_execution_status(project_id, agent_id)
    )

    assert result["status"] == "failed"
    assert result["progress"] == 0
    assert result["error"] == "route contract validation failed"


def test_running_locked_coordinator_cannot_project_completion_or_start_qa(
    monkeypatch,
) -> None:
    project_id = "locked-running-coordinator"
    phase_id = "phase-1"
    agent_id = "agent-1"
    generation = "generation-2"
    contract_digest = "sha256:" + ("c" * 64)
    baseline_digest = "sha256:" + ("d" * 64)
    phase = {
        "phase_id": phase_id,
        "subprojects": ["sp-1"],
        "status": "in_progress",
        "execution_generation": generation,
        "execution_contract_digest": contract_digest,
        "execution_artifact_baseline_digest": baseline_digest,
        "execution_requirements_revision": 4,
        "execution_dispatch_plan": {"task_ids": ["task-1"]},
        "execution_coordinator": {
            "status": "running",
            "durable_run_id": "coordinator-1",
        },
        "execution_dispatch_result": {"success": True},
    }
    receipt = {
        "task_id": "task-1",
        "agent_id": agent_id,
        "phase_id": phase_id,
        "status": "succeeded",
        "completion_run_id": "old-success-run",
        "execution_generation": generation,
        "contract_digest": contract_digest,
        "artifact_baseline_digest": baseline_digest,
        "requirements_revision": 4,
    }
    agent = {
        "id": agent_id,
        "phase_id": phase_id,
        "status": "completed",
        "progress": 100,
        "task_execution_receipts": {"task-1": receipt},
    }
    child = {
        "id": "sp-1",
        "phase_id": phase_id,
        "agent_id": agent_id,
        "status": "completed",
        "progress": 100,
    }
    ctx = SimpleNamespace(
        project_id=project_id,
        agents={agent_id: agent},
        subprojects=[child],
    )
    monkeypatch.setitem(
        routes_execution._phase_managers,
        project_id,
        _PhaseManager([phase]),
    )
    qa_calls = []

    async def fake_start_auto_repair(*args, **kwargs):
        qa_calls.append((args, kwargs))

    monkeypatch.setattr(
        routes_phases, "start_auto_repair", fake_start_auto_repair,
    )

    asyncio.run(routes_execution._start_phase_quality_cycle_if_ready(ctx))

    assert (agent["status"], agent["progress"]) == ("completed", 100)
    assert (child["status"], child["progress"]) == ("completed", 100)
    assert qa_calls == []

    phase["execution_coordinator"]["status"] = "completed"
    receipt["artifact_baseline_digest"] = "sha256:" + ("e" * 64)
    asyncio.run(routes_execution._start_phase_quality_cycle_if_ready(ctx))

    assert (agent["status"], agent["progress"]) == ("completed", 100)
    assert (child["status"], child["progress"]) == ("completed", 100)
    assert qa_calls == []


def test_unprojectable_locked_receipt_preserves_persisted_quality_state(
    monkeypatch,
) -> None:
    project_id = "startup-quality-owns-completion"
    phase_id = "phase-1"
    agent_id = "agent-1"
    generation = "generation-1"
    contract_digest = "sha256:" + ("a" * 64)
    baseline_digest = "sha256:" + ("b" * 64)
    phase = {
        "phase_id": phase_id,
        "subprojects": ["sp-1"],
        "status": "waiting_engineer",
        "execution_generation": generation,
        "execution_contract_digest": contract_digest,
        "execution_artifact_baseline_digest": baseline_digest,
        "execution_requirements_revision": 1,
        "execution_dispatch_plan": {"task_ids": ["task-1"]},
        "execution_coordinator": {
            "status": "completed",
            "durable_run_id": "coordinator-not-yet-projectable",
        },
        "execution_dispatch_result": {"success": True},
    }
    receipt = {
        "task_id": "task-1",
        "agent_id": agent_id,
        "phase_id": phase_id,
        "status": "succeeded",
        "completion_run_id": "task-run-1",
        "execution_generation": generation,
        "contract_digest": contract_digest,
        "artifact_baseline_digest": baseline_digest,
        "requirements_revision": 1,
    }
    agent = {
        "id": agent_id,
        "phase_id": phase_id,
        "status": "completed",
        "progress": 100,
        "task_execution_receipts": {"task-1": receipt},
    }
    child = {
        "id": "sp-1",
        "phase_id": phase_id,
        "agent_id": agent_id,
        "status": "completed",
        "progress": 100,
    }
    ctx = SimpleNamespace(
        project_id=project_id,
        agents={agent_id: agent},
        subprojects=[child],
        supervisor_quality_runs={
            phase_id: {
                "state": "waiting_engineer",
                "waiting_for": [agent_id],
            },
        },
    )
    state = {
        "running": False,
        "status": "pre_qa_failed",
        "pre_qa_repair_runs": {"agent-1": "repair-run-pending"},
        "action_required": {"message": "Engineer repair is still pending"},
    }
    monkeypatch.setitem(
        routes_execution._phase_managers,
        project_id,
        _PhaseManager([phase]),
    )
    monkeypatch.setitem(
        routes_phases._auto_repair_states,
        f"{project_id}-{phase_id}",
        state,
    )
    monkeypatch.setattr(
        routes_phases,
        "_current_phase_coordinator_run",
        lambda *_args, **_kwargs: None,
    )
    qa_calls = []
    resume_calls = []

    async def fake_start_auto_repair(*args, **kwargs):
        qa_calls.append((args, kwargs))

    async def fake_resume(*args, **kwargs):
        resume_calls.append((args, kwargs))
        return False

    monkeypatch.setattr(
        routes_phases,
        "start_auto_repair",
        fake_start_auto_repair,
    )
    monkeypatch.setattr(
        routes_phases,
        "_resume_completed_pre_qa_repair_runs",
        fake_resume,
    )

    asyncio.run(routes_execution._start_phase_quality_cycle_if_ready(ctx))

    assert (agent["status"], agent["progress"]) == ("completed", 100)
    assert (child["status"], child["progress"]) == ("completed", 100)
    assert phase["status"] == "waiting_engineer"
    assert state == {
        "running": False,
        "status": "pre_qa_failed",
        "pre_qa_repair_runs": {"agent-1": "repair-run-pending"},
        "action_required": {"message": "Engineer repair is still pending"},
    }
    assert receipt["completion_run_id"] == "task-run-1"
    assert qa_calls == []
    assert resume_calls == []


def test_new_execution_generation_detaches_terminal_quality_state_before_qa(
    monkeypatch,
) -> None:
    """A phase id is not a sufficient idempotency key across executions."""
    project_id = "quality-generation-rollover"
    phase_id = "phase-1"
    agent_id = "agent-1"
    old_generation = "generation-old"
    generation = "generation-new"
    contract_digest = "sha256:" + ("c" * 64)
    baseline_digest = "sha256:" + ("d" * 64)
    phase = {
        "phase_id": phase_id,
        "subprojects": ["sp-1"],
        "status": "in_progress",
        "started_at": 200.0,
        "execution_generation": generation,
        "execution_contract_digest": contract_digest,
        "execution_artifact_baseline_digest": baseline_digest,
        "execution_requirements_revision": 2,
        "execution_dispatch_plan": {"task_ids": ["task-1"]},
        "execution_coordinator": {
            "status": "completed",
            "durable_run_id": "coordinator-new",
        },
        "execution_dispatch_result": {"success": True},
    }
    receipt = {
        "task_id": "task-1",
        "agent_id": agent_id,
        "phase_id": phase_id,
        "status": "succeeded",
        "completion_run_id": "task-run-new",
        "execution_generation": generation,
        "contract_digest": contract_digest,
        "artifact_baseline_digest": baseline_digest,
        "requirements_revision": 2,
    }
    agent = {
        "id": agent_id,
        "phase_id": phase_id,
        "status": "completed",
        "progress": 100,
        "task_execution_receipts": {"task-1": receipt},
    }
    child = {
        "id": "sp-1",
        "phase_id": phase_id,
        "agent_id": agent_id,
        "status": "completed",
        "progress": 100,
    }
    old_supervisor_run = {
        "run_id": "supervisor-old",
        "status": "completed",
        "scope": {
            "project_id": project_id,
            "phase_id": phase_id,
            "phase_generation_id": "100.0",
        },
        "completion_gate": {"passed": True},
    }
    old_state = {
        "running": False,
        "status": "passed",
        "execution_generation": old_generation,
        "requirements_revision": 1,
        "review_result": {"passed": True},
        "supervisor_run": old_supervisor_run,
    }
    ctx = SimpleNamespace(
        project_id=project_id,
        agents={agent_id: agent},
        subprojects=[child],
        supervisor_quality_runs={phase_id: old_supervisor_run},
    )
    key = f"{project_id}-{phase_id}"
    monkeypatch.setitem(
        routes_execution._phase_managers,
        project_id,
        _PhaseManager([phase]),
    )
    monkeypatch.setitem(routes_phases._auto_repair_states, key, old_state)
    monkeypatch.setattr(
        routes_phases,
        "_current_phase_coordinator_run",
        lambda *_args, **_kwargs: {"run_id": "coordinator-new"},
    )
    qa_calls = []

    async def fake_start_auto_repair(called_project_id, called_phase_id):
        # The real starter reads both registries. Leaving either old terminal
        # record in place would return "already completed" for this new run.
        replacement = routes_phases._auto_repair_states.get(key)
        assert replacement is not old_state
        assert (
            replacement is None
            or replacement.get("execution_generation") == generation
        )
        assert ctx.supervisor_quality_runs.get(phase_id) is not old_supervisor_run
        qa_calls.append((called_project_id, called_phase_id))

    monkeypatch.setattr(
        routes_phases,
        "start_auto_repair",
        fake_start_auto_repair,
    )

    asyncio.run(routes_execution._start_phase_quality_cycle_if_ready(ctx))

    assert qa_calls == [(project_id, phase_id)]
    assert (agent["status"], agent["progress"]) == ("completed", 100)
    assert (child["status"], child["progress"]) == ("completed", 100)


@pytest.mark.parametrize(
    ("agent_status", "repair_runs"),
    [
        ("fix_required", {}),
        ("fix_required", {"agent-1": "pending-repair-run"}),
        ("completed", {}),
        ("completed", {"agent-1": "pending-repair-run"}),
    ],
    ids=[
        "repair-required-dispatch-missing",
        "repair-required-run-pending",
        "local-completion-dispatch-missing",
        "local-completion-run-pending",
    ],
)
def test_persisted_pre_qa_waiting_engineer_does_not_reuse_initial_receipt(
    monkeypatch,
    agent_status,
    repair_runs,
) -> None:
    """Startup must keep the failed pre-QA generation blocked for fresh repair."""
    project_id = f"startup-pre-qa-{len(repair_runs)}"
    phase_id = "phase-1"
    agent_id = "agent-1"
    generation = "generation-1"
    contract_digest = "sha256:" + ("a" * 64)
    baseline_digest = "sha256:" + ("b" * 64)
    phase = {
        "phase_id": phase_id,
        "subprojects": ["sp-1"],
        "status": "waiting_engineer",
        "execution_generation": generation,
        "execution_contract_digest": contract_digest,
        "execution_artifact_baseline_digest": baseline_digest,
        "execution_requirements_revision": 1,
        "execution_dispatch_plan": {"task_ids": ["task-1"]},
        "execution_coordinator": {
            "status": "completed",
            "durable_run_id": "initial-coordinator-run",
        },
        "execution_dispatch_result": {"success": True},
    }
    initial_receipt = {
        "task_id": "task-1",
        "agent_id": agent_id,
        "phase_id": phase_id,
        "status": "succeeded",
        "completion_run_id": "initial-task-run",
        "execution_generation": generation,
        "contract_digest": contract_digest,
        "artifact_baseline_digest": baseline_digest,
        "requirements_revision": 1,
    }
    agent = {
        "id": agent_id,
        "phase_id": phase_id,
        "status": agent_status,
        "progress": 100 if agent_status == "completed" else 0,
        "fix_task": "Replace the README placeholder",
        "task_execution_receipts": {"task-1": initial_receipt},
    }
    child = {
        "id": "sp-1",
        "phase_id": phase_id,
        "agent_id": agent_id,
        "status": agent_status,
        "progress": 100 if agent_status == "completed" else 0,
    }
    ctx = SimpleNamespace(
        project_id=project_id,
        agents={agent_id: agent},
        subprojects=[child],
        supervisor_quality_runs={
            phase_id: {
                "state": "waiting_engineer",
                "waiting_for": [agent_id],
            },
        },
    )
    state = {
        "running": False,
        "status": "pre_qa_failed",
        "pre_qa_repair_runs": dict(repair_runs),
        "action_required": {
            "message": "Engineer repair is required before verification",
        },
    }
    monkeypatch.setitem(
        routes_execution._phase_managers,
        project_id,
        _PhaseManager([phase]),
    )
    monkeypatch.setitem(
        routes_phases._auto_repair_states,
        f"{project_id}-{phase_id}",
        state,
    )
    monkeypatch.setattr(
        routes_phases,
        "_current_phase_coordinator_run",
        lambda *_args, **_kwargs: {"status": "succeeded"},
    )
    monkeypatch.setattr(
        routes_phases,
        "_phase_rebuild_terminal_failures",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        routes_execution._run_registry,
        "get",
        lambda run_id: {"run_id": run_id, "status": "pending"},
    )
    qa_calls = []
    scheduled = []

    async def fake_start_auto_repair(*args, **kwargs):
        qa_calls.append((args, kwargs))

    def capture_task(coro, name=""):
        scheduled.append(name)
        coro.close()

    monkeypatch.setattr(
        routes_phases,
        "start_auto_repair",
        fake_start_auto_repair,
    )
    monkeypatch.setattr(routes_phases, "_safe_create_task", capture_task)

    asyncio.run(routes_execution._start_phase_quality_cycle_if_ready(ctx))

    expected_progress = 100 if agent_status == "completed" else 0
    assert (agent["status"], agent["progress"]) == (
        agent_status,
        expected_progress,
    )
    assert (child["status"], child["progress"]) == (
        agent_status,
        expected_progress,
    )
    assert phase["status"] == "waiting_engineer"
    assert state["running"] is False
    assert state["status"] == "pre_qa_failed"
    assert state["action_required"]
    assert initial_receipt["completion_run_id"] == "initial-task-run"
    assert qa_calls == []
    assert scheduled == []


def test_orphaned_locked_child_run_is_rejected_before_execution(
    monkeypatch,
) -> None:
    project_id = "orphaned-child-run"
    phase_id = "phase-1"
    phase = {
        "phase_id": phase_id,
        "execution_generation": "generation-1",
        "execution_contract_digest": "sha256:" + ("a" * 64),
        "execution_requirements_revision": 1,
        "execution_artifact_baseline_digest": "sha256:" + ("b" * 64),
        "execution_coordinator": {
            "status": "running",
            "durable_run_id": "new-coordinator",
            "dispatch_attempt_digest": "new-attempt",
        },
    }
    monkeypatch.setitem(
        routes_execution._phase_managers,
        project_id,
        _PhaseManager([phase]),
    )
    monkeypatch.setattr(
        routes_execution._run_registry,
        "get",
        lambda _run_id: {
            "status": "pending",
            "payload": {
                "project_id": project_id,
                "phase_id": phase_id,
                "dispatch_attempt_digest": "old-attempt",
            },
        },
    )

    with pytest.raises(Exception) as caught:
        routes_execution._assert_current_phase_attempt({
            "project_id": project_id,
            "phase_id": phase_id,
            "task_id": "task-1",
            "agent_id": "agent-1",
            "execution_generation": "generation-1",
            "contract_digest": "sha256:" + ("a" * 64),
            "requirements_revision": 1,
            "artifact_baseline_digest": "sha256:" + ("b" * 64),
            "phase_coordinator_run_id": "old-coordinator",
            "dispatch_attempt_digest": "old-attempt",
        })

    assert "stale or superseded" in str(caught.value)


def _current_locked_child_attempt_state():
    project_id = "current-child-attempt"
    phase_id = "phase-1"
    task_id = "task-1"
    agent_id = "agent-1"
    coordinator_run_id = "coordinator-1"
    generation = "generation-1"
    contract_digest = "sha256:" + ("a" * 64)
    baseline_digest = "sha256:" + ("b" * 64)
    required_path = "backend/app.py"
    phase = {
        "phase_id": phase_id,
        "execution_generation": generation,
        "execution_contract_digest": contract_digest,
        "execution_requirements_revision": 3,
        "execution_artifact_baseline_digest": baseline_digest,
        "execution_dispatch_plan": {
            "task_ids": [task_id],
            "waves": [[{
                "task_id": task_id,
                "agent_id": agent_id,
                "dependencies": [],
            }]],
        },
        "execution_coordinator": {
            "status": "running",
            "durable_run_id": coordinator_run_id,
            "dispatch_attempt_digest": "",
        },
    }
    phase_manager = _PhaseManager([phase])
    phase_manager.project_contract = {
        "locked": True,
        "contract_version": 3,
        "required_files": [{
            "path": required_path,
            "phase_id": phase_id,
            "task_id": task_id,
            "required": True,
        }],
    }
    coordinator_run = {
        "run_id": coordinator_run_id,
        "run_type": "phase.dispatch",
        "project_id": project_id,
        "status": "pending",
        "payload": {
            "project_id": project_id,
            "phase_id": phase_id,
            "execution_generation": generation,
            "contract_digest": contract_digest,
            "requirements_revision": 3,
            "artifact_baseline_digest": baseline_digest,
            "dispatch_attempt_digest": "",
        },
    }
    child_payload = {
        "project_id": project_id,
        "phase_id": phase_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "execution_generation": generation,
        "contract_digest": contract_digest,
        "requirements_revision": 3,
        "artifact_baseline_digest": baseline_digest,
        "phase_coordinator_run_id": coordinator_run_id,
        "dispatch_attempt_digest": "",
        "artifact_policy": {
            "required_files": [required_path],
            "allowed_path_prefixes": [required_path],
            "rebuild_file_specs": [],
        },
    }
    return (
        project_id,
        phase_id,
        phase_manager,
        phase,
        coordinator_run,
        child_payload,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_parent_run_type",
        "stale_parent_generation",
        "stale_parent_contract",
        "stale_parent_revision",
        "stale_parent_baseline",
        "unknown_task",
        "duplicate_task_owner",
        "repair_parent_non_repair_task",
        "artifact_policy_scope_escalation",
    ],
)
def test_locked_child_attempt_rejects_mutated_parent_or_scope(
    monkeypatch, mutation,
) -> None:
    (
        project_id,
        _phase_id,
        phase_manager,
        phase,
        coordinator_run,
        child_payload,
    ) = _current_locked_child_attempt_state()
    monkeypatch.setitem(
        routes_execution._phase_managers, project_id, phase_manager,
    )
    monkeypatch.setattr(
        routes_execution._run_registry,
        "get",
        lambda _run_id: coordinator_run,
    )

    # Prove the unmodified identity is accepted before killing one invariant.
    routes_execution._assert_current_phase_attempt(child_payload)

    if mutation == "wrong_parent_run_type":
        coordinator_run["run_type"] = "agent.execute"
    elif mutation == "stale_parent_generation":
        coordinator_run["payload"]["execution_generation"] = "stale-generation"
    elif mutation == "stale_parent_contract":
        coordinator_run["payload"]["contract_digest"] = "sha256:" + ("c" * 64)
    elif mutation == "stale_parent_revision":
        coordinator_run["payload"]["requirements_revision"] = 2
    elif mutation == "stale_parent_baseline":
        coordinator_run["payload"]["artifact_baseline_digest"] = (
            "sha256:" + ("d" * 64)
        )
    elif mutation == "unknown_task":
        child_payload["task_id"] = "task-unknown"
    elif mutation == "duplicate_task_owner":
        phase["execution_dispatch_plan"]["waves"].append([{
            "task_id": "task-1",
            "agent_id": "agent-1",
            "dependencies": [],
        }])
    elif mutation == "repair_parent_non_repair_task":
        phase["execution_dispatch_plan"]["task_ids"].append("task-2")
        phase["execution_dispatch_plan"]["waves"].append([{
            "task_id": "task-2",
            "agent_id": "agent-2",
            "dependencies": [],
        }])
        phase["execution_coordinator"].update({
            "dispatch_attempt_digest": "repair-attempt",
            "repair_task_ids": ["task-2"],
        })
        coordinator_run["payload"]["dispatch_attempt_digest"] = "repair-attempt"
        child_payload["dispatch_attempt_digest"] = "repair-attempt"
    elif mutation == "artifact_policy_scope_escalation":
        child_payload["artifact_policy"]["allowed_path_prefixes"] = ["*"]
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(f"unknown mutation: {mutation}")

    with pytest.raises(LeaseConflict, match="stale or superseded"):
        routes_execution._assert_current_phase_attempt(child_payload)


def test_project_execution_guard_rechecks_current_attempt_on_every_write(
    monkeypatch, tmp_path,
) -> None:
    (
        project_id,
        _phase_id,
        phase_manager,
        phase,
        coordinator_run,
        child_payload,
    ) = _current_locked_child_attempt_state()
    monkeypatch.setitem(
        routes_execution._phase_managers, project_id, phase_manager,
    )
    monkeypatch.setattr(
        routes_execution._run_registry,
        "get",
        lambda _run_id: coordinator_run,
    )
    monkeypatch.setattr(
        "core.project_write_fence.expert_lock.get_active_locks",
        lambda **_kwargs: [{"lock_id": "child-file-lock"}],
    )
    checks = []

    def assert_current_attempt():
        checks.append(phase["execution_coordinator"]["durable_run_id"])
        routes_execution._assert_current_phase_attempt(child_payload)

    guard = ProjectExecutionGuard(
        project_id,
        tmp_path,
        generation="child-run:1",
        lock_id="child-file-lock",
        authorization_check=assert_current_attempt,
    )

    first_write_entered = False
    with guard.write_guard():
        first_write_entered = True
    assert first_write_entered is True

    phase["execution_coordinator"]["durable_run_id"] = "successor-coordinator"
    stale_write_entered = False
    with pytest.raises(LeaseConflict, match="stale or superseded"):
        with guard.write_guard():
            stale_write_entered = True

    assert stale_write_entered is False
    assert checks == ["coordinator-1", "successor-coordinator"]


def test_durable_lease_loss_revokes_child_write_guard(
    monkeypatch, tmp_path,
) -> None:
    run_id = "child-run-lost-lease"
    lock_id = "child-file-lock"
    heartbeat_calls = []

    class LostLeaseRegistry:
        def heartbeat(self, candidate_run_id, owner, *, lease_seconds):
            heartbeat_calls.append((candidate_run_id, owner, lease_seconds))
            raise LeaseConflict("child durable lease was superseded")

    class ImmediateHeartbeat:
        def is_set(self):
            return False

        async def wait(self):
            raise asyncio.TimeoutError

    monkeypatch.setattr(routes_execution, "_run_registry", LostLeaseRegistry())
    monkeypatch.setattr(
        "core.project_write_fence.expert_lock.get_active_locks",
        lambda **_kwargs: [{"lock_id": lock_id}],
    )
    cancel_event = threading.Event()
    execution_guard = ProjectExecutionGuard(
        "lease-loss-fence",
        tmp_path,
        generation=f"{run_id}:1",
        lock_id=lock_id,
    )
    monkeypatch.setitem(
        routes_execution._run_cancel_events, run_id, cancel_event,
    )
    monkeypatch.setitem(
        routes_execution._run_execution_guards, run_id, execution_guard,
    )

    with execution_guard.write_guard():
        pass

    asyncio.run(routes_execution._maintain_durable_run_lease(
        run_id,
        "superseded-owner",
        ImmediateHeartbeat(),
    ))

    assert heartbeat_calls == [
        (run_id, "superseded-owner", routes_execution._RUN_LEASE_SECONDS),
    ]
    assert cancel_event.is_set()
    assert execution_guard.valid is False
    with pytest.raises(RevokedExecutionGuard, match="no longer authorized"):
        with execution_guard.write_guard():
            pytest.fail("lease-losing child entered a later write section")


def test_durable_worker_blocks_orphan_before_file_lease_or_agent_task(
    monkeypatch,
) -> None:
    project_id = "orphaned-child-worker"
    phase_id = "phase-1"
    agent_id = "agent-1"
    child_run_id = "old-child"
    old_coordinator_id = "old-coordinator"
    generation = "generation-1"
    contract_digest = "sha256:" + ("a" * 64)
    baseline_digest = "sha256:" + ("b" * 64)
    phase = {
        "phase_id": phase_id,
        "execution_generation": generation,
        "execution_contract_digest": contract_digest,
        "execution_requirements_revision": 1,
        "execution_artifact_baseline_digest": baseline_digest,
        "execution_coordinator": {
            "status": "running",
            "durable_run_id": "new-coordinator",
            "dispatch_attempt_digest": "new-attempt",
        },
    }
    child_payload = {
        "project_id": project_id,
        "phase_id": phase_id,
        "task_id": "task-1",
        "agent_id": agent_id,
        "subproject_id": "sp-1",
        "execution_generation": generation,
        "contract_digest": contract_digest,
        "requirements_revision": 1,
        "artifact_baseline_digest": baseline_digest,
        "phase_coordinator_run_id": old_coordinator_id,
        "dispatch_attempt_digest": "old-attempt",
    }
    runs = {
        child_run_id: {
            "run_id": child_run_id,
            "status": "pending",
            "next_attempt_at": 0,
            "attempt_count": 0,
            "timeout_seconds": 30,
            "payload": child_payload,
        },
        old_coordinator_id: {
            "run_id": old_coordinator_id,
            "status": "pending",
            "payload": {
                "project_id": project_id,
                "phase_id": phase_id,
                "dispatch_attempt_digest": "old-attempt",
            },
        },
    }
    blocked_reasons = []

    class Registry:
        def get(self, run_id):
            return runs[run_id]

        def claim(self, run_id, _owner, **_kwargs):
            runs[run_id]["status"] = "running"
            return runs[run_id]

        def block(self, run_id, *, reason):
            runs[run_id]["status"] = "blocked"
            runs[run_id]["last_error"] = reason
            blocked_reasons.append(reason)
            return runs[run_id]

    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=_test_workspace(project_id),
        agents={agent_id: {"id": agent_id, "phase_id": phase_id}},
    )
    file_lease_calls = []
    agent_task_calls = []

    async def no_lease_heartbeat(*_args, **_kwargs):
        return None

    def unexpected_file_lease(*args, **kwargs):
        file_lease_calls.append((args, kwargs))
        raise AssertionError("orphan must be blocked before file lease")

    async def unexpected_agent_task(*args, **kwargs):
        agent_task_calls.append((args, kwargs))
        raise AssertionError("orphan must be blocked before Agent execution")

    monkeypatch.setitem(routes_execution.projects, project_id, ctx)
    monkeypatch.setitem(
        routes_execution._phase_managers,
        project_id,
        _PhaseManager([phase]),
    )
    monkeypatch.setattr(routes_execution, "_run_registry", Registry())
    monkeypatch.setattr(
        routes_execution,
        "_maintain_durable_run_lease",
        no_lease_heartbeat,
    )
    monkeypatch.setattr(
        routes_execution,
        "_ensure_agent_run_file_lease",
        unexpected_file_lease,
    )
    monkeypatch.setattr(
        routes_execution,
        "_run_agent_task",
        unexpected_agent_task,
    )

    result = asyncio.run(
        routes_execution._execute_durable_agent_run(child_run_id)
    )

    assert result["status"] == "blocked"
    assert blocked_reasons
    assert "phase task attempt rejected" in blocked_reasons[0]
    assert file_lease_calls == []
    assert agent_task_calls == []


def test_two_owner_repair_receipts_gate_quality_until_both_are_current(
    monkeypatch,
) -> None:
    project_id = "two-owner-repair-receipt-gate"
    phase_id = "phase-1"
    generation = "generation-1"
    contract_digest = "sha256:" + ("c" * 64)
    baseline_digest = "sha256:" + ("d" * 64)
    coordinator_run_id = "repair-coordinator"
    attempt_digest = "repair-attempt"
    owner_tasks = {
        "backend-agent": "task-1",
        "frontend-agent": "task-2",
    }
    phase = {
        "phase_id": phase_id,
        "subprojects": ["sp-1", "sp-2"],
        "status": "in_progress",
        "execution_generation": generation,
        "execution_contract_digest": contract_digest,
        "execution_requirements_revision": 2,
        "execution_artifact_baseline_digest": baseline_digest,
        "execution_dispatch_plan": {
            "task_ids": list(owner_tasks.values()),
        },
        "execution_coordinator": {
            "status": "completed",
            "durable_run_id": coordinator_run_id,
            "dispatch_attempt_digest": attempt_digest,
            "repair_task_ids": list(owner_tasks.values()),
            "repair_agent_ids": list(owner_tasks),
        },
        "execution_dispatch_result": {"success": True},
    }
    agents = {}
    children = []
    for index, (agent_id, task_id) in enumerate(owner_tasks.items(), start=1):
        agents[agent_id] = {
            "id": agent_id,
            "phase_id": phase_id,
            "status": "completed",
            "progress": 100,
            "task_execution_receipts": {
                task_id: {
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "phase_id": phase_id,
                    "status": "succeeded",
                    "completion_run_id": f"old-{task_id}",
                    "execution_generation": generation,
                    "contract_digest": contract_digest,
                    "requirements_revision": 2,
                    "artifact_baseline_digest": baseline_digest,
                },
            },
        }
        children.append({
            "id": f"sp-{index}",
            "phase_id": phase_id,
            "agent_id": agent_id,
            "status": "completed",
            "progress": 100,
        })
    ctx = SimpleNamespace(
        project_id=project_id,
        agents=agents,
        subprojects=children,
    )
    coordinator_payload = {
        "project_id": project_id,
        "phase_id": phase_id,
        "execution_generation": generation,
        "contract_digest": contract_digest,
        "requirements_revision": 2,
        "artifact_baseline_digest": baseline_digest,
        "dispatch_attempt_digest": attempt_digest,
    }
    qa_calls = []

    async def fake_start_auto_repair(*args, **kwargs):
        qa_calls.append((args, kwargs))

    monkeypatch.setitem(
        routes_execution._phase_managers,
        project_id,
        _PhaseManager([phase]),
    )
    monkeypatch.setattr(
        routes_execution._run_registry,
        "get",
        lambda run_id: {
            "run_id": run_id,
            "run_type": "phase.dispatch",
            "status": "succeeded",
            "payload": coordinator_payload,
        },
    )
    monkeypatch.setattr(
        routes_phases,
        "start_auto_repair",
        fake_start_auto_repair,
    )

    asyncio.run(routes_execution._start_phase_quality_cycle_if_ready(ctx))
    assert qa_calls == []

    backend_receipt = agents["backend-agent"][
        "task_execution_receipts"
    ]["task-1"]
    backend_receipt.update({
        "completion_run_id": "current-task-1",
        "dispatch_attempt_digest": attempt_digest,
        "repair_coordinator_run_id": coordinator_run_id,
    })
    asyncio.run(routes_execution._start_phase_quality_cycle_if_ready(ctx))
    assert qa_calls == []

    frontend_receipt = agents["frontend-agent"][
        "task_execution_receipts"
    ]["task-2"]
    frontend_receipt.update({
        "completion_run_id": "current-task-2",
        "dispatch_attempt_digest": attempt_digest,
        "repair_coordinator_run_id": coordinator_run_id,
    })
    asyncio.run(routes_execution._start_phase_quality_cycle_if_ready(ctx))

    assert len(qa_calls) == 1
    assert all(agent["status"] == "completed" for agent in agents.values())
    assert all(child["status"] == "completed" for child in children)


def test_execute_all_only_schedules_current_queued_or_idle_contracts(monkeypatch):
    # Durable run idempotency is persisted in the local test database.  Use a
    # unique project id so rerunning this test cannot reuse a previous run.
    from uuid import uuid4

    project_id = f"execute-all-current-{uuid4().hex}"
    current_phase = {"phase_id": "phase-1", "status": "active"}
    future_phase = {"phase_id": "phase-2", "status": "pending"}
    pm = _PhaseManager([current_phase, future_phase])
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=_test_workspace(project_id),
        agents={
            "queued": {
                "id": "queued", "phase_id": "phase-1", "status": "queued",
                "subproject_id": "sp-q",
                "execution_contract": {
                    "subproject_id": "sp-q", "subproject_name": "queued",
                    "description": "queued contract", "tech_stack": ["Python"],
                    "project_context": "queued context",
                },
            },
            "idle": {
                "id": "idle", "phase_id": "phase-1", "status": "idle",
                "subproject_id": "sp-i",
                "execution_contract": {
                    "subproject_id": "sp-i", "subproject_name": "idle",
                    "description": "idle contract", "tech_stack": ["React"],
                    "project_context": "idle context",
                },
            },
            "failed": {
                "id": "failed", "phase_id": "phase-1", "status": "failed",
                "subproject_id": "sp-f",
            },
            "completed": {
                "id": "completed", "phase_id": "phase-1", "status": "completed",
                "subproject_id": "sp-c",
            },
            "future": {
                "id": "future", "phase_id": "phase-2", "status": "queued",
                "subproject_id": "sp-2",
            },
        },
        subprojects=[
            {"id": "sp-q", "name": "stale q", "description": "stale q"},
            {"id": "sp-i", "name": "stale i", "description": "stale i"},
            {"id": "sp-f", "name": "failed", "description": "failed"},
            {"id": "sp-c", "name": "completed", "description": "completed"},
            {"id": "sp-2", "name": "future", "description": "future"},
        ],
        pm=SimpleNamespace(context_summary="fallback"),
        description="project",
    )
    captured = []

    def capture(coro, name=""):
        captured.append(dict(coro.cr_frame.f_locals))
        coro.close()

    monkeypatch.setitem(routes_execution.projects, project_id, ctx)
    monkeypatch.setitem(routes_execution._phase_managers, project_id, pm)
    monkeypatch.setattr(routes_execution, "_safe_create_task", capture)

    result = asyncio.run(routes_execution.execute_all_agents(project_id))

    assert {item["agent_id"] for item in result["started"]} == {"queued", "idle"}
    assert {item["agent_id"] for item in captured} == {"queued", "idle"}
    assert {item["description"] for item in captured} == {
        "queued contract", "idle contract",
    }


def _execution_context(project_id, tmp_path, status, lock_id=""):
    agent_id = f"{project_id}-agent"
    subproject_id = f"{project_id}-subproject"
    agent = {
        "id": agent_id,
        "phase_id": "phase-1",
        "subproject_id": subproject_id,
        "status": status,
        "output_files": [],
        "required_rebuild_files": [],
        "required_delivery_files": [],
    }
    if lock_id:
        agent["lock_id"] = lock_id
    return SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        status="executing",
        agents={agent_id: agent},
        subprojects=[{
            "id": subproject_id,
            "phase_id": "phase-1",
            "agent_id": agent_id,
            "name": "task",
            "status": "pending",
            "progress": 0,
        }],
        _derive_project_status=lambda _rows: "running",
    ), agent_id, subproject_id


def test_agent_execution_renews_lease_and_stops_heartbeat(
    monkeypatch, tmp_path
):
    project_id = "heartbeat-renewal"
    ctx, agent_id, subproject_id = _execution_context(
        project_id, tmp_path, "queued", "lock-1",
    )
    renewals = []
    releases = []

    class SuccessfulExecution:
        def execute_task(self, **_kwargs):
            import time
            time.sleep(0.04)
            return {
                "success": True,
                "status": "completed",
                "output_files": [],
                "logs": [],
            }

    def renew(lock_id):
        renewals.append(lock_id)
        return {"success": True, "lock_id": lock_id, "leased_until": 9999999999}

    async def no_persist():
        return None

    async def no_quality_cycle(_ctx):
        return None

    monkeypatch.setitem(routes_execution.projects, project_id, ctx)
    monkeypatch.setattr(
        routes_execution,
        "_make_exec_agent",
        lambda *_args, **_kwargs: SuccessfulExecution(),
    )
    monkeypatch.setattr(routes_execution.expert_lock, "renew_lock", renew)
    monkeypatch.setattr(
        routes_execution.expert_lock,
        "release_lock",
        lambda lock_id: releases.append(lock_id) or {"success": True},
    )
    monkeypatch.setattr(routes_execution, "_AGENT_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(routes_execution, "_persist_all_async", no_persist)
    monkeypatch.setattr(routes_execution, "_persist_execution_state", lambda: None)
    monkeypatch.setattr(
        routes_execution,
        "_start_phase_quality_cycle_if_ready",
        no_quality_cycle,
    )

    async def run():
        result = await routes_execution._run_agent_task_unlocked(
            project_id=project_id,
            agent_id=agent_id,
            subproject_id=subproject_id,
            subproject_name="task",
            description="implement task",
            tech_stack=[],
            project_context="context",
        )
        renewal_count = len(renewals)
        await asyncio.sleep(0.02)
        return result, renewal_count

    result, renewal_count = asyncio.run(run())

    assert result["success"] is True
    assert len(renewals) >= 2
    assert set(renewals) == {"lock-1"}
    assert len(renewals) == renewal_count
    assert ctx.agents[agent_id]["heartbeat_at"] > 0
    assert ctx.agents[agent_id]["locked_until"] is None
    assert releases == ["lock-1"]


def test_truncated_repair_delivery_is_retried_before_marking_agent_failed(
    monkeypatch, tmp_path
):
    project_id = "repair-delivery-retry"
    ctx, agent_id, subproject_id = _execution_context(
        project_id, tmp_path, "fix_required",
    )
    calls = []

    class SequencedRepair:
        def execute_task(self, **kwargs):
            calls.append(kwargs["description"])
            if len(calls) == 1:
                return {
                    "success": False,
                    "status": "failed",
                    "error": "repair response was truncated before complete file content",
                    "output_files": [],
                    "logs": ["truncated"],
                }
            return {
                "success": True,
                "status": "completed",
                "output_files": [],
                "logs": ["complete repair"],
            }

    async def no_persist():
        return None

    monkeypatch.setitem(routes_execution.projects, project_id, ctx)
    monkeypatch.setattr(
        routes_execution,
        "_make_exec_agent",
        lambda *_args, **_kwargs: SequencedRepair(),
    )
    monkeypatch.setattr(routes_execution, "_persist_all_async", no_persist)
    monkeypatch.setattr(routes_execution, "_persist_execution_state", lambda: None)

    result = asyncio.run(routes_execution._run_agent_task_unlocked(
        project_id=project_id,
        agent_id=agent_id,
        subproject_id=subproject_id,
        subproject_name="task",
        description="【质检修复任务】repair backend/src/server.js",
        tech_stack=[],
        project_context="context",
        defer_fix_qc=True,
    ))

    assert result["success"] is True
    assert len(calls) == 2
    assert "REPAIR DELIVERY RECOVERY" in calls[1]
    assert any("Automatic orchestration retry" in line for line in result["logs"])
    assert ctx.agents[agent_id]["status"] == "completed"


def test_fix_task_defers_raw_qc_to_authoritative_supervisor(
    monkeypatch, tmp_path
):
    project_id = "fix-qc-error"
    ctx, agent_id, subproject_id = _execution_context(
        project_id, tmp_path, "fix_required",
    )

    class SuccessfulRepair:
        def execute_task(self, **_kwargs):
            return {
                "success": True,
                "status": "completed",
                "output_files": [],
                "logs": [],
            }

    async def no_persist():
        return None

    async def no_quality_cycle(_ctx):
        return None

    raw_qc_calls = []

    def qc_failure(*_args, **_kwargs):
        raw_qc_calls.append(True)
        raise RuntimeError("quality service unavailable")

    monkeypatch.setitem(routes_execution.projects, project_id, ctx)
    monkeypatch.setattr(
        routes_execution,
        "_make_exec_agent",
        lambda *_args, **_kwargs: SuccessfulRepair(),
    )
    monkeypatch.setattr(routes_execution, "_persist_all_async", no_persist)
    monkeypatch.setattr(routes_execution, "_persist_execution_state", lambda: None)
    monkeypatch.setattr(
        routes_execution,
        "_start_phase_quality_cycle_if_ready",
        no_quality_cycle,
    )
    monkeypatch.setattr(
        "api.routes_supervisor._run_qc_for_subproject",
        qc_failure,
    )

    result = asyncio.run(routes_execution._run_agent_task_unlocked(
        project_id=project_id,
        agent_id=agent_id,
        subproject_id=subproject_id,
        subproject_name="task",
        description="【质检修复任务】repair the delivery",
        tech_stack=[],
        project_context="context",
    ))

    assert result["success"] is True
    assert result["status"] == "completed"
    assert raw_qc_calls == []
    assert routes_execution.execution_status[agent_id]["status"] == "completed"
    assert routes_execution.execution_status[agent_id]["progress"] == 100
    assert routes_execution.execution_status[agent_id]["output_files"] == []
    assert routes_execution.execution_status[agent_id]["qc_after_fix"] == {
        "status": "pending_authoritative_supervisor",
        "fix_attempt": 1,
    }
    assert ctx.agents[agent_id]["status"] == "completed"
    assert ctx.subprojects[0]["status"] == "completed"
    assert "error" not in ctx.subprojects[0]


def test_failed_execution_rollback_preserves_sibling_task_files(
    monkeypatch, tmp_path
):
    project_id = "scoped-transaction-rollback"
    owned = tmp_path / "backend" / "owned"
    sibling = tmp_path / "frontend"
    owned.mkdir(parents=True)
    sibling.mkdir()
    (owned / "app.py").write_text("owned-before", encoding="utf-8")
    (sibling / "app.ts").write_text("sibling-before", encoding="utf-8")
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        agents={"agent-1": {"id": "agent-1"}},
        subprojects=[{"id": "task-1", "status": "queued"}],
    )
    manager = SimpleNamespace(file_registry={
        "backend/owned/app.py": {"agent_id": "agent-1"},
        "frontend/app.ts": {"agent_id": "agent-2"},
    })
    monkeypatch.setitem(routes_execution._phase_managers, project_id, manager)

    snapshot = routes_execution._capture_execution_transaction(
        ctx,
        "agent-1",
        "task-1",
        {"allowed_prefixes": ["Backend/Owned/"]},
    )
    assert snapshot["scopes"] == ["backend/owned/"]
    (owned / "app.py").write_text("owned-broken", encoding="utf-8")
    (owned / "new.py").write_text("owned-new", encoding="utf-8")
    (sibling / "app.ts").write_text("sibling-success", encoding="utf-8")
    (sibling / "new.ts").write_text("sibling-new", encoding="utf-8")
    manager.file_registry["backend/owned/new.py"] = {"agent_id": "agent-1"}
    manager.file_registry["frontend/app.ts"] = {"agent_id": "agent-2-new"}
    manager.file_registry["frontend/new.ts"] = {"agent_id": "agent-2"}

    routes_execution._restore_execution_transaction(
        ctx, "agent-1", "task-1", snapshot,
    )

    assert (owned / "app.py").read_text(encoding="utf-8") == "owned-before"
    assert not (owned / "new.py").exists()
    assert (sibling / "app.ts").read_text(encoding="utf-8") == "sibling-success"
    assert (sibling / "new.ts").read_text(encoding="utf-8") == "sibling-new"
    assert manager.file_registry["backend/owned/app.py"]["agent_id"] == "agent-1"
    assert "backend/owned/new.py" not in manager.file_registry
    assert manager.file_registry["frontend/app.ts"]["agent_id"] == "agent-2-new"
    assert manager.file_registry["frontend/new.ts"]["agent_id"] == "agent-2"


def test_legacy_unscoped_execution_uses_exclusive_transaction_scope(tmp_path):
    ctx = SimpleNamespace(
        project_id="legacy-exclusive",
        workspace=tmp_path,
        agents={"agent-1": {"id": "agent-1"}},
    )
    assert routes_execution._execution_transaction_scopes(
        ctx, "agent-1", None,
    ) == ["*"]


def test_explicit_empty_execution_scope_fails_closed(tmp_path):
    ctx = SimpleNamespace(
        project_id="empty-scope",
        workspace=tmp_path,
        agents={"agent-1": {"id": "agent-1"}},
    )
    with pytest.raises(ValueError, match="explicitly empty"):
        routes_execution._execution_transaction_scopes(
            ctx,
            "agent-1",
            {"allowed_path_prefixes": [], "required_files": []},
        )


def test_case_alias_execution_scopes_fail_closed(tmp_path):
    ctx = SimpleNamespace(
        project_id="case-alias-scope",
        workspace=tmp_path,
        agents={"agent-1": {"id": "agent-1"}},
    )
    with pytest.raises(ValueError, match="case or alias collision"):
        routes_execution._execution_transaction_scopes(
            ctx,
            "agent-1",
            {
                "allowed_path_prefixes": [
                    "Backend/Src/",
                    "backend/src/",
                ],
            },
        )


def test_transaction_scope_prefix_has_a_directory_boundary():
    scopes = ["backend/api/"]
    assert routes_execution._transaction_scope_covers(
        scopes, "backend/api/routes.py",
    )
    assert not routes_execution._transaction_scope_covers(
        scopes, "backend/api_evil/routes.py",
    )


def test_old_execution_lock_cannot_restore_over_successor(tmp_path):
    target = tmp_path / "backend" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("old-baseline", encoding="utf-8")
    ctx = SimpleNamespace(
        project_id="successor-fence",
        workspace=tmp_path,
        agents={"agent-1": {"id": "agent-1", "lock_id": "lock-old"}},
        subprojects=[{"id": "task-1"}],
    )
    snapshot = routes_execution._capture_execution_transaction(
        ctx,
        "agent-1",
        "task-1",
        {"allowed_prefixes": ["backend/"]},
    )
    target.write_text("successor-content", encoding="utf-8")

    class SuccessorGuard:
        lock_id = "lock-new"

        def __call__(self):
            raise AssertionError("mismatched generation must fail before guard call")

    with pytest.raises(ValueError, match="active lock"):
        routes_execution._restore_execution_transaction(
            ctx, "agent-1", "task-1", snapshot, SuccessorGuard(),
        )
    assert target.read_text(encoding="utf-8") == "successor-content"


def test_revoked_execution_guard_cannot_restore_over_successor(tmp_path):
    target = tmp_path / "backend" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("old-baseline", encoding="utf-8")
    ctx = SimpleNamespace(
        project_id="revoked-restore-fence",
        workspace=tmp_path,
        agents={"agent-1": {"id": "agent-1", "lock_id": "lock-old"}},
        subprojects=[{"id": "task-1"}],
    )
    snapshot = routes_execution._capture_execution_transaction(
        ctx,
        "agent-1",
        "task-1",
        {"required_files": ["backend/app.py"]},
    )
    target.write_text("successor-content", encoding="utf-8")

    class RevokedGuard:
        lock_id = "lock-old"

        def __call__(self):
            # A non-atomic check could still pass immediately before revocation.
            return None

        @contextmanager
        def write_guard(self):
            raise RevokedExecutionGuard("old generation revoked")
            yield

    with pytest.raises(RevokedExecutionGuard, match="revoked"):
        routes_execution._restore_execution_transaction(
            ctx, "agent-1", "task-1", snapshot, RevokedGuard(),
        )
    assert target.read_text(encoding="utf-8") == "successor-content"


def test_superseded_child_with_live_expert_lock_restores_its_snapshot(
    monkeypatch, tmp_path,
) -> None:
    (
        project_id,
        _phase_id,
        phase_manager,
        phase,
        coordinator_run,
        child_payload,
    ) = _current_locked_child_attempt_state()
    target = tmp_path / "backend" / "app.py"
    generated = tmp_path / "backend" / "generated.py"
    target.parent.mkdir(parents=True)
    target.write_text("baseline", encoding="utf-8")
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        agents={"agent-1": {
            "id": "agent-1",
            "phase_id": "phase-1",
            "lock_id": "child-file-lock",
        }},
        subprojects=[{"id": "task-1", "status": "working"}],
    )
    snapshot = routes_execution._capture_execution_transaction(
        ctx,
        "agent-1",
        "task-1",
        {"required_files": ["backend/app.py", "backend/generated.py"]},
    )
    monkeypatch.setitem(
        routes_execution._phase_managers, project_id, phase_manager,
    )
    monkeypatch.setattr(
        routes_execution._run_registry,
        "get",
        lambda _run_id: coordinator_run,
    )
    monkeypatch.setattr(
        "core.project_write_fence.expert_lock.get_active_locks",
        lambda **_kwargs: [{"lock_id": "child-file-lock"}],
    )
    guard = ProjectExecutionGuard(
        project_id,
        tmp_path,
        generation="child-run:1",
        lock_id="child-file-lock",
        authorization_check=lambda: (
            routes_execution._assert_current_phase_attempt(child_payload)
        ),
    )

    # The old child wrote before learning that the phase pointer moved.
    target.write_text("stale-child-content", encoding="utf-8")
    generated.write_text("stale-generated-content", encoding="utf-8")
    phase["execution_coordinator"]["durable_run_id"] = "successor-coordinator"

    # Supersession must fence ordinary writes immediately.
    with pytest.raises(LeaseConflict, match="stale or superseded"):
        with guard.write_guard():
            pytest.fail("superseded child entered an ordinary write section")

    # The exact still-live ExpertLock may only compensate its own snapshot.
    routes_execution._restore_execution_transaction(
        ctx, "agent-1", "task-1", snapshot, guard,
    )

    assert target.read_text(encoding="utf-8") == "baseline"
    assert not generated.exists()


@pytest.mark.parametrize("guard_failure", ["expert_lock_lost", "guard_revoked"])
def test_compensation_guard_never_overwrites_successor_without_live_authority(
    monkeypatch, tmp_path, guard_failure,
) -> None:
    project_id = f"compensation-{guard_failure}"
    target = tmp_path / "backend" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("baseline", encoding="utf-8")
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        agents={"agent-1": {
            "id": "agent-1",
            "lock_id": "old-child-lock",
        }},
        subprojects=[{"id": "task-1", "status": "working"}],
    )
    snapshot = routes_execution._capture_execution_transaction(
        ctx,
        "agent-1",
        "task-1",
        {"required_files": ["backend/app.py"]},
    )
    target.write_text("successor-content", encoding="utf-8")
    active_locks = [{"lock_id": "old-child-lock"}]
    monkeypatch.setattr(
        "core.project_write_fence.expert_lock.get_active_locks",
        lambda **_kwargs: list(active_locks),
    )
    guard = ProjectExecutionGuard(
        project_id,
        tmp_path,
        generation="old-child:1",
        lock_id="old-child-lock",
        authorization_check=lambda: None,
    )
    if guard_failure == "expert_lock_lost":
        active_locks.clear()
    else:
        guard.revoke()

    with pytest.raises(RevokedExecutionGuard):
        routes_execution._restore_execution_transaction(
            ctx, "agent-1", "task-1", snapshot, guard,
        )

    assert target.read_text(encoding="utf-8") == "successor-content"


def test_immutable_fullstack_scope_does_not_gain_legacy_paths(tmp_path):
    (tmp_path / "src").mkdir()
    ctx = SimpleNamespace(
        project_id="immutable-fullstack",
        workspace=tmp_path,
        agents={"agent-1": {
            "id": "agent-1",
            "role": "Full Stack Engineer",
            "allowed_path_prefixes": ["src/"],
        }},
    )
    agent = routes_execution._make_exec_agent(
        ctx,
        "agent-1",
        attempt_scope={
            "allowed_path_prefixes": ["backend/"],
            "required_files": ["backend/app.py"],
        },
    )
    assert set(agent.allowed_path_prefixes) == {
        "backend/",
        "backend/app.py",
    }
    agent._active_repair_targets = {"src/App.tsx"}
    assert agent._path_is_allowed("src/App.tsx") is False
    assert agent._path_is_allowed("output/task.log") is False
    with pytest.raises(ValueError):
        agent._write_file("output/task-1_execution.log", "model-controlled")

    log_path = agent._write_execution_log("task-1", "runner-controlled")
    assert log_path == "output/task-1_execution.log"
    assert (tmp_path / log_path).read_text(encoding="utf-8") == "runner-controlled"
    assert agent.output_files == []
    unsafe_id_log = agent._write_execution_log("../../outside", "still-internal")
    assert Path(unsafe_id_log).parts[0] == "output"
    assert ".." not in Path(unsafe_id_log).parts
    assert (tmp_path / unsafe_id_log).is_file()


def test_root_runtime_scope_is_rolled_back(tmp_path):
    target = tmp_path / "dist" / "bundle.js"
    target.parent.mkdir()
    target.write_text("baseline", encoding="utf-8")
    ctx = SimpleNamespace(
        project_id="runtime-scope",
        workspace=tmp_path,
        agents={"agent-1": {"id": "agent-1"}},
        subprojects=[{"id": "task-1"}],
    )
    snapshot = routes_execution._capture_execution_transaction(
        ctx,
        "agent-1",
        "task-1",
        {"allowed_prefixes": ["dist/"]},
    )
    target.write_text("broken", encoding="utf-8")
    generated = tmp_path / "dist" / "new.js"
    generated.write_text("partial", encoding="utf-8")

    routes_execution._restore_execution_transaction(
        ctx, "agent-1", "task-1", snapshot,
    )

    assert target.read_text(encoding="utf-8") == "baseline"
    assert not generated.exists()


def test_transaction_restore_never_follows_a_replacement_link(tmp_path):
    owned = tmp_path / "backend" / "app.py"
    sibling = tmp_path / "frontend" / "app.py"
    owned.parent.mkdir()
    sibling.parent.mkdir()
    owned.write_text("owned-baseline", encoding="utf-8")
    sibling.write_text("sibling-success", encoding="utf-8")
    ctx = SimpleNamespace(
        project_id="link-rollback",
        workspace=tmp_path,
        agents={"agent-1": {"id": "agent-1"}},
        subprojects=[{"id": "task-1"}],
    )
    snapshot = routes_execution._capture_execution_transaction(
        ctx,
        "agent-1",
        "task-1",
        {"required_files": ["backend/app.py"]},
    )
    owned.unlink()
    try:
        os.symlink(sibling, owned)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="crosses a link"):
        routes_execution._restore_execution_transaction(
            ctx, "agent-1", "task-1", snapshot,
        )
    agent = routes_execution.ExecutionAgent(
        agent_id="agent-1",
        role="Backend Engineer",
        workspace=tmp_path,
        hermes_client=SimpleNamespace(),
        allowed_path_prefixes=["backend/app.py"],
        immutable_path_scope=True,
    )
    with pytest.raises(ValueError, match="符号链接或联接点"):
        agent._write_file("backend/app.py", "must-not-overwrite")
    assert sibling.read_text(encoding="utf-8") == "sibling-success"
