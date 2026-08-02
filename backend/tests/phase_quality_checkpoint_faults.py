"""Fault matrix for Pre-QA and Supervisor state checkpoints.

The state-machine methods and SQLite writer are production code.  The oracle
records whether a failed durable checkpoint leaves the live machine ahead of
SQLite, then restores from SQLite and proves the same operation is retryable.
"""

from __future__ import annotations

import copy
import sqlite3

import pytest

from api import routes_phases
from core import database
from core.supervisor_quality_state import SupervisorQualityMachine


def _scope():
    return {
        "phase_id": "phase-1",
        "scope_digest": "scope-1",
        "artifact_digest": "artifact-1",
        "dependencies": {},
        "required_evidence": ["qa"],
    }


def _machine_verifying():
    machine = SupervisorQualityMachine()
    machine.start_run(
        _scope(), "start-1", True, run_id="run-1",
        agents=[{"agent_id": "agent-1", "status": "succeeded"}],
        required_evidence_kinds=["qa"],
        required_pre_qa_evidence_kinds=["qa"],
    )
    machine.engineer_completed("artifact:artifact-1")
    machine.start_verification()
    return machine


def _machine_waiting():
    machine = SupervisorQualityMachine()
    machine.start_run(
        _scope(), "start-1", True, run_id="run-1",
        agents=[{"agent_id": "agent-1", "status": "pending"}],
        required_evidence_kinds=["qa"],
    )
    return machine


def _machine_qa_running():
    machine = _machine_verifying()
    machine.record_evidence(
        kind="qa", command="pytest", exit_code=0, passed=True,
        log="passed", step_id="qa-1", metadata=_scope(),
    )
    machine.start_qa_round(_scope(), [], qa_round_id="round-1")
    return machine


def _preqa_evidence(machine):
    routes_phases._record_pre_qa_machine_evidence(machine, {
        "evidence": [{
            "kind": "test", "gate_id": "test-root", "command": "pytest",
            "exit_code": 0, "passed": True, "applicable": True,
            "executed": True, "log_digest": "sha256:preqa",
        }],
    })


def _supervisor_evidence(machine):
    machine.record_evidence(
        kind="qa", command="supervisor-check", exit_code=0, passed=True,
        log="passed", step_id="supervisor-qa", metadata=_scope(),
    )


def _attempt(machine):
    machine.record_agent("agent-1", "running", task_id="attempt-1")


def _progress(machine):
    machine.record_agent("agent-1", "succeeded", task_id="attempt-1")
    machine.engineer_completed("artifact:artifact-1")
    machine.start_verification()


def _preqa_status(machine):
    machine.fail_pre_qa(
        [{"message": "missing artifact", "severity": "error"}],
        agent_ids=["agent-1"],
    )


def _budget(machine):
    machine.finish_verification([])


CHECKPOINTS = {
    "preqa_status": (_machine_verifying, _preqa_status),
    "preqa_evidence_recorder": (_machine_verifying, _preqa_evidence),
    "supervisor_qa_evidence": (_machine_verifying, _supervisor_evidence),
    "supervisor_attempt": (_machine_waiting, _attempt),
    "supervisor_progress": (_machine_waiting, _progress),
    "supervisor_budget": (_machine_qa_running, _budget),
}


def _fresh_db(monkeypatch, tmp_path, name):
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setattr(database, "_sqlite_path", str(tmp_path / name))
    database.init_db()


def _install_abort_trigger(path, timing):
    connection = sqlite3.connect(path)
    trigger_timing = "BEFORE" if timing == "before" else "AFTER"
    for operation in ("INSERT", "UPDATE"):
        connection.execute(
            f"CREATE TRIGGER checkpoint_abort_{operation.lower()} "
            f"{trigger_timing} {operation} ON kv_store "
            "BEGIN SELECT RAISE(ABORT, 'checkpoint injected failure'); END"
        )
    connection.commit()
    connection.close()


def _drop_abort_trigger(path):
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER checkpoint_abort_insert")
    connection.execute("DROP TRIGGER checkpoint_abort_update")
    connection.commit()
    connection.close()


@pytest.mark.parametrize("checkpoint", sorted(CHECKPOINTS))
@pytest.mark.parametrize("timing", ["before", "during"])
def test_checkpoint_failure_snapshot_and_restart_retry(
    checkpoint, timing, monkeypatch, tmp_path
):
    """12 production checkpoints: rollback DB, expose memory drift, retry."""
    path = tmp_path / f"{checkpoint}-{timing}.db"
    _fresh_db(monkeypatch, tmp_path, path.name)
    factory, operation = CHECKPOINTS[checkpoint]
    machine = factory()
    durable_before = machine.to_dict()
    database.kv_many_set({"supervisor_quality_run": durable_before})
    machine.mark_persisted()

    operation(machine)
    live_after_operation = machine.to_dict()
    assert live_after_operation != durable_before
    _install_abort_trigger(path, timing)
    with pytest.raises(Exception, match="checkpoint injected failure"):
        database.kv_many_set({"supervisor_quality_run": live_after_operation})
    _drop_abort_trigger(path)

    # SQLite remains atomic and the live machine must compensate to the same
    # durable snapshot without requiring a process restart.
    assert database.kv_get("supervisor_quality_run") == durable_before
    machine.rollback_unpersisted()
    assert machine.to_dict() == durable_before
    assert machine.to_dict() == database.kv_get("supervisor_quality_run")

    operation(machine)
    database.kv_many_set({"supervisor_quality_run": machine.to_dict()})
    machine.mark_persisted()
    assert database.kv_get("supervisor_quality_run") == machine.to_dict()
