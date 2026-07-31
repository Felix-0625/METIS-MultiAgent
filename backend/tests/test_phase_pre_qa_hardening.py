import hashlib
from types import SimpleNamespace

from api import routes_execution, routes_phases
from core.phase_manager import PhaseManager
from core.project_contract import FrozenContractDict


def _context(tmp_path, project_id="preqa-hardening"):
    return SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        name="Hardening",
        agents={},
        subprojects=[],
    )


def test_phase_pre_qa_filters_future_phase_files_and_does_not_materialize_templates(
    monkeypatch, tmp_path,
):
    ctx = _context(tmp_path)
    pm = PhaseManager(ctx.project_id, tmp_path)
    pm.phases = [{"phase_id": "phase-1"}, {"phase_id": "phase-2"}]
    pm.project_contract = FrozenContractDict({
        "locked": True,
        "technology_stack": ["Python"],
        "required_files": (
            FrozenContractDict({"path": "README.md", "owner_type": "devops", "phase_id": "phase-1", "required": True}),
            FrozenContractDict({"path": "Dockerfile", "owner_type": "devops", "phase_id": "phase-2", "required": True}),
        ),
    })
    (tmp_path / "README.md").write_text("ready", encoding="utf-8")
    agent = {
        "id": "devops-1", "phase_id": "phase-1", "expert_type": "devops",
        "allowed_path_prefixes": ["README.md"],
    }
    ctx.agents[agent["id"]] = agent
    pm.register_file("README.md", agent["id"], "devops", "phase-1")
    monkeypatch.setitem(routes_phases._phase_managers, ctx.project_id, pm)

    result = routes_phases._execute_phase_pre_qa(ctx, "phase-1")

    assert result["passed"] is True
    assert not (tmp_path / "Dockerfile").exists()


def test_phase_pre_qa_accepts_execution_selected_files_without_planned_manifest(
    monkeypatch, tmp_path,
):
    ctx = _context(tmp_path, "preqa-no-manifest")
    pm = PhaseManager(ctx.project_id, tmp_path)
    payload = b"print('delivered')\n"
    (tmp_path / "generated.py").write_bytes(payload)
    ctx.agents["agent-1"] = {
        "id": "agent-1",
        "phase_id": "phase-1",
        "expert_type": "backend",
        "allowed_path_prefixes": ["generated.py"],
    }
    pm.phases = [{
        "phase_id": "phase-1",
        "phase_plan": {
            "schema_version": "phase-plan/v1",
            "tasks": [{"task_id": "phase-1-task-1"}],
        },
    }]
    pm.project_contract = {"locked": True, "required_files": []}
    monkeypatch.setitem(routes_phases._phase_managers, ctx.project_id, pm)
    monkeypatch.setattr(
        routes_phases,
        "load_phase_qa_scope",
        lambda **_kwargs: {
            "available": True,
            "issues": [],
            "incomplete_task_ids": [],
            "files": [{
                "path": "generated.py",
                "phase_id": "phase-1",
                "task_id": "phase-1-task-1",
                "agent_id": "agent-1",
                "expert_id": "expert-1",
                "agent_role": "backend",
                "revision": 1,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }],
        },
    )

    result = routes_phases._execute_phase_pre_qa(ctx, "phase-1")

    assert result.get("issues") == []
    assert result["passed"] is True, result
    assert pm.project_contract["required_files"] == []


def test_pathless_jwt_issue_routes_to_backend_agent():
    ctx = _context(SimpleNamespace())
    ctx.agents = {
        "devops": {"id": "devops", "phase_id": "1", "expert_type": "devops"},
        "backend": {"id": "backend", "phase_id": "1", "expert_type": "backend"},
        "frontend": {"id": "frontend", "phase_id": "1", "expert_type": "frontend"},
    }

    owner = routes_phases._match_issue_agent(ctx, "1", {
        "code": "jwt_implementation_missing",
        "gate": "security",
        "message": "JWT implementation is required",
    })

    assert owner["id"] == "backend"


def test_failed_agent_output_is_never_promoted_to_completed_without_evidence(tmp_path):
    ctx = _context(tmp_path, "recovery-no-evidence")
    (tmp_path / "result.txt").write_text("candidate", encoding="utf-8")
    ctx.agents["agent-1"] = {
        "id": "agent-1", "phase_id": "phase-1", "status": "failed",
        "output_files": ["result.txt"], "required_delivery_files": ["result.txt"],
    }
    ctx.subprojects = [{
        "id": "sp-1", "phase_id": "phase-1", "agent_id": "agent-1", "status": "failed",
    }]
    routes_execution.execution_status["agent-1"] = {"status": "failed"}
    try:
        routes_phases._recover_failed_agent_outputs(ctx, "phase-1")
        assert ctx.agents["agent-1"]["status"] == "failed"
        assert ctx.agents["agent-1"]["recovery_status"] == "verification_evidence_incomplete"
        assert ctx.subprojects[0]["status"] == "failed"
    finally:
        routes_execution.execution_status.pop("agent-1", None)


def test_digest_bound_recovery_stops_at_pending_verification(tmp_path):
    ctx = _context(tmp_path, "recovery-with-evidence")
    payload = b"candidate"
    (tmp_path / "result.txt").write_bytes(payload)
    evidence = {
        "run_id": "run-1",
        "files": [{
            "path": "result.txt", "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload),
        }],
    }
    ctx.agents["agent-1"] = {
        "id": "agent-1", "phase_id": "phase-1", "status": "failed",
        "lock_id": "lock-1", "lock_run_id": "run-1",
        "required_delivery_files": ["result.txt"], "delivery_evidence": evidence,
    }
    ctx.subprojects = [{
        "id": "sp-1", "phase_id": "phase-1", "agent_id": "agent-1", "status": "failed",
    }]
    routes_execution.execution_status["agent-1"] = {
        "status": "failed", "run_id": "run-1", "delivery_evidence": evidence,
    }
    try:
        routes_phases._recover_failed_agent_outputs(ctx, "phase-1")
        assert ctx.agents["agent-1"]["status"] == "pending_verification"
        assert routes_execution.execution_status["agent-1"]["status"] == "pending_verification"
        assert ctx.subprojects[0]["status"] == "pending_verification"
    finally:
        routes_execution.execution_status.pop("agent-1", None)
