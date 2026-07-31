import hashlib
import json

import pytest
from types import SimpleNamespace

from core import qc_review_contract as qc
from api import routes_supervisor
from agents import quality_agents


def _record(value):
    return {"content": json.dumps(value).encode("utf-8")}


def _sources(tmp_path):
    source = b"export function addTask(name) { return { name }; }\n"
    dependency = b"export const storage = {};\n"
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "task.ts").write_bytes(source)
    (tmp_path / "src" / "storage.ts").write_bytes(dependency)
    source_sha = hashlib.sha256(source).hexdigest()
    delivery = {
        "schema_version": "phase-delivery/v1",
        "project_id": "proj-1",
        "phase_id": "phase-1",
        "effective_technical_requirements": ["TypeScript"],
        "tasks": {
            "task-1": {
                "status": "completed",
                "name": "Task editor",
                "objective": "Create tasks",
                "functional_details": ["Show a new task immediately"],
                "implementation": "Use a typed function",
                "dependencies": [],
                "acceptance_criteria": ["Adding a task returns the task"],
                "delivery": {"files": [{"path": "src/task.ts"}]},
            }
        },
    }
    responsibility = {
        "schema_version": "file-responsibility/v1",
        "files": {},
    }
    scope = {
        "available": True,
        "issues": [],
        "incomplete_task_ids": [],
        "responsibility_ledger_revision": 3,
        "files": [{
            "path": "src/task.ts",
            "task_id": "task-1",
            "agent_id": "agent-1",
            "agent_role": "Frontend Developer",
            "revision": 2,
            "sha256": source_sha,
        }],
    }
    return delivery, responsibility, scope


def test_packet_binds_delivery_tasks_files_owners_and_pre_qa(monkeypatch, tmp_path):
    delivery, responsibility, scope = _sources(tmp_path)
    monkeypatch.setattr(qc, "load_project_files", lambda _project_id: {
        "docs/metis/file-responsibility.json": _record(responsibility),
        "docs/metis/phase-deliveries/phase-1.json": _record(delivery),
    })
    monkeypatch.setattr(qc, "load_phase_qa_scope", lambda **_kwargs: scope)

    packet = qc.build_qc_review_packet(
        project_id="proj-1",
        phase_id="phase-1",
        workspace=tmp_path,
        phase={
            "name": "Build tasks",
            "execution_generation": "generation-2",
            "pre_qa_result": {"passed": True, "evidence": [{"gate_id": "build"}]},
        },
        output_files=["src/task.ts"],
        dependency_files=["src/storage.ts"],
        qa_context={"artifact_digest": "sha256:artifact", "qa_round_id": "qa-1"},
    )

    assert packet["schema_version"] == "qc-review-packet/v1"
    assert packet["tasks"][0]["objective"] == "Create tasks"
    assert packet["tasks"][0]["owned_files"] == ["src/task.ts"]
    assert packet["files"][0]["agent_id"] == "agent-1"
    assert packet["files"][0]["content_complete"] is True
    assert packet["dependency_files"][0]["path"] == "src/storage.ts"
    assert packet["pre_qa"]["passed"] is True


def test_packet_rejects_projection_that_differs_from_delivery_scope(monkeypatch, tmp_path):
    delivery, responsibility, scope = _sources(tmp_path)
    monkeypatch.setattr(qc, "load_project_files", lambda _project_id: {
        "docs/metis/file-responsibility.json": _record(responsibility),
        "docs/metis/phase-deliveries/phase-1.json": _record(delivery),
    })
    monkeypatch.setattr(qc, "load_phase_qa_scope", lambda **_kwargs: scope)

    with pytest.raises(qc.QCContractError, match="file set"):
        qc.build_qc_review_packet(
            project_id="proj-1",
            phase_id="phase-1",
            workspace=tmp_path,
            phase={"pre_qa_result": {"passed": True}},
            output_files=["src/other.ts"],
            qa_context={"artifact_digest": "sha256:artifact"},
        )


def test_packet_ignores_readme_in_output_projection(monkeypatch, tmp_path):
    delivery, responsibility, scope = _sources(tmp_path)
    (tmp_path / "README.md").write_text("# Usage", encoding="utf-8")
    delivery["tasks"]["readme-task"] = {
        "status": "completed",
        "name": "Documentation",
        "objective": "Describe usage",
        "acceptance_criteria": ["README explains usage"],
        "delivery": {"files": [{"path": "README.md"}]},
    }
    monkeypatch.setattr(qc, "load_project_files", lambda _project_id: {
        "docs/metis/file-responsibility.json": _record(responsibility),
        "docs/metis/phase-deliveries/phase-1.json": _record(delivery),
    })
    monkeypatch.setattr(qc, "load_phase_qa_scope", lambda **_kwargs: scope)

    packet = qc.build_qc_review_packet(
        project_id="proj-1",
        phase_id="phase-1",
        workspace=tmp_path,
        phase={"pre_qa_result": {"passed": True}},
        output_files=["src/task.ts", "README.md"],
        qa_context={"artifact_digest": "sha256:artifact"},
    )

    assert [task["task_id"] for task in packet["tasks"]] == ["task-1"]
    assert [item["path"] for item in packet["files"]] == ["src/task.ts"]


def test_result_must_cover_each_criterion_and_reference_packet_files():
    packet = {
        "tasks": [{
            "acceptance_criteria": [{
                "criterion_id": "task-1:acceptance:0",
                "criterion": "Works",
            }],
        }],
        "files": [{"path": "src/task.ts"}],
        "dependency_files": [],
    }
    qc.validate_qc_review_result(
        packet,
        acceptance_observations=[],
        issues=[],
    )
    qc.validate_qc_review_result(
        packet,
        acceptance_observations=[{
            "criterion_id": "task-1:acceptance:0",
            "passed": True,
            "observation": "addTask returns the task object",
            "observed_files": ["src/task.ts"],
        }],
        issues=[],
    )

    with pytest.raises(qc.QCContractError, match="packet file"):
        qc.validate_qc_review_result(
            packet,
            acceptance_observations=[{
                "criterion_id": "task-1:acceptance:0",
                "passed": False,
                "observation": "Missing behavior",
                "observed_files": [],
            }],
            issues=[{
                "severity": "error",
                "file": "src/unknown.ts",
                "message": "Missing behavior",
            }],
        )


def test_supervisor_builds_packet_before_invoking_qc_agent(monkeypatch, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "task.ts").write_text("export const ok = true;\n", "utf-8")
    criterion = "The task implementation is present"
    phase = {
        "phase_id": "phase-1",
        "name": "Tasks",
        "description": "Implement tasks",
        "execution_generation": "generation-1",
        "phase_plan": {"schema_version": "phase-plan/v1"},
        "mechanically_passed_criterion_ids": [],
    }
    agent = {
        "id": "agent-1",
        "phase_id": "phase-1",
        "role": "Frontend Developer",
        "locked_tasks": [{
            "task_id": "task-1",
            "acceptance_criteria": ["src/task.ts exists", criterion],
        }],
        "task_execution_receipts": {"task-1": {
            "status": "succeeded",
            "completion_run_id": "run-1",
            "execution_generation": "generation-1",
            # Pathless phase tasks discover files during execution.
            "required_files": [],
        }},
    }
    ctx = SimpleNamespace(
        project_id="proj-1",
        workspace=tmp_path,
        subprojects=[],
        agents={"agent-1": agent},
        qc_results={},
    )

    class _PM:
        phases = [phase, {"phase_id": "phase-2"}]
        file_registry = {"src/task.ts": {
            "file_path": "src/task.ts",
            "phase_id": "phase-1",
            "agent_id": "agent-1",
        }}

        @staticmethod
        def get_phase(phase_id):
            return phase if phase_id == "phase-1" else None

        @staticmethod
        def get_files_by_phase(phase_id):
            return ([{"file_path": "src/task.ts"}]
                    if phase_id == "phase-1" else [])

    packet = {
        "schema_version": "qc-review-packet/v1",
        "project": {"artifact_digest": "sha256:artifact"},
        "responsibility": {"ledger_revision": 4},
        "tasks": [{"task_id": "task-1", "acceptance_criteria": [{
            "criterion_id": "task-1:acceptance:2",
            "criterion": criterion,
            "source_class": "semantic",
        }]}],
        "files": [{"path": "src/task.ts"}],
        "dependency_files": [],
    }
    captured = {}

    class _QA:
        def __init__(self, **_kwargs):
            pass

        def inspect(self, **kwargs):
            captured.update(kwargs)
            contract = kwargs["acceptance_contracts"][0]
            return {
                "passed": True,
                "score": 100,
                "issues": [],
                "user_report": "passed",
                "developer_report": "passed",
                "layer_results": [],
                "needs_rewrite": False,
                "error_count": 0,
                "warning_count": 0,
                "acceptance_observations": [{
                    **contract,
                    "passed": True,
                    "observation": "src/task.ts contains the implementation",
                    "observed_files": ["src/task.ts"],
                }],
            }

    monkeypatch.setattr(routes_supervisor, "_get_phase_manager", lambda _id: _PM())
    monkeypatch.setattr(routes_supervisor, "build_qc_review_packet", lambda **_kwargs: packet)
    monkeypatch.setattr(quality_agents, "QAAgent", _QA)

    entry = routes_supervisor._run_qc_for_subproject(
        ctx,
        "phase-1",
        "Tasks",
        qa_context={
            "run_id": "supervisor-1",
            "qa_round_id": "qa-1",
            "artifact_digest": "sha256:artifact",
        },
    )

    assert captured["review_packet"] is packet
    assert entry["schema_version"] == "qc-review-result/v1"
    assert entry["artifact_digest"] == "sha256:artifact"
    assert entry["review_packet"]["responsibility_ledger_revision"] == 4


def test_final_qa_uses_authoritative_scope_instead_of_file_registry(
    monkeypatch, tmp_path
):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "current.ts").write_text(
        "export const current = true;\n", encoding="utf-8"
    )
    (tmp_path / "src" / "stale.ts").write_text(
        "export const stale = true;\n", encoding="utf-8"
    )
    ctx = SimpleNamespace(
        project_id="proj-1",
        workspace=tmp_path,
        subprojects=[],
        agents={},
        qc_results={},
    )

    class _PM:
        phases = []
        file_registry = {
            "src/stale.ts": {"file_path": "src/stale.ts", "agent_id": "old"}
        }

    captured = {}

    class _QA:
        def __init__(self, **_kwargs):
            pass

        def inspect(self, **kwargs):
            captured.update(kwargs)
            return {
                "passed": True,
                "score": 100,
                "issues": [],
                "user_report": "passed",
                "developer_report": "passed",
                "layer_results": [],
                "needs_rewrite": False,
                "error_count": 0,
                "warning_count": 0,
                "acceptance_observations": [],
            }

    final_scope = {
        "files": [{"path": "src/current.ts"}],
        "criteria": [{
            "criterion": "phase-1:task-1:1",
            "text": "Current implementation is present",
            "files": ["src/current.ts"],
        }],
    }
    monkeypatch.setattr(routes_supervisor, "_get_phase_manager", lambda _id: _PM())
    monkeypatch.setattr(quality_agents, "QAAgent", _QA)

    routes_supervisor._run_qc_for_subproject(
        ctx,
        "__whole_project__",
        "Whole project",
        is_final_phase=True,
        qa_context={
            "run_id": "final-1",
            "qa_round_id": "round-1",
            "scope_digest": "sha256:artifact",
            "final_qa_scope": final_scope,
        },
    )

    assert captured["output_files"] == ["src/current.ts"]
    assert captured["acceptance_contracts"] == [{
        "criterion_id": "phase-1:task-1:1",
        "criterion": "Current implementation is present",
        "artifact_paths": ["src/current.ts"],
    }]
