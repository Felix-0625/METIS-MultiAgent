import asyncio
from types import SimpleNamespace

from api import routes_adjustments
from core.workspace_integrity import (
    compute_delivery_manifest,
    compute_workspace_digest,
)


def test_final_qa_status_restores_persisted_pass_after_restart(monkeypatch, tmp_path):
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    project = SimpleNamespace(
        workspace=tmp_path,
        qc_results={
            "__whole_project__": {
                "qa": {
                    "passed": True,
                    "status": "passed",
                    "score": 100,
                    "error_count": 0,
                    "warning_count": 0,
                    "fixed_count": 1,
                    "qc_round": 2,
                    "checked_at": 1234.5,
                    "issues_detail": [],
                        "workspace_digest": compute_workspace_digest(tmp_path),
                        "delivery_manifest": compute_delivery_manifest(tmp_path),
                }
            }
        }
    )
    monkeypatch.setattr(routes_adjustments, "_get_project", lambda _project_id: project)
    routes_adjustments._final_qa_status.clear()

    result = asyncio.run(routes_adjustments.get_final_qa_status("proj-test"))

    assert result["status"] == "passed"
    assert result["all_passed"] is True
    assert result["round"] == 2
    assert result["qc_summary"]["score"] == 100
    assert result["restored_from_persisted_result"] is True


def test_final_qa_status_remains_not_started_without_persisted_result(monkeypatch, tmp_path):
    project = SimpleNamespace(qc_results={}, workspace=tmp_path)
    monkeypatch.setattr(routes_adjustments, "_get_project", lambda _project_id: project)
    routes_adjustments._final_qa_status.clear()

    result = asyncio.run(routes_adjustments.get_final_qa_status("proj-test"))

    assert result == {"project_id": "proj-test", "status": "not_started"}


def test_final_qa_status_never_restores_pass_with_failed_runtime_evidence(monkeypatch, tmp_path):
    project = SimpleNamespace(
        workspace=tmp_path,
        qc_results={
            "__whole_project__": {
                "qa": {
                    "passed": True,
                    "status": "passed",
                    "qc_round": 3,
                    "runtime_acceptance": {
                        "enabled": True,
                        "passed": False,
                        "status": "update_failed",
                    },
                    "workspace_digest": compute_workspace_digest(tmp_path),
                }
            }
        }
    )
    monkeypatch.setattr(routes_adjustments, "_get_project", lambda _project_id: project)
    routes_adjustments._final_qa_status.clear()

    result = asyncio.run(routes_adjustments.get_final_qa_status("proj-test"))

    assert result["status"] == "failed"
    assert result["all_passed"] is False
    assert result["runtime_acceptance"]["status"] == "update_failed"


def test_final_qa_status_invalidates_pass_when_workspace_changes(monkeypatch, tmp_path):
    source = tmp_path / "app.py"
    source.write_text("print('good')\n", encoding="utf-8")
    approved_digest = compute_workspace_digest(tmp_path)
    approved_manifest = compute_delivery_manifest(tmp_path)
    source.write_text("raise RuntimeError('changed')\n", encoding="utf-8")
    project = SimpleNamespace(
        workspace=tmp_path,
        qc_results={
            "__whole_project__": {
                "qa": {
                    "passed": True,
                    "status": "passed",
                    "workspace_digest": approved_digest,
                    "delivery_manifest": approved_manifest,
                }
            }
        },
    )
    monkeypatch.setattr(routes_adjustments, "_get_project", lambda _project_id: project)
    routes_adjustments._final_qa_status.clear()

    result = asyncio.run(routes_adjustments.get_final_qa_status("proj-test"))

    assert result["status"] == "failed"
    assert result["all_passed"] is False
    assert result["workspace_digest_current"] is False


def test_final_qa_status_requires_exact_generation_recovery_before_retry(
    monkeypatch, tmp_path
):
    project = SimpleNamespace(
        workspace=tmp_path,
        qc_results={
            "__whole_project__": {
                "qa": {
                    "passed": False,
                    "status": "running",
                    "final_qa_run": {
                        "status": "qc_running_round_2",
                        "round": 2,
                        "current_step": "llm_quality_review",
                        "steps": [{"name": "infrastructure_preflight", "status": "passed"}],
                    },
                }
            }
        },
    )
    monkeypatch.setattr(routes_adjustments, "_get_project", lambda _project_id: project)
    routes_adjustments._final_qa_status.clear()

    result = asyncio.run(routes_adjustments.get_final_qa_status("proj-test"))

    assert result["status"] == "recovery_required"
    assert result["round"] == 2
    assert result["retryable"] is False
    assert result["action_required"]["options"] == ["inspect_recovery"]
    assert result["restored_from_persisted_result"] is True


def test_trigger_final_qa_is_idempotent_for_current_pass(monkeypatch, tmp_path):
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    qa_result = {
        "passed": True,
        "status": "passed",
        "workspace_digest": compute_workspace_digest(tmp_path),
        "delivery_manifest": compute_delivery_manifest(tmp_path),
    }
    project = SimpleNamespace(
        project_id="proj-test-idempotent",
        description="Deliver app.py",
        workspace=tmp_path,
        qc_results={"__whole_project__": {"qa": qa_result}},
        supervisor_quality_runs={
            "phase-1": {
                "status": "completed",
                "completion_gate": {"passed": True},
            }
        },
    )
    phase_manager = SimpleNamespace(
        phases=[{
            "phase_id": "phase-1",
            "status": "completed",
            "user_confirmed": True,
        }]
    )
    monkeypatch.setattr(routes_adjustments, "_get_project", lambda _project_id: project)
    monkeypatch.setattr(routes_adjustments, "_runtime_acceptance_required", lambda: False)
    project_id = "proj-test-idempotent"
    monkeypatch.setitem(routes_adjustments._phase_managers, project_id, phase_manager)
    routes_adjustments._final_qa_status.clear()

    result = asyncio.run(routes_adjustments.trigger_final_qa(project_id))

    assert result["already_completed"] is True
    assert result["status"] == "passed"
    assert qa_result["passed"] is True
    assert qa_result["workspace_digest"] == compute_workspace_digest(tmp_path)
