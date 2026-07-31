import base64
from pathlib import Path

import pytest

from core import runtime_acceptance


class FakeResponse:
    def __init__(self, status_code, body=None, *, invalid_json=False):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.invalid_json = invalid_json

    def json(self):
        if self.invalid_json:
            raise ValueError("empty response")
        return self._body

    @property
    def text(self):
        return str(self._body)


@pytest.mark.parametrize("case_index", range(1200))
def test_runtime_stack_location_and_fix_instruction_are_precise(case_index):
    line = 7 + case_index
    relative = (
        f"routes/tasks-{case_index}.js"
        if case_index % 5 < 3
        else f"src/handlers/task-{case_index}.ts"
    )
    separator = "\\" if case_index % 2 else "/"
    absolute = (
        f"C:{separator}tmp{separator}runtime{separator}"
        + relative.replace("/", separator)
    )
    message = (
        "TypeError: Cannot destructure property 'title' of 'req.body' "
        f"as it is undefined\n    at {absolute}:{line}:11"
    )

    path, observed_line = runtime_acceptance._http_failure_location(message)
    hint = runtime_acceptance._http_failure_fix_hint(message, "generic")

    assert path == relative
    assert observed_line == line
    assert "express.json()" in hint
    assert "before reading req.body" in hint


def test_http_500_preserves_status_response_source_and_exact_fix():
    class FailedSession:
        def request(self, *_args, **_kwargs):
            return FakeResponse(
                500,
                "TypeError: Cannot destructure property 'title' of "
                "'req.body' as it is undefined\n"
                "    at C:\\tmp\\app\\routes\\tasks.js:9:11",
                invalid_json=True,
            )

    config = runtime_acceptance._Config(
        github_token="", github_repository="", github_branch="",
        github_base_branch="", github_api_url="", render_api_key="",
        render_service_id="", render_service_url="", render_api_url="",
        max_files=10, max_total_bytes=1000, request_timeout=1,
        deploy_timeout=0, health_timeout=0, poll_interval=0,
    )
    profile = {"http_checks": [{
        "check_id": "create-task",
        "method": "POST",
        "path": "/tasks",
        "body": {"title": "test"},
        "expected_statuses": [201],
    }]}

    with pytest.raises(runtime_acceptance.RuntimeAcceptanceError) as caught:
        runtime_acceptance._run_remote_profile_http_checks(
            FailedSession(), "http://127.0.0.1:1", config, profile, [],
        )

    error = caught.value
    assert "expected [201], got 500" in str(error)
    assert "Cannot destructure property" in str(error)
    assert error.file_path == "routes/tasks.js"
    assert "express.json()" in error.fix_hint


class FakeSession:
    def __init__(self, deploy_statuses=("building", "live"), health_status=200, root_status=200, empty_deploy_202=False):
        self.calls = []
        self.deploy_statuses = list(deploy_statuses)
        self.health_status = health_status
        self.root_status = root_status
        self.empty_deploy_202 = empty_deploy_202
        self.blob_number = 0
        self.assets = []
        self.tickets = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/git/ref/heads/metis-runtime-acceptance"):
            return FakeResponse(404)
        if url.endswith("/git/ref/heads/develop"):
            return FakeResponse(200, {"object": {"sha": "base-sha"}})
        if url.endswith("/git/blobs"):
            self.blob_number += 1
            return FakeResponse(201, {"sha": f"blob-{self.blob_number}"})
        if url.endswith("/git/trees"):
            return FakeResponse(201, {"sha": "tree-sha"})
        if url.endswith("/git/commits"):
            return FakeResponse(201, {"sha": "commit-sha"})
        if url.endswith("/git/refs"):
            return FakeResponse(201, {"ref": "refs/heads/metis-runtime-acceptance"})
        if url.endswith("/services/srv-test/deploys") and method == "POST":
            if self.empty_deploy_202:
                return FakeResponse(202, invalid_json=True)
            return FakeResponse(202, {"id": "dep-test", "status": "created"})
        if url.endswith("/services/srv-test/deploys?limit=20") and method == "GET":
            return FakeResponse(200, [{
                "deploy": {"id": "dep-test", "status": "created", "commit": {"id": "commit-sha"}}
            }])
        if url.endswith("/services/srv-test/deploys/dep-test"):
            status = self.deploy_statuses.pop(0)
            return FakeResponse(200, {"id": "dep-test", "status": status})
        if url.endswith("/services/srv-test"):
            return FakeResponse(
                200,
                {
                    "ownerId": "tea-test",
                    "serviceDetails": {"url": "https://acceptance.example.test"},
                },
            )
        if "/logs?" in url:
            return FakeResponse(200, {"logs": [
                {"message": "\x1b[31mTest Suites: 2 failed\x1b[0m"},
                {"message": "render-secret must never be exposed"},
                {"message": "npm test exited with code 1"},
                {"message": "TypeError: req.end is not a function"},
                {"message": "at check (file:///app/integration/start.js:59:11)"},
            ]})
        if url.endswith(("/api/health", "/health")):
            return FakeResponse(self.health_status, {"ok": True})
        if url == "https://acceptance.example.test/":
            return FakeResponse(self.root_status, "<!doctype html>")
        if url.endswith("/api/auth/register"):
            return FakeResponse(201, {"id": 1})
        if url.endswith("/api/auth/login"):
            email = str((kwargs.get("json") or {}).get("email") or "")
            token = "secondary-token" if "secondary" in email else "test-token"
            return FakeResponse(200, {"access_token": token})
        if url.endswith("/api/auth/me"):
            return FakeResponse(200, {"id": 1})
        if url.endswith("/api/assets") and method == "POST":
            code = kwargs["json"].get("assetCode") or kwargs["json"].get("code")
            if any((item.get("assetCode") or item.get("code")) == code for item in self.assets):
                return FakeResponse(409, {"error": "duplicate asset code"})
            item = {**kwargs["json"], "id": 7}
            self.assets.append(item)
            return FakeResponse(201, item)
        if url.endswith("/api/assets") and method == "GET":
            if (kwargs.get("headers") or {}).get("Authorization") == "Bearer secondary-token":
                return FakeResponse(200, {"assets": []})
            return FakeResponse(200, {"assets": self.assets})
        if url.endswith("/api/tickets") and method == "POST":
            item = {**kwargs["json"], "id": 11}
            self.tickets.append(item)
            return FakeResponse(201, item)
        if url.endswith("/api/tickets") and method == "GET":
            if (kwargs.get("headers") or {}).get("Authorization") == "Bearer secondary-token":
                return FakeResponse(200, {"tickets": []})
            return FakeResponse(200, {"tickets": self.tickets})
        if url.endswith("/api/tickets/11/comments"):
            return FakeResponse(201, {"id": 21})
        if url.endswith("/api/assets/7") and method == "DELETE":
            return FakeResponse(409, {"error": "asset is in use"})
        if url.endswith("/api/dashboard/stats"):
            return FakeResponse(200, {"assets": 1, "tickets": 1})
        raise AssertionError(f"unexpected request: {method} {url}")


@pytest.fixture(autouse=True)
def clean_runtime_env(monkeypatch):
    for name in list(runtime_acceptance.os.environ):
        if name.startswith("RUNTIME_ACCEPTANCE_"):
            monkeypatch.delenv(name, raising=False)


def configure(monkeypatch):
    values = {
        "RUNTIME_ACCEPTANCE_ENABLED": "true",
        "RUNTIME_ACCEPTANCE_GITHUB_TOKEN": "github-secret",
        "RUNTIME_ACCEPTANCE_GITHUB_REPOSITORY": "owner/repository",
        "RUNTIME_ACCEPTANCE_RENDER_API_KEY": "render-secret",
        "RUNTIME_ACCEPTANCE_RENDER_SERVICE_ID": "srv-test",
        "RUNTIME_ACCEPTANCE_DEPLOY_TIMEOUT": "1",
        "RUNTIME_ACCEPTANCE_HEALTH_TIMEOUT": "0",
        "RUNTIME_ACCEPTANCE_POLL_INTERVAL": "0",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def make_workspace(tmp_path: Path) -> Path:
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / "package.json").write_text(
        '{"scripts":{"start":"node server.js"}}',
        encoding="utf-8",
    )
    (tmp_path / "backend" / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "frontend" / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "Runtime acceptance requires GET /api/health returns 200.\n",
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile").write_bytes(
        b"FROM node:20-alpine\nCMD [\"npm\", \"start\"]\n",
    )
    (tmp_path / ".env").write_text("SECRET=never-push\n", encoding="utf-8")
    (tmp_path / "state.sqlite3").write_bytes(b"private database")
    (tmp_path / "node_modules" / "secret.txt").write_text("excluded", encoding="utf-8")
    (tmp_path / ".git" / "config").write_text("excluded", encoding="utf-8")
    return tmp_path


def test_disabled_without_a_runtime_profile_fails_closed(tmp_path):
    result = runtime_acceptance.run_runtime_acceptance(tmp_path, "proj-disabled")

    assert result["enabled"] is False
    assert result["mode"] == "local"
    assert result["passed"] is False
    assert result["status"] == "unsupported_runtime_profile"


def test_enabled_missing_configuration_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNTIME_ACCEPTANCE_ENABLED", "1")

    result = runtime_acceptance.run_runtime_acceptance(tmp_path, "proj-missing")

    assert result["enabled"] is True
    assert result["passed"] is False
    assert result["status"] == "infrastructure_blocked"
    assert result["error_category"] == "environment_misconfigured"
    assert "RUNTIME_ACCEPTANCE_GITHUB_TOKEN" in result["summary"]


def test_pushes_clean_snapshot_deploys_commit_and_checks_health(monkeypatch, tmp_path):
    configure(monkeypatch)
    workspace = make_workspace(tmp_path)
    fake = FakeSession()
    monkeypatch.setattr(runtime_acceptance.requests, "Session", lambda: fake)

    result = runtime_acceptance.run_runtime_acceptance(workspace, "proj-123")

    assert result["passed"] is True
    assert result["status"] == "passed"
    assert result["commit_sha"] == "commit-sha"
    assert result["deploy_id"] == "dep-test"
    assert result["service_url"] == "https://acceptance.example.test"

    blob_payloads = [
        call[2]["json"]
        for call in fake.calls
        if call[1].endswith("/git/blobs")
    ]
    assert len(blob_payloads) == 5
    decoded_blobs = [base64.b64decode(item["content"]) for item in blob_payloads]
    dockerfile = next(blob for blob in decoded_blobs if blob.startswith(b"FROM node:20"))
    assert dockerfile == b'FROM node:20-alpine\nCMD ["npm", "start"]\n'
    assert b'CMD ["npm", "start"]' in dockerfile
    assert b"never-push" not in decoded_blobs
    assert b"private database" not in decoded_blobs

    tree_call = next(call for call in fake.calls if call[1].endswith("/git/trees"))
    paths = {item["path"] for item in tree_call[2]["json"]["tree"]}
    assert paths == {
        "Dockerfile",
        "README.md",
        "package.json",
        "backend/package.json",
        "frontend/package.json",
    }
    deploy_call = next(
        call
        for call in fake.calls
        if call[0] == "POST" and call[1].endswith("/services/srv-test/deploys")
    )
    assert deploy_call[2]["json"]["commitId"] == "commit-sha"
    serialized_calls = repr(fake.calls)
    assert "github-secret" in serialized_calls  # Sent only in the private request headers.
    assert "github-secret" not in repr(result)
    assert "render-secret" not in repr(result)


def test_preserves_python_release_dockerfile(monkeypatch, tmp_path):
    configure(monkeypatch)
    dockerfile = (
        b"FROM python:3.12-slim\n"
        b"COPY . /app\n"
        b"CMD [\"uvicorn\", \"backend.main:app\", \"--host\", \"0.0.0.0\"]\n"
    )
    workspace = make_workspace(tmp_path)
    (workspace / "Dockerfile").write_bytes(dockerfile)

    files = runtime_acceptance._workspace_snapshot(
        workspace,
        runtime_acceptance._load_config(),
    )

    assert files["Dockerfile"] == dockerfile


def test_missing_release_dockerfile_is_allowed_when_contract_does_not_require_it(
    monkeypatch,
    tmp_path,
):
    configure(monkeypatch)
    workspace = make_workspace(tmp_path)
    (workspace / "Dockerfile").unlink()
    called = False

    def session_factory():
        nonlocal called
        called = True
        return FakeSession()

    monkeypatch.setattr(runtime_acceptance.requests, "Session", session_factory)
    result = runtime_acceptance.run_runtime_acceptance(
        workspace, "proj-missing-dockerfile"
    )

    assert result["passed"] is True
    assert called is True


def test_snapshot_limit_fails_before_any_network_call(monkeypatch, tmp_path):
    configure(monkeypatch)
    monkeypatch.setenv("RUNTIME_ACCEPTANCE_MAX_FILES", "1")
    workspace = make_workspace(tmp_path)
    called = False

    def session_factory():
        nonlocal called
        called = True
        return FakeSession()

    monkeypatch.setattr(runtime_acceptance.requests, "Session", session_factory)
    result = runtime_acceptance.run_runtime_acceptance(workspace, "proj-limit")

    assert result["passed"] is False
    assert "file limit" in result["summary"]
    assert called is False


def test_render_build_failure_is_not_a_pass(monkeypatch, tmp_path):
    configure(monkeypatch)
    fake = FakeSession(deploy_statuses=("build_failed",))
    monkeypatch.setattr(runtime_acceptance.requests, "Session", lambda: fake)

    result = runtime_acceptance.run_runtime_acceptance(
        make_workspace(tmp_path), "proj-build-failure"
    )

    assert result["passed"] is False
    assert result["status"] == "build_failed"
    assert result["deploy_id"] == "dep-test"
    assert "build: Test Suites: 2 failed" in result["logs"]
    assert "build: npm test exited with code 1" in result["logs"]
    assert "build: TypeError: req.end is not a function" in result["logs"]
    assert any("integration/start.js:59:11" in line for line in result["logs"])
    log_calls = [call for call in fake.calls if "/logs?" in call[1]]
    assert log_calls and "type=build" not in log_calls[0][1]
    assert "render-secret" not in repr(result)
    assert not any(call[1].endswith("/api/health") for call in fake.calls)


def test_render_startup_failure_with_application_error_is_actionable(monkeypatch, tmp_path):
    configure(monkeypatch)
    fake = FakeSession(deploy_statuses=("update_failed",))
    monkeypatch.setattr(runtime_acceptance.requests, "Session", lambda: fake)

    result = runtime_acceptance.run_runtime_acceptance(
        make_workspace(tmp_path), "proj-startup-failure"
    )

    assert result["status"] == "update_failed"
    assert result["error_category"] == "project_defect"
    assert result["actionable"] is True
    assert result["retryable"] is False
    assert "startTime=" in next(call[1] for call in fake.calls if "/logs?" in call[1])


def test_live_deploy_with_failed_health_is_not_a_pass(monkeypatch, tmp_path):
    configure(monkeypatch)
    fake = FakeSession(deploy_statuses=("live",), health_status=503)
    monkeypatch.setattr(runtime_acceptance.requests, "Session", lambda: fake)

    result = runtime_acceptance.run_runtime_acceptance(
        make_workspace(tmp_path), "proj-health-failure"
    )

    assert result["passed"] is False
    assert result["status"] == "runtime_check_failed"
    assert result["service_url"] == "https://acceptance.example.test"
    assert "runtime HTTP check GET /api/health failed" in result["logs"]


def test_empty_202_deploy_response_is_recovered_by_commit(monkeypatch, tmp_path):
    configure(monkeypatch)
    fake = FakeSession(deploy_statuses=("live",), empty_deploy_202=True)
    monkeypatch.setattr(runtime_acceptance.requests, "Session", lambda: fake)

    result = runtime_acceptance.run_runtime_acceptance(
        make_workspace(tmp_path), "proj-async-deploy"
    )

    assert result["passed"] is True
    assert result["deploy_id"] == "dep-test"
    assert "Render accepted deploy recovered: dep-test" in result["logs"]


def test_remote_runtime_does_not_invent_an_undeclared_frontend_root_check(
    monkeypatch,
    tmp_path,
):
    configure(monkeypatch)
    fake = FakeSession(deploy_statuses=("live",), root_status=404)
    monkeypatch.setattr(runtime_acceptance.requests, "Session", lambda: fake)
    workspace = make_workspace(tmp_path)
    (workspace / "integration").mkdir()
    (workspace / "integration" / "start.js").write_text("// entrypoint\n", encoding="utf-8")

    result = runtime_acceptance.run_runtime_acceptance(
        workspace, "proj-frontend-failure"
    )

    assert result["passed"] is True
    assert not any(
        url == "https://acceptance.example.test/"
        for _method, url, _kwargs in fake.calls
    )


def test_unexpected_provider_error_fails_without_leaking_exception(monkeypatch, tmp_path):
    configure(monkeypatch)

    class BrokenSession(FakeSession):
        def request(self, method, url, **kwargs):
            raise RuntimeError("provider echoed render-secret")

    monkeypatch.setattr(runtime_acceptance.requests, "Session", BrokenSession)

    result = runtime_acceptance.run_runtime_acceptance(
        make_workspace(tmp_path), "proj-provider-error"
    )

    assert result["passed"] is False
    assert result["status"] == "infrastructure_blocked"
    assert result["error_category"] == "infrastructure_provider_error"
    assert "unexpected provider error" in result["summary"]
    assert "render-secret" not in repr(result)


@pytest.mark.parametrize(
    ("status_code", "category", "retryable"),
    [
        (404, "infrastructure_unavailable", False),
        (429, "infrastructure_transient", True),
        (503, "infrastructure_transient", True),
    ],
)
def test_preflight_classifies_provider_failures(
    monkeypatch, status_code, category, retryable
):
    configure(monkeypatch)

    class PreflightFailure(FakeSession):
        def request(self, method, url, **kwargs):
            if url.endswith("/services/srv-test"):
                return FakeResponse(status_code, {})
            return super().request(method, url, **kwargs)

    monkeypatch.setattr(runtime_acceptance.requests, "Session", PreflightFailure)

    result = runtime_acceptance.preflight_runtime_acceptance()

    assert result["status"] == "infrastructure_blocked"
    assert result["error_category"] == category
    assert result["retryable"] is retryable


def test_passed_artifact_is_reused_without_network(monkeypatch, tmp_path):
    configure(monkeypatch)
    workspace = make_workspace(tmp_path)
    fake = FakeSession(deploy_statuses=("live",))
    monkeypatch.setattr(runtime_acceptance.requests, "Session", lambda: fake)
    first = runtime_acceptance.run_runtime_acceptance(workspace, "proj-cache")
    assert first["passed"] is True

    def no_network():
        raise AssertionError("cached artifact must not access providers")

    monkeypatch.setattr(runtime_acceptance.requests, "Session", no_network)
    second = runtime_acceptance.run_runtime_acceptance(
        workspace, "proj-cache", first
    )

    assert second["passed"] is True
    assert second["cached"] is True
    assert second["artifact_sha256"] == first["artifact_sha256"]
    assert second["rule_version"] == first["rule_version"]


def test_runtime_does_not_invent_an_undeclared_auth_flow(monkeypatch, tmp_path):
    configure(monkeypatch)

    class MissingAuthMe(FakeSession):
        def request(self, method, url, **kwargs):
            if url.endswith("/api/auth/me"):
                return FakeResponse(404, {})
            return super().request(method, url, **kwargs)

    fake = MissingAuthMe()
    monkeypatch.setattr(runtime_acceptance.requests, "Session", lambda: fake)
    result = runtime_acceptance.run_runtime_acceptance(
        make_workspace(tmp_path), "proj-contract"
    )

    assert result["passed"] is True
    assert not any(
        "/api/auth/" in url for _method, url, _kwargs in fake.calls
    )


def test_runtime_uses_only_project_declared_http_checks(monkeypatch, tmp_path):
    configure(monkeypatch)
    fake = FakeSession(deploy_statuses=("live",))
    monkeypatch.setattr(runtime_acceptance.requests, "Session", lambda: fake)

    result = runtime_acceptance.run_runtime_acceptance(
        make_workspace(tmp_path), "proj-identity"
    )

    assert result["passed"] is True
    runtime_calls = [
        (method, url)
        for method, url, _kwargs in fake.calls
        if url.startswith("https://acceptance.example.test")
    ]
    assert runtime_calls == [
        ("GET", "https://acceptance.example.test/api/health")
    ]


def test_declared_contract_endpoint_failure_is_actionable(monkeypatch, tmp_path):
    configure(monkeypatch)

    class MissingDashboard(FakeSession):
        def request(self, method, url, **kwargs):
            if url.endswith("/api/dashboard/stats"):
                return FakeResponse(404, {})
            return super().request(method, url, **kwargs)

    monkeypatch.setattr(runtime_acceptance.requests, "Session", MissingDashboard)
    result = runtime_acceptance.run_runtime_acceptance(
        make_workspace(tmp_path),
        "proj-dashboard-contract",
        project_contract={
            "locked": True,
            "acceptance_criteria": ["GET /api/dashboard/stats returns 200"],
            "phases": [],
        },
    )

    assert result["passed"] is False
    assert result["error_category"] == "project_defect"
    assert result["status"] == "runtime_check_failed"
    assert "/api/dashboard/stats" in result["summary"]
    assert "file_path" not in result


def test_render_startup_syntax_stack_is_classified_as_project_failure():
    logs = [
        "build: file:///app/backend/src/routes/auth.js:56",
        "build: SyntaxError: Unexpected reserved word",
        "build: Node.js v20.20.2",
        "build: ==> Exited with status 1",
    ]

    assert runtime_acceptance._render_logs_show_project_failure(logs) is True
