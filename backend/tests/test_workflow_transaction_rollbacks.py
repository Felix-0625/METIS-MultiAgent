import asyncio
import copy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agents.pm_team import PMLeaderAgent
from api import routes_phases, routes_pm
from core.phase_manager import PhaseManager


class _ValidPlan:
    valid = True
    violations = []

    @staticmethod
    def to_dict():
        return {"valid": True, "issues": []}


def _phase_plan(contract):
    return {
        "project_contract": copy.deepcopy(contract),
        "phases": [{
            "phase_id": "phase-1",
            "name": "Build",
            "roles_needed": ["backend"],
            "tasks": [{
                "task_id": "phase-1-task-1",
                "name": "Build API",
                "roles": ["backend"],
                "acceptance_criteria": ["API starts"],
            }],
            "acceptance_criteria": ["API starts"],
        }],
    }


def _leader_for_phase_init(contract):
    member = SimpleNamespace(
        member_id="pm-member-1",
        name="Phase PM",
        phase_plan={},
    )
    return SimpleNamespace(
        final_plan=_phase_plan(contract),
        canonical_requirements={},
        get_final_plan_for_hr=lambda: _phase_plan(contract),
        get_member_for_phase=lambda _phase_id: member,
    )


def test_phase_init_persist_failure_restores_existing_manager(monkeypatch, tmp_path):
    project_id = "phase-init-rollback"
    old_contract = {"locked": False, "marker": "old"}
    new_contract = {"locked": True, "marker": "new"}
    manager = PhaseManager(project_id, tmp_path)
    manager.project_contract = copy.deepcopy(old_contract)
    manager.phase_agents = {"old-phase": ["agent-1"]}
    manager.file_registry = {"old.py": {"phase_id": "old-phase"}}
    before = copy.deepcopy(manager.to_dict())
    leader = _leader_for_phase_init(new_contract)

    async def fail_persist():
        raise OSError("database unavailable")

    monkeypatch.setattr(
        routes_phases,
        "_get_project",
        lambda _project_id: SimpleNamespace(workspace=tmp_path),
    )
    monkeypatch.setattr(routes_phases, "validate_plan_layers", lambda *_args: _ValidPlan())
    monkeypatch.setattr(routes_phases, "_persist_all_async", fail_persist)
    monkeypatch.setitem(routes_phases._pm_teams, project_id, leader)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, manager)

    with pytest.raises(OSError, match="database unavailable"):
        asyncio.run(routes_phases.init_project_phases(project_id))

    assert routes_phases._phase_managers[project_id] is manager
    assert manager.to_dict() == before


def test_phase_init_persist_failure_removes_new_manager(monkeypatch, tmp_path):
    project_id = "phase-init-new-manager-rollback"
    contract = {"locked": True}
    leader = _leader_for_phase_init(contract)

    async def fail_persist():
        raise OSError("database unavailable")

    monkeypatch.setattr(
        routes_phases,
        "_get_project",
        lambda _project_id: SimpleNamespace(workspace=tmp_path),
    )
    monkeypatch.setattr(routes_phases, "validate_plan_layers", lambda *_args: _ValidPlan())
    monkeypatch.setattr(routes_phases, "_persist_all_async", fail_persist)
    monkeypatch.setitem(routes_phases._pm_teams, project_id, leader)
    routes_phases._phase_managers.pop(project_id, None)

    with pytest.raises(OSError, match="database unavailable"):
        asyncio.run(routes_phases.init_project_phases(project_id))

    assert project_id not in routes_phases._phase_managers


def test_confirm_persist_failure_deeply_restores_phase_manager(
    monkeypatch, tmp_path,
):
    project_id = "confirm-deep-rollback"
    leader = PMLeaderAgent()
    canonical = leader.record_user_requirements("Build an API.", source="user")
    leader.draft_plan = {"marker": "draft"}
    leader.plan_status = "saved"
    old_leader_state = routes_pm._snapshot_requirement_state(leader)

    manager = PhaseManager(project_id, tmp_path)
    manager.phases = [{"phase_id": "old-phase", "status": "pending"}]
    manager.project_contract = {"marker": "old"}
    manager.phase_agents = {"old-phase": ["agent-1"]}
    manager.file_registry = {"old.py": {"phase_id": "old-phase"}}
    old_manager_state = copy.deepcopy(manager.to_dict())
    ctx = SimpleNamespace(subprojects=[{"id": "old-subproject"}])
    new_plan = _phase_plan({
        "locked": True,
        "requirements_revision": canonical["requirements_revision"],
        "requirements_digest": canonical["requirements_digest"],
    })

    def confirm(*_args):
        leader.plan_confirmed = True
        leader.final_plan = new_plan
        leader.plan_status = "confirmed"
        return {"success": True, "status": "confirmed"}

    leader.confirm_plan = confirm
    leader.get_final_plan_for_hr = lambda: new_plan

    async def fail_persist():
        raise OSError("database unavailable")

    monkeypatch.setattr(routes_pm, "_get_project", lambda _project_id: ctx)
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)
    monkeypatch.setattr(routes_pm, "_get_phase_manager", lambda _project_id: manager)
    monkeypatch.setattr(
        routes_pm,
        "_get_supervisor_leader",
        lambda _project_id: SimpleNamespace(load_project_plan=lambda _plan: None),
    )
    monkeypatch.setattr(
        routes_pm,
        "_get_engineer",
        lambda _project_id: SimpleNamespace(load_project_context=lambda _plan: None),
    )
    monkeypatch.setattr(routes_pm, "_persist_all_async", fail_persist)
    monkeypatch.setitem(routes_pm._phase_managers, project_id, manager)

    with pytest.raises(OSError, match="database unavailable"):
        asyncio.run(routes_pm.confirm_pm_plan(
            project_id,
            routes_pm.PlanConfirmRequest(
                requirements_revision=canonical["requirements_revision"],
                requirements_digest=canonical["requirements_digest"],
            ),
        ))

    assert routes_pm._snapshot_requirement_state(leader) == old_leader_state
    assert manager.to_dict() == old_manager_state
    assert ctx.subprojects == [{"id": "old-subproject"}]


def test_confirm_persisted_business_rejection_is_not_rolled_back(monkeypatch):
    project_id = "confirm-business-rejection"
    leader = PMLeaderAgent()
    canonical = leader.record_user_requirements("Build an API.", source="user")
    leader.draft_plan = {"marker": "draft"}
    leader.plan_status = "saved"
    persisted_statuses = []

    def reject(*_args):
        leader.plan_status = "validation_failed"
        leader.last_plan_violations = [{"code": "phase_tasks_empty"}]
        return {
            "success": False,
            "status": "validation_failed",
            "validation": {
                "valid": False,
                "issues": [{"code": "phase_tasks_empty"}],
            },
        }

    leader.confirm_plan = reject

    async def persist():
        persisted_statuses.append(leader.plan_status)

    monkeypatch.setattr(
        routes_pm,
        "_get_project",
        lambda _project_id: SimpleNamespace(subprojects=[]),
    )
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)
    monkeypatch.setattr(routes_pm, "_persist_all_async", persist)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_pm.confirm_pm_plan(
            project_id,
            routes_pm.PlanConfirmRequest(
                requirements_revision=canonical["requirements_revision"],
                requirements_digest=canonical["requirements_digest"],
            ),
        ))

    assert caught.value.status_code == 422
    assert persisted_statuses == ["validation_failed"]
    assert leader.plan_status == "validation_failed"
    assert leader.last_plan_violations == [{"code": "phase_tasks_empty"}]


def test_confirm_holds_requirement_lock_until_business_result_is_persisted(
    monkeypatch,
):
    project_id = "confirm-revision-linearization"
    leader = PMLeaderAgent()
    canonical = leader.record_user_requirements("Use React.", source="user")
    leader.draft_plan = {"marker": "draft"}
    confirmation_entered = asyncio.Event()
    release_confirmation = asyncio.Event()
    revision_mutated = asyncio.Event()

    def reject(*_args):
        leader.plan_status = "validation_failed"
        return {
            "success": False,
            "status": "validation_failed",
            "validation": {
                "valid": False,
                "issues": [{"code": "phase_tasks_empty"}],
            },
        }

    leader.confirm_plan = reject

    async def fake_to_thread(function, *args, **kwargs):
        if function is reject:
            confirmation_entered.set()
            await release_confirmation.wait()
        return function(*args, **kwargs)

    async def persist():
        return None

    monkeypatch.setattr(routes_pm.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        routes_pm,
        "_get_project",
        lambda _project_id: SimpleNamespace(subprojects=[]),
    )
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)
    monkeypatch.setattr(routes_pm, "_persist_all_async", persist)

    async def scenario():
        confirmation = asyncio.create_task(routes_pm.confirm_pm_plan(
            project_id,
            routes_pm.PlanConfirmRequest(
                requirements_revision=canonical["requirements_revision"],
                requirements_digest=canonical["requirements_digest"],
            ),
        ))
        await asyncio.wait_for(confirmation_entered.wait(), timeout=1)

        def revise():
            leader.record_user_requirements(
                "Use Vue.",
                source="user",
                replace=True,
                expected_revision=canonical["requirements_revision"],
                expected_digest=canonical["requirements_digest"],
            )
            revision_mutated.set()

        revision = asyncio.create_task(routes_pm._commit_requirements_transaction(
            project_id, leader, revise,
        ))
        await asyncio.sleep(0)
        assert revision_mutated.is_set() is False
        release_confirmation.set()
        confirmation_result, revision_result = await asyncio.gather(
            confirmation, revision, return_exceptions=True,
        )
        return confirmation_result, revision_result

    confirmation_result, revision_result = asyncio.run(scenario())

    assert isinstance(confirmation_result, HTTPException)
    assert confirmation_result.status_code == 422
    assert not isinstance(revision_result, Exception)
    assert revision_mutated.is_set() is True
    assert leader.requirements_revision == canonical["requirements_revision"] + 1
    assert leader.canonical_requirements == "Use Vue."


def test_phase_completion_evidence_failure_rolls_back_all_receiver_state(
    monkeypatch, tmp_path,
):
    project_id = "phase-completion-evidence-rollback"
    phase_id = "phase-1"
    phase = {
        "phase_id": phase_id,
        "reviewed": True,
        "review_passed": True,
        "status": "reviewing",
        "authoritative_criterion_evidence": [],
    }
    manager = SimpleNamespace(
        phases=[phase],
        phase_agents={},
        file_registry={},
        current_phase_index=0,
        get_phase=lambda requested: phase if requested == phase_id else None,
    )
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        subprojects=[],
        agents={"agent-1": {
            "id": "agent-1",
            "phase_id": phase_id,
            "task_execution_receipts": {"task-1": {
                "criterion_evidence": [],
            }},
        }},
        supervisor_quality_runs={phase_id: {
            "run_id": "supervisor-run",
            "status": "completed",
            "completion_gate": {"passed": True},
            "phase_evidence": [],
        }},
    )
    before_phase = copy.deepcopy(phase)
    before_agents = copy.deepcopy(ctx.agents)
    before_supervisor = copy.deepcopy(ctx.supervisor_quality_runs)

    monkeypatch.setitem(routes_phases._phase_managers, project_id, manager)
    monkeypatch.setattr(
        routes_phases,
        "_reconcile_verified_supervisor_registry",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        routes_phases,
        "_reconcile_deterministic_pre_qa_registry",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        routes_phases,
        "_assert_supervisor_artifact_current",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        routes_phases.SupervisorQualityMachine,
        "from_dict",
        classmethod(lambda _cls, _data: object()),
    )
    monkeypatch.setattr(
        routes_phases,
        "_record_server_acceptance_evidence",
        lambda *_args: None,
    )

    def receive_supervisor_evidence(receiver_ctx, receiver_phase):
        record = {"evidence_id": "evidence-1"}
        receiver_phase["authoritative_criterion_evidence"].append(record)
        receiver_ctx.agents["agent-1"]["task_execution_receipts"][
            "task-1"
        ]["criterion_evidence"].append({"record": record})
        receiver_ctx.supervisor_quality_runs[phase_id][
            "phase_evidence"
        ].append({"record": record})

    monkeypatch.setattr(
        routes_phases,
        "_record_supervisor_acceptance_evidence",
        receive_supervisor_evidence,
    )
    monkeypatch.setattr(
        routes_phases,
        "_record_user_confirmation_evidence",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        routes_phases,
        "_assert_phase_execution_completed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            HTTPException(status_code=409, detail="acceptance incomplete")
        ),
    )

    with pytest.raises(HTTPException, match="acceptance incomplete"):
        asyncio.run(routes_phases._confirm_phase_complete_locked(
            ctx, project_id, phase_id,
        ))

    assert phase == before_phase
    assert ctx.agents == before_agents
    assert ctx.supervisor_quality_runs == before_supervisor
