import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api import routes_supervisor
from core.workspace_integrity import compute_delivery_manifest


def _project(tmp_path):
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    manifest = compute_delivery_manifest(tmp_path)
    return SimpleNamespace(
        project_id="signoff-contract",
        workspace=tmp_path,
        status="active",
        qc_results={
            "__whole_project__": {
                "qa": {
                    "passed": True,
                    "status": "passed",
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
        supervisor_quality_runs={
            "phase-1": {
                "status": "completed",
                "completion_gate": {"passed": True},
            }
        },
        agents={"agent-1": {"status": "completed", "progress": 100}},
        subprojects=[],
        supervisor=SimpleNamespace(
            dispatcher=SimpleNamespace(
                context=SimpleNamespace(decisions=[])
            )
        ),
    )


def _install_valid_dependencies(monkeypatch, project):
    monkeypatch.setattr(
        routes_supervisor,
        "_get_project",
        lambda _project_id: project,
    )
    monkeypatch.setattr(
        routes_supervisor,
        "load_final_qa_scope",
        lambda **_kwargs: {
            "available": True,
            "issues": [],
            "files": [
                {
                    "path": "app.py",
                    "agent_id": "agent-1",
                    "phase_id": "phase-1",
                }
            ],
        },
    )
    monkeypatch.setattr(
        routes_supervisor,
        "_signoff_adjustments",
        lambda _project_id: [],
    )
    monkeypatch.setitem(
        routes_supervisor._phase_managers,
        project.project_id,
        SimpleNamespace(
            phases=[{
                "phase_id": "phase-1",
                "status": "completed",
                "user_confirmed": True,
            }]
        ),
    )


def test_signoff_blocker_contract_has_fixed_fields():
    blocker = routes_supervisor._normalize_signoff_blocker({
        "code": "FINAL_QA_STALE",
        "scope": "project",
        "message": "stale",
        "action": "rerun",
    })

    assert tuple(blocker) == routes_supervisor.SIGNOFF_BLOCKER_FIELDS
    assert blocker == {
        "code": "FINAL_QA_STALE",
        "scope": "project",
        "target": None,
        "message": "stale",
        "action": "rerun",
        "owner": None,
    }


def test_signoff_returns_fixed_completed_contract(monkeypatch, tmp_path):
    project = _project(tmp_path)
    _install_valid_dependencies(monkeypatch, project)
    persisted = []

    async def persist():
        persisted.append(project.status)

    monkeypatch.setattr(routes_supervisor, "_persist_all_async", persist)

    result = asyncio.run(routes_supervisor.sign_off(project.project_id))

    assert result["passed"] is True
    assert result["status"] == "completed"
    assert result["artifact_sha256"] == compute_delivery_manifest(tmp_path)["artifact_sha256"]
    assert result["blockers"] == []
    assert result["receipt"]["schema_version"] == "metis/signoff-receipt/v1"
    assert result["receipt"]["artifact_sha256"] == result["artifact_sha256"]
    assert persisted == ["completed"]


def test_signoff_failure_uses_structured_blockers(monkeypatch, tmp_path):
    project = _project(tmp_path)
    _install_valid_dependencies(monkeypatch, project)
    (tmp_path / "app.py").write_text("print('changed')\n", encoding="utf-8")

    response = asyncio.run(routes_supervisor.sign_off(project.project_id))
    assert response.status_code == 409
    payload = json.loads(response.body)
    assert payload["passed"] is False
    assert payload["status"] == "blocked"
    assert payload["artifact_sha256"] is None
    assert tuple(payload["blockers"][0]) == routes_supervisor.SIGNOFF_BLOCKER_FIELDS
    assert payload["blockers"][0]["code"] == "FINAL_QA_STALE"


def test_signoff_status_is_read_only_and_idempotent(monkeypatch, tmp_path):
    project = _project(tmp_path)
    project.status = "completed"
    _install_valid_dependencies(monkeypatch, project)

    first = asyncio.run(routes_supervisor.get_signoff_status(project.project_id))
    second = asyncio.run(routes_supervisor.sign_off(project.project_id))

    assert first["status"] == "completed"
    assert first["receipt"] is None
    assert second["status"] == "completed"
    assert second["receipt"]["schema_version"] == "metis/signoff-receipt/v1"


def test_signoff_blocks_invalid_authoritative_delivery_documents(
    monkeypatch, tmp_path,
):
    project = _project(tmp_path)
    _install_valid_dependencies(monkeypatch, project)
    monkeypatch.setattr(
        routes_supervisor,
        "load_final_qa_scope",
        lambda **_kwargs: {
            "available": False,
            "issues": ["phase delivery missing"],
            "files": [],
        },
    )

    response = asyncio.run(routes_supervisor.sign_off(project.project_id))
    blocker = json.loads(response.body)["blockers"][0]
    assert blocker["code"] == "DELIVERY_DOCUMENTS_INVALID"
    assert blocker["scope"] == "project"


def test_signoff_maps_file_persistence_blocker_to_authoritative_owner(
    monkeypatch, tmp_path,
):
    project = _project(tmp_path)
    _install_valid_dependencies(monkeypatch, project)
    monkeypatch.setattr(
        routes_supervisor,
        "workspace_persistence_issues",
        lambda _workspace: ["app.py: file exceeds the durable snapshot limit"],
    )

    response = asyncio.run(routes_supervisor.sign_off(project.project_id))
    blocker = json.loads(response.body)["blockers"][0]
    assert blocker["scope"] == "file"
    assert blocker["target"] == "app.py"
    assert blocker["owner"] == "agent-1"


def test_signoff_blocks_open_rework(monkeypatch, tmp_path):
    project = _project(tmp_path)
    _install_valid_dependencies(monkeypatch, project)
    monkeypatch.setattr(
        routes_supervisor,
        "_signoff_adjustments",
        lambda _project_id: [{
            "id": "adj-1",
            "status": "awaiting_final_qa",
            "requires_final_qa": True,
        }],
    )

    response = asyncio.run(routes_supervisor.sign_off(project.project_id))
    blocker = json.loads(response.body)["blockers"][0]
    assert blocker["code"] == "REWORK_OPEN"
    assert blocker["target"] == "adj-1"


def test_signoff_rolls_back_completed_state_when_persistence_fails(
    monkeypatch, tmp_path,
):
    project = _project(tmp_path)
    _install_valid_dependencies(monkeypatch, project)

    async def fail_persist():
        raise RuntimeError("persist failed")

    monkeypatch.setattr(routes_supervisor, "_persist_all_async", fail_persist)

    with pytest.raises(RuntimeError, match="persist failed"):
        asyncio.run(routes_supervisor.sign_off(project.project_id))

    assert project.status == "active"
