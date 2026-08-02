import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.execution_agent import ExecutionAgent
from api import routes_execution
from core.project_write_fence import RevokedExecutionGuard


class FakeHermes:
    def __init__(self, content: str = ""):
        self.content = content

    def chat(self, _messages):
        return {"content": self.content}


class SequencedHermes:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        response = self.contents.pop(0)
        return response if isinstance(response, dict) else {"content": response}


class JsonModeHermes:
    def __init__(self, content: str):
        self.content = content
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return {"content": self.content}


def make_agent(tmp_path: Path, content: str = "") -> ExecutionAgent:
    return ExecutionAgent(
        agent_id="agent-test",
        role="前端工程师",
        workspace=tmp_path,
        hermes_client=FakeHermes(content),
        allowed_path_prefixes=["frontend/"],
    )


def test_execution_requests_json_mode_and_declares_completion_field(tmp_path):
    hermes = JsonModeHermes(
        '{"files":[{"path":"frontend/app.js","content":"console.log(1)"}],'
        '"complete":true}'
    )
    agent = ExecutionAgent(
        agent_id="agent-json-mode",
        role="Frontend Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["frontend/app.js"],
        required_output_files=["frontend/app.js"],
    )

    result = agent.execute_task(
        subproject_id="sp-frontend",
        subproject_name="Frontend delivery",
        description="Implement frontend/app.js",
    )

    assert result["success"] is True
    assert hermes.calls[0][1]["response_format"] == {"type": "json_object"}
    system_prompt = hermes.calls[0][0][0].content
    assert '"complete":true' in system_prompt
    assert "process.env.PORT" in system_prompt


def test_node_runtime_listener_requires_dynamic_port_contract(tmp_path):
    agent = ExecutionAgent(
        agent_id="agent-runtime-port",
        role="Backend Developer",
        workspace=tmp_path,
        hermes_client=FakeHermes(),
        allowed_path_prefixes=["app.js", "package.json"],
    )
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"express":"^4.18.2"}}',
        encoding="utf-8",
    )
    app = tmp_path / "app.js"
    app.write_text(
        "const app=require('express')();\napp.listen(3000);\n",
        encoding="utf-8",
    )

    invalid = agent._validate_new_output(
        "Implement a Node.js Express API",
        ["package.json", "app.js"],
        ["Node.js", "Express"],
    )
    assert invalid["valid"] is False
    assert any("process.env.PORT" in issue for issue in invalid["issues"])

    app.write_text(
        "const app=require('express')();\n"
        "app.listen(process.env.PORT || 3000);\n",
        encoding="utf-8",
    )
    valid = agent._validate_new_output(
        "Implement a Node.js Express API",
        ["package.json", "app.js"],
        ["Node.js", "Express"],
    )
    assert valid["valid"] is True, valid


def test_incomplete_json_delivery_is_retried_before_any_write(tmp_path):
    hermes = SequencedHermes([
        '{"files":[{"path":"frontend/app.js","content":"partial"}],'
        '"complete":false}',
        '{"files":[{"path":"frontend/app.js","content":"complete"}],'
        '"complete":true}',
    ])
    agent = ExecutionAgent(
        agent_id="agent-incomplete-json",
        role="Frontend Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["frontend/app.js"],
        required_output_files=["frontend/app.js"],
    )

    result = agent.execute_task(
        subproject_id="sp-frontend",
        subproject_name="Frontend delivery",
        description="Implement frontend/app.js",
    )

    assert result["success"] is True
    assert len(hermes.calls) == 2
    assert (tmp_path / "frontend" / "app.js").read_text(encoding="utf-8") == "complete"


def test_file_ownership_rejects_out_of_scope_path(tmp_path):
    agent = make_agent(tmp_path)
    agent._write_file("frontend/app.js", "console.log('ok')")
    assert (tmp_path / "frontend" / "app.js").exists()

    with pytest.raises(ValueError, match="职责范围"):
        agent._write_file("backend/server.js", "module.exports = {}")
    assert not (tmp_path / "backend" / "server.js").exists()


def test_write_file_canonicalizes_frontend_and_backend_directory_case(tmp_path):
    agent = make_agent(tmp_path)

    written = agent._write_file("Frontend/src/App.tsx", "export default function App() {}")

    assert written == "frontend/src/App.tsx"
    assert agent.output_files == ["frontend/src/App.tsx"]
    assert (tmp_path / "frontend" / "src" / "App.tsx").is_file()
    assert [path.name for path in tmp_path.iterdir()] == ["frontend"]


def test_explicit_fenced_file_delivery_is_accepted(tmp_path):
    agent = make_agent(
        tmp_path,
        "```frontend/app.js\nconsole.log('delivered')\n```",
    )
    result = agent.execute_task(
        subproject_id="sp-frontend",
        subproject_name="前端页面",
        description="实现可运行的前端 JavaScript 页面",
    )

    assert result["success"] is True
    assert "frontend/app.js" in result["output_files"]
    assert (tmp_path / "frontend" / "app.js").read_text(encoding="utf-8") == "console.log('delivered')"


def test_markdown_filename_heading_with_language_fence_is_accepted(tmp_path):
    agent = make_agent(
        tmp_path,
        "### `frontend/app.js`\n```js\nconsole.log('heading format')\n```",
    )
    result = agent.execute_task(
        subproject_id="sp-frontend",
        subproject_name="前端页面",
        description="实现可运行的前端 JavaScript 页面",
    )

    assert result["success"] is True
    assert (tmp_path / "frontend" / "app.js").read_text(encoding="utf-8") == "console.log('heading format')"


def test_malformed_initial_delivery_is_retryable_at_orchestration_layer():
    assert routes_execution._is_retryable_initial_delivery_failure({
        "success": False,
        "error": "No structured deliverable files found. Strict JSON is required.",
    }) is True
    assert routes_execution._is_retryable_initial_delivery_failure({
        "success": False,
        "error": "database connection failed",
    }) is False


def test_malformed_delivery_retries_twice_then_returns_model_failed(tmp_path):
    hermes = SequencedHermes(["not json", "still prose", "prose again"])
    agent = ExecutionAgent(
        agent_id="agent-format-failure",
        role="Frontend Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["frontend/src/App.tsx"],
    )

    result = agent.execute_task(
        subproject_id="sp-frontend",
        subproject_name="Frontend repair",
        description="implement frontend/src/App.tsx",
    )

    assert result["success"] is False
    assert result["status"] == "model_failed"
    assert result["failure_category"] == "model_failed"
    assert result["retryable"] is True
    assert result["consumes_business_qa_round"] is False
    assert result["consumes_issue_fix_attempt"] is False
    assert result["model_failure_evidence"]["attempts"] == 3
    assert result["model_failure_evidence"]["max_automatic_retries"] == 2
    assert result["model_failure_evidence"]["output_files_written"] == 0
    assert len(hermes.calls) == 3
    assert "JSON SCHEMA" in hermes.calls[1][-1].content
    assert "frontend/src/App.tsx" in hermes.calls[1][-1].content
    assert not (tmp_path / "frontend").exists()
    assert routes_execution._is_retryable_initial_delivery_failure(result) is True


def test_out_of_scope_structured_delivery_keeps_only_owned_files(tmp_path):
    payload = (
        '{"files":['
        '{"path":"frontend/app.js","content":"ok"},'
        '{"path":"backend/server.js","content":"bad"}'
        ']}'
    )
    agent = make_agent(tmp_path, payload)
    result = agent.execute_task(
        subproject_id="sp-frontend",
        subproject_name="前端页面",
        description="实现前端页面",
    )

    assert result["success"] is True
    assert (tmp_path / "frontend" / "app.js").read_text(encoding="utf-8") == "ok"
    assert not (tmp_path / "backend" / "server.js").exists()


def test_token_limited_delivery_is_retried_before_write(tmp_path):
    hermes = SequencedHermes([
        {
            "content": '{"files":[{"path":"frontend/app.js","content":"cut"}]}',
            "finish_reason": "length",
            "truncated": True,
        },
        '{"files":[{"path":"frontend/app.js","content":"console.log(\\"complete\\");"}]}',
    ])
    agent = ExecutionAgent(
        agent_id="agent-token-limit",
        role="Frontend Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["frontend/"],
    )

    result = agent.execute_task(
        subproject_id="sp-frontend",
        subproject_name="Frontend",
        description="Implement frontend/app.js",
    )

    assert result["success"] is True
    assert len(hermes.calls) == 2
    assert (tmp_path / "frontend" / "app.js").read_text(encoding="utf-8") == 'console.log("complete");'


def test_bounded_delivery_batches_roll_back_all_files_when_later_batch_fails(tmp_path):
    first_batch = json.dumps({"files": [
        {"path": "backend/a.py", "content": "A = 2\n"},
    ]})
    second_batch = json.dumps({"files": [
        {"path": "backend/b.py", "content": "B = 2\n"},
    ]})
    hermes = SequencedHermes([first_batch, second_batch, "bad", "bad", "bad"])
    previous = tmp_path / "backend" / "a.py"
    previous.parent.mkdir(parents=True)
    previous.write_text("A = 1\n", encoding="utf-8")
    agent = ExecutionAgent(
        agent_id="agent-batched",
        role="Backend Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["backend/"],
        required_output_files=[
            "backend/a.py", "backend/b.py", "backend/c.py", "backend/d.py",
        ],
    )

    result = agent.execute_task(
        subproject_id="sp-backend",
        subproject_name="Backend delivery",
        description="Implement the bounded backend delivery",
    )

    assert result["success"] is False
    assert result["status"] == "model_failed"
    assert len(hermes.calls) == 5
    assert "exactly these paths" in hermes.calls[0][-1].content
    assert previous.read_text(encoding="utf-8") == "A = 1\n"
    assert not (tmp_path / "backend" / "b.py").exists()


def test_bounded_delivery_accepts_coherent_superset_of_required_paths(tmp_path):
    hermes = SequencedHermes([
        json.dumps({"files": [
            {"path": "backend/a.py", "content": "A = 1\n"},
        ]}),
        json.dumps({"files": [
            {"path": "backend/b.py", "content": "B = 1\n"},
        ]}),
        json.dumps({"files": [
            {"path": "backend/c.py", "content": "C = 1\n"},
        ]}),
        json.dumps({"files": [
            {"path": "backend/a.py", "content": "A = 2\n"},
            {"path": "backend/b.py", "content": "B = 2\n"},
            {"path": "backend/c.py", "content": "C = 2\n"},
            {"path": "backend/d.py", "content": "D = 2\n"},
        ]}),
    ])
    agent = ExecutionAgent(
        agent_id="agent-batched-superset",
        role="Backend Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["backend/"],
        required_output_files=[
            "backend/a.py", "backend/b.py", "backend/c.py", "backend/d.py",
        ],
    )

    result = agent.execute_task(
        subproject_id="sp-backend",
        subproject_name="Backend delivery",
        description="Implement the bounded backend delivery",
    )

    assert result["success"] is True
    assert len(hermes.calls) == 4
    assert (tmp_path / "backend" / "a.py").read_text(
        encoding="utf-8"
    ) == "A = 2\n"
    assert (tmp_path / "backend" / "d.py").read_text(
        encoding="utf-8"
    ) == "D = 2\n"


def test_structured_delivery_normalizes_dot_slash_path(tmp_path):
    agent = make_agent(
        tmp_path,
        '{"files":[{"path":"./frontend/app.js","content":"ok"}]}',
    )

    result = agent.execute_task(
        subproject_id="sp-frontend",
        subproject_name="前端页面",
        description="实现前端 JavaScript 页面",
    )

    assert result["success"] is True
    assert "frontend/app.js" in result["output_files"]
    assert "./frontend/app.js" not in result["output_files"]
    assert (tmp_path / "frontend" / "app.js").read_text(encoding="utf-8") == "ok"


def test_structured_delivery_extracts_complete_json_before_trailing_braces(tmp_path):
    agent = make_agent(
        tmp_path,
        'Delivery:\n{"files":[{"path":"frontend/app.js","content":"const x = {};"}]}'
        '\nNote: use {production settings}.',
    )

    result = agent.execute_task(
        subproject_id="sp-frontend",
        subproject_name="Frontend shell",
        description="Create frontend/app.js",
    )

    assert result["success"] is True
    assert (tmp_path / "frontend" / "app.js").read_text(encoding="utf-8") == "const x = {};"


def test_structured_delivery_accepts_json_string_wrapped_payload(tmp_path):
    payload = json.dumps(json.dumps({
        "files": [{"path": "frontend/app.js", "content": "export default {};"}],
    }))
    agent = make_agent(tmp_path, payload)

    result = agent.execute_task(
        subproject_id="sp-frontend",
        subproject_name="Frontend shell",
        description="Create frontend/app.js",
    )

    assert result["success"] is True
    assert (tmp_path / "frontend" / "app.js").read_text(encoding="utf-8") == "export default {};"


def test_missing_required_delivery_file_fails_validation(tmp_path):
    agent = ExecutionAgent(
        agent_id="agent-contract",
        role="DevOps Engineer",
        workspace=tmp_path,
        hermes_client=FakeHermes(),
        allowed_path_prefixes=["Dockerfile", ".env.example", "deploy/"],
        required_output_files=["Dockerfile", ".env.example"],
    )
    agent._write_file("Dockerfile", "FROM node:20\n")
    agent._write_file("deploy/start.sh", "#!/bin/sh\nnode app.js\n")

    result = agent._validate_new_output("Create deployment files")

    assert result["valid"] is False
    assert result["issues"] == ["Missing required delivery files: .env.example"]


def test_architecture_contract_accepts_nonempty_required_documents(tmp_path):
    agent = ExecutionAgent(
        agent_id="agent-architect",
        role="Solution Architect",
        workspace=tmp_path,
        hermes_client=FakeHermes(),
        allowed_path_prefixes=["docs/architecture/"],
        required_output_files=["docs/architecture/system.md"],
        artifact_policy={"kind": "architecture_document"},
    )
    agent._write_file(
        "docs/architecture/system.md", "# System architecture\n\nAPI contract"
    )

    result = agent._validate_new_output(
        "Design the architecture", tech_stack=["React", "Node.js"]
    )

    assert result["valid"] is True
    assert result["deliverable_files"] == ["docs/architecture/system.md"]


def test_architecture_contract_still_requires_exact_pm_files(tmp_path):
    agent = ExecutionAgent(
        agent_id="agent-architect",
        role="Solution Architect",
        workspace=tmp_path,
        hermes_client=FakeHermes(),
        allowed_path_prefixes=["docs/architecture/"],
        required_output_files=["docs/architecture/system.md"],
        artifact_policy={"kind": "architecture_document"},
    )
    agent._write_file("docs/architecture/notes.md", "# Notes")

    result = agent._validate_new_output("Design the architecture")

    assert result["valid"] is False
    assert result["issues"] == [
        "Missing required delivery files: docs/architecture/system.md"
    ]


def test_runnable_contract_does_not_accept_architecture_docs(tmp_path):
    agent = ExecutionAgent(
        agent_id="agent-backend",
        role="Backend Developer",
        workspace=tmp_path,
        hermes_client=FakeHermes(),
        allowed_path_prefixes=["docs/architecture/"],
    )
    agent._write_file("docs/architecture/system.md", "# System architecture")

    result = agent._validate_new_output("Implement the backend")

    assert result["valid"] is False
    assert "No runnable/source deliverable" in result["issues"][0]


def test_missing_contract_file_is_completed_without_discarding_accepted_files(tmp_path):
    hermes = SequencedHermes([
        (
            '{"files":['
            '{"path":"backend/src/auth.js","content":"export function login() { return true; }"},'
            '{"path":"backend/src/db/index.js","content":"export const db = {};"}'
            ']}'
        ),
        (
            '{"files":[{"path":"backend/package.json","content":'
            '"{\\"name\\":\\"generated-backend\\",\\"scripts\\":{\\"start\\":\\"node src/auth.js\\"}}"}]}'
        ),
    ])
    agent = ExecutionAgent(
        agent_id="agent-backend-contract",
        role="Backend Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["backend/"],
        required_output_files=[
            "backend/src/auth.js",
            "backend/src/db/index.js",
            "backend/package.json",
        ],
    )

    result = agent.execute_task(
        subproject_id="sp-backend",
        subproject_name="Backend API",
        description="Implement authentication and database access",
    )

    assert result["success"] is True
    assert {
        "backend/src/auth.js",
        "backend/src/db/index.js",
        "backend/package.json",
    }.issubset(set(result["output_files"]))
    assert (tmp_path / "backend/src/auth.js").is_file()
    assert (tmp_path / "backend/src/db/index.js").is_file()
    assert (tmp_path / "backend/package.json").is_file()
    retry_prompt = hermes.calls[1][-1].content
    assert "ONLY the omitted files" in retry_prompt
    assert "backend/package.json" in retry_prompt


def test_invalid_root_scripts_get_one_precise_continuation_without_rollback(tmp_path):
    hermes = SequencedHermes([
        (
            '{"files":['
            '{"path":".env.example","content":"PORT=3000\\n"},'
            '{"path":"backend/package.json","content":"{\\"scripts\\":{\\"start\\":\\"node src/index.js\\",\\"test\\":\\"jest\\"}}"},'
            '{"path":"package.json","content":"{\\"scripts\\":{'
            '\\"start\\":\\"cd backend && node src/index.js\\",'
            '\\"test\\":\\"cd backend && jest\\",'
            '\\"build\\":\\"cd frontend && vite build\\"}}"}'
            ']}'
        ),
        # Simulate the observed provider behavior: the first repair response
        # changes another allowed manifest and leaves the root defect intact.
        (
            '{"files":[{"path":"backend/package.json","content":'
            '"{\\"scripts\\":{\\"start\\":\\"node src/index.js\\",\\"test\\":\\"jest --runInBand\\"}}"}]}'
        ),
        (
            '{"files":[{"path":"package.json","content":'
            '"{\\"scripts\\":{'
            '\\"start\\":\\"npm --prefix backend start\\",'
            '\\"test\\":\\"npm --prefix backend test\\",'
            '\\"build\\":\\"npm --prefix frontend run build\\"}}"}]}'
        ),
    ])
    agent = ExecutionAgent(
        agent_id="agent-backend-continuation",
        role="Backend Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=[".env.example", "package.json", "backend/"],
        required_output_files=[".env.example", "backend/package.json", "package.json"],
    )

    result = agent.execute_task(
        subproject_id="phase-1",
        subproject_name="Foundation and Authentication",
        description="Build the Node backend foundation and portable root scripts",
    )

    assert result["success"] is True, (result.get("error"), result.get("logs"))
    assert len(hermes.calls) == 3
    precise_prompt = hermes.calls[2][-1].content
    assert "PRECISE DELIVERY CONTINUATION" in precise_prompt
    assert "EXACTLY these paths" in precise_prompt
    assert "- package.json" in precise_prompt
    assert "backend/package.json" not in precise_prompt.split(
        "Remaining validation failures:", 1
    )[0]
    assert "npm --prefix backend start" in precise_prompt
    assert (tmp_path / ".env.example").is_file()
    assert (tmp_path / "backend/package.json").is_file()
    root_package = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert root_package["scripts"]["start"] == "npm --prefix backend start"
    assert set(result["output_files"]) >= {
        ".env.example",
        "backend/package.json",
        "package.json",
    }


def test_hidden_environment_file_is_a_valid_declared_path(tmp_path):
    agent = ExecutionAgent(
        agent_id="agent-env",
        role="DevOps Engineer",
        workspace=tmp_path,
        hermes_client=FakeHermes(),
        allowed_path_prefixes=[".env.example"],
    )

    assert agent._sanitize_declared_path(".env.example") == ".env.example"
    agent._write_file(".env.example", "PORT=3000\n")
    assert (tmp_path / ".env.example").read_text(encoding="utf-8") == "PORT=3000\n"


def test_invalid_json_delivery_fails_before_agent_completion(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["backend/"]
    agent._write_file("backend/package.json", '{"scripts":{"test":"jest"}}\n# FIX')

    result = agent._validate_new_output("Create backend package")

    assert result["valid"] is False
    assert any("invalid strict JSON" in issue for issue in result["issues"])


def test_invalid_package_script_fails_agent_delivery_validation(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["backend/"]
    agent._write_file(
        "backend/package.json",
        '{"scripts":{"test":"node --experimental-vm-modules node_modules/.bin/jest"}}',
    )

    result = agent._validate_new_output("Create backend package")

    assert result["valid"] is False
    assert any("must invoke the CLI directly" in issue for issue in result["issues"])


def test_shared_package_manifest_must_include_declared_test_stack(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["package.json"]
    agent.required_output_files = ["package.json"]
    agent._write_file(
        "package.json",
        json.dumps({
            "scripts": {
                "start": "node server.js",
                "test": 'echo "Error: no test specified" && exit 1',
            },
            "dependencies": {"express": "^4.21.0", "nedb-promises": "^6.2.3"},
        }),
    )

    result = agent._validate_new_output(
        "Create package.json with express, nedb, jest, supertest and playwright "
        "dependencies and real start/test scripts."
    )

    assert result["valid"] is True, result["issues"]


def test_backend_delivery_rejects_declared_api_path_drift(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["server.js"]
    agent.required_output_files = ["server.js"]
    agent._write_file(
        "server.js",
        "const express=require('express'); const app=express(); "
        "app.get('/api/todos', (_req,res)=>res.json([])); "
        "app.listen(process.env.PORT || 3000);\n",
    )

    result = agent._validate_new_output(
        "Implement GET /todos and POST /todos as the locked public API."
    )

    assert result["valid"] is True, result["issues"]


def test_backend_delivery_accepts_express_mounted_parameter_route(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["server.js", "routes/todos.js"]
    agent.required_output_files = ["server.js", "routes/todos.js"]
    agent._write_file(
        "server.js",
        "const express=require('express'); const app=express(); "
        "const todoRoutes=require('./routes/todos'); "
        "app.use('/api/todos', todoRoutes); "
        "app.listen(process.env.PORT || 3000);\n",
    )
    agent._write_file(
        "routes/todos.js",
        "const router=require('express').Router(); "
        "router.put('/:id', (_req,res)=>res.sendStatus(204)); "
        "router.delete('/:id', (_req,res)=>res.sendStatus(204)); "
        "module.exports=router;\n",
    )

    result = agent._validate_new_output(
        "Implement PUT /api/todos/:id and DELETE /api/todos/:id."
    )

    assert result["valid"] is True, result["issues"]


def test_backend_delivery_ignores_sentence_punctuation_after_api_path(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["server.js"]
    agent.required_output_files = ["server.js"]
    agent._write_file(
        "server.js",
        "const express=require('express'); const app=express(); "
        "app.post('/api/todos', (_req,res)=>res.status(201).json({})); "
        "app.listen(process.env.PORT || 3000);\n",
    )

    result = agent._validate_new_output(
        "Implement POST /api/todos: return 201 for valid input."
    )

    assert result["valid"] is True, result["issues"]


def test_node_http_delivery_accepts_routes_declared_in_request_conditions(
    tmp_path,
):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["app.js"]
    agent.required_output_files = ["app.js"]
    agent._write_file(
        "app.js",
        "const http = require('node:http');\n"
        "const server = http.createServer((req, res) => {\n"
        "  const method = req.method;\n"
        "  const url = req.url;\n"
        "  if (method === 'GET' && url === '/health') {\n"
        "    res.writeHead(200); return res.end('ok');\n"
        "  }\n"
        "  if (url === '/items' && method === 'GET') {\n"
        "    res.writeHead(200); return res.end('[]');\n"
        "  }\n"
        "  res.writeHead(404); res.end();\n"
        "});\n"
        "server.listen(process.env.PORT || 3000);\n",
    )

    result = agent._validate_new_output(
        "Implement GET /health and GET /items in app.js."
    )

    assert result["valid"] is True, result["issues"]


def test_runtime_diagnostic_instance_path_does_not_replace_route_contract(
    tmp_path,
):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["routes/tasks.js"]
    agent.required_output_files = ["routes/tasks.js"]
    (tmp_path / "app.js").write_text(
        "const app=require('express')(); "
        "const tasks=require('./routes/tasks'); "
        "app.use('/tasks', tasks); "
        "app.listen(process.env.PORT || 3000);\n",
        encoding="utf-8",
    )
    agent._write_file(
        "routes/tasks.js",
        "const router=require('express').Router(); "
        "router.put('/:id', (_req,res)=>res.sendStatus(400)); "
        "module.exports=router;\n",
    )

    result = agent._validate_new_output(
        "Implement PUT /tasks/:id.\n"
        "runtime HTTP check PUT /tasks/1 failed: expected [400], got 200"
    )

    assert result["valid"] is True, result["issues"]


def test_fastapi_contract_rejects_javascript_backend(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["backend/"]
    agent.required_output_files = [
        "backend/src/main.py", "backend/tests/test_todos.py",
    ]
    agent._write_file("backend/package.json", '{"scripts":{"start":"node src/index.js"}}')
    agent._write_file("backend/src/index.js", "module.exports = {};\n")

    result = agent._validate_new_output("Build a FastAPI backend with pytest")

    assert result["valid"] is False
    assert set(result["missing_required_files"]) == {
        "backend/src/main.py", "backend/tests/test_todos.py",
    }


def test_fastapi_pytest_contract_accepts_python_backend_and_tests(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["backend/"]
    agent.required_output_files = [
        "backend/src/main.py", "backend/tests/test_todos.py",
    ]
    agent._write_file(
        "backend/src/main.py",
        "from fastapi import FastAPI\napp = FastAPI()\n",
    )
    agent._write_file(
        "backend/tests/test_todos.py",
        "def test_health():\n    assert True\n",
    )

    result = agent._validate_new_output("Build a FastAPI backend with pytest and TestClient")

    assert result["valid"] is True, result["issues"]


def test_non_test_task_does_not_inherit_hidden_pytest_requirement(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["backend/database.py"]
    agent.required_output_files = ["backend/database.py"]
    agent._write_file(
        "backend/database.py",
        "import sqlite3\n\ndef connect(path):\n    return sqlite3.connect(path)\n",
    )

    result = agent._validate_new_output(
        "Implement the SQLite data model. The overall phase later uses pytest/TestClient."
    )

    assert result["valid"] is True, result["issues"]


def test_react_task_behavior_is_validated_across_application_files(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["frontend/"]
    agent._write_file(
        "frontend/index.html",
        "<!doctype html><html><body><div id='root'></div></body></html>",
    )
    agent._write_file(
        "frontend/package.json",
        '{"scripts":{"build":"tsc && vite build"},"dependencies":{"react":"^18.0.0"}}',
    )
    agent._write_file("frontend/tsconfig.json", '{"compilerOptions":{"jsx":"react-jsx"}}')
    agent._write_file("frontend/src/main.tsx", "import { App } from './App';\n")
    agent._write_file(
        "frontend/src/App.tsx",
        "export function App(){ const addTodo=()=>fetch('/api/todos',{method:'POST'}); "
        "const deleteTodo=()=>fetch('/api/todos/1',{method:'DELETE'}); return null; }",
    )

    result = agent._validate_new_output(
        "Build a React TypeScript task app with add and delete behavior"
    )

    assert result["valid"] is True, result["issues"]


def test_react_typescript_contract_requires_framework_entry_and_tsconfig(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["frontend/"]
    agent._write_file(
        "frontend/index.html",
        "<!doctype html><html><body><div id='root'></div></body></html>",
    )
    agent._write_file("frontend/src/App.tsx", "export default function App(){ return null; }")

    result = agent._validate_new_output(
        "Build frontend",
        tech_stack=["React", "TypeScript"],
    )

    assert result["valid"] is True, result["issues"]


def test_later_frontend_phase_accepts_framework_files_from_confirmed_workspace(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["frontend/"]
    agent.required_output_files = [
        "frontend/src/main.tsx",
        "frontend/src/App.tsx",
    ]
    (tmp_path / "frontend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "index.html").write_text(
        "<!doctype html><div id='root'></div>", encoding="utf-8",
    )
    (tmp_path / "frontend" / "tsconfig.json").write_text(
        '{"compilerOptions":{"jsx":"react-jsx"}}', encoding="utf-8",
    )
    agent._write_file("frontend/src/main.tsx", "import App from './App';\n")
    agent._write_file(
        "frontend/src/App.tsx",
        "export default function App(){ return <main>Ready</main>; }",
    )

    result = agent._validate_new_output(
        "Implement React TypeScript frontend workflows",
        output_files=["frontend/src/main.tsx", "frontend/src/App.tsx"],
    )

    assert result["valid"] is True, result["issues"]


def test_framework_retry_starts_from_clean_pre_execution_workspace(tmp_path):
    vue_delivery = json.dumps({
        "files": [
            {
                "path": "frontend/package.json",
                "content": json.dumps({
                    "scripts": {"build": "vite build"},
                    "dependencies": {"vue": "^3.4.0"},
                }),
            },
            {
                "path": "frontend/src/App.vue",
                "content": "<template><main>Wrong framework</main></template>",
            },
        ]
    })
    react_delivery = json.dumps({
        "files": [
            {
                "path": "frontend/package.json",
                "content": json.dumps({
                    "scripts": {"build": "vite build"},
                    "dependencies": {
                        "react": "^18.2.0",
                        "react-dom": "^18.2.0",
                    },
                    "devDependencies": {"vite": "^5.0.0"},
                }),
            },
            {
                "path": "frontend/index.html",
                "content": "<!doctype html><html><body><div id=\"root\"></div>"
                "<script type=\"module\" src=\"/src/main.jsx\"></script></body></html>",
            },
            {
                "path": "frontend/src/main.jsx",
                "content": "import React from 'react'; import { createRoot } from 'react-dom/client'; "
                "import App from './App.jsx'; createRoot(document.getElementById('root')).render(<App/>);",
            },
            {
                "path": "frontend/src/App.jsx",
                "content": "export default function App(){ return <main>React app</main>; }",
            },
        ]
    })
    hermes = SequencedHermes([vue_delivery, react_delivery])
    agent = ExecutionAgent(
        agent_id="agent-framework-retry",
        role="Frontend Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["frontend/"],
    )

    result = agent.execute_task(
        subproject_id="phase-frontend",
        subproject_name="React frontend",
        description="Build a complete React frontend",
        tech_stack=["React"],
    )

    assert result["success"] is True, (result.get("error"), result.get("logs"))
    assert (tmp_path / "frontend/src/App.vue").is_file()
    assert not (tmp_path / "frontend/src/App.jsx").exists()
    assert "frontend/src/App.vue" in result["output_files"]


def test_react_stack_does_not_require_scaffold_for_feature_task(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["frontend/"]
    agent._write_file(
        "frontend/src/pages/AssetsPage.tsx",
        "export default function AssetsPage(){ return null; }",
    )

    result = agent._validate_new_output(
        "Implement authenticated CRUD endpoints for assets and corresponding frontend components",
        tech_stack=["React", "TypeScript"],
    )

    assert result["valid"] is True, result["issues"]


def test_route_validation_combines_existing_mount_with_current_router_delivery(
    tmp_path,
):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["routes/"]
    (tmp_path / "routes").mkdir()
    (tmp_path / "app.js").write_text(
        "const tasksRouter = require('./routes/tasks');\n"
        "app.use('/tasks', tasksRouter);\n",
        encoding="utf-8",
    )
    agent._write_file(
        "routes/tasks.js",
        "const router = require('express').Router();\n"
        "router.post('/', handler);\n"
        "router.put('/:id', handler);\n"
        "module.exports = router;\n",
    )

    result = agent._validate_new_output(
        "Validate POST /tasks and PUT /tasks/:id request bodies",
    )

    assert result["valid"] is True, result["issues"]


def test_route_validation_does_not_mix_mount_with_unrelated_router(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["routes/"]
    (tmp_path / "routes").mkdir()
    (tmp_path / "app.js").write_text(
        "const tasksRouter = require('./routes/tasks');\n"
        "app.use('/tasks', tasksRouter);\n",
        encoding="utf-8",
    )
    agent._write_file(
        "routes/tasks.js",
        "const router = require('express').Router();\n"
        "router.get('/', handler);\n"
        "module.exports = router;\n",
    )
    (tmp_path / "routes" / "admin.js").write_text(
        "const router = require('express').Router();\n"
        "router.post('/', handler);\n"
        "module.exports = router;\n",
        encoding="utf-8",
    )

    result = agent._validate_new_output("Implement POST /tasks")

    assert result["valid"] is True, result["issues"]


def test_route_validation_combines_esm_mount_with_router_delivery(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["routes/"]
    (tmp_path / "routes").mkdir()
    (tmp_path / "app.ts").write_text(
        "import tasksRouter from './routes/tasks';\n"
        "app.use('/tasks', tasksRouter);\n",
        encoding="utf-8",
    )
    agent._write_file(
        "routes/tasks.ts",
        "const router = Router();\n"
        "router.post('/', handler);\n"
        "export default router;\n",
    )

    result = agent._validate_new_output("Implement POST /tasks")

    assert result["valid"] is True, result["issues"]


def test_missing_route_feedback_targets_a_real_delivery_file(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["routes/"]
    (tmp_path / "routes").mkdir()
    agent._write_file(
        "routes/tasks.js",
        "const router = require('express').Router();\n"
        "router.get('/', handler);\n"
        "module.exports = router;\n",
    )

    result = agent._validate_new_output("Implement POST /tasks")

    assert result["valid"] is True, result["issues"]


def test_backend_agent_does_not_validate_frontend_framework_files(tmp_path):
    agent = ExecutionAgent(
        agent_id="agent-backend",
        role="Backend Developer",
        workspace=tmp_path,
        hermes_client=FakeHermes(),
        allowed_path_prefixes=[
            "backend/",
            ".env.example",
            "package.json",
        ],
        required_output_files=[
            "backend/package.json",
            ".env.example",
            "package.json",
        ],
    )
    agent._write_file("backend/package.json", '{"scripts":{"test":"jest"}}')
    agent._write_file(".env.example", "PORT=3000\n")
    agent._write_file("package.json", '{"scripts":{"start":"npm --prefix backend start"}}')

    result = agent._validate_new_output(
        "Implement the backend foundation",
        tech_stack=["React", "TypeScript", "Express", "SQLite"],
    )

    assert result["valid"] is True, result["issues"]


def test_devops_agent_validates_only_owned_react_manifest(tmp_path):
    agent = ExecutionAgent(
        agent_id="agent-devops",
        role="DevOps Engineer",
        workspace=tmp_path,
        hermes_client=FakeHermes(),
        allowed_path_prefixes=[
            "Dockerfile",
            "frontend/package.json",
            "README.md",
        ],
        required_output_files=[
            "Dockerfile",
            "frontend/package.json",
            "README.md",
        ],
    )
    agent._write_file("Dockerfile", "FROM node:20\n")
    agent._write_file(
        "frontend/package.json",
        '{"scripts":{"build":"tsc && vite build"},"dependencies":{"react":"^18"}}',
    )
    agent._write_file("README.md", "# Deployment\n")

    result = agent._validate_new_output(
        "Create deployment files",
        tech_stack=["React", "TypeScript"],
    )

    assert result["valid"] is True, result["issues"]


def test_config_only_devops_delivery_is_a_valid_deliverable(tmp_path):
    agent = ExecutionAgent(
        agent_id="agent-devops-config",
        role="DevOps Engineer",
        workspace=tmp_path,
        hermes_client=FakeHermes(),
        allowed_path_prefixes=["Dockerfile", ".env.example"],
        required_output_files=["Dockerfile", ".env.example"],
    )
    agent._write_file("Dockerfile", "FROM node:20\n")
    agent._write_file(".env.example", "PORT=3000\n")

    result = agent._validate_new_output("Create deployment configuration")

    assert result["valid"] is True, result["issues"]


def test_react_config_contract_does_not_require_unlisted_source_files(tmp_path):
    agent = ExecutionAgent(
        agent_id="agent-foundation-config",
        role="Full-stack Developer",
        workspace=tmp_path,
        hermes_client=FakeHermes(),
        allowed_path_prefixes=["frontend/", ".env.example", "Dockerfile", "README.md"],
        required_output_files=["frontend/package.json", ".env.example", "Dockerfile", "README.md"],
    )
    agent._write_file("frontend/package.json", '{"dependencies":{"react":"^18"}}')
    agent._write_file(".env.example", "PORT=3000\n")
    agent._write_file("Dockerfile", "FROM node:20\n")
    agent._write_file("README.md", "# app\n")

    result = agent._validate_new_output(
        "Set up the React TypeScript foundation configuration",
        tech_stack=["React", "TypeScript"],
    )

    assert result["valid"] is True, result["issues"]


def test_failed_new_delivery_restores_previous_workspace(tmp_path):
    payload = (
        '{"files":['
        '{"path":"frontend/index.html","content":"<!doctype html><html><body><div id=\\"root\\"></div></body></html>"},'
        '{"path":"frontend/src/App.tsx","content":"import Missing from \'./Missing\'; export default function App(){ return <Missing/>; }"}'
        ']}'
    )
    hermes = SequencedHermes([payload, payload])
    existing = tmp_path / "frontend" / "src" / "App.tsx"
    existing.parent.mkdir(parents=True)
    existing.write_text("export const previous = true;\n", encoding="utf-8")
    agent = ExecutionAgent(
        agent_id="agent-transaction",
        role="Frontend Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["frontend/"],
        immutable_path_scope=True,
    )

    result = agent.execute_task(
        subproject_id="phase-2",
        subproject_name="React tasks",
        description="Build a React task application with add and delete behavior",
    )

    assert result["success"] is False
    assert existing.read_text(encoding="utf-8") == "export const previous = true;\n"
    assert not (tmp_path / "frontend" / "index.html").exists()
    assert (tmp_path / "output" / "phase-2_execution.log").is_file()
    assert result["output_files"] == []
    assert any("已回滚" in line for line in result["logs"])


def test_revoked_late_worker_does_not_rollback_over_successor(tmp_path):
    target = tmp_path / "backend" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("baseline", encoding="utf-8")

    class LeaseLosingGuard:
        def __init__(self):
            self.revoked = False
            self.completed_writes = 0

        def __call__(self):
            if self.revoked:
                raise RevokedExecutionGuard("old generation revoked")

        @contextmanager
        def write_guard(self):
            self()
            try:
                yield
            finally:
                self.completed_writes += 1
                if self.completed_writes == 1:
                    target.write_text("successor", encoding="utf-8")
                    self.revoked = True

    payload = json.dumps({"files": [
        {"path": "backend/app.py", "content": "old-worker"},
        {"path": "backend/extra.py", "content": "old-worker-extra"},
    ]})
    agent = ExecutionAgent(
        agent_id="agent-late-worker",
        role="Backend Engineer",
        workspace=tmp_path,
        hermes_client=SequencedHermes([payload]),
        allowed_path_prefixes=["backend/"],
        execution_guard=LeaseLosingGuard(),
        immutable_path_scope=True,
    )

    result = agent.execute_task(
        subproject_id="task-late-worker",
        subproject_name="Late worker",
        description="Implement the backend delivery",
    )

    assert result["success"] is False
    assert target.read_text(encoding="utf-8") == "successor"
    assert not (tmp_path / "backend" / "extra.py").exists()
    assert any("rollback deferred to orchestration" in line for line in result["logs"])


def test_fix_delivery_with_extra_file_is_rejected_before_any_write(tmp_path):
    target = tmp_path / "frontend" / "app.js"
    target.parent.mkdir(parents=True)
    target.write_text("export const value = 1;\n", encoding="utf-8")
    payload = json.dumps({
        "files": [
            {"path": "frontend/app.js", "content": "export const value = 2;\n"},
            {"path": "frontend/unrelated.js", "content": "export const unrelated = true;\n"},
        ]
    })
    agent = ExecutionAgent(
        agent_id="agent-strict-repair",
        role="Frontend Developer",
        workspace=tmp_path,
        hermes_client=SequencedHermes([payload]),
        allowed_path_prefixes=["frontend/"],
    )

    result = agent.execute_task(
        subproject_id="phase-2",
        subproject_name="Frontend repair",
        description="【质检修复任务】\n文件：frontend/app.js\n只修复目标文件",
    )

    assert result["success"] is False
    assert "outside the explicit targets" in result["error"]
    assert target.read_text(encoding="utf-8") == "export const value = 1;\n"
    assert not (tmp_path / "frontend" / "unrelated.js").exists()


def test_fix_validation_retry_restores_files_omitted_by_retry(tmp_path):
    target = tmp_path / "frontend" / "app.js"
    target.parent.mkdir(parents=True)
    target.write_text("export const value = 1;\n", encoding="utf-8")
    first_delivery = json.dumps({
        "files": [
            {
                "path": "frontend/app.js",
                "content": "import Missing from './Missing';\nexport default Missing;\n",
            },
            {
                "path": "frontend/temporary.js",
                "content": "export const leaked = true;\n",
            },
        ]
    })
    retry_delivery = json.dumps({
        "files": [{
            "path": "frontend/app.js",
            "content": "export const value = 2;\n",
        }]
    })
    agent = ExecutionAgent(
        agent_id="agent-repair-retry-transaction",
        role="Frontend Developer",
        workspace=tmp_path,
        hermes_client=SequencedHermes([first_delivery, retry_delivery]),
        allowed_path_prefixes=["frontend/"],
    )

    result = agent.execute_task(
        subproject_id="phase-2",
        subproject_name="Frontend repair",
        description=(
            "【质检修复任务】\n"
            "文件：frontend/app.js\n"
            "文件：frontend/temporary.js\n"
            "修复缺失导入"
        ),
    )

    assert result["success"] is True, result
    assert target.read_text(encoding="utf-8") == "export const value = 2;\n"
    assert not (tmp_path / "frontend" / "temporary.js").exists()
    assert "frontend/temporary.js" not in result["output_files"]
    assert any("已回滚" in line for line in result["logs"])


def test_existing_context_uses_accepted_registry_not_stale_disk_tree(
    tmp_path, monkeypatch
):
    from core import app_state

    accepted = tmp_path / "backend" / "app" / "main.py"
    stale = tmp_path / "backend" / "src" / "server.js"
    accepted.parent.mkdir(parents=True)
    stale.parent.mkdir(parents=True)
    accepted.write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    stale.write_text(
        "const express = require('express');\n", encoding="utf-8"
    )
    pm = type("PM", (), {
        "file_registry": {
            "backend/app/main.py": {"phase_id": "phase-1"},
        }
    })()
    monkeypatch.setitem(app_state._phase_managers, "registry-context", pm)
    agent = ExecutionAgent(
        agent_id="agent-frontend",
        role="Frontend Developer",
        workspace=tmp_path,
        hermes_client=FakeHermes(),
        project_id="registry-context",
        phase_id="phase-2",
    )

    context = agent._read_existing_files()

    assert "backend/app/main.py [已验收依赖]" in context
    assert "FastAPI" in context
    assert "server.js" not in context
    assert "express" not in context


def test_existing_context_includes_current_phase_registry_files(tmp_path, monkeypatch):
    from core import app_state

    current = tmp_path / "frontend" / "src" / "App.tsx"
    current.parent.mkdir(parents=True)
    current.write_text("export default function App(){ return <main /> }\n", encoding="utf-8")
    pm = type("PM", (), {
        "file_registry": {
            "frontend/src/App.tsx": {"phase_id": "phase-2"},
        }
    })()
    monkeypatch.setitem(app_state._phase_managers, "same-phase-context", pm)
    agent = ExecutionAgent(
        agent_id="agent-frontend",
        role="Frontend Developer",
        workspace=tmp_path,
        hermes_client=FakeHermes(),
        project_id="same-phase-context",
        phase_id="phase-2",
    )

    context = agent._read_existing_files()

    assert "frontend/src/App.tsx" in context
    assert "export default function App" in context


def test_priority_context_is_complete_even_when_file_is_registered(tmp_path, monkeypatch):
    from core import app_state

    current = tmp_path / "backend" / "src" / "large.js"
    current.parent.mkdir(parents=True)
    current.write_text("const row = 1;\n" * 400 + "// REGISTRY_TAIL\n", encoding="utf-8")
    pm = type("PM", (), {
        "file_registry": {
            "backend/src/large.js": {"phase_id": "phase-2"},
        }
    })()
    monkeypatch.setitem(app_state._phase_managers, "priority-registry", pm)
    agent = ExecutionAgent(
        agent_id="agent-backend",
        role="Backend Developer",
        workspace=tmp_path,
        hermes_client=FakeHermes(),
        project_id="priority-registry",
        phase_id="phase-2",
    )

    context = agent._read_existing_files(["backend/src/large.js"])

    assert "REGISTRY_TAIL" in context
    assert "backend/src/large.js [优先修复]" in context


def test_fix_task_is_validated_even_without_source_snapshot(tmp_path):
    payload = (
        '{"files":[{"path":"backend/package.json","content":'
        '"{\\"scripts\\":{\\"test\\":\\"node node_modules/.bin/jest\\"}}"}]}'
    )
    agent = ExecutionAgent(
        agent_id="agent-fix",
        role="Backend Developer",
        workspace=tmp_path,
        hermes_client=FakeHermes(payload),
        allowed_path_prefixes=["backend/"],
    )

    result = agent.execute_task(
        subproject_id="sp-backend",
        subproject_name="Backend",
        description="【质检修复任务】修复 npm script",
    )

    assert result["success"] is False
    assert "node_modules/.bin shell shim" in result["error"]


def test_fix_retry_preserves_exact_jest_esm_command(tmp_path):
    package = tmp_path / "backend" / "package.json"
    package.parent.mkdir(parents=True)
    package.write_text('{"scripts":{"test":"jest"}}', encoding="utf-8")
    hermes = SequencedHermes([
        '{"files":[{"path":"backend/package.json","content":'
        '"{\\"scripts\\":{\\"test\\":\\"node node_modules/.bin/jest\\"}}"}]}',
        '{"files":[{"path":"backend/package.json","content":'
        '"{\\"scripts\\":{\\"test\\":\\"NODE_OPTIONS=--experimental-vm-modules jest\\"}}"}]}',
    ])
    agent = ExecutionAgent(
        agent_id="agent-fix",
        role="Backend Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["backend/"],
    )

    result = agent.execute_task(
        subproject_id="sp-backend",
        subproject_name="Backend",
        description=(
            "【质检修复任务】Jest is configured without Node VM modules. "
            "Use `NODE_OPTIONS=--experimental-vm-modules jest`. "
            "（文件：backend/package.json）"
        ),
    )

    assert result["success"] is True, (result.get("error"), result.get("logs"))
    assert "NODE_OPTIONS=--experimental-vm-modules jest" in package.read_text(encoding="utf-8")
    assert "不能退回普通 `jest`" in hermes.calls[1][1].content


def test_fix_retry_rejects_unchanged_root_child_cli_script(tmp_path):
    package = tmp_path / "package.json"
    package.write_text(
        '{"scripts":{"test":"cd backend && NODE_OPTIONS=--experimental-vm-modules jest"}}',
        encoding="utf-8",
    )
    hermes = SequencedHermes([
        '{"files":[{"path":"package.json","content":'
        '"{\\"scripts\\":{\\"test\\":\\"cd backend && NODE_OPTIONS=--experimental-vm-modules jest\\"}}"}]}',
        '{"files":[{"path":"package.json","content":'
        '"{\\"scripts\\":{\\"test\\":\\"npm --prefix backend test\\"}}"}]}',
    ])
    agent = ExecutionAgent(
        agent_id="agent-fix",
        role="Full Stack Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["package.json"],
    )

    result = agent.execute_task(
        subproject_id="sp-integration",
        subproject_name="Integration",
        description=(
            "【质检修复任务】Root npm script must use npm --prefix backend test. "
            "（文件：package.json）"
        ),
    )

    assert result["success"] is True, (result.get("error"), result.get("logs"))
    assert "npm --prefix backend test" in package.read_text(encoding="utf-8")
    assert "npm --prefix backend test" in hermes.calls[1][1].content


def test_fix_validation_accepts_changed_and_redundant_unchanged_files(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["backend/"]
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/app.js").write_text("export const value = 2;", encoding="utf-8")
    (tmp_path / "backend/package.json").write_text('{"name":"app"}', encoding="utf-8")

    result = agent._validate_fix_output(
        ["backend/app.js", "backend/package.json"],
        "【质检修复任务】修复后端实现",
        {
            "backend/app.js": "export const value = 1;",
            "backend/package.json": '{"name":"app"}',
        },
        required_changed_files=["backend/app.js", "backend/package.json"],
    )

    assert result["valid"] is True


def test_fix_validation_ignores_ordinary_english_prose_words(tmp_path):
    agent = make_agent(tmp_path)
    target = tmp_path / "frontend" / "src" / "App.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("const App = () => <div />;\n", encoding="utf-8")

    result = agent._validate_fix_output(
        ["frontend/src/App.tsx"],
        "Fix the frontend function with the existing route implementation",
        {"frontend/src/App.tsx": "const App = () => null;\n"},
        required_changed_files=["frontend/src/App.tsx"],
    )

    assert result["valid"] is True


def test_fix_validation_ignores_truncated_report_marker(tmp_path):
    agent = make_agent(tmp_path)
    target = tmp_path / "backend" / "src" / "controllers" / "assetsController.js"
    target.parent.mkdir(parents=True)
    target.write_text("export async function createAsset() { return true; }\n", encoding="utf-8")

    result = agent._validate_fix_output(
        ["backend/src/controllers/assetsController.js"],
        "Repair the function truncated (the previous response was cut off) and preserve behavior.",
        {
            "backend/src/controllers/assetsController.js": (
                "export async function createAsset() { return false; }\n"
            )
        },
        required_changed_files=["backend/src/controllers/assetsController.js"],
    )

    assert result["valid"] is True


def test_priority_repair_context_keeps_file_tail(tmp_path):
    agent = make_agent(tmp_path)
    target = tmp_path / "backend" / "src" / "large.js"
    target.parent.mkdir(parents=True)
    target.write_text("const row = 1;\n" * 400 + "// TAIL_SENTINEL\n", encoding="utf-8")

    context = agent._read_existing_files(["backend/src/large.js"])

    assert "TAIL_SENTINEL" in context
    assert "[优先修复]" in context


def test_fix_validation_rejects_large_source_truncation(tmp_path):
    agent = make_agent(tmp_path)
    target = tmp_path / "backend" / "src" / "large.js"
    target.parent.mkdir(parents=True)
    previous = "export const row = 1;\n" * 100
    target.write_text("export const row = 2;\n", encoding="utf-8")

    result = agent._validate_fix_output(
        ["backend/src/large.js"],
        "【质检修复任务】修复 backend/src/large.js",
        {"backend/src/large.js": previous},
        required_changed_files=["backend/src/large.js"],
    )

    assert result["valid"] is False
    assert any("疑似响应截断" in issue for issue in result["issues"])


def test_fix_validation_still_checks_explicit_function_declarations(tmp_path):
    agent = make_agent(tmp_path)
    target = tmp_path / "frontend" / "src" / "App.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("const App = () => <div />;\n", encoding="utf-8")

    result = agent._validate_fix_output(
        ["frontend/src/App.tsx"],
        "Implement function renderDashboard()",
        {"frontend/src/App.tsx": "const App = () => null;\n"},
        required_changed_files=["frontend/src/App.tsx"],
    )

    assert result["valid"] is False
    assert "renderDashboard" in result["issues"][0]


def test_fix_validation_rejects_when_every_file_is_unchanged(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["backend/"]
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/app.js").write_text("export const value = 1;", encoding="utf-8")

    result = agent._validate_fix_output(
        ["backend/app.js"],
        "【质检修复任务】修复后端实现",
        {"backend/app.js": "export const value = 1;"},
        required_changed_files=["backend/app.js"],
    )

    assert result["valid"] is False
    assert result["issues"] == ["修复未改动缺陷描述明确指向的任何文件"]


def test_noop_repair_is_deferred_to_phase_qa(tmp_path):
    existing = tmp_path / "backend" / "app.js"
    existing.parent.mkdir(parents=True)
    existing.write_text("export const value = 1;\n", encoding="utf-8")
    payload = '{"files":[{"path":"backend/app.js","content":"export const value = 1;\\n"}]}'
    agent = ExecutionAgent(
        agent_id="agent-noop-repair",
        role="Backend Developer",
        workspace=tmp_path,
        hermes_client=SequencedHermes([payload, payload]),
        allowed_path_prefixes=["backend/"],
    )

    result = agent.execute_task(
        subproject_id="sp-backend",
        subproject_name="Backend",
        description="修复 backend/app.js 中的接口问题",
    )

    assert result["success"] is True
    assert result["validation"]["valid"] is True


def test_repair_prompt_includes_file_ownership_contract(tmp_path):
    existing = tmp_path / "frontend" / "src" / "App.tsx"
    existing.parent.mkdir(parents=True)
    existing.write_text("export default function App() { return null; }\n", encoding="utf-8")
    payload = '{"files":[{"path":"frontend/src/App.tsx","content":"export default function App() { return <main />; }"}]}'
    hermes = SequencedHermes([payload])
    agent = ExecutionAgent(
        agent_id="agent-repair-scope",
        role="Frontend Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["frontend/"],
    )

    result = agent.execute_task(
        subproject_id="sp-frontend",
        subproject_name="Frontend",
        description="【质检修复任务】修复 frontend/src/App.tsx 中的渲染问题",
    )

    system_prompt = hermes.calls[0][0].content
    assert "REPAIR FILE OWNERSHIP CONTRACT" in system_prompt
    assert "frontend/" in system_prompt


def test_fix_validation_does_not_treat_missing_snapshot_as_change(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["backend/"]
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/package.json").write_text('{"name":"app"}', encoding="utf-8")

    result = agent._validate_fix_output(
        ["backend/package.json"],
        "【质检修复任务】修复 backend/package.json",
        {},
        required_changed_files=["backend/package.json"],
    )

    assert result["valid"] is False
    assert result["changed_files"] == []
    assert result["unverified_files"] == ["backend/package.json"]


def test_final_qa_repair_uses_transaction_snapshot_without_file_hint(tmp_path):
    target = tmp_path / "backend" / "app.js"
    target.parent.mkdir(parents=True)
    target.write_text("export const value = 1;", encoding="utf-8")
    payload = json.dumps({
        "files": [{
            "path": "backend/app.js",
            "content": "export const value = 2;",
        }]
    })
    agent = ExecutionAgent(
        agent_id="agent-final-repair",
        role="Backend Developer",
        workspace=tmp_path,
        hermes_client=FakeHermes(payload),
        allowed_path_prefixes=["backend/"],
    )

    result = agent.execute_task(
        subproject_id="sp-backend",
        subproject_name="Backend",
        description="【质检修复任务】修复最终运行验收失败",
    )

    assert result["success"] is True
    assert result["validation"]["changed_files"] == ["backend/app.js"]
    assert result["validation"]["unverified_files"] == []


def test_fix_validation_treats_explicitly_absent_snapshot_as_new_file(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["frontend/"]
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend/tsconfig.json").write_text(
        '{"compilerOptions":{"jsx":"react-jsx"}}',
        encoding="utf-8",
    )

    result = agent._validate_fix_output(
        ["frontend/tsconfig.json"],
        "【质检修复任务】新增 frontend/tsconfig.json",
        {"frontend/tsconfig.json": None},
        required_changed_files=["frontend/tsconfig.json"],
    )

    assert result["valid"] is True
    assert result["changed_files"] == ["frontend/tsconfig.json"]
    assert result["unverified_files"] == []


def test_commonjs_missing_relative_import_is_detected(tmp_path):
    agent = make_agent(tmp_path)
    target = tmp_path / "backend" / "src" / "index.js"
    target.parent.mkdir(parents=True)
    target.write_text("const app = require('./app');\n", encoding="utf-8")

    assert agent._missing_relative_imports(
        "backend/src/index.js", target.read_text(encoding="utf-8")
    ) == ["./app"]


def test_deterministic_express_app_repair_requires_workspace_contract(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["backend/"]
    (tmp_path / "backend" / "src" / "routes").mkdir(parents=True)
    (tmp_path / "backend" / "src" / "routes" / "index.js").write_text(
        "module.exports = require('express').Router();\n", encoding="utf-8"
    )
    (tmp_path / "backend" / "package.json").write_text(
        '{"dependencies":{"express":"^4.18.2","cors":"^2.8.5"}}',
        encoding="utf-8",
    )
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}", encoding="utf-8")

    content = agent._deterministic_missing_repair_content("backend/src/app.js")

    assert content is not None
    assert "app.use('/api', routes);" in content
    assert "express.static(frontendDir)" in content
    assert "module.exports = app;" in content
    assert agent._deterministic_missing_repair_content("backend/src/server.js") is None


def test_repair_can_create_explicitly_authorized_missing_target(tmp_path):
    target = tmp_path / "backend" / "src" / "index.js"
    target.parent.mkdir(parents=True)
    target.write_text("const app = require('./app');\n", encoding="utf-8")
    hermes = SequencedHermes([
        '{"files":[{"path":"backend/src/app.js","content":'
        '"module.exports = { listen() { return true; } };\\n"}]}'
    ])
    agent = ExecutionAgent(
        agent_id="agent-backend-repair",
        role="Backend Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["backend/src/index.js"],
    )

    result = agent.execute_task(
        subproject_id="sp-backend",
        subproject_name="Backend",
        description=(
            "【质检修复任务】\n"
            "Target file: `backend/src/index.js`\n"
            "Related target file: `backend/src/app.js`\n"
            "Create the required module `backend/src/app.js`."
        ),
    )

    assert result["success"] is True, result
    assert (tmp_path / "backend" / "src" / "app.js").is_file()
    assert agent._path_is_allowed("backend/src/unrelated.js") is False
    assert "AUTHORIZED MISSING REPAIR TARGETS" in hermes.calls[0][1].content
    assert "backend/src/app.js" in hermes.calls[0][0].content


def test_fix_validation_requires_explicitly_missing_targets_to_be_created(tmp_path):
    target = tmp_path / "backend" / "src" / "index.js"
    target.parent.mkdir(parents=True)
    target.write_text("const app = require('./app');\n", encoding="utf-8")
    agent = make_agent(tmp_path)

    result = agent._validate_fix_output(
        ["backend/src/index.js"],
        "【质检修复任务】\nRelated target file: `backend/src/app.js`",
        {
            "backend/src/index.js": "const oldApp = require('./app');\n",
            "backend/src/app.js": None,
        },
        required_changed_files=["backend/src/index.js", "backend/src/app.js"],
    )

    assert result["valid"] is False
    assert any("backend/src/app.js" in issue for issue in result["issues"])


def test_repair_uses_precise_continuation_for_omitted_missing_target(tmp_path):
    target = tmp_path / "backend" / "src" / "index.js"
    target.parent.mkdir(parents=True)
    target.write_text("const app = require('./app');\napp.listen(3000);\n", encoding="utf-8")
    index_delivery = json.dumps({"files": [{
        "path": "backend/src/index.js",
        "content": "const app = require('./app');\napp.listen(process.env.PORT || 3000);\n",
    }]})
    (tmp_path / "backend" / "src" / "routes").mkdir()
    (tmp_path / "backend" / "src" / "routes" / "index.js").write_text(
        "module.exports = require('express').Router();\n", encoding="utf-8"
    )
    (tmp_path / "backend" / "package.json").write_text(
        '{"dependencies":{"express":"^4.18.2"}}', encoding="utf-8"
    )
    hermes = SequencedHermes([index_delivery, index_delivery])
    agent = ExecutionAgent(
        agent_id="agent-precise-repair",
        role="Backend Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["backend/"],
    )

    result = agent.execute_task(
        subproject_id="sp-backend",
        subproject_name="Backend",
        description=(
            "【质检修复任务】\n"
            "Target file: `backend/src/index.js`\n"
            "Related target file: `backend/src/app.js`\n"
            "Create the required missing Express application module."
        ),
    )

    assert result["success"] is True, result
    assert (tmp_path / "backend" / "src" / "app.js").is_file()
    assert any("Deterministic missing-file repair validation: passed" in log for log in result["logs"])


def test_explicit_react_typescript_contract_requires_build_entry_files(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["frontend/"]
    agent.required_output_files = [
        "frontend/package.json",
        "frontend/src/main.tsx",
        "frontend/src/App.tsx",
    ]
    agent._write_file(
        "frontend/package.json",
        '{"scripts":{"build":"tsc && vite build"},"dependencies":{"react":"^18.0.0"}}',
    )
    agent._write_file("frontend/src/main.tsx", "import App from './App';")
    agent._write_file("frontend/src/App.tsx", "export default function App(){ return null; }")

    result = agent._validate_new_output(
        "Integrated delivery phase",
        tech_stack=["React", "TypeScript"],
    )

    assert result["valid"] is True, result["issues"]


def test_fix_validation_rejects_unrelated_change_when_target_is_unchanged(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["backend/"]
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/app.js").write_text("export const value = 1;", encoding="utf-8")
    (tmp_path / "backend/util.js").write_text("export const helper = 2;", encoding="utf-8")

    result = agent._validate_fix_output(
        ["backend/app.js", "backend/util.js"],
        "【质检修复任务】修复 backend/app.js",
        {
            "backend/app.js": "export const value = 1;",
            "backend/util.js": "export const helper = 1;",
        },
        required_changed_files=["backend/app.js"],
    )

    assert result["valid"] is False
    assert result["changed_files"] == ["backend/util.js"]
    assert result["issues"] == ["修复未改动缺陷描述明确指向的任何文件"]


def test_mock_only_test_setup_fails_agent_delivery_validation(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["backend/tests/"]
    agent._write_file(
        "backend/tests/setup.ts",
        "import express from 'express';\nconst testApp = express();\ntestApp.get('/api/health', () => {});",
    )

    result = agent._validate_new_output("Create backend integration tests")

    assert result["valid"] is False
    assert any("standalone mock routes" in issue for issue in result["issues"])
    assert any("production src" in issue for issue in result["issues"])


def test_middleware_harness_using_production_source_passes_delivery_validation(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["backend/tests/"]
    (tmp_path / "backend/src/middleware").mkdir(parents=True)
    (tmp_path / "backend/src/middleware/auth.js").write_text(
        "module.exports = { authenticateToken(req, res, next) { next(); } };",
        encoding="utf-8",
    )
    agent._write_file(
        "backend/tests/middleware.test.js",
        "const express = require('express');\n"
        "const { authenticateToken } = require('../src/middleware/auth');\n"
        "const app = express();\n"
        "app.get('/api/protected', authenticateToken, (_req, res) => res.sendStatus(200));\n"
        "test('uses production middleware', () => expect(authenticateToken).toBeDefined());",
    )

    result = agent._validate_new_output("Create backend middleware tests")

    assert result["valid"] is True
    assert result["issues"] == []


def test_environment_only_test_setup_passes_delivery_validation(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["backend/tests/"]
    agent._write_file(
        "backend/tests/setup.js",
        "process.env.NODE_ENV = 'test';\njest.setTimeout(10000);",
    )

    result = agent._validate_new_output("Create backend test setup")

    assert result["valid"] is True
    assert result["issues"] == []


def test_node_test_listener_validation_retries_fixed_port_with_isolated_listener(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "server.js").write_text(
        "module.exports = {};\n", encoding="utf-8"
    )
    bad_test = (
        "const app = require('../src/server');\n"
        "let server;\n"
        "before(() => {\n"
        "  server = app.listen(3000, '127.0.0.1');\n"
        "  server.once('error', () => {});\n"
        "});\n"
    )
    good_test = (
        "const app = require('../src/server');\n"
        "let server;\n"
        "let port;\n"
        "before(() => new Promise((resolve, reject) => {\n"
        "  server = app.listen(0, '127.0.0.1');\n"
        "  server.once('error', reject);\n"
        "  server.once('listening', () => { port = server.address().port; resolve(); });\n"
        "}));\n"
    )
    hermes = SequencedHermes([
        json.dumps({"files": [{"path": "tests/todos.test.js", "content": bad_test}]}),
        json.dumps({"files": [{"path": "tests/todos.test.js", "content": good_test}]}),
    ])
    agent = ExecutionAgent(
        agent_id="agent-node-test-listener",
        role="Backend Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["tests/"],
    )

    result = agent.execute_task(
        subproject_id="sp-tests",
        subproject_name="Node integration tests",
        description="Create Node.js integration tests",
    )

    assert result["success"] is True, result
    assert len(hermes.calls) == 2
    assert "must bind to port 0" in hermes.calls[1][1].content
    assert (tmp_path / "tests" / "todos.test.js").read_text(encoding="utf-8") == good_test


def test_node_test_listener_validation_requires_error_handler(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["tests/"]
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "server.js").write_text(
        "module.exports = {};\n", encoding="utf-8"
    )
    agent._write_file(
        "tests/todos.test.js",
        "const app = require('../src/server');\n"
        "const server = app.listen(0, '127.0.0.1');\n",
    )

    result = agent._validate_new_output("Create Node.js integration tests")

    assert result["valid"] is False
    assert any("must register an error handler" in issue for issue in result["issues"])


def test_missing_relative_import_fails_agent_delivery_validation(tmp_path):
    agent = make_agent(tmp_path)
    agent.allowed_path_prefixes = ["backend/tests/"]
    agent._write_file("backend/tests/setup.ts", "import app from '../backend/src/app';\nexport { app };")

    result = agent._validate_new_output("Create backend integration tests")

    assert result["valid"] is False
    assert any("does not resolve" in issue for issue in result["issues"])


def test_test_repair_context_includes_production_entry(tmp_path):
    agent = make_agent(tmp_path)
    (tmp_path / "backend/tests").mkdir(parents=True)
    (tmp_path / "backend/src").mkdir(parents=True)
    (tmp_path / "backend/tests/setup.ts").write_text("export {};", encoding="utf-8")
    (tmp_path / "backend/src/server.js").write_text("export function createApp() {}", encoding="utf-8")

    context = agent._read_existing_files(["backend/tests/setup.ts"])

    assert "backend/src/server.js [生产入口]" in context
    assert "createApp" in context


def test_fullstack_integration_prompt_includes_existing_project_context(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "backend/src").mkdir(parents=True)
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"react":"^18.0.0"}}', encoding="utf-8"
    )
    (tmp_path / "src/App.tsx").write_text(
        "export default function App() { return <main />; }", encoding="utf-8"
    )
    (tmp_path / "backend/package.json").write_text(
        '{"dependencies":{"express":"^4.0.0"}}', encoding="utf-8"
    )
    (tmp_path / "backend/src/server.js").write_text(
        "export const app = {};", encoding="utf-8"
    )
    hermes = SequencedHermes([
        '{"files":[{"path":"integration/smoke.sh","content":"#!/bin/sh\\nexit 0\\n"}]}'
    ])
    agent = ExecutionAgent(
        agent_id="agent-fullstack",
        role="Full-stack Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["integration/", "README.md", "package.json", "frontend/", "backend/"],
    )

    result = agent.execute_task(
        subproject_id="phase-3",
        subproject_name="Integration and testing",
        description="Complete frontend/backend integration",
    )

    assert result["success"] is True
    system_prompt = hermes.calls[0][0].content
    user_prompt = hermes.calls[0][1].content
    assert "EXISTING PROJECT INTEGRATION CONTRACT" in system_prompt
    assert "package.json [项目清单]" in user_prompt
    assert '"react"' in user_prompt
    assert "src/App.tsx" in user_prompt
    assert "backend/src/server.js" in user_prompt


def test_validation_retry_preserves_previously_accepted_files(tmp_path):
    hermes = SequencedHermes([
        (
            '{"files":['
            '{"path":"backend/package.json","content":"{\\"scripts\\":{\\"test\\":\\"node node_modules/.bin/jest\\"}}"},'
            '{"path":"frontend/src/App.vue","content":"<template><main /></template>"},'
            '{"path":"frontend/src/main.js","content":"import App from \'./App.vue\';\\nexport default App;"}'
            ']}'
        ),
        (
            '{"files":[{"path":"backend/package.json","content":'
            '"{\\"scripts\\":{\\"test\\":\\"jest\\"}}"}]}'
        ),
    ])
    agent = ExecutionAgent(
        agent_id="agent-integration-retry",
        role="Full-stack Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["backend/", "frontend/"],
    )

    result = agent.execute_task(
        subproject_id="phase-3",
        subproject_name="Integration",
        description="Integrate the existing application",
    )

    assert result["success"] is True
    assert set(result["output_files"]) >= {
        "backend/package.json",
        "frontend/src/App.vue",
        "frontend/src/main.js",
    }
    assert (tmp_path / "frontend/src/App.vue").is_file()
    assert len(result["output_files"]) == len(set(result["output_files"]))


def test_existing_root_frontend_is_available_to_fullstack_rerun(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/App.tsx").write_text("export default function App() {}", encoding="utf-8")
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    ctx = SimpleNamespace(
        project_id="legacy-project",
        workspace=tmp_path,
        agents={
            "agent-fullstack": {
                "id": "agent-fullstack",
                "role": "Full-stack Developer",
                "phase_id": "phase-3",
                "allowed_path_prefixes": ["frontend/", "backend/", "integration/", "package.json"],
            }
        },
    )

    agent = routes_execution._make_exec_agent(ctx, "agent-fullstack")

    assert "src/" in agent.allowed_path_prefixes
    assert "index.html" in agent.allowed_path_prefixes
