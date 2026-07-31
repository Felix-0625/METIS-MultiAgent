import asyncio
import concurrent.futures
import hashlib
import time
from types import SimpleNamespace

from api import routes_execution, routes_phases
from core import app_state
from core import database, expert_lock
from core.supervisor_quality_state import SupervisorQualityMachine


def test_startup_quality_recovery_isolates_invalid_project(monkeypatch):
    broken = SimpleNamespace(project_id="broken")
    healthy = SimpleNamespace(project_id="healthy")
    recovered = []

    async def no_phase_dispatches():
        return 0

    async def recover_quality(ctx):
        if ctx is broken:
            raise RuntimeError("invalid persisted evidence")
        recovered.append(ctx.project_id)

    monkeypatch.setattr(routes_execution, "projects", {
        broken.project_id: broken,
        healthy.project_id: healthy,
    })
    monkeypatch.setattr(
        routes_phases, "resume_pending_phase_dispatches", no_phase_dispatches,
    )
    monkeypatch.setattr(
        routes_execution._run_registry, "list_runs", lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        routes_execution, "_start_phase_quality_cycle_if_ready", recover_quality,
    )

    resumed = asyncio.run(routes_execution.resume_pending_execution_runs())

    assert resumed == 0
    assert recovered == ["healthy"]


def test_async_persistence_coalesces_without_dropping_latest_revision(monkeypatch):
    writes = []
    monkeypatch.setattr(app_state, "_PERSIST_DEBOUNCE_SEC", 0.01)
    monkeypatch.setattr(app_state, "_last_persist_time", time.monotonic())
    monkeypatch.setattr(app_state, "_persist_revision", 0)
    monkeypatch.setattr(app_state, "_persisted_revision", 0)
    monkeypatch.setattr(app_state, "_do_persist", lambda: writes.append(time.monotonic()))

    async def run():
        await asyncio.gather(
            app_state._persist_all_async(),
            app_state._persist_all_async(),
            app_state._persist_all_async(),
        )

    asyncio.run(run())

    assert writes
    assert app_state._persisted_revision == app_state._persist_revision == 3


def test_sync_shutdown_persistence_is_never_suppressed(monkeypatch):
    writes = []
    monkeypatch.setattr(app_state, "_last_persist_time", time.monotonic())
    monkeypatch.setattr(app_state, "_persist_revision", 0)
    monkeypatch.setattr(app_state, "_persisted_revision", 0)
    monkeypatch.setattr(app_state, "_do_persist", lambda: writes.append(True))

    app_state._persist_all()
    app_state._persist_all()

    assert writes == [True, True]
    assert app_state._persisted_revision == 2


def test_running_quality_loop_restores_as_explicit_interruption(monkeypatch):
    key = "proj-recovery-phase-1"
    monkeypatch.setattr(app_state, "projects", {"proj-recovery": object()})
    routes_phases._auto_repair_states.pop(key, None)

    restored = app_state._restore_auto_repair_states({
        key: {"running": True, "status": "repairing", "messages": []},
        "deleted-project-phase-1": {"running": True},
    })

    state = routes_phases._auto_repair_states.pop(key)
    assert restored == 1
    assert state["running"] is False
    assert state["status"] == "interrupted"
    assert state["interrupted_from_status"] == "repairing"
    assert state["action_required"]["options"] == [
        "manual_edit", "retry_cycle", "rebuild_phase",
    ]


def _interrupted_pre_qa_context(project_id, phase_id, tmp_path):
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        agents={
            "agent-1": {
                "id": "agent-1",
                "phase_id": phase_id,
                "status": "completed",
                "progress": 100,
            },
        },
        supervisor_quality_runs={},
    )
    machine = SupervisorQualityMachine(phase_id)
    scope = routes_phases._supervisor_scope_snapshot(ctx, phase_id)
    machine.start_run(
        scope=scope,
        idempotency_key=f"{project_id}:{phase_id}:scope-digest",
        dependencies_ready=True,
        agents=[{"agent_id": "agent-1", "status": "succeeded"}],
        required_evidence_kinds=["scope", "qa"],
        required_pre_qa_evidence_kinds=["scope", "pre_qa"],
    )
    machine.engineer_completed(
        commit="artifact:" + str(
            scope.get("artifact_digest") or scope.get("workspace_digest") or ""
        ),
    )
    machine.start_verification()
    machine.record_evidence(
        kind="scope",
        command="lock phase scope",
        exit_code=0,
        passed=True,
        log="scope locked",
        step_id="scope-step",
    )
    ctx.supervisor_quality_runs[phase_id] = machine.to_dict()
    return ctx


def _mock_pre_qa_recovery_claim(monkeypatch):
    claims = []

    def claim(expert_id, project_id, task_id, file_scope, ttl_seconds):
        claims.append((expert_id, project_id, task_id, file_scope, ttl_seconds))
        return {
            "success": True,
            "lock_id": f"lock:{expert_id}:{project_id}:{task_id}",
            "leased_until": time.time() + ttl_seconds,
        }

    monkeypatch.setattr(routes_phases.expert_lock, "atomic_claim_lock", claim)
    return claims


def test_explicit_resume_restarts_safe_interrupted_pre_qa_exactly_once(
    monkeypatch, tmp_path,
):
    project_id, phase_id = "proj-preqa-restart", "phase-1"
    key = f"{project_id}-{phase_id}"
    ctx = _interrupted_pre_qa_context(project_id, phase_id, tmp_path)
    original_run_id = ctx.supervisor_quality_runs[phase_id]["run_id"]
    scheduled = []

    def capture_task(coro, *, name):
        scheduled.append(name)
        coro.close()

    async def no_persist():
        return None

    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    routes_phases._auto_repair_states.pop(key, None)
    assert app_state._restore_auto_repair_states({
        key: {
            "running": True,
            "status": "pre_qa_verifying",
            "needs_manual": False,
            "messages": [],
        },
    }) == 1
    state = routes_phases._auto_repair_states[key]
    monkeypatch.setattr(routes_phases, "_safe_create_task", capture_task)
    monkeypatch.setattr(routes_phases, "_persist_all_async", no_persist)
    claims = _mock_pre_qa_recovery_claim(monkeypatch)
    try:
        polled = asyncio.run(
            routes_phases.get_auto_repair_status(project_id, phase_id)
        )
        first = asyncio.run(
            routes_phases.resume_auto_repair(project_id, phase_id)
        )
        second = asyncio.run(
            routes_phases.resume_auto_repair(project_id, phase_id)
        )
        assert polled["status"] == "interrupted"
        assert polled["running"] is False
        assert polled["auto_recovery_eligible"] is True
        assert polled["recovery_blockers"] == []
        assert first["scheduled"] is True
        assert first["status"]["status"] == "pre_qa_verifying"
        assert second["scheduled"] is False
        assert second["already_claimed"] is True
        assert scheduled == [f"pre-qa-restart-recovery-{phase_id}"]
        assert len(claims) == 1
        assert ctx.supervisor_quality_runs[phase_id]["run_id"] == original_run_id
        assert ctx.supervisor_quality_runs[phase_id]["business_rounds_used"] == 0
    finally:
        routes_phases.projects.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)


def test_interrupted_pre_qa_with_partial_result_remains_fail_closed(
    monkeypatch, tmp_path,
):
    project_id, phase_id = "proj-preqa-partial", "phase-1"
    key = f"{project_id}-{phase_id}"
    ctx = _interrupted_pre_qa_context(project_id, phase_id, tmp_path)
    state = {
        "running": False,
        "status": "interrupted",
        "interrupted_from_status": "pre_qa_verifying",
        "needs_manual": True,
        "messages": [],
        "pre_qa_result": {"passed": True, "evidence": []},
        "action_required": {"options": ["retry_cycle"]},
    }
    scheduled = []

    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._auto_repair_states, key, state)
    monkeypatch.setattr(
        routes_phases, "_safe_create_task",
        lambda *_args, **_kwargs: scheduled.append(True),
    )
    try:
        result = asyncio.run(
            routes_phases.get_auto_repair_status(project_id, phase_id)
        )
        assert result["status"] == "interrupted"
        assert result["needs_manual"] is True
        assert result["auto_recovery_eligible"] is False
        assert result["recovery_blockers"] == [
            "pre_qa_result_not_terminal_failed",
        ]
        assert scheduled == []
    finally:
        routes_phases.projects.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)


def test_legacy_restart_message_without_repair_evidence_is_not_sufficient(
    monkeypatch, tmp_path,
):
    project_id, phase_id = "proj-preqa-legacy-ambiguous", "phase-1"
    key = f"{project_id}-{phase_id}"
    ctx = _interrupted_pre_qa_context(project_id, phase_id, tmp_path)
    state = {
        "running": False,
        "status": "interrupted",
        "needs_manual": True,
        "messages": [],
        "action_required": {
            "message": "The quality loop was interrupted by a service restart.",
            "options": ["manual_edit", "retry_cycle", "rebuild_phase"],
        },
    }
    claims = []
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._auto_repair_states, key, state)
    monkeypatch.setattr(
        routes_phases.expert_lock, "atomic_claim_lock",
        lambda *_args, **_kwargs: claims.append(True),
    )
    try:
        result = asyncio.run(
            routes_phases.get_auto_repair_status(project_id, phase_id)
        )
        assert result["status"] == "interrupted"
        assert result["recovery_blockers"] == [
            "legacy_interruption_evidence_insufficient",
        ]
        assert claims == []
    finally:
        routes_phases.projects.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)


def test_legacy_interrupted_pre_qa_resumes_after_verified_deterministic_repairs(
    monkeypatch, tmp_path,
):
    project_id, phase_id = "proj-preqa-legacy-repair", "phase-1"
    key = f"{project_id}-{phase_id}"
    repaired = tmp_path / "backend" / "src" / "auth.js"
    repaired.parent.mkdir(parents=True)
    repaired.write_text("export const secure = true;\n", encoding="utf-8")
    ctx = _interrupted_pre_qa_context(project_id, phase_id, tmp_path)
    machine = SupervisorQualityMachine.from_dict(
        ctx.supervisor_quality_runs[phase_id]
    )
    digest = hashlib.sha256(repaired.read_bytes()).hexdigest()
    intermediate_digest = hashlib.sha256(
        b"export const secure = process.env.JWT_SECRET;\n"
    ).hexdigest()
    failed_gate_evidence = {
        "kind": "pre_qa",
        "gate_id": "jwt-gate",
        "command": "npm test",
        "exit_code": 1,
        "passed": False,
        "log_excerpt": "jwt default secret detected",
        "log_digest": hashlib.sha256(
            b"jwt default secret detected"
        ).hexdigest(),
    }
    machine.record_evidence(
        kind="pre_qa",
        command=failed_gate_evidence["command"],
        exit_code=failed_gate_evidence["exit_code"],
        passed=failed_gate_evidence["passed"],
        log=failed_gate_evidence["log_excerpt"],
        step_id=routes_phases._pre_qa_evidence_step_id(
            failed_gate_evidence
        ),
        metadata={"scope_digest": machine.to_dict()["scope"]["scope_digest"]},
    )
    machine.record_evidence(
        kind="deterministic_patch",
        command="deterministic-pre-qa-repair backend/src/auth.js",
        exit_code=0,
        passed=True,
        log="Applied deterministic repair for jwt_default_secret",
        step_id=f"pre-qa-repair:backend/src/auth.js:{intermediate_digest}",
        metadata={
            "kind": "deterministic_patch",
            "path": "backend/src/auth.js",
            "issue_code": "jwt_default_secret",
            "before_digest": "0" * 64,
            "after_digest": intermediate_digest,
        },
    )
    machine.record_evidence(
        kind="deterministic_patch",
        command="deterministic-pre-qa-repair backend/src/auth.js",
        exit_code=0,
        passed=True,
        log="Applied deterministic repair for jwt_not_fail_closed",
        step_id=f"pre-qa-repair:backend/src/auth.js:{digest}",
        metadata={
            "kind": "deterministic_patch",
            "path": "backend/src/auth.js",
            "issue_code": "jwt_not_fail_closed",
            "before_digest": intermediate_digest,
            "after_digest": digest,
        },
    )
    ctx.supervisor_quality_runs[phase_id] = machine.to_dict()
    original_run_id = machine.to_dict()["run_id"]
    state = {
        "running": False,
        "status": "interrupted",
        "needs_manual": True,
        "messages": [],
        "pre_qa_result": {
            "passed": False,
            "status": "pre_qa_failed",
            "failure_category": "pre_qa_failed",
            "failed_gate": "jwt-gate",
            "issues": [{
                "code": "jwt_default_secret",
                "message": "JWT secret must fail closed",
                "gate": "jwt-gate",
            }],
            "evidence": [failed_gate_evidence],
        },
        "repair_batch": None,
        "action_required": {
            "message": "The quality loop was interrupted by a service restart.",
            "options": ["manual_edit", "retry_cycle", "rebuild_phase"],
        },
    }
    saved_result_evidence = state["pre_qa_result"]["evidence"]
    state["pre_qa_result"]["evidence"] = []
    assert routes_phases._interrupted_pre_qa_recovery_blockers(
        ctx, phase_id, state,
    ) == ["pre_qa_result_incomplete"]
    state["pre_qa_result"]["evidence"] = [{
        **saved_result_evidence[0],
        "log_digest": "f" * 64,
    }]
    assert routes_phases._interrupted_pre_qa_recovery_blockers(
        ctx, phase_id, state,
    ) == ["pre_qa_result_evidence_mismatch"]
    state["pre_qa_result"]["evidence"] = saved_result_evidence
    assert routes_phases._can_resume_interrupted_pre_qa(
        ctx, phase_id, state,
    ) is True
    repaired.write_text("tampered\n", encoding="utf-8")
    assert routes_phases._interrupted_pre_qa_recovery_blockers(
        ctx, phase_id, state,
    ) == ["registry_scope_changed"]
    repaired.write_text("export const secure = true;\n", encoding="utf-8")
    stored_evidence = ctx.supervisor_quality_runs[phase_id]["pending_evidence"]
    last_repair = next(
        item for item in reversed(stored_evidence)
        if str(item.get("step_id") or "").startswith("pre-qa-repair:")
    )
    valid_before = last_repair["metadata"]["before_digest"]
    last_repair["metadata"]["before_digest"] = "f" * 64
    assert routes_phases._interrupted_pre_qa_recovery_blockers(
        ctx, phase_id, state,
    ) == ["repair_digest_chain_broken"]
    last_repair["metadata"]["before_digest"] = valid_before
    scheduled = []

    def capture_task(coro, *, name):
        scheduled.append(name)
        coro.close()

    async def no_persist():
        return None

    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._auto_repair_states, key, state)
    monkeypatch.setattr(routes_phases, "_safe_create_task", capture_task)
    monkeypatch.setattr(routes_phases, "_persist_all_async", no_persist)
    claims = _mock_pre_qa_recovery_claim(monkeypatch)
    monkeypatch.setattr(
        routes_phases,
        "_apply_deterministic_pre_qa_repairs",
        lambda *_args, **_kwargs: pytest.fail(
            "persisted deterministic repairs must not be applied twice"
        ),
    )
    try:
        polled = asyncio.run(
            routes_phases.get_auto_repair_status(project_id, phase_id)
        )
        result = asyncio.run(
            routes_phases.resume_auto_repair(project_id, phase_id)
        )
        restored = ctx.supervisor_quality_runs[phase_id]
        assert polled["status"] == "interrupted"
        assert polled["auto_recovery_eligible"] is True
        assert polled["recovery_blockers"] == []
        assert result["scheduled"] is True
        assert result["status"]["status"] == "pre_qa_verifying"
        assert scheduled == [f"pre-qa-restart-recovery-{phase_id}"]
        assert len(claims) == 1
        assert restored["run_id"] == original_run_id
        assert restored["business_rounds_used"] == 0
        assert restored["verification_commit"].startswith("artifact:")
        assert state["pre_qa_result"] is None
        assert state["pre_qa_result_before_recovery"]["passed"] is False
        assert any(
            str(item.get("step_id") or "").startswith("scope-relock:")
            for item in restored["pending_evidence"]
        )
    finally:
        routes_phases.projects.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)


def test_agent_completion_enters_phase_quality_cycle(monkeypatch, tmp_path):
    project_id = "proj-qc-entry"
    agent_id = "agent-qc-entry"
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        agents={agent_id: {"id": agent_id, "status": "queued", "phase_id": "phase-1"}},
        subprojects=[{
            "id": "sp-1", "phase_id": "phase-1", "agent_id": agent_id,
            "status": "in_progress", "progress": 0,
        }],
    )
    monkeypatch.setitem(routes_execution.projects, project_id, ctx)

    class FakeExecutionAgent:
        def execute_task(self, **kwargs):
            return {
                "success": True,
                "status": "completed",
                "output_files": ["src/main.py"],
                "logs": [],
                "summary": "done",
            }

    qc_calls = []

    async def fake_quality_cycle(received_ctx):
        qc_calls.append(received_ctx.project_id)

    async def no_persist():
        return None

    monkeypatch.setattr(routes_execution, "_make_exec_agent", lambda *args, **kwargs: FakeExecutionAgent())
    monkeypatch.setattr(routes_execution, "_refresh_project_rollup", lambda received_ctx: None)
    monkeypatch.setattr(routes_execution, "_start_phase_quality_cycle_if_ready", fake_quality_cycle)
    monkeypatch.setattr(routes_execution, "_persist_all_async", no_persist)
    monkeypatch.setattr(routes_execution, "_persist_execution_state", lambda: None)

    asyncio.run(routes_execution._run_agent_task_unlocked(
        project_id=project_id,
        agent_id=agent_id,
        subproject_id="sp-1",
        subproject_name="Task",
        description="Build the real project",
        tech_stack=[],
        project_context="context",
    ))

    assert qc_calls == [project_id]
    assert ctx.agents[agent_id]["status"] == "completed"
    routes_execution.projects.pop(project_id, None)


def test_expert_lock_rejects_overlapping_file_scopes_atomically(monkeypatch, tmp_path):
    db_path = tmp_path / "locks.db"
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "_sqlite_path", str(db_path))
    monkeypatch.setattr(database, "_pg_unavailable", True)
    database.init_db()

    def claim(expert_number):
        return expert_lock.create_lock(
            expert_id=f"expert-{expert_number}",
            project_id="project-locks",
            task_id=f"task-{expert_number}",
            file_scope=["backend/src/"],
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (1, 2)))

    assert sorted(result["success"] for result in results) == [False, True]
    assert expert_lock.create_lock(
        "expert-3", "project-locks", "task-3", ["frontend/src/"]
    )["success"] is True
    wildcard = expert_lock.create_lock(
        "expert-wild", "project-wildcard", "task-wild", ["*"],
    )
    assert wildcard["success"] is True
    assert expert_lock.create_lock(
        "expert-sibling",
        "project-wildcard",
        "task-sibling",
        ["frontend/src/"],
    )["success"] is False
    assert expert_lock.create_lock(
        "expert-case-a",
        "project-casefold",
        "task-case-a",
        ["Backend/Src/"],
    )["success"] is True
    assert expert_lock.create_lock(
        "expert-case-b",
        "project-casefold",
        "task-case-b",
        ["backend/src/app.py"],
    )["success"] is False
