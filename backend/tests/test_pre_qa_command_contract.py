import json
from types import SimpleNamespace

import pytest

from core.pre_qa_verifier import (
    CommandGate,
    LocalCommandRunner,
    PreQAVerifier,
    structured_command_gates,
)


def _verification_inputs(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {}}),
        encoding="utf-8",
    )
    return {
        "contract": {
            "required_files": [{
                "path": "package.json",
                "required": True,
                "owner_type": "backend",
                "agent_id": "backend",
            }]
        },
        "file_registry": {"package.json": {"agent_id": "backend"}},
        "agents": [{
            "id": "backend",
            "expert_type": "backend",
            "allowed_path_prefixes": ["package.json"],
        }],
        "commit_digest": "artifact:test",
        "jwt_required": False,
    }


def test_required_command_missing_script_fails_without_execution(tmp_path):
    inputs = _verification_inputs(tmp_path)

    def unexpected_runner(_gate, _workspace):
        raise AssertionError("missing required script must not execute")

    result = PreQAVerifier(
        tmp_path,
        command_runner=unexpected_runner,
    ).verify(
        **inputs,
        command_gates=(
            CommandGate(
                "test-root",
                "test",
                ("npm", "test"),
                required=True,
            ),
        ),
    )

    assert result.passed is False
    assert result.issues[0].code == "command_precondition_missing"
    assert result.evidence[0].passed is False
    assert result.evidence[0].applicable is True
    assert result.evidence[0].executed is False


def test_optional_command_missing_workspace_is_not_applicable_evidence(tmp_path):
    inputs = _verification_inputs(tmp_path)

    def unexpected_runner(_gate, _workspace):
        raise AssertionError("optional missing workspace must not execute")

    result = PreQAVerifier(
        tmp_path,
        command_runner=unexpected_runner,
    ).verify(
        **inputs,
        command_gates=(
            CommandGate(
                "build-frontend",
                "build",
                ("npm", "run", "build"),
                "frontend",
                required=False,
            ),
        ),
    )

    assert result.passed is True
    assert result.evidence[0].passed is None
    assert result.evidence[0].applicable is False
    assert result.evidence[0].executed is False


def test_node_test_rejects_importing_entrypoint_that_listens_on_import(tmp_path):
    inputs = _verification_inputs(tmp_path)
    (tmp_path / "package.json").write_text(
        json.dumps({
            "scripts": {"test": "node --test tests/todos.test.js"},
        }),
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "server.js").write_text(
        "const app = require('express')();\n"
        "app.listen(3000);\n"
        "module.exports = app;\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "todos.test.js").write_text(
        "const app = require('../src/server');\n",
        encoding="utf-8",
    )

    def unexpected_runner(_gate, _workspace):
        raise AssertionError("unsafe test import must fail before execution")

    result = PreQAVerifier(
        tmp_path,
        command_runner=unexpected_runner,
    ).verify(
        **inputs,
        command_gates=(
            CommandGate("test-root", "test", ("npm", "test")),
        ),
    )

    assert result.passed is False
    assert result.failure_category == "pre_qa_failed"
    assert result.issues[0].code == "test_import_starts_server"
    assert result.issues[0].path == "tests/todos.test.js"
    assert result.evidence[0].executed is False


@pytest.mark.parametrize(
    ("manager_file", "criterion", "expected"),
    [
        ("pnpm-lock.yaml", "运行 pnpm run e2e 并通过", ("pnpm", "run", "e2e")),
        ("yarn.lock", "运行 yarn playwright test 并通过", ("yarn", "playwright", "test")),
        ("package-lock.json", "运行 npm run cypress 并通过", ("npm", "run", "cypress")),
        ("package-lock.json", "运行 npx playwright test 并通过", ("npx", "playwright", "test")),
    ],
)
def test_structured_command_recognition_preserves_exact_argv(
    tmp_path,
    manager_file,
    criterion,
    expected,
):
    (tmp_path / "package.json").write_text(
        json.dumps({
            "scripts": {
                "e2e": "playwright test",
                "cypress": "cypress run",
            }
        }),
        encoding="utf-8",
    )
    (tmp_path / manager_file).write_text("", encoding="utf-8")

    gates = structured_command_gates(tmp_path, [criterion])

    assert len(gates) == 1
    assert gates[0].command == expected
    assert gates[0].kind == "test"
    assert gates[0].timeout_seconds == 180
    assert gates[0].required is True


@pytest.mark.parametrize(
    ("criterion", "expected"),
    [
        ("运行 pnpm run e2e 并通过", ("pnpm", "run", "e2e")),
        (
            "运行 yarn playwright test 并通过",
            ("yarn", "playwright", "test"),
        ),
    ],
)
def test_non_npm_manager_executes_exact_argv_without_npm_only_flags(
    monkeypatch,
    tmp_path,
    criterion,
    expected,
):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"e2e": "playwright test"}}),
        encoding="utf-8",
    )
    captured = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="passed", stderr="")

    monkeypatch.setattr(
        "core.pre_qa_verifier._run_bounded_command",
        fake_run,
    )
    gate = structured_command_gates(tmp_path, [criterion])[0]

    result = LocalCommandRunner()(gate, tmp_path)

    assert result.exit_code == 0
    assert captured["command"] == expected
    assert "--cache" not in captured["command"]
    assert "--userconfig" not in captured["command"]
