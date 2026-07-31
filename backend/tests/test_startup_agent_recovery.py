import asyncio
from types import SimpleNamespace

import pytest

from api import routes_execution
from core import app_state


def _context(*, agent, subprojects):
    return SimpleNamespace(
        owner_user_id="owner-1",
        agents={agent["id"]: agent},
        subprojects=subprojects,
        pm=SimpleNamespace(context_summary="project context"),
        description="project description",
    )


def test_startup_quality_recovery_failure_isolated_per_project(monkeypatch):
    broken = _context(agent={"id": "broken-agent", "status": "completed"}, subprojects=[])
    healthy = _context(agent={"id": "healthy-agent", "status": "completed"}, subprojects=[])
    recovered = []

    async def start_quality(ctx):
        if ctx is broken:
            raise RuntimeError("invalid persisted project evidence")
        recovered.append(ctx)

    monkeypatch.delenv("METIS_TEST_MODE", raising=False)
    monkeypatch.setattr(app_state, "projects", {
        "broken-project": broken,
        "healthy-project": healthy,
    })
    monkeypatch.setattr(routes_execution, "_start_phase_quality_cycle_if_ready", start_quality)

    resumed = asyncio.run(app_state._resume_interrupted_execution_tasks())

    assert resumed == 0
    assert recovered == [healthy]


def test_startup_leaves_active_locked_phase_agent_to_durable_recovery(
    monkeypatch, tmp_path,
):
    project_id = "startup-locked-durable"
    agent_id = "agent-locked"
    generation = "generation-current"
    agent = {
        "id": agent_id,
        "status": "working",
        "phase_id": "phase-3",
        "subproject_id": "frontend",
        "locked_tasks": [{"task_id": "phase-3-task-1"}],
        "task_execution_receipts": {
            "phase-3-task-1": {
                "status": "pending",
                "execution_generation": generation,
                "phase_coordinator_run_id": "coordinator-1",
            },
        },
    }
    ctx = _context(
        agent=agent,
        subprojects=[{
            "id": "frontend",
            "name": "Frontend",
            "description": "Build the frontend",
            "tech_stack": ["React"],
        }],
    )
    phase = {
        "phase_id": "phase-3",
        "agents": [agent_id],
        "execution_generation": generation,
        "execution_coordinator": {
            "status": "running",
            "durable_run_id": "coordinator-1",
        },
        "execution_dispatch_plan": {
            "waves": [[{
                "agent_id": agent_id,
                "task_id": "phase-3-task-1",
            }]],
        },
    }
    phase_manager = SimpleNamespace(
        project_contract={"locked": True},
        phases=[phase],
    )
    sentinel = tmp_path / "delivery.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    scheduled = []
    model_calls = []
    quality_calls = []

    async def run_agent(**kwargs):
        model_calls.append(kwargs)
        sentinel.write_text("illegal stale write", encoding="utf-8")

    def create_task(coro, *, name):
        scheduled.append(name)
        coro.close()
        return None

    async def start_quality(current_ctx):
        quality_calls.append(current_ctx)

    async def unexpected_persist():
        raise AssertionError("skipping durable recovery must not persist legacy state")

    monkeypatch.delenv("METIS_TEST_MODE", raising=False)
    monkeypatch.setattr(app_state, "projects", {project_id: ctx})
    monkeypatch.setattr(app_state, "_phase_managers", {
        project_id: phase_manager,
    })
    monkeypatch.setattr(app_state, "user_api_configs", {
        "owner-1": {"api_key": "configured"},
    })
    monkeypatch.setattr(routes_execution, "_run_agent_task", run_agent)
    monkeypatch.setattr(routes_execution, "_safe_create_task", create_task)
    monkeypatch.setattr(routes_execution, "execution_status", {})
    monkeypatch.setattr(
        routes_execution, "_start_phase_quality_cycle_if_ready", start_quality,
    )
    monkeypatch.setattr(app_state, "_persist_all_async", unexpected_persist)

    resumed = asyncio.run(app_state._resume_interrupted_execution_tasks())

    assert resumed == 0
    assert scheduled == []
    assert model_calls == []
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert agent["status"] == "working"
    assert quality_calls == [ctx]


def test_startup_still_resumes_active_nonlocked_legacy_agent(monkeypatch):
    project_id = "startup-legacy-agent"
    agent_id = "agent-legacy"
    agent = {
        "id": agent_id,
        "status": "working",
        "phase_id": "phase-legacy",
        "subproject_id": "legacy-task",
        # Legacy phases use these compatibility fields too; none of them is
        # authoritative durable ownership without a current coordinator.
        "locked_tasks": [{"task_id": "legacy-task-1"}],
        "task_execution_receipts": {
            "legacy-task-1": {
                "status": "pending",
                "phase_id": "phase-legacy",
                "execution_generation": "legacy-generation",
            },
        },
    }
    ctx = _context(
        agent=agent,
        subprojects=[{
            "id": "legacy-task",
            "name": "Legacy task",
            "description": "Resume ordinary work",
            "tech_stack": ["Python"],
        }],
    )
    model_calls = []
    scheduled = []
    persist_calls = []

    async def run_agent(**kwargs):
        model_calls.append(kwargs)
        return {"success": True}

    def create_task(coro, *, name):
        task = asyncio.create_task(coro, name=name)
        scheduled.append(task)
        return task

    async def start_quality(_ctx):
        return None

    async def persist():
        persist_calls.append(True)

    async def scenario():
        resumed = await app_state._resume_interrupted_execution_tasks()
        await asyncio.gather(*scheduled)
        return resumed

    monkeypatch.delenv("METIS_TEST_MODE", raising=False)
    monkeypatch.setattr(app_state, "projects", {project_id: ctx})
    monkeypatch.setattr(app_state, "_phase_managers", {
        project_id: SimpleNamespace(
            project_contract={"locked": False},
            phases=[{
                "phase_id": "phase-legacy",
                "agents": [agent_id],
                "execution_generation": "legacy-generation",
                "execution_run_specs": [{
                    "agent_id": agent_id,
                    "subproject_id": "legacy-task",
                }],
                "execution_dispatch_plan": {
                    "waves": [[{
                        "agent_id": agent_id,
                        "task_id": "legacy-task-1",
                    }]],
                },
            }],
        ),
    })
    monkeypatch.setattr(app_state, "user_api_configs", {
        "owner-1": {"api_key": "configured"},
    })
    monkeypatch.setattr(routes_execution, "_run_agent_task", run_agent)
    monkeypatch.setattr(routes_execution, "_safe_create_task", create_task)
    monkeypatch.setattr(routes_execution, "execution_status", {})
    monkeypatch.setattr(
        routes_execution, "_start_phase_quality_cycle_if_ready", start_quality,
    )
    monkeypatch.setattr(app_state, "_persist_all_async", persist)

    assert asyncio.run(scenario()) == 1
    assert len(model_calls) == 1
    assert model_calls[0]["agent_id"] == agent_id
    assert persist_calls == [True]


def test_startup_does_not_legacy_replay_pending_manual_durable_run(
    monkeypatch,
):
    project_id = "startup-manual-durable"
    agent_id = "agent-manual-durable"
    run_id = "manual-run-pending"
    agent = {
        "id": agent_id,
        "status": "working",
        "subproject_id": "manual-task",
    }
    ctx = _context(
        agent=agent,
        subprojects=[{
            "id": "manual-task",
            "name": "Manual durable task",
            "description": "Already scheduled by durable recovery",
            "tech_stack": ["Python"],
        }],
    )
    scheduled = []
    model_calls = []

    class Registry:
        @staticmethod
        def get(requested_run_id):
            assert requested_run_id == run_id
            return {
                "run_id": run_id,
                "run_type": "agent.execute",
                "status": "pending",
                "project_id": project_id,
                "payload": {
                    "project_id": project_id,
                    "agent_id": agent_id,
                },
            }

    async def run_agent(**kwargs):
        model_calls.append(kwargs)

    def create_task(coro, *, name):
        scheduled.append(name)
        coro.close()
        return None

    async def start_quality(_ctx):
        return None

    async def unexpected_persist():
        raise AssertionError("durable-owned Agent must not use legacy recovery")

    monkeypatch.delenv("METIS_TEST_MODE", raising=False)
    monkeypatch.setattr(app_state, "projects", {project_id: ctx})
    monkeypatch.setattr(app_state, "_phase_managers", {})
    monkeypatch.setattr(app_state, "user_api_configs", {
        "owner-1": {"api_key": "configured"},
    })
    monkeypatch.setattr(routes_execution, "_run_registry", Registry())
    monkeypatch.setattr(routes_execution, "_run_agent_task", run_agent)
    monkeypatch.setattr(routes_execution, "_safe_create_task", create_task)
    monkeypatch.setattr(routes_execution, "execution_status", {
        agent_id: {"run_id": run_id, "run_status": "pending"},
    })
    monkeypatch.setattr(
        routes_execution, "_start_phase_quality_cycle_if_ready", start_quality,
    )
    monkeypatch.setattr(app_state, "_persist_all_async", unexpected_persist)

    resumed = asyncio.run(app_state._resume_interrupted_execution_tasks())

    assert resumed == 0
    assert scheduled == []
    assert model_calls == []
    assert agent["status"] == "working"


def test_startup_durable_run_lookup_failure_is_fail_closed(monkeypatch):
    class BrokenRegistry:
        @staticmethod
        def get(_run_id):
            raise RuntimeError("database temporarily unavailable")

    monkeypatch.setattr(app_state, "_phase_managers", {})

    assert app_state._uses_durable_locked_phase_recovery(
        "project-1",
        "agent-1",
        {"id": "agent-1", "status": "working"},
        BrokenRegistry(),
        {"run_id": "indeterminate-run"},
    ) is True


@pytest.mark.parametrize(
    "run_status",
    ["succeeded", "failed", "timeout", "blocked", "cancelled"],
)
def test_startup_never_legacy_replays_terminal_manual_durable_run(
    monkeypatch, run_status,
):
    project_id = "project-terminal"
    agent_id = "agent-terminal"

    class Registry:
        @staticmethod
        def get(_run_id):
            return {
                "run_type": "agent.execute",
                "status": run_status,
                "project_id": project_id,
                "payload": {
                    "project_id": project_id,
                    "agent_id": agent_id,
                },
            }

    monkeypatch.setattr(app_state, "_phase_managers", {})

    assert app_state._uses_durable_locked_phase_recovery(
        project_id,
        agent_id,
        {"id": agent_id, "status": "working"},
        Registry(),
        {"run_id": "terminal-run"},
    ) is True


def test_startup_nonlocked_preflight_failure_remains_fail_closed(monkeypatch):
    project_id = "startup-preflight-failure"
    agent_id = "agent-missing-subproject"
    agent = {
        "id": agent_id,
        "status": "queued",
        "subproject_id": "missing-task",
    }
    ctx = _context(agent=agent, subprojects=[])
    persist_calls = []
    scheduled = []

    def create_task(coro, *, name):
        scheduled.append(name)
        coro.close()
        return None

    async def start_quality(_ctx):
        return None

    async def persist():
        persist_calls.append(True)

    monkeypatch.delenv("METIS_TEST_MODE", raising=False)
    monkeypatch.setattr(app_state, "projects", {project_id: ctx})
    monkeypatch.setattr(app_state, "_phase_managers", {})
    monkeypatch.setattr(app_state, "user_api_configs", {
        "owner-1": {"api_key": "configured"},
    })
    monkeypatch.setattr(routes_execution, "_safe_create_task", create_task)
    monkeypatch.setattr(
        routes_execution, "_start_phase_quality_cycle_if_ready", start_quality,
    )
    monkeypatch.setattr(routes_execution, "execution_status", {})
    monkeypatch.setattr(app_state, "_persist_all_async", persist)

    resumed = asyncio.run(app_state._resume_interrupted_execution_tasks())

    assert resumed == 0
    assert scheduled == []
    assert agent["status"] == "failed"
    assert "subproject is missing" in agent["lifecycle_events"][-1]["message"]
    assert routes_execution.execution_status[agent_id]["status"] == "failed"
    assert persist_calls == [True]
