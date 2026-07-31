import hashlib
from types import SimpleNamespace

import pytest

from agents.execution_agent import ExecutionAgent
from api import routes_phases
from core.rebuild_policy import (
    RebuildPolicyError,
    assert_preserved_files_unchanged,
    classify_rebuild_files,
)


class _Hermes:
    base_url = "http://unused"
    api_key = "test"
    model = "test"
    max_tokens = 100
    temperature = 0


class _ReplyHermes(_Hermes):
    def __init__(self, content):
        self.content = content
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        return {"content": self.content}


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def test_classifies_existing_clean_issue_target_and_missing_contract_file(tmp_path):
    clean = tmp_path / "backend" / "src" / "auth.js"
    target = tmp_path / "backend" / "src" / "tickets.js"
    clean.parent.mkdir(parents=True)
    clean.write_text("secure auth\n", encoding="utf-8")
    target.write_text("old tickets\n", encoding="utf-8")
    contract_owners = {
        "backend/src/auth.js": "backend",
        "backend/src/tickets.js": "backend",
        "Dockerfile": "devops",
    }

    entries = classify_rebuild_files(
        tmp_path,
        ["backend/src/auth.js", "backend/src/tickets.js", "Dockerfile"],
        [{
            "id": "issue-priority",
            "file_path": "backend/src/tickets.js",
            "fix_hint": "Reject invalid priority with HTTP 400",
        }],
        owner_for_path=contract_owners.__getitem__,
    )
    by_path = {entry["path"]: entry for entry in entries}

    assert by_path["backend/src/auth.js"] == {
        "path": "backend/src/auth.js",
        "mode": "preserve",
        "baseline_digest": _digest(clean.read_bytes()),
        "issue_ids": [],
        "owner_type": "backend",
        "required_invariants": [],
    }
    assert by_path["backend/src/tickets.js"]["mode"] == "patch"
    assert by_path["backend/src/tickets.js"]["issue_ids"] == ["issue-priority"]
    assert by_path["backend/src/tickets.js"]["required_invariants"] == [
        "Reject invalid priority with HTTP 400"
    ]
    assert by_path["Dockerfile"]["mode"] == "create"
    assert by_path["Dockerfile"]["baseline_digest"] is None
    assert by_path["Dockerfile"]["owner_type"] == "devops"


def test_create_policy_accepts_optional_template_evidence(tmp_path):
    entry = classify_rebuild_files(
        tmp_path,
        ["Dockerfile"],
        [],
        template_metadata={
            "Dockerfile": {"template": "node-react-express", "template_version": 1}
        },
    )[0]
    assert entry["template"] == "node-react-express"
    assert entry["template_version"] == 1


def test_node_contract_materializes_only_missing_create_mode_templates(tmp_path):
    existing = tmp_path / "package.json"
    existing.write_text('{"name":"custom-root"}\n', encoding="utf-8")
    patched = tmp_path / "Dockerfile"
    patched.write_text("FROM node:20-alpine\n# custom\n", encoding="utf-8")

    entries = classify_rebuild_files(
        tmp_path,
        [
            "package.json",
            "backend/package.json",
            "frontend/package.json",
            "Dockerfile",
            ".env.example",
        ],
        [{"id": "docker-issue", "file_path": "Dockerfile"}],
        technology_stack=["Node.js", "React", "Express"],
        project_name="industrial-app",
    )
    by_path = {entry["path"]: entry for entry in entries}

    assert existing.read_text(encoding="utf-8") == '{"name":"custom-root"}\n'
    assert patched.read_text(encoding="utf-8") == "FROM node:20-alpine\n# custom\n"
    assert by_path["package.json"]["mode"] == "preserve"
    assert by_path["Dockerfile"]["mode"] == "patch"
    assert by_path["backend/package.json"]["mode"] == "create"
    assert not (tmp_path / "backend" / "package.json").exists()
    assert by_path[".env.example"]["mode"] == "create"
    assert (tmp_path / ".env.example").is_file()
    assert by_path[".env.example"]["template"] == "node-react-express"
    assert by_path[".env.example"]["template_version"] == 4
    assert by_path[".env.example"]["template_digest"] == _digest(
        (tmp_path / ".env.example").read_bytes()
    )


def test_legacy_node_contract_paths_materialize_templates_without_stack(tmp_path):
    entries = classify_rebuild_files(
        tmp_path,
        ["package.json", "backend/package.json", "Dockerfile", ".dockerignore"],
        [],
    )
    by_path = {entry["path"]: entry for entry in entries}

    assert (tmp_path / "package.json").is_file()
    assert (tmp_path / "Dockerfile").is_file()
    assert (tmp_path / ".dockerignore").is_file()
    assert by_path["Dockerfile"]["mode"] == "create"
    assert by_path["Dockerfile"]["template_digest"] == _digest(
        (tmp_path / "Dockerfile").read_bytes()
    )


def test_python_contract_never_materializes_node_templates(tmp_path):
    entries = classify_rebuild_files(
        tmp_path,
        ["requirements.txt", "backend/main.py", "Dockerfile"],
        [],
        technology_stack=["Python", "FastAPI"],
    )

    assert all(entry["mode"] == "create" for entry in entries)
    assert not (tmp_path / "Dockerfile").exists()
    assert not any("template" in entry for entry in entries)


def test_materialized_create_target_allows_one_controlled_write(tmp_path):
    entries = classify_rebuild_files(
        tmp_path,
        ["package.json", "frontend/package.json", "Dockerfile"],
        [],
        technology_stack=["React", "Express"],
    )
    entry = next(item for item in entries if item["path"] == "Dockerfile")
    agent = ExecutionAgent(
        "a",
        "devops",
        tmp_path,
        _Hermes(),
        allowed_path_prefixes=["Dockerfile"],
        rebuild_file_specs=[entry],
    )

    agent._write_file("Dockerfile", "FROM node:20-alpine\n")
    assert (tmp_path / "Dockerfile").read_text(encoding="utf-8") == "FROM node:20-alpine\n"
    with pytest.raises(RebuildPolicyError, match="Create target already exists"):
        agent._write_file("Dockerfile", "FROM node:22-alpine\n")


def test_preserve_file_cannot_be_overwritten_and_digest_change_fails(tmp_path):
    target = tmp_path / "auth.js"
    target.write_text("secure\n", encoding="utf-8")
    entry = classify_rebuild_files(tmp_path, ["auth.js"], [])[0]
    agent = ExecutionAgent(
        "a", "backend", tmp_path, _Hermes(),
        allowed_path_prefixes=["auth.js"], rebuild_file_specs=[entry],
    )

    with pytest.raises(RebuildPolicyError, match="Preserve file cannot be overwritten"):
        agent._write_file("auth.js", "regressed\n")
    assert target.read_text(encoding="utf-8") == "secure\n"

    target.write_text("external mutation\n", encoding="utf-8")
    with pytest.raises(RebuildPolicyError, match="Preserve file digest changed"):
        assert_preserved_files_unchanged(tmp_path, [entry])


def test_patch_requires_current_and_declared_baseline_digest(tmp_path):
    target = tmp_path / "tickets.js"
    target.write_text("old\n", encoding="utf-8")
    entry = classify_rebuild_files(
        tmp_path, ["tickets.js"], [{"id": "issue-1", "file_path": "tickets.js"}],
    )[0]
    agent = ExecutionAgent(
        "a", "backend", tmp_path, _Hermes(),
        allowed_path_prefixes=["tickets.js"], rebuild_file_specs=[entry],
    )

    with pytest.raises(RebuildPolicyError, match="response baseline_digest mismatch"):
        agent._write_file("tickets.js", "fixed\n", declared_baseline_digest="sha256:stale")
    assert target.read_text(encoding="utf-8") == "old\n"

    target.write_text("changed after planning\n", encoding="utf-8")
    with pytest.raises(RebuildPolicyError, match="Patch baseline digest mismatch"):
        agent._write_file(
            "tickets.js", "fixed\n",
            declared_baseline_digest=entry["baseline_digest"],
        )


def test_patch_delivery_injects_canonical_metadata_when_model_omits_it(tmp_path):
    target = tmp_path / "tickets.js"
    target.write_text('module.exports = "old";\n', encoding="utf-8")
    entry = classify_rebuild_files(
        tmp_path, ["tickets.js"], [{"id": "issue-1", "file_path": "tickets.js"}],
    )[0]
    hermes = _ReplyHermes(
        '{"files":[{"path":"tickets.js",'
        '"content":"module.exports = \\"fixed\\";\\n"}]}',
    )
    agent = ExecutionAgent(
        "a", "backend", tmp_path, hermes,
        allowed_path_prefixes=["tickets.js"],
        required_output_files=["tickets.js"],
        rebuild_file_specs=[entry],
    )

    result = agent.execute_task(
        "sp-patch", "Patch tickets", "Update tickets.js",
    )

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == 'module.exports = "fixed";\n'
    assert len(hermes.calls) == 1


def test_patch_delivery_rejects_wrong_nonempty_digest_without_writing(tmp_path):
    original = 'module.exports = "old";\n'
    target = tmp_path / "tickets.js"
    target.write_text(original, encoding="utf-8")
    entry = classify_rebuild_files(
        tmp_path, ["tickets.js"], [{"id": "issue-1", "file_path": "tickets.js"}],
    )[0]
    hermes = _ReplyHermes(
        '{"files":[{"path":"tickets.js",'
        '"content":"module.exports = \\"fixed\\";\\n",'
        '"baseline_digest":"sha256:wrong"}]}',
    )
    agent = ExecutionAgent(
        "a", "backend", tmp_path, hermes,
        allowed_path_prefixes=["tickets.js"],
        required_output_files=["tickets.js"],
        rebuild_file_specs=[entry],
    )

    result = agent.execute_task(
        "sp-patch", "Patch tickets", "Update tickets.js",
    )

    assert result["success"] is False
    assert result["status"] == "failed"
    assert "response baseline_digest mismatch" in result["error"]
    assert target.read_text(encoding="utf-8") == original


def test_patch_delivery_rejects_drift_when_model_omits_digest(tmp_path):
    target = tmp_path / "tickets.js"
    target.write_text('module.exports = "old";\n', encoding="utf-8")
    entry = classify_rebuild_files(
        tmp_path, ["tickets.js"], [{"id": "issue-1", "file_path": "tickets.js"}],
    )[0]
    drifted = 'module.exports = "changed after planning";\n'
    target.write_text(drifted, encoding="utf-8")
    hermes = _ReplyHermes(
        '{"files":[{"path":"tickets.js",'
        '"content":"module.exports = \\"fixed\\";\\n"}]}',
    )
    agent = ExecutionAgent(
        "a", "backend", tmp_path, hermes,
        allowed_path_prefixes=["tickets.js"],
        required_output_files=["tickets.js"],
        rebuild_file_specs=[entry],
    )

    result = agent.execute_task(
        "sp-patch", "Patch tickets", "Update tickets.js",
    )

    assert result["success"] is False
    assert "Patch baseline digest mismatch" in result["error"]
    assert target.read_text(encoding="utf-8") == drifted
    assert len(hermes.calls) == 1


def test_rebuild_agent_rejects_undeclared_output_without_mutating_it(tmp_path):
    target = tmp_path / "tickets.js"
    unauthorized = tmp_path / "auth.js"
    target.write_text("old\n", encoding="utf-8")
    unauthorized.write_text("secure\n", encoding="utf-8")
    entry = classify_rebuild_files(
        tmp_path, ["tickets.js"], [{"id": "issue-1", "file_path": "tickets.js"}],
    )[0]
    agent = ExecutionAgent(
        "a", "backend", tmp_path, _Hermes(),
        allowed_path_prefixes=["tickets.js", "auth.js"], rebuild_file_specs=[entry],
    )

    with pytest.raises(RebuildPolicyError, match="unauthorized path"):
        agent._write_file("auth.js", "regressed\n")
    assert unauthorized.read_text(encoding="utf-8") == "secure\n"


def test_python_only_contract_does_not_invent_node_manifests():
    pm = SimpleNamespace(project_contract={
        "source_requirements": "Python FastAPI service with requirements.txt",
        "required_tech": ["Python", "FastAPI"],
    })
    files = routes_phases._contract_required_rebuild_files(
        pm, {"deliverables": ["requirements.txt", "backend/main.py"]},
    )
    assert "requirements.txt" in files
    assert "frontend/package.json" not in files
    assert "backend/package.json" not in files


def test_node_fullstack_rebuild_uses_explicit_contract_inventory():
    required = {
        "package.json", "backend/package.json", "frontend/package.json",
        "Dockerfile", "README.md", ".env.example",
    }
    pm = SimpleNamespace(project_contract={
        "source_requirements": (
            "Node TypeScript React frontend and Express backend. Deliver package.json, "
            "Dockerfile, README.md and .env.example."
        ),
        "required_tech": ["Node", "React", "Express"],
        "required_files": [
            {"path": path, "phase_id": "phase-1", "required": True}
            for path in sorted(required)
        ],
    })
    files = set(routes_phases._contract_required_rebuild_files(
        pm, {"phase_id": "phase-1", "deliverables": []},
    ))
    assert files == required
