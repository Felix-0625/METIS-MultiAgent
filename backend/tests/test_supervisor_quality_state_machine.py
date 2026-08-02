"""Contract tests for the durable Supervisor quality state machine.

These tests deliberately exercise the pure state-machine adapter instead of
FastAPI routes.  Route tests may then stay focused on request/response wiring,
while the convergence and recovery invariants remain deterministic.
"""

from __future__ import annotations

import copy

import pytest

from core.supervisor_quality_state import (
    IllegalQualityTransition,
    SupervisorQualityMachine,
)


SCOPE = {
    "project_id": "project-1",
    "phase_id": "phase-1",
    "phase_generation_id": "generation-1",
    "scope_digest": "scope-v1",
    "artifact_digest": "artifact-v1",
    "tests": ["pytest -q"],
    "required_evidence": ["tests", "build", "service", "api", "docker", "deployment"],
}


def _issue(name: str, *, severity: str = "error") -> dict:
    return {
        "fingerprint": f"functionality|src/{name}.py|{name}-broken",
        "message": f"{name} is broken",
        "file_path": f"src/{name}.py",
        "severity": severity,
        "status": "open",
    }


def _new_machine(*, run_id: str = "run-1") -> SupervisorQualityMachine:
    machine = SupervisorQualityMachine()
    machine.start_run(
        scope=copy.deepcopy(SCOPE),
        idempotency_key=f"start-{run_id}",
        dependencies_ready=True,
        run_id=run_id,
        required_evidence_kinds=SCOPE["required_evidence"],
    )
    return machine


def _finish_engineering(
    machine: SupervisorQualityMachine, *, commit: str = "artifact:artifact-v1"
) -> None:
    machine.record_agent("engineer-1", "succeeded", critical=True)
    machine.engineer_completed(commit=commit)


def _record_complete_evidence(machine: SupervisorQualityMachine, *, prefix: str = "r1") -> None:
    for kind in SCOPE["required_evidence"]:
        machine.record_evidence(
            step_id=f"{prefix}-{kind}",
            kind=kind,
            command=f"verify-{kind}",
            exit_code=0,
            passed=True,
            log=f"{kind} verification passed",
        )


def _verify_and_start_round(
    machine: SupervisorQualityMachine,
    round_number: int,
    issues: list[dict],
) -> None:
    machine.start_verification()
    _record_complete_evidence(machine, prefix=f"r{round_number}")
    _start_round(machine, round_number, issues)


def _start_round(
    machine: SupervisorQualityMachine,
    round_number: int,
    issues: list[dict],
) -> None:
    machine.start_qa_round(
        scope_snapshot=copy.deepcopy(SCOPE),
        issue_snapshot=copy.deepcopy(issues),
        qa_round_id=f"qa-{round_number}",
    )


def test_happy_path_has_one_strict_completion_gate() -> None:
    machine = _new_machine()
    assert machine.to_dict()["state"] == "waiting_engineer"

    _finish_engineering(machine)
    _verify_and_start_round(machine, 1, [])
    assert machine.to_dict()["state"] == "qa_running"

    machine.finish_verification(issues=[])
    machine.complete()

    state = machine.to_dict()
    assert state["state"] == "completed"
    assert state["business_rounds_used"] == 1
    assert state["completion_gate"]["passed"] is True
    assert state["rounds"][0]["counts"] == {
        "total": 0,
        "blocking": 0,
        "fixed": 0,
        "remaining": 0,
        "new": 0,
        "repeated": 0,
    }
    committed = state["rounds"][0]
    assert committed["run_id"] == "run-1"
    assert committed["qa_round_id"] == "qa-1"
    assert committed["commit"] == "artifact:artifact-v1"
    assert committed["scope_snapshot"]["scope_digest"] == "scope-v1"
    assert len(committed["commands"]) == len(SCOPE["required_evidence"])
    assert all(isinstance(item["exit_code"], int) for item in committed["commands"])
    assert all(item["command"] and item["log"] for item in committed["evidence"])


@pytest.mark.parametrize(
    "operation",
    [
        lambda machine: machine.start_qa_round(SCOPE, [], "qa-illegal"),
        lambda machine: machine.mark_repair_required(),
        lambda machine: machine.start_verification(),
        lambda machine: machine.complete(),
    ],
)
def test_illegal_transitions_from_idle_are_rejected(operation) -> None:
    machine = SupervisorQualityMachine()
    with pytest.raises(IllegalQualityTransition):
        operation(machine)
    assert machine.to_dict()["state"] == "idle"


def test_dependencies_must_be_satisfied_before_start() -> None:
    machine = SupervisorQualityMachine()
    with pytest.raises(IllegalQualityTransition, match="depend"):
        machine.start_run(
            scope=copy.deepcopy(SCOPE),
            idempotency_key="missing-dependency",
            dependencies_ready=False,
            run_id="run-dependency",
        )
    assert machine.to_dict()["state"] == "idle"
    assert machine.to_dict()["rounds"] == []


def test_qa_scope_must_match_the_scope_locked_at_run_start() -> None:
    machine = _new_machine(run_id="run-scope-lock")
    _finish_engineering(machine)
    machine.start_verification()
    _record_complete_evidence(machine)
    narrowed_scope = {
        **copy.deepcopy(SCOPE),
        "scope_digest": "scope-narrowed-after-failures",
        "tests": [],
    }

    with pytest.raises(IllegalQualityTransition, match="scope|digest"):
        machine.start_qa_round(
            scope_snapshot=narrowed_scope,
            issue_snapshot=[],
            qa_round_id="qa-scope-bypass",
        )


@pytest.mark.parametrize("pre_qa_metadata", [
    {},
    {"scope_digest": "scope-v1", "artifact_digest": "artifact-old"},
    {"scope_digest": "scope-old", "artifact_digest": "artifact-v1"},
    {
        "project_id": "project-1",
        "phase_id": "phase-1",
        "phase_generation_id": "generation-old",
        "scope_digest": "scope-v1",
        "artifact_digest": "artifact-v1",
    },
])
def test_pre_qa_evidence_must_bind_locked_scope(pre_qa_metadata: dict) -> None:
    machine = SupervisorQualityMachine()
    machine.start_run(
        scope=copy.deepcopy(SCOPE),
        idempotency_key="scoped-pre-qa",
        dependencies_ready=True,
        run_id="run-scoped-pre-qa",
        required_evidence_kinds=["qa"],
        required_pre_qa_evidence_kinds=["scope", "pre_qa"],
    )
    _finish_engineering(machine)
    machine.start_verification()
    for kind, metadata in (("scope", copy.deepcopy(SCOPE)), ("pre_qa", pre_qa_metadata)):
        machine.record_evidence(
            step_id=f"pre-{kind}",
            kind=kind,
            command=f"verify-{kind}",
            exit_code=0,
            passed=True,
            log=f"{kind} passed",
            metadata=metadata,
        )

    with pytest.raises(IllegalQualityTransition, match="scoped verification evidence"):
        machine.start_qa_round(
            scope_snapshot=copy.deepcopy(SCOPE),
            issue_snapshot=[],
            qa_round_id="qa-forged-pre-qa",
        )


def test_scoped_pre_qa_evidence_allows_qa_round() -> None:
    machine = SupervisorQualityMachine()
    machine.start_run(
        scope=copy.deepcopy(SCOPE),
        idempotency_key="valid-scoped-pre-qa",
        dependencies_ready=True,
        run_id="run-valid-scoped-pre-qa",
        required_evidence_kinds=["qa"],
        required_pre_qa_evidence_kinds=["scope", "pre_qa"],
    )
    _finish_engineering(machine)
    machine.start_verification()
    for kind in ("scope", "pre_qa"):
        machine.record_evidence(
            step_id=f"valid-{kind}",
            kind=kind,
            command=f"verify-{kind}",
            exit_code=0,
            passed=True,
            log=f"{kind} passed",
            metadata=copy.deepcopy(SCOPE),
        )

    machine.start_qa_round(
        scope_snapshot=copy.deepcopy(SCOPE),
        issue_snapshot=[],
        qa_round_id="qa-valid-pre-qa",
    )

    assert machine.to_dict()["state"] == "qa_running"


@pytest.mark.parametrize("status", ["pending", "running", "failed", "timeout", "blocked", "cancelled"])
def test_qa_cannot_start_until_every_critical_engineer_succeeds(status: str) -> None:
    machine = _new_machine(run_id=f"run-{status}")
    machine.record_agent("engineer-1", status, critical=True)

    with pytest.raises(IllegalQualityTransition):
        machine.engineer_completed(commit="artifact:artifact-v1")
    with pytest.raises(IllegalQualityTransition):
        machine.start_verification()

    assert machine.to_dict()["state"] == "waiting_engineer"
    assert machine.to_dict()["business_rounds_used"] == 0


def test_agent_status_vocabulary_is_closed() -> None:
    machine = _new_machine()
    with pytest.raises(ValueError, match="status"):
        machine.record_agent("engineer-1", "completed", critical=True)


@pytest.mark.parametrize("failed_status", ["failed", "timeout", "blocked", "cancelled"])
def test_failed_agent_can_be_explicitly_retried_without_replacing_the_run(failed_status: str) -> None:
    machine = _new_machine(run_id=f"run-agent-retry-{failed_status}")
    machine.record_agent("engineer-1", failed_status, critical=True, task_id="attempt-1")
    with pytest.raises(IllegalQualityTransition):
        machine.engineer_completed(commit="artifact:artifact-v1")

    machine.record_agent("engineer-1", "pending", critical=True, task_id="attempt-2")
    machine.record_agent("engineer-1", "running", critical=True, task_id="attempt-2")
    machine.record_agent("engineer-1", "succeeded", critical=True, task_id="attempt-2")
    machine.engineer_completed(commit="artifact:artifact-v1")

    state = machine.to_dict()
    assert state["run_id"] == f"run-agent-retry-{failed_status}"
    assert state["agents"]["engineer-1"]["task_id"] == "attempt-2"
    assert state["agents"]["engineer-1"]["status"] == "succeeded"


def test_zero_critical_agents_cannot_vacuously_pass_engineering_gate() -> None:
    machine = _new_machine(run_id="run-no-critical-agent")

    with pytest.raises(IllegalQualityTransition, match="critical|Agent"):
        machine.engineer_completed(commit="artifact:artifact-v1")
    with pytest.raises(IllegalQualityTransition):
        machine.start_verification()


def test_cannot_bypass_engineering_verification_or_snapshot_commit_order() -> None:
    machine = _new_machine(run_id="run-order")
    machine.record_agent("engineer-1", "succeeded", critical=True)

    with pytest.raises(IllegalQualityTransition):
        _start_round(machine, 1, [])

    machine.engineer_completed(commit="artifact:artifact-v1")
    with pytest.raises(IllegalQualityTransition):
        _start_round(machine, 1, [])

    machine.start_verification()
    with pytest.raises(IllegalQualityTransition):
        machine.mark_repair_required(agent_ids=["engineer-1"])
    with pytest.raises(IllegalQualityTransition):
        machine.complete()

    _record_complete_evidence(machine)
    _start_round(machine, 1, [])
    with pytest.raises(IllegalQualityTransition):
        machine.complete()

    machine.finish_verification(issues=[])
    machine.complete()
    assert machine.to_dict()["state"] == "completed"


@pytest.mark.parametrize("failure_kind", ["infrastructure", "model"])
def test_external_failure_does_not_consume_round_and_resumes_same_ids(failure_kind: str) -> None:
    machine = _new_machine(run_id=f"run-{failure_kind}")
    _finish_engineering(machine)
    _verify_and_start_round(machine, 1, [])
    before = machine.to_dict()

    machine.fail(kind=failure_kind, reason=f"{failure_kind} unavailable")
    failed = machine.to_dict()
    assert failed["state"] == f"{failure_kind}_failed"
    assert failed["business_rounds_used"] == 0

    machine.resume()
    resumed = machine.to_dict()
    assert resumed["run_id"] == before["run_id"]
    assert resumed["active_qa_round_id"] == before["active_qa_round_id"] == "qa-1"
    assert resumed["state"] == "qa_running"
    assert resumed["business_rounds_used"] == 0


@pytest.mark.parametrize("failure_kind", ["infrastructure", "model"])
def test_pre_qa_failure_resumes_verifying_without_skipping_evidence(failure_kind: str) -> None:
    machine = _new_machine(run_id=f"run-pre-qa-{failure_kind}")
    _finish_engineering(machine)
    machine.start_verification()
    before = machine.to_dict()

    machine.fail(kind=failure_kind, reason="external dependency unavailable")
    assert machine.to_dict()["business_rounds_used"] == 0

    machine.resume()
    resumed = machine.to_dict()
    assert resumed["run_id"] == before["run_id"]
    assert resumed["state"] == "verifying"
    assert resumed["business_rounds_used"] == 0

    with pytest.raises(IllegalQualityTransition):
        _start_round(machine, 1, [])


def test_sixth_business_round_defers_repeated_blocker_to_manual() -> None:
    machine = _new_machine()
    blocker = _issue("auth")
    issue_id = None

    for round_number in range(1, 7):
        if round_number > 1:
            machine.mark_repair_required(agent_ids=["engineer-1"])
        _finish_engineering(machine, commit="artifact:artifact-v1")
        _verify_and_start_round(machine, round_number, [blocker])
        current = machine.to_dict()["rounds"][-1]
        if issue_id is None:
            issue_id = current["issues"][0]["issue_id"]
        else:
            assert current["issues"][0]["issue_id"] == issue_id

        machine.finish_verification(issues=[copy.deepcopy(blocker)])

        if round_number < 6:
            assert machine.to_dict()["state"] != "blocked"

    # 6 轮上限后：转人工 blocked 终态（routes 据此停止 auto repair + awaiting_decision），
    # 剩余 blocker 标记 deferred（人工重验时不再阻塞 completion gate）。
    state = machine.to_dict()
    assert state["state"] == "blocked"
    assert state["business_rounds_used"] == 6
    assert state["max_qa_rounds"] == 6
    assert state["next_action"]["type"] == "manual_intervention"
    assert state["manual_items"][0]["issue_id"] == issue_id
    assert state["manual_items"][0]["status"] == "deferred"

    with pytest.raises(IllegalQualityTransition):
        _verify_and_start_round(machine, 7, [blocker])

    with pytest.raises(IllegalQualityTransition):
        machine.start_run(
            scope=copy.deepcopy(SCOPE),
            idempotency_key="bypass-six-round-limit",
            dependencies_ready=True,
            run_id="replacement-run",
        )
    assert machine.to_dict()["business_rounds_used"] == 6
    assert machine.to_dict()["state"] == "blocked"


def test_new_blocker_enters_repair_instead_of_blocking() -> None:
    machine = _new_machine()
    original = _issue("auth")
    introduced = _issue("billing", severity="critical")

    _finish_engineering(machine, commit="artifact:artifact-v1")
    _verify_and_start_round(machine, 1, [original])
    machine.finish_verification(issues=[copy.deepcopy(original), copy.deepcopy(introduced)])

    state = machine.to_dict()
    # 新指纹不再立即 block，并入 repair 流程收敛
    assert state["state"] == "repair_required"
    counts = state["rounds"][-1]["counts"]
    assert counts["remaining"] == 2
    assert counts["new"] == 1
    assert counts["repeated"] == 1
    assert state["next_action"]["type"] == "dispatch_repair"


def test_start_and_round_creation_are_idempotent_but_conflicts_fail() -> None:
    machine = _new_machine()
    first_run = machine.to_dict()
    machine.start_run(
        scope=copy.deepcopy(SCOPE),
        idempotency_key="start-run-1",
        dependencies_ready=True,
        run_id="different-client-run-id",
    )
    assert machine.to_dict()["run_id"] == first_run["run_id"]

    _finish_engineering(machine)
    machine.start_verification()
    _record_complete_evidence(machine)
    _start_round(machine, 1, [])
    first_round = machine.to_dict()["rounds"]
    _start_round(machine, 1, [])
    assert machine.to_dict()["rounds"] == first_round
    assert len(machine.to_dict()["rounds"]) == 1

    with pytest.raises(IllegalQualityTransition):
        machine.start_qa_round(copy.deepcopy(SCOPE), [], "qa-conflicting-active")


def test_checkpoint_round_trip_resumes_without_replaying_verified_evidence() -> None:
    machine = _new_machine()
    _finish_engineering(machine)
    machine.start_verification()
    machine.record_evidence(
        step_id="r1-test",
        kind="test",
        command="pytest -q",
        exit_code=0,
        passed=True,
        log="42 passed",
    )
    checkpoint = machine.to_dict()

    restored = SupervisorQualityMachine.from_dict(copy.deepcopy(checkpoint))
    assert restored.to_dict() == checkpoint

    restored.record_evidence(
        step_id="r1-test",
        kind="test",
        command="pytest -q",
        exit_code=0,
        passed=True,
        log="42 passed",
    )
    assert len(restored.to_dict()["pending_evidence"]) == 1

    with pytest.raises(IllegalQualityTransition):
        restored.record_evidence(
            step_id="r1-test",
            kind="test",
            command="pytest -q tests/unit",
            exit_code=0,
            passed=True,
            log="different evidence must not overwrite an accepted step",
        )


def test_restore_rejects_an_impossible_completed_checkpoint() -> None:
    corrupt = {
        "schema_version": 1,
        "run_id": "run-corrupt",
        "state": "completed",
        "status": "completed",
        "active": False,
        "dependencies_ready": True,
        "rounds": [],
        "agents": {},
        "business_rounds_used": 0,
        "completion_gate": {"passed": True},
    }

    with pytest.raises(IllegalQualityTransition, match="completed|checkpoint|gate"):
        SupervisorQualityMachine.from_dict(corrupt)


def test_completed_run_cannot_be_replaced_by_a_new_idempotency_key() -> None:
    machine = _new_machine(run_id="run-complete-once")
    _finish_engineering(machine)
    _verify_and_start_round(machine, 1, [])
    machine.finish_verification(issues=[])
    machine.complete()

    with pytest.raises(IllegalQualityTransition):
        machine.start_run(
            scope=copy.deepcopy(SCOPE),
            idempotency_key="new-key-after-completion",
            dependencies_ready=True,
            run_id="duplicate-completed-run",
        )
    assert machine.to_dict()["run_id"] == "run-complete-once"
    assert machine.to_dict()["state"] == "completed"


@pytest.mark.parametrize("critical_status", ["failed", "timeout", "blocked", "cancelled"])
def test_critical_agent_failure_or_timeout_can_never_complete(critical_status: str) -> None:
    machine = _new_machine(run_id=f"run-gate-{critical_status}")
    _finish_engineering(machine)
    _verify_and_start_round(machine, 1, [])
    machine.finish_verification(issues=[])
    machine.record_agent("qa-critical", critical_status, critical=True)

    with pytest.raises(IllegalQualityTransition):
        machine.complete()
    assert machine.to_dict()["state"] != "completed"


def test_missing_or_failed_real_evidence_prevents_completion() -> None:
    machine = _new_machine()
    _finish_engineering(machine)
    machine.start_verification()
    machine.record_evidence(
        step_id="r1-test",
        kind="test",
        command="pytest -q",
        exit_code=1,
        passed=False,
        log="1 failed",
    )

    with pytest.raises(IllegalQualityTransition):
        _start_round(machine, 1, [])
    with pytest.raises(IllegalQualityTransition):
        machine.complete()
    assert machine.to_dict()["state"] != "completed"


def test_passed_flag_cannot_contradict_nonzero_exit_code() -> None:
    machine = _new_machine(run_id="run-contradictory-evidence")
    _finish_engineering(machine)
    machine.start_verification()

    with pytest.raises(IllegalQualityTransition, match="exit|passed"):
        machine.record_evidence(
            step_id="contradictory-test",
            kind="tests",
            command="pytest -q",
            exit_code=1,
            passed=True,
            log="1 failed",
        )


def test_explicit_manual_fix_resumes_blocked_run_without_resetting_budget() -> None:
    machine = _new_machine()
    _finish_engineering(machine)
    _verify_and_start_round(machine, 1, [_issue("manual")])
    machine.block("manual correction required", [_issue("manual")])
    machine.data["agents"]["manual-repair"] = {
        "agent_id": "manual-repair",
        "task_id": "repair-task",
        "critical": True,
        "status": "pending",
    }

    before = machine.to_dict()["business_rounds_used"]
    resumed = machine.resume_after_manual_fix()

    assert resumed["state"] == "verifying"
    assert resumed["business_rounds_used"] == before
    assert resumed["agents"]["manual-repair"]["status"] == "succeeded"
    assert resumed["checkpoint"]["name"] == "manual_fix_verification_started"


def test_blocked_manual_fix_can_cas_bind_a_new_artifact_generation() -> None:
    machine = _new_machine()
    _finish_engineering(machine)
    _verify_and_start_round(machine, 1, [_issue("manual")])
    machine.block("manual correction required", [_issue("manual")])
    next_scope = {
        **SCOPE,
        "scope_digest": "scope-v2",
        "artifact_digest": "artifact-v2",
    }

    bound = machine.bind_artifact_generation(
        artifact_digest="artifact-v2",
        scope_digest="scope-v2",
        repair_commit="artifact:artifact-v2",
        scope_snapshot=next_scope,
        transition_reason="manual_fix",
        expected_previous_artifact_digest="artifact-v1",
    )

    assert bound["state"] == "blocked"
    assert bound["scope"] == next_scope
    assert bound["verification_commit"] == "artifact:artifact-v2"
    assert bound["artifact_generation_history"][-1]["transition_reason"] == "manual_fix"
    assert machine.resume_after_manual_fix()["state"] == "verifying"


def test_manual_artifact_generation_bind_rejects_stale_previous_cas() -> None:
    machine = _new_machine()
    _finish_engineering(machine)
    _verify_and_start_round(machine, 1, [_issue("manual")])
    machine.block("manual correction required", [_issue("manual")])

    with pytest.raises(IllegalQualityTransition, match="previous-generation CAS"):
        machine.bind_artifact_generation(
            artifact_digest="artifact-v2",
            scope_digest="scope-v2",
            repair_commit="artifact:artifact-v2",
            transition_reason="manual_fix",
            expected_previous_artifact_digest="stale-artifact",
        )


def test_manual_fix_can_resume_after_six_round_limit() -> None:
    # 6 轮上限是自动收敛预算，不应阻塞人工修复后的复核。
    # routes 给 blocked 状态的选项就是 manual_fix；若 6 轮后拒绝 resume，
    # 人工修复后会死路（无法重验）。见 supervisor_quality_state.resume_after_manual_fix。
    machine = _new_machine()
    _finish_engineering(machine)
    _verify_and_start_round(machine, 1, [_issue("manual")])
    machine.block("manual correction required", [_issue("manual")])
    machine.data["business_rounds_used"] = 6

    result = machine.resume_after_manual_fix()
    assert result["status"] == "verifying"
    assert result["manual_items"] == []


def test_same_qa_round_id_rejects_conflicting_scope_or_issue_snapshot() -> None:
    machine = _new_machine()
    _finish_engineering(machine)
    _verify_and_start_round(machine, 1, [_issue("original")])

    with pytest.raises(IllegalQualityTransition, match="Conflicting replay"):
        machine.start_qa_round(
            scope_snapshot={**SCOPE, "artifact_digest": "artifact-other"},
            issue_snapshot=[_issue("original")],
            qa_round_id="qa-1",
        )
    with pytest.raises(IllegalQualityTransition, match="Conflicting replay"):
        machine.start_qa_round(
            scope_snapshot=SCOPE,
            issue_snapshot=[_issue("different")],
            qa_round_id="qa-1",
        )


def test_finished_qa_round_rejects_conflicting_verdict_replay() -> None:
    machine = _new_machine()
    _finish_engineering(machine)
    _verify_and_start_round(machine, 1, [])
    _record_complete_evidence(machine, prefix="qa")
    machine.finish_verification([])

    with pytest.raises(IllegalQualityTransition, match="Conflicting verification"):
        machine.finish_verification([_issue("late-conflict")])


def test_start_run_idempotency_key_rejects_changed_start_request() -> None:
    machine = _new_machine()

    with pytest.raises(IllegalQualityTransition, match="Conflicting replay"):
        machine.start_run(
            scope={**SCOPE, "artifact_digest": "other-artifact"},
            idempotency_key="start-run-1",
            dependencies_ready=True,
            run_id="run-1",
            required_evidence_kinds=SCOPE["required_evidence"],
        )


def test_engineering_commit_must_bind_locked_artifact() -> None:
    machine = _new_machine()
    machine.record_agent("engineer-1", "succeeded", critical=True)

    with pytest.raises(IllegalQualityTransition, match="locked artifact"):
        machine.engineer_completed(commit="git-only-commit")
