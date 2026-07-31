from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import routes_supervisor
from core.workspace_integrity import (
    compute_delivery_manifest,
    compute_workspace_digest,
)


def test_final_qa_to_signoff_api_gate_uses_current_artifact(
    monkeypatch, tmp_path,
) -> None:
    source = tmp_path / "app.py"
    source.write_text("print('verified')\n", encoding="utf-8")
    manifest = compute_delivery_manifest(tmp_path)
    project = SimpleNamespace(
        project_id="supervisor-component-gate",
        workspace=tmp_path,
        qc_results={
            "__whole_project__": {
                "qa": {
                    "passed": True,
                    "status": "passed",
                    "workspace_digest": compute_workspace_digest(tmp_path),
                    "delivery_manifest": manifest,
                    "runtime_acceptance": {
                        "enabled": True,
                        "passed": True,
                        "status": "passed",
                        "artifact_sha256": manifest["artifact_sha256"],
                        "artifact_manifest_rule_version": manifest["rule_version"],
                    },
                }
            }
        },
        supervisor=SimpleNamespace(
            sign_off=lambda project_id: {
                "success": True,
                "project_id": project_id,
            }
        ),
        supervisor_quality_runs={
            "phase-1": {
                "status": "completed",
                "completion_gate": {"passed": True},
            }
        },
        agents={
            "engineer-1": {
                "status": "completed",
                "progress": 100,
                "phase_id": "phase-1",
            }
        },
        subprojects=[
            {
                "id": "task-1",
                "phase_id": "phase-1",
                "agent_id": "engineer-1",
                "status": "completed",
                "progress": 100,
            }
        ],
        status="active",
    )
    phase_manager = SimpleNamespace(
        phases=[
            {
                "phase_id": "phase-1",
                "status": "completed",
                "user_confirmed": True,
            }
        ]
    )
    persist_calls = []

    async def persist() -> None:
        persist_calls.append(project.status)

    monkeypatch.setattr(
        routes_supervisor, "_get_project", lambda _project_id: project,
    )
    monkeypatch.setitem(
        routes_supervisor._phase_managers,
        project.project_id,
        phase_manager,
    )
    monkeypatch.setattr(routes_supervisor, "_persist_all_async", persist)
    monkeypatch.setattr(
        routes_supervisor,
        "load_final_qa_scope",
        lambda **_kwargs: {
            "available": True,
            "issues": [],
            "files": [{"path": "app.py", "agent_id": "engineer-1"}],
        },
    )
    monkeypatch.setattr(
        routes_supervisor,
        "_signoff_adjustments",
        lambda _project_id: [],
    )

    app = FastAPI()
    app.include_router(routes_supervisor.router)
    response = TestClient(app).post(
        f"/projects/{project.project_id}/signoff",
    )

    assert response.status_code == 200
    assert response.json()["passed"] is True
    assert response.json()["artifact_sha256"] == manifest["artifact_sha256"]
    assert project.status == "completed"
    assert persist_calls == ["completed"]
