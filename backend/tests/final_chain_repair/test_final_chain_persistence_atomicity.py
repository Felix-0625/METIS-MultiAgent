import asyncio
import copy
from types import SimpleNamespace

import pytest

from api import routes_adjustments, routes_phases


def test_final_qa_initial_publish_rolls_back_all_memory_on_persist_failure(monkeypatch, tmp_path):
    project_id = "final-qa-persist-failure-red-b"
    durable_before = {"__whole_project__": {"qa": {
        "passed": False, "status": "failed",
        "runtime_acceptance": {"passed": False, "status": "previous_failure"},
        "issues_detail": [{"id": "repair-1", "status": "open", "fix_rounds": 1}],
    }}}
    ctx = SimpleNamespace(project_id=project_id, workspace=tmp_path,
                          qc_results=copy.deepcopy(durable_before))
    live_status = {
        "status": "running", "round": 2, "all_passed": False,
        "runtime_acceptance": {"passed": True, "status": "passed",
                               "artifact_sha256": "a" * 64},
        "repair_state": {"issue_id": "repair-1", "status": "fixing", "fix_rounds": 2},
    }
    live_before = copy.deepcopy(live_status)
    monkeypatch.setitem(routes_adjustments.projects, project_id, ctx)
    monkeypatch.setitem(routes_adjustments._final_qa_status, project_id, live_status)
    monkeypatch.setitem(routes_adjustments._final_qa_fence_tokens, project_id, "fence-b")

    async def fail_persist():
        raise OSError("injected sqlite commit failure")

    monkeypatch.setattr(routes_adjustments, "_persist_all_async", fail_persist)
    with pytest.raises(OSError, match="sqlite commit failure"):
        asyncio.run(routes_adjustments._run_final_qa_loop(project_id))
    assert ctx.qc_results == durable_before
    assert routes_adjustments._final_qa_status[project_id] == live_before


def test_auto_repair_stale_transition_rolls_back_on_persist_failure(monkeypatch, tmp_path):
    project_id, phase_id = "auto-repair-persist-failure-red-b", "phase-1"
    key = f"{project_id}-{phase_id}"
    phase = {"phase_id": phase_id, "status": "reviewing",
             "reviewed": True, "review_passed": True}
    state = {"running": False, "status": "passed", "needs_manual": False,
             "action_required": None, "review_result": {"passed": True}}
    phase_before, state_before = copy.deepcopy(phase), copy.deepcopy(state)
    ctx = SimpleNamespace(project_id=project_id, workspace=tmp_path)
    manager = SimpleNamespace(get_phase=lambda requested: phase if requested == phase_id else None)
    machine = SimpleNamespace(state="blocked", to_dict=lambda: {
        "state": "blocked", "completion_gate": {"passed": False}})
    monkeypatch.setattr(routes_phases, "_get_project", lambda _pid: ctx)
    monkeypatch.setattr(routes_phases, "_assert_project_write_available", lambda _ctx: None)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, manager)
    monkeypatch.setitem(routes_phases._auto_repair_states, key, state)
    monkeypatch.setattr(routes_phases, "_supervisor_quality_machine", lambda *_args: machine)

    async def fail_persist():
        raise OSError("injected repair-state sqlite commit failure")

    monkeypatch.setattr(routes_phases, "_persist_all_async", fail_persist)
    with pytest.raises(OSError, match="repair-state sqlite commit failure"):
        asyncio.run(routes_phases.start_auto_repair(project_id, phase_id))
    assert routes_phases._auto_repair_states[key] == state_before
    assert phase == phase_before
