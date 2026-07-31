import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from agents.execution_agent import ExecutionAgent
from core import app_state
from core.database import delete_project_files, init_db, load_project_files
from core.delivery_documents import (
    DeliveryContentionError,
    load_final_qa_scope,
    load_phase_qa_scope,
    record_successful_task_delivery,
    validate_delivery_write_intents,
)


class _SequencedModel:
    def __init__(self, contents):
        self.contents = list(contents)

    def chat(self, _messages, **_kwargs):
        return {"content": self.contents.pop(0)}


@pytest.fixture(autouse=True)
def _clean_project_files():
    init_db()
    delete_project_files("proj-1")
    yield
    delete_project_files("proj-1")


def _phase_plan():
    return {
        "schema_version": "phase-plan/v1",
        "phase_id": "phase-1",
        "summary": "实现基础功能",
        "effective_technical_requirements": [
            {"requirement": "Python", "source": "phase_pm"},
        ],
        "tasks": [
            {
                "task_id": "phase-1-task-1",
                "name": "创建应用",
                "objective": "创建可运行应用",
                "functional_details": ["提供首页"],
                "implementation": "使用 Python 实现",
                "dependencies": [],
                "acceptance_criteria": ["首页文件存在"],
            },
            {
                "task_id": "phase-1-task-2",
                "name": "扩展应用",
                "objective": "增加状态显示",
                "functional_details": ["显示运行状态"],
                "implementation": "扩展现有入口",
                "dependencies": ["phase-1-task-1"],
                "acceptance_criteria": ["页面显示运行状态"],
            },
        ],
        "assignments": [
            {
                "expert_id": "expert-1",
                "task_ids": ["phase-1-task-1"],
                "responsibility": "创建应用入口",
            },
            {
                "expert_id": "expert-2",
                "task_ids": ["phase-1-task-2"],
                "responsibility": "扩展应用入口",
            },
        ],
        "expert_pool_revision": 3,
    }


def _evidence(path, payload):
    return {
        "kind": "delivery_files",
        "files": [
            {
                "path": path,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        ],
    }


def test_delivery_write_allows_canonical_task_dependency_to_modify_owner_file(
    tmp_path,
):
    target = tmp_path / "tests" / "test_business_logic.py"
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")

    validate_delivery_write_intents(
        workspace=tmp_path,
        intents=[{
            "path": "tests/test_business_logic.py",
            "content": "new",
        }],
        file_registry={
            "tests/test_business_logic.py": {
                "subproject_id": "sp-phase-1-002",
                "task_id": "phase-1-task-2",
            },
        },
        task_id="phase-1-task-3",
        dependencies=["phase-1-task-2"],
        fully_exposed_paths=["tests/test_business_logic.py"],
    )


def test_dependent_task_updates_ledger_when_attempt_baseline_omits_existing_file(
    tmp_path,
):
    target = tmp_path / "tests" / "test_counter.py"
    target.parent.mkdir(parents=True)
    first = b"def test_base():\n    assert True\n"
    target.write_bytes(first)
    record_successful_task_delivery(
        workspace=tmp_path,
        project_id="proj-1",
        phase_id="phase-1",
        phase_plan=_phase_plan(),
        task_id="phase-1-task-1",
        agent_id="agent-1",
        expert_id="expert-1",
        agent_role="developer",
        summary="base",
        delivery_evidence=_evidence("tests/test_counter.py", first),
        baseline_files={},
    )

    second = b"def test_base():\n    assert True\n\ndef test_edge():\n    assert True\n"
    target.write_bytes(second)
    record_successful_task_delivery(
        workspace=tmp_path,
        project_id="proj-1",
        phase_id="phase-1",
        phase_plan=_phase_plan(),
        task_id="phase-1-task-2",
        agent_id="agent-2",
        expert_id="expert-2",
        agent_role="qa",
        summary="edge",
        delivery_evidence=_evidence("tests/test_counter.py", second),
        baseline_files={},
    )

    scope = load_phase_qa_scope(
        project_id="proj-1", phase_id="phase-1", workspace=tmp_path,
    )
    assert scope["issues"] == []
    assert len(scope["files"]) == 1
    assert scope["files"][0]["task_id"] == "phase-1-task-2"
    assert scope["files"][0]["revision"] == 2


def test_phase_qa_scope_uses_current_v1_owner_and_hash(tmp_path):
    first = b"first\n"
    target = tmp_path / "app.py"
    target.write_bytes(first)
    record_successful_task_delivery(
        workspace=tmp_path,
        project_id="proj-1",
        phase_id="phase-1",
        phase_plan=_phase_plan(),
        task_id="phase-1-task-1",
        agent_id="agent-1",
        expert_id="expert-1",
        agent_role="backend",
        summary="create",
        delivery_evidence=_evidence("app.py", first),
        baseline_files={},
    )
    second = b"second\n"
    target.write_bytes(second)
    record_successful_task_delivery(
        workspace=tmp_path,
        project_id="proj-1",
        phase_id="phase-1",
        phase_plan=_phase_plan(),
        task_id="phase-1-task-2",
        agent_id="agent-2",
        expert_id="expert-2",
        agent_role="backend",
        summary="modify",
        delivery_evidence=_evidence("app.py", second),
        baseline_files={"app.py": first},
    )

    scope = load_phase_qa_scope(project_id="proj-1", phase_id="phase-1")

    assert scope["available"] is True
    assert scope["issues"] == []
    assert scope["incomplete_task_ids"] == []
    assert scope["files"] == [{
        "path": "app.py",
        "phase_id": "phase-1",
        "task_id": "phase-1-task-2",
        "agent_id": "agent-2",
        "expert_id": "expert-2",
        "agent_role": "backend",
        "revision": 2,
        "sha256": hashlib.sha256(second).hexdigest(),
        "size_bytes": len(second),
    }]


def test_phase_qa_scope_rejects_workspace_bytes_outside_committed_hash(tmp_path):
    payload = b"print('ok')\n"
    target = tmp_path / "app.py"
    target.write_bytes(payload)
    plan = _phase_plan()
    plan["tasks"] = plan["tasks"][:1]
    plan["assignments"] = plan["assignments"][:1]
    record_successful_task_delivery(
        workspace=tmp_path,
        project_id="proj-1",
        phase_id="phase-1",
        phase_plan=plan,
        task_id="phase-1-task-1",
        agent_id="agent-1",
        expert_id="expert-1",
        agent_role="backend",
        summary="create",
        delivery_evidence=_evidence("app.py", payload),
        baseline_files={},
    )
    target.write_bytes(payload + b"# TODO: injected\n")

    scope = load_phase_qa_scope(
        project_id="proj-1",
        phase_id="phase-1",
        workspace=tmp_path,
    )

    assert scope["files"] == []
    assert scope["issues"] == [{
        "code": "workspace_file_hash_mismatch",
        "path": "app.py",
        "message": (
            "Workspace file bytes do not match the committed responsibility record"
        ),
    }]


def test_partial_repair_keeps_previously_verified_task_files(tmp_path):
    plan = _phase_plan()
    plan["tasks"] = plan["tasks"][:1]
    plan["assignments"] = plan["assignments"][:1]
    first_a, first_b = b"a-v1\n", b"b-v1\n"
    (tmp_path / "a.py").write_bytes(first_a)
    (tmp_path / "b.py").write_bytes(first_b)
    evidence = {
        "kind": "delivery_files",
        "files": [
            {
                "path": path,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
            for path, payload in (("a.py", first_a), ("b.py", first_b))
        ],
    }
    record_successful_task_delivery(
        workspace=tmp_path, project_id="proj-1", phase_id="phase-1",
        phase_plan=plan, task_id="phase-1-task-1", agent_id="agent-1",
        expert_id="expert-1", agent_role="backend", summary="initial",
        delivery_evidence=evidence, baseline_files={},
    )
    repaired_a = b"a-v2\n"
    (tmp_path / "a.py").write_bytes(repaired_a)
    record_successful_task_delivery(
        workspace=tmp_path, project_id="proj-1", phase_id="phase-1",
        phase_plan=plan, task_id="phase-1-task-1", agent_id="agent-1",
        expert_id="expert-1", agent_role="backend", summary="repair a only",
        delivery_evidence=_evidence("a.py", repaired_a),
        baseline_files={"a.py": first_a, "b.py": first_b},
    )

    scope = load_phase_qa_scope(
        project_id="proj-1", phase_id="phase-1", workspace=tmp_path,
    )

    assert scope["issues"] == []
    assert [(item["path"], item["revision"]) for item in scope["files"]] == [
        ("a.py", 2), ("b.py", 1),
    ]


def test_successful_delivery_generates_file_keyed_documents(tmp_path):
    payload = b"print('hello')\n"
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_bytes(payload)

    result = record_successful_task_delivery(
        workspace=tmp_path,
        project_id="proj-1",
        phase_id="phase-1",
        phase_plan=_phase_plan(),
        task_id="phase-1-task-1",
        agent_id="agent-1",
        expert_id="expert-1",
        agent_role="Python developer",
        summary=json.dumps({
            "files": [{"path": "src/app.py", "content": "sensitive source"}],
        }),
        delivery_evidence=_evidence("src/app.py", payload),
        baseline_files={},
    )

    responsibility = json.loads(result["responsibility_path"].read_text("utf-8"))
    stage = json.loads(result["phase_delivery_path"].read_text("utf-8"))
    tracked = responsibility["files"]["src/app.py"]

    assert tracked["current_revision"] == 1
    assert tracked["size_bytes"] == len(payload)
    assert tracked["created_by"]["task_id"] == "phase-1-task-1"
    assert tracked["current_responsible"]["expert_id"] == "expert-1"
    assert tracked["history"][0]["action"] == "create"
    assert "run_id" not in json.dumps(responsibility)

    task = stage["tasks"]["phase-1-task-1"]
    assert task["objective"] == "创建可运行应用"
    assert task["assignment"]["expert_id"] == "expert-1"
    assert task["status"] == "completed"
    assert task["delivery"]["summary"] == "Delivered 1 file."
    assert "sensitive source" not in json.dumps(stage, ensure_ascii=False)
    assert task["delivery"]["files"] == [
        {
            "path": "src/app.py",
            "action": "create",
            "revision": 1,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
    ]
    stored = load_project_files("proj-1")
    assert stored["src/app.py"]["content"] == payload
    assert stored["src/app.py"]["kind"] == "agent_output"
    assert stored["docs/metis/file-responsibility.json"]["kind"] == (
        "file_responsibility_document"
    )
    assert stored["docs/metis/phase-deliveries/phase-1.json"]["kind"] == (
        "phase_delivery_document"
    )
    for relative in (
        "src/app.py",
        "docs/metis/file-responsibility.json",
        "docs/metis/phase-deliveries/phase-1.json",
    ):
        (tmp_path / relative).unlink()
    app_state._restore_workspace(
        SimpleNamespace(workspace=tmp_path),
        {
            path: base64.b64encode(record["content"]).decode("ascii")
            for path, record in stored.items()
        },
    )
    assert (tmp_path / "src/app.py").read_bytes() == payload
    assert result["responsibility_path"].is_file()
    assert result["phase_delivery_path"].is_file()


def test_final_qa_scope_uses_current_database_responsibility(tmp_path):
    first = b"print('v1')\n"
    current = b"print('v2')\n"
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_bytes(first)
    plan = _phase_plan()

    record_successful_task_delivery(
        workspace=tmp_path,
        project_id="proj-1",
        phase_id="phase-1",
        phase_plan=plan,
        task_id="phase-1-task-1",
        agent_id="agent-1",
        expert_id="expert-1",
        agent_role="backend",
        summary="create",
        delivery_evidence=_evidence("src/app.py", first),
        baseline_files={},
    )
    target.write_bytes(current)
    record_successful_task_delivery(
        workspace=tmp_path,
        project_id="proj-1",
        phase_id="phase-1",
        phase_plan=plan,
        task_id="phase-1-task-2",
        agent_id="agent-2",
        expert_id="expert-2",
        agent_role="backend",
        summary="modify",
        delivery_evidence=_evidence("src/app.py", current),
        baseline_files={"src/app.py": first},
    )

    scope = load_final_qa_scope(project_id="proj-1", workspace=tmp_path)

    assert scope["available"] is True
    assert scope["issues"] == []
    assert scope["responsibility_ledger_revision"] == 2
    assert scope["files"] == [{
        "path": "src/app.py",
        "phase_id": "phase-1",
        "task_id": "phase-1-task-2",
        "agent_id": "agent-2",
        "expert_id": "expert-2",
        "agent_role": "backend",
        "revision": 2,
        "sha256": hashlib.sha256(current).hexdigest(),
        "size_bytes": len(current),
    }]
    assert {
        item["criterion"] for item in scope["criteria"]
    } == {
        "phase-1:phase-1-task-1:1",
        "phase-1:phase-1-task-2:1",
    }


def test_modification_preserves_creator_and_records_current_responsible(tmp_path):
    original = b"print('v1')\n"
    modified = b"print('v2')\n"
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_bytes(original)
    plan = _phase_plan()

    record_successful_task_delivery(
        workspace=tmp_path,
        project_id="proj-1",
        phase_id="phase-1",
        phase_plan=plan,
        task_id="phase-1-task-1",
        agent_id="agent-1",
        expert_id="expert-1",
        agent_role="developer",
        summary="初始版本",
        delivery_evidence=_evidence("src/app.py", original),
        baseline_files={},
    )

    target.write_bytes(modified)
    result = record_successful_task_delivery(
        workspace=tmp_path,
        project_id="proj-1",
        phase_id="phase-1",
        phase_plan=plan,
        task_id="phase-1-task-2",
        agent_id="agent-2",
        expert_id="expert-2",
        agent_role="developer",
        summary="扩展版本",
        delivery_evidence=_evidence("src/app.py", modified),
        baseline_files={"src/app.py": original},
    )

    responsibility = json.loads(result["responsibility_path"].read_text("utf-8"))
    tracked = responsibility["files"]["src/app.py"]
    assert tracked["current_revision"] == 2
    assert tracked["created_by"]["expert_id"] == "expert-1"
    assert tracked["current_responsible"]["expert_id"] == "expert-2"
    assert [item["action"] for item in tracked["history"]] == ["create", "modify"]
    assert tracked["history"][1]["before_sha256"] == hashlib.sha256(original).hexdigest()
    assert tracked["history"][1]["after_sha256"] == hashlib.sha256(modified).hexdigest()


def test_unchanged_file_is_reused_without_stealing_responsibility(tmp_path):
    payload = b"unchanged\n"
    target = tmp_path / "shared.txt"
    target.write_bytes(payload)
    plan = _phase_plan()

    first = record_successful_task_delivery(
        workspace=tmp_path,
        project_id="proj-1",
        phase_id="phase-1",
        phase_plan=plan,
        task_id="phase-1-task-1",
        agent_id="agent-1",
        expert_id="expert-1",
        agent_role="developer",
        summary="创建",
        delivery_evidence=_evidence("shared.txt", payload),
        baseline_files={},
    )
    second = record_successful_task_delivery(
        workspace=tmp_path,
        project_id="proj-1",
        phase_id="phase-1",
        phase_plan=plan,
        task_id="phase-1-task-2",
        agent_id="agent-2",
        expert_id="expert-2",
        agent_role="developer",
        summary="复用",
        delivery_evidence=_evidence("shared.txt", payload),
        baseline_files={"shared.txt": payload},
    )

    responsibility = json.loads(second["responsibility_path"].read_text("utf-8"))
    tracked = responsibility["files"]["shared.txt"]
    assert tracked["current_revision"] == 1
    assert tracked["current_responsible"]["expert_id"] == "expert-1"
    assert len(tracked["history"]) == 1
    stage = json.loads(first["phase_delivery_path"].read_text("utf-8"))
    assert stage["tasks"]["phase-1-task-2"]["delivery"]["files"][0]["action"] == "reuse"


def test_existing_file_without_v1_database_responsibility_is_rejected(tmp_path):
    payload = b"legacy-or-untracked\n"
    target = tmp_path / "shared.txt"
    target.write_bytes(payload)

    with pytest.raises(DeliveryContentionError) as raised:
        record_successful_task_delivery(
            workspace=tmp_path,
            project_id="proj-1",
            phase_id="phase-1",
            phase_plan=_phase_plan(),
            task_id="phase-1-task-2",
            agent_id="agent-2",
            expert_id="expert-2",
            agent_role="developer",
            summary="modify",
            delivery_evidence=_evidence("shared.txt", payload),
            baseline_files={"shared.txt": payload},
        )

    assert raised.value.code == "untracked_existing_file"
    assert load_project_files("proj-1") == {}


def test_bootstrap_readme_is_excluded_from_task_delivery_scope(tmp_path):
    before = b"# Project\n\nBootstrap description.\n"
    after = b"# Project\n\nComplete usage documentation.\n"
    target = tmp_path / "README.md"
    target.write_bytes(after)

    result = record_successful_task_delivery(
        workspace=tmp_path,
        project_id="proj-readme",
        phase_id="phase-3",
        phase_plan=_phase_plan(),
        task_id="phase-1-task-2",
        agent_id="agent-2",
        expert_id="expert-2",
        agent_role="developer",
        summary="document",
        delivery_evidence=_evidence("README.md", after),
        baseline_files={"README.md": before},
    )

    responsibility = json.loads(
        result["responsibility_path"].read_text("utf-8")
    )
    assert "README.md" not in responsibility["files"]
    assert result["files"] == []


def test_unrelated_task_cannot_silently_replace_an_owned_file(tmp_path):
    target = tmp_path / "index.html"
    target.write_text("old", encoding="utf-8")

    with pytest.raises(DeliveryContentionError) as raised:
        validate_delivery_write_intents(
            workspace=tmp_path,
            intents=[{"path": "index.html", "content": "new"}],
            file_registry={
                "index.html": {
                    "subproject_id": "phase-1-task-1",
                    "agent_id": "agent-1",
                }
            },
            task_id="phase-1-task-3",
            dependencies=[],
            fully_exposed_paths={"index.html"},
        )

    assert raised.value.code == "unrelated_file_conflict"
    assert "phase-1-task-1" in str(raised.value)
    assert target.read_text("utf-8") == "old"


def test_dependency_must_rebase_on_complete_current_file_before_modify(tmp_path):
    target = tmp_path / "index.html"
    target.write_text("current complete content", encoding="utf-8")
    kwargs = {
        "workspace": tmp_path,
        "intents": [{"path": "index.html", "content": "modified"}],
        "file_registry": {
            "index.html": {
                "subproject_id": "phase-1-task-1",
                "agent_id": "agent-1",
            }
        },
        "task_id": "phase-1-task-2",
        "dependencies": ["phase-1-task-1"],
    }

    with pytest.raises(DeliveryContentionError) as raised:
        validate_delivery_write_intents(
            **kwargs,
            fully_exposed_paths=set(),
        )

    assert raised.value.code == "existing_file_rebase_required"
    assert "current complete content" in raised.value.repair_context

    validate_delivery_write_intents(
        **kwargs,
        fully_exposed_paths={"index.html"},
    )


def test_execution_agent_rejects_unrelated_owned_file_without_writing(
    tmp_path, monkeypatch
):
    target = tmp_path / "index.html"
    target.write_text("owned", encoding="utf-8")
    monkeypatch.setitem(
        app_state._phase_managers,
        "proj-1",
        type("PM", (), {
            "file_registry": {
                "index.html": {
                    "subproject_id": "phase-1-task-1",
                    "agent_id": "agent-1",
                    "phase_id": "phase-1",
                }
            }
        })(),
    )
    response = json.dumps({
        "files": [{"path": "index.html", "content": "replacement"}],
    })
    agent = ExecutionAgent(
        agent_id="agent-2",
        role="developer",
        workspace=tmp_path,
        hermes_client=_SequencedModel([response, response, response]),
        project_id="proj-1",
        phase_id="phase-1",
        artifact_policy={
            "task_id": "phase-1-task-2",
            "task_dependencies": [],
        },
    )

    result = agent.execute_task(
        subproject_id="phase-1-task-2",
        subproject_name="Unrelated task",
        description="Create a separate feature",
    )

    assert result["success"] is False
    assert result["status"] == "model_failed"
    assert target.read_text("utf-8") == "owned"
    assert any(
        "unrelated_file_conflict" in error
        for error in result["model_failure_evidence"]["parser_errors"]
    )


def test_execution_agent_rebases_dependent_large_file_once(
    tmp_path, monkeypatch
):
    original = "// " + ("A" * 2997)
    modified = original + "\nconst updated = true;\n"
    target = tmp_path / "src" / "app.js"
    target.parent.mkdir()
    target.write_text(original, encoding="utf-8")
    monkeypatch.setitem(
        app_state._phase_managers,
        "proj-1",
        type("PM", (), {
            "file_registry": {
                "src/app.js": {
                    "subproject_id": "phase-1-task-1",
                    "agent_id": "agent-1",
                    "phase_id": "phase-1",
                }
            }
        })(),
    )
    first = json.dumps({
        "files": [{"path": "src/app.js", "content": "blind replacement"}],
    })
    rebased = json.dumps({
        "files": [{"path": "src/app.js", "content": modified}],
    })
    model = _SequencedModel([first, rebased])
    agent = ExecutionAgent(
        agent_id="agent-2",
        role="developer",
        workspace=tmp_path,
        hermes_client=model,
        project_id="proj-1",
        phase_id="phase-1",
        artifact_policy={
            "task_id": "phase-1-task-2",
            "task_dependencies": ["phase-1-task-1"],
        },
    )

    result = agent.execute_task(
        subproject_id="phase-1-task-2",
        subproject_name="Dependent task",
        description="Extend the existing application",
    )

    assert result["success"] is True, result
    assert target.read_text("utf-8") == modified
    assert len(model.contents) == 0
    assert any("existing_file_rebase_required" in line for line in result["logs"])
