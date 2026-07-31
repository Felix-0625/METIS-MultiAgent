import asyncio
import copy
import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest

from api import routes_execution, routes_phases
from core.phase_execution_contract import acceptance_criterion_contracts


def test_locked_phase_manual_execution_routes_are_gone(monkeypatch) -> None:
    project_id = "locked-manual-bypass"
    phase_id = "phase-1"
    ctx = SimpleNamespace(
        project_id=project_id,
        agents={"agent-1": {"id": "agent-1", "phase_id": phase_id}},
        subprojects=[],
    )
    pm = SimpleNamespace(
        project_contract={"locked": True, "contract_version": 3},
        phases=[{
            "phase_id": phase_id,
            "execution_dispatch_plan": {"task_ids": ["task-1"]},
        }],
    )
    monkeypatch.setitem(routes_execution.projects, project_id, ctx)
    monkeypatch.setitem(routes_execution._phase_managers, project_id, pm)

    with pytest.raises(Exception) as single:
        asyncio.run(routes_execution.execute_agent_task(project_id, "agent-1"))
    assert single.value.status_code == 410

    with pytest.raises(Exception) as batch:
        asyncio.run(routes_execution.execute_all_agents(project_id))
    assert batch.value.status_code == 410


def test_root_package_command_criterion_cannot_bind_backend_gate() -> None:
    contract = acceptance_criterion_contracts(
        "phase-1-task-1",
        ["npm install 无报错"],
        artifact_paths=["package.json", "server.js"],
    )[0]
    row = {
        "contract": contract,
        "scope_text": "后端开发工程师负责根目录 Node.js 应用",
    }

    assert routes_phases._pre_qa_gate_matches_contract(
        "install-root", row,
    ) is True
    assert routes_phases._pre_qa_gate_matches_contract(
        "install-backend", row,
    ) is False


def test_start_phase_rejects_stale_canonical_requirements_revision(
    monkeypatch, tmp_path,
) -> None:
    project_id = "stale-requirements-phase"
    phase_id = "phase-1"
    phase = {"phase_id": phase_id, "status": "pending"}
    pm = SimpleNamespace(
        project_contract={
            "locked": True,
            "contract_version": 3,
            "requirements_revision": 1,
            "requirements_digest": "sha256:" + ("1" * 64),
        },
        get_phase=lambda candidate: phase if candidate == phase_id else None,
    )
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        agents={},
        subprojects=[],
    )
    leader = SimpleNamespace(
        requirements_revision=2,
        requirements_digest="sha256:" + ("2" * 64),
    )
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)
    monkeypatch.setitem(routes_phases._pm_teams, project_id, leader)

    with pytest.raises(Exception) as blocked:
        asyncio.run(routes_phases.start_phase(project_id, phase_id))

    assert blocked.value.status_code == 409
    assert phase["status"] == "pending"


def test_start_phase_accepts_matching_revision_before_planning(
    monkeypatch, tmp_path,
) -> None:
    project_id = "current-requirements-phase"
    phase_id = "phase-1"
    phase = {"phase_id": phase_id, "status": "pending"}
    digest = "sha256:" + ("3" * 64)
    pm = SimpleNamespace(
        project_contract={
            "locked": True,
            "contract_version": 3,
            "requirements_revision": 1,
            "requirements_digest": digest,
        },
        get_phase=lambda candidate: phase if candidate == phase_id else None,
    )
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        agents={},
        subprojects=[],
    )
    leader = SimpleNamespace(
        requirements_revision=1, requirements_digest=digest,
    )

    class BindingAccepted(RuntimeError):
        pass

    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)
    monkeypatch.setitem(routes_phases._pm_teams, project_id, leader)
    monkeypatch.setattr(
        routes_phases,
        "_migrate_invalid_phase_plan",
        lambda *_args: (_ for _ in ()).throw(BindingAccepted()),
    )

    with pytest.raises(BindingAccepted):
        asyncio.run(routes_phases.start_phase(project_id, phase_id))


def test_persisted_phase_coordinator_keeps_success_when_quality_start_fails(
    monkeypatch,
) -> None:
    project_id = "recover-wave"
    phase_id = "phase-1"
    generation = "generation-1"
    contract_digest = "sha256:" + ("a" * 64)
    baseline_digest = "sha256:" + ("b" * 64)
    task_one = {
        "task_id": "task-1", "agent_id": "agent-1",
        "required_role": "backend", "dependencies": [],
        "acceptance_criteria": [], "source_requirement_ids": [],
    }
    task_two = {
        "task_id": "task-2", "agent_id": "agent-1",
        "required_role": "backend", "dependencies": ["task-1"],
        "acceptance_criteria": [], "source_requirement_ids": [],
    }
    phase = {
        "phase_id": phase_id,
        "execution_generation": generation,
        "execution_contract_digest": contract_digest,
        "execution_requirements_revision": 1,
        "execution_artifact_baseline_digest": baseline_digest,
        "execution_coordinator": {
            "status": "running",
            "dispatch_attempt_digest": "resume-attempt-digest",
        },
        "rebuild_file_manifest": {
            "by_task_id": {
                "task-1": [],
                "task-2": ["backend/src/api.py", "backend/src/not-contract.py"],
            },
        },
        "execution_dispatch_plan": {
            "task_ids": ["task-1", "task-2"],
            "waves": [[task_one], [task_two]],
        },
        "execution_run_specs": [{
            "agent_id": "agent-1", "project_id": project_id,
            "subproject_id": "sp-1", "subproject_name": "API",
            "description": "base", "tech_stack": [], "project_context": "",
        }],
    }
    agent = {
        "id": "agent-1", "phase_id": phase_id,
        "required_rebuild_files": [], "required_delivery_files": [],
        "artifact_policy": {},
        "rebuild_file_specs": [
            {"path": "backend/src/api.py", "mode": "patch"},
            {"path": "backend/src/auth.py", "mode": "preserve"},
            {"path": "backend/src/not-contract.py", "mode": "patch"},
        ],
        "task_execution_receipts": {
                "task-1": {
                    "task_id": "task-1", "agent_id": "agent-1",
                    "phase_id": phase_id,
                    "status": "succeeded", "completion_run_id": "run-1",
                "execution_generation": generation,
                "contract_digest": contract_digest,
                "requirements_revision": 1,
                "artifact_baseline_digest": baseline_digest,
            },
        },
    }
    ctx = SimpleNamespace(
        project_id=project_id, agents={"agent-1": agent}, subprojects=[],
    )
    pm = SimpleNamespace(
        project_contract={
            "locked": True, "contract_version": 3,
            "requirements_revision": 1,
            "required_files": [
                {
                    "path": "backend/src/api.py",
                    "phase_id": phase_id,
                    "task_id": "task-2",
                    "required": True,
                },
                {
                    "path": "backend/src/auth.py",
                    "phase_id": phase_id,
                    "task_id": "task-2",
                    "required": True,
                },
            ],
        },
        phases=[phase],
        get_phase=lambda candidate: phase if candidate == phase_id else None,
    )
    runs = {
        "run-1": {
            "run_id": "run-1", "status": "succeeded",
                "payload": {
                    "task_id": "task-1", "agent_id": "agent-1",
                    "phase_id": phase_id,
                    "execution_generation": generation,
                    "contract_digest": contract_digest,
                    "requirements_revision": 1,
                    "artifact_baseline_digest": baseline_digest,
                },
            "result": {},
        },
        "run-2": {
            "run_id": "run-2", "status": "succeeded",
            "started_at": 2, "finished_at": 3,
            "payload": {
                "task_id": "task-2", "agent_id": "agent-1",
                "phase_id": phase_id, "execution_generation": generation,
                    "contract_digest": contract_digest,
                    "requirements_revision": 1,
                    "artifact_baseline_digest": baseline_digest,
                },
            "result": {},
        },
    }
    scheduled = []

    async def schedule(payload, **_kwargs):
        scheduled.append(copy.deepcopy(payload))
        return runs["run-2"], True

    async def persist():
        return None

    async def fail_quality(_ctx):
        raise FileNotFoundError(".dockerignore")

    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)
    monkeypatch.setattr(
        routes_execution._run_registry, "get",
        lambda run_id: runs[run_id],
    )
    monkeypatch.setattr(
        routes_execution, "_schedule_durable_agent_run", schedule,
    )
    monkeypatch.setattr(
        routes_execution, "_start_phase_quality_cycle_if_ready", fail_quality,
    )
    monkeypatch.setattr(routes_phases, "_persist_all_async", persist)

    assert asyncio.run(
        routes_phases._resume_locked_phase_coordinator(project_id, phase_id)
    ) is True
    assert [payload["task_id"] for payload in scheduled] == ["task-2"]
    resumed_policy = scheduled[0]["artifact_policy"]
    assert resumed_policy["required_files"] == ["backend/src/api.py"]
    assert resumed_policy["allowed_path_prefixes"] == ["backend/src/api.py"]
    assert resumed_policy["rebuild_file_specs"] == [
        {"path": "backend/src/api.py", "mode": "patch"},
    ]
    assert scheduled[0]["dispatch_attempt_digest"] == (
        "resume-attempt-digest"
    )
    assert phase["execution_coordinator"]["status"] == "completed"
    assert agent["task_execution_receipts"]["task-2"]["status"] == "succeeded"
    assert agent["task_execution_receipts"]["task-2"]["required_files"] == [
        "backend/src/api.py",
    ]
    assert phase["status"] == "qa_pending"
    assert phase["quality_start_error"]["error_code"] == "FileNotFoundError"
    assert asyncio.run(
        routes_phases._resume_locked_phase_coordinator(project_id, phase_id)
    ) is False


def test_current_coordinator_falls_back_only_to_unanimous_current_receipts(
    monkeypatch,
) -> None:
    project_id = "failed-repair-pointer"
    phase_id = "phase-2"
    identity = {
        "project_id": project_id,
        "phase_id": phase_id,
        "execution_generation": "generation-current",
        "contract_digest": "sha256:" + ("a" * 64),
        "requirements_revision": 3,
        "artifact_baseline_digest": "sha256:" + ("b" * 64),
    }
    prior_parent_id = "prior-authoritative-parent"
    prior_digest = "prior-attempt-digest"
    phase = {
        "phase_id": phase_id,
        "execution_generation": identity["execution_generation"],
        "execution_contract_digest": identity["contract_digest"],
        "execution_requirements_revision": identity["requirements_revision"],
        "execution_artifact_baseline_digest": identity[
            "artifact_baseline_digest"
        ],
        "execution_coordinator": {
            "status": "failed",
            "durable_run_id": "failed-repair-parent",
            "dispatch_attempt_digest": "failed-repair-digest",
            "repair_task_ids": ["task-1"],
            "repair_agent_ids": ["agent-1"],
        },
        "execution_dispatch_result": {"success": False},
        "execution_dispatch_plan": {
            "task_ids": ["task-1", "task-2"],
            "waves": [[
                {"task_id": "task-1", "agent_id": "agent-1"},
                {"task_id": "task-2", "agent_id": "agent-2"},
            ]],
        },
    }

    def receipt(task_id, agent_id, child_id):
        return {
            "task_id": task_id,
            "agent_id": agent_id,
            "phase_id": phase_id,
            "status": "succeeded",
            "completion_run_id": child_id,
            "execution_generation": identity["execution_generation"],
            "contract_digest": identity["contract_digest"],
            "requirements_revision": identity["requirements_revision"],
            "artifact_baseline_digest": identity[
                "artifact_baseline_digest"
            ],
        }

    agents = {
        "agent-1": {
            "id": "agent-1",
            "phase_id": phase_id,
            "locked_tasks": [{"task_id": "task-1", "required_role": "backend"}],
            "task_execution_receipts": {
                "task-1": receipt("task-1", "agent-1", "child-1"),
            },
            "pre_qa_repair_attempt_receipts": {
                "failed-repair-parent": {"task-1": {"status": "failed"}},
            },
        },
        "agent-2": {
            "id": "agent-2",
            "phase_id": phase_id,
            "locked_tasks": [{"task_id": "task-2", "required_role": "backend"}],
            "task_execution_receipts": {
                "task-2": receipt("task-2", "agent-2", "child-2"),
            },
        },
    }

    def child(task_id, agent_id, parent_id=prior_parent_id):
        return {
            "run_type": "agent.execute",
            "status": "succeeded",
            "payload": {
                **identity,
                "task_id": task_id,
                "agent_id": agent_id,
                "phase_coordinator_run_id": parent_id,
                "dispatch_attempt_digest": prior_digest,
            },
        }

    runs = {
        "failed-repair-parent": {
            "run_type": "phase.dispatch",
            "status": "failed",
            "payload": {
                **identity,
                "dispatch_attempt_digest": "failed-repair-digest",
            },
        },
        prior_parent_id: {
            "run_id": prior_parent_id,
            "run_type": "phase.dispatch",
            "status": "succeeded",
            "payload": {
                **identity,
                "dispatch_attempt_digest": prior_digest,
            },
        },
        "other-parent": {
            "run_id": "other-parent",
            "run_type": "phase.dispatch",
            "status": "succeeded",
            "payload": {
                **identity,
                "dispatch_attempt_digest": prior_digest,
            },
        },
        "child-1": child("task-1", "agent-1"),
        "child-2": child("task-2", "agent-2"),
    }
    ctx = SimpleNamespace(
        project_id=project_id,
        agents=agents,
        workspace=".",
        supervisor_quality_runs={},
    )
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setattr(
        routes_execution._run_registry, "get", lambda run_id: runs[run_id],
    )

    recovered = routes_phases._current_phase_coordinator_run(
        project_id, phase_id, phase,
    )
    assert recovered["run_id"] == prior_parent_id

    captured = {}

    def build_bundle(_phase, _assignments, **kwargs):
        captured.update(kwargs)
        return {"schema_version": 1}

    monkeypatch.setattr(
        routes_phases, "build_phase_evidence_bundle", build_bundle,
    )
    routes_phases._locked_phase_evidence_bundle(
        ctx,
        SimpleNamespace(file_registry={}),
        phase,
    )
    assert set(captured["run_records"]) == {"task-1", "task-2"}
    assert all(
        run["status"] == "succeeded"
        for run in captured["run_records"].values()
    )

    phase["execution_coordinator"].update({
        "status": "completed",
    })
    phase["execution_dispatch_result"] = {"success": True}
    runs["failed-repair-parent"]["status"] = "succeeded"
    runs["failed-repair-parent"]["run_id"] = "failed-repair-parent"
    runs["child-1"]["payload"].update({
        "phase_coordinator_run_id": "failed-repair-parent",
        "dispatch_attempt_digest": "failed-repair-digest",
    })
    agents["agent-1"]["task_execution_receipts"]["task-1"].update({
        "dispatch_attempt_digest": "failed-repair-digest",
        "repair_coordinator_run_id": "failed-repair-parent",
    })
    captured.clear()
    routes_phases._locked_phase_evidence_bundle(
        ctx,
        SimpleNamespace(file_registry={}),
        phase,
    )
    assert all(
        run["status"] == "succeeded"
        for run in captured["run_records"].values()
    )

    phase["execution_coordinator"]["status"] = "failed"
    phase["execution_dispatch_result"] = {"success": False}
    runs["failed-repair-parent"]["status"] = "failed"
    runs["child-1"] = child("task-1", "agent-1")
    agents["agent-1"]["task_execution_receipts"]["task-1"].pop(
        "dispatch_attempt_digest",
    )
    agents["agent-1"]["task_execution_receipts"]["task-1"].pop(
        "repair_coordinator_run_id",
    )

    task_two_receipt = agents["agent-2"]["task_execution_receipts"].pop(
        "task-2"
    )
    assert not routes_phases._current_phase_coordinator_run(
        project_id, phase_id, phase,
    )
    agents["agent-2"]["task_execution_receipts"]["task-2"] = task_two_receipt

    runs["child-2"]["payload"]["phase_coordinator_run_id"] = "other-parent"
    assert not routes_phases._current_phase_coordinator_run(
        project_id, phase_id, phase,
    )
    runs["child-2"] = child("task-2", "agent-2")

    runs["child-2"]["payload"]["execution_generation"] = "stale-generation"
    assert not routes_phases._current_phase_coordinator_run(
        project_id, phase_id, phase,
    )


def test_phase_coordinator_preserves_successful_peer_when_same_wave_task_fails(
    monkeypatch,
) -> None:
    project_id = "same-wave-peer-failure"
    phase_id = "phase-1"
    generation = "generation-peer-failure"
    contract_digest = "sha256:" + ("c" * 64)
    baseline_digest = "sha256:" + ("d" * 64)
    task_a = {
        "task_id": "task-a", "agent_id": "agent-a",
        "required_role": "backend", "dependencies": [],
        "acceptance_criteria": [], "source_requirement_ids": [],
    }
    task_b = {
        "task_id": "task-b", "agent_id": "agent-b",
        "required_role": "frontend", "dependencies": [],
        "acceptance_criteria": [], "source_requirement_ids": [],
    }
    downstream = {
        "task_id": "task-c", "agent_id": "agent-a",
        "required_role": "backend", "dependencies": ["task-a", "task-b"],
        "acceptance_criteria": [], "source_requirement_ids": [],
    }
    phase = {
        "phase_id": phase_id,
        "status": "active",
        "execution_generation": generation,
        "execution_contract_digest": contract_digest,
        "execution_requirements_revision": 7,
        "execution_artifact_baseline_digest": baseline_digest,
        "execution_coordinator": {"status": "running"},
        "execution_dispatch_plan": {
            "phase_id": phase_id,
            "task_ids": ["task-a", "task-b", "task-c"],
            "waves": [[task_a, task_b], [downstream]],
        },
        "execution_run_specs": [
            {
                "agent_id": "agent-a", "project_id": project_id,
                "subproject_id": "sp-a", "subproject_name": "Backend",
                "description": "backend", "tech_stack": [],
                "project_context": "",
            },
            {
                "agent_id": "agent-b", "project_id": project_id,
                "subproject_id": "sp-b", "subproject_name": "Frontend",
                "description": "frontend", "tech_stack": [],
                "project_context": "",
            },
        ],
    }
    agents = {
        agent_id: {
            "id": agent_id,
            "phase_id": phase_id,
            "required_rebuild_files": [],
            "required_delivery_files": [],
            "artifact_policy": {},
            "task_execution_receipts": {},
        }
        for agent_id in ("agent-a", "agent-b")
    }
    ctx = SimpleNamespace(
        project_id=project_id,
        agents=agents,
        subprojects=[
            {"id": "sp-a", "agent_id": "agent-a", "status": "in_progress"},
            {"id": "sp-b", "agent_id": "agent-b", "status": "in_progress"},
        ],
    )
    pm = SimpleNamespace(
        project_contract={
            "locked": True,
            "contract_version": 3,
            "requirements_revision": 7,
            "required_files": [],
        },
        phases=[phase],
        get_phase=lambda candidate: phase if candidate == phase_id else None,
    )
    runs = {
        "task-a": {
            "run_id": "run-task-a",
            "status": "succeeded",
            "started_at": 1,
            "finished_at": 2,
            "payload": {
                "task_id": "task-a",
                "agent_id": "agent-a",
                "phase_id": phase_id,
                "execution_generation": generation,
            },
            "result": {"output_files": ["backend/a.py"]},
        },
        "task-b": {
            "run_id": "run-task-b",
            "status": "failed",
            "started_at": 1,
            "finished_at": 3,
            "payload": {
                "task_id": "task-b",
                "agent_id": "agent-b",
                "phase_id": phase_id,
                "execution_generation": generation,
            },
            "result": {"error_code": "agent_execution_failed"},
        },
    }
    runs_by_id = {
        run["run_id"]: run for run in runs.values()
    }
    scheduled = []
    persisted = []

    async def schedule(payload, **_kwargs):
        task_id = payload["task_id"]
        scheduled.append(task_id)
        if task_id == "task-c":
            pytest.fail("downstream task must not be scheduled after peer failure")
        return runs[task_id], True

    async def persist():
        persisted.append({
            "phase_status": phase.get("status"),
            "coordinator_status": (
                phase.get("execution_coordinator") or {}
            ).get("status"),
            "task_a_status": (
                agents["agent-a"]["task_execution_receipts"]
                .get("task-a") or {}
            ).get("status"),
            "task_b_status": (
                agents["agent-b"]["task_execution_receipts"]
                .get("task-b") or {}
            ).get("status"),
        })

    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)
    monkeypatch.setattr(
        routes_execution._run_registry,
        "get",
        lambda run_id: runs_by_id[run_id],
    )
    monkeypatch.setattr(
        routes_execution, "_schedule_durable_agent_run", schedule,
    )
    monkeypatch.setattr(routes_phases, "_persist_all_async", persist)

    assert asyncio.run(
        routes_phases._resume_locked_phase_coordinator(project_id, phase_id)
    ) is True

    receipt_a = agents["agent-a"]["task_execution_receipts"]["task-a"]
    receipt_b = agents["agent-b"]["task_execution_receipts"]["task-b"]
    assert scheduled == ["task-a", "task-b"]
    assert receipt_a["status"] == "succeeded"
    assert receipt_a["completion_run_id"] == "run-task-a"
    assert receipt_a["result"]["output_files"] == ["backend/a.py"]
    assert receipt_b["status"] == "failed"
    assert receipt_b["completion_run_id"] == "run-task-b"
    assert receipt_b["status"] != "succeeded"
    assert "task-c" not in scheduled
    assert phase["status"] == "failed"
    assert phase["execution_coordinator"]["status"] == "failed"
    assert phase["execution_coordinator"]["error_code"] == (
        "PhaseExecutionContractError"
    )
    assert persisted[-1] == {
        "phase_status": "failed",
        "coordinator_status": "failed",
        "task_a_status": "succeeded",
        "task_b_status": "failed",
    }


def test_startup_recovery_claims_once_without_awaiting_full_dag(
    monkeypatch,
) -> None:
    project_id = "nonblocking-recovery-" + uuid.uuid4().hex
    phase_id = "phase-1"
    phase = {
        "phase_id": phase_id,
        "execution_generation": "generation-1",
        "execution_coordinator": {"status": "running"},
        "execution_dispatch_plan": {
            "task_ids": ["task-1"],
            "waves": [[{"task_id": "task-1", "agent_id": "agent-1"}]],
        },
        "execution_run_specs": [{"agent_id": "agent-1"}],
    }
    pm = SimpleNamespace(
        phases=[phase],
        phase_agents={phase_id: ["agent-1"]},
        get_phase=lambda candidate: phase if candidate == phase_id else None,
    )
    ctx = SimpleNamespace(project_id=project_id, agents={}, subprojects=[])
    scheduled = []
    executed = []

    async def fake_resume(*_args):
        executed.append(True)
        return True

    def capture(coro, name=""):
        scheduled.append((coro, name))
        return SimpleNamespace()

    async def persist():
        return None

    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)
    monkeypatch.setattr(
        routes_phases, "_resume_locked_phase_coordinator", fake_resume,
    )
    monkeypatch.setattr(routes_phases, "_safe_create_task", capture)
    monkeypatch.setattr(routes_phases, "_persist_all_async", persist)

    assert asyncio.run(routes_phases.resume_pending_phase_dispatches()) == 1
    assert executed == []
    assert len(scheduled) == 1
    assert asyncio.run(routes_phases.resume_pending_phase_dispatches()) == 0
    assert len(scheduled) == 1
    scheduled[0][0].close()


def test_startup_cancels_superseded_parent_and_payload_linked_child_then_allows_reset(
    monkeypatch, tmp_path,
) -> None:
    project_id = "startup-superseded-reset"
    phase_id = "phase-3"
    parent_id = "old-phase-dispatch"
    blocked_child_id = "blocked-frontend-child"
    succeeded_child_id = "succeeded-backend-child"
    agent_id = "frontend-agent"
    subproject_id = "frontend-task"
    phase = {
        "phase_id": phase_id,
        "status": "failed",
        "execution_generation": "generation-current",
        "execution_contract_digest": "sha256:" + ("a" * 64),
        "execution_requirements_revision": 3,
        "execution_artifact_baseline_digest": "sha256:" + ("b" * 64),
        "execution_coordinator": {
            "status": "failed",
            "durable_run_id": parent_id,
            "dispatch_attempt_digest": "",
        },
        "agents": [agent_id],
        "subprojects": [subproject_id],
    }
    pm = SimpleNamespace(
        phases=[phase],
        phase_agents={phase_id: [agent_id]},
        file_registry={},
        get_phase=lambda candidate: phase if candidate == phase_id else None,
    )
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        agents={
            agent_id: {
                "id": agent_id,
                "phase_id": phase_id,
                "subproject_id": subproject_id,
                "status": "working",
                "lock_id": "blocked-child-lock",
                "lock_run_id": blocked_child_id,
                "task_execution_receipts": {
                    subproject_id: {
                        "start_run_id": blocked_child_id,
                    },
                },
            },
        },
        subprojects=[{
            "id": subproject_id,
            "phase_id": phase_id,
            "agent_id": agent_id,
            "status": "in_progress",
            "progress": 40,
        }],
        qc_results={},
        supervisor_quality_runs={},
    )

    def old_payload(**extra):
        return {
            "project_id": project_id,
            "phase_id": phase_id,
            "execution_generation": "generation-old",
            "contract_digest": phase["execution_contract_digest"],
            "requirements_revision": 3,
            "artifact_baseline_digest": (
                phase["execution_artifact_baseline_digest"]
            ),
            "dispatch_attempt_digest": "",
            **extra,
        }

    runs = {
        parent_id: {
            "run_id": parent_id,
            "run_type": "phase.dispatch",
            "project_id": project_id,
            "status": "pending",
            "version": 1,
            "payload": old_payload(),
        },
        blocked_child_id: {
            "run_id": blocked_child_id,
            "run_type": "agent.execute",
            "project_id": project_id,
            "parent_run_id": None,
            "status": "blocked",
            "version": 4,
            "payload": old_payload(
                phase_coordinator_run_id=parent_id,
                agent_id=agent_id,
                task_id=subproject_id,
            ),
        },
        succeeded_child_id: {
            "run_id": succeeded_child_id,
            "run_type": "agent.execute",
            "project_id": project_id,
            "parent_run_id": None,
            "status": "succeeded",
            "version": 3,
            "payload": old_payload(
                phase_coordinator_run_id=parent_id,
                agent_id="backend-agent",
                task_id="backend-task",
            ),
        },
    }
    cancelled = []

    class Registry:
        def recover_startup(self):
            return []

        def list_runs(self, *, statuses, run_type, limit):
            assert limit == 1000
            return [
                run for run in runs.values()
                if run["run_type"] == run_type
                and run["status"] in statuses
            ]

        def get(self, run_id):
            return runs[run_id]

        def cancel_unleased(
            self, run_id, *, reason, actor, expected_version=None,
        ):
            run = runs[run_id]
            assert run["status"] in {"pending", "blocked"}
            assert expected_version == run["version"]
            assert actor == "startup-reconcile"
            run.update(
                status="cancelled",
                version=run["version"] + 1,
                last_error=reason,
                finished_at=123.0,
            )
            cancelled.append(run_id)
            return run

        def claim(self, *_args, **_kwargs):
            pytest.fail("superseded coordinator must not be claimed")

    locks = [{
        "lock_id": "blocked-child-lock",
        "project_id": project_id,
        "task_id": f"{subproject_id}:run:{blocked_child_id}",
    }]

    def release_lock(lock_id):
        locks[:] = [
            lock for lock in locks if lock.get("lock_id") != lock_id
        ]
        return {"success": True}

    async def no_persist():
        return None

    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)
    monkeypatch.setattr(routes_execution, "_run_registry", Registry())
    monkeypatch.setattr(routes_execution, "_active_run_tasks", {})
    monkeypatch.setattr(routes_execution, "_run_execution_guards", {})
    monkeypatch.setattr(routes_execution, "_run_cancel_events", {})
    monkeypatch.setattr(
        routes_execution,
        "execution_status",
        {
            agent_id: {
                "run_id": blocked_child_id,
                "status": "working",
                "run_status": "blocked",
                "progress": 40,
            },
        },
    )
    monkeypatch.setattr(
        routes_execution, "_persist_execution_state", lambda: None,
    )
    monkeypatch.setattr(routes_phases, "_persist_all_async", no_persist)
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "get_active_locks",
        lambda **_kwargs: list(locks),
    )
    monkeypatch.setattr(
        routes_phases.expert_lock, "release_lock", release_lock,
    )
    monkeypatch.setattr(
        routes_phases, "_get_supervisor_leader", lambda _project_id: None,
    )

    assert asyncio.run(routes_phases.resume_pending_phase_dispatches()) == 0
    assert cancelled == [parent_id, blocked_child_id]
    assert runs[parent_id]["status"] == "cancelled"
    assert runs[blocked_child_id]["status"] == "cancelled"
    assert runs[succeeded_child_id]["status"] == "succeeded"
    assert runs[blocked_child_id]["parent_run_id"] is None
    assert (
        runs[blocked_child_id]["payload"]["phase_coordinator_run_id"]
        == parent_id
    )
    assert ctx.agents[agent_id]["status"] == "failed"
    assert ctx.subprojects[0]["status"] == "failed"
    assert routes_execution.execution_status[agent_id]["status"] == "cancelled"
    assert locks == []

    reset = asyncio.run(
        routes_phases._reset_phase(
            project_id, phase_id, preserve_rebuild_state=True,
        )
    )
    assert reset["success"] is True
    assert agent_id not in ctx.agents
    assert phase["status"] == "pending"


def test_startup_keeps_and_claims_current_authoritative_pending_parent(
    monkeypatch,
) -> None:
    project_id = "startup-current-parent"
    phase_id = "phase-1"
    parent_id = "current-phase-dispatch"
    generation = "generation-current"
    contract_digest = "sha256:" + ("c" * 64)
    baseline_digest = "sha256:" + ("d" * 64)
    phase = {
        "phase_id": phase_id,
        "status": "active",
        "execution_generation": generation,
        "execution_contract_digest": contract_digest,
        "execution_requirements_revision": 2,
        "execution_artifact_baseline_digest": baseline_digest,
        "execution_dispatch_plan": {
            "task_ids": ["task-1"],
            "waves": [[{"task_id": "task-1", "agent_id": "agent-1"}]],
        },
        "execution_run_specs": [{"agent_id": "agent-1"}],
        "execution_coordinator": {
            "status": "running",
            "durable_run_id": parent_id,
            "dispatch_attempt_digest": "",
        },
    }
    pm = SimpleNamespace(
        phases=[phase],
        phase_agents={phase_id: ["agent-1"]},
        get_phase=lambda candidate: phase if candidate == phase_id else None,
    )
    ctx = SimpleNamespace(project_id=project_id, agents={}, subprojects=[])
    parent = {
        "run_id": parent_id,
        "run_type": "phase.dispatch",
        "project_id": project_id,
        "status": "pending",
        "version": 5,
        "payload": {
            "project_id": project_id,
            "phase_id": phase_id,
            "execution_generation": generation,
            "contract_digest": contract_digest,
            "requirements_revision": 2,
            "artifact_baseline_digest": baseline_digest,
            "dispatch_attempt_digest": "",
        },
    }
    claimed = []
    scheduled = []

    class Registry:
        def recover_startup(self):
            return []

        def list_runs(self, *, statuses, run_type, limit):
            if run_type == "phase.dispatch" and parent["status"] in statuses:
                return [parent]
            return []

        def get(self, run_id):
            assert run_id == parent_id
            return parent

        def cancel_unleased(self, *_args, **_kwargs):
            pytest.fail("current authoritative coordinator must not be cancelled")

        def claim(self, run_id, owner, *, lease_seconds):
            assert run_id == parent_id
            assert lease_seconds > 0
            parent.update(
                status="running",
                lease_owner=owner,
                lease_expires_at=9999999999,
                version=6,
            )
            claimed.append(run_id)
            return parent

    def capture(coro, name=""):
        scheduled.append(coro)
        return SimpleNamespace()

    async def no_persist():
        return None

    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)
    monkeypatch.setattr(routes_execution, "_run_registry", Registry())
    monkeypatch.setattr(routes_execution, "_active_run_tasks", {})
    monkeypatch.setattr(routes_execution, "_run_execution_guards", {})
    monkeypatch.setattr(routes_phases, "_safe_create_task", capture)
    monkeypatch.setattr(routes_phases, "_persist_all_async", no_persist)

    assert asyncio.run(routes_phases.resume_pending_phase_dispatches()) == 1
    assert claimed == [parent_id]
    assert parent["status"] == "running"
    assert len(scheduled) == 1
    scheduled[0].close()


def _reconcile_current_parent_child_case(
    monkeypatch,
    *,
    child_task_id: str,
    child_agent_id: str,
):
    project_id = f"startup-child-membership-{child_task_id}-{child_agent_id}"
    phase_id = "phase-1"
    parent_id = "current-phase-dispatch"
    child_id = "pending-phase-child"
    generation = "generation-current"
    contract_digest = "sha256:" + ("e" * 64)
    baseline_digest = "sha256:" + ("f" * 64)
    identity = {
        "project_id": project_id,
        "phase_id": phase_id,
        "execution_generation": generation,
        "contract_digest": contract_digest,
        "requirements_revision": 4,
        "artifact_baseline_digest": baseline_digest,
        "dispatch_attempt_digest": "",
    }
    phase = {
        "phase_id": phase_id,
        "status": "active",
        "execution_generation": generation,
        "execution_contract_digest": contract_digest,
        "execution_requirements_revision": 4,
        "execution_artifact_baseline_digest": baseline_digest,
        "execution_dispatch_plan": {
            "task_ids": ["task-1"],
            "waves": [[{
                "task_id": "task-1",
                "agent_id": "agent-1",
            }]],
        },
        "execution_run_specs": [{"agent_id": "agent-1"}],
        "execution_coordinator": {
            "status": "running",
            "durable_run_id": parent_id,
            "dispatch_attempt_digest": "",
        },
    }
    parent = {
        "run_id": parent_id,
        "run_type": "phase.dispatch",
        "project_id": project_id,
        "status": "running",
        "version": 7,
        "payload": dict(identity),
    }
    child = {
        "run_id": child_id,
        "run_type": "agent.execute",
        "project_id": project_id,
        "status": "pending",
        "version": 3,
        "payload": {
            **identity,
            "phase_coordinator_run_id": parent_id,
            "task_id": child_task_id,
            "agent_id": child_agent_id,
        },
    }
    pm = SimpleNamespace(phases=[phase])
    ctx = SimpleNamespace(
        project_id=project_id,
        agents={},
        subprojects=[],
    )
    cancelled = []

    class Registry:
        @staticmethod
        def list_runs(*, statuses, run_type, limit):
            assert limit == 1000
            candidates = [parent, child]
            return [
                run for run in candidates
                if run["run_type"] == run_type
                and run["status"] in statuses
            ]

        @staticmethod
        def get(run_id):
            assert run_id == parent_id
            return parent

        @staticmethod
        def cancel_unleased(
            run_id, *, reason, actor, expected_version=None,
        ):
            assert run_id == child_id
            assert actor == "startup-reconcile"
            assert expected_version == child["version"]
            child.update({
                "status": "cancelled",
                "version": child["version"] + 1,
                "last_error": reason,
            })
            cancelled.append(run_id)
            return child

    monkeypatch.setattr(routes_phases, "projects", {project_id: ctx})
    monkeypatch.setattr(routes_phases, "_phase_managers", {project_id: pm})
    monkeypatch.setattr(routes_execution, "_run_registry", Registry())
    monkeypatch.setattr(routes_execution, "_active_run_tasks", {})
    monkeypatch.setattr(routes_execution, "_run_execution_guards", {})
    monkeypatch.setattr(routes_execution, "_run_cancel_events", {})
    monkeypatch.setattr(routes_execution, "execution_status", {})
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "get_active_locks",
        lambda **_kwargs: [],
    )

    changed = asyncio.run(routes_phases._reconcile_superseded_phase_runs())
    return changed, parent, child, cancelled


def test_startup_does_not_preserve_forged_child_outside_current_plan(
    monkeypatch,
) -> None:
    changed, parent, child, cancelled = (
        _reconcile_current_parent_child_case(
            monkeypatch,
            child_task_id="forged-task",
            child_agent_id="agent-1",
        )
    )

    assert changed is True
    assert parent["status"] == "running"
    assert child["status"] == "cancelled"
    assert cancelled == [child["run_id"]]


def test_startup_preserves_child_with_current_parent_and_task_membership(
    monkeypatch,
) -> None:
    changed, parent, child, cancelled = (
        _reconcile_current_parent_child_case(
            monkeypatch,
            child_task_id="task-1",
            child_agent_id="agent-1",
        )
    )

    assert changed is False
    assert parent["status"] == "running"
    assert child["status"] == "pending"
    assert cancelled == []


@pytest.mark.parametrize("crash_point", ["after_claim_before_task", "mid_wave"])
def test_expired_durable_phase_claim_is_recoverable_after_crash(
    monkeypatch,
    crash_point,
) -> None:
    from core.execution_runs import DurableRunRegistry

    now = [100.0]
    registry = DurableRunRegistry(clock=lambda: now[0])
    project_id = f"recover-expired-{crash_point}-{uuid.uuid4().hex}"
    phase_id = "phase-1"
    phase = {
        "phase_id": phase_id,
        "execution_generation": f"generation-{crash_point}",
        "execution_contract_digest": "sha256:" + ("a" * 64),
        "execution_requirements_revision": 1,
        "execution_artifact_baseline_digest": "sha256:" + ("b" * 64),
        "execution_coordinator": {"status": "running"},
        "execution_dispatch_plan": {
            "task_ids": ["task-1"],
            "waves": [[{"task_id": "task-1", "agent_id": "agent-1"}]],
        },
        "execution_run_specs": [{"agent_id": "agent-1"}],
    }
    pm = SimpleNamespace(
        phases=[phase],
        phase_agents={phase_id: ["agent-1"]},
        get_phase=lambda candidate: phase if candidate == phase_id else None,
    )
    ctx = SimpleNamespace(project_id=project_id, agents={}, subprojects=[])
    scheduled = []

    def capture(coro, name=""):
        scheduled.append(coro)
        return SimpleNamespace()

    async def persist():
        return None

    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)
    monkeypatch.setattr(routes_execution, "_run_registry", registry)
    monkeypatch.setattr(routes_phases, "_safe_create_task", capture)
    monkeypatch.setattr(routes_phases, "_persist_all_async", persist)

    assert asyncio.run(routes_phases.resume_pending_phase_dispatches()) == 1
    first = registry.get(phase["execution_coordinator"]["durable_run_id"])
    assert first["status"] == "running"
    scheduled.pop(0).close()  # process died before/mid coordinator coroutine

    now[0] += routes_phases._PHASE_COORDINATOR_LEASE_SECONDS + 1
    assert asyncio.run(routes_phases.resume_pending_phase_dispatches()) == 1
    second = registry.get(phase["execution_coordinator"]["durable_run_id"])
    assert second["status"] == "running"
    assert second["attempt_count"] == 2
    scheduled.pop(0).close()


def test_phase_coordinator_lease_loss_cancels_old_driver_before_commit(
    monkeypatch,
) -> None:
    from core.execution_runs import LeaseConflict

    committed = []
    cancelled = []

    class LostLeaseRegistry:
        def heartbeat(self, *_args, **_kwargs):
            raise LeaseConflict("owner-b took over")

        def succeed(self, *_args, **_kwargs):
            committed.append("succeeded")

        def fail(self, *_args, **_kwargs):
            committed.append("failed")

    async def driver():
        try:
            await asyncio.Event().wait()
            committed.append("receipt")
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(
        routes_execution, "_run_registry", LostLeaseRegistry(),
    )
    monkeypatch.setattr(
        routes_phases, "_PHASE_COORDINATOR_LEASE_SECONDS", 0.01,
    )

    asyncio.run(routes_phases._run_claimed_phase_coordinator(
        "lease-loss-project", "phase-1", "run-a", "owner-a", driver,
    ))

    assert cancelled == [True]
    assert committed == []


def test_cancelled_coordinator_closes_already_persisted_dispatch_failure(
    monkeypatch,
) -> None:
    project_id = "cancelled-failed-coordinator"
    phase_id = "phase-1"
    run_id = "coordinator-run"
    owner = "coordinator-owner"
    phase = {
        "phase_id": phase_id,
        "status": "failed",
        "execution_generation": "generation-1",
        "execution_contract_digest": "sha256:" + ("a" * 64),
        "execution_requirements_revision": 1,
        "execution_artifact_baseline_digest": "sha256:" + ("b" * 64),
        "execution_dispatch_result": {
            "status": "failed",
            "error_code": "ModelOutputFormatError",
        },
        "execution_coordinator": {
            "status": "failed",
            "durable_run_id": run_id,
            "dispatch_attempt_digest": "",
            "dispatch_completed": True,
            "error_code": "ModelOutputFormatError",
        },
    }
    pm = SimpleNamespace(
        get_phase=lambda candidate: phase if candidate == phase_id else None,
    )
    driver_persisted = asyncio.Event()
    failed = []

    class Registry:
        def get(self, candidate):
            assert candidate == run_id
            return {
                "run_id": run_id,
                "status": "running",
                "lease_owner": owner,
                "lease_expires_at": 9999999999,
                "payload": {
                    "project_id": project_id,
                    "phase_id": phase_id,
                    "execution_generation": "generation-1",
                    "contract_digest": "sha256:" + ("a" * 64),
                    "requirements_revision": 1,
                    "artifact_baseline_digest": "sha256:" + ("b" * 64),
                    "dispatch_attempt_digest": "",
                },
            }

        def heartbeat(self, *_args, **_kwargs):
            return None

        def fail(self, candidate, candidate_owner, *, error, retryable):
            assert candidate == run_id
            assert candidate_owner == owner
            assert retryable is False
            failed.append(error)

    async def driver():
        driver_persisted.set()
        await asyncio.Event().wait()

    async def scenario():
        task = asyncio.create_task(
            routes_phases._run_claimed_phase_coordinator(
                project_id, phase_id, run_id, owner, driver,
            )
        )
        await driver_persisted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)
    monkeypatch.setattr(routes_execution, "_run_registry", Registry())

    asyncio.run(scenario())

    assert failed == ["ModelOutputFormatError"]


def test_durable_coordinator_succeeds_before_quality_starts(
    monkeypatch,
) -> None:
    project_id = "coordinator-quality-order"
    phase_id = "phase-1"
    run_id = "coordinator-run"
    owner = "coordinator-owner"
    phase = {
        "phase_id": phase_id,
        "execution_generation": "generation-1",
        "execution_contract_digest": "sha256:" + ("a" * 64),
        "execution_requirements_revision": 2,
        "execution_artifact_baseline_digest": "sha256:" + ("b" * 64),
        "execution_dispatch_result": {"success": True},
        "execution_coordinator": {
            "status": "running",
            "durable_run_id": run_id,
            "dispatch_completed": True,
        },
    }
    pm = SimpleNamespace(
        get_phase=lambda candidate: phase if candidate == phase_id else None,
    )
    ctx = SimpleNamespace(project_id=project_id, agents={})
    events = []

    class Registry:
        status = "running"

        def get(self, candidate):
            assert candidate == run_id
            return {
                "run_id": run_id,
                "status": self.status,
                "lease_owner": owner,
                "lease_expires_at": 9999999999,
                "payload": {
                    "project_id": project_id,
                    "phase_id": phase_id,
                    "execution_generation": "generation-1",
                    "contract_digest": "sha256:" + ("a" * 64),
                    "requirements_revision": 2,
                    "artifact_baseline_digest": "sha256:" + ("b" * 64),
                    "dispatch_attempt_digest": "",
                },
            }

        def heartbeat(self, *_args, **_kwargs):
            return None

        def succeed(self, *_args, **_kwargs):
            events.append("succeed")
            self.status = "succeeded"
            return self.get(run_id)

        def fail(self, *_args, **_kwargs):
            pytest.fail("successful coordinator must not fail")

    async def driver():
        return True

    async def persist():
        events.append("persist")

    async def quality(_ctx, current_phase, current_phase_id):
        assert current_phase is phase
        assert current_phase_id == phase_id
        assert registry.status == "succeeded"
        assert phase["execution_coordinator"]["status"] == "completed"
        events.append("quality")
        return True

    registry = Registry()
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)
    monkeypatch.setattr(routes_execution, "_run_registry", registry)
    monkeypatch.setattr(routes_phases, "_persist_all_async", persist)
    monkeypatch.setattr(
        routes_phases, "_start_phase_quality_after_execution", quality,
    )

    asyncio.run(routes_phases._run_claimed_phase_coordinator(
        project_id, phase_id, run_id, owner, driver,
    ))

    assert events == ["succeed", "persist", "quality", "persist"]


def test_startup_recovery_rolls_back_incomplete_construction(
    monkeypatch,
) -> None:
    project_id = "construction-crash"
    phase_id = "phase-1"
    phase = {
        "phase_id": phase_id,
        "status": "active",
        "agents": ["agent-1"],
        "execution_generation": "generation-1",
        "execution_coordinator": {"status": "starting"},
    }
    agent = {
        "id": "agent-1", "phase_id": phase_id, "lock_id": "lock-1",
    }
    subproject = {
        "id": "sp-1", "phase_id": phase_id, "agent_id": "agent-1",
        "status": "in_progress", "progress": 20,
    }
    pm = SimpleNamespace(
        phases=[phase],
        phase_agents={phase_id: ["agent-1"]},
    )
    ctx = SimpleNamespace(
        project_id=project_id,
        agents={"agent-1": agent},
        subprojects=[subproject],
    )
    released = []

    async def persist():
        return None

    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)
    monkeypatch.setattr(
        routes_phases.expert_lock, "release_lock",
        lambda lock_id: released.append(lock_id),
    )
    monkeypatch.setattr(routes_phases, "_persist_all_async", persist)

    assert asyncio.run(routes_phases.resume_pending_phase_dispatches()) == 0
    assert ctx.agents == {}
    assert released == ["lock-1"]
    assert phase["status"] == "pending"
    assert phase["execution_coordinator"]["status"] == "failed"
    assert phase["execution_coordinator"]["error_code"] == (
        "construction_contract_incomplete"
    )
    assert subproject["status"] == "pending"
    assert "agent_id" not in subproject


def test_startup_reconciles_succeeded_coordinator_before_quality(
    monkeypatch,
) -> None:
    project_id = "reconcile-succeeded-coordinator"
    phase_id = "phase-1"
    run_id = "coordinator-succeeded"
    phase = {
        "phase_id": phase_id,
        "status": "active",
        "execution_generation": "generation-1",
        "execution_contract_digest": "sha256:" + ("a" * 64),
        "execution_requirements_revision": 3,
        "execution_artifact_baseline_digest": "sha256:" + ("b" * 64),
        "execution_dispatch_plan": {
            "task_ids": ["task-1"],
            "waves": [[{"task_id": "task-1", "agent_id": "agent-1"}]],
        },
        "execution_run_specs": [{"agent_id": "agent-1"}],
        "execution_dispatch_result": {"success": True},
        "execution_coordinator": {
            "status": "running",
            "durable_run_id": run_id,
            "dispatch_completed": True,
        },
    }
    pm = SimpleNamespace(
        phases=[phase],
        get_phase=lambda candidate: phase if candidate == phase_id else None,
    )
    ctx = SimpleNamespace(project_id=project_id, agents={})
    quality = []

    class Registry:
        def recover_startup(self):
            return 0

        def get(self, candidate):
            assert candidate == run_id
            return {
                "run_id": run_id,
                "status": "succeeded",
                "finished_at": 123,
                "payload": {
                    "project_id": project_id,
                    "phase_id": phase_id,
                    "execution_generation": "generation-1",
                    "contract_digest": "sha256:" + ("a" * 64),
                    "requirements_revision": 3,
                    "artifact_baseline_digest": "sha256:" + ("b" * 64),
                    "dispatch_attempt_digest": "",
                },
            }

        def claim(self, *_args, **_kwargs):
            pytest.fail("succeeded coordinator must not be claimed again")

    async def persist():
        return None

    async def start_quality(_ctx, current_phase, current_phase_id):
        quality.append((current_phase, current_phase_id))
        return True

    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)
    monkeypatch.setattr(routes_execution, "_run_registry", Registry())
    monkeypatch.setattr(routes_phases, "_persist_all_async", persist)
    monkeypatch.setattr(
        routes_phases, "_start_phase_quality_after_execution",
        start_quality,
    )

    assert asyncio.run(
        routes_phases.resume_pending_phase_dispatches()
    ) == 0
    assert phase["execution_coordinator"]["status"] == "completed"
    assert phase["execution_coordinator"]["durable_status"] == "succeeded"
    assert quality == [(phase, phase_id)]


@pytest.mark.parametrize(
    ("mutation", "validation_valid"),
    [
        ({"execution_generation": "stale-generation"}, True),
        ({"phase_id": "wrong-phase"}, True),
        ({}, False),
    ],
)
def test_cross_phase_unlock_rejects_stale_wrong_or_unverified_bundle(
    monkeypatch, mutation, validation_valid,
) -> None:
    project_id = "verified-cross-phase"
    contract_digest = "sha256:" + ("c" * 64)
    bundle = {"schema_version": 3, "tasks": [{"task_id": "upstream-task"}]}
    bundle_digest = routes_phases._phase_evidence_bundle_digest(bundle)
    phase = {
        "phase_id": "phase-1", "user_confirmed": True,
        "execution_generation": "generation-1",
        "execution_artifact_baseline_digest": "sha256:" + ("d" * 64),
    }
    completion = {
        "project_id": project_id, "phase_id": "phase-1",
        "execution_generation": "generation-1",
        "contract_digest": contract_digest, "requirements_revision": 4,
        "artifact_baseline_digest": "sha256:" + ("d" * 64),
        "bundle_digest_version": 2,
        "bundle_digest": bundle_digest, "task_ids": ["upstream-task"],
    }
    for key, value in mutation.items():
        (phase if key == "execution_generation" else completion)[key] = value
    phase["validated_completion_receipt"] = completion
    monkeypatch.setattr(
        routes_phases, "_locked_phase_evidence_bundle",
        lambda *_args: ([], bundle),
    )
    monkeypatch.setattr(
        routes_phases, "validate_phase_completion",
        lambda *_args: SimpleNamespace(valid=validation_valid),
    )
    assert routes_phases._verified_completed_task_ids(
        SimpleNamespace(project_id=project_id),
        SimpleNamespace(phases=[phase]),
        project_id=project_id,
        contract_digest=contract_digest,
        requirements_revision=4,
    ) == []


def test_cross_phase_unlock_accepts_current_validated_completion_bundle(
    monkeypatch,
) -> None:
    project_id = "verified-cross-phase-positive"
    contract_digest = "sha256:" + ("e" * 64)
    baseline = "sha256:" + ("f" * 64)
    bundle = {"schema_version": 3, "tasks": [{"task_id": "upstream-task"}]}
    bundle_digest = routes_phases._phase_evidence_bundle_digest(bundle)
    phase = {
        "phase_id": "phase-1", "user_confirmed": True,
        "execution_generation": "generation-1",
        "execution_artifact_baseline_digest": baseline,
        "validated_completion_receipt": {
            "project_id": project_id, "phase_id": "phase-1",
                "execution_generation": "generation-1",
                "contract_digest": contract_digest, "requirements_revision": 4,
                "artifact_baseline_digest": baseline,
                "bundle_digest_version": 2,
                "bundle_digest": bundle_digest, "task_ids": ["upstream-task"],
        },
    }
    monkeypatch.setattr(
        routes_phases, "_locked_phase_evidence_bundle",
        lambda *_args: ([], bundle),
    )
    monkeypatch.setattr(
        routes_phases, "validate_phase_execution_evidence",
        lambda *_args: SimpleNamespace(valid=True),
    )
    assert routes_phases._verified_completed_task_ids(
        SimpleNamespace(project_id=project_id),
        SimpleNamespace(phases=[phase]),
        project_id=project_id,
        contract_digest=contract_digest,
        requirements_revision=4,
    ) == ["upstream-task"]


def test_server_produces_typed_criterion_evidence_without_agent_claims() -> None:
    phase_id = "phase-evidence"
    generation = "generation-evidence"
    task = {
        "task_id": "task-1",
        "acceptance_criteria": [
            "pytest suite passes",
            "GET /health API returns 200",
            "manual behavior review approved",
            "backend/app.py is delivered",
        ],
    }
    receipt = {
        "status": "succeeded",
        "completion_run_id": "run-task-1",
        "execution_generation": generation,
        "required_files": ["backend/app.py"],
        "result": {
            "delivery_evidence": {
                "artifact_digest": "sha256:" + ("9" * 64),
            },
        },
    }
    phase = {
        "phase_id": phase_id,
        "execution_generation": generation,
        "execution_contract_digest": "sha256:" + ("a" * 64),
        "execution_requirements_revision": 1,
        "execution_artifact_baseline_digest": "sha256:" + ("8" * 64),
        "acceptance_criteria": ["manual phase review approved"],
        "human_acceptance_observations": [
            {
                "ledger_id": "human-task-observation-1",
                "task_id": "task-1",
                "task_run_id": "run-task-1",
                "criterion_id": "task-1:acceptance:3",
                "criterion": "manual behavior review approved",
                "covered_criterion_ids": ["task-1:acceptance:3"],
                "execution_generation": generation,
                "artifact_generation": generation,
                "artifact_digest": "sha256:" + ("9" * 64),
                "actor_id": "user-1",
                "observation": "Reviewed the locked behavior",
                "result": "passed",
            },
            {
                "ledger_id": "human-phase-observation-1",
                "task_id": phase_id,
                "task_run_id": f"phase:{phase_id}:confirmation",
                "criterion_id": f"{phase_id}:acceptance:1",
                "criterion": "manual phase review approved",
                "covered_criterion_ids": [f"{phase_id}:acceptance:1"],
                "execution_generation": generation,
                "artifact_generation": generation,
                "artifact_digest": "sha256:" + ("8" * 64),
                "actor_id": "user-1",
                "observation": "Reviewed the phase acceptance",
                "result": "passed",
            },
        ],
        "pre_qa_result": {
            "passed": True,
            "evidence": [
                {
                    "kind": "test", "gate_id": "pytest",
                    "command": "pytest -q", "exit_code": 0,
                    "passed": True,
                    "task_id": "task-1",
                    "task_run_id": "run-task-1",
                    "execution_generation": generation,
                    "covered_criterion_ids": ["task-1:acceptance:1"],
                    "criterion_bindings": [{
                        "task_id": "task-1",
                        "task_run_id": "run-task-1",
                        "execution_generation": generation,
                        "criterion_id": "task-1:acceptance:1",
                    }],
                    "log_digest": "sha256:" + ("1" * 64),
                    "log_excerpt": "12 passed",
                },
                {
                    "kind": "api", "gate_id": "health",
                    "command": "GET /health", "exit_code": 0,
                    "passed": True,
                    "task_id": "task-1",
                    "task_run_id": "run-task-1",
                    "execution_generation": generation,
                    "covered_criterion_ids": ["task-1:acceptance:2"],
                    "criterion_bindings": [{
                        "task_id": "task-1",
                        "task_run_id": "run-task-1",
                        "execution_generation": generation,
                        "criterion_id": "task-1:acceptance:2",
                    }],
                    "log_digest": "sha256:" + ("2" * 64),
                    "status_code": 200, "endpoint": "/health",
                    "assertions": [{"name": "healthy", "passed": True}],
                },
            ],
        },
    }
    agent = {
        "id": "agent-1", "phase_id": phase_id,
        "locked_tasks": [task],
        "task_execution_receipts": {"task-1": receipt},
    }
    ctx = SimpleNamespace(
        project_id="project-evidence",
        owner_user_id="user-1",
        agents={"agent-1": agent},
        supervisor_quality_runs={
            phase_id: {"status": "completed", "completion_gate": {"passed": True}},
        },
    )

    routes_phases._record_server_acceptance_evidence(ctx, phase)
    routes_phases._record_user_confirmation_evidence(ctx, phase)

    bindings = {
        item["criterion_id"]: item["record"]
        for item in receipt["criterion_evidence"]
    }
    assert bindings["task-1:acceptance:1"]["producer"] == "metis.runner"
    assert bindings["task-1:acceptance:1"]["kind"] == "command"
    assert (
        bindings["task-1:acceptance:2"]["producer"]
        == "metis.runtime_acceptance"
    )
    assert bindings["task-1:acceptance:2"]["kind"] == "api"
    assert (
        bindings["task-1:acceptance:3"]["producer"]
        == "metis.user_confirmation"
    )
    assert "task-1:acceptance:4" not in bindings
    phase_human_record = (
        ctx.supervisor_quality_runs[phase_id]["phase_evidence"][0]["record"]
    )
    assert {
        record["evidence_id"]
        for record in phase["authoritative_criterion_evidence"]
    } == {
        bindings["task-1:acceptance:1"]["evidence_id"],
        bindings["task-1:acceptance:2"]["evidence_id"],
        bindings["task-1:acceptance:3"]["evidence_id"],
        phase_human_record["evidence_id"],
    }


def _server_evidence_fixture(criteria, evidence):
    phase_id = "phase-evidence-binding"
    generation = "generation-evidence-binding"
    receipt = {
        "status": "succeeded",
        "completion_run_id": "run-current",
        "execution_generation": generation,
        "required_files": [],
        "result": {},
    }
    phase = {
        "phase_id": phase_id,
        "execution_generation": generation,
        "execution_contract_digest": "sha256:" + ("b" * 64),
        "execution_requirements_revision": 2,
        "acceptance_criteria": [],
        "pre_qa_result": {"passed": True, "evidence": evidence},
    }
    ctx = SimpleNamespace(
        project_id="project-evidence-binding",
        agents={
            "agent-1": {
                "id": "agent-1",
                "phase_id": phase_id,
                "locked_tasks": [{
                    "task_id": "task-1",
                    "acceptance_criteria": criteria,
                }],
                "task_execution_receipts": {"task-1": receipt},
            },
        },
        supervisor_quality_runs={},
    )
    return ctx, phase, receipt


def test_same_kind_runner_record_covers_only_declared_criterion() -> None:
    generation = "generation-evidence-binding"
    ctx, phase, receipt = _server_evidence_fixture(
        ["pytest unit suite passes", "pytest integration suite passes"],
        [{
            "kind": "test",
            "gate_id": "pytest-unit",
            "command": "pytest -q tests/unit",
            "exit_code": 0,
            "passed": True,
            "task_id": "task-1",
            "task_run_id": "run-current",
            "execution_generation": generation,
            "covered_criterion_ids": ["task-1:acceptance:1"],
            "criterion_bindings": [{
                "task_id": "task-1",
                "task_run_id": "run-current",
                "execution_generation": generation,
                "criterion_id": "task-1:acceptance:1",
            }],
            "log_digest": "sha256:" + ("3" * 64),
            "log_excerpt": "8 passed",
        }],
    )

    routes_phases._record_server_acceptance_evidence(ctx, phase)

    bindings = receipt.get("criterion_evidence") or []
    assert [row["criterion_id"] for row in bindings] == [
        "task-1:acceptance:1"
    ]


def test_server_record_from_old_task_run_cannot_cover_current_criterion() -> None:
    generation = "generation-evidence-binding"
    ctx, phase, receipt = _server_evidence_fixture(
        ["pytest suite passes"],
        [{
            "kind": "test",
            "gate_id": "pytest",
            "command": "pytest -q",
            "exit_code": 0,
            "passed": True,
            "task_id": "task-1",
            "task_run_id": "run-old",
            "execution_generation": generation,
            "covered_criterion_ids": ["task-1:acceptance:1"],
            "criterion_bindings": [{
                "task_id": "task-1",
                "task_run_id": "run-old",
                "execution_generation": generation,
                "criterion_id": "task-1:acceptance:1",
            }],
            "log_digest": "sha256:" + ("4" * 64),
            "log_excerpt": "8 passed",
        }],
    )

    routes_phases._record_server_acceptance_evidence(ctx, phase)

    assert receipt.get("criterion_evidence") in (None, [])
    assert phase.get("authoritative_criterion_evidence") == []


def test_api_record_cannot_cover_declared_id_for_different_endpoint() -> None:
    generation = "generation-evidence-binding"
    ctx, phase, receipt = _server_evidence_fixture(
        ["GET /health API returns 200", "GET /ready API returns 200"],
        [{
            "kind": "api",
            "gate_id": "health",
            "command": "GET /health",
            "exit_code": 0,
            "passed": True,
            "task_id": "task-1",
            "task_run_id": "run-current",
            "execution_generation": generation,
            "covered_criterion_ids": ["task-1:acceptance:2"],
            "criterion_bindings": [{
                "task_id": "task-1",
                "task_run_id": "run-current",
                "execution_generation": generation,
                "criterion_id": "task-1:acceptance:2",
            }],
            "log_digest": "sha256:" + ("5" * 64),
            "status_code": 200,
            "endpoint": "/health",
            "assertions": [{"name": "healthy", "passed": True}],
        }],
    )

    routes_phases._record_server_acceptance_evidence(ctx, phase)

    assert receipt.get("criterion_evidence") in (None, [])
    assert phase.get("authoritative_criterion_evidence") == []


def test_one_confirmation_observation_cannot_pass_multiple_semantic_criteria() -> None:
    generation = "generation-human-binding"
    digest = "sha256:" + ("6" * 64)
    phase = {
        "phase_id": "phase-human-binding",
        "execution_generation": generation,
        "execution_contract_digest": "sha256:" + ("7" * 64),
        "execution_requirements_revision": 1,
        "acceptance_criteria": [],
        "human_acceptance_observations": [{
            "ledger_id": "one-click-observation",
            "task_id": "task-1",
            "task_run_id": "run-current",
            "criterion_id": "task-1:acceptance:1",
            "criterion": "manual accessibility review",
            "covered_criterion_ids": [
                "task-1:acceptance:1",
                "task-1:acceptance:2",
            ],
            "execution_generation": generation,
            "artifact_generation": generation,
            "artifact_digest": digest,
            "actor_id": "user-1",
            "observation": "Clicked confirm once",
            "result": "passed",
        }],
    }
    receipt = {
        "status": "succeeded",
        "completion_run_id": "run-current",
        "execution_generation": generation,
        "required_files": [],
        "result": {
            "delivery_evidence": {"artifact_digest": digest},
        },
    }
    ctx = SimpleNamespace(
        project_id="project-human-binding",
        owner_user_id="user-1",
        agents={"agent-1": {
            "id": "agent-1",
            "phase_id": phase["phase_id"],
            "locked_tasks": [{
                "task_id": "task-1",
                "acceptance_criteria": [
                    "manual accessibility review",
                    "manual usability review",
                ],
            }],
            "task_execution_receipts": {"task-1": receipt},
        }},
        supervisor_quality_runs={},
    )

    routes_phases._record_user_confirmation_evidence(ctx, phase)

    assert receipt.get("criterion_evidence") in (None, [])
    assert phase.get("authoritative_criterion_evidence") == []


def test_unknown_roles_are_not_collapsed_into_backend() -> None:
    phase = {
        "roles_needed": ["Domain Curator", "Policy Steward"],
        "expert_requirements": [
            {"task_id": "one", "required_role": "Domain Curator"},
            {"task_id": "two", "required_role": "Policy Steward"},
        ],
    }

    assert routes_phases._phase_role_expert_type("Backend Developer") == "backend"
    assert routes_phases._phase_role_expert_type("Developer") == "fullstack_engineer"
    assert routes_phases._phase_role_expert_type("开发工程师") == "fullstack_engineer"
    assert routes_phases._phase_role_expert_type("Domain Curator") == ""
    assert routes_phases._unsupported_phase_roles(phase) == [
        "Domain Curator", "Policy Steward",
    ]


def test_pre_qa_failure_reopens_completed_responsible_agent(monkeypatch) -> None:
    agent = {
        "id": "backend-agent",
        "phase_id": "phase-1",
        "status": "completed",
        "progress": 100,
        "output_files": ["backend/src/auth.js"],
    }
    ctx = SimpleNamespace(agents={"backend-agent": agent})
    monkeypatch.setattr(
        routes_phases,
        "_match_issue_agent",
        lambda *_args, **_kwargs: agent,
    )

    routes_phases._mark_pre_qa_agents_for_repair(
        ctx,
        "phase-1",
        ["backend-agent"],
        [{"path": "backend/src/auth.js", "message": "JWT fallback secret"}],
    )

    assert agent["status"] == "fix_required"
    assert agent["progress"] == 0
    assert agent["output_files"] == ["backend/src/auth.js"]
    assert "JWT fallback secret" in agent["fix_task"]
    assert routes_phases._supervisor_agent_status(agent) == "pending"


def test_pre_qa_repair_contract_returns_source_location_and_protected_files(
    monkeypatch, tmp_path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "print('ok')\n# TODO: remove me\n", encoding="utf-8",
    )
    owner = {
        "id": "agent-1", "phase_id": "phase-1", "status": "completed",
    }
    ctx = SimpleNamespace(
        project_id="repair-contract-project",
        workspace=tmp_path,
        agents={owner["id"]: owner},
        subprojects=[],
    )
    phase = {
        "phase_id": "phase-1",
        "phase_plan": {
            "schema_version": "phase-plan/v1",
            "tasks": [{
                "task_id": "task-app",
                "name": "实现应用",
                "objective": "程序输出 ok",
                "functional_details": ["不得包含占位符"],
                "implementation": "使用 Python",
                "dependencies": ["task-config"],
                "acceptance_criteria": ["运行输出 ok"],
            }],
        },
    }
    pm = SimpleNamespace(get_phase=lambda _phase_id: phase)
    monkeypatch.setitem(
        routes_phases._phase_managers, ctx.project_id, pm,
    )
    monkeypatch.setattr(
        routes_phases, "_match_issue_agent",
        lambda *_args, **_kwargs: owner,
    )
    monkeypatch.setattr(
        routes_phases, "load_phase_qa_scope",
        lambda **_kwargs: {
            "files": [
                {
                    "path": "src/app.py", "task_id": "task-app",
                    "sha256": "bad-hash",
                },
                {
                    "path": "config.json", "task_id": "task-config",
                    "sha256": "config-hash",
                },
                {
                    "path": "README.md", "task_id": "task-docs",
                    "sha256": "readme-hash",
                },
            ],
        },
    )

    routes_phases._mark_pre_qa_agents_for_repair(
        ctx,
        "phase-1",
        [owner["id"]],
        [{
            "path": "src/app.py",
            "code": "forbidden_placeholder",
            "gate": "contract",
            "message": "required file contains placeholder",
            "expected": "no placeholder",
        }],
    )

    assert "pre_qa_repair_contract" not in owner
    payload = json.loads(owner["fix_task"].split("\n", 1)[1])
    assert payload["editable_files"] == ["src/app.py"]
    assert payload["issues"][0]["location"] == {"line": 2, "column": None}
    assert payload["issues"][0]["excerpt"] == "# TODO: remove me"
    assert payload["issues"][0]["source_task"]["objective"] == "程序输出 ok"
    assert payload["issues"][0]["source_task"]["acceptance_criteria"] == [
        "运行输出 ok"
    ]
    assert payload["readonly_dependency_files"] == ["config.json"]
    assert payload["protected_passed_files"] == [
        {"path": "README.md", "sha256": "readme-hash"},
        {"path": "config.json", "sha256": "config-hash"},
    ]
    assert '"code": "forbidden_placeholder"' in owner["fix_task"]


def test_pre_qa_runtime_repair_packet_preserves_exact_http_diagnostic(
    monkeypatch, tmp_path,
) -> None:
    target = tmp_path / "routes" / "tasks.js"
    target.parent.mkdir(parents=True)
    target.write_text(
        "router.post('/', (req, res) => { const { title } = req.body; });\n",
        encoding="utf-8",
    )
    owner = {
        "id": "api-agent",
        "phase_id": "phase-2",
        "status": "completed",
        "subproject_id": "sp-api",
    }
    ctx = SimpleNamespace(
        project_id="precise-http-repair",
        workspace=tmp_path,
        agents={owner["id"]: owner},
        subprojects=[],
    )
    phase = {
        "phase_id": "phase-2",
        "phase_plan": {
            "schema_version": "phase-plan/v1",
            "tasks": [{
                "task_id": "task-api",
                "name": "Create task API",
                "objective": "POST /tasks returns 201",
                "functional_details": ["Accept JSON title"],
                "implementation": "Express router",
                "dependencies": ["task-shell"],
                "acceptance_criteria": ["POST /tasks returns 201"],
            }],
        },
    }
    monkeypatch.setitem(
        routes_phases._phase_managers,
        ctx.project_id,
        SimpleNamespace(get_phase=lambda _phase_id: phase),
    )
    monkeypatch.setattr(
        routes_phases, "_match_issue_agent",
        lambda *_args, **_kwargs: owner,
    )
    monkeypatch.setattr(
        routes_phases,
        "load_phase_qa_scope",
        lambda **_kwargs: {
            "files": [{
                "path": "routes/tasks.js",
                "task_id": "task-api",
                "sha256": "fixture",
            }],
        },
    )

    routes_phases._mark_pre_qa_agents_for_repair(
        ctx,
        "phase-2",
        [owner["id"]],
        [{
            "path": "routes/tasks.js",
            "code": "api_runtime_acceptance_failed",
            "gate": "api",
            "message": (
                "runtime HTTP check POST /tasks failed: "
                "expected [201], got 500"
            ),
            "method": "POST",
            "endpoint": "/tasks",
            "actual": "HTTP 500: req.body is undefined",
            "expected": "[201]",
            "fix_hint": (
                "Install JSON body parsing before this route handler "
                "(for Express, use express.json() before reading req.body)."
            ),
        }],
    )

    payload = json.loads(owner["fix_task"].split("\n", 1)[1])
    issue = payload["issues"][0]
    assert payload["editable_files"] == ["routes/tasks.js"]
    assert issue["method"] == "POST"
    assert issue["endpoint"] == "/tasks"
    assert issue["actual"] == "HTTP 500: req.body is undefined"
    assert issue["expected"] == "[201]"
    assert "express.json()" in issue["fix_hint"]
    assert issue["source_task"]["task_id"] == "task-api"


def test_pre_qa_failure_reopens_confirmed_earlier_phase_file_owner(
    monkeypatch, tmp_path,
) -> None:
    owner = {
        "id": "backend-owner",
        "phase_id": "phase-1",
        "status": "completed",
        "progress": 100,
        "allowed_path_prefixes": ["package.json"],
        "output_files": ["package.json"],
    }
    ctx = SimpleNamespace(
        project_id="cross-phase-pre-qa",
        workspace=tmp_path,
        agents={owner["id"]: owner},
        subprojects=[],
    )
    monkeypatch.setattr(
        routes_phases,
        "_match_issue_agent",
        lambda *_args, **_kwargs: owner,
    )

    routes_phases._mark_pre_qa_agents_for_repair(
        ctx,
        "phase-3",
        [owner["id"]],
        [{
            "path": "package.json",
            "gate": "install-root",
            "message": "native dependency is incompatible",
        }],
    )

    assert owner["status"] == "fix_required"
    assert owner["pre_qa_previous_status"] == "completed"
    assert "package.json" in owner["fix_task"]


@pytest.mark.parametrize(
    ("gate", "path"),
    [
        ("test-backend", "backend"),
        ("test-root", "."),
        ("health", "."),
        ("api-contract", "backend"),
    ],
)
def test_backend_pre_qa_and_runtime_gates_never_fall_through_to_devops(
    gate, path,
) -> None:
    devops = {
        "id": "devops-agent",
        "phase_id": "phase-1",
        "expert_type": "devops",
        "allowed_path_prefixes": ["package.json", "start.js", "Dockerfile"],
    }
    backend = {
        "id": "backend-agent",
        "phase_id": "phase-1",
        "expert_type": "backend",
        "allowed_path_prefixes": ["backend/"],
    }
    # Preserve the problematic insertion order from the live project: before
    # the fix, the generic fallback selected DevOps for every case below.
    ctx = SimpleNamespace(
        agents={devops["id"]: devops, backend["id"]: backend},
    )

    selected = routes_phases._match_issue_agent(
        ctx,
        "phase-1",
        {
            "code": "command_gate_failed",
            "gate": gate,
            "path": path,
            "file_path": path,
            "message": "command exited with 1",
        },
    )

    assert selected["id"] == backend["id"]


def test_deterministic_pre_qa_repair_removes_jwt_fallback_and_fails_closed(tmp_path) -> None:
    auth_path = tmp_path / "backend" / "src" / "auth.ts"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(
        "import jwt from 'jsonwebtoken';\n"
        "const secret = process.env.JWT_SECRET || 'dev-secret';\n"
        "export const sign = (payload: object) => jwt.sign(payload, secret);\n",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        project_id="deterministic-jwt-repair",
        workspace=tmp_path,
    )
    registry_entry = {
        "file_path": "backend/src/auth.ts",
        "agent_id": "backend-agent",
        "phase_id": "phase-2",
        "sha256": "sha256:stale",
    }
    routes_phases._phase_managers[ctx.project_id] = SimpleNamespace(
        file_registry={"backend/src/auth.ts": registry_entry},
    )
    try:
        first = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [
            {
                "code": "jwt_default_secret",
                "path": "backend/src/auth.ts",
                "message": "fallback secret",
            },
            {
                "code": "jwt_not_fail_closed",
                "path": "backend/src/auth.ts",
                "message": "JWT_SECRET must be required",
            },
        ])
        second = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [
            {"code": "jwt_not_fail_closed", "path": "backend/src/auth.ts"},
        ])
    finally:
        routes_phases._phase_managers.pop(ctx.project_id, None)
    content = auth_path.read_text(encoding="utf-8")

    assert len(first) == 2
    assert second == []
    assert "process.env.JWT_SECRET ||" not in content
    assert "if (!process.env.JWT_SECRET)" in content
    assert "throw new Error('JWT_SECRET is required')" in content
    assert registry_entry["sha256"] == (
        "sha256:" + hashlib.sha256(auth_path.read_bytes()).hexdigest()
    )
    assert registry_entry["generation_source"] == "deterministic_pre_qa_repair"


def test_deterministic_pre_qa_repair_removes_invalid_child_postinstall(tmp_path) -> None:
    package_path = tmp_path / "package.json"
    package_path.write_text(
        '{"scripts":{"postinstall":"npm --prefix backend postinstall","test":"echo ok"}}',
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        project_id="deterministic-child-postinstall",
        workspace=tmp_path,
    )

    evidence = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [
        {
            "code": "command_gate_failed",
            "gate": "install-root",
            "path": ".",
            "message": "command exited with 1",
        },
    ])
    updated = package_path.read_text(encoding="utf-8")

    assert len(evidence) == 1
    assert evidence[0]["path"] == "package.json"
    assert "postinstall" not in updated
    assert '"test": "echo ok"' in updated


def test_verified_supervisor_scope_rebinds_task_delivery_evidence(
    monkeypatch, tmp_path,
) -> None:
    relative = "backend/src/auth.js"
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_text("export const auth = true;\n", encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    receipt = {
        "status": "succeeded",
        "completion_run_id": "run-1",
        "required_files": [relative],
        "result": {"delivery_evidence": {
            "run_id": "run-1",
            "files": [{"path": relative, "sha256": "stale", "size": 1}],
        }},
    }
    ctx = SimpleNamespace(
        project_id="verified-scope-rebind",
        workspace=tmp_path,
        agents={"backend-agent": {
            "id": "backend-agent",
            "phase_id": "phase-2",
            "task_execution_receipts": {"phase-2-task-1": receipt},
        }},
    )
    manager = SimpleNamespace(file_registry={relative: {
        "file_path": relative,
        "agent_id": "backend-agent",
        "phase_id": "phase-2",
        "sha256": "sha256:stale",
    }})
    monkeypatch.setitem(
        routes_phases._phase_managers,
        ctx.project_id,
        manager,
    )

    routes_phases._reconcile_verified_supervisor_registry(
        ctx,
        "phase-2",
        {"rounds": [{
            "state": "verified",
            "scope_snapshot": {"delivery_manifest": {"files": [{
                "path": relative,
                "sha256": digest,
                "size": target.stat().st_size,
            }]}},
        }]},
    )

    rebound = receipt["result"]["delivery_evidence"]
    assert rebound["run_id"] == "run-1"
    assert rebound["files"] == [{
        "path": relative,
        "sha256": digest,
        "size": target.stat().st_size,
    }]
    assert manager.file_registry[relative]["sha256"] == f"sha256:{digest}"


@pytest.mark.parametrize("gate", ["install-root", "test-root", "build-root"])
def test_deterministic_pre_qa_repair_materializes_missing_root_package(
    monkeypatch, tmp_path, gate
) -> None:
    devops = {
        "id": "devops-agent",
        "phase_id": "phase-1",
        "role": "devops",
    }
    ctx = SimpleNamespace(
        project_id="project-template-root",
        name="Template Project",
        workspace=tmp_path,
        agents={"devops-agent": devops},
    )
    registered = []
    monkeypatch.setitem(
        routes_phases._phase_managers,
        ctx.project_id,
        SimpleNamespace(register_file=lambda *args: registered.append(args)),
    )
    monkeypatch.setattr(
        routes_phases,
        "_match_issue_agent",
        lambda *_args, **_kwargs: devops,
    )

    evidence = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [{
        "code": "command_gate_failed",
        "gate": gate,
        "phase_id": "phase-1",
        "message": "package.json is missing",
    }])

    package = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    root_evidence = [item for item in evidence if item["path"] == "package.json"]
    assert package["name"] == "Template Project"
    assert package["scripts"] == {
        "build": "npm --prefix frontend run build",
        "start": "npm --prefix backend start",
        "test": "npm --prefix backend test",
    }
    assert root_evidence[0]["issue_code"] == "missing_root_package"
    assert root_evidence[0]["template"] == "node-react-express"
    assert registered[0][:3] == ("package.json", "devops-agent", "devops")


def test_deterministic_pre_qa_repair_never_overwrites_existing_root_package(
    tmp_path,
) -> None:
    package_path = tmp_path / "package.json"
    original = '{"name":"custom","scripts":{"test":"custom"}}\n'
    package_path.write_text(original, encoding="utf-8")

    evidence = routes_phases._apply_deterministic_pre_qa_repairs(
        SimpleNamespace(
            project_id="deterministic-root-package",
            workspace=tmp_path,
        ),
        [{
            "code": "command_gate_failed",
            "gate": "test-root",
            "message": "package.json is missing",
        }],
    )

    assert evidence == []
    assert package_path.read_text(encoding="utf-8") == original


def test_deterministic_pre_qa_repair_adds_only_missing_supertest_dependency(
    tmp_path,
) -> None:
    package_path = tmp_path / "backend" / "package.json"
    package_path.parent.mkdir(parents=True)
    package_path.write_text(json.dumps({
        "dependencies": {"express": "^4.18.0"},
        "devDependencies": {"jest": "^29.0.0"},
    }), encoding="utf-8")
    test_path = tmp_path / "backend" / "src" / "app.test.ts"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "import request from 'supertest';\nexport { request };\n",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        project_id="deterministic-supertest",
        workspace=tmp_path,
    )

    first = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [{
        "code": "command_gate_failed",
        "gate": "test-backend",
        "message": "command exited with 1",
    }])
    second = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [{
        "code": "command_gate_failed",
        "gate": "test-backend",
        "message": "command exited with 1",
    }])
    updated = json.loads(package_path.read_text(encoding="utf-8"))

    assert [item["path"] for item in first] == ["backend/package.json"]
    assert first[0]["issue_code"] == "missing_supertest_dependency"
    assert updated["dependencies"] == {"express": "^4.18.0"}
    assert updated["devDependencies"] == {
        "jest": "^29.0.0",
        "supertest": "^7.0.0",
    }
    assert second == []


def test_deterministic_pre_qa_repair_creates_backend_smoke_test(monkeypatch, tmp_path) -> None:
    (tmp_path / "backend" / "src").mkdir(parents=True)
    ctx = SimpleNamespace(
        project_id="project-1",
        workspace=tmp_path,
        agents={"backend-agent": {
            "id": "backend-agent",
            "phase_id": "phase-1",
            "role": "backend",
        }},
    )
    registered = []
    monkeypatch.setitem(
        routes_phases._phase_managers,
        "project-1",
        SimpleNamespace(register_file=lambda *args: registered.append(args)),
    )
    monkeypatch.setattr(
        routes_phases,
        "_match_issue_agent",
        lambda *_args, **_kwargs: ctx.agents["backend-agent"],
    )

    evidence = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [
        {
            "code": "command_gate_failed",
            "gate": "test-backend",
            "path": "backend",
            "phase_id": "phase-1",
        },
    ])
    target = tmp_path / "backend" / "src" / "__tests__" / "smoke.test.ts"
    second = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [
        {"code": "command_gate_failed", "gate": "test-backend", "path": "backend"},
    ])

    assert len(evidence) == 1
    assert target.is_file()
    content = target.read_text(encoding="utf-8")
    assert content.startswith(
        "// @ts-nocheck"
    )
    assert "expect(true).toBe(true)" in content
    assert registered[0][0] == "backend/src/__tests__/smoke.test.ts"
    assert registered[0][1] == "backend-agent"
    assert second == []


def test_deterministic_pre_qa_repair_fixes_root_build_script(tmp_path) -> None:
    package_path = tmp_path / "package.json"
    package_path.write_text(
        '{"scripts":{"build":"npm --prefix backend build","test":"npm --prefix backend test"}}',
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        project_id="deterministic-root-build",
        workspace=tmp_path,
    )

    evidence = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [
        {"code": "command_gate_failed", "gate": "build-root", "path": "."},
    ])
    updated = package_path.read_text(encoding="utf-8")

    assert {entry["path"] for entry in evidence} == {"package.json", "backend/tsconfig.json"}
    assert "npm --prefix backend run build" in updated
    assert "npm --prefix backend test" in updated
    assert (tmp_path / "backend" / "tsconfig.json").is_file()


def test_deterministic_root_build_migrates_legacy_smoke_test_globals(
    tmp_path,
) -> None:
    (tmp_path / "backend" / "src" / "__tests__").mkdir(parents=True)
    (tmp_path / "package.json").write_text(
        '{"scripts":{"build":"npm --prefix backend run build"}}',
        encoding="utf-8",
    )
    (tmp_path / "backend" / "tsconfig.json").write_text(
        '{"compilerOptions":{"module":"CommonJS"}}',
        encoding="utf-8",
    )
    smoke = tmp_path / "backend" / "src" / "__tests__" / "smoke.test.ts"
    smoke.write_text(
        "describe('backend smoke test', () => {\n"
        "  it('runs the automated backend test harness', () => {\n"
        "    expect(true).toBe(true);\n"
        "  });\n"
        "});\n",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        project_id="deterministic-typed-smoke",
        workspace=tmp_path,
    )
    issue = {"code": "command_gate_failed", "gate": "build-root", "path": "."}

    first = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [issue])
    second = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [issue])

    assert [item["issue_code"] for item in first] == [
        "normalize_jest_smoke_globals",
    ]
    assert smoke.read_text(encoding="utf-8").startswith(
        "// @ts-nocheck"
    )
    assert second == []


def test_deterministic_backend_test_removes_runtime_jest_globals_import(
    tmp_path,
) -> None:
    (tmp_path / "backend" / "src" / "__tests__").mkdir(parents=True)
    smoke = tmp_path / "backend" / "src" / "__tests__" / "smoke.test.ts"
    smoke.write_text(
        "import { describe, expect, it } from '@jest/globals';\n\n"
        "describe('backend smoke test', () => {\n"
        "  it('runs the automated backend test harness', () => {\n"
        "    expect(true).toBe(true);\n"
        "  });\n"
        "});\n",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        project_id="deterministic-cjs-smoke",
        workspace=tmp_path,
    )
    issue = {"code": "command_gate_failed", "gate": "test-backend", "path": "backend"}

    first = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [issue])
    second = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [issue])

    assert [item["issue_code"] for item in first] == [
        "normalize_jest_smoke_globals",
    ]
    assert smoke.read_text(encoding="utf-8").startswith(
        "// @ts-nocheck"
    )
    assert second == []


def test_deterministic_backend_test_ignores_compiled_smoke_copy(
    tmp_path,
) -> None:
    (tmp_path / "backend" / "src" / "__tests__").mkdir(parents=True)
    smoke = tmp_path / "backend" / "src" / "__tests__" / "smoke.test.ts"
    smoke.write_text(
        "// @ts-nocheck\n\n"
        "describe('backend smoke test', () => {\n"
        "  it('runs the automated backend test harness', () => {\n"
        "    expect(true).toBe(true);\n"
        "  });\n"
        "});\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "backend" / "package.json"
    package_path.write_text(
        json.dumps({
            "type": "module",
            "scripts": {
                "test": "NODE_OPTIONS=--experimental-vm-modules jest",
            },
        }),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        project_id="deterministic-dist-smoke",
        workspace=tmp_path,
    )
    issue = {"code": "command_gate_failed", "gate": "test-backend", "path": "backend"}

    first = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [issue])
    second = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [issue])
    package_data = json.loads(package_path.read_text(encoding="utf-8"))

    assert [item["issue_code"] for item in first] == [
        "ignore_compiled_jest_tests",
    ]
    assert package_data["jest"]["testPathIgnorePatterns"] == ["/dist/"]
    assert second == []


def test_deterministic_pre_qa_repair_removes_unused_react_imports(tmp_path) -> None:
    layout = tmp_path / "frontend" / "src" / "Layout.tsx"
    context = tmp_path / "frontend" / "src" / "AuthContext.tsx"
    page = tmp_path / "frontend" / "src" / "DashboardPage.tsx"
    main = tmp_path / "frontend" / "src" / "main.tsx"
    layout.parent.mkdir(parents=True)
    layout.write_text("import React from 'react';\nexport default function Layout() { return <div />; }\n", encoding="utf-8")
    context.write_text("import React, { createContext } from 'react';\nexport const C = createContext(null);\n", encoding="utf-8")
    page.write_text("import React, { useState } from 'react';\nexport function P() { const [x] = useState(1); return <>{x}</>; }\n", encoding="utf-8")
    main.write_text(
        "import ReactDOM from 'react-dom/client';\nReactDOM.createRoot(el).render(<React.StrictMode />);\n",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        project_id="deterministic-react-imports",
        workspace=tmp_path,
    )

    evidence = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [
        {"code": "command_gate_failed", "gate": "build-frontend", "path": "frontend"},
    ])

    assert len(evidence) == 4
    assert "import React" not in layout.read_text(encoding="utf-8")
    assert context.read_text(encoding="utf-8").startswith("import { createContext } from 'react';")
    assert page.read_text(encoding="utf-8").startswith("import { useState } from 'react';")
    assert main.read_text(encoding="utf-8").startswith("import React from 'react';")


def test_deterministic_pre_qa_repair_materializes_registers_and_is_idempotent(
    monkeypatch, tmp_path,
) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "package.json").write_text(
        '{"scripts":{"build":"tsc && vite build"}}',
        encoding="utf-8",
    )
    (frontend / "src" / "main.tsx").write_text(
        "export const app = true;\n",
        encoding="utf-8",
    )
    owner = {
        "id": "frontend-agent",
        "phase_id": "phase-2",
        "role": "frontend",
        "subproject_id": "web",
    }
    ctx = SimpleNamespace(
        project_id="deterministic-frontend-support",
        name="Deterministic Frontend",
        workspace=tmp_path,
        agents={owner["id"]: owner},
    )

    class RecordingPhaseManager:
        def __init__(self) -> None:
            self.calls = []
            self.file_registry = {}

        def register_file(
            self, path, agent_id, role, phase_id, subproject_id="",
        ) -> None:
            self.calls.append(
                (path, agent_id, role, phase_id, subproject_id),
            )
            self.file_registry[path] = {
                "agent_id": agent_id,
                "role": role,
                "phase_id": phase_id,
                "subproject_id": subproject_id,
            }

    manager = RecordingPhaseManager()
    monkeypatch.setitem(
        routes_phases._phase_managers,
        ctx.project_id,
        manager,
    )
    monkeypatch.setattr(
        routes_phases,
        "_match_issue_agent",
        lambda *_args, **_kwargs: owner,
    )
    issues = [
        {
            "code": "missing_typescript_config",
            "gate": "build-frontend",
            "path": "frontend/tsconfig.json",
            "phase_id": "phase-2",
        },
        {
            "code": "missing_vite_entry",
            "gate": "build-frontend",
            "path": "frontend/index.html",
            "phase_id": "phase-2",
        },
    ]

    first = routes_phases._apply_deterministic_pre_qa_repairs(ctx, issues)
    first_bytes = {
        path: (tmp_path / path).read_bytes()
        for path in ("frontend/tsconfig.json", "frontend/index.html")
    }
    second = routes_phases._apply_deterministic_pre_qa_repairs(ctx, issues)

    assert [item["path"] for item in first] == [
        "frontend/tsconfig.json",
        "frontend/index.html",
    ]
    assert [item["issue_code"] for item in first] == [
        "missing_frontend_tsconfig",
        "missing_frontend_index",
    ]
    assert all(item["template_version"] == 4 for item in first)
    assert manager.calls == [
        (
            "frontend/tsconfig.json",
            "frontend-agent",
            "frontend",
            "phase-2",
            "web",
        ),
        (
            "frontend/index.html",
            "frontend-agent",
            "frontend",
            "phase-2",
            "web",
        ),
    ]
    assert all(
        manager.file_registry[path]["generation_source"]
        == "deterministic_pre_qa_template"
        for path in first_bytes
    )
    assert all(
        manager.file_registry[path]["template_version"] == 4
        for path in first_bytes
    )
    assert second == []
    assert {
        path: (tmp_path / path).read_bytes()
        for path in first_bytes
    } == first_bytes


@pytest.mark.parametrize(
    "missing_dependency",
    ["phase_manager", "owner", "current_phase_owner"],
)
def test_deterministic_frontend_support_creation_fails_closed_without_provenance(
    monkeypatch, tmp_path, missing_dependency,
) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "package.json").write_text(
        '{"scripts":{"build":"tsc && vite build"}}',
        encoding="utf-8",
    )
    (frontend / "src" / "main.tsx").write_text(
        "export const app = true;\n",
        encoding="utf-8",
    )
    owner = {
        "id": "frontend-agent",
        "phase_id": "phase-2",
        "role": "frontend",
    }
    project_id = f"frontend-support-missing-{missing_dependency}"
    ctx = SimpleNamespace(
        project_id=project_id,
        name="Fail Closed Frontend",
        workspace=tmp_path,
        agents={owner["id"]: owner},
    )
    registrations = []
    manager = SimpleNamespace(
        file_registry={},
        register_file=lambda *args: registrations.append(args),
    )
    if missing_dependency in {"owner", "current_phase_owner"}:
        monkeypatch.setitem(routes_phases._phase_managers, project_id, manager)
        matched_owner = (
            {**owner, "phase_id": "phase-1"}
            if missing_dependency == "current_phase_owner"
            else None
        )
        monkeypatch.setattr(
            routes_phases,
            "_match_issue_agent",
            lambda *_args, **_kwargs: matched_owner,
        )
    else:
        monkeypatch.delitem(
            routes_phases._phase_managers,
            project_id,
            raising=False,
        )
        monkeypatch.setattr(
            routes_phases,
            "_match_issue_agent",
            lambda *_args, **_kwargs: owner,
        )

    evidence = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [
        {
            "code": "missing_typescript_config",
            "gate": "build-frontend",
            "path": "frontend/tsconfig.json",
            "phase_id": "phase-2",
        },
        {
            "code": "missing_vite_entry",
            "gate": "build-frontend",
            "path": "frontend/index.html",
            "phase_id": "phase-2",
        },
    ])

    assert evidence == []
    assert registrations == []
    assert not (frontend / "tsconfig.json").exists()
    assert not (frontend / "index.html").exists()


@pytest.mark.parametrize("gate", ["health", "api-contract", "docker-run"])
def test_deterministic_pre_qa_repair_creates_sqlite_parent_directory(
    tmp_path, gate
) -> None:
    db_path = tmp_path / "backend" / "src" / "db.js"
    db_path.parent.mkdir(parents=True)
    db_path.write_text(
        "import Database from 'better-sqlite3';\n"
        "import path from 'path';\n"
        "const DB_PATH = path.join('data', 'app.db');\n"
        "export function getDb() {\n"
        "  return new Database(DB_PATH);\n"
        "}\n",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        project_id=f"deterministic-sqlite-parent-{gate}",
        workspace=tmp_path,
    )

    first = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [{
        "code": "command_gate_failed",
        "gate": gate,
        "path": "backend",
    }])
    second = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [{
        "code": "command_gate_failed",
        "gate": gate,
        "path": "backend",
    }])
    updated = db_path.read_text(encoding="utf-8")

    assert len(first) == 1
    assert first[0]["issue_code"] == "missing_sqlite_parent_directory"
    assert second == []
    assert "import { mkdirSync } from 'fs';" in updated
    assert "import { dirname } from 'path';" in updated
    assert "mkdirSync(dirname(DB_PATH), { recursive: true });" in updated
    assert updated.index("mkdirSync(") < updated.index("new Database(DB_PATH)")


def test_deterministic_pre_qa_repair_resolves_sqlite_env_on_first_get_db(
    tmp_path,
) -> None:
    db_path = tmp_path / "backend" / "src" / "db.js"
    db_path.parent.mkdir(parents=True)
    db_path.write_text(
        "import Database from 'better-sqlite3';\n"
        "import path from 'path';\n"
        "const DB_PATH = process.env.DB_PATH || path.join('data', 'app.db');\n"
        "let db;\n"
        "export function getDb() {\n"
        "  if (!db) {\n"
        "    db = new Database(DB_PATH);\n"
        "  }\n"
        "  return db;\n"
        "}\n",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        project_id="deterministic-sqlite-get-db",
        workspace=tmp_path,
    )
    issue = {
        "code": "command_gate_failed",
        "gate": "api-contract",
        "path": "backend",
    }

    first = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [issue])
    second = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [issue])
    updated = db_path.read_text(encoding="utf-8")

    assert len(first) == 1
    assert second == []
    assert "const DEFAULT_DB_PATH = path.join('data', 'app.db');" in updated
    assert (
        "const resolvedDbPath = process.env.DB_PATH || DEFAULT_DB_PATH;"
        in updated
    )
    assert "mkdirSync(dirname(resolvedDbPath), { recursive: true });" in updated
    assert "new Database(resolvedDbPath)" in updated
    assert updated.index("process.env.DB_PATH") > updated.index("getDb()")


def test_deterministic_pre_qa_repair_upgrades_static_sqlite_mkdir(
    tmp_path,
) -> None:
    db_path = tmp_path / "backend" / "src" / "db.js"
    db_path.parent.mkdir(parents=True)
    db_path.write_text(
        "import { mkdirSync } from 'fs';\n"
        "import { dirname } from 'path';\n"
        "import Database from 'better-sqlite3';\n"
        "import path from 'path';\n"
        "const DB_PATH = process.env.SQLITE_PATH || path.join('data', 'app.db');\n"
        "export function getDb() {\n"
        "  mkdirSync(dirname(DB_PATH), { recursive: true });\n"
        "  return new Database(DB_PATH);\n"
        "}\n",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        project_id="deterministic-sqlite-mkdir",
        workspace=tmp_path,
    )
    issue = {
        "code": "command_gate_failed",
        "gate": "health",
        "path": "backend",
    }

    first = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [issue])
    second = routes_phases._apply_deterministic_pre_qa_repairs(ctx, [issue])
    updated = db_path.read_text(encoding="utf-8")

    assert len(first) == 1
    assert second == []
    assert updated.count("mkdirSync") == 2  # one import and one invocation
    assert "process.env.SQLITE_PATH || DEFAULT_DB_PATH" in updated
    assert "dirname(DB_PATH)" not in updated
    assert "new Database(resolvedDbPath)" in updated


def test_deterministic_pre_qa_restore_completed_agent() -> None:
    agent = {
        "id": "backend-agent",
        "phase_id": "phase-1",
        "status": "failed",
        "progress": 0,
        "subproject_id": "sp-1",
        "pre_qa_previous_status": "completed",
        "pre_qa_repair_pending": True,
        "error": "repair failed",
    }
    ctx = SimpleNamespace(
        agents={"backend-agent": agent},
        subprojects=[{
            "id": "sp-1",
            "agent_id": "backend-agent",
            "status": "fix_required",
            "progress": 0,
            "error": "repair failed",
        }],
    )

    restored = routes_phases._restore_pre_qa_agents_after_deterministic_repair(
        ctx, "phase-1", ["backend-agent"],
    )

    assert restored == ["backend-agent"]
    assert agent["status"] == "completed"
    assert agent["progress"] == 100
    assert "pre_qa_repair_pending" not in agent
    assert "error" not in agent
    assert ctx.subprojects[0]["status"] == "completed"
    assert ctx.subprojects[0]["progress"] == 100


def test_pre_qa_evidence_step_id_allows_distinct_retry_logs() -> None:
    first = routes_phases._pre_qa_evidence_step_id({
        "gate_id": "install-root",
        "log_digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
    })
    second = routes_phases._pre_qa_evidence_step_id({
        "gate_id": "install-root",
        "log_digest": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
    })

    assert first == routes_phases._pre_qa_evidence_step_id({
        "gate_id": "install-root",
        "log_digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
    })
    assert first.startswith("pre-qa:install-root:")
    assert second.startswith("pre-qa:install-root:")
    assert first != second


def test_pre_qa_reopened_agent_can_schedule_a_fresh_run(
    monkeypatch, tmp_path,
) -> None:
    agent = {
        "id": "backend-agent",
        "phase_id": "phase-1",
        "subproject_id": "sp-1",
        "status": "completed",
        "progress": 100,
        "role": "backend",
    }
    ctx = SimpleNamespace(
        project_id="project-1",
        workspace=tmp_path,
        agents={"backend-agent": agent},
        subprojects=[{
            "id": "sp-1", "name": "Backend", "description": "implement API",
            "tech_stack": ["Node.js"],
        }],
        pm=SimpleNamespace(context_summary=""),
        description="project",
    )
    monkeypatch.setattr(
        routes_phases, "_match_issue_agent", lambda *_args, **_kwargs: agent,
    )
    routes_phases._mark_pre_qa_agents_for_repair(
        ctx, "phase-1", ["backend-agent"],
        [{"path": "backend/src/auth.js", "message": "remove fallback"}],
    )
    agent["pre_qa_repair_pending"] = True
    scheduled = []

    async def fake_schedule(payload, **_kwargs):
        scheduled.append(payload)
        return {"run_id": "fresh-run", "status": "pending"}, True

    monkeypatch.setattr(routes_execution, "_get_project", lambda _project_id: ctx)
    monkeypatch.setattr(routes_execution, "_schedule_durable_agent_run", fake_schedule)

    result = asyncio.run(
        routes_execution.execute_agent_task("project-1", "backend-agent", None)
    )

    assert result["run_id"] == "fresh-run"
    assert scheduled
    assert "remove fallback" in scheduled[0]["description"]
    assert scheduled[0]["defer_fix_qc"] is True


def test_pre_qa_repair_dispatch_is_idempotent(monkeypatch) -> None:
    agent = {
        "id": "backend-agent",
        "status": "fix_required",
        "pre_qa_repair_request_token": "scope-and-issues",
    }
    ctx = SimpleNamespace(
        project_id="project-1",
        agents={"backend-agent": agent},
    )
    calls = []

    async def fake_execute(project_id, agent_id, idempotency_key):
        calls.append((project_id, agent_id, idempotency_key))
        return {"run_id": "fresh-run", "run_status": "pending"}

    monkeypatch.setattr(routes_execution, "execute_agent_task", fake_execute)

    first = asyncio.run(
        routes_phases._schedule_pre_qa_repair_runs(ctx, ["backend-agent"])
    )
    second = asyncio.run(
        routes_phases._schedule_pre_qa_repair_runs(ctx, ["backend-agent"])
    )

    assert first["scheduled"] == {"backend-agent": "fresh-run"}
    assert second["scheduled"] == first["scheduled"]
    assert calls == [("project-1", "backend-agent", None)]
    assert agent["pre_qa_repair_pending"] is True


def test_locked_pre_qa_repair_groups_dispatch_in_parallel_by_phase(
    monkeypatch,
) -> None:
    ctx = SimpleNamespace(
        project_id="project-1",
        agents={
            "backend-agent": {"id": "backend-agent", "phase_id": "phase-1"},
            "frontend-agent": {"id": "frontend-agent", "phase_id": "phase-2"},
        },
    )
    groups, failures = routes_phases._group_repair_agents_by_phase(
        ctx,
        ["backend-agent", "frontend-agent"],
    )
    assert failures == {}
    assert groups == {
        "phase-1": ["backend-agent"],
        "phase-2": ["frontend-agent"],
    }

    calls = []

    async def fake_schedule(_ctx, agent_ids, *, _split_locked=True):
        calls.append((tuple(agent_ids), _split_locked))
        agent_id = list(agent_ids)[0]
        return {
            "scheduled": {agent_id: f"run-{agent_id}"},
            "failures": {},
        }

    monkeypatch.setattr(
        routes_phases,
        "_schedule_pre_qa_repair_runs",
        fake_schedule,
    )
    result = asyncio.run(
        routes_phases._schedule_pre_qa_repair_phase_groups(ctx, groups)
    )

    assert sorted(calls) == [
        (("backend-agent",), False),
        (("frontend-agent",), False),
    ]
    assert result == {
        "scheduled": {
            "backend-agent": "run-backend-agent",
            "frontend-agent": "run-frontend-agent",
        },
        "failures": {},
    }


def test_cross_phase_repair_invalidates_prior_completion_receipt() -> None:
    phase = {
        "phase_id": "phase-1",
        "status": "completed",
        "user_confirmed": True,
        "reviewed": True,
        "review_passed": True,
        "validated_completion_receipt": {
            "bundle_digest": "sha256:old",
        },
    }
    pm = SimpleNamespace(
        phases=[phase],
        get_phase=lambda phase_id: phase if phase_id == "phase-1" else None,
    )
    ctx = SimpleNamespace(
        project_id="project-1",
        supervisor_quality_runs={"phase-1": {"status": "completed"}},
        qc_results={"phase-1": {"qa": {"status": "passed"}}},
    )

    invalidated = routes_phases._invalidate_cross_phase_completion(
        ctx,
        pm,
        active_phase_id="phase-3",
        owner_phase_id="phase-1",
        repair_request_token="repair-token",
    )

    assert invalidated is True
    assert phase["status"] == "waiting_engineer"
    assert phase["user_confirmed"] is False
    assert phase["reviewed"] is False
    assert phase["review_passed"] is False
    assert "validated_completion_receipt" not in phase
    assert phase["completion_invalidation_history"][-1] == {
        "active_phase_id": "phase-3",
        "repair_request_token": "repair-token",
        "prior_bundle_digest": "sha256:old",
    }
    assert "phase-1" not in ctx.supervisor_quality_runs
    assert "phase-1" not in ctx.qc_results


def test_locked_pre_qa_repair_dispatches_dag_coordinator(
    monkeypatch, tmp_path,
) -> None:
    project_id = "locked-pre-qa-repair"
    phase_id = "phase-1"
    agents = {
        agent_id: {
            "id": agent_id,
            "phase_id": phase_id,
            "status": "fix_required",
            "pre_qa_repair_request_token": "scope-and-issues",
            "task_execution_receipts": {
                task_id: {"status": "succeeded"},
            },
        }
        for agent_id, task_id in (
            ("backend-agent", "task-1"),
            ("frontend-agent", "task-2"),
        )
    }
    phase = {
        "phase_id": phase_id,
        "execution_generation": "generation-1",
        "execution_contract_digest": "sha256:" + ("a" * 64),
        "execution_requirements_revision": 1,
        "execution_artifact_baseline_digest": "sha256:" + ("b" * 64),
        "execution_coordinator": {"status": "completed"},
        "execution_dispatch_plan": {
            "task_ids": ["task-1", "task-2"],
            "waves": [[{
                "task_id": "task-1",
                "agent_id": "backend-agent",
                "dependencies": [],
            }], [{
                "task_id": "task-2",
                "agent_id": "frontend-agent",
                "dependencies": ["task-1"],
            }]],
        },
        "execution_run_specs": [
            {"agent_id": "backend-agent"},
            {"agent_id": "frontend-agent"},
        ],
    }
    pm = SimpleNamespace(
        project_contract={"locked": True, "contract_version": 3},
        phases=[phase],
        get_phase=lambda candidate: phase if candidate == phase_id else None,
    )
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        agents=agents,
    )
    resumed = []
    released = []

    async def create_coordinator(*_args, **_kwargs):
        return {
            "run_id": "repair-coordinator",
            "status": "pending",
            "payload": {"dispatch_attempt_digest": "attempt-digest"},
        }

    async def resume_coordinator():
        assert (
            agents["backend-agent"]["task_execution_receipts"]["task-1"][
                "status"
            ]
            == "succeeded"
        )
        assert (
            agents["frontend-agent"]["task_execution_receipts"]["task-2"][
                "status"
            ]
            == "succeeded"
        )
        resumed.append(True)
        return 1

    async def persist():
        return None

    async def reject_direct_agent(*_args, **_kwargs):
        pytest.fail("locked repair must not call the single-Agent route")

    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)
    monkeypatch.setattr(
        routes_phases, "_create_phase_coordinator_run", create_coordinator,
    )
    monkeypatch.setattr(
        routes_phases, "resume_pending_phase_dispatches", resume_coordinator,
    )
    monkeypatch.setattr(routes_phases, "_persist_all_async", persist)
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "atomic_claim_lock",
        lambda *_args, **_kwargs: {
            "success": True,
            "lock_id": "pre-qa-claim",
        },
    )
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "release_lock",
        lambda lock_id: released.append(lock_id),
    )
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "get_active_locks",
        lambda **_kwargs: [{"lock_id": "pre-qa-claim"}],
    )
    monkeypatch.setattr(
        routes_execution, "execute_agent_task", reject_direct_agent,
    )

    result = asyncio.run(
        routes_phases._schedule_pre_qa_repair_runs(
            ctx, ["backend-agent", "frontend-agent"],
        )
    )

    assert result == {
        "scheduled": {
            "backend-agent": "repair-coordinator",
            "frontend-agent": "repair-coordinator",
        },
        "failures": {},
    }
    assert resumed == [True]
    assert phase["execution_coordinator"]["repair_task_ids"] == [
        "task-1", "task-2",
    ]
    assert (
        phase["execution_coordinator"]["dispatch_attempt_digest"]
        == "attempt-digest"
    )
    assert agents["backend-agent"]["task_execution_receipts"] == {
        "task-1": {"status": "succeeded"},
    }
    assert agents["frontend-agent"]["task_execution_receipts"] == {
        "task-2": {"status": "succeeded"},
    }
    assert released == ["pre-qa-claim"]
    assert {
        agent["pre_qa_repair_run_id"] for agent in agents.values()
    } == {"repair-coordinator"}


def test_locked_repair_promotes_receipt_only_after_current_attempt_succeeds(
    monkeypatch, tmp_path,
) -> None:
    project_id = "locked-repair-promotion"
    phase_id = "phase-1"
    agent_id = "backend-agent"
    task_id = "task-1"
    generation = "generation-1"
    contract_digest = "sha256:" + ("a" * 64)
    baseline_digest = "sha256:" + ("b" * 64)
    coordinator_run_id = "repair-coordinator"
    attempt_digest = "repair-attempt-digest"
    old_receipt = {
        "task_id": task_id,
        "agent_id": agent_id,
        "phase_id": phase_id,
        "status": "succeeded",
        "completion_run_id": "old-run",
        "execution_generation": generation,
        "contract_digest": contract_digest,
        "requirements_revision": 1,
        "artifact_baseline_digest": baseline_digest,
    }
    agent = {
        "id": agent_id,
        "phase_id": phase_id,
        "fix_task": "remove placeholder",
        "task_execution_receipts": {task_id: copy.deepcopy(old_receipt)},
        "rebuild_file_specs": [
            {"path": "README.md", "mode": "patch"},
            {"path": "backend/src/auth.py", "mode": "preserve"},
        ],
    }
    phase = {
        "phase_id": phase_id,
        "status": "waiting_engineer",
        "execution_generation": generation,
        "execution_contract_digest": contract_digest,
        "execution_requirements_revision": 1,
        "execution_artifact_baseline_digest": baseline_digest,
        "rebuild_file_manifest": {
            "by_task_id": {task_id: ["README.md"]},
        },
        "execution_dispatch_plan": {
            "phase_id": phase_id,
            "task_ids": [task_id],
            "waves": [[{
                "task_id": task_id,
                "agent_id": agent_id,
                "dependencies": [],
            }]],
        },
        "execution_run_specs": [{
            "project_id": project_id,
            "agent_id": agent_id,
            "subproject_id": "sp-1",
            "subproject_name": "Backend",
            "description": "build backend",
            "tech_stack": ["Node.js"],
            "project_context": "test",
            "artifact_policy": {},
        }],
        "execution_coordinator": {
            "status": "running",
            "durable_run_id": coordinator_run_id,
            "dispatch_attempt_digest": attempt_digest,
            "repair_task_ids": [task_id],
            "repair_agent_ids": [agent_id],
        },
    }
    pm = SimpleNamespace(
        project_contract={
            "locked": True,
            "required_files": [
                {
                    "path": "README.md",
                    "phase_id": phase_id,
                    "task_id": task_id,
                    "required": True,
                },
                {
                    "path": "backend/src/auth.py",
                    "phase_id": phase_id,
                    "task_id": task_id,
                    "required": True,
                },
            ],
        },
        get_phase=lambda candidate: phase if candidate == phase_id else None,
    )
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        agents={agent_id: agent},
    )
    parent_payload = {
        "project_id": project_id,
        "phase_id": phase_id,
        "execution_generation": generation,
        "contract_digest": contract_digest,
        "requirements_revision": 1,
        "artifact_baseline_digest": baseline_digest,
        "dispatch_attempt_digest": attempt_digest,
    }
    child_payload = {
        **parent_payload,
        "agent_id": agent_id,
        "task_id": task_id,
        "phase_coordinator_run_id": coordinator_run_id,
    }
    scheduled_payloads = []

    class Registry:
        def get(self, run_id):
            if run_id == coordinator_run_id:
                return {
                    "run_id": run_id,
                    "status": "running",
                    "lease_owner": "owner-1",
                    "lease_expires_at": 9999999999,
                    "payload": parent_payload,
                }
            if run_id == "repair-child":
                return {
                    "run_id": run_id,
                    "status": "succeeded",
                    "started_at": 1,
                    "finished_at": 2,
                    "payload": child_payload,
                    "result": {"success": True},
                }
            return {
                "run_id": run_id,
                "status": "succeeded",
                "payload": {},
            }

    async def schedule(payload, **_kwargs):
        scheduled_payloads.append(copy.deepcopy(payload))
        assert agent["task_execution_receipts"][task_id] == old_receipt
        assert payload["dispatch_attempt_digest"] == attempt_digest
        assert payload["phase_coordinator_run_id"] == coordinator_run_id
        assert payload["artifact_policy"]["required_files"] == ["README.md"]
        assert payload["artifact_policy"]["allowed_path_prefixes"] == [
            "README.md",
        ]
        assert payload["artifact_policy"]["rebuild_file_specs"] == [
            {"path": "README.md", "mode": "patch"},
        ]
        return Registry().get("repair-child"), True

    async def persist():
        return None

    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)
    monkeypatch.setattr(routes_execution, "_run_registry", Registry())
    monkeypatch.setattr(
        routes_execution, "_schedule_durable_agent_run", schedule,
    )
    monkeypatch.setattr(routes_phases, "_persist_all_async", persist)

    assert asyncio.run(routes_phases._resume_locked_phase_coordinator(
        project_id,
        phase_id,
        coordinator_run_id,
        "owner-1",
    ))

    promoted = agent["task_execution_receipts"][task_id]
    assert promoted["completion_run_id"] == "repair-child"
    assert promoted["required_files"] == ["README.md"]
    assert promoted["dispatch_attempt_digest"] == attempt_digest
    assert promoted["repair_coordinator_run_id"] == coordinator_run_id
    attempt = agent["pre_qa_repair_attempt_receipts"][
        coordinator_run_id
    ][task_id]
    assert attempt == promoted
    assert phase["execution_coordinator"]["status"] == "running"
    assert phase["execution_coordinator"]["dispatch_completed"] is True

    phase["execution_coordinator"]["repair_agent_ids"] = ["other-agent"]
    with pytest.raises(Exception, match="failed wave"):
        asyncio.run(routes_phases._resume_locked_phase_coordinator(
            project_id,
            phase_id,
            coordinator_run_id,
            "owner-1",
        ))
    assert len(scheduled_payloads) == 1


def test_phase_jwt_gate_waits_for_scoped_source_manifest(
    monkeypatch, tmp_path,
) -> None:
    phase_one = {
        "phase_id": "phase-1",
        "user_confirmed": False,
        "task_contract": [{
            "task_id": "task-1",
            "name": "Create authentication and backend foundation",
        }],
    }
    phase_two = {"phase_id": "phase-2", "user_confirmed": False}
    contract = {
        "locked": True,
        "source_requirements": "JWT authentication and login are required",
        "technology_stack": ["Node.js"],
        "required_files": [
            {
                "path": "backend/package.json",
                "owner_type": "backend",
                "phase_id": "phase-1",
                "task_id": "task-1",
                "required": True,
            },
            {
                "path": "backend/src/auth.ts",
                "owner_type": "backend",
                "phase_id": "phase-2",
                "task_id": "task-2",
                "required": True,
            },
            {
                "path": "frontend/src/App.tsx",
                "owner_type": "frontend",
                "phase_id": "phase-1",
                "task_id": "task-1",
                "required": True,
            },
        ],
    }
    pm = SimpleNamespace(
        project_contract=contract,
        file_registry={
            "backend/package.json": {"phase_id": "phase-1"},
            "frontend/src/App.tsx": {"phase_id": "phase-1"},
            "backend/src/auth.ts": {"phase_id": "phase-2"},
        },
        phases=[phase_one, phase_two],
        get_phase=lambda candidate: (
            phase_one if candidate == "phase-1" else phase_two
        ),
    )
    ctx = SimpleNamespace(
        project_id="jwt-scope",
        workspace=tmp_path,
        agents={},
    )
    observed = []

    class CapturingVerifier:
        def __init__(self, *_args, **_kwargs):
            pass

        def verify(self, **kwargs):
            observed.append({
                "jwt_required": kwargs["jwt_required"],
                "registry": set(kwargs["file_registry"]),
            })
            return SimpleNamespace(to_dict=lambda: {"passed": True})

    monkeypatch.setenv("METIS_TEST_MODE", "1")
    monkeypatch.setitem(routes_phases._phase_managers, ctx.project_id, pm)
    monkeypatch.setattr(routes_phases, "PreQAVerifier", CapturingVerifier)

    routes_phases._execute_phase_pre_qa(ctx, "phase-1")
    phase_one["user_confirmed"] = True
    routes_phases._execute_phase_pre_qa(ctx, "phase-2")

    assert observed == [
        {
            "jwt_required": False,
            "registry": {
                "backend/package.json",
                "frontend/src/App.tsx",
            },
        },
        {
            "jwt_required": True,
            "registry": {
                "backend/package.json",
                "frontend/src/App.tsx",
                "backend/src/auth.ts",
            },
        },
    ]


def test_phase_pre_qa_only_uses_docker_for_declared_docker_delivery(
    monkeypatch, tmp_path,
) -> None:
    phase = {
        "phase_id": "phase-1",
        "user_confirmed": False,
        "task_contract": [{"task_id": "task-1", "name": "Build app"}],
    }
    contract = {
        "locked": True,
        "technology_stack": ["Node.js"],
        "required_files": [{
            "path": "package.json",
            "owner_type": "backend",
            "phase_id": "phase-1",
            "task_id": "task-1",
            "required": True,
        }],
    }
    pm = SimpleNamespace(
        project_contract=contract,
        file_registry={"package.json": {"phase_id": "phase-1"}},
        phases=[phase],
        get_phase=lambda candidate: phase if candidate == "phase-1" else None,
    )
    ctx = SimpleNamespace(
        project_id="docker-applicability",
        workspace=tmp_path,
        agents={},
    )
    observed = []

    class CapturingVerifier:
        def __init__(self, *_args, **_kwargs):
            pass

        def verify(self, **kwargs):
            observed.append([
                gate.gate_id for gate in kwargs["command_gates"]
            ])
            return SimpleNamespace(to_dict=lambda: {"passed": True})

    monkeypatch.delenv("METIS_TEST_MODE", raising=False)
    monkeypatch.delenv("METIS_PRE_QA_USE_DOCKER", raising=False)
    monkeypatch.setattr(routes_phases.shutil, "which", lambda _name: "docker")
    monkeypatch.setitem(routes_phases._phase_managers, ctx.project_id, pm)
    monkeypatch.setattr(routes_phases, "PreQAVerifier", CapturingVerifier)

    routes_phases._execute_phase_pre_qa(ctx, "phase-1")
    contract["required_files"].append({
        "path": "Dockerfile",
        "owner_type": "devops",
        "phase_id": "phase-1",
        "task_id": "task-1",
        "required": True,
    })
    pm.file_registry["Dockerfile"] = {"phase_id": "phase-1"}
    routes_phases._execute_phase_pre_qa(ctx, "phase-1")

    assert "docker-daemon" not in observed[0]
    assert "docker-daemon" in observed[1]


def test_pre_qa_dispatch_replaces_terminal_failed_repair_run(monkeypatch) -> None:
    agent = {
        "id": "backend-agent", "status": "fix_required",
        "pre_qa_repair_request_token": "scope-and-issues",
        "pre_qa_scheduled_token": "scope-and-issues",
        "pre_qa_repair_run_id": "old-run",
    }
    ctx = SimpleNamespace(project_id="project-1", agents={"backend-agent": agent})
    monkeypatch.setattr(
        routes_execution._run_registry, "get",
        lambda _run_id: {"run_id": "old-run", "status": "succeeded"},
    )
    calls = []

    async def fake_execute(*args):
        calls.append(args)
        return {"run_id": "new-run", "run_status": "pending"}

    monkeypatch.setattr(routes_execution, "execute_agent_task", fake_execute)
    result = asyncio.run(routes_phases._schedule_pre_qa_repair_runs(ctx, ["backend-agent"]))

    assert result["scheduled"] == {"backend-agent": "new-run"}
    assert len(calls) == 1


def test_pre_qa_dispatch_failure_keeps_explicit_retry_action() -> None:
    action = routes_phases._pre_qa_repair_action(
        "build failed",
        {
            "scheduled": {"frontend-agent": "run-1"},
            "failures": {"backend-agent": "lease unavailable"},
        },
    )

    assert "backend-agent: lease unavailable" in action["message"]
    assert action["options"] == ["retry_cycle", "manual_fix", "rebuild_phase"]
    assert action["scheduled_runs"] == {"frontend-agent": "run-1"}


def test_completed_pre_qa_repair_auto_restarts_quality_once(monkeypatch) -> None:
    project_id = "project-1"
    phase_id = "phase-1"
    phase = {
        "phase_id": phase_id,
        "subprojects": ["sp-1"],
        "status": "waiting_engineer",
    }
    ctx = SimpleNamespace(
        project_id=project_id,
        agents={"backend-agent": {
            "id": "backend-agent", "phase_id": phase_id,
            "status": "completed",
            "pre_qa_repair_pending": True,
        }},
        subprojects=[{
            "id": "sp-1", "phase_id": phase_id,
            "agent_id": "backend-agent", "status": "completed",
        }],
        supervisor_quality_runs={
            phase_id: {
                "run_id": "qa-run",
                "phase_id": phase_id,
                "status": "waiting_engineer",
                "state": "waiting_engineer",
                "active": True,
                "waiting_for": ["backend-agent"],
                "next_action": {"type": "repair_pre_qa_failures"},
            },
        },
    )
    phase_manager = SimpleNamespace(
        phases=[phase],
        get_phase=lambda candidate: phase if candidate == phase_id else None,
    )
    monkeypatch.setitem(
        routes_execution._phase_managers,
        project_id,
        phase_manager,
    )
    monkeypatch.setitem(
        routes_phases._phase_managers,
        project_id,
        phase_manager,
    )
    state = {
        "status": "pre_qa_failed",
        "running": False,
        "action_required": {"message": "waiting"},
        "pre_qa_repair_runs": {"backend-agent": "fresh-run"},
    }
    monkeypatch.setitem(
        routes_phases._auto_repair_states,
        f"{project_id}-{phase_id}",
        state,
    )
    monkeypatch.setattr(
        routes_execution._run_registry,
        "get",
        lambda run_id: {"run_id": run_id, "status": "succeeded"},
    )
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "atomic_claim_lock",
        lambda *_args, **_kwargs: {
            "success": True,
            "lock_id": "quality-resume-lock",
            "leased_until": 9999999999,
        },
    )
    monkeypatch.setattr(
        routes_phases,
        "_prepare_supervisor_verification",
        lambda *_args, **_kwargs: None,
    )

    async def persist():
        return None

    monkeypatch.setattr(routes_phases, "_persist_all_async", persist)
    scheduled = []

    def fake_create_task(coro, **_kwargs):
        scheduled.append(coro)
        coro.close()

    monkeypatch.setattr(routes_phases, "_safe_create_task", fake_create_task)

    asyncio.run(routes_execution._start_phase_quality_cycle_if_ready(ctx))
    asyncio.run(routes_execution._start_phase_quality_cycle_if_ready(ctx))

    assert len(scheduled) == 1
    assert state["running"] is True
    assert state["status"] == "pre_qa_verifying"
    assert state["action_required"] is None
    assert "pre_qa_repair_pending" not in ctx.agents["backend-agent"]


def test_prepare_reconciles_succeeded_agent_still_in_waiting_for(monkeypatch):
    calls = []

    class Machine:
        state = "verifying"

        def to_dict(self):
            return {
                "agents": {"backend-agent": {"status": "succeeded"}},
                "waiting_for": ["backend-agent"],
            }

        def record_agent(self, agent_id, status, **_kwargs):
            calls.append((agent_id, status))

    ctx = SimpleNamespace(agents={
        "backend-agent": {
            "id": "backend-agent", "phase_id": "phase-1",
            "subproject_id": "sp-1", "status": "completed", "progress": 100,
        },
    })

    routes_phases._prepare_supervisor_verification(ctx, "phase-1", Machine())

    assert calls == [("backend-agent", "succeeded")]


def test_status_poll_does_not_restart_succeeded_pre_qa_repair(
    monkeypatch,
) -> None:
    project_id, phase_id = "project-resume", "phase-1"
    state = {
        "status": "continuing",
        "running": False,
        "pre_qa_repair_runs": {"backend-agent": "run-1"},
        "action_required": {"message": "waiting"},
    }
    ctx = SimpleNamespace(
        project_id=project_id,
        agents={"backend-agent": {
            "id": "backend-agent", "status": "completed", "progress": 100,
            "pre_qa_repair_pending": True,
        }},
    )
    machine = SimpleNamespace(state="waiting_engineer")
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(
        routes_phases._auto_repair_states, f"{project_id}-{phase_id}", state,
    )
    monkeypatch.setattr(
        routes_execution._run_registry, "get",
        lambda _run_id: {"status": "succeeded"},
    )
    monkeypatch.setattr(
        routes_phases, "_supervisor_quality_machine", lambda *_args: machine,
    )
    monkeypatch.setattr(
        routes_phases, "_prepare_supervisor_verification", lambda *_args: None,
    )
    monkeypatch.setattr(
        routes_phases, "_store_supervisor_quality_machine", lambda *_args: {},
    )
    async def no_persist():
        return None
    monkeypatch.setattr(routes_phases, "_persist_all_async", no_persist)
    scheduled = []
    monkeypatch.setattr(
        routes_phases,
        "_safe_create_task",
        lambda coro, **_kwargs: (scheduled.append(coro), coro.close()),
    )

    result = asyncio.run(
        routes_phases.get_auto_repair_status(project_id, phase_id),
    )

    assert result["status"] == "continuing"
    assert result["running"] is False
    assert scheduled == []
    assert ctx.agents["backend-agent"]["pre_qa_repair_pending"] is True
    assert state["action_required"] == {"message": "waiting"}
    assert machine.state == "waiting_engineer"
from api.routes_phases import (
    _infer_phase_tech_stack,
    _parallel_expert_scopes,
    _finalize_phase_scope,
)
from core.phase_manager import PhaseManager


def _scopes_overlap(left, right):
    left = left.rstrip("/")
    right = right.rstrip("/")
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def test_mixed_role_parallel_scopes_are_globally_disjoint():
    scopes = _parallel_expert_scopes(
        has_frontend=True,
        has_backend=True,
        has_database=True,
        has_devops=True,
    )
    roles = [
        "frontend", "backend", "database", "qa",
        "security", "devops", "architecture", "fullstack_engineer",
    ]

    for index, role in enumerate(roles):
        for sibling in roles[index + 1:]:
            assert not any(
                _scopes_overlap(left, right)
                for left in scopes[role]
                for right in scopes[sibling]
            ), f"overlap: {role}={scopes[role]} / {sibling}={scopes[sibling]}"

    assert "README.md" not in scopes["architecture"]
    assert "tests/security/" not in scopes["security"]
    assert "frontend/tests/" not in scopes["frontend"]
    assert scopes["frontend"] == ["frontend/"]
    assert "frontend/tests/" not in scopes["qa"]


def test_backend_owns_tests_without_a_dedicated_qa_role():
    scopes = _parallel_expert_scopes(
        has_frontend=False,
        has_backend=True,
        has_database=False,
        has_devops=False,
        has_qa=False,
    )

    assert "backend/tests/" in scopes["backend"]
    assert "backend/app/db/" in scopes["backend"]


def test_fullstack_and_qa_scopes_are_disjoint_without_frontend_expert():
    scopes = _parallel_expert_scopes(
        has_frontend=False,
        has_backend=False,
        has_database=False,
        has_devops=False,
        has_qa=True,
        has_fullstack=True,
    )

    assert "frontend/" in scopes["fullstack_engineer"]
    assert "frontend/tests/" not in scopes["qa"]
    assert not any(
        _scopes_overlap(fullstack_path, qa_path)
        for fullstack_path in scopes["fullstack_engineer"]
        for qa_path in scopes["qa"]
    )


def test_final_scope_cuts_late_delivery_overlap_before_lock_claim():
    finalized = _finalize_phase_scope(
        ["backend/", "package.json"],
        ["backend/tests/test_auth.py"],
        [["backend/tests/"]],
        subproject_id="sp-backend",
    )

    assert finalized == ["package.json"]
    assert not any(
        _scopes_overlap(left, right)
        for left in finalized
        for right in ["backend/tests/"]
    )


def test_failed_agent_with_unaudited_outputs_is_not_recovered_before_review(monkeypatch, tmp_path):
    phase_id = "phase-recover"
    agent_id = "agent-timeout"
    ctx = SimpleNamespace(
        project_id="project-recover",
        workspace=tmp_path,
        agents={agent_id: {
            "id": agent_id,
            "phase_id": phase_id,
            "status": "failed",
            "progress": 42,
            "output_files": ["backend/app.py", ".env.example"],
        }},
        subprojects=[{
            "id": "sp-recover",
            "phase_id": phase_id,
            "agent_id": agent_id,
            "status": "failed",
        }],
    )
    output = tmp_path / "backend" / "app.py"
    output.parent.mkdir(parents=True)
    output.write_text("ok", encoding="utf-8")
    (tmp_path / ".env.example").write_text("PORT=3000\n", encoding="utf-8")
    monkeypatch.setattr(routes_phases, "_auto_repair_states", {})
    routes_execution.execution_status[agent_id] = {"status": "failed", "progress": 42}

    with pytest.raises(Exception) as caught:
        routes_phases._assert_phase_execution_completed(ctx, phase_id)

    assert getattr(caught.value, "status_code", None) == 409
    assert ctx.agents[agent_id]["status"] == "failed"
    assert ctx.agents[agent_id]["recovery_status"] == "verification_evidence_incomplete"
    assert ctx.subprojects[0]["status"] == "failed"
    assert routes_execution.execution_status[agent_id]["status"] == "failed"


def test_repair_rollback_restores_phase_metadata_and_removes_new_agents(monkeypatch):
    phase = {"phase_id": "phase-rollback", "rebuild_file_manifest": {"all": ["a.py"]}}
    pm = SimpleNamespace(
        file_registry={"a.py": {"phase_id": "phase-rollback", "agent_id": "old"},
                       "new.py": {"phase_id": "phase-rollback", "agent_id": "new"}},
        phase_agents={"phase-rollback": ["old", "new"]},
        get_phase=lambda _phase_id: phase,
    )
    ctx = SimpleNamespace(
        project_id="project-rollback",
        agents={
            "old": {"id": "old", "phase_id": "phase-rollback", "status": "completed"},
            "new": {"id": "new", "phase_id": "phase-rollback", "status": "completed"},
        },
        subprojects=[],
    )
    monkeypatch.setattr(routes_phases, "_phase_managers", {ctx.project_id: pm})
    snapshot = {
        "phase_id": "phase-rollback",
        "agents": {"old": {"id": "old", "phase_id": "phase-rollback", "status": "failed"}},
        "subprojects": {},
        "file_registry": {"a.py": {"phase_id": "phase-rollback", "agent_id": "old"}},
        "phase": {"rebuild_file_manifest": {"all": ["a.py"]}},
        "phase_agents": ["old"],
        "execution_status": {},
    }

    routes_phases._restore_repair_metadata(ctx, snapshot)

    assert set(ctx.agents) == {"old"}
    assert ctx.agents["old"]["status"] == "failed"
    assert set(pm.file_registry) == {"a.py"}
    assert pm.phase_agents["phase-rollback"] == ["old"]


def test_rebuild_regression_restores_files_agents_tasks_registry_and_qc(monkeypatch, tmp_path):
    project_id, phase_id = "project-rebuild-rollback", "phase-rollback"
    old_phase = {
        "phase_id": phase_id, "name": "后端", "status": "needs_rework",
        "agents": ["old-agent"], "subprojects": ["old-task"],
    }
    current_phase = {
        "phase_id": phase_id, "name": "后端", "status": "reviewing",
        "agents": ["new-agent"],
        "rebuild_file_manifest": {"all": ["backend/app.py"]},
    }

    class Manager:
        phases = [current_phase]
        phase_agents = {phase_id: ["new-agent"]}
        file_registry = {
            "backend/app.py": {"phase_id": phase_id, "agent_id": "new-agent"},
        }

        def get_phase(self, wanted):
            return current_phase if wanted == phase_id else None

    pm = Manager()
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        agents={"new-agent": {
            "id": "new-agent", "phase_id": phase_id, "status": "completed",
            "output_files": ["backend/app.py"],
        }},
        subprojects=[{
            "id": "new-task", "phase_id": phase_id, "agent_id": "new-agent",
            "status": "completed",
        }],
        qc_results={phase_id: {"qa": {"passed": False, "issues": ["new"]}}},
        supervisor_quality_runs={phase_id: {"run_id": "new-run"}},
    )
    snapshot_dir = tmp_path / ".project" / "versions" / "7"
    (snapshot_dir / "backend").mkdir(parents=True)
    (snapshot_dir / "backend" / "app.py").write_text("old version", encoding="utf-8")
    (snapshot_dir / "commit.json").write_text(
        '{"files":["backend/app.py"],"kind":"pre_rebuild"}', encoding="utf-8",
    )
    target = tmp_path / "backend" / "app.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old version", encoding="utf-8")
    durable_snapshot = routes_phases._encode_durable_rebuild_snapshot(
        tmp_path, ["backend/app.py"],
    )
    target.write_text("regressed version", encoding="utf-8")
    unregistered_new_file = tmp_path / "backend" / "rebuild-only.py"
    unregistered_new_file.write_text("must be rolled back", encoding="utf-8")
    monkeypatch.setattr(routes_phases, "_phase_managers", {project_id: pm})
    monkeypatch.setattr(routes_phases.expert_lock, "release_lock", lambda _lock: None)
    routes_execution.execution_status["new-agent"] = {"status": "completed"}
    state = {
        "pre_rebuild_snapshot_version": 7,
        "pre_rebuild_delivery_inventory": ["backend/app.py"],
        "durable_rebuild_snapshot": durable_snapshot,
        "pre_rebuild_restore_point": {
            "phase": old_phase,
            "agents": {"old-agent": {
                "id": "old-agent", "phase_id": phase_id, "status": "completed",
            }},
            "subprojects": {"old-task": {
                "id": "old-task", "phase_id": phase_id,
                "agent_id": "old-agent", "status": "completed",
            }},
            "file_registry": {
                "backend/app.py": {"phase_id": phase_id, "agent_id": "old-agent"},
            },
            "phase_agents": ["old-agent"],
            "execution_status": {"old-agent": {"status": "completed"}},
            "qc_result": {"qa": {"passed": False, "issues": ["old"]}},
            "supervisor_quality_run": {"run_id": "old-run"},
        },
    }

    routes_phases._restore_phase_rebuild_snapshot(ctx, phase_id, state)

    assert target.read_text(encoding="utf-8") == "old version"
    assert not unregistered_new_file.exists()
    assert set(ctx.agents) == {"old-agent"}
    assert [item["id"] for item in ctx.subprojects] == ["old-task"]
    assert pm.phase_agents[phase_id] == ["old-agent"]
    assert pm.file_registry["backend/app.py"]["agent_id"] == "old-agent"
    assert ctx.qc_results[phase_id]["qa"]["issues"] == ["old"]
    assert ctx.supervisor_quality_runs[phase_id]["run_id"] == "old-run"
    assert "new-agent" not in routes_execution.execution_status
    assert routes_execution.execution_status["old-agent"]["status"] == "completed"


def test_devops_owns_readme_when_no_fullstack_expert_exists():
    scopes = _parallel_expert_scopes(
        has_frontend=False,
        has_backend=False,
        has_database=False,
        has_devops=True,
        has_qa=True,
        has_fullstack=False,
    )

    assert "README.md" in scopes["devops"]


def test_manual_phase_reset_cleans_owned_files_and_stale_repair_state(
    monkeypatch, tmp_path
):
    project_id = "clean-phase-reset"
    phase_id = "phase-2"
    pm = PhaseManager(project_id, tmp_path)
    pm.init_phases_from_plan({"phases": [{
        "phase_id": phase_id,
        "name": "Frontend",
        "description": "rebuild description",
        "roles_needed": ["Frontend Developer"],
    }]})
    phase = pm.get_phase(phase_id)
    phase.update({
        "agents": ["agent-frontend"],
        "rebuild_base_description": "original description",
        "rebuild_file_manifest": {"all": ["frontend/src/App.tsx"]},
        "rebuild_notes": {"issue": "old"},
    })
    app_file = tmp_path / "frontend" / "src" / "App.tsx"
    stale_file = tmp_path / "frontend" / "src" / "Old.tsx"
    shared_file = tmp_path / "frontend" / "src" / "Shared.tsx"
    backend_file = tmp_path / "backend" / "app" / "main.py"
    app_file.parent.mkdir(parents=True)
    backend_file.parent.mkdir(parents=True)
    app_file.write_text("new", encoding="utf-8")
    stale_file.write_text("stale", encoding="utf-8")
    shared_file.write_text("owned by another phase", encoding="utf-8")
    backend_file.write_text("accepted backend", encoding="utf-8")
    pm.register_file(
        "frontend/src/App.tsx", "agent-frontend", "Frontend Developer",
        phase_id,
    )
    pm.register_file(
        "frontend/src/Shared.tsx", "agent-other", "Frontend Developer",
        "phase-1",
    )
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        agents={"agent-frontend": {
            "id": "agent-frontend",
            "phase_id": phase_id,
            "status": "failed",
            "allowed_path_prefixes": ["frontend/src/"],
            "output_files": ["frontend/src/App.tsx"],
        }},
        subprojects=[{
            "id": phase_id,
            "phase_id": phase_id,
            "agent_id": "agent-frontend",
            "status": "failed",
        }],
        qc_results={phase_id: {"passed": False}},
    )
    key = f"{project_id}-{phase_id}"
    routes_phases.projects[project_id] = ctx
    routes_phases._phase_managers[project_id] = pm
    routes_phases._auto_repair_states[key] = {"status": "error"}
    routes_phases._auto_repair_api_configs[key] = {"api_key": "test"}

    async def no_persist():
        return None

    monkeypatch.setattr(routes_phases, "_persist_all_async", no_persist)
    monkeypatch.setattr(routes_execution, "_persist_execution_state", lambda: None)
    released_locks = []
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "get_active_locks",
        lambda project_id: [
            {
                "lock_id": "other-phase",
                "project_id": project_id,
                "task_id": "phase-1-subproject",
            },
        ],
    )
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "release_lock",
        lambda lock_id: released_locks.append(lock_id) or {"success": True},
    )
    try:
        result = asyncio.run(routes_phases.reset_phase(project_id, phase_id))
    finally:
        routes_phases.projects.pop(project_id, None)
        routes_phases._phase_managers.pop(project_id, None)
        routes_phases._auto_repair_states.pop(key, None)
        routes_phases._auto_repair_api_configs.pop(key, None)

    assert result["deleted_files"] == 1
    assert not app_file.exists()
    assert stale_file.is_file()
    assert shared_file.is_file()
    assert backend_file.is_file()
    assert key not in routes_phases._auto_repair_states
    assert key not in routes_phases._auto_repair_api_configs
    assert phase["description"] == "original description"
    assert "rebuild_file_manifest" not in phase
    assert released_locks == []


def _install_durable_phase_reset_state(
    monkeypatch,
    tmp_path,
    *,
    run_status="failed",
    active_task=False,
    valid_guard=False,
    active_lock=False,
):
    project_id = "durable-phase-reset"
    phase_id = "phase-1"
    agent_id = "agent-stale"
    subproject_id = "sp-phase-1-001"
    run_id = "durable-agent-run"
    pm = PhaseManager(project_id, tmp_path)
    pm.init_phases_from_plan({"phases": [{
        "phase_id": phase_id,
        "name": "Foundation",
        "roles_needed": ["Backend Developer"],
    }]})
    phase = pm.get_phase(phase_id)
    phase["agents"] = [agent_id]
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        agents={agent_id: {
            "id": agent_id,
            "phase_id": phase_id,
            "subproject_id": subproject_id,
            "status": "queued",
            "task_execution_receipts": {
                "phase-1-task-1": {"start_run_id": run_id},
            },
        }},
        subprojects=[{
            "id": subproject_id,
            "phase_id": phase_id,
            "agent_id": agent_id,
            "status": "queued",
        }],
        qc_results={},
        supervisor_quality_runs={},
    )
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)

    async def no_persist():
        return None

    monkeypatch.setattr(routes_phases, "_persist_all_async", no_persist)
    monkeypatch.setattr(routes_execution, "_persist_execution_state", lambda: None)
    monkeypatch.setattr(
        routes_execution,
        "execution_status",
        {agent_id: {"run_id": run_id, "status": "queued"}},
    )
    monkeypatch.setattr(
        routes_execution,
        "_active_run_tasks",
        {
            run_id: SimpleNamespace(done=lambda: False)
        } if active_task else {},
    )
    monkeypatch.setattr(
        routes_execution,
        "_run_execution_guards",
        {
            run_id: SimpleNamespace(valid=True)
        } if valid_guard else {},
    )
    monkeypatch.setattr(
        routes_execution._run_registry,
        "get",
        lambda candidate: {
            "run_id": candidate,
            "project_id": project_id,
            "status": run_status,
            "payload": {
                "project_id": project_id,
                "phase_id": phase_id,
                "agent_id": agent_id,
            },
        },
    )
    locks = [{
        "lock_id": "live-agent-lock",
        "task_id": f"{subproject_id}:run:{run_id}",
        "expert_id": "expert-stale",
    }] if active_lock else []
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "get_active_locks",
        lambda **_kwargs: locks,
    )
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "release_lock",
        lambda _lock_id: {"success": True},
    )
    return project_id, phase_id, agent_id, phase, ctx


@pytest.mark.parametrize("run_status", ["cancelled", "failed"])
def test_manual_phase_reset_accepts_stale_queued_agent_after_terminal_run(
    monkeypatch, tmp_path, run_status
):
    project_id, phase_id, agent_id, phase, ctx = _install_durable_phase_reset_state(
        monkeypatch, tmp_path, run_status=run_status,
    )

    result = asyncio.run(routes_phases.reset_phase(project_id, phase_id))

    assert result["success"] is True
    assert result["deleted_agents"] == 1
    assert phase["status"] == "pending"
    assert agent_id not in ctx.agents


def test_manual_phase_reset_accepts_terminal_agent_with_missing_historical_run(
    monkeypatch, tmp_path
):
    from core.execution_runs import RunNotFound

    project_id, phase_id, agent_id, phase, ctx = _install_durable_phase_reset_state(
        monkeypatch, tmp_path, run_status="failed",
    )
    ctx.agents[agent_id]["status"] = "failed"
    routes_execution.execution_status[agent_id]["status"] = "failed"
    monkeypatch.setattr(
        routes_execution._run_registry,
        "get",
        lambda _candidate: (_ for _ in ()).throw(RunNotFound("run purged")),
    )

    result = asyncio.run(routes_phases.reset_phase(project_id, phase_id))

    assert result["success"] is True
    assert phase["status"] == "pending"
    assert agent_id not in ctx.agents


def test_manual_phase_reset_releases_terminal_agents_planning_lock(
    monkeypatch,
    tmp_path,
):
    project_id, phase_id, agent_id, phase, ctx = (
        _install_durable_phase_reset_state(
            monkeypatch,
            tmp_path,
            run_status="failed",
        )
    )
    subproject_id = ctx.agents[agent_id]["subproject_id"]
    ctx.agents[agent_id].update({
        "status": "failed",
        "expert_id": "expert-stale",
        "lock_id": "terminal-planning-lock",
    })
    routes_execution.execution_status[agent_id]["status"] = "failed"
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "get_active_locks",
        lambda **_kwargs: [{
            "lock_id": "terminal-planning-lock",
            "task_id": subproject_id,
            "expert_id": "expert-stale",
            "project_id": project_id,
        }],
    )
    released = []
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "release_lock",
        lambda lock_id: released.append(lock_id) or {"success": True},
    )

    result = asyncio.run(routes_phases.reset_phase(project_id, phase_id))

    assert result["success"] is True
    assert phase["status"] == "pending"
    assert agent_id not in ctx.agents
    assert "terminal-planning-lock" in released


def test_manual_phase_reset_rejects_orphan_phase_run_lock_without_agent(
    monkeypatch, tmp_path
):
    project_id, phase_id, agent_id, _, ctx = _install_durable_phase_reset_state(
        monkeypatch, tmp_path, run_status="cancelled", active_lock=True,
    )
    ctx.agents.pop(agent_id)

    with pytest.raises(routes_phases.HTTPException) as blocked:
        asyncio.run(routes_phases.reset_phase(project_id, phase_id))

    assert blocked.value.status_code == 409


@pytest.mark.parametrize(
    "blocker",
    ["pending_run", "running_run", "live_task", "valid_guard", "active_lock"],
)
def test_manual_phase_reset_rejects_canonical_live_execution(
    monkeypatch, tmp_path, blocker
):
    project_id, phase_id, _, _, _ = _install_durable_phase_reset_state(
        monkeypatch,
        tmp_path,
        run_status=(
            "pending" if blocker == "pending_run"
            else "running" if blocker == "running_run"
            else "failed"
        ),
        active_task=blocker == "live_task",
        valid_guard=blocker == "valid_guard",
        active_lock=blocker == "active_lock",
    )

    with pytest.raises(routes_phases.HTTPException) as blocked:
        asyncio.run(routes_phases.reset_phase(project_id, phase_id))

    assert blocked.value.status_code == 409


def test_rebuild_reset_retains_patch_and_preserve_baselines(monkeypatch, tmp_path):
    project_id = "rebuild-reset-baselines"
    phase_id = "phase-2"
    pm = PhaseManager(project_id, tmp_path)
    pm.init_phases_from_plan({"phases": [{
        "phase_id": phase_id,
        "name": "Frontend",
        "description": "rebuild",
        "roles_needed": ["Frontend Developer"],
    }]})
    phase = pm.get_phase(phase_id)
    phase["agents"] = ["old-agent"]
    phase["rebuild_file_manifest"] = {"files": [
        {"path": "frontend/src/App.tsx", "mode": "patch", "owner_type": "frontend"},
        {"path": "frontend/src/api.ts", "mode": "preserve", "owner_type": "frontend"},
        {"path": "frontend/src/New.tsx", "mode": "create", "owner_type": "frontend"},
    ]}
    for path in ("frontend/src/App.tsx", "frontend/src/api.ts", "frontend/src/New.tsx"):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path, encoding="utf-8")
        pm.register_file(path, "old-agent", "Frontend Developer", phase_id)
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        agents={"old-agent": {
            "id": "old-agent", "phase_id": phase_id, "status": "failed",
            "output_files": [
                "frontend/src/App.tsx", "frontend/src/api.ts", "frontend/src/New.tsx",
            ],
        }},
        subprojects=[],
        qc_results={},
    )
    routes_phases.projects[project_id] = ctx
    routes_phases._phase_managers[project_id] = pm

    async def no_persist():
        return None

    monkeypatch.setattr(routes_phases, "_persist_all_async", no_persist)
    monkeypatch.setattr(routes_execution, "_persist_execution_state", lambda: None)
    monkeypatch.setattr(routes_phases.expert_lock, "get_active_locks", lambda **_kwargs: [])
    try:
        asyncio.run(routes_phases._reset_phase(
            project_id, phase_id, preserve_rebuild_state=True,
        ))
    finally:
        routes_phases.projects.pop(project_id, None)
        routes_phases._phase_managers.pop(project_id, None)

    assert (tmp_path / "frontend/src/App.tsx").is_file()
    assert (tmp_path / "frontend/src/api.ts").is_file()
    assert not (tmp_path / "frontend/src/New.tsx").exists()
    assert pm.file_registry["frontend/src/App.tsx"]["rebuild_mode"] == "patch"
    assert pm.file_registry["frontend/src/api.ts"]["rebuild_mode"] == "preserve"
    assert "agent_id" not in pm.file_registry["frontend/src/App.tsx"]
    assert "agent_id" not in pm.file_registry["frontend/src/api.ts"]
    assert "frontend/src/New.tsx" not in pm.file_registry
    assert phase["rebuild_file_manifest"]["files"]


def test_phase_tech_stack_is_inferred_from_the_full_contract():
    stack = _infer_phase_tech_stack({
        "description": "Build the FastAPI API with SQLite",
        "deliverables": ["pytest + FastAPI TestClient coverage"],
    }, "React + TypeScript client")

    assert stack == [
        "FastAPI (Python)", "React", "TypeScript", "SQLite", "pytest",
        "FastAPI TestClient",
    ]


def test_phase_list_exposes_persisted_qc_round_summary(monkeypatch):
    project_id = "qc-round-summary"
    phase_id = "phase-1"
    ctx = SimpleNamespace(
        agents={},
        qc_results={
            phase_id: {
                "qa": {"qc_round": 4, "fixed_count": 3, "score": 100}
            }
        },
    )
    pm = SimpleNamespace(to_dict=lambda: {
        "project_id": project_id,
        "phases": [{"phase_id": phase_id, "name": "backend", "agents": []}],
    })
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)

    result = asyncio.run(routes_phases.list_project_phases(project_id))

    phase = result["phases"][0]
    assert phase["qc_round"] == 4
    assert phase["qc_fixed_count"] == 3
    assert phase["qc_score"] == 100


def test_six_mixed_roles_start_with_non_conflicting_file_leases(monkeypatch, tmp_path):
    monkeypatch.setenv("METIS_TEST_MODE", "1")
    project_id = "mixed-role-start"
    phase_id = "phase-1"
    roles_and_files = [
        ("Frontend Developer", "frontend/src/App.tsx"),
        ("Backend Developer", "backend/src/server.js"),
        ("QA Engineer", "tests/api.test.js"),
        ("Security Engineer", "security/policy.md"),
        ("Solution Architect", "docs/architecture/system.md"),
        ("Full-stack Developer", "README.md"),
    ]
    pm = PhaseManager(project_id, tmp_path)
    pm.init_phases_from_plan({"phases": [{
        "phase_id": phase_id,
        "name": "mixed delivery",
        "description": "Deliver a complete mixed-role system",
        "roles_needed": [role for role, _path in roles_and_files],
        "deliverables": [path for _role, path in roles_and_files],
        "agent_count": len(roles_and_files),
    }]})
    subprojects = [
        {
            "id": f"sp-{index}", "name": role, "description": f"Produce {path}",
            "agent_role": role, "roles_needed": [role], "phase_id": phase_id,
            "status": "pending", "progress": 0,
        }
        for index, (role, path) in enumerate(roles_and_files, 1)
    ]
    ctx = SimpleNamespace(
        project_id=project_id, workspace=tmp_path, status="planning",
        description="mixed role project", pm=SimpleNamespace(context_summary="context"),
        agents={}, subprojects=subprojects, qc_results={},
        _derive_project_status=lambda _rows: "running",
    )
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)

    claimed = []

    def claim(**kwargs):
        scope = list(kwargs["file_scope"])
        for previous in claimed:
            assert not any(
                _scopes_overlap(left, right) for left in previous for right in scope
            ), f"lease conflict: {previous} / {scope}"
        claimed.append(scope)
        return {
            "success": True,
            "lock_id": f"lock-{len(claimed)}",
            "leased_until": 9999999999,
        }

    class EmptyExpertPool:
        def match_experts(self, **_kwargs):
            return []

    async def fake_persist():
        return None

    def discard_task(coro, name=""):
        coro.close()

    monkeypatch.setattr("core.expert_pool.get_expert_pool", lambda: EmptyExpertPool())
    monkeypatch.setattr("core.global_agent_pool.get_global_agent_pool", lambda: object())
    monkeypatch.setattr("core.dispatch_integration.register_phase_agents", lambda *_args: 6)
    monkeypatch.setattr(routes_phases.expert_lock, "atomic_claim_lock", claim)
    monkeypatch.setattr(routes_phases, "_persist_all_async", fake_persist)
    monkeypatch.setattr(routes_execution, "_safe_create_task", discard_task)

    result = asyncio.run(routes_phases.start_phase(project_id, phase_id))

    assert result["success"] is True
    assert len(result["created_agents"]) == 6
    assert len(claimed) == 6
    assert len(pm.phase_agents[phase_id]) == 6
    policies = {
        agent["expert_type"]: agent["artifact_policy"]["kind"]
        for agent in result["created_agents"]
    }
    assert policies["architecture"] == "architecture_document"
    assert policies["backend"] == "runnable"
    architect = next(
        agent for agent in result["created_agents"]
        if agent["expert_type"] == "architecture"
    )
    assert (
        architect["execution_contract"]["artifact_policy"]["kind"]
        == "architecture_document"
    )

    # A single-role phase must keep the same canonical ownership used by
    # mixed-role phases so later integration does not see a second app tree.
    claimed.clear()
    single_project_id = "single-frontend-start"
    single_phase_id = "phase-frontend"
    single_pm = PhaseManager(single_project_id, tmp_path / "single")
    single_pm.init_phases_from_plan({"phases": [{
        "phase_id": single_phase_id,
        "name": "frontend",
        "description": (
            "Build the React frontend; verify dist/index.html and src/App.tsx "
            "after the build"
        ),
        "roles_needed": ["Frontend Developer"],
        "deliverables": ["frontend/src/App.tsx"],
        "agent_count": 1,
    }]})
    single_pm.project_contract = {
        "locked": True,
        "required_files": [{
            "path": "frontend/src/App.tsx",
            "owner_type": "frontend",
            "phase_id": single_phase_id,
            "task_id": "frontend-task",
            "required": True,
        }],
    }
    single_subprojects = [{
        "id": "sp-frontend",
        "name": "Frontend Developer",
        "description": "Produce frontend/src/App.tsx",
        "agent_role": "Frontend Developer",
        "roles_needed": ["Frontend Developer"],
        "phase_id": single_phase_id,
        "status": "pending",
        "progress": 0,
    }]
    single_ctx = SimpleNamespace(
        project_id=single_project_id,
        workspace=tmp_path / "single",
        status="planning",
        description="single frontend project",
        pm=SimpleNamespace(context_summary="context"),
        agents={},
        subprojects=single_subprojects,
        qc_results={},
        _derive_project_status=lambda _rows: "running",
    )
    monkeypatch.setitem(routes_phases.projects, single_project_id, single_ctx)
    monkeypatch.setitem(routes_phases._phase_managers, single_project_id, single_pm)

    single_result = asyncio.run(routes_phases.start_phase(single_project_id, single_phase_id))

    assert single_result["success"] is True
    assert len(single_result["created_agents"]) == 1
    allowed = single_result["created_agents"][0]["allowed_path_prefixes"]
    assert allowed == ["frontend/"]
    assert "src/" not in allowed
    assert single_result["created_agents"][0]["required_delivery_files"] == [
        "frontend/src/App.tsx"
    ]


def test_single_role_without_explicit_task_split_creates_one_agent(monkeypatch, tmp_path):
    """A planner's inflated agent_count must not clone one undivided role."""
    monkeypatch.setenv("METIS_TEST_MODE", "1")
    project_id = "single-role-no-split"
    phase_id = "phase-backend"
    pm = PhaseManager(project_id, tmp_path)
    pm.init_phases_from_plan({"phases": [{
        "phase_id": phase_id,
        "name": "Backend core",
        "description": "Implement the FastAPI backend as one cohesive task",
        "roles_needed": ["Backend Developer"],
        "agent_count": 3,
    }]})
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        status="planning",
        description="single backend role",
        pm=SimpleNamespace(context_summary="context"),
        agents={},
        subprojects=[],
        qc_results={},
        _derive_project_status=lambda _rows: "running",
    )
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)

    class EmptyExpertPool:
        def match_experts(self, **_kwargs):
            return []

    claimed = []

    def claim(**kwargs):
        claimed.append(list(kwargs["file_scope"]))
        return {
            "success": True,
            "lock_id": f"lock-{len(claimed)}",
            "leased_until": 9999999999,
        }

    async def fake_persist():
        return None

    def discard_task(coro, name=""):
        coro.close()

    monkeypatch.setattr("core.expert_pool.get_expert_pool", lambda: EmptyExpertPool())
    monkeypatch.setattr("core.global_agent_pool.get_global_agent_pool", lambda: object())
    monkeypatch.setattr("core.dispatch_integration.register_phase_agents", lambda *_args: 1)
    monkeypatch.setattr(routes_phases.expert_lock, "atomic_claim_lock", claim)
    monkeypatch.setattr(routes_phases, "_persist_all_async", fake_persist)
    monkeypatch.setattr(routes_execution, "_safe_create_task", discard_task)

    result = asyncio.run(routes_phases.start_phase(project_id, phase_id))

    assert result["success"] is True
    assert len(result["created_agents"]) == 1
    assert len(ctx.subprojects) == 1
    assert len(pm.phase_agents[phase_id]) == 1
    assert len(claimed) == 1


def test_locked_phase_does_not_create_agents_without_locked_tasks(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("METIS_TEST_MODE", "1")
    project_id = "locked-task-owner-only"
    phase_id = "phase-1"
    task_id = "phase-1-task-1"
    pm = PhaseManager(project_id, tmp_path)
    pm.init_phases_from_plan({"phases": [{
        "phase_id": phase_id,
        "name": "Backend implementation",
        "description": "Build a small Express API",
        "roles_needed": [
            "Developer",
            "DevOps Engineer",
            "Backend Developer",
        ],
        "agent_count": 3,
    }]})
    phase = pm.get_phase(phase_id)
    phase["plan_contract_validated"] = True
    phase["task_contract"] = [{
        "task_id": task_id,
        "name": "Implement API",
        "roles": ["Developer"],
        "dependencies": [],
        "acceptance_criteria": [],
    }]
    phase["expert_requirements"] = [{
        "task_id": task_id,
        "task_name": "Implement API",
        "required_role": "Developer",
        "dependencies": [],
        "acceptance_criteria": [],
        "required_files": ["package.json", "src/server.js"],
    }]
    pm.project_contract = {
        "locked": True,
        "contract_version": 3,
        "requirements_revision": 1,
        "requirements_digest": "sha256:" + ("a" * 64),
        "required_files": [
            {
                "path": "package.json",
                "owner_type": "devops",
                "phase_id": phase_id,
                "task_id": task_id,
                "required": True,
            },
            {
                "path": "src/server.js",
                "owner_type": "backend",
                "phase_id": phase_id,
                "task_id": task_id,
                "required": True,
            },
        ],
    }
    subprojects = [
        {
            "id": f"sp-{index}",
            "name": role,
            "description": f"{role} work",
            "agent_role": role,
            "roles_needed": [role],
            "phase_id": phase_id,
            "status": "pending",
            "progress": 0,
        }
        for index, role in enumerate(
            phase["roles_needed"],
            1,
        )
    ]
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        status="planning",
        description="locked phase",
        pm=SimpleNamespace(context_summary="context"),
        agents={},
        subprojects=subprojects,
        qc_results={},
        _derive_project_status=lambda _rows: "running",
    )
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)

    class EmptyExpertPool:
        def match_experts(self, **_kwargs):
            return []

    claimed = []

    def claim(**kwargs):
        claimed.append(list(kwargs["file_scope"]))
        return {
            "success": True,
            "lock_id": f"lock-{len(claimed)}",
            "leased_until": 9999999999,
        }

    async def create_coordinator(*_args, **_kwargs):
        return {"run_id": "coordinator-run"}

    async def fake_persist():
        return None

    def discard_task(coro, name=""):
        coro.close()

    monkeypatch.setattr(
        "core.expert_pool.get_expert_pool",
        lambda: EmptyExpertPool(),
    )
    monkeypatch.setattr(
        "core.global_agent_pool.get_global_agent_pool",
        lambda: object(),
    )
    monkeypatch.setattr(
        "core.dispatch_integration.register_phase_agents",
        lambda *_args: 1,
    )
    monkeypatch.setattr(
        routes_phases,
        "_create_phase_coordinator_run",
        create_coordinator,
    )
    monkeypatch.setattr(
        routes_phases,
        "_migrate_invalid_phase_plan",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        routes_execution._run_registry,
        "claim",
        lambda *_args, **_kwargs: {"lease_expires_at": 9999999999},
    )
    monkeypatch.setattr(routes_phases.expert_lock, "atomic_claim_lock", claim)
    monkeypatch.setattr(routes_phases, "_persist_all_async", fake_persist)
    monkeypatch.setattr(routes_phases, "_safe_create_task", discard_task)

    result = asyncio.run(routes_phases.start_phase(project_id, phase_id))

    assert result["success"] is True
    assert len(result["created_agents"]) == 1
    assert result["created_agents"][0]["expert_type"] == "fullstack_engineer"
    assert result["created_agents"][0]["assigned_task_ids"] == [task_id]
    assert {
        "package.json",
        "src/server.js",
    }.issubset(result["created_agents"][0]["allowed_path_prefixes"])
    assert len(claimed) == 1


def test_phase_wide_root_files_do_not_create_cross_role_lock_conflicts(monkeypatch, tmp_path):
    monkeypatch.setenv("METIS_TEST_MODE", "1")
    project_id = "integration-root-files"
    phase_id = "phase-4"
    description = (
        "Integrate the application. Write README.md, .env.example, Dockerfile, "
        "and ensure root package.json starts the service."
    )
    roles = ["Backend Developer", "Frontend Developer", "DevOps Engineer"]
    pm = PhaseManager(project_id, tmp_path)
    pm.init_phases_from_plan({"phases": [{
        "phase_id": phase_id,
        "name": "Integration and deployment",
        "description": description,
        "roles_needed": roles,
        "deliverables": ["README.md", ".env.example", "Dockerfile", "package.json"],
        "agent_count": 2,
    }]})
    phase = pm.get_phase(phase_id)
    phase["rebuild_base_description"] = description
    phase["description"] = description + "\n\nPrevious QA rebuild notes"
    phase["acceptance_criteria"] = ["health endpoint passes", "frontend builds"]
    phase["task_contract"] = [
        {"task_id": "backend-task", "name": "Build API", "roles": ["Backend Developer"]},
        {"task_id": "frontend-task", "name": "Build UI", "roles": ["Frontend Developer"]},
    ]
    phase["expert_requirements"] = [{
        "task_id": "deploy-task",
        "task_name": "Deploy application",
        "required_role": "DevOps Engineer",
        "acceptance_criteria": ["container starts"],
    }]
    phase["rebuild_file_manifest"] = {
        "all": [
            "README.md", ".env.example", "Dockerfile", "package.json",
            "backend/src/routes/auth.js", "frontend/src/App.tsx",
        ],
        # Simulate stale ownership produced by an older phase partition.
        "by_subproject": {
            phase_id: [
                "README.md", ".env.example", "Dockerfile", "package.json",
                "backend/src/routes/auth.js", "frontend/src/App.tsx",
            ]
        },
    }
    ctx = SimpleNamespace(
        project_id=project_id, workspace=tmp_path, status="planning",
        description="industrial application", pm=SimpleNamespace(context_summary="context"),
        agents={}, qc_results={},
        subprojects=[{
            "id": phase_id, "name": "Integration and deployment",
            "description": description, "roles_needed": roles,
            "phase_id": phase_id, "status": "pending", "progress": 0,
        }],
        _derive_project_status=lambda _rows: "running",
    )
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)
    repair_key = f"{project_id}-{phase_id}"
    monkeypatch.setitem(routes_phases._auto_repair_states, repair_key, {
        "running": False,
        "status": "quality_regressed",
        "messages": [{"role": "system", "content": "historical failure"}],
        "action_required": {"options": ["rebuild_phase"]},
        "needs_manual": True,
    })
    claimed = []

    def claim(**kwargs):
        scope = list(kwargs["file_scope"])
        for previous in claimed:
            assert not any(
                _scopes_overlap(left, right) for left in previous for right in scope
            ), f"lease conflict: {previous} / {scope}"
        claimed.append(scope)
        return {"success": True, "lock_id": f"lock-{len(claimed)}", "leased_until": 9999999999}

    requested_expert_types = []

    class EmptyExpertPool:
        def match_experts(self, **kwargs):
            requested_expert_types.append(kwargs.get("required_expert_type"))
            return []

    async def fake_persist():
        return None

    def discard_task(coro, name=""):
        coro.close()

    monkeypatch.setattr("core.expert_pool.get_expert_pool", lambda: EmptyExpertPool())
    monkeypatch.setattr("core.global_agent_pool.get_global_agent_pool", lambda: object())
    monkeypatch.setattr("core.dispatch_integration.register_phase_agents", lambda *_args: 3)
    monkeypatch.setattr(routes_phases.expert_lock, "atomic_claim_lock", claim)
    monkeypatch.setattr(routes_phases, "_persist_all_async", fake_persist)
    monkeypatch.setattr(routes_execution, "_safe_create_task", discard_task)

    result = asyncio.run(routes_phases.start_phase(project_id, phase_id))

    assert result["success"] is True
    assert len(result["created_agents"]) == 3
    assert set(requested_expert_types) == {
        "backend", "frontend", "devops",
    }
    devops = next(agent for agent in result["created_agents"] if agent["expert_type"] == "devops")
    backend = next(agent for agent in result["created_agents"] if agent["expert_type"] == "backend")
    assert {"README.md", ".env.example", "Dockerfile"}.issubset(devops["allowed_path_prefixes"])
    assert not {"README.md", ".env.example", "Dockerfile"}.intersection(backend["allowed_path_prefixes"])
    assert {"README.md", ".env.example", "Dockerfile"}.issubset(devops["required_rebuild_files"])
    assert "backend/src/routes/auth.js" in backend["required_rebuild_files"]
    assert all(
        agent["execution_contract"]["defer_fix_qc"] is True
        for agent in result["created_agents"]
    )
    for agent in result["created_agents"]:
        prompt = agent["execution_contract"]["description"]
        assert "FULL PHASE PLAN (READ-ONLY CONTEXT)" in prompt
        assert '"roles":' in prompt
        assert "backend-task" in prompt
        assert "frontend-task" in prompt
        assert "health endpoint passes" in prompt
        assert "Implement only the current Agent's assigned tasks and authorized paths" in prompt
    assert routes_phases._auto_repair_states[repair_key]["status"] == "awaiting_execution"
    assert routes_phases._auto_repair_states[repair_key]["action_required"] is None


def test_partial_blocker_resolution_keeps_rebuild_converging():
    comparison = routes_phases._compare_rebuild_issue_snapshots(
        [
            {"id": "blocker-a", "severity": "error", "status": "open"},
            {"id": "blocker-b", "severity": "critical", "status": "open"},
        ],
        [{"id": "blocker-b", "severity": "critical", "status": "open"}],
    )

    assert comparison["status"] == "converging"
    assert comparison["resolved_blockers"]
    assert comparison["new_blockers"] == []


def test_rebuild_comparison_control_flow_for_convergence_and_rollback():
    old = [
        {"fingerprint": "blocker-a", "severity": "error", "status": "open"},
        {"fingerprint": "blocker-b", "severity": "critical", "status": "open"},
    ]
    cases = [
        (
            [{"fingerprint": "blocker-b", "severity": "critical", "status": "open"}],
            "converging",
            False,
        ),
        (copy.deepcopy(old), "rebuild_no_progress", True),
        (
            [{
                "fingerprint": "new-blocker",
                "severity": "critical",
                "status": "open",
            }],
            "rebuild_regressed",
            True,
        ),
        ([], "converged", False),
    ]

    for after, expected_status, expected_rollback in cases:
        comparison = routes_phases._compare_rebuild_issue_snapshots(old, after)
        assert comparison["status"] == expected_status
        assert (
            routes_phases._rebuild_comparison_requires_rollback(
                comparison["status"],
            )
            is expected_rollback
        )


def _ownership_fixture(*, owner_type="frontend", task_id="ui-task"):
    phase = {
        "phase_id": "phase-ui",
        "expert_requirements": [{
            "task_id": "ui-task",
            "required_role": "Frontend Developer",
        }],
    }
    pm = SimpleNamespace(
        project_contract={
            "locked": True,
            "required_files": [{
                "path": "frontend/src/App.tsx",
                "phase_id": "phase-ui",
                "task_id": task_id,
                "owner_type": owner_type,
                "required": True,
            }],
        },
        file_registry={},
    )
    return phase, pm


def test_rebuild_rejects_registry_owner_that_conflicts_with_contract():
    phase, pm = _ownership_fixture()
    pm.file_registry["frontend/src/App.tsx"] = {
        "phase_id": "phase-ui",
        "agent_id": "old-backend",
        "owner_type": "backend",
    }
    old_agents = {
        "old-backend": {
            "id": "old-backend",
            "phase_id": "phase-ui",
            "expert_type": "backend",
            "output_files": ["frontend/src/App.tsx"],
        },
    }

    with pytest.raises(routes_phases.HTTPException) as captured:
        routes_phases._rebuild_contract_ownership(pm, phase, old_agents)

    assert captured.value.status_code == 409
    assert captured.value.detail["code"] == "rebuild_registry_contract_owner_conflict"


def test_rebuild_rejects_multiple_historical_claimants_for_one_path():
    phase, pm = _ownership_fixture()
    old_agents = {
        agent_id: {
            "id": agent_id,
            "phase_id": "phase-ui",
            "expert_type": "frontend",
            "output_files": ["frontend/src/App.tsx"],
        }
        for agent_id in ("old-ui-a", "old-ui-b")
    }

    with pytest.raises(routes_phases.HTTPException) as captured:
        routes_phases._rebuild_contract_ownership(pm, phase, old_agents)

    assert captured.value.status_code == 409
    assert captured.value.detail["code"] == "rebuild_historical_owner_conflict"


def test_rebuild_rejects_contract_file_with_unknown_locked_task():
    phase, pm = _ownership_fixture(task_id="missing-task")

    with pytest.raises(routes_phases.HTTPException) as captured:
        routes_phases._rebuild_contract_ownership(pm, phase, {})

    assert captured.value.status_code == 409
    assert captured.value.detail == {
        "code": "rebuild_unknown_contract_task",
        "path": "frontend/src/App.tsx",
        "task_id": "missing-task",
    }


def _install_phase(monkeypatch, tmp_path):
    monkeypatch.setenv("METIS_TEST_MODE", "1")

    async def fake_persist():
        return None

    monkeypatch.setattr(routes_phases, "_persist_all_async", fake_persist)
    project_id = "phase-rebuild-test"
    phase_id = "phase-1"
    pm = PhaseManager(project_id, tmp_path)
    pm.init_phases_from_plan({
        "project_contract": {
            "locked": True,
            "required_files": [
                {"path": "backend/src/api.py", "owner_type": "backend", "phase_id": phase_id, "required": True},
                {"path": "backend/src/auth.py", "owner_type": "backend", "phase_id": phase_id, "required": True},
            ],
        },
        "phases": [{
            "phase_id": phase_id,
            "name": "API implementation",
            "description": "Implement both API modules",
            "deliverables": [
                "backend/src/api.py", "backend/src/auth.py", "backend/package.json"
            ],
            "roles_needed": ["Backend engineer", "Backend engineer"],
            "agent_count": 2,
        }]
    })
    phase = pm.get_phase(phase_id)
    phase["subprojects"] = ["sp-api", "sp-auth"]

    old_agents = {
        "old-api": {
            "id": "old-api", "project_id": project_id, "phase_id": phase_id,
            "subproject_id": "sp-api", "role": "Backend engineer",
            "expert_type": "backend", "status": "completed",
            "allowed_path_prefixes": ["backend/src/api.py"],
            "output_files": ["backend/src/api.py", "output/sp-api_execution.log"],
        },
        "old-auth": {
            "id": "old-auth", "project_id": project_id, "phase_id": phase_id,
            "subproject_id": "sp-auth", "role": "Backend engineer",
            "expert_type": "backend", "status": "completed",
            "allowed_path_prefixes": ["backend/src/auth.py"],
            "output_files": ["backend/src/auth.py", "output/sp-auth_execution.log"],
        },
    }
    phase["agents"] = list(old_agents)
    pm.phase_agents[phase_id] = list(old_agents)
    for path, agent_id, subproject_id in (
        ("backend/src/api.py", "old-api", "sp-api"),
        ("backend/src/auth.py", "old-auth", "sp-auth"),
        ("output/sp-api_execution.log", "old-api", "sp-api"),
        ("output/sp-auth_execution.log", "old-auth", "sp-auth"),
    ):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {subproject_id}\n", encoding="utf-8")
        pm.register_file(path, agent_id, "Backend engineer", phase_id, subproject_id)

    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        status="executing",
        description="test project",
        pm=SimpleNamespace(context_summary="test context"),
        agents=old_agents,
        subprojects=[
            {
                "id": "sp-api", "name": "API", "phase_id": phase_id,
                "agent_id": "old-api", "agent_role": "Backend engineer",
                "roles_needed": ["Backend engineer"], "description": "Implement API",
                "status": "completed", "progress": 100,
            },
            {
                "id": "sp-auth", "name": "Auth", "phase_id": phase_id,
                "agent_id": "old-auth", "agent_role": "Backend engineer",
                "roles_needed": ["Backend engineer"], "description": "Implement auth",
                "status": "completed", "progress": 100,
            },
        ],
        qc_results={},
        _derive_project_status=lambda _rows: "running",
    )
    monkeypatch.setitem(routes_phases.projects, project_id, ctx)
    monkeypatch.setitem(routes_phases._phase_managers, project_id, pm)
    routes_phases._auto_repair_states[f"{project_id}-{phase_id}"] = {
        "running": False,
        "round": 5,
        "status": "awaiting_decision",
        "messages": [],
        "phase_name": phase["name"],
        "issue_report": {"backend/src/api.py": [{"severity": "warning", "message": "warning"}]},
    }
    return project_id, phase_id, ctx, pm


def test_manual_fix_is_a_real_pause_option(monkeypatch, tmp_path):
    project_id, phase_id, _ctx, _pm = _install_phase(monkeypatch, tmp_path)

    result = asyncio.run(routes_phases.start_auto_repair(project_id, phase_id, "manual_fix"))

    assert result["success"] is True
    assert result["status"]["status"] == "awaiting_manual_fix"
    assert result["status"]["running"] is False
    assert result["status"]["action_required"]["options"] == [
        "manual_fix", "retry_cycle", "rebuild_phase"
    ]


class _BlockedManualRetryMachine:
    def __init__(self, scope):
        self.state = "blocked"
        self.scope = copy.deepcopy(scope)
        self.bind_calls = []
        self.evidence = []

    def to_dict(self):
        return {"run_id": "manual-retry-run", "scope": copy.deepcopy(self.scope)}

    def bind_artifact_generation(self, **kwargs):
        self.bind_calls.append(copy.deepcopy(kwargs))
        self.scope = copy.deepcopy(kwargs["scope_snapshot"])
        return self.to_dict()

    def resume_after_manual_fix(self):
        self.state = "verifying"
        return self.to_dict()

    def record_evidence(self, **kwargs):
        self.evidence.append(copy.deepcopy(kwargs))
        return self.to_dict()


def _patch_blocked_manual_retry(monkeypatch, machine):
    async def fake_persist():
        return None

    monkeypatch.setattr(
        routes_phases, "_supervisor_quality_machine",
        lambda _ctx, _phase_id: machine,
    )
    monkeypatch.setattr(
        routes_phases, "_store_supervisor_quality_machine",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(routes_phases, "_persist_all_async", fake_persist)
    monkeypatch.setattr(
        routes_phases, "_safe_create_task",
        lambda coro, **_kwargs: coro.close(),
    )


def test_manual_fix_baseline_and_issue_paths_are_write_once(monkeypatch, tmp_path):
    project_id, phase_id, _ctx, _pm = _install_phase(monkeypatch, tmp_path)
    state = routes_phases._auto_repair_states[f"{project_id}-{phase_id}"]
    state["review_result"] = {
        "issues": [{"file_path": "backend/src/api.py", "status": "open"}],
    }

    asyncio.run(routes_phases.start_auto_repair(project_id, phase_id, "manual_fix"))
    first_scope = copy.deepcopy(state["manual_fix_scope"])
    first_issue_paths = list(state["manual_fix_issue_paths"])
    assert state["manual_fix_session_active"] is True

    (tmp_path / "backend/src/api.py").write_text("# edited\n", encoding="utf-8")
    state["review_result"] = {
        "issues": [{"file_path": "backend/src/auth.py", "status": "open"}],
    }
    asyncio.run(routes_phases.start_auto_repair(project_id, phase_id, "manual_fix"))

    assert state["manual_fix_scope"] == first_scope
    assert state["manual_fix_issue_paths"] == first_issue_paths
    assert state["manual_fix_session_active"] is True
    assert first_issue_paths == ["backend/src/api.py"]


def test_manual_retry_uses_issue_paths_frozen_at_pause(monkeypatch, tmp_path):
    project_id, phase_id, ctx, _pm = _install_phase(monkeypatch, tmp_path)
    state = routes_phases._auto_repair_states[f"{project_id}-{phase_id}"]
    state["review_result"] = {
        "issues": [{"file_path": "backend/src/api.py", "status": "open"}],
    }
    locked_scope = routes_phases._supervisor_scope_snapshot(ctx, phase_id)
    machine = _BlockedManualRetryMachine(locked_scope)
    _patch_blocked_manual_retry(monkeypatch, machine)

    asyncio.run(routes_phases.start_auto_repair(project_id, phase_id, "manual_fix"))
    state["review_result"] = {
        "issues": [{"file_path": "backend/src/auth.py", "status": "open"}],
    }
    (tmp_path / "backend/src/api.py").write_text("# repaired\n", encoding="utf-8")

    result = asyncio.run(
        routes_phases.start_auto_repair(project_id, phase_id, "retry_cycle")
    )

    assert result["success"] is True
    assert len(machine.bind_calls) == 1
    assert machine.bind_calls[0]["transition_reason"] == "manual_fix"
    assert "manual_fix_scope" not in state
    assert "manual_fix_issue_paths" not in state
    assert "manual_fix_session_active" not in state


def test_manual_retry_rejects_zero_change_legacy_adoption(monkeypatch, tmp_path):
    project_id, phase_id, ctx, _pm = _install_phase(monkeypatch, tmp_path)
    state = routes_phases._auto_repair_states[f"{project_id}-{phase_id}"]
    state["review_result"] = {
        "issues": [{"file_path": "backend/src/api.py", "status": "open"}],
    }
    locked_scope = routes_phases._supervisor_scope_snapshot(ctx, phase_id)
    machine = _BlockedManualRetryMachine(locked_scope)
    _patch_blocked_manual_retry(monkeypatch, machine)

    # Simulate legacy drift that predates the explicit manual-fix pause.
    (tmp_path / "backend/src/api.py").write_text(
        "# pre-pause drift\n", encoding="utf-8",
    )
    asyncio.run(routes_phases.start_auto_repair(project_id, phase_id, "manual_fix"))

    with pytest.raises(Exception) as blocked:
        asyncio.run(
            routes_phases.start_auto_repair(project_id, phase_id, "retry_cycle")
        )

    assert blocked.value.status_code == 409
    assert machine.bind_calls == []
    assert state["status"] == "awaiting_manual_fix"


def test_manual_retry_allows_zero_file_change_after_reviewer_recovers(
    monkeypatch, tmp_path,
):
    project_id, phase_id, ctx, _pm = _install_phase(monkeypatch, tmp_path)
    state = routes_phases._auto_repair_states[f"{project_id}-{phase_id}"]
    state["review_result"] = {
        "issues": [{
            "file_path": "backend/src/api.py",
            "status": "open",
            "message": (
                "Functionality review could not produce valid acceptance "
                "evidence (ValueError)"
            ),
        }],
    }
    locked_scope = routes_phases._supervisor_scope_snapshot(ctx, phase_id)
    machine = _BlockedManualRetryMachine(locked_scope)
    _patch_blocked_manual_retry(monkeypatch, machine)

    (tmp_path / "backend/src/api.py").write_text(
        "# drift before reviewer recovery\n", encoding="utf-8",
    )
    asyncio.run(routes_phases.start_auto_repair(
        project_id, phase_id, "manual_fix",
    ))
    result = asyncio.run(routes_phases.start_auto_repair(
        project_id, phase_id, "retry_cycle",
    ))

    assert result["success"] is True
    assert len(machine.bind_calls) == 1
    assert machine.bind_calls[0]["transition_reason"] == "manual_fix"


def test_retry_decision_is_idempotent_while_cycle_is_running(monkeypatch, tmp_path):
    project_id, phase_id, _ctx, _pm = _install_phase(monkeypatch, tmp_path)
    state = routes_phases._auto_repair_states[f"{project_id}-{phase_id}"]
    state.update({"running": True, "status": "running"})
    monkeypatch.setattr(
        routes_phases,
        "_safe_create_task",
        lambda *_args, **_kwargs: pytest.fail("duplicate repair loop was scheduled"),
    )

    result = asyncio.run(
        routes_phases.start_auto_repair(project_id, phase_id, "retry_cycle")
    )

    assert result["success"] is True
    assert result["already_running"] is True
    assert result["status"] == routes_phases._public_auto_repair_state(state)
    assert result["status"] is not state


@pytest.mark.parametrize(
    "stale_status",
    ["pre_qa_verifying", "continuing", "blocked"],
)
def test_retry_recovers_stale_pre_qa_waiting_engineer(monkeypatch, tmp_path, stale_status):
    project_id, phase_id, ctx, _pm = _install_phase(monkeypatch, tmp_path)
    ctx.supervisor_quality_runs = {
        phase_id: {
            "run_id": "qa-run",
            "status": "waiting_engineer",
            "state": "waiting_engineer",
            "waiting_for": ["old-api"],
            "next_action": {"type": "repair_pre_qa_failures", "agent_ids": ["old-api"]},
        }
    }
    state = routes_phases._auto_repair_states[f"{project_id}-{phase_id}"]
    state.update({
        "running": stale_status != "blocked",
        "status": stale_status,
        "action_required": (
            {
                "message": (
                    "QA scope digest does not match the scope locked at run start"
                ),
                "options": ["manual_fix", "rebuild_phase"],
            }
            if stale_status == "blocked"
            else None
        ),
        "pre_qa_result": {
            "issues": [{
                "code": "command_gate_failed",
                "gate": "test-backend",
                "path": "backend",
            }],
        },
    })
    scheduled = []

    monkeypatch.setattr(
        routes_phases,
        "_apply_deterministic_pre_qa_repairs",
        lambda *_args, **_kwargs: [{"kind": "deterministic_patch", "path": "backend/src/__tests__/smoke.test.ts"}],
    )
    monkeypatch.setattr(
        routes_phases,
        "_restore_pre_qa_agents_after_deterministic_repair",
        lambda *_args, **_kwargs: ["old-api"],
    )
    monkeypatch.setattr(
        routes_phases,
        "_safe_create_task",
        lambda coro, **_kwargs: (scheduled.append(coro), coro.close()),
    )

    result = asyncio.run(
        routes_phases.start_auto_repair(project_id, phase_id, "retry_cycle")
    )

    assert result["success"] is True
    assert result.get("already_running") is not True
    assert scheduled
    assert state["running"] is True
    assert state["status"] == "pre_qa_verifying"
    assert state["action_required"] is None


def test_initial_start_rejects_legacy_quality_pass_projection(
    monkeypatch, tmp_path,
):
    project_id, phase_id, _ctx, pm = _install_phase(monkeypatch, tmp_path)
    state = routes_phases._auto_repair_states[f"{project_id}-{phase_id}"]
    state.update({
        "running": False,
        "status": "passed",
        "review_result": {"passed": True, "score": 94},
        "round": 1,
        "repair_attempts": 1,
        "total_rounds": 2,
        "lifetime_qc_runs": 2,
    })
    monkeypatch.setattr(
        routes_phases,
        "_safe_create_task",
        lambda *_args, **_kwargs: pytest.fail("passed quality cycle was restarted"),
    )

    with pytest.raises(Exception) as blocked:
        asyncio.run(routes_phases.start_auto_repair(project_id, phase_id))

    assert blocked.value.status_code == 409
    assert state["status"] == "stale"
    assert state["running"] is False
    assert state["needs_manual"] is True
    assert state["total_rounds"] == 2
    assert state["repair_attempts"] == 1
    phase = pm.get_phase(phase_id)
    assert phase["reviewed"] is False
    assert phase["review_passed"] is False
    assert phase["status"] == "qa_pending"


def test_retry_decision_starts_new_bounded_cycle_after_historical_limit(monkeypatch, tmp_path):
    project_id, phase_id, _ctx, _pm = _install_phase(monkeypatch, tmp_path)
    state = routes_phases._auto_repair_states[f"{project_id}-{phase_id}"]
    state.update({
        "running": False,
        "status": "awaiting_decision",
        "round": 10,
        "repair_attempts": 10,
        "total_rounds": 13,
        "lifetime_qc_runs": 13,
        "needs_manual": True,
    })
    scheduled = []
    monkeypatch.setattr(
        routes_phases,
        "_safe_create_task",
        lambda coro, **_kwargs: (scheduled.append(coro), coro.close()),
    )

    result = asyncio.run(
        routes_phases.start_auto_repair(project_id, phase_id, "retry_cycle")
    )

    assert result["success"] is True
    assert scheduled
    assert state["running"] is True
    assert state["status"] == "continuing"
    assert state["action_required"] is None
    assert state["needs_manual"] is False
    # Historical totals remain visible, but no longer block the new cycle.
    assert state["round"] == 10
    assert state["repair_attempts"] == 10


def test_restarting_unfinished_qc_does_not_reset_lifetime_counters(monkeypatch, tmp_path):
    project_id, phase_id, ctx, _pm = _install_phase(monkeypatch, tmp_path)
    ctx.agents["old-api"]["status"] = "completed"
    monkeypatch.setitem(routes_execution.execution_status, "old-api", {"status": "completed"})
    state = routes_phases._auto_repair_states[f"{project_id}-{phase_id}"]
    state.update({
        "running": False, "status": "awaiting_decision", "round": 3,
        "repair_attempts": 3, "total_rounds": 7, "lifetime_qc_runs": 7,
    })
    scheduled = []
    monkeypatch.setattr(
        routes_phases, "_safe_create_task",
        lambda coro, **_kwargs: (scheduled.append(coro), coro.close()),
    )

    result = asyncio.run(routes_phases.start_auto_repair(project_id, phase_id))

    assert result["success"] is True
    assert scheduled
    assert state["round"] == 3
    assert state["repair_attempts"] == 3
    assert state["total_rounds"] == 7
    assert state["lifetime_qc_runs"] == 7


def test_fifth_repair_runs_before_final_verification(monkeypatch, tmp_path):
    project_id, phase_id, ctx, pm = _install_phase(monkeypatch, tmp_path)
    key = f"{project_id}-{phase_id}"
    state = routes_phases._auto_repair_states[key]
    state.update({
        "running": True, "round": 4, "repair_attempts": 4,
        "total_rounds": 0, "lifetime_qc_runs": 0, "status": "running",
    })

    failed_entry = {
        "passed": False, "score": 80, "error_count": 1, "warning_count": 0,
        "issues": ["one error"],
        "issues_detail": [{
            "file_path": "backend/src/api.py", "severity": "error",
            "message": "one error", "status": "open",
            "responsible_agent_id": "old-api",
        }],
    }
    passed_entry = {
        "passed": True, "score": 100, "error_count": 0, "warning_count": 0,
        "issues": [], "issues_detail": [],
    }
    qc_results = iter([failed_entry, passed_entry])
    monkeypatch.setattr(
        "api.routes_supervisor._run_qc_for_subproject",
        lambda *_args, **_kwargs: next(qc_results),
    )
    repairs = []

    async def fake_repair(*args, **_kwargs):
        repairs.append(args[2])
        return {"status": "completed", "total": 1, "completed": 1, "failed": 0, "files_changed": True}

    async def fake_persist():
        return None

    monkeypatch.setattr(routes_phases, "_repair_all_issue_owners", fake_repair)
    monkeypatch.setattr(routes_phases, "_persist_all_async", fake_persist)

    asyncio.run(routes_phases._run_auto_repair_loop(project_id, phase_id, False))

    assert repairs == [5]
    assert state["round"] == 5
    assert state["repair_attempts"] == 5
    assert state["lifetime_qc_runs"] == 2
    assert state["status"] == "passed"


def test_user_authorized_cycle_can_repair_after_historical_tenth_attempt(monkeypatch, tmp_path):
    project_id, phase_id, _ctx, _pm = _install_phase(monkeypatch, tmp_path)
    key = f"{project_id}-{phase_id}"
    state = routes_phases._auto_repair_states[key]
    state.update({
        "running": True,
        "round": 10,
        "repair_attempts": 10,
        "total_rounds": 13,
        "lifetime_qc_runs": 13,
        "status": "continuing",
    })
    failed_entry = {
        "passed": False, "score": 80, "error_count": 1, "warning_count": 0,
        "issues": ["one error"],
        "issues_detail": [{
            "file_path": "backend/src/api.py", "severity": "error",
            "message": "one error", "status": "open",
            "responsible_agent_id": "old-api",
        }],
    }
    passed_entry = {
        "passed": True, "score": 100, "error_count": 0, "warning_count": 0,
        "issues": [], "issues_detail": [],
    }
    qc_results = iter([failed_entry, passed_entry])
    monkeypatch.setattr(
        "api.routes_supervisor._run_qc_for_subproject",
        lambda *_args, **_kwargs: next(qc_results),
    )
    repairs = []

    async def fake_repair(*args, **_kwargs):
        repairs.append(args[2])
        return {"status": "completed", "total": 1, "completed": 1, "failed": 0, "files_changed": True}

    async def fake_persist():
        return None

    monkeypatch.setattr(routes_phases, "_repair_all_issue_owners", fake_repair)
    monkeypatch.setattr(routes_phases, "_persist_all_async", fake_persist)

    asyncio.run(routes_phases._run_auto_repair_loop(project_id, phase_id, False))

    assert repairs == [11]
    assert state["round"] == 11
    assert state["repair_attempts"] == 11
    assert state["status"] == "passed"


def test_auto_repair_rolls_back_when_blockers_increase(monkeypatch, tmp_path):
    project_id, phase_id, ctx, pm = _install_phase(monkeypatch, tmp_path)
    key = f"{project_id}-{phase_id}"
    state = routes_phases._auto_repair_states[key]
    state.update({"running": True, "round": 0, "status": "running"})
    target = tmp_path / "backend/src/api.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("original", encoding="utf-8")

    def qc_entry(count):
        return {
            "passed": False, "score": 50, "error_count": count, "warning_count": 0,
            "issues": [f"error-{index}" for index in range(count)],
            "issues_detail": [{
                "file_path": "backend/src/api.py", "severity": "error",
                "message": f"error-{index}", "status": "open",
                "responsible_agent_id": "old-api",
            } for index in range(count)],
        }

    results = iter([qc_entry(1), qc_entry(2)])

    def fake_qc(*_args, **_kwargs):
        result = next(results)
        ctx.qc_results[phase_id] = {"qa": result}
        return result

    monkeypatch.setattr(
        "api.routes_supervisor._run_qc_for_subproject",
        fake_qc,
    )

    async def fake_repair(*_args, **_kwargs):
        target.write_text("regressed", encoding="utf-8")
        return {"status": "completed", "total": 1, "completed": 1, "failed": 0}

    async def fake_persist():
        return None

    monkeypatch.setattr(routes_phases, "_repair_all_issue_owners", fake_repair)
    monkeypatch.setattr(routes_phases, "_persist_all_async", fake_persist)

    asyncio.run(routes_phases._run_auto_repair_loop(project_id, phase_id, False))

    assert state["status"] == "quality_regressed"
    assert pm.get_phase(phase_id)["status"] == "qa_blocked"
    assert target.read_text(encoding="utf-8") == "original"
    assert len(ctx.qc_results[phase_id]["qa"]["issues_detail"]) == 1


def test_auto_repair_splits_same_owner_work_by_file(monkeypatch, tmp_path):
    project_id, phase_id, ctx, _pm = _install_phase(monkeypatch, tmp_path)
    calls = []
    released = []
    ctx.agents["old-api"]["execution_contract"] = {
        "tech_stack": ["Python", "FastAPI"],
    }

    async def fake_run_agent_task(**kwargs):
        calls.append(kwargs)
        ctx.agents[kwargs["agent_id"]]["status"] = "completed"
        return {"success": True}

    async def fake_persist():
        return None

    monkeypatch.setattr(routes_execution, "_run_agent_task", fake_run_agent_task)
    monkeypatch.setattr(routes_phases, "_persist_all_async", fake_persist)
    monkeypatch.setattr(routes_execution, "_reset_fix_attempt", lambda _agent_id: None)
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "atomic_claim_lock",
        lambda **kwargs: {
            "success": True,
            "lock_id": f"repair-lock:{kwargs['task_id']}",
            "leased_until": 9999999999,
        },
    )
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "renew_lock",
        lambda _lock_id: {"success": False},
    )
    monkeypatch.setattr(
        routes_phases.expert_lock,
        "release_lock",
        lambda lock_id: released.append(lock_id) or {"success": True},
    )
    entry = {"issues_detail": [
        {
            "file_path": "backend/src/api.py", "severity": "error",
            "message": "api broken", "fix_hint": "repair api", "status": "open",
            "responsible_agent_id": "old-api",
        },
        {
            "file_path": "backend/src/extra.py", "severity": "error",
            "message": "extra broken", "fix_hint": "repair extra", "status": "open",
            "responsible_agent_id": "old-api",
        },
        {
            "file_path": "backend/src/manual.py", "severity": "error",
            "message": "manual only", "status": "needs_manual",
            "responsible_agent_id": "old-api",
        },
    ]}
    ctx.qc_results[phase_id] = {"qa": entry}

    asyncio.run(routes_phases._repair_all_issue_owners(
        project_id, phase_id, 1, entry, {"messages": []}, False, {"api_key": "test"}
    ))

    assert len(calls) == 2
    assert [issue["status"] for issue in entry["issues_detail"]] == [
        "fixing", "fixing", "needs_manual",
    ]
    assert "backend/src/api.py" in calls[0]["description"]
    assert "backend/src/extra.py" not in calls[0]["description"]
    assert "backend/src/extra.py" in calls[1]["description"]
    assert "backend/src/api.py" not in calls[1]["description"]
    assert {call["agent_id"] for call in calls} == {"old-api"}
    assert all(call["tech_stack"] == ["Python", "FastAPI"] for call in calls)
    assert [
        call["artifact_policy"]["required_files"] for call in calls
    ] == [["backend/src/api.py"], ["backend/src/extra.py"]]
    assert [
        call["artifact_policy"]["allowed_path_prefixes"] for call in calls
    ] == [["backend/src/api.py"], ["backend/src/extra.py"]]
    assert len(released) == 2
    assert ctx.agents["old-api"].get("required_rebuild_files", []) == []


def test_rebuild_recreates_and_executes_agents_with_per_task_file_ownership(
    monkeypatch, tmp_path
):
    project_id, phase_id, ctx, pm = _install_phase(monkeypatch, tmp_path)
    pm.project_contract = {
        "locked": True,
        "contract_version": 3,
        "requirements_revision": 1,
        "requirements_digest": "sha256:" + ("1" * 64),
        "required_files": [
            {
                "path": "backend/src/api.py", "owner_type": "backend",
                "phase_id": phase_id, "task_id": "api-task",
                "criterion": "backend/src/api.py is delivered",
                "evidence_spec": "registry_byte_digest", "required": True,
            },
            {
                "path": "backend/src/auth.py", "owner_type": "backend",
                "phase_id": phase_id, "task_id": "api-task",
                "criterion": "backend/src/auth.py remains unchanged",
                "evidence_spec": "registry_byte_digest", "required": True,
            },
            {
                "path": "backend/package.json", "owner_type": "backend",
                "phase_id": phase_id, "task_id": "package-task",
                "criterion": "backend/package.json is delivered",
                "evidence_spec": "registry_byte_digest", "required": True,
            },
        ],
    }
    phase = pm.get_phase(phase_id)
    phase["roles_needed"] = ["backend"]
    phase["task_contract"] = [
        {
            "task_id": "api-task", "name": "Implement API",
                "description": "Deliver backend/src/api.py",
                "required_role": "backend", "roles": ["backend"],
                "dependencies": [], "acceptance_criteria": [],
                "required_files": [
                    "backend/src/api.py",
                    "backend/src/auth.py",
                ],
            },
            {
                "task_id": "package-task", "name": "Package backend",
                "description": "Deliver backend/package.json",
                "required_role": "backend", "roles": ["backend"],
                "dependencies": [], "acceptance_criteria": [],
                "required_files": ["backend/package.json"],
            },
    ]
    phase["expert_requirements"] = [
        {
            "task_id": task["task_id"],
            "task_name": task["name"],
            "task_description": task["description"],
                "required_role": task["required_role"],
                "dependencies": list(task["dependencies"]),
                "acceptance_criteria": [],
                "source_requirement_ids": [],
                "required_files": [
                    row["path"]
                    for row in pm.project_contract["required_files"]
                    if row["task_id"] == task["task_id"]
                ],
            }
            for task in phase["task_contract"]
        ]
    phase["plan_contract_validated"] = True
    phase["phase_plan"] = {
        "schema_version": "phase-plan/v1",
        "phase_id": phase_id,
        "summary": "Rebuild the backend delivery",
        "effective_technical_requirements": [],
        "tasks": [
            {
                "task_id": task["task_id"],
                "name": task["name"],
                "objective": task["description"],
                "functional_details": [task["description"]],
                "implementation": task["description"],
                "dependencies": list(task["dependencies"]),
                "acceptance_criteria": list(task["acceptance_criteria"]),
            }
            for task in phase["task_contract"]
        ],
        "assignments": [],
        "expert_pool_revision": 1,
    }
    old_ids = set(ctx.agents)
    launched = []
    tasks = []

    class EmptyExpertPool:
        def list_experts(self, **_kwargs):
            return []

        def match_experts(self, **_kwargs):
            return []

    async def fake_persist():
        return None

    async def skip_automatic_qa(_ctx):
        return None

    quality_gate = routes_execution._start_phase_quality_cycle_if_ready

    class FakeExecutionAgent:
        def __init__(self, agent_id, attempt_scope):
            self.agent_id = agent_id
            self.attempt_scope = dict(attempt_scope or {})

        def execute_task(self, **kwargs):
            output_files = list(dict.fromkeys(
                self.attempt_scope.get("required_files") or []
            ))
            for path in output_files:
                target = tmp_path / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("{}" if path.endswith(".json") else "// generated\n", encoding="utf-8")
            launched.append({
                "agent_id": self.agent_id,
                "subproject_id": kwargs["subproject_id"],
                "description": kwargs["description"],
                "required_rebuild_files": output_files,
            })
            return {
                "success": True, "status": "completed", "progress": 100,
                "output_files": output_files, "logs": ["executed"],
                "validation": {"valid": True, "issues": []},
            }

    def capture_task(coro, name=""):
        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return task

    def record_fixture_delivery(**kwargs):
        return {
            "phase_delivery_path": tmp_path / ".metis" / "phase-delivery.json",
            "responsibility_path": tmp_path / ".metis" / "responsibility.json",
            "responsibility_ledger_revision": 1,
            "files": [
                {"path": path}
                for path in (
                    kwargs.get("delivery_evidence", {}).get("files") or []
                )
                if isinstance(path, str)
            ],
        }

    monkeypatch.setattr("core.expert_pool.get_expert_pool", lambda: EmptyExpertPool())
    monkeypatch.setattr("core.global_agent_pool.get_global_agent_pool", lambda: object())
    monkeypatch.setattr("core.dispatch_integration.register_phase_agents", lambda *_args: 2)
    monkeypatch.setattr(routes_phases, "_persist_all_async", fake_persist)
    monkeypatch.setattr(routes_execution, "_persist_all_async", fake_persist)
    monkeypatch.setattr(routes_execution, "_persist_execution_state", lambda: None)
    monkeypatch.setattr(
        routes_execution,
        "record_successful_task_delivery",
        record_fixture_delivery,
    )
    monkeypatch.setattr(
        routes_execution, "_start_phase_quality_cycle_if_ready", skip_automatic_qa,
    )
    monkeypatch.setattr(
        routes_execution, "_make_exec_agent",
        lambda _ctx, agent_id, user_api_config=None, **kwargs: FakeExecutionAgent(
            agent_id, kwargs.get("attempt_scope")
        ),
    )
    monkeypatch.setattr(routes_execution, "_safe_create_task", capture_task)
    monkeypatch.setattr(routes_phases, "_safe_create_task", capture_task)

    async def run_rebuild():
        result = await routes_phases.start_auto_repair(project_id, phase_id, "rebuild_phase")
        response_status = result["status"]["status"]
        await asyncio.gather(*tasks)
        return result, response_status

    result, response_status = asyncio.run(run_rebuild())

    assert result["success"] is True
    assert result["created_agents"] == 1
    assert response_status == "rebuild_started"
    assert result["status"]["status"] in {"rebuild_started", "starting", "running", "passed"}
    assert result["status"]["action_required"] is None
    assert result["status"]["needs_manual"] is False
    assert not old_ids.intersection(ctx.agents)
    assert len(ctx.agents) == 1
    new_ids = set(ctx.agents)
    assert set(pm.get_phase(phase_id)["agents"]) == new_ids
    assert set(pm.phase_agents[phase_id]) == new_ids
    executing_ids = {
        agent_id for agent_id, agent in ctx.agents.items()
        if not agent.get("verification_only")
    }
    assert {item["agent_id"] for item in launched} == executing_ids
    assert len(launched) == 2
    assert [item["subproject_id"] for item in launched] == ["sp-api", "sp-api"]
    assert [item["required_rebuild_files"] for item in launched] == [
        ["backend/src/api.py", "backend/src/auth.py"],
        ["backend/package.json"],
    ]
    assert "api-task" in launched[0]["description"]
    assert "package-task" not in launched[0]["description"]
    assert "package-task" in launched[1]["description"]
    assert "api-task" not in launched[1]["description"]
    executing_agent = ctx.agents[next(iter(executing_ids))]
    receipts = executing_agent["task_execution_receipts"]
    assert list(receipts) == ["api-task", "package-task"]
    assert receipts["api-task"]["status"] == "succeeded"
    assert receipts["package-task"]["status"] == "succeeded"
    assert receipts["api-task"]["required_files"] == [
        "backend/src/api.py", "backend/src/auth.py",
    ]
    assert receipts["package-task"]["required_files"] == ["backend/package.json"]
    assert receipts["api-task"]["completion_run_id"] != receipts["package-task"]["completion_run_id"]

    qc_starts = []

    async def record_qc_start(_project_id, _phase_id, *_args, **_kwargs):
        qc_starts.append(len(launched))
        state = routes_phases._auto_repair_states[f"{project_id}-{phase_id}"]
        state["running"] = True
        state["status"] = "running"
        return {"success": True}

    monkeypatch.setattr(routes_phases, "start_auto_repair", record_qc_start)
    repair_state = routes_phases._auto_repair_states[f"{project_id}-{phase_id}"]
    repair_state["running"] = False
    repair_state["status"] = "rebuild_started"
    final_receipt = receipts.pop("package-task")
    asyncio.run(quality_gate(ctx))
    assert qc_starts == []
    receipts["package-task"] = final_receipt
    asyncio.run(quality_gate(ctx))
    asyncio.run(quality_gate(ctx))
    assert qc_starts == [2]
    manifest_files = {
        item["path"]: item
        for item in pm.get_phase(phase_id)["rebuild_file_manifest"]["files"]
    }
    assert manifest_files["backend/src/auth.py"]["mode"] == "patch"
    assert "Implement auth" not in launched[0]["description"]
    delivery_owners = [
        path
        for agent in ctx.agents.values()
        for path in agent["required_delivery_files"]
    ]
    assert sorted(delivery_owners) == [
        "backend/package.json", "backend/src/api.py", "backend/src/auth.py"
    ]
    assert len(delivery_owners) == len(set(delivery_owners))
    allowed_sets = [set(agent["allowed_path_prefixes"]) for agent in ctx.agents.values()]
    assert "backend/src/api.py" in allowed_sets[0]
    assert not any(agent.get("verification_only") for agent in ctx.agents.values())
    rebuild_manifest = pm.get_phase(phase_id)["rebuild_file_manifest"]
    assert rebuild_manifest["writer_counts"] == {
        "backend/package.json": 1,
        "backend/src/api.py": 1,
        "backend/src/auth.py": 1,
    }
    assert rebuild_manifest["by_task_id"] == {
        "api-task": ["backend/src/api.py", "backend/src/auth.py"],
        "package-task": ["backend/package.json"],
    }
    assert rebuild_manifest["preserve_verifier_paths"] == []
    assert all(not path.startswith("output/") for path in rebuild_manifest["all"])
    assert all(
        not path.startswith("output/")
        for agent in ctx.agents.values()
        for path in agent["required_rebuild_files"] + agent["required_delivery_files"]
    )
    agent_statuses = {
        agent_id: agent["status"] for agent_id, agent in ctx.agents.items()
    }
    assert all(agent["status"] == "completed" for agent in ctx.agents.values()), agent_statuses
    assert all(
        routes_execution.execution_status[agent_id]["status"] == "completed"
        for agent_id in executing_ids
    )
