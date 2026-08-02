import asyncio
from types import SimpleNamespace

from api import routes_phases


class _PhaseManager:
    def __init__(self, phase):
        self.phases = [phase]
        self.phase_agents = {}
        self.file_registry = {}
        self.current_phase_index = 0

    def get_phase(self, phase_id):
        return next(p for p in self.phases if p["phase_id"] == phase_id)

    def mark_phase_completed(self, phase_id):
        phase = self.get_phase(phase_id)
        phase.update(status="completed", user_confirmed=True)
        return {"success": True}


def test_duplicate_confirm_complete_rewrites_authoritative_receipt(
    monkeypatch,
    tmp_path,
):
    """Minimal reproduction: a second production confirm reaches persistence."""
    project_id = "duplicate-confirm-min-repro"
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
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        agents={},
        subprojects=[],
        supervisor_quality_runs={
            phase_id: {
                "status": "completed",
                "completion_gate": {"passed": True},
            },
        },
    )
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(
        routes_phases._phase_managers,
        project_id,
        _PhaseManager(phase),
    )
    monkeypatch.setattr(
        routes_phases.SupervisorQualityMachine,
        "from_dict",
        lambda _payload: SimpleNamespace(),
    )
    monkeypatch.setattr(
        routes_phases,
        "_assert_supervisor_artifact_current",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        routes_phases,
        "_record_server_acceptance_evidence",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        routes_phases,
        "_record_supervisor_acceptance_evidence",
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
        lambda *_args, **_kwargs: {"tasks": [{"task_id": "task-1"}]},
    )
    persist_count = 0

    async def persist():
        nonlocal persist_count
        persist_count += 1

    monkeypatch.setattr(routes_phases, "_persist_all_async", persist)

    first = asyncio.run(
        routes_phases.confirm_phase_complete(project_id, phase_id),
    )
    first_confirmed_at = phase["validated_completion_receipt"]["confirmed_at"]
    second = asyncio.run(
        routes_phases.confirm_phase_complete(project_id, phase_id),
    )

    assert first["success"] is True
    assert second["success"] is True
    assert persist_count == 2
    assert (
        phase["validated_completion_receipt"]["confirmed_at"]
        >= first_confirmed_at
    )
