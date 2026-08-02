import hashlib

from core import database
from core.delivery_documents import record_successful_task_delivery


def test_writer_materializes_exact_authoritative_database_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setattr(database, "_pg_pool", None)
    monkeypatch.setattr(database, "_sqlite_path", str(tmp_path / "state.sqlite3"))
    database.init_db()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = b"print('sealed')\n"
    (workspace / "app.py").write_bytes(payload)
    plan = {
        "schema_version": "phase-plan/v1", "phase_id": "phase-1",
        "summary": "sealed", "effective_technical_requirements": [],
        "tasks": [{"task_id": "task-1", "name": "app", "objective": "deliver",
                   "functional_details": [], "implementation": "python",
                   "dependencies": [], "acceptance_criteria": ["app.py exists"]}],
        "assignments": [{"expert_id": "expert-1", "task_ids": ["task-1"],
                         "responsibility": "app.py"}], "expert_pool_revision": 1,
    }
    record_successful_task_delivery(
        workspace=workspace, project_id="writer-byte-repro", phase_id="phase-1",
        phase_plan=plan, task_id="task-1", agent_id="agent-1",
        expert_id="expert-1", agent_role="backend", summary="sealed",
        delivery_evidence={"files": [{"path": "app.py",
            "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}]},
        baseline_files={},
    )
    authoritative = database.load_project_files("writer-byte-repro")
    for relative in (
        "docs/metis/file-responsibility.json",
        "docs/metis/phase-deliveries/phase-1.json",
    ):
        assert (workspace / relative).read_bytes() == authoritative[relative]["content"]
