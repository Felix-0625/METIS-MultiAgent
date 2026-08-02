import asyncio
import copy
import json
from types import SimpleNamespace

from api import routes_supervisor, websocket
from core.workspace_integrity import compute_delivery_manifest


async def _noop():
    return None


def _ready(monkeypatch, tmp_path):
    (tmp_path / "app.py").write_text("print('sealed')\n", encoding="utf-8")
    manifest = compute_delivery_manifest(tmp_path)
    decisions = SimpleNamespace(decisions=[])
    decisions.add_decision = lambda decision, reason: decisions.decisions.append(
        {"decision": decision, "reason": reason})
    project = SimpleNamespace(
        project_id="receipt-red", workspace=tmp_path, status="active",
        signoff_receipt=None,
        qc_results={"__whole_project__": {"qa": {
            "passed": True, "status": "passed", "delivery_manifest": manifest,
            "runtime_acceptance": {"enabled": True, "passed": True, "status": "passed",
                "artifact_sha256": manifest["artifact_sha256"],
                "artifact_manifest_rule_version": manifest["rule_version"]},
        }}},
        supervisor_quality_runs={"p1": {"status": "completed",
            "completion_gate": {"passed": True}}},
        agents={"a1": {"status": "completed", "progress": 100}}, subprojects=[],
        supervisor=SimpleNamespace(dispatcher=SimpleNamespace(context=decisions)),
    )
    monkeypatch.setattr(routes_supervisor, "_get_project", lambda _pid: project)
    monkeypatch.setitem(routes_supervisor._phase_managers, project.project_id,
        SimpleNamespace(phases=[{"phase_id": "p1", "status": "completed",
                                "user_confirmed": True}]))
    monkeypatch.setattr(routes_supervisor, "load_final_qa_scope", lambda **_kw: {
        "available": True, "issues": [],
        "files": [{"path": "app.py", "phase_id": "p1", "agent_id": "a1"}]})
    monkeypatch.setattr(routes_supervisor, "_signoff_adjustments", lambda _pid: [])
    monkeypatch.setattr(routes_supervisor, "_persist_all_async", _noop)
    return project, manifest


def test_signoff_returns_artifact_bound_immutable_receipt(monkeypatch, tmp_path):
    project, manifest = _ready(monkeypatch, tmp_path)
    result = asyncio.run(routes_supervisor.sign_off(project.project_id))
    receipt = result["receipt"]
    assert receipt["schema_version"] == "metis/signoff-receipt/v1"
    assert receipt["receipt_id"]
    assert receipt["project_id"] == project.project_id
    assert receipt["artifact_sha256"] == manifest["artifact_sha256"]
    assert receipt["signed_off_at"] > 0


def test_duplicate_signoff_replays_same_receipt(monkeypatch, tmp_path):
    project, _ = _ready(monkeypatch, tmp_path)
    first = asyncio.run(routes_supervisor.sign_off(project.project_id))
    second = asyncio.run(routes_supervisor.sign_off(project.project_id))
    assert first["receipt"] == second["receipt"]


def test_signoff_persists_before_single_terminal_event(monkeypatch, tmp_path):
    project, _ = _ready(monkeypatch, tmp_path)
    order = []

    async def persist():
        order.append("persist")

    async def broadcast(project_id, event_type, payload):
        order.append(("event", project_id, event_type, payload))

    monkeypatch.setattr(routes_supervisor, "_persist_all_async", persist)
    monkeypatch.setattr(websocket.manager, "broadcast", broadcast)
    result = asyncio.run(routes_supervisor.sign_off(project.project_id))
    assert order[0] == "persist"
    assert order[1][0:3] == ("event", project.project_id, "project.signoff.completed")
    assert order[1][3]["receipt_id"] == result["receipt"]["receipt_id"]
    asyncio.run(routes_supervisor.sign_off(project.project_id))
    assert len([item for item in order if isinstance(item, tuple)]) == 1


def test_signoff_persistence_failure_rolls_back_receipt_and_event(monkeypatch, tmp_path):
    project, _ = _ready(monkeypatch, tmp_path)
    events = []

    async def fail_persist():
        raise OSError("injected signoff commit failure")

    async def broadcast(*args):
        events.append(args)

    monkeypatch.setattr(routes_supervisor, "_persist_all_async", fail_persist)
    monkeypatch.setattr(websocket.manager, "broadcast", broadcast)
    original_status = project.status
    try:
        asyncio.run(routes_supervisor.sign_off(project.project_id))
    except OSError as exc:
        assert "signoff commit failure" in str(exc)
    else:
        raise AssertionError("persistence failure must propagate")
    assert project.status == original_status
    assert project.signoff_receipt is None
    assert events == []


def test_signoff_rejects_runtime_evidence_drift_without_replacing_receipt(monkeypatch, tmp_path):
    project, _ = _ready(monkeypatch, tmp_path)
    first = asyncio.run(routes_supervisor.sign_off(project.project_id))
    receipt = copy.deepcopy(first["receipt"])
    project.qc_results["__whole_project__"]["qa"]["runtime_acceptance"]["attempt"] = 2
    response = asyncio.run(routes_supervisor.sign_off(project.project_id))
    assert response.status_code == 409
    payload = json.loads(response.body)
    assert payload["blockers"][0]["code"] == "SIGNOFF_RECEIPT_STALE"
    assert project.signoff_receipt == receipt


def test_concurrent_duplicate_signoff_creates_one_receipt_and_event(monkeypatch, tmp_path):
    project, _ = _ready(monkeypatch, tmp_path)
    events = []

    async def broadcast(*args):
        events.append(args)

    monkeypatch.setattr(websocket.manager, "broadcast", broadcast)

    async def sign_many():
        return await asyncio.gather(
            *(routes_supervisor.sign_off(project.project_id) for _ in range(8))
        )

    results = asyncio.run(sign_many())
    receipts = [item["receipt"] for item in results if isinstance(item, dict)]
    assert len(receipts) == 8
    assert all(receipt == receipts[0] for receipt in receipts)
    assert len(events) == 1
