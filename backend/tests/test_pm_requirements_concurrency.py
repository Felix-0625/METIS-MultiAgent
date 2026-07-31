import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agents.pm_team import PMLeaderAgent
from api import routes_pm


def test_confirmation_projection_linearizes_before_requirement_revision(monkeypatch):
    project_id = "confirmation-linearization"
    leader = PMLeaderAgent()
    current = leader.record_user_requirements("Use React.", source="user")
    leader.plan_confirmed = True
    leader.final_plan = {"phases": [{"phase_id": "phase-1", "name": "Build"}]}
    leader.get_final_plan_for_hr = lambda: leader.final_plan
    ctx = SimpleNamespace(subprojects=[])
    phase_manager = SimpleNamespace(phases=[])

    def init_phases(plan):
        phase_manager.phases = [
            {"phase_id": "phase-1", "name": "Build", "roles_needed": ["frontend"]}
        ]

    phase_manager.init_phases_from_plan = init_phases
    supervisor = SimpleNamespace(load_project_plan=lambda _plan: None)
    engineer = SimpleNamespace(load_project_context=lambda _plan: None)
    projection_entered = asyncio.Event()
    release_projection = asyncio.Event()
    revision_started = asyncio.Event()
    revision_mutated = asyncio.Event()
    persist_revisions = []

    async def fake_to_thread(function, *args, **kwargs):
        if function is routes_pm._get_phase_manager:
            projection_entered.set()
            await release_projection.wait()
        return function(*args, **kwargs)

    async def persist():
        persist_revisions.append(leader.requirements_revision)

    monkeypatch.setattr(routes_pm.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(routes_pm, "_get_phase_manager", lambda _project_id: phase_manager)
    monkeypatch.setattr(routes_pm, "_get_supervisor_leader", lambda _project_id: supervisor)
    monkeypatch.setattr(routes_pm, "_get_engineer", lambda _project_id: engineer)
    monkeypatch.setattr(routes_pm, "_persist_all_async", persist)

    async def scenario():
        confirmation = asyncio.create_task(
            routes_pm._commit_confirmation_result(
                project_id,
                ctx,
                leader,
                expected_revision=current["requirements_revision"],
                expected_digest=current["requirements_digest"],
                result={"success": True, "status": "confirmed"},
            )
        )
        await asyncio.wait_for(projection_entered.wait(), timeout=1)

        def revise():
            leader.record_user_requirements(
                "Use Vue.",
                source="user",
                replace=True,
                expected_revision=current["requirements_revision"],
                expected_digest=current["requirements_digest"],
            )
            revision_mutated.set()

        async def run_revision():
            revision_started.set()
            return await routes_pm._commit_requirements_transaction(
                project_id,
                leader,
                revise,
            )

        revision = asyncio.create_task(run_revision())
        await asyncio.wait_for(revision_started.wait(), timeout=1)
        assert revision_mutated.is_set() is False

        release_projection.set()
        confirmed, _ = await asyncio.gather(confirmation, revision)
        return confirmed

    confirmed = asyncio.run(scenario())

    assert confirmed["status"] == "confirmed"
    assert persist_revisions == [1, 2]
    assert phase_manager.phases[0]["requirements_revision"] == 1
    assert phase_manager.phases[0]["requirements_digest"] == current["requirements_digest"]
    assert leader.requirements_revision == 2


def test_synthesis_commit_rejects_a_stale_requirements_generation(monkeypatch):
    leader = PMLeaderAgent()
    original = leader.record_user_requirements("Use React.", source="user")
    leader.record_user_requirements(
        "Use Vue.",
        source="user",
        replace=True,
        expected_revision=original["requirements_revision"],
        expected_digest=original["requirements_digest"],
    )
    leader.draft_plan = {"marker": "stale"}

    async def persist():
        return None

    monkeypatch.setattr(routes_pm, "_persist_all_async", persist)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            routes_pm._commit_synthesis_result(
                "stale-synthesis",
                leader,
                bound_revision=original["requirements_revision"],
                bound_digest=original["requirements_digest"],
                result={"success": True, "status": "saved"},
            )
        )

    assert caught.value.status_code == 409
    assert leader.plan_confirmed is False
    assert leader.draft_plan is None
    assert leader.final_plan is None
