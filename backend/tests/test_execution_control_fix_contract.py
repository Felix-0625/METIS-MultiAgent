"""Failing contracts for the four execution-control fixes awaiting one writer."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading

import pytest

from api import routes_phases
from core import database
from core.execution_runs import DurableRunRegistry
from tests.test_execution_runs import Clock
from tests.test_phase_rebuild_flow import _install_durable_phase_reset_state


class InjectedWriteFailure(RuntimeError):
    pass


@pytest.fixture
def run_db(tmp_path, monkeypatch):
    path = tmp_path / "execution-control-fix-contract.db"
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setattr(database, "_sqlite_path", str(path))
    database.init_db()
    return path


def _parent_with_children(registry, *, prefix: str, child_count: int = 2):
    parent, _ = registry.create_or_get_run(
        idempotency_key=f"{prefix}-parent", run_type="phase.dispatch",
        actor_id="coordinator", timeout_seconds=60,
        run_id=f"{prefix}-parent",
    )
    registry.claim(parent["run_id"], "coordinator", lease_seconds=30)
    children = []
    for index in range(child_count):
        child, _ = registry.create_or_get_run(
            idempotency_key=f"{prefix}-child-{index}",
            run_type="agent.execute", actor_id=f"agent-{index}",
            parent_run_id=parent["run_id"], critical=True,
            timeout_seconds=10, run_id=f"{prefix}-child-{index}",
        )
        registry.claim(child["run_id"], f"worker-{index}", lease_seconds=5)
        children.append(child)
    return parent, children


def test_parent_blocked_upgrades_to_failed_after_later_critical_failure(run_db):
    registry = DurableRunRegistry(Clock())
    parent, children = _parent_with_children(registry, prefix="ordered-upgrade")
    registry.block(children[0]["run_id"], reason="dependency unavailable")
    assert registry.get(parent["run_id"])["status"] == "blocked"

    registry.fail(
        children[1]["run_id"], "worker-1",
        error="provider permanently failed", retryable=False,
    )
    upgraded = registry.get(parent["run_id"])
    assert upgraded["status"] == "failed"
    assert upgraded["last_error"] == (
        f"critical child {children[1]['run_id']} entered failed"
    )
    assert [event["to_status"] for event in registry.events(parent["run_id"])] == [
        "pending", "running", "blocked", "failed",
    ]


def test_parent_converges_under_concurrent_block_and_failure(run_db):
    registry = DurableRunRegistry(Clock())
    parent, children = _parent_with_children(registry, prefix="concurrent-upgrade")
    barrier = threading.Barrier(2)

    def finish(index):
        barrier.wait()
        if index == 0:
            return registry.block(children[index]["run_id"], reason="blocked")
        return registry.fail(
            children[index]["run_id"], f"worker-{index}",
            error="hard failure", retryable=False,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(finish, range(2)))

    parent_run = registry.get(parent["run_id"])
    assert parent_run["status"] == "failed"
    versions = [event["version"] for event in registry.events(parent["run_id"])]
    assert versions == sorted(set(versions))


def test_restart_blocked_parent_can_upgrade_after_peer_failure(run_db):
    clock = Clock()
    registry = DurableRunRegistry(clock)
    parent, children = _parent_with_children(registry, prefix="restart-upgrade")
    registry.heartbeat(children[1]["run_id"], "worker-1", lease_seconds=30)
    clock.advance(6)
    registry.recover_startup()
    assert registry.get(children[0]["run_id"])["status"] == "blocked"
    assert registry.get(parent["run_id"])["status"] == "blocked"

    registry.fail(
        children[1]["run_id"], "worker-1",
        error="peer failed after restart", retryable=False,
    )
    assert registry.get(parent["run_id"])["status"] == "failed"


def test_blocked_child_without_live_execution_allows_manual_phase_reset(
    monkeypatch, tmp_path,
):
    project_id, phase_id, _, phase, ctx = _install_durable_phase_reset_state(
        monkeypatch, tmp_path, run_status="blocked", active_lock=False,
    )
    result = asyncio.run(routes_phases.reset_phase(project_id, phase_id))
    assert result["success"] is True
    assert phase["status"] == "pending"
    assert ctx.agents == {}


def test_phase_reset_removes_stale_durable_file_ownership(
    monkeypatch, tmp_path, run_db,
):
    project_id, phase_id, _, _, _ = _install_durable_phase_reset_state(
        monkeypatch, tmp_path, run_status="failed", active_lock=False,
    )
    stale_path = "backend/main.py"
    database.commit_project_files(project_id, {
        stale_path: {
            "content": b"old phase output\n",
            "phase_id": phase_id,
            "task_id": "old-task",
            "agent_id": "old-agent",
        },
        "README.md": {
            "content": b"preserve another phase\n",
            "phase_id": "phase-2",
        },
    })

    result = asyncio.run(routes_phases.reset_phase(project_id, phase_id))

    assert result["success"] is True
    assert set(database.load_project_files(project_id)) == {"README.md"}


def test_blocked_child_without_live_execution_allows_rebuild_reset(
    monkeypatch, tmp_path,
):
    project_id, phase_id, _, phase, _ = _install_durable_phase_reset_state(
        monkeypatch, tmp_path, run_status="blocked", active_lock=False,
    )
    phase["rebuild_file_manifest"] = {"files": []}
    result = asyncio.run(routes_phases._reset_phase(
        project_id, phase_id, preserve_rebuild_state=True,
    ))
    assert result["success"] is True
    assert phase["status"] == "pending"


@pytest.mark.parametrize("side", ["before", "during"])
def test_phase_reset_rolls_back_memory_when_persistence_fails(
    monkeypatch, tmp_path, side,
):
    project_id, phase_id, _, phase, ctx = _install_durable_phase_reset_state(
        monkeypatch, tmp_path, run_status="failed", active_lock=False,
    )
    before = {
        "phase": deepcopy(phase),
        "agents": deepcopy(ctx.agents),
        "subprojects": deepcopy(ctx.subprojects),
    }

    async def fail_persist():
        if side == "during":
            (tmp_path / "partial-reset-write").write_text("partial", encoding="utf-8")
        raise InjectedWriteFailure(f"reset {side}-write failure")

    monkeypatch.setattr(routes_phases, "_persist_all_async", fail_persist)
    with pytest.raises(InjectedWriteFailure):
        asyncio.run(routes_phases.reset_phase(project_id, phase_id))

    assert phase == before["phase"]
    assert ctx.agents == before["agents"]
    assert ctx.subprojects == before["subprojects"]


def test_cancel_persists_reason_and_releases_lease(run_db):
    registry = DurableRunRegistry(Clock())
    run, _ = registry.create_or_get_run(
        idempotency_key="cancel-reason", run_type="agent.execute",
        actor_id="agent", timeout_seconds=10, run_id="cancel-reason",
    )
    registry.claim(run["run_id"], "worker", lease_seconds=5)
    cancelled = registry.cancel(
        run["run_id"], reason="operator cancelled", actor="operator",
    )
    assert cancelled["status"] == "cancelled"
    assert cancelled["last_error"] == "operator cancelled"
    assert cancelled["lease_owner"] is None
    assert cancelled["lease_expires_at"] is None
    assert registry.events(run["run_id"])[-1]["reason"] == (
        "operator: operator cancelled"
    )
