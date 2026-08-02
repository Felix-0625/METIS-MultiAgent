import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

from api import routes_engineer


def test_rectification_parser_accepts_prose_wrapped_json():
    parsed = routes_engineer._parse_rectification_response(
        'Proposal:\n```json\n{"summary":"fix","changes":[{"path":"a.py","content":"x = 1\\n"}]}\n```'
    )
    assert parsed["changes"][0]["path"] == "a.py"


def test_create_rectification_durably_binds_source_issue(monkeypatch):
    saved = {}
    defect = {
        "id": "defect-1", "defect_id": "defect-1", "observation_id": "obs-1",
        "_source_issue": {"verification_spec": {"kind": "runtime_acceptance"}},
    }
    monkeypatch.setattr(routes_engineer, "_get_project", lambda _pid: SimpleNamespace())
    monkeypatch.setattr(routes_engineer, "_find_canonical_defect", lambda *_args: defect)
    monkeypatch.setattr(routes_engineer, "_consultation_sessions", lambda _pid: [])
    monkeypatch.setattr(routes_engineer, "_save_consultation_sessions", lambda _pid, value: saved.setdefault("sessions", value))
    result = asyncio.run(routes_engineer.engineer_create_consultation(
        "project-1",
        routes_engineer.EngineerConsultationCreateRequest(mode="rectification", source_issue_id="defect-1"),
    ))
    binding = result["session"]["source_issue_binding"]
    assert binding["defect_id"] == "defect-1"
    assert binding["observation_id"] == "obs-1"
    assert saved["sessions"][0]["source_issue_binding"] == binding


def test_bound_rectification_failure_rolls_back_without_reopening_qa(monkeypatch, tmp_path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    issue = {"status": "needs_manual", "verification_spec": {"kind": "static_file"}}
    defect = {"id": "defect-1", "defect_id": "defect-1", "observation_id": "obs-1", "_source_issue": issue}
    proposal = {
        "id": "proposal-1", "status": "pending_confirm", "baseline_artifact_sha256": "digest-1",
        "source_issue_binding": {"defect_id": "defect-1", "observation_id": "obs-1"},
        "changes": [{"path": "app.py", "content": "value = 2\n"}],
    }
    sessions = [{"id": "session-1", "mode": "rectification", "messages": [], "proposal": proposal}]

    class Agent:
        def __init__(self):
            self.file_edit_counter = {}
            self.file_last_score = {}
            self.pending_fixes = {}
        def _resolve_within_workspace(self, rel):
            return (tmp_path / rel).resolve()
        def apply_fix(self, **kwargs):
            self._resolve_within_workspace(kwargs["file_path"]).write_text(kwargs["new_content"], encoding="utf-8")
            return {"success": True}

    async def failed_verification(*_args):
        return {"status": "failed", "passed": False, "reason": "original_check_failed"}
    async def persist():
        return None

    ctx = SimpleNamespace(workspace=str(tmp_path), project_id="project-1")
    monkeypatch.setattr(routes_engineer, "_get_project", lambda _pid: ctx)
    monkeypatch.setattr(routes_engineer, "_consultation_sessions", lambda _pid: sessions)
    monkeypatch.setattr(routes_engineer, "_save_consultation_sessions", lambda *_args: None)
    monkeypatch.setattr(routes_engineer, "_find_canonical_defect", lambda *_args: defect)
    monkeypatch.setattr(routes_engineer, "_get_engineer", lambda _pid: Agent())
    monkeypatch.setattr(routes_engineer, "compute_delivery_manifest", lambda _path: {"artifact_sha256": "digest-1"})
    monkeypatch.setattr(routes_engineer, "_engineer_apply_claim", lambda _pid: nullcontext())
    monkeypatch.setattr(routes_engineer, "project_write_guard", lambda *_args: nullcontext())
    monkeypatch.setattr(routes_engineer, "_run_engineer_targeted_verification", failed_verification)
    monkeypatch.setattr(routes_engineer, "_persist_all_async", persist)

    result = asyncio.run(routes_engineer.engineer_apply_rectification(
        "project-1",
        routes_engineer.EngineerRectificationApplyRequest(session_id="session-1", proposal_id="proposal-1"),
    ))
    assert result["success"] is False
    assert result["rolled_back"] is True
    assert result["requires_final_qa"] is False
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert issue["status"] == "needs_manual"
