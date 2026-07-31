import json
import shutil
from pathlib import Path

import pytest

from core import runtime_acceptance
from core.workspace_integrity import (
    compute_delivery_manifest,
    runtime_artifact_matches,
)


def _locked_contract(*criteria: str) -> dict:
    return {
        "locked": True,
        "acceptance_criteria": list(criteria),
        "phases": [],
        "required_files": [
            {
                "path": "package.json",
                "required": True,
                "phase_id": "phase-1",
                "owner_type": "backend",
            }
        ],
    }


def _node_workspace(tmp_path: Path, *, scripts: dict[str, str]) -> Path:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": scripts}),
        encoding="utf-8",
    )
    (tmp_path / "server.js").write_text(
        "console.log('server')\n",
        encoding="utf-8",
    )
    return tmp_path


def test_disabled_remote_runs_local_profile_and_binds_current_manifest(
    monkeypatch,
    tmp_path,
):
    workspace = _node_workspace(
        tmp_path,
        scripts={"start": "node server.js", "test": "jest"},
    )
    contract = _locked_contract(
        "运行 npm test 并通过",
        "启动应用后 GET /api/todos 返回 200",
    )
    observed = {}

    def fake_local(workspace_arg, profile, logs):
        observed["workspace"] = workspace_arg
        observed["package_exists"] = workspace_arg.joinpath(
            "package.json"
        ).is_file()
        observed["profile"] = profile
        logs.append("local profile passed")
        return {"checks": ["test-root", "GET /api/todos"]}

    monkeypatch.setattr(
        runtime_acceptance,
        "_run_local_runtime_profile",
        fake_local,
    )

    result = runtime_acceptance.run_runtime_acceptance(
        workspace,
        "proj-local",
        project_contract=contract,
    )
    manifest = compute_delivery_manifest(workspace)

    assert result["enabled"] is False
    assert result["mode"] == "local"
    assert result["status"] == "passed"
    assert result["passed"] is True
    assert runtime_artifact_matches(result, manifest) is True
    assert observed["workspace"] != workspace
    assert observed["workspace"].name.startswith(
        "metis-runtime-acceptance-"
    )
    assert observed["package_exists"] is True
    assert observed["profile"]["http_checks"] == [
        {
            "method": "GET",
            "path": "/api/todos",
            "expected_statuses": [200],
        }
    ]


def test_profile_is_project_driven_and_has_no_historic_asset_ticket_probes(
    tmp_path,
):
    workspace = _node_workspace(
        tmp_path,
        scripts={
            "start": "node server.js",
            "test": "jest",
            "cypress": "cypress run",
        },
    )

    profile = runtime_acceptance.compile_runtime_profile(
        workspace,
        _locked_contract(
            "运行 npm test 并通过",
            "运行 npm run cypress 并通过",
            "GET /api/todos 返回 200",
        ),
    )

    serialized = json.dumps(profile, sort_keys=True)
    assert "/api/todos" in serialized
    assert "/api/assets" not in serialized
    assert "/api/tickets" not in serialized
    assert profile["e2e_commands"][0]["argv"] == [
        "npm",
        "run",
        "cypress",
    ]


def test_snapshot_validation_does_not_require_dockerfile(monkeypatch, tmp_path):
    workspace = _node_workspace(
        tmp_path,
        scripts={"start": "node server.js"},
    )
    monkeypatch.setenv("RUNTIME_ACCEPTANCE_MAX_FILES", "10")
    monkeypatch.setenv("RUNTIME_ACCEPTANCE_MAX_TOTAL_BYTES", "10000")

    runtime_acceptance._validate_workspace_snapshot(
        {
            "package.json": (workspace / "package.json").read_bytes(),
            "server.js": (workspace / "server.js").read_bytes(),
        },
        type("Config", (), {"max_files": 10, "max_total_bytes": 10000})(),
    )


def test_remote_profile_checks_only_declared_contract_endpoint(
    monkeypatch,
    tmp_path,
):
    workspace = _node_workspace(
        tmp_path,
        scripts={"start": "node server.js"},
    )
    contract = _locked_contract("GET /api/todos 返回 200")
    calls = []

    class Response:
        status_code = 200

    class Session:
        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return Response()

    profile = runtime_acceptance.compile_runtime_profile(workspace, contract)
    evidence = runtime_acceptance._run_remote_profile_http_checks(
        Session(),
        "https://generated.example",
        type("Config", (), {"request_timeout": 1.0, "health_timeout": 0.0, "poll_interval": 0.0})(),
        profile,
        [],
    )

    assert evidence["passed"] is True
    assert [(method, url) for method, url, _ in calls] == [
        ("GET", "https://generated.example/api/todos")
    ]


def test_runtime_profile_compiles_declared_post_example_body(tmp_path):
    workspace = _node_workspace(
        tmp_path,
        scripts={"start": "node server.js"},
    )
    contract = _locked_contract(
        "POST /api/todos with valid {title:'...'} returns 201 JSON",
    )

    profile = runtime_acceptance.compile_runtime_profile(
        workspace,
        contract,
    )

    assert profile["http_checks"] == [{
        "method": "POST",
        "path": "/api/todos",
        "expected_statuses": [201],
        "body": {"title": "metis-runtime-check"},
    }]


def test_runtime_profile_preserves_same_endpoint_scenarios(tmp_path):
    workspace = _node_workspace(
        tmp_path,
        scripts={"start": "node server.js"},
    )
    contract = _locked_contract()
    contract["acceptance_criteria"] = [
        {
            "criterion": "POST /api/todos returns 201",
            "evidence_spec": {
                "check_id": "valid",
                "body": {"title": "acceptance"},
            },
        },
        {
            "criterion": "POST /api/todos returns 400",
            "evidence_spec": {"check_id": "invalid", "body": {}},
        },
    ]

    profile = runtime_acceptance.compile_runtime_profile(workspace, contract)

    assert profile["http_checks"] == [
        {
            "check_id": "valid",
            "method": "POST",
            "path": "/api/todos",
            "expected_statuses": [201],
            "body": {"title": "acceptance"},
        },
        {
            "check_id": "invalid",
            "method": "POST",
            "path": "/api/todos",
            "expected_statuses": [400],
        },
    ]


def test_runtime_profile_preserves_duplicate_endpoint_for_distinct_criteria(tmp_path):
    workspace = _node_workspace(
        tmp_path,
        scripts={"start": "node server.js"},
    )
    contract = _locked_contract()
    contract["acceptance_criteria"] = [
        {
            "criterion": "GET /health returns 200 JSON",
            "evidence_spec": {"check_id": "task-health"},
        },
        {
            "criterion": "GET /health returns 200 JSON",
            "evidence_spec": {"check_id": "phase-health"},
        },
    ]

    profile = runtime_acceptance.compile_runtime_profile(workspace, contract)

    assert profile["http_checks"] == [
        {
            "check_id": "task-health",
            "method": "GET",
            "path": "/health",
            "expected_statuses": [200],
        },
        {
            "check_id": "phase-health",
            "method": "GET",
            "path": "/health",
            "expected_statuses": [200],
        },
    ]


def test_runtime_profile_preserves_distinct_check_ids_for_identical_get_probes(tmp_path):
    workspace = _node_workspace(tmp_path, scripts={"start": "node server.js"})
    contract = _locked_contract()
    contract["acceptance_criteria"] = [
        {
            "criterion": "GET /health returns 200 JSON",
            "evidence_spec": {"check_id": "health-status"},
        },
        {
            "criterion": "GET /health returns 200 JSON",
            "evidence_spec": {"check_id": "health-schema"},
        },
    ]

    profile = runtime_acceptance.compile_runtime_profile(workspace, contract)

    assert profile["http_checks"] == [
        {
            "check_id": "health-status",
            "method": "GET",
            "path": "/health",
            "expected_statuses": [200],
        },
        {
            "check_id": "health-schema",
            "method": "GET",
            "path": "/health",
            "expected_statuses": [200],
        },
    ]


def test_runtime_profile_reads_status_after_long_endpoint_description(tmp_path):
    workspace = _node_workspace(
        tmp_path,
        scripts={"start": "node server.js"},
    )
    contract = _locked_contract(
        "POST /api/todos accepts a JSON request body containing a required title "
        "field, validates that the title is not blank, persists the new record, "
        "and returns the created todo with its generated identifier and timestamps "
        "(201) or a validation error (400).",
    )

    profile = runtime_acceptance.compile_runtime_profile(
        workspace,
        contract,
    )

    assert profile["http_checks"] == [{
        "method": "POST",
        "path": "/api/todos",
        "expected_statuses": [201],
        "body": {"title": "metis-runtime-check"},
    }]


def test_runtime_profile_reads_status_from_multiline_readme_section(tmp_path):
    workspace = _node_workspace(
        tmp_path,
        scripts={"start": "node server.js"},
    )
    contract = _locked_contract(
        "### POST /api/todos\n\n"
        "**Request body**: JSON containing a required title.\n\n"
        "**Success response (201)**: the created todo.\n\n"
        "**Error response (400)**: invalid input.",
    )

    profile = runtime_acceptance.compile_runtime_profile(
        workspace,
        contract,
    )

    assert profile["http_checks"] == [{
        "method": "POST",
        "path": "/api/todos",
        "expected_statuses": [201],
        "body": {"title": "metis-runtime-check"},
    }]


@pytest.mark.skipif(
    shutil.which("node") is None or shutil.which("npm") is None,
    reason="Node.js runtime is unavailable",
)
def test_local_runtime_executes_real_start_test_and_http_in_isolated_snapshot(
    tmp_path,
):
    workspace = _node_workspace(
        tmp_path,
        scripts={
            "start": "node server.js",
            "test": "node -e \"process.exit(0)\"",
        },
    )
    (workspace / "server.js").write_text(
        "const http=require('http');"
        "const port=Number(process.env.PORT);"
        "http.createServer((req,res)=>{"
        "res.statusCode=req.url==='/health'?200:404;"
        "res.end('ok');"
        "}).listen(port,'127.0.0.1');\n",
        encoding="utf-8",
    )
    contract = _locked_contract(
        "运行 npm test 并通过",
        "启动应用后 GET /health 返回 200",
    )

    result = runtime_acceptance.run_runtime_acceptance(
        workspace,
        "proj-real-local",
        project_contract=contract,
    )

    assert result["passed"] is True
    assert result["mode"] == "local"
    assert result["checks"]
    assert runtime_artifact_matches(
        result, compute_delivery_manifest(workspace)
    )
    assert not (workspace / "node_modules").exists()
    assert not (workspace / "package-lock.json").exists()


def test_contract_http_checks_bind_created_id_and_missing_id():
    checks = runtime_acceptance._contract_http_checks([
        "POST /tasks 发送有效请求体返回 201，任务包含 id 和 title。",
        "PUT /tasks/:id 发送有效请求体返回 200。",
        "PUT /tasks/:id 更新不存在的任务返回 404。",
    ])

    assert checks[1]["path_params"] == {"id": {"capture": "id"}}
    assert checks[2]["path_params"] == {
        "id": {"literal": "metis-missing-id"}
    }


def test_contract_http_checks_build_valid_body_from_sibling_validation_rule():
    checks = runtime_acceptance._contract_http_checks([
        "POST /tasks 发送合法请求体返回 201 及任务对象。",
        "POST /tasks 发送缺少 title 的请求体返回 400。",
    ])

    assert checks[0]["expected_statuses"] == [201]
    assert checks[0]["body"] == {"title": "metis-runtime-check"}
    assert "body" not in checks[1]


def test_contract_http_checks_do_not_leak_fields_between_endpoints():
    checks = runtime_acceptance._contract_http_checks([
        "POST /users 发送合法请求体返回 201。",
        "POST /tasks 缺少 title 返回 400。",
    ])

    assert "body" not in checks[0]


def test_contract_http_checks_recognize_chinese_missing_resource_cue():
    checks = runtime_acceptance._contract_http_checks([
        "PUT /tasks/:id 更新找不到的任务返回 404。",
    ])

    assert checks[0]["path_params"] == {
        "id": {"literal": "metis-missing-id"}
    }


def test_runtime_http_checks_reuse_created_resource_for_put():
    requests_seen = []

    class Response:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload

        def json(self):
            return self._payload

    class Session:
        def request(self, method, url, **kwargs):
            requests_seen.append((method, url, kwargs.get("json")))
            if method == "POST":
                return Response(201, {"id": "task-1", "title": "created"})
            return Response(200, {"id": "task-1", "title": "created"})

    config = type("Config", (), {
        "request_timeout": 1,
        "health_timeout": 1,
        "poll_interval": 0,
    })()
    profile = {"http_checks": [
        {
            "method": "POST",
            "path": "/tasks",
            "expected_statuses": [201],
            "body": {"title": "created"},
        },
        {
            "method": "PUT",
            "path": "/tasks/:id",
            "expected_statuses": [200],
            "path_params": {"id": {"capture": "id"}},
        },
    ]}

    result = runtime_acceptance._run_remote_profile_http_checks(
        Session(), "http://127.0.0.1:1", config, profile, []
    )

    assert result["passed"] is True
    assert requests_seen[1] == (
        "PUT",
        "http://127.0.0.1:1/tasks/task-1",
        {"title": "created"},
    )


def test_runtime_http_failure_reports_actionable_request_and_response():
    class Response:
        status_code = 400

        @staticmethod
        def json():
            return {"error": "description is required"}

    class Session:
        @staticmethod
        def request(*_args, **_kwargs):
            return Response()

    config = type("Config", (), {
        "request_timeout": 1,
        "health_timeout": 1,
        "poll_interval": 0,
    })()

    with pytest.raises(
        runtime_acceptance.RuntimeAcceptanceError,
        match=(
            r"expected \[201\], got 400; "
            r"request_body_keys=\['title'\].*description is required"
        ),
    ):
        runtime_acceptance._run_remote_profile_http_checks(
            Session(),
            "http://127.0.0.1:1",
            config,
            {"http_checks": [{
                "method": "POST",
                "path": "/tasks",
                "expected_statuses": [201],
                "body": {"title": "created"},
            }]},
            [],
        )


def test_missing_resource_failure_tells_repair_agent_to_check_existence_first():
    class Response:
        status_code = 400

        @staticmethod
        def json():
            return {"errors": ["title is required"]}

    class Session:
        @staticmethod
        def request(*_args, **_kwargs):
            return Response()

    config = type("Config", (), {
        "request_timeout": 1,
        "health_timeout": 1,
        "poll_interval": 0,
    })()

    with pytest.raises(
        runtime_acceptance.RuntimeAcceptanceError,
        match=(
            r"scenario=missing_resource.*"
            r"required_fix=check whether the resource exists before "
            r"validating the update body"
        ),
    ):
        runtime_acceptance._run_remote_profile_http_checks(
            Session(),
            "http://127.0.0.1:1",
            config,
            {"http_checks": [{
                "method": "PUT",
                "path": "/tasks/:id",
                "expected_statuses": [404],
                "path_params": {"id": {"literal": "metis-missing-id"}},
            }]},
            [],
        )


def test_runtime_http_checks_cover_full_stateful_task_contract():
    tasks = {}
    next_id = 1
    requests_seen = []

    class Response:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload

        def json(self):
            return self._payload

    class Session:
        def request(self, method, url, **kwargs):
            nonlocal next_id
            path = url.split("http://127.0.0.1:1", 1)[1]
            body = kwargs.get("json")
            requests_seen.append((method, path, body))
            if method == "POST":
                if not body or not body.get("title"):
                    return Response(400, {"error": "title required"})
                task = {"id": next_id, **body}
                tasks[next_id] = task
                next_id += 1
                return Response(201, task)
            raw_id = path.rsplit("/", 1)[1]
            if not raw_id.isdigit():
                return Response(404, {"error": "not found"})
            task_id = int(raw_id)
            if task_id not in tasks:
                return Response(404, {"error": "not found"})
            if not body:
                return Response(400, {"error": "empty update"})
            tasks[task_id].update(body)
            return Response(200, tasks[task_id])

    criteria = [
        "POST /tasks 有效请求体包含 title，返回 201。",
        "POST /tasks 缺少 title，返回 400。",
        "PUT /tasks/:id 有效请求体更新已存在任务，返回 200。",
        "PUT /tasks/:id 空请求体返回 400。",
        "PUT /tasks/:id 更新不存在的任务返回 404。",
    ]
    profile = {
        "http_checks": runtime_acceptance._contract_http_checks(criteria)
    }
    config = type("Config", (), {
        "request_timeout": 1,
        "health_timeout": 1,
        "poll_interval": 0,
    })()

    result = runtime_acceptance._run_remote_profile_http_checks(
        Session(), "http://127.0.0.1:1", config, profile, []
    )

    assert result["passed"] is True
    assert [status for status in result["checks"]] == [
        "POST /tasks",
        "POST /tasks",
        "PUT /tasks/1",
        "PUT /tasks/1",
        "PUT /tasks/metis-missing-id",
    ]
    assert requests_seen[2][2] == {"title": "metis-runtime-check"}
    assert requests_seen[3][2] is None


def test_local_runtime_ignores_declared_production_port(monkeypatch, tmp_path):
    workspace = _node_workspace(
        tmp_path,
        scripts={"start": "node server.js"},
    )
    observed = {}

    class Process:
        pid = 18437

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    def fake_http(_session, service_url, _config, _profile, _logs):
        observed["service_url"] = service_url
        return {"checks": [], "observations": []}

    monkeypatch.setattr(runtime_acceptance, "_free_loopback_port", lambda: 18437)
    monkeypatch.setattr(runtime_acceptance, "_run_remote_profile_http_checks", fake_http)
    monkeypatch.setattr(runtime_acceptance.subprocess, "Popen", lambda *args, **kwargs: Process())

    result = runtime_acceptance._run_local_runtime_profile(
        workspace,
        {
            "commands": [],
            "start": {"argv": ["node", "server.js"], "cwd": "."},
            "http_checks": [{"method": "GET", "path": "/health", "expected_statuses": [200]}],
            "e2e_commands": [],
            "port": 3000,
        },
        [],
    )

    assert result["port"] == 18437
    assert observed["service_url"] == "http://127.0.0.1:18437"
