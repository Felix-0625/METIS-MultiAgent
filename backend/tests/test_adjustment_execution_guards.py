import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api import routes_adjustments, routes_engineer
from core import app_state
from core.project_write_fence import (
    acquire_project_write_fence,
    get_project_write_fence,
    release_project_write_fence,
)
from core.workspace_integrity import compute_delivery_manifest


def _context(tmp_path: Path, project_id: str = "project-adjustment"):
    return SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        description="Deliver app.py and config.py",
        pm=SimpleNamespace(context_summary=""),
        subprojects=[],
        agents={},
        qc_results={"__whole_project__": {"qa": {"passed": True, "status": "passed"}}},
        status="completed",
    )


def test_adjustment_required_paths_use_locked_manifest_not_plan_prose(
    monkeypatch,
    tmp_path,
):
    ctx = _context(tmp_path)
    ctx.description = "Build a React application."
    leader = SimpleNamespace(final_plan={
        "phases": [{
            "tasks": [{
                "description": "Create frontend with src/App.jsx and styles.",
                "required_files": ["frontend/src/App.jsx"],
            }],
        }],
    })
    manager = SimpleNamespace(project_contract={
        "locked": True,
        "required_files": [{
            "path": "frontend/src/App.jsx",
            "required": True,
        }],
    })
    monkeypatch.setitem(routes_adjustments._pm_teams, ctx.project_id, leader)
    monkeypatch.setitem(
        routes_adjustments._phase_managers,
        ctx.project_id,
        manager,
    )

    assert routes_adjustments._adjustment_required_paths(ctx) == [
        "frontend/src/App.jsx",
    ]


def _adjustment(*, tasks):
    for task in tasks:
        task.setdefault("expert_role", "Backend Engineer")
        task.setdefault("description", f"Implement {task.get('task_id')}")
        task.setdefault("files", ["app.py"])
        task.setdefault("deliverables", list(task["files"]))
        task.setdefault("acceptance_criteria", ["Declared behavior is verified"])
    return {
        "id": "adj-1",
        "title": "Guarded adjustment",
        "description": "Change the delivery",
        "tasks": tasks,
        "phases": [],
        "status": "pending",
        "exec_log": [],
    }


def _ready_acceptance(adjustment, manifest):
    contract = routes_adjustments._build_adjustment_execution_contract(
        adjustment,
        adjustment["tasks"],
        mode="full",
        phase_index=None,
        request_digest="request",
        modifications="",
        input_manifest=manifest,
    )
    adjustment["active_run"] = {
        "status": "awaiting_final_qa",
        "execution_contract": contract,
    }
    adjustment["status"] = "awaiting_final_qa"
    adjustment["requires_final_qa"] = True
    adjustment["adjustment_acceptance"] = {
        "contract_sha256": contract["contract_sha256"],
        "result_artifact_sha256": manifest["artifact_sha256"],
        "changed_paths": ["app.py"],
        "acceptance_criteria": [
            criterion
            for task in contract["tasks"]
            for criterion in task["acceptance_criteria"]
        ],
        "modifications": "",
        "status": "pending_final_qa",
    }
    return contract


def _authoritative_qa(
    manifest,
    adjustment=None,
    *,
    run_id="qa-run",
    generation="qa-generation",
):
    result = {
        "passed": True,
        "status": "passed",
        "final_qa_run_id": run_id,
        "final_qa_generation": generation,
        "artifact_sha256": manifest["artifact_sha256"],
        "score": 100,
        "observed_issues_detail": [],
        "runtime_acceptance": {
            "enabled": True,
            "passed": True,
            "status": "passed",
            "artifact_sha256": manifest["artifact_sha256"],
            "artifact_manifest_rule_version": manifest["rule_version"],
        },
    }
    if adjustment is not None:
        contract = adjustment["active_run"]["execution_contract"]
        result["criterion_evidence"] = [
            {
                "criterion_id": criterion["criterion_id"],
                "evidence_type": criterion["required_evidence_type"],
                "run_id": run_id,
                "qa_generation": generation,
                "artifact_sha256": manifest["artifact_sha256"],
                "source_sha256": f"source-{criterion['criterion_id']}",
                "passed": True,
                "evidence_id": f"evidence-{criterion['criterion_id']}",
                "source": {"verified": True},
            }
            for task in contract["tasks"]
            for criterion in task["criteria"]
        ]
    return result


def test_adjustment_dag_fails_closed_for_missing_dependency_and_cycle():
    missing = [
        {"task_id": "a", "status": "pending", "depends_on": ["missing"]},
    ]
    with pytest.raises(ValueError, match="missing dependency"):
        routes_adjustments._adjustment_task_layers(missing, missing)

    cycle = [
        {"task_id": "a", "status": "pending", "depends_on": ["b"]},
        {"task_id": "b", "status": "pending", "depends_on": ["a"]},
    ]
    with pytest.raises(ValueError, match="cycle"):
        routes_adjustments._adjustment_task_layers(cycle, cycle)


def test_adjustment_snapshot_restore_is_atomic_when_second_replace_fails(
    monkeypatch,
    tmp_path,
):
    ctx = _context(tmp_path)
    (tmp_path / "app.py").write_text("original app\n", encoding="utf-8")
    (tmp_path / "config.py").write_text("original config\n", encoding="utf-8")
    snapshot = routes_adjustments._take_adjustment_snapshot(ctx)
    (tmp_path / "app.py").write_text("changed app\n", encoding="utf-8")
    (tmp_path / "config.py").write_text("changed config\n", encoding="utf-8")
    changed_digest = compute_delivery_manifest(tmp_path)["artifact_sha256"]

    original_replace = routes_adjustments.os.replace
    calls = 0

    def fail_second_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second-file failure")
        return original_replace(source, destination)

    monkeypatch.setattr(routes_adjustments.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="second-file"):
        routes_adjustments._restore_adjustment_snapshot(ctx, snapshot)

    assert compute_delivery_manifest(tmp_path)["artifact_sha256"] == changed_digest
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "changed app\n"
    assert (tmp_path / "config.py").read_text(encoding="utf-8") == "changed config\n"


def test_failed_adjustment_agent_is_not_marked_completed(monkeypatch, tmp_path):
    ctx = _context(tmp_path)
    adjustment = _adjustment(
        tasks=[{"task_id": "a", "status": "pending", "depends_on": []}]
    )
    task = adjustment["tasks"][0]

    class FailedExecutionAgent:
        def execute_task(self, **_kwargs):
            return {"success": False, "error": "provider failure"}

    from api import routes_execution

    monkeypatch.setattr(
        routes_execution,
        "_make_exec_agent",
        lambda *_args, **_kwargs: FailedExecutionAgent(),
    )
    fence = acquire_project_write_fence(
        ctx.project_id,
        tmp_path,
        owner="adjustment-test",
        purpose="adjustment",
    )
    guard = routes_adjustments._FenceExecutionGuard(
        ctx.project_id,
        tmp_path,
        str(fence["token"]),
    )
    try:
        with pytest.raises(RuntimeError, match="provider failure"):
            asyncio.run(
                routes_adjustments._run_adjustment_task(
                    ctx,
                    adjustment,
                    task,
                    "",
                    guard,
                )
            )
    finally:
        release_project_write_fence(
            ctx.project_id, tmp_path, str(fence["token"])
        )

    assert task["status"] == "failed"
    assert ctx.agents[task["agent_id"]]["status"] == "failed"


def test_adjustment_failure_restores_workspace_and_never_runs_raw_qc(
    monkeypatch,
    tmp_path,
):
    project_id = "project-adjustment-failure"
    ctx = _context(tmp_path, project_id)
    (tmp_path / "app.py").write_text("baseline\n", encoding="utf-8")
    adjustment = _adjustment(
        tasks=[
            {
                "task_id": "a",
                "status": "pending",
                "depends_on": ["missing"],
            }
        ]
    )
    adjustments = [adjustment]

    async def persist():
        return None

    def raw_qc_forbidden(*_args, **_kwargs):
        raise AssertionError("adjustment execution must not call ad-hoc QC")

    monkeypatch.setitem(routes_adjustments.projects, project_id, ctx)
    monkeypatch.setattr(routes_adjustments, "_get_project", lambda _pid: ctx)
    monkeypatch.setattr(routes_adjustments, "_get_adjustments", lambda _pid: adjustments)
    monkeypatch.setattr(routes_adjustments, "_persist_all_async", persist)
    monkeypatch.setattr(
        routes_adjustments,
        "_run_qc_for_subproject",
        raw_qc_forbidden,
    )

    async def scenario():
        result = await routes_adjustments._start_adjustment_run(
            project_id,
            adjustment["id"],
            mode="full",
            phase_index=None,
            request_digest="request-a",
        )
        task = routes_adjustments._adjustment_tasks[project_id]
        await task
        return result

    result = asyncio.run(scenario())

    assert result["status"] == "executing"
    assert adjustment["status"] == "needs_manual"
    assert adjustment["active_run"]["workspace_restored"] is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "baseline\n"
    assert adjustment["tasks"][0]["status"] == "pending"


def test_successful_adjustment_stops_at_authoritative_final_qa_gate(
    monkeypatch,
    tmp_path,
):
    project_id = "project-adjustment-success"
    ctx = _context(tmp_path, project_id)
    (tmp_path / "app.py").write_text("baseline\n", encoding="utf-8")
    adjustment = _adjustment(
        tasks=[{"task_id": "a", "status": "pending", "depends_on": []}]
    )
    adjustments = [adjustment]

    async def persist():
        return None

    async def successful_task(
        _ctx, _adj, task, _context, execution_guard, _contract
    ):
        with pytest.raises(
            routes_adjustments.ProjectWriteFenceConflict,
            match="frozen",
        ):
            with routes_adjustments.project_write_guard(project_id, tmp_path):
                pass
        with execution_guard.write_guard():
            (tmp_path / "app.py").write_text("adjusted\n", encoding="utf-8")
        task["status"] = "done"
        return {"success": True, "output_files": ["app.py"]}

    def raw_qc_forbidden(*_args, **_kwargs):
        raise AssertionError("adjustment execution must not call ad-hoc QC")

    monkeypatch.setitem(routes_adjustments.projects, project_id, ctx)
    monkeypatch.setattr(routes_adjustments, "_get_project", lambda _pid: ctx)
    monkeypatch.setattr(routes_adjustments, "_get_adjustments", lambda _pid: adjustments)
    monkeypatch.setattr(routes_adjustments, "_persist_all_async", persist)
    monkeypatch.setattr(routes_adjustments, "_run_adjustment_task", successful_task)
    monkeypatch.setattr(
        routes_adjustments,
        "_run_qc_for_subproject",
        raw_qc_forbidden,
    )

    async def scenario():
        first = await routes_adjustments._start_adjustment_run(
            project_id,
            adjustment["id"],
            mode="full",
            phase_index=None,
            request_digest="same-request",
        )
        replay = await routes_adjustments._start_adjustment_run(
            project_id,
            adjustment["id"],
            mode="full",
            phase_index=None,
            request_digest="same-request",
        )
        with pytest.raises(HTTPException) as conflict:
            await routes_adjustments._start_adjustment_run(
                project_id,
                adjustment["id"],
                mode="full",
                phase_index=None,
                request_digest="different-request",
            )
        task = routes_adjustments._adjustment_tasks[project_id]
        await task
        return first, replay, conflict.value

    first, replay, conflict = asyncio.run(scenario())

    assert first["status"] == "executing"
    assert replay["already_running"] is True
    assert conflict.status_code == 409
    assert adjustment["status"] == "awaiting_final_qa"
    assert adjustment["requires_final_qa"] is True
    assert ctx.qc_results["__whole_project__"]["qa"]["passed"] is False
    assert ctx.qc_results["__whole_project__"]["qa"]["status"] == "stale"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "adjusted\n"
    assert get_project_write_fence(project_id, tmp_path) is None


def test_revoked_fence_guard_blocks_late_worker_write(tmp_path):
    guard = routes_adjustments._FenceExecutionGuard(
        "late-worker-project",
        tmp_path,
        "unused-token",
    )
    guard.revoke()
    with pytest.raises(
        routes_adjustments.ProjectWriteFenceConflict,
        match="revoked",
    ):
        with guard.write_guard():
            (tmp_path / "late.py").write_text("late write", encoding="utf-8")
    assert not (tmp_path / "late.py").exists()


def test_old_final_qa_callback_cannot_pop_new_task_or_fence():
    project_id = "final-qa-task-identity"
    old_task = object()
    new_task = object()
    routes_adjustments._final_qa_tasks[project_id] = new_task
    routes_adjustments._final_qa_fence_tokens[project_id] = "new-token"

    routes_adjustments._on_final_qa_done(project_id, old_task)

    assert routes_adjustments._final_qa_tasks[project_id] is new_task
    assert routes_adjustments._final_qa_fence_tokens[project_id] == "new-token"
    routes_adjustments._final_qa_tasks.pop(project_id, None)
    routes_adjustments._final_qa_fence_tokens.pop(project_id, None)


def test_old_final_qa_qc_generation_cannot_overwrite_new_failed_result(tmp_path):
    project_id = "final-qa-qc-generation"
    ctx = _context(tmp_path, project_id)
    (tmp_path / "app.py").write_text("stable\n", encoding="utf-8")
    manifest = compute_delivery_manifest(tmp_path)
    ctx.qc_results["__whole_project__"] = {
        "qa": {"passed": False, "status": "failed"}
    }
    fence = routes_adjustments.acquire_project_write_fence(
        project_id,
        tmp_path,
        owner="final-qa-test",
        purpose="final_qa",
    )
    token = fence["token"]
    status = {
        "run_id": "new-run",
        "qc_generation": "new-generation",
    }

    async def scenario():
        task = asyncio.current_task()
        routes_adjustments._final_qa_status[project_id] = status
        routes_adjustments._final_qa_tasks[project_id] = task
        routes_adjustments._final_qa_fence_tokens[project_id] = token
        stale_committed = routes_adjustments._commit_final_qa_qc_entry(
            ctx,
            status,
            project_id=project_id,
            run_id="new-run",
            generation="old-generation",
            artifact_sha256=manifest["artifact_sha256"],
            required_files=[],
            subproject_id="__whole_project__",
            qc_entry={"passed": True, "status": "passed"},
            fence_token=token,
            owner_task=task,
        )
        current_committed = routes_adjustments._commit_final_qa_qc_entry(
            ctx,
            status,
            project_id=project_id,
            run_id="new-run",
            generation="new-generation",
            artifact_sha256=manifest["artifact_sha256"],
            required_files=[],
            subproject_id="__whole_project__",
            qc_entry={"passed": False, "status": "failed"},
            fence_token=token,
            owner_task=task,
        )
        return stale_committed, current_committed

    try:
        stale_committed, current_committed = asyncio.run(scenario())
        assert stale_committed is False
        assert current_committed is True
        assert ctx.qc_results["__whole_project__"]["qa"]["passed"] is False
        assert (
            ctx.qc_results["__whole_project__"]["qa"]["final_qa_generation"]
            == "new-generation"
        )
    finally:
        routes_adjustments._final_qa_status.pop(project_id, None)
        routes_adjustments._final_qa_tasks.pop(project_id, None)
        routes_adjustments._final_qa_fence_tokens.pop(project_id, None)
        routes_adjustments.release_project_write_fence(
            project_id,
            tmp_path,
            token,
        )


def test_multiphase_adjustment_can_continue_until_last_phase(monkeypatch, tmp_path):
    project_id = "adjustment-multiphase"
    ctx = _context(tmp_path, project_id)
    (tmp_path / "one.py").write_text("old one\n", encoding="utf-8")
    (tmp_path / "two.py").write_text("old two\n", encoding="utf-8")
    adjustment = _adjustment(tasks=[
        {
            "task_id": "one", "status": "pending", "depends_on": [],
            "files": ["one.py"], "deliverables": ["one.py"],
        },
        {
            "task_id": "two", "status": "pending", "depends_on": ["one"],
            "files": ["two.py"], "deliverables": ["two.py"],
        },
    ])
    adjustment["phases"] = [
        {"phase_index": 0, "tasks": ["one"], "status": "pending"},
        {"phase_index": 1, "tasks": ["two"], "status": "pending"},
    ]
    adjustments = [adjustment]

    async def persist():
        return None

    async def execute(_ctx, _adj, task, _context, guard, contract):
        with guard.write_guard():
            (tmp_path / contract["files"][0]).write_text(
                f"changed {task['task_id']}\n", encoding="utf-8"
            )
        task["status"] = "done"
        return {"success": True, "output_files": contract["files"]}

    monkeypatch.setitem(routes_adjustments.projects, project_id, ctx)
    monkeypatch.setattr(routes_adjustments, "_get_project", lambda _pid: ctx)
    monkeypatch.setattr(routes_adjustments, "_get_adjustments", lambda _pid: adjustments)
    monkeypatch.setattr(routes_adjustments, "_persist_all_async", persist)
    monkeypatch.setattr(routes_adjustments, "_run_adjustment_task", execute)

    async def scenario():
        await routes_adjustments._start_adjustment_run(
            project_id, "adj-1", mode="phase", phase_index=0,
            request_digest="phase-one",
        )
        await routes_adjustments._adjustment_tasks[project_id]
        first_status = adjustment["active_run"]["status"]
        await routes_adjustments._start_adjustment_run(
            project_id, "adj-1", mode="phase", phase_index=1,
            request_digest="phase-two",
        )
        await routes_adjustments._adjustment_tasks[project_id]
        return first_status

    first_status = asyncio.run(scenario())
    assert first_status == "phase_done"
    assert adjustment["phases"][0]["status"] == "done"
    assert adjustment["phases"][1]["status"] == "done"
    assert adjustment["status"] == "awaiting_final_qa"


def test_confirmed_modifications_are_consumed_by_immutable_contract(
    monkeypatch, tmp_path
):
    project_id = "adjustment-modifications"
    ctx = _context(tmp_path, project_id)
    (tmp_path / "app.py").write_text("old\n", encoding="utf-8")
    adjustment = _adjustment(tasks=[
        {"task_id": "a", "status": "pending", "depends_on": []}
    ])
    adjustments = [adjustment]
    observed = {}

    async def persist():
        return None

    async def execute(_ctx, adj, task, context, guard, contract):
        observed["modifications"] = adj["active_run"]["execution_contract"][
            "modifications"
        ]
        observed["contract"] = contract
        with guard.write_guard():
            (tmp_path / "app.py").write_text("modified\n", encoding="utf-8")
        task["status"] = "done"
        return {"success": True, "output_files": ["app.py"]}

    monkeypatch.setitem(routes_adjustments.projects, project_id, ctx)
    monkeypatch.setattr(routes_adjustments, "_get_project", lambda _pid: ctx)
    monkeypatch.setattr(routes_adjustments, "_get_adjustments", lambda _pid: adjustments)
    monkeypatch.setattr(routes_adjustments, "_persist_all_async", persist)
    monkeypatch.setattr(routes_adjustments, "_run_adjustment_task", execute)

    async def scenario():
        await routes_adjustments._start_adjustment_run(
            project_id, "adj-1", mode="full", phase_index=None,
            request_digest="mods", modifications="Preserve HTTP 403 semantics",
        )
        await routes_adjustments._adjustment_tasks[project_id]

    asyncio.run(scenario())
    assert observed["modifications"] == "Preserve HTTP 403 semantics"
    assert (
        adjustment["adjustment_acceptance"]["modifications"]
        == "Preserve HTTP 403 semantics"
    )
    routes_adjustments._validate_adjustment_contract(
        adjustment["active_run"]["execution_contract"]
    )


def test_empty_file_scope_and_noop_adjustment_fail_closed(monkeypatch, tmp_path):
    project_id = "adjustment-empty-noop"
    ctx = _context(tmp_path, project_id)
    (tmp_path / "app.py").write_text("old\n", encoding="utf-8")
    empty = _adjustment(tasks=[
        {
            "task_id": "empty", "status": "pending", "depends_on": [],
            "files": [], "deliverables": [],
        }
    ])
    adjustments = [empty]

    async def persist():
        return None

    monkeypatch.setitem(routes_adjustments.projects, project_id, ctx)
    monkeypatch.setattr(routes_adjustments, "_get_project", lambda _pid: ctx)
    monkeypatch.setattr(routes_adjustments, "_get_adjustments", lambda _pid: adjustments)
    monkeypatch.setattr(routes_adjustments, "_persist_all_async", persist)
    with pytest.raises(HTTPException, match="explicit files"):
        asyncio.run(routes_adjustments._start_adjustment_run(
            project_id, "adj-1", mode="full", phase_index=None,
            request_digest="empty",
        ))

    noop = _adjustment(tasks=[
        {"task_id": "noop", "status": "pending", "depends_on": []}
    ])
    adjustments[0] = noop

    async def no_change(_ctx, _adj, task, _context, _guard, _contract):
        task["status"] = "done"
        return {"success": True, "output_files": []}

    monkeypatch.setattr(routes_adjustments, "_run_adjustment_task", no_change)

    async def scenario():
        await routes_adjustments._start_adjustment_run(
            project_id, "adj-1", mode="full", phase_index=None,
            request_digest="noop",
        )
        await routes_adjustments._adjustment_tasks[project_id]

    asyncio.run(scenario())
    assert noop["status"] == "needs_manual"
    assert "no verified delivery-file delta" in noop["failure"]["message"].lower() or (
        noop["failure"]["type"] == "RuntimeError"
    )
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "old\n"


def test_orphaned_adjustment_generation_can_be_revoked_and_restored(
    monkeypatch, tmp_path
):
    project_id = "adjustment-restart"
    ctx = _context(tmp_path, project_id)
    (tmp_path / "app.py").write_text("baseline\n", encoding="utf-8")
    adjustment = _adjustment(tasks=[
        {"task_id": "a", "status": "executing", "depends_on": []}
    ])
    adjustment["pre_run_snapshot"] = routes_adjustments._take_adjustment_snapshot(ctx)
    (tmp_path / "app.py").write_text("half-written\n", encoding="utf-8")
    fence = routes_adjustments.acquire_project_write_fence(
        project_id, tmp_path, owner="adjustment:adj-1", purpose="adjustment"
    )
    adjustment["active_run"] = {
        "run_id": "orphaned", "status": "executing",
        "fence_lock_id": fence["lock_id"],
    }
    adjustment["status"] = "executing"
    adjustments = [adjustment]

    async def persist():
        return None

    monkeypatch.setattr(routes_adjustments, "_get_project", lambda _pid: ctx)
    monkeypatch.setattr(routes_adjustments, "_get_adjustments", lambda _pid: adjustments)
    monkeypatch.setattr(routes_adjustments, "_persist_all_async", persist)
    result = asyncio.run(
        routes_adjustments.recover_adjustment_run(project_id, "adj-1")
    )
    assert result["status"] == "pending_confirm"
    assert adjustment["active_run"]["status"] == "revoked"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "baseline\n"
    assert get_project_write_fence(project_id, tmp_path) is None


def test_new_adjustment_is_blocked_while_another_awaits_final_qa(
    monkeypatch, tmp_path
):
    project_id = "adjustment-awaiting-conflict"
    ctx = _context(tmp_path, project_id)
    first = _adjustment(tasks=[])
    first["id"] = "adj-first"
    first["status"] = "awaiting_final_qa"
    second = _adjustment(tasks=[
        {"task_id": "b", "status": "pending", "depends_on": []}
    ])
    second["id"] = "adj-second"
    adjustments = [first, second]
    monkeypatch.setattr(routes_adjustments, "_get_project", lambda _pid: ctx)
    monkeypatch.setattr(routes_adjustments, "_get_adjustments", lambda _pid: adjustments)

    with pytest.raises(HTTPException, match="not terminal"):
        asyncio.run(routes_adjustments._start_adjustment_run(
            project_id, "adj-second", mode="full", phase_index=None,
            request_digest="second",
        ))


def test_corrupt_adjustment_snapshot_is_rejected_before_workspace_mutation(tmp_path):
    ctx = _context(tmp_path)
    (tmp_path / "app.py").write_text("current\n", encoding="utf-8")
    snapshot = routes_adjustments._take_adjustment_snapshot(ctx)
    snapshot["manifest"]["artifact_sha256"] = "0" * 64
    before = compute_delivery_manifest(tmp_path)["artifact_sha256"]

    with pytest.raises(ValueError, match="manifest identity"):
        routes_adjustments._restore_adjustment_snapshot(ctx, snapshot)

    assert compute_delivery_manifest(tmp_path)["artifact_sha256"] == before
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "current\n"


def test_final_qa_full_snapshot_restores_unrelated_changes_and_deletes_new_files(
    tmp_path,
):
    (tmp_path / "a.py").write_text("a-before\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b-before\n", encoding="utf-8")
    snapshot = routes_adjustments._snapshot_final_qa_rework_files(
        tmp_path,
        [{"file_path": "a.py"}],
    )

    (tmp_path / "a.py").write_text("a-after\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("unauthorized\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("new unauthorized\n", encoding="utf-8")
    routes_adjustments._restore_final_qa_rework_snapshot(snapshot)

    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "a-before\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "b-before\n"
    assert not (tmp_path / "c.py").exists()


def test_final_qa_fence_capability_allows_scoped_rework_but_blocks_external_writer(
    tmp_path,
):
    project_id = "final-qa-self-fence"
    (tmp_path / "a.py").write_text("before\n", encoding="utf-8")
    fence = routes_adjustments.acquire_project_write_fence(
        project_id,
        tmp_path,
        owner=f"final-qa:{project_id}",
        purpose="final_qa",
    )
    guard = routes_adjustments._FenceExecutionGuard(
        project_id,
        tmp_path,
        fence["token"],
    )
    try:
        with pytest.raises(routes_adjustments.ProjectWriteFenceConflict):
            with routes_adjustments.project_write_guard(project_id, tmp_path):
                pass
        with guard.write_guard():
            (tmp_path / "a.py").write_text("repaired\n", encoding="utf-8")
        assert (tmp_path / "a.py").read_text(encoding="utf-8") == "repaired\n"
        assert routes_adjustments._final_qa_rework_scope(
            {"allowed_path_prefixes": ["*"]},
            [{"file_path": "a.py"}],
        ) == ["a.py"]
    finally:
        routes_adjustments.release_project_write_fence(
            project_id,
            tmp_path,
            fence["token"],
        )


def test_final_qa_accepts_adjustment_atomically_and_clears_pending_gate(
    monkeypatch, tmp_path
):
    project_id = "adjustment-accepted"
    (tmp_path / "app.py").write_text("accepted\n", encoding="utf-8")
    manifest = compute_delivery_manifest(tmp_path)
    adjustment = _adjustment(tasks=[
        {"task_id": "a", "status": "done", "depends_on": []}
    ])
    _ready_acceptance(adjustment, manifest)
    adjustments = [adjustment]
    monkeypatch.setattr(
        routes_adjustments, "_get_adjustments", lambda _pid: adjustments
    )
    blockers, bindings = routes_adjustments._pending_adjustment_acceptance(
        project_id,
        manifest,
        qa_entry=_authoritative_qa(manifest, adjustment),
        final_qa_status={},
        run_id="qa-run",
        qa_generation="qa-generation",
    )
    assert blockers == []
    routes_adjustments._accept_adjustment_bindings(
        project_id,
        bindings,
        final_qa_run_id="qa-run",
        artifact_sha256=manifest["artifact_sha256"],
    )
    assert adjustment["status"] == "done"
    assert adjustment["active_run"]["status"] == "accepted"
    assert adjustment["requires_final_qa"] is False
    assert adjustment["adjustment_acceptance"]["final_qa_run_id"] == "qa-run"
    assert routes_adjustments._adjustment_is_terminal(adjustment) is True


def test_phase_done_blocks_final_qa_and_another_adjustment(
    monkeypatch, tmp_path
):
    project_id = "adjustment-phase-done-blocker"
    ctx = _context(tmp_path, project_id)
    first = _adjustment(tasks=[
        {"task_id": "a", "status": "done", "depends_on": []}
    ])
    first["id"] = "phase-adjustment"
    first["status"] = "phase_done"
    first["active_run"] = {"status": "phase_done"}
    second = _adjustment(tasks=[
        {"task_id": "b", "status": "pending", "depends_on": []}
    ])
    second["id"] = "next-adjustment"
    adjustments = [first, second]
    monkeypatch.setattr(routes_adjustments, "_get_project", lambda _pid: ctx)
    monkeypatch.setattr(
        routes_adjustments, "_get_adjustments", lambda _pid: adjustments
    )
    assert routes_adjustments._adjustment_final_qa_state_blockers(project_id) == [
        "adjustment phase-adjustment: phase_done",
        "adjustment next-adjustment: pending",
    ]
    with pytest.raises(HTTPException, match="not terminal"):
        asyncio.run(routes_adjustments._start_adjustment_run(
            project_id,
            "next-adjustment",
            mode="full",
            phase_index=None,
            request_digest="next",
        ))


def test_final_qa_rework_creates_superseding_acceptance_generation(
    monkeypatch, tmp_path
):
    project_id = "adjustment-rework-acceptance"
    (tmp_path / "app.py").write_text("before\n", encoding="utf-8")
    before_manifest = compute_delivery_manifest(tmp_path)
    adjustment = _adjustment(tasks=[
        {"task_id": "a", "status": "done", "depends_on": []}
    ])
    _ready_acceptance(adjustment, before_manifest)
    (tmp_path / "app.py").write_text("repaired\n", encoding="utf-8")
    after_manifest = compute_delivery_manifest(tmp_path)
    adjustments = [adjustment]
    rework_evidence = {
        "generation_id": "repair-1",
        "root_acceptance_artifact_sha256": before_manifest["artifact_sha256"],
        "from_artifact_sha256": before_manifest["artifact_sha256"],
        "to_artifact_sha256": after_manifest["artifact_sha256"],
        "authorized_paths": ["app.py"],
        "repair_evidence": [{
            "agent_id": "agent-a",
            "issue_ids": ["defect-a"],
            "output_files": ["app.py"],
            "success": True,
        }],
    }
    monkeypatch.setattr(
        routes_adjustments, "_get_adjustments", lambda _pid: adjustments
    )
    blockers, bindings = routes_adjustments._pending_adjustment_acceptance(
        project_id,
        after_manifest,
        qa_entry=_authoritative_qa(after_manifest, adjustment),
        final_qa_status={
            "adjustment_rework_generations": [rework_evidence]
        },
        run_id="qa-run",
        qa_generation="qa-generation",
    )
    assert blockers == []
    assert bindings[0]["repair_evidence"] == rework_evidence
    assert bindings[0]["artifact_sha256"] == after_manifest["artifact_sha256"]
    assert all(
        result["evidence"]["artifact_sha256"]
        == after_manifest["artifact_sha256"]
        for result in bindings[0]["criterion_results"]
    )


def test_adjustment_criteria_without_authoritative_evidence_cannot_pass(
    monkeypatch, tmp_path
):
    project_id = "adjustment-missing-criterion-evidence"
    (tmp_path / "app.py").write_text("candidate\n", encoding="utf-8")
    manifest = compute_delivery_manifest(tmp_path)
    adjustment = _adjustment(tasks=[
        {"task_id": "a", "status": "done", "depends_on": []}
    ])
    _ready_acceptance(adjustment, manifest)
    monkeypatch.setattr(
        routes_adjustments,
        "_get_adjustments",
        lambda _pid: [adjustment],
    )
    blockers, bindings = routes_adjustments._pending_adjustment_acceptance(
        project_id,
        manifest,
    )
    assert bindings == []
    assert "criterion evidence is missing" in blockers[0]
    with pytest.raises(ValueError, match="criterion evidence"):
        routes_adjustments._accept_adjustment_bindings(
            project_id,
            [{
                "adjustment_id": adjustment["id"],
                "acceptance_criteria": ["criterion"],
                "criterion_results": [],
            }],
            final_qa_run_id="qa-run",
            artifact_sha256=manifest["artifact_sha256"],
        )


def test_final_qa_round_limit_uses_supervisor_authority(monkeypatch):
    monkeypatch.setenv("MAX_FINAL_QC_ROUNDS", "9")
    assert (
        routes_adjustments._final_qa_round_limit()
        == routes_adjustments.MAX_BUSINESS_QA_ROUNDS
        == 6
    )


def test_adjustment_state_persistence_roundtrip_preserves_recovery_and_acceptance(
    monkeypatch, tmp_path
):
    project_id = "adjustment-persistence-roundtrip"
    ctx = _context(tmp_path, project_id)
    monkeypatch.setitem(app_state.projects, project_id, ctx)
    interrupted = _adjustment(tasks=[
        {"task_id": "a", "status": "executing", "depends_on": []}
    ])
    interrupted["status"] = "executing"
    interrupted["pre_run_snapshot"] = {
        "schema_version": 1,
        "manifest": {"artifact_sha256": "before"},
        "files": {"app.py": "YmVmb3Jl"},
    }
    interrupted["active_run"] = {
        "run_id": "run-interrupted",
        "status": "executing",
        "fence_lock_id": "lock-interrupted",
        "execution_contract": {"contract_sha256": "contract"},
    }
    interrupted["adjustment_acceptance"] = {
        "status": "pending_final_qa",
        "acceptance_criteria": ["criterion"],
        "result_artifact_sha256": "candidate",
    }
    awaiting = _adjustment(tasks=[
        {"task_id": "b", "status": "done", "depends_on": []}
    ])
    awaiting["id"] = "adj-awaiting"
    awaiting["status"] = "awaiting_final_qa"
    awaiting["requires_final_qa"] = True
    awaiting["active_run"] = {
        "run_id": "run-awaiting",
        "status": "awaiting_final_qa",
        "fence_lock_id": "released-lock",
    }
    awaiting["adjustment_acceptance"] = {
        "status": "pending_final_qa",
        "result_artifact_sha256": "candidate-awaiting",
        "acceptance_criteria": ["criterion"],
    }
    monkeypatch.setattr(
        routes_engineer,
        "_adjustments",
        {project_id: [interrupted, awaiting]},
    )

    persisted = app_state._persistable_adjustments()
    monkeypatch.setattr(routes_engineer, "_adjustments", {})
    restored_count = app_state._restore_adjustments(persisted)

    assert restored_count == 2
    restored = routes_engineer._adjustments[project_id]
    assert restored[0]["status"] == "recovery_required"
    assert restored[0]["active_run"]["status"] == "interrupted"
    assert restored[0]["active_run"]["run_id"] == "run-interrupted"
    assert restored[0]["active_run"]["fence_lock_id"] == "lock-interrupted"
    assert restored[0]["pre_run_snapshot"]["files"]["app.py"] == "YmVmb3Jl"
    assert restored[0]["adjustment_acceptance"]["acceptance_criteria"] == [
        "criterion"
    ]
    assert restored[1]["status"] == "awaiting_final_qa"
    assert restored[1]["active_run"]["status"] == "awaiting_final_qa"
    assert restored[1]["requires_final_qa"] is True


def test_executed_or_changed_adjustment_cannot_be_cancelled(
    monkeypatch, tmp_path
):
    project_id = "adjustment-cancel-guard"
    (tmp_path / "app.py").write_text("baseline\n", encoding="utf-8")
    ctx = _context(tmp_path, project_id)
    pristine = _adjustment(tasks=[
        {"task_id": "a", "status": "pending", "depends_on": []}
    ])
    pristine["status"] = "pending_confirm"
    pristine["creation_evidence"] = (
        routes_adjustments._adjustment_creation_evidence(ctx)
    )
    adjustments = [pristine]

    async def persist():
        return None

    monkeypatch.setattr(routes_adjustments, "_get_project", lambda _pid: ctx)
    monkeypatch.setattr(
        routes_adjustments, "_get_adjustments", lambda _pid: adjustments
    )
    monkeypatch.setattr(routes_adjustments, "_persist_all_async", persist)

    asyncio.run(routes_adjustments.cancel_adjustment(project_id, pristine["id"]))
    assert pristine["status"] == "cancelled"

    for status in ("phase_done", "awaiting_final_qa", "done"):
        pristine["status"] = status
        with pytest.raises(HTTPException) as refused:
            asyncio.run(routes_adjustments.cancel_adjustment(
                project_id, pristine["id"]
            ))
        assert refused.value.status_code == 409

    pristine["status"] = "pending_confirm"
    pristine["active_run"] = {"status": "revoked", "run_id": "executed-run"}
    with pytest.raises(HTTPException) as executed:
        asyncio.run(routes_adjustments.confirm_adjustment(
            project_id,
            pristine["id"],
            routes_adjustments.AdjustmentConfirmRequest(
                adjustment_id=pristine["id"],
                confirmed=False,
            ),
        ))
    assert executed.value.status_code == 409

    pristine.pop("active_run")
    pristine["creation_evidence"] = (
        routes_adjustments._adjustment_creation_evidence(ctx)
    )
    (tmp_path / "app.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(HTTPException) as changed:
        asyncio.run(routes_adjustments.cancel_adjustment(
            project_id, pristine["id"]
        ))
    assert changed.value.status_code == 409
    assert "workspace changed" in str(changed.value.detail).lower()


def test_final_qa_preregistration_is_durable_and_reclaimed_after_crash(
    monkeypatch, tmp_path
):
    project_id = (
        "final-qa-preregistration-crash-"
        + tmp_path.parent.parent.name
    )
    (tmp_path / "app.py").write_text("candidate\n", encoding="utf-8")
    ctx = _context(tmp_path, project_id)
    ctx.supervisor_quality_runs = {
        "phase-1": {
            "status": "completed",
            "completion_gate": {"passed": True},
        }
    }
    monkeypatch.setattr(routes_adjustments, "projects", {project_id: ctx})
    monkeypatch.setattr(
        routes_adjustments,
        "_get_adjustments",
        lambda _pid: [],
    )
    monkeypatch.setitem(
        routes_adjustments._phase_managers,
        project_id,
        SimpleNamespace(phases=[{
            "phase_id": "phase-1",
            "status": "completed",
            "user_confirmed": True,
        }]),
    )
    persist_calls = []
    monkeypatch.setattr(
        routes_adjustments, "_persist_all", lambda: persist_calls.append(True)
    )

    manifest = compute_delivery_manifest(tmp_path)
    registration = routes_adjustments.register_final_qa_reinspection(
        ctx, manifest["artifact_sha256"]
    )
    durable = ctx.qc_results["__whole_project__"]["qa"][
        "final_qa_registration"
    ]
    old_lock_id = durable["write_fence"]["lock_id"]
    assert persist_calls
    assert durable["registration_id"] == registration["registration_id"]
    assert durable["artifact_digest"] == manifest["artifact_sha256"]
    assert durable["write_fence"]["owner"].startswith(
        f"final-qa:{project_id}:"
    )
    assert "token" not in durable
    assert "token" not in durable["write_fence"]

    routes_adjustments._final_qa_pre_registrations.clear()
    routes_adjustments._final_qa_fence_tokens.clear()
    started = []

    async def persist():
        return None

    async def trigger(pid, reclaimed):
        started.append((pid, reclaimed))
        return {"success": True}

    monkeypatch.setattr(routes_adjustments, "_persist_all_async", persist)
    monkeypatch.setattr(routes_adjustments, "_trigger_final_qa", trigger)
    recovered = asyncio.run(
        routes_adjustments.reconcile_interrupted_final_qa_runs()
    )

    marker = get_project_write_fence(project_id, tmp_path)
    assert recovered == 1
    assert started[0][1]["registration_id"] == registration["registration_id"]
    assert marker["lock_id"] != old_lock_id
    token = routes_adjustments._final_qa_fence_tokens.pop(project_id)
    routes_adjustments.release_project_write_fence(
        project_id, tmp_path, token
    )
    routes_adjustments._final_qa_pre_registrations.pop(project_id, None)
    routes_adjustments._final_qa_status.pop(project_id, None)


def test_final_qa_construction_intent_recovers_acquire_before_identity_persist(
    monkeypatch, tmp_path
):
    project_id = "final-qa-construction-" + tmp_path.parent.parent.name
    (tmp_path / "app.py").write_text("candidate\n", encoding="utf-8")
    ctx = _context(tmp_path, project_id)
    manifest = compute_delivery_manifest(tmp_path)
    generation = "construction-generation"
    owner = f"final-qa:{project_id}:{generation}"
    intent = routes_adjustments._final_qa_construction_record(
        ctx,
        registration_id=generation,
        owner=owner,
        state="acquiring",
        artifact_digest=manifest["artifact_sha256"],
        required_paths=[],
        previous_whole_project_qc=ctx.qc_results.get("__whole_project__"),
    )
    routes_adjustments._persist_final_qa_registration(ctx, intent)
    crashed_fence = routes_adjustments.acquire_project_write_fence(
        project_id,
        tmp_path,
        owner=owner,
        purpose="final_qa",
    )
    assert intent["write_fence"]["lock_id"] == ""
    monkeypatch.setattr(routes_adjustments, "projects", {project_id: ctx})
    started = []

    async def persist():
        return None

    async def trigger(pid, registration):
        started.append((pid, registration))
        return {"success": True}

    monkeypatch.setattr(routes_adjustments, "_persist_all_async", persist)
    monkeypatch.setattr(routes_adjustments, "_trigger_final_qa", trigger)
    recovered = asyncio.run(
        routes_adjustments.reconcile_interrupted_final_qa_runs()
    )

    marker = get_project_write_fence(project_id, tmp_path)
    assert recovered == 1
    assert started
    assert marker["owner"] != crashed_fence["owner"]
    token = routes_adjustments._final_qa_fence_tokens.pop(project_id)
    routes_adjustments.release_project_write_fence(
        project_id, tmp_path, token
    )
    routes_adjustments._final_qa_pre_registrations.pop(project_id, None)
    routes_adjustments._final_qa_status.pop(project_id, None)


@pytest.mark.parametrize("crash_point", ["mid_qc", "mid_rework"])
def test_active_final_qa_crash_restores_and_reclaims_exact_generation(
    monkeypatch, tmp_path, crash_point
):
    project_id = (
        f"final-qa-{crash_point}-" + tmp_path.parent.parent.name
    )
    (tmp_path / "app.py").write_text("before\n", encoding="utf-8")
    ctx = _context(tmp_path, project_id)
    manifest = compute_delivery_manifest(tmp_path)
    old_fence = routes_adjustments.acquire_project_write_fence(
        project_id,
        tmp_path,
        owner=f"final-qa:{project_id}",
        purpose="final_qa",
    )
    run = {
        "run_id": "crashed-run",
        "status": (
            "qc_running_round_1"
            if crash_point == "mid_qc"
            else "rework_running_round_1"
        ),
        "artifact_digest": manifest["artifact_sha256"],
        "qc_artifact_sha256": manifest["artifact_sha256"],
        "required_paths": [],
        "write_fence": routes_adjustments._durable_fence_identity(old_fence),
    }
    if crash_point == "mid_rework":
        snapshot = routes_adjustments._snapshot_final_qa_rework_files(
            tmp_path, [{"file_path": "app.py"}]
        )
        run["rework_snapshot"] = (
            routes_adjustments._serialize_final_qa_rework_snapshot(
                tmp_path, snapshot
            )
        )
        (tmp_path / "app.py").write_text("partial late write\n", encoding="utf-8")
    ctx.qc_results = {
        "__whole_project__": {
            "qa": {
                "passed": False,
                "status": "running",
                "final_qa_run": run,
            }
        }
    }
    monkeypatch.setattr(routes_adjustments, "projects", {project_id: ctx})
    started = []

    async def persist():
        return None

    async def trigger(pid, reclaimed):
        started.append((pid, reclaimed))
        return {"success": True}

    monkeypatch.setattr(routes_adjustments, "_persist_all_async", persist)
    monkeypatch.setattr(routes_adjustments, "_trigger_final_qa", trigger)
    recovered = asyncio.run(
        routes_adjustments.reconcile_interrupted_final_qa_runs()
    )

    marker = get_project_write_fence(project_id, tmp_path)
    assert recovered == 1
    assert marker["lock_id"] != old_fence["lock_id"]
    assert started[0][1]["artifact_digest"] == manifest["artifact_sha256"]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "before\n"
    token = routes_adjustments._final_qa_fence_tokens.pop(project_id)
    routes_adjustments.release_project_write_fence(
        project_id, tmp_path, token
    )
    routes_adjustments._final_qa_pre_registrations.pop(project_id, None)
    routes_adjustments._final_qa_status.pop(project_id, None)


def test_final_qa_state_uses_current_adjustment_receipt_not_historical_agents(
    monkeypatch, tmp_path
):
    project_id = "adjustment-receipt-gate"
    (tmp_path / "app.py").write_text("candidate\n", encoding="utf-8")
    ctx = _context(tmp_path, project_id)
    ctx.agents = {
        "old-failed": {"status": "failed", "source": "adjustment"},
        "old-revoked": {"status": "revoked", "source": "adjustment"},
    }
    adjustment = _adjustment(tasks=[
        {"task_id": "a", "status": "done", "depends_on": []}
    ])
    manifest = compute_delivery_manifest(tmp_path)
    contract = routes_adjustments._build_adjustment_execution_contract(
        adjustment,
        adjustment["tasks"],
        mode="full",
        phase_index=None,
        request_digest="receipt-run",
        modifications="",
        input_manifest=manifest,
    )
    adjustment["status"] = "awaiting_final_qa"
    adjustment["active_run"] = {
        "run_id": "current-run",
        "status": "awaiting_final_qa",
        "execution_contract": contract,
        "task_receipts": {},
    }
    monkeypatch.setattr(
        routes_adjustments,
        "_get_adjustments",
        lambda _pid: [adjustment],
    )
    routes_adjustments._record_adjustment_task_receipt(
        ctx,
        adjustment,
        adjustment["tasks"][0],
        contract["tasks"][0],
        {"success": True, "output_files": ["app.py"]},
        run_id="current-run",
    )

    assert routes_adjustments._final_qa_state_blockers(ctx) == []
    adjustment["active_run"]["task_receipts"].clear()
    adjustment["tasks"][0]["status"] = "completed"
    blockers = routes_adjustments._final_qa_state_blockers(ctx)
    assert "missing authoritative success receipt" in blockers[0]


def test_adjustment_criteria_require_independent_exact_evidence(
    monkeypatch, tmp_path
):
    project_id = "adjustment-independent-criteria"
    (tmp_path / "app.py").write_text("candidate\n", encoding="utf-8")
    manifest = compute_delivery_manifest(tmp_path)
    adjustment = _adjustment(tasks=[{
        "task_id": "a",
        "status": "done",
        "depends_on": [],
        "acceptance_criteria": [
            {
                "text": "API returns HTTP 200",
                "required_evidence_type": "api",
            },
            {
                "text": "Behavior matches the user contract",
                "required_evidence_type": "semantic",
            },
        ],
    }])
    _ready_acceptance(adjustment, manifest)
    monkeypatch.setattr(
        routes_adjustments,
        "_get_adjustments",
        lambda _pid: [adjustment],
    )
    complete = _authoritative_qa(manifest, adjustment)
    one_only = {**complete, "criterion_evidence": complete["criterion_evidence"][:1]}
    blockers, bindings = routes_adjustments._pending_adjustment_acceptance(
        project_id,
        manifest,
        qa_entry=one_only,
        run_id="qa-run",
        qa_generation="qa-generation",
    )
    assert bindings == []
    assert "criterion evidence is missing" in blockers[0]

    wrong = {
        **complete,
        "criterion_evidence": [
            dict(item) for item in complete["criterion_evidence"]
        ],
    }
    wrong["criterion_evidence"][1]["artifact_sha256"] = "wrong-artifact"
    wrong["criterion_evidence"][1]["criterion_id"] = (
        wrong["criterion_evidence"][0]["criterion_id"]
    )
    blockers, bindings = routes_adjustments._pending_adjustment_acceptance(
        project_id,
        manifest,
        qa_entry=wrong,
        run_id="qa-run",
        qa_generation="qa-generation",
    )
    assert bindings == []
    assert "criterion evidence is missing" in blockers[0]

    blockers, bindings = routes_adjustments._pending_adjustment_acceptance(
        project_id,
        manifest,
        qa_entry=complete,
        run_id="qa-run",
        qa_generation="qa-generation",
    )
    assert blockers == []
    assert len(bindings[0]["criterion_results"]) == 2
    routes_adjustments._accept_adjustment_bindings(
        project_id,
        bindings,
        final_qa_run_id="qa-run",
        artifact_sha256=manifest["artifact_sha256"],
    )
    assert adjustment["status"] == "done"


def test_lifespan_restores_projects_before_resuming_pending_runs(monkeypatch):
    from core import auth, database
    from core.execution_runs import DurableRunRegistry
    from api import routes_execution

    events = []
    project_id = "persisted-project-with-pending-run"
    monkeypatch.setattr(database, "init_db", lambda: None)
    monkeypatch.setattr(database, "kv_get", lambda key, default=None: {"seeded": True})
    monkeypatch.setattr(
        DurableRunRegistry, "recover_startup", lambda self: []
    )

    def restore():
        events.append("restore")
        app_state.projects[project_id] = SimpleNamespace(project_id=project_id)

    async def reconcile():
        events.append("final_qa_reconcile")
        assert project_id in app_state.projects
        return 0

    async def resume():
        events.append("resume_pending")
        assert project_id in app_state.projects
        return 1

    async def resume_agents():
        events.append("resume_agents")
        return 0

    async def persist():
        return None

    monkeypatch.setattr(app_state, "_restore_from_disk", restore)
    monkeypatch.setattr(
        routes_adjustments,
        "reconcile_interrupted_final_qa_runs",
        reconcile,
    )
    monkeypatch.setattr(
        routes_execution, "resume_pending_execution_runs", resume
    )
    monkeypatch.setattr(
        app_state, "_resume_interrupted_execution_tasks", resume_agents
    )
    monkeypatch.setattr(app_state, "_persist_all_async", persist)
    monkeypatch.setattr(auth, "ensure_admin_user", lambda: None)
    monkeypatch.setattr(auth, "repair_user_indexes", lambda: None)
    monkeypatch.setattr(auth, "cleanup_unverified_accounts", lambda: None)

    async def scenario():
        try:
            async with app_state.lifespan(app_state.app):
                assert events[:3] == [
                    "restore",
                    "final_qa_reconcile",
                    "resume_pending",
                ]
        finally:
            app_state.projects.pop(project_id, None)

    asyncio.run(scenario())


def test_concurrent_final_qa_posts_create_one_task_and_generation(
    monkeypatch, tmp_path
):
    project_id = "final-qa-double-post-" + tmp_path.parent.parent.name
    (tmp_path / "app.py").write_text("candidate\n", encoding="utf-8")
    ctx = _context(tmp_path, project_id)
    ctx.name = "Concurrent QA"
    ctx.supervisor_quality_runs = {
        "phase-1": {
            "status": "completed",
            "completion_gate": {"passed": True},
        }
    }
    phase_manager = SimpleNamespace(phases=[{
        "phase_id": "phase-1",
        "status": "completed",
        "user_confirmed": True,
    }])
    monkeypatch.setattr(routes_adjustments, "_get_project", lambda _pid: ctx)
    monkeypatch.setattr(routes_adjustments, "projects", {project_id: ctx})
    monkeypatch.setattr(
        routes_adjustments, "_get_adjustments", lambda _pid: []
    )
    monkeypatch.setitem(
        routes_adjustments._phase_managers, project_id, phase_manager
    )
    monkeypatch.setattr(
        routes_adjustments, "_reset_final_qa_fix_budget", lambda _ctx: None
    )
    routes_adjustments._final_qa_status.pop(project_id, None)
    task_started = []

    async def persist():
        return None

    async def scenario():
        release_worker = asyncio.Event()

        async def worker(pid):
            task_started.append(pid)
            await release_worker.wait()

        monkeypatch.setattr(
            routes_adjustments, "_run_final_qa_loop", worker
        )
        results = await asyncio.gather(
            routes_adjustments.trigger_final_qa(project_id),
            routes_adjustments.trigger_final_qa(project_id),
        )
        task = routes_adjustments._final_qa_tasks[project_id]
        marker = get_project_write_fence(project_id, tmp_path)
        persisted_run = ctx.qc_results["__whole_project__"]["qa"][
            "final_qa_run"
        ]
        assert len(task_started) == 1
        assert sum(bool(item.get("already_running")) for item in results) == 1
        assert marker["lock_id"] == persisted_run["write_fence"]["lock_id"]
        assert persisted_run["registration_id"]
        assert (
            "final_qa_registration"
            not in ctx.qc_results["__whole_project__"]["qa"]
        )
        release_worker.set()
        await task

    monkeypatch.setattr(routes_adjustments, "_persist_all_async", persist)
    asyncio.run(scenario())
    routes_adjustments._final_qa_status.pop(project_id, None)
    routes_adjustments._final_qa_tasks.pop(project_id, None)
    routes_adjustments._final_qa_start_mutexes.pop(project_id, None)


def test_cancelling_preregistration_crash_is_released_on_startup(
    monkeypatch, tmp_path
):
    project_id = "final-qa-cancelling-" + tmp_path.parent.parent.name
    (tmp_path / "app.py").write_text("candidate\n", encoding="utf-8")
    ctx = _context(tmp_path, project_id)
    previous_qc = {"qa": {"passed": False, "status": "failed"}}
    ctx.qc_results["__whole_project__"] = copy.deepcopy(previous_qc)
    generation = "cancel-generation"
    owner = f"final-qa:{project_id}:{generation}"
    fence = routes_adjustments.acquire_project_write_fence(
        project_id, tmp_path, owner=owner, purpose="final_qa"
    )
    record = routes_adjustments._final_qa_construction_record(
        ctx,
        registration_id=generation,
        owner=owner,
        state="cancelling",
        artifact_digest=compute_delivery_manifest(tmp_path)[
            "artifact_sha256"
        ],
        required_paths=[],
        previous_whole_project_qc=previous_qc,
    )
    record["write_fence"] = routes_adjustments._durable_fence_identity(
        fence
    )
    routes_adjustments._persist_final_qa_registration(ctx, record)
    monkeypatch.setattr(routes_adjustments, "projects", {project_id: ctx})
    triggered = []

    async def persist():
        return None

    async def trigger(*args):
        triggered.append(args)

    monkeypatch.setattr(routes_adjustments, "_persist_all_async", persist)
    monkeypatch.setattr(routes_adjustments, "_trigger_final_qa", trigger)
    recovered = asyncio.run(
        routes_adjustments.reconcile_interrupted_final_qa_runs()
    )

    assert recovered == 1
    assert triggered == []
    assert get_project_write_fence(project_id, tmp_path) is None
    assert ctx.qc_results["__whole_project__"] == previous_qc


def test_final_qa_acquire_failure_compare_deletes_only_its_registration(
    monkeypatch, tmp_path
):
    project_id = "final-qa-cas-loser-" + tmp_path.parent.parent.name
    (tmp_path / "app.py").write_text("candidate\n", encoding="utf-8")
    ctx = _context(tmp_path, project_id)
    ctx.supervisor_quality_runs = {
        "phase-1": {
            "status": "completed",
            "completion_gate": {"passed": True},
        }
    }
    monkeypatch.setattr(routes_adjustments, "_get_project", lambda _pid: ctx)
    monkeypatch.setattr(
        routes_adjustments, "_get_adjustments", lambda _pid: []
    )
    monkeypatch.setitem(
        routes_adjustments._phase_managers,
        project_id,
        SimpleNamespace(phases=[{
            "phase_id": "phase-1",
            "status": "completed",
            "user_confirmed": True,
        }]),
    )
    monkeypatch.setattr(routes_adjustments, "_persist_all", lambda: None)
    winner_id = "winner-generation"

    def lose_acquire(*_args, **_kwargs):
        winner = {
            "registration_id": winner_id,
            "project_id": project_id,
            "state": "acquiring",
        }
        routes_adjustments._persist_final_qa_registration(ctx, winner)
        raise routes_adjustments.ProjectWriteFenceConflict("lost")

    monkeypatch.setattr(
        routes_adjustments, "acquire_project_write_fence", lose_acquire
    )

    with pytest.raises(HTTPException) as conflict:
        asyncio.run(routes_adjustments.trigger_final_qa(project_id))

    assert conflict.value.status_code == 409
    durable = ctx.qc_results["__whole_project__"]["qa"][
        "final_qa_registration"
    ]
    assert durable["registration_id"] == winner_id
    routes_adjustments._final_qa_start_mutexes.pop(project_id, None)


def test_final_qa_recovery_keeps_unavailable_project_retryable(
    monkeypatch, tmp_path
):
    project_id = "final-qa-unavailable-" + tmp_path.parent.parent.name
    unavailable = tmp_path / "offline-workspace"
    ctx = _context(unavailable, project_id)
    generation = "offline-generation"
    record = routes_adjustments._final_qa_construction_record(
        ctx,
        registration_id=generation,
        owner=f"final-qa:{project_id}:{generation}",
        state="acquiring",
        artifact_digest="artifact",
        required_paths=[],
    )
    routes_adjustments._persist_final_qa_registration(ctx, record)
    monkeypatch.setattr(routes_adjustments, "projects", {project_id: ctx})

    async def persist():
        return None

    monkeypatch.setattr(routes_adjustments, "_persist_all_async", persist)

    recovered = asyncio.run(
        routes_adjustments.reconcile_interrupted_final_qa_runs()
    )

    durable = ctx.qc_results["__whole_project__"]["qa"][
        "final_qa_registration"
    ]
    assert recovered == 0
    assert durable["registration_id"] == generation
    assert durable["recovery_retryable"] is True
    assert (
        routes_adjustments._final_qa_status[project_id]["status"]
        == "recovery_required"
    )
    assert routes_adjustments._final_qa_status[project_id]["retryable"] is True
    routes_adjustments._final_qa_status.pop(project_id, None)
