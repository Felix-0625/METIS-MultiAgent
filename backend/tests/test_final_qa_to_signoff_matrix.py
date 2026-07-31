"""Large deterministic Final QA -> Signoff transition matrix."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api import routes_supervisor
from core.workspace_integrity import compute_delivery_manifest


_SCENARIOS = (
    ("ready", None),
    ("final_qa_failed", "FINAL_QA_NOT_PASSED"),
    ("runtime_failed", "FINAL_QA_RUNTIME_INVALID"),
    ("artifact_stale", "FINAL_QA_STALE"),
    ("documents_invalid", "DELIVERY_DOCUMENTS_INVALID"),
    ("phase_incomplete", "EXECUTION_STATE_INCOMPLETE"),
    ("rework_open", "REWORK_OPEN"),
    ("persistence_blocked", "PERSISTENCE_BLOCKED"),
)
_CASES = [
    (scenario, expected_code, index)
    for scenario, expected_code in _SCENARIOS
    for index in range(150)
]


@pytest.mark.parametrize(
    ("scenario", "expected_code", "index"),
    _CASES,
    ids=[
        f"{scenario}-{index}"
        for scenario, _, index in _CASES
    ],
)
def test_final_qa_to_signoff_transition_matrix(
    monkeypatch,
    tmp_path,
    scenario: str,
    expected_code: str | None,
    index: int,
) -> None:
    project_id = f"signoff-matrix-{scenario}-{index}"
    source = tmp_path / "app.py"
    source.write_text(f"print({index})\n", encoding="utf-8")
    manifest = compute_delivery_manifest(tmp_path)
    qa = {
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
    if scenario == "final_qa_failed":
        qa["passed"] = False
        qa["status"] = "failed"
    elif scenario == "runtime_failed":
        qa["runtime_acceptance"]["passed"] = False

    project = SimpleNamespace(
        project_id=project_id,
        workspace=tmp_path,
        status="active",
        qc_results={"__whole_project__": {"qa": qa}},
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
    phases = [{
        "phase_id": "phase-1",
        "status": "completed",
        "user_confirmed": True,
    }]
    if scenario == "phase_incomplete":
        phases[0]["status"] = "needs_rework"
    if scenario == "artifact_stale":
        source.write_text(f"print('changed-{index}')\n", encoding="utf-8")

    monkeypatch.setattr(
        routes_supervisor,
        "_get_project",
        lambda _project_id: project,
    )
    monkeypatch.setitem(
        routes_supervisor._phase_managers,
        project_id,
        SimpleNamespace(phases=phases),
    )
    monkeypatch.setattr(
        routes_supervisor,
        "load_final_qa_scope",
        lambda **_kwargs: {
            "available": scenario != "documents_invalid",
            "issues": (
                [{"message": "phase delivery missing"}]
                if scenario == "documents_invalid" else []
            ),
            "files": [{
                "path": "app.py",
                "agent_id": "agent-1",
                "phase_id": "phase-1",
            }],
        },
    )
    monkeypatch.setattr(
        routes_supervisor,
        "_signoff_adjustments",
        lambda _project_id: (
            [{
                "id": f"adj-{index}",
                "status": "awaiting_final_qa",
                "requires_final_qa": True,
            }]
            if scenario == "rework_open" else []
        ),
    )
    monkeypatch.setattr(
        routes_supervisor,
        "workspace_persistence_issues",
        lambda _workspace: (
            ["app.py: file exceeds the durable snapshot limit"]
            if scenario == "persistence_blocked" else []
        ),
    )
    persisted = []

    async def persist() -> None:
        persisted.append(project.status)

    monkeypatch.setattr(routes_supervisor, "_persist_all_async", persist)

    if expected_code is None:
        result = asyncio.run(routes_supervisor.sign_off(project_id))
        assert result["passed"] is True
        assert result["status"] == "completed"
        assert result["blockers"] == []
        assert result["artifact_sha256"] == manifest["artifact_sha256"]
        assert project.status == "completed"
        assert persisted == ["completed"]
        return

    response = asyncio.run(routes_supervisor.sign_off(project_id))
    assert response.status_code == 409
    payload = json.loads(response.body)
    assert payload["passed"] is False
    assert payload["status"] == "blocked"
    assert payload["artifact_sha256"] is None
    assert payload["blockers"][0]["code"] == expected_code
    assert tuple(payload["blockers"][0]) == routes_supervisor.SIGNOFF_BLOCKER_FIELDS
    assert project.status == "active"
    assert persisted == []
