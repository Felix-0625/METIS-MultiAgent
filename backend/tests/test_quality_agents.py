from pathlib import Path
from types import SimpleNamespace

from agents.quality_agents import (
    QAAgent,
    SecAgent,
    _is_configuration_only_delivery,
    _read_source_files,
    _reconcile_functionality_issues,
    _select_functionality_context,
    build_feedback_reports,
    check_layer1_syntax,
    check_layer3_functionality,
    check_api_contract_consistency,
    check_layer4_collaboration,
    _resolve_output_file,
)


def test_qc_markdown_is_normalized_without_format_blocking():
    from agents.quality_agents import _parse_qc_prose_result

    result = _parse_qc_prose_result(
        "1. frontend/src/App.vue:42 createTask only updates local state\n"
        "2. backend/app/main.py health route is missing"
    )

    assert result is not None
    assert result["passed"] is False
    assert [issue["file"] for issue in result["issues"]] == [
        "frontend/src/App.vue",
        "backend/app/main.py",
    ]
    assert result["issues"][0]["line"] == 42


def test_quality_delivery_path_matches_directory_case_insensitively(tmp_path):
    package = tmp_path / "backend" / "package.json"
    package.parent.mkdir()
    package.write_text("{}", encoding="utf-8")

    resolved = _resolve_output_file("Backend/package.json", str(tmp_path))

    assert resolved is not None
    assert resolved[0] == package
    assert resolved[1] == "backend/package.json"


def test_api_contract_rejects_unstripped_vite_proxy_prefix():
    result = check_api_contract_consistency([
        ("backend/app/api/todos.py", '@router.get("/todos")\ndef list_todos(): pass'),
        ("frontend/src/services/todoService.ts", "const api = axios.create({ baseURL: '/api' }); api.get<Todo[]>('/todos');"),
        ("frontend/vite.config.ts", "export default { server: { proxy: { '/api': { target: 'http://localhost:8000' } } } }"),
    ])

    assert result["passed"] is False
    assert result["issues"][0]["severity"] == "error"
    assert result["issues"][0]["file"] == "frontend/vite.config.ts"
    assert "forwarded by Vite as /api/todos" in result["issues"][0]["message"]


def test_api_contract_accepts_vite_proxy_rewrite():
    result = check_api_contract_consistency([
        ("backend/app/api/todos.py", '@router.get("/todos")\ndef list_todos(): pass'),
        ("frontend/src/services/todoService.ts", "const api = axios.create({ baseURL: '/api' }); api.get('/todos');"),
        ("frontend/vite.config.ts", "export default { server: { proxy: { '/api': { target: 'http://localhost:8000', rewrite: path => path.replace(/^\\/api/, '') } } } }"),
    ])

    assert result["passed"] is True
    assert result["issues"] == []


def test_api_contract_combines_include_and_router_prefixes():
    result = check_api_contract_consistency([
        ("backend/app/main.py", "app.include_router(todos_router, prefix='/api')"),
        ("backend/app/api/todos.py", "router = APIRouter(prefix='/todos')\n@router.get('/')\ndef list_todos(): pass"),
        ("frontend/src/services/todoService.ts", "const apiClient = axios.create({ baseURL: '/api' }); apiClient.get<Todo[]>('/todos');"),
        ("frontend/vite.config.ts", "export default { server: { proxy: { '/api': { target: 'http://localhost:8000' } } } }"),
    ])

    assert result["passed"] is True
    assert result["issues"] == []


def test_api_contract_does_not_apply_router_prefix_to_app_routes():
    result = check_api_contract_consistency([
        ("backend/app/main.py", "app.include_router(todos_router, prefix='/api')\n@app.get('/health')\ndef health(): pass"),
        ("backend/app/api/todos.py", "@router.get('/todos')\ndef list_todos(): pass"),
        ("frontend/src/services/health.ts", "const api = axios.create({ baseURL: '' }); api.get('/health');"),
        ("frontend/vite.config.ts", "export default { server: { proxy: { '/health': { target: 'http://localhost:8000' } } } }"),
    ])

    assert result["passed"] is True


def test_api_contract_skips_unproven_fetch_method_and_ambiguous_mounts():
    result = check_api_contract_consistency([
        ("backend/app/main.py", "app.include_router(a, prefix='/a')\napp.include_router(b, prefix='/b')"),
        ("backend/app/api/items.py", '@router.post("/items")\ndef create(): pass'),
        ("frontend/src/items.ts", "fetch('/api/items', { method: 'POST' })"),
        ("frontend/vite.config.ts", "export default { server: { proxy: { '/api': { target: 'http://localhost:8000' } } } }"),
    ])

    assert result["passed"] is True
    assert result["applicable"] is False


def test_api_contract_rejects_proven_response_model_field_mismatch():
    result = check_api_contract_consistency([
        (
            "backend/app/schemas/todo.py",
            "class TodoResponse(BaseModel):\n"
            "    id: int\n    title: str\n    status: str\n",
        ),
        (
            "frontend/src/types/todo.ts",
            "export interface Todo { id: number; text: string; completed: boolean; }",
        ),
        (
            "frontend/src/services/todoService.ts",
            "const api = axios.create({ baseURL: '/api' }); api.get<Todo[]>('/todos');",
        ),
    ])

    assert result["passed"] is False
    assert result["applicable"] is True
    assert "completed, text" in result["issues"][0]["message"]


def _frontend_files_with_entrypoint_after_configs():
    return [
        ("frontend/package.json", '{"scripts":{"build":"vite"}}'),
        ("frontend/vite.config.ts", "export default {}"),
        ("frontend/index.html", "<div id='root'></div>"),
        ("frontend/src/main.tsx", "import App from './App';"),
        ("frontend/src/index.css", "body { margin: 0; }"),
        (
            "frontend/src/App.tsx",
            "const load = () => fetch('/api/todos');\n"
            "export default function App(){ return <TodoInput />; }",
        ),
        ("frontend/src/App.css", ".app { display: block; }"),
        (
            "frontend/src/components/TodoInput.tsx",
            "export default function TodoInput(){ return <input />; }",
        ),
        (
            "frontend/src/components/TodoList.tsx",
            "export default function TodoList({items}){ return <ul>{items.map(x => <li><button>Delete</button></li>)}</ul>; }",
        ),
        (
            "frontend/src/components/FilterBar.tsx",
            "export default function FilterBar(){ return <button>Filter completed status</button>; }",
        ),
    ]


def test_functionality_context_prioritizes_entrypoints_over_config_order():
    files = _frontend_files_with_entrypoint_after_configs()

    selected, manifest = _select_functionality_context(
        files,
        "React TypeScript todo input list filter buttons and backend API calls",
    )

    selected_paths = [path for path, _ in selected]
    assert "frontend/src/App.tsx" in selected_paths[:3]
    assert "frontend/src/components/TodoInput.tsx" in selected_paths
    assert "frontend/src/App.tsx" in manifest
    assert "frontend/package.json" in manifest


def test_functionality_context_keeps_medium_source_file_complete():
    content = "const row = 1;\n" * 500 + "export default true;\n"

    selected, _manifest = _select_functionality_context(
        [("frontend/src/App.tsx", content)], "React application"
    )

    assert selected == [("frontend/src/App.tsx", content)]
    assert "QC CONTEXT WINDOW" not in selected[0][1]


def test_functionality_reconciliation_drops_findings_contradicted_by_workspace():
    files = _frontend_files_with_entrypoint_after_configs()
    selected, _manifest = _select_functionality_context(files, "React todo API")
    issues = [
        {
            "file": "frontend/src/App.tsx",
            "severity": "error",
            "message": "文件不存在：缺少 App.tsx 文件",
        },
        {
            "file": "frontend/src/App.tsx",
            "severity": "error",
            "message": "功能缺失：没有实现 fetch API 调用",
        },
        {
            "file": "frontend/src/App.tsx",
            "severity": "error",
            "message": "功能缺失：没有实现输入框、列表、按钮、筛选、删除和状态切换",
        },
        {
            "file": "frontend/src/app.tsx",
            "severity": "error",
            "message": "未发现 fetch/axios API 调用",
        },
    ]

    assert _reconcile_functionality_issues(files, selected, issues) == []


def test_functionality_reconciliation_keeps_evidence_based_findings():
    files = _frontend_files_with_entrypoint_after_configs()
    selected, _manifest = _select_functionality_context(files, "React todo API")
    issues = [
        {
            "file": "frontend/src/app.tsx",
            "severity": "error",
            "message": "line 1 calls /api/todos but the required endpoint is /todos",
        },
        {
            "file": "frontend/src/components/MissingPanel.tsx",
            "severity": "error",
            "message": "missing file: required MissingPanel.tsx",
        },
        {
            "file": "frontend/src/App.tsx",
            "severity": "error",
            "message": "Delete API call is not implemented",
        },
    ]

    reconciled = _reconcile_functionality_issues(files, selected, issues)

    assert [issue["file"] for issue in reconciled] == [
        "frontend/src/App.tsx",
        "frontend/src/components/MissingPanel.tsx",
        "frontend/src/App.tsx",
    ]


def test_functionality_reconciliation_rejects_context_window_truncation_claim():
    full_content = "const row = 1;\n" * 400 + "export default true;\n"
    files = [("frontend/src/Large.tsx", full_content)]
    selected, _manifest = _select_functionality_context(
        files, "React large page", max_chars=800
    )

    reconciled = _reconcile_functionality_issues(files, selected, [{
        "file": "frontend/src/Large.tsx",
        "severity": "error",
        "message": "The file is incomplete; it cuts off at const row",
    }])

    assert "QC CONTEXT WINDOW" in selected[0][1]
    assert reconciled == []


def test_functionality_check_sends_authoritative_manifest_and_rejects_false_missing(
    monkeypatch,
):
    captured = {}

    class FakeHermesClient:
        def __init__(self, **_kwargs):
            pass

        def chat(self, prompt):
            captured["prompt"] = "\n".join(message.content for message in prompt)
            return {
                "content": (
                    '{"passed": false, "score": 60, "issues": ['
                    '{"file":"frontend/src/App.tsx","severity":"error",'
                    '"message":"missing file: App.tsx","fix_hint":"create it"},'
                    '{"file":"frontend/src/App.tsx","severity":"error",'
                    '"message":"no implementation for fetch API call",'
                    '"fix_hint":"add fetch"}],"summary":"incomplete"}'
                )
            }

    monkeypatch.setattr("core.hermes_client.HermesClient", FakeHermesClient)
    result = check_layer3_functionality(
        _frontend_files_with_entrypoint_after_configs(),
        "React TypeScript todo application with backend API calls",
        SimpleNamespace(
            base_url="http://example.invalid",
            api_key="test",
            model="test-model",
            max_tokens=4096,
        ),
    )

    assert "frontend/src/App.tsx" in captured["prompt"]
    assert "### frontend/src/App.tsx" in captured["prompt"]
    assert result["passed"] is False
    assert result["score"] == 60
    assert any(issue["severity"] == "error" for issue in result["issues"])


def test_functionality_check_discards_source_contradicted_typo(monkeypatch) -> None:
    class FakeHermesClient:
        def __init__(self, **_kwargs):
            pass

        def chat(self, _prompt, **_kwargs):
            return {
                "content": (
                    '{"passed":false,"score":70,"issues":['
                    '{"file":"frontend/src/App.tsx","severity":"error",'
                    '"message":"typo `e.targe` should be `e.target.value`",'
                    '"fix_hint":"fix typo"}],"summary":"compile failure"}'
                )
            }

    monkeypatch.setattr("core.hermes_client.HermesClient", FakeHermesClient)
    result = check_layer3_functionality(
        [
            ("frontend/package.json", '{"dependencies":{"react":"1"}}'),
            (
                "frontend/src/App.tsx",
                "export default function App(){ return <input "
                "onChange={(e) => console.log(e.target.value)} />; }",
            ),
        ],
        "React application",
        SimpleNamespace(
            base_url="http://example.invalid",
            api_key="test",
            model="test-model",
            max_tokens=4096,
        ),
    )

    assert result["passed"] is True


def test_functionality_check_discards_single_quoted_chinese_typo(monkeypatch) -> None:
    class FakeHermesClient:
        def __init__(self, **_kwargs):
            pass

        def chat(self, _prompt, **_kwargs):
            return {
                "content": (
                    '{"passed":false,"score":85,"issues":['
                    '{"file":"frontend/src/App.tsx","severity":"error",'
                    '"message":"第 230 行: 拼写错误 \u0027e.targe\u0027 应为 '
                    '\u0027e.target\u0027，导致 TypeScript 编译错误",'
                    '"fix_hint":"修复拼写"}],"summary":"编译失败"}'
                )
            }

    monkeypatch.setattr("core.hermes_client.HermesClient", FakeHermesClient)
    result = check_layer3_functionality(
        [
            ("frontend/package.json", '{"dependencies":{"react":"1"}}'),
            (
                "frontend/src/App.tsx",
                "export default function App(){ return <input "
                "onChange={(e) => console.log(e.target.value)} />; }",
            ),
        ],
        "React application",
        SimpleNamespace(
            base_url="http://example.invalid",
            api_key="test",
            model="test-model",
            max_tokens=4096,
        ),
    )

    assert result["passed"] is True


def test_functionality_check_accepts_all_reconciled_blocking_findings(monkeypatch) -> None:
    class FakeHermesClient:
        def __init__(self, **_kwargs):
            pass

        def chat(self, _prompt, **_kwargs):
            return {
                "content": (
                    '{"passed":false,"score":85,"issues":['
                    '{"file":"frontend/src/App.tsx","severity":"error",'
                    '"message":"第 202 行: e.targe 拼写错误，应为 e.target",'
                    '"fix_hint":"fix"}],"summary":"failed"}'
                )
            }

    monkeypatch.setattr("core.hermes_client.HermesClient", FakeHermesClient)
    result = check_layer3_functionality(
        [
            ("frontend/package.json", '{"dependencies":{"react":"1"}}'),
            ("frontend/src/App.tsx", "const value = e.target.value;"),
        ],
        "React application",
        SimpleNamespace(
            base_url="http://example.invalid",
            api_key="test",
            model="test-model",
            max_tokens=4096,
        ),
    )

    assert result["passed"] is True
    assert result["issues"] == []
    assert result["issues"] == []


def test_functionality_check_fails_closed_when_llm_raises(monkeypatch) -> None:
    class FailingHermesClient:
        def __init__(self, **_kwargs):
            pass

        def chat(self, _prompt):
            raise TimeoutError("provider timeout")

    monkeypatch.setattr("core.hermes_client.HermesClient", FailingHermesClient)
    result = check_layer3_functionality(
        [("app.py", "def create_project():\n    raise RuntimeError('not implemented')\n")],
        "Create a working project service",
        SimpleNamespace(
            base_url="http://example.invalid",
            api_key="test",
            model="test-model",
            max_tokens=4096,
        ),
    )

    assert result["passed"] is False
    assert result["issues"][0]["severity"] == "error"
    assert result["issues"][0]["layer"] == "functionality"


def test_functionality_check_fails_closed_for_invalid_json(monkeypatch) -> None:
    class InvalidHermesClient:
        def __init__(self, **_kwargs):
            pass

        def chat(self, _prompt):
            return {"content": "not JSON"}

    monkeypatch.setattr("core.hermes_client.HermesClient", InvalidHermesClient)
    result = check_layer3_functionality(
        [("app.py", "def create_project():\n    return {'status': 'placeholder'}\n")],
        "Create a working project service",
        SimpleNamespace(
            base_url="http://example.invalid",
            api_key="test",
            model="test-model",
            max_tokens=4096,
        ),
    )

    assert result["passed"] is False
    assert result["issues"][0]["severity"] == "error"


def test_functionality_check_retries_invalid_json_once(monkeypatch) -> None:
    class RecoveringHermesClient:
        calls = 0

        def __init__(self, **_kwargs):
            pass

        def chat(self, _prompt, **_kwargs):
            type(self).calls += 1
            if type(self).calls == 1:
                return {"content": "not JSON"}
            return {
                "content": (
                    'Result:\n```json\n{"passed":true,"score":100,'
                    '"issues":[],"summary":"implemented"}\n```'
                )
            }

    monkeypatch.setattr("core.hermes_client.HermesClient", RecoveringHermesClient)
    result = check_layer3_functionality(
        [("app.py", "def create_project():\n    return {'status': 'ok'}\n")],
        "Create a working project service",
        SimpleNamespace(
            base_url="http://example.invalid",
            api_key="test",
            model="test-model",
            max_tokens=4096,
        ),
    )

    assert RecoveringHermesClient.calls == 2
    assert result["passed"] is True


def test_functionality_check_retries_invalid_schema_once(monkeypatch) -> None:
    class RecoveringHermesClient:
        calls = 0

        def __init__(self, **_kwargs):
            pass

        def chat(self, _prompt, **_kwargs):
            type(self).calls += 1
            if type(self).calls == 1:
                return {"content": '{"passed":1,"score":100,"issues":[]}'}
            return {
                "content": (
                    '{"passed":true,"score":100,"issues":[],'
                    '"summary":"implemented"}'
                )
            }

    monkeypatch.setattr("core.hermes_client.HermesClient", RecoveringHermesClient)
    result = check_layer3_functionality(
        [("app.py", "def create_project():\n    return {'status': 'ok'}\n")],
        "Create a working project service",
        SimpleNamespace(
            base_url="http://example.invalid",
            api_key="test",
            model="test-model",
            max_tokens=4096,
        ),
    )

    assert RecoveringHermesClient.calls == 2
    assert result["passed"] is True


def test_functionality_check_normalizes_boolean_string_verdict(monkeypatch) -> None:
    class HermesClient:
        def __init__(self, **_kwargs):
            pass

        def chat(self, _prompt, **_kwargs):
            return {"content": '{"passed":"true","score":100,"issues":[]}'}

    monkeypatch.setattr("core.hermes_client.HermesClient", HermesClient)
    result = check_layer3_functionality(
        [("app.py", "def create_project():\n    return {'status': 'ok'}\n")],
        "Create a working project service",
        SimpleNamespace(
            base_url="http://example.invalid",
            api_key="test",
            model="test-model",
            max_tokens=4096,
        ),
    )

    assert result["passed"] is True


def test_functionality_check_respects_explicit_failed_verdict_without_issues(
    monkeypatch,
) -> None:
    class RejectingHermesClient:
        def __init__(self, **_kwargs):
            pass

        def chat(self, _prompt):
            return {
                "content": (
                    '{"passed":false,"score":0,"issues":[],'
                    '"summary":"Required workflow is not implemented"}'
                )
            }

    monkeypatch.setattr("core.hermes_client.HermesClient", RejectingHermesClient)
    result = check_layer3_functionality(
        [("app.py", "def create_project():\n    return {'status': 'placeholder'}\n")],
        "Create a working project service",
        SimpleNamespace(
            base_url="http://example.invalid",
            api_key="test",
            model="test-model",
            max_tokens=4096,
        ),
    )

    assert result["passed"] is False
    assert any(issue["severity"] == "error" for issue in result["issues"])


def test_concise_complete_code_warns_without_blocking(monkeypatch, tmp_path) -> None:
    source = tmp_path / "index.html"
    source.write_text(
        "<!doctype html>\n<html><body><button>Add</button><button>Delete</button>"
        "<script>localStorage.setItem('tasks','[]')</script></body></html>",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agents.quality_agents.check_layer3_functionality",
        lambda *_args, **_kwargs: {
            "layer": "functionality", "passed": True, "score": 100,
            "issues": [], "issue_count": 0,
        },
    )

    result = QAAgent(hermes_client=None).inspect(
        subproject_id="concise-ui",
        workspace_path=str(tmp_path),
        output_files=["index.html"],
        subproject_description="task add delete localStorage",
    )

    assert result["passed"] is True
    assert result["error_count"] == 0
    assert result["warning_count"] == 0
    assert result["needs_rewrite"] is False


def test_architecture_delivery_reads_declared_docs(monkeypatch, tmp_path) -> None:
    docs_dir = tmp_path / "docs" / "architecture"
    docs_dir.mkdir(parents=True)
    output_files = [
        "docs/architecture/system-architecture.md",
        "docs/architecture/data-model.md",
        "docs/architecture/api-contract.md",
        "docs/architecture/rbac-matrix.md",
    ]
    for index, rel_path in enumerate(output_files, start=1):
        (tmp_path / rel_path).write_text(
            f"# Architecture deliverable {index}\n\n"
            "## Scope\nDefined project boundaries and responsibilities.\n\n"
            "## Contract\nDefined inputs, outputs, and acceptance criteria.\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        "agents.quality_agents.check_layer3_functionality",
        lambda *_args, **_kwargs: {
            "layer": "functionality", "passed": True, "score": 100,
            "issues": [], "issue_count": 0,
        },
    )

    result = QAAgent(hermes_client=None).inspect(
        subproject_id="architecture-phase",
        workspace_path=str(tmp_path),
        output_files=output_files,
        subproject_description="Produce the system architecture documents",
        agent_role="架构师",
        artifact_kind="architecture_document",
    )

    assert result["passed"] is True
    assert result["details"]["files_checked"] == 4
    assert result["details"].get("validation") != "NO_FILES_FOUND"


def test_explicit_delivery_is_not_silently_dropped_by_suffix_or_build_dir(tmp_path) -> None:
    paths = ["infra/main.tf", "dist/runtime.bundle.mjs"]
    for rel_path in paths:
        target = tmp_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("resource = true\n", encoding="utf-8")

    files = _read_source_files(str(tmp_path), paths)

    assert [path for path, _content in files] == paths


def test_fallback_scan_still_ignores_unrelated_docs(tmp_path) -> None:
    target = tmp_path / "docs" / "old-design.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Historical design\n", encoding="utf-8")

    assert _read_source_files(str(tmp_path)) == []


def test_console_error_does_not_require_immediate_return() -> None:
    result = check_layer1_syntax(
        [
            (
                "backend/src/middleware/error.js",
                """function errorHandler(err, req, res, next) {
  console.error(err.stack);
  const status = err.status || 500;
  res.status(status).json({ error: err.message });
}
""",
            )
        ]
    )

    assert result["passed"] is True
    assert result["issues"] == []


def test_invalid_package_json_is_a_syntax_error() -> None:
    result = check_layer1_syntax(
        [("backend/package.json", '{"scripts":{"test":"jest"}}\n# FIX: explanation')]
    )

    assert result["issue_count"] == 1
    assert result["issues"][0]["severity"] == "error"
    assert "JSON 语法错误" in result["issues"][0]["message"]


def test_missing_functionality_reviewer_fails_closed() -> None:
    result = check_layer3_functionality(
        [("backend/src/db/index.js", "export { getDb } from './connection.js';\n")],
        "database module",
        hermes_client=None,
    )

    assert result["passed"] is False
    assert result["issues"][0]["severity"] == "error"


def test_error_issue_cannot_be_reported_as_overall_pass() -> None:
    feedback = build_feedback_reports(
        [{
            "layer": "functionality",
            "passed": True,
            "score": 70,
            "issues": [{"severity": "error", "message": "broken", "file": "app.js"}],
            "issue_count": 1,
        }],
        "project",
        "developer",
    )

    assert feedback["overall_passed"] is False
    assert "❌ 未通过" in feedback["user_report"]


def test_package_script_cannot_run_bin_shell_shim_with_node() -> None:
    result = check_layer4_collaboration([
        (
            "backend/package.json",
            '{"scripts":{"test":"node --experimental-vm-modules node_modules/.bin/jest"}}',
        )
    ])

    assert result["passed"] is False
    assert result["issues"][0]["file"] == "backend/package.json"


def test_final_scan_includes_package_manifests(tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"test":"jest"}}', encoding="utf-8")
    (tmp_path / "app.js").write_text("console.log('ok')", encoding="utf-8")

    files = dict(_read_source_files(str(tmp_path)))

    assert "package.json" in files
    assert "app.js" in files


def test_explicit_missing_output_files_do_not_fall_back_to_workspace(tmp_path) -> None:
    (tmp_path / "healthy.py").write_text("def healthy():\n    return True\n", encoding="utf-8")

    files = _read_source_files(str(tmp_path), ["missing.py"])

    assert files == []


def test_final_qa_static_checks_cover_files_after_first_two_hundred(
    monkeypatch,
    tmp_path,
) -> None:
    output_files = []
    for index in range(200):
        name = f"module_{index:03d}.py"
        (tmp_path / name).write_text("value = 1\n", encoding="utf-8")
        output_files.append(name)
    (tmp_path / "zzz_broken.py").write_text("def broken(:\n", encoding="utf-8")
    output_files.append("zzz_broken.py")
    monkeypatch.setattr(
        "agents.quality_agents.check_layer3_functionality",
        lambda *_args, **_kwargs: {
            "layer": "functionality",
            "passed": True,
            "score": 100,
            "issues": [],
            "issue_count": 0,
        },
    )

    result = QAAgent(hermes_client=None).inspect(
        subproject_id="large-project",
        workspace_path=str(tmp_path),
        output_files=output_files,
        is_final_phase=True,
    )

    assert result["passed"] is False
    assert result["details"]["files_checked"] == 201
    assert any(
        issue.get("file") == "zzz_broken.py"
        for layer in result["layer_results"]
        for issue in layer.get("issues", [])
    )


def test_qa_declared_unreadable_output_is_blocking(monkeypatch, tmp_path) -> None:
    source = tmp_path / "app.py"
    source.write_text("print('ok')\n", encoding="utf-8")
    original_read_text = Path.read_text

    def failing_read_text(path, *args, **kwargs):
        if path == source:
            raise OSError("read denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read_text)
    result = QAAgent(hermes_client=None).inspect(
        subproject_id="unreadable",
        workspace_path=str(tmp_path),
        output_files=["app.py"],
    )

    assert result["passed"] is False
    assert result["error_count"] >= 1
    assert any("app.py" in issue for issue in result["issues"])


def test_security_declared_missing_output_cannot_pass_clean(tmp_path) -> None:
    result = SecAgent(hermes_client=None).inspect(
        subproject_id="missing-security-target",
        workspace_path=str(tmp_path),
        output_files=["missing.py"],
    )

    assert result["passed"] is False
    assert result["security_status"] == "scan_failed"
    assert result["details"]["files_scanned"] == 0


def test_security_declared_unreadable_output_is_blocking(monkeypatch, tmp_path) -> None:
    source = tmp_path / "app.py"
    source.write_text("print('ok')\n", encoding="utf-8")
    original_read_text = Path.read_text

    def failing_read_text(path, *args, **kwargs):
        if path == source:
            raise OSError("read denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read_text)
    result = SecAgent(hermes_client=None).inspect(
        subproject_id="unreadable-security-target",
        workspace_path=str(tmp_path),
        output_files=["app.py"],
    )

    assert result["passed"] is False
    assert result["security_status"] == "scan_failed"
    assert any("app.py" in issue for issue in result["issues"])


def test_devops_delivery_can_be_configuration_only(tmp_path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    env_example = tmp_path / ".env.example"
    dockerfile.write_text("FROM node:20\nCMD [\"npm\", \"start\"]\n", encoding="utf-8")
    env_example.write_text("PORT=3000\n", encoding="utf-8")
    files = _read_source_files(str(tmp_path), ["Dockerfile", ".env.example"])

    assert {path for path, _ in files} == {"Dockerfile", ".env.example"}
    assert _is_configuration_only_delivery(
        files,
        ["Dockerfile", ".env.example"],
        "Create deployment configuration",
        "DevOps Engineer",
    ) is True


def test_collaboration_accepts_typescript_interface_and_type_exports() -> None:
    result = check_layer4_collaboration([
        (
            "frontend/src/types/todo.ts",
            "export interface Todo { id: number; title: string }\n"
            "export type FilterStatus = 'all' | 'pending';\n",
        ),
        (
            "frontend/src/App.tsx",
            "import { Todo, FilterStatus } from './types/todo';\n"
            "export default function App(){ const value: FilterStatus = 'all'; return <div>{value}</div>; }\n",
        ),
    ])

    assert result["passed"] is True
    assert result["issues"] == []


def test_collaboration_rejects_missing_parent_relative_import() -> None:
    result = check_layer4_collaboration([
        ("backend/package.json", '{"type":"module"}'),
        ("backend/tests/setup.ts", "import { app } from '../src/app';"),
        ("backend/src/index.js", "export const ok = true;"),
    ])

    assert result["passed"] is False
    assert any("../src/app" in issue["message"] for issue in result["issues"])


def test_collaboration_rejects_unconfigured_typescript_jest_tests() -> None:
    result = check_layer4_collaboration([
        ("backend/package.json", '{"type":"module","scripts":{"test":"jest"},"devDependencies":{"jest":"^29"}}'),
        ("backend/tests/auth.test.ts", "test('auth', () => expect(true).toBe(true));"),
        ("backend/src/index.js", "export const app = {};"),
    ])

    assert result["passed"] is False
    assert any("TypeScript tests exist" in issue["message"] for issue in result["issues"])


def test_collaboration_rejects_missing_named_export_and_dependency() -> None:
    result = check_layer4_collaboration([
        ("backend/package.json", '{"type":"module","dependencies":{}}'),
        ("backend/tests/setup.ts", "export { app };"),
        (
            "backend/tests/auth.test.ts",
            "import { app, AppDataSource } from './setup';\nimport request from 'supertest';",
        ),
    ])

    assert result["passed"] is False
    assert any("AppDataSource" in issue["message"] for issue in result["issues"])
    assert any("supertest" in issue["message"] for issue in result["issues"])
    dependency_issue = next(issue for issue in result["issues"] if "supertest" in issue["message"])
    assert dependency_issue["file"] == "backend/package.json"


def test_collaboration_does_not_treat_template_message_from_as_import() -> None:
    result = check_layer4_collaboration([
        ("backend/package.json", '{"type":"module","dependencies":{}}'),
        (
            "backend/src/routes/tickets.js",
            "export function transition(currentStatus, status) {\n"
            "  throw new Error(`Cannot transition from '${currentStatus}' to '${status}'`);\n"
            "}\n",
        ),
    ])

    assert not any("currentStatus" in issue["message"] for issue in result["issues"])


def test_collaboration_rejects_jest_esm_tests_without_vm_modules() -> None:
    result = check_layer4_collaboration([
        (
            "backend/package.json",
            '{"type":"module","scripts":{"test":"jest"},'
            '"devDependencies":{"jest":"^29","@jest/globals":"^29"}}',
        ),
        (
            "backend/tests/assets.test.js",
            "import { describe, test } from '@jest/globals';\n"
            "import { app } from '../src/server.js';\n",
        ),
        ("backend/src/server.js", "export const app = {};\n"),
    ])

    assert result["passed"] is False
    issue = next(issue for issue in result["issues"] if "VM modules" in issue["message"])
    assert issue["file"] == "backend/package.json"
    assert "--experimental-vm-modules" in issue["fix_hint"]


def test_collaboration_rejects_jest_esm_array_instanceof_matcher() -> None:
    result = check_layer4_collaboration([
        (
            "backend/package.json",
            '{"type":"module","scripts":{"test":"NODE_OPTIONS=--experimental-vm-modules jest"},'
            '"devDependencies":{"jest":"^29","@jest/globals":"^29"}}',
        ),
        (
            "backend/tests/assets.test.js",
            "import { expect, test } from '@jest/globals';\n"
            "import { app } from '../src/server.js';\n"
            "test('lists assets', () => expect(app.assets).toBeInstanceOf(Array));\n",
        ),
        ("backend/src/server.js", "export const app = { assets: [] };\n"),
    ])

    assert result["passed"] is False
    issue = next(
        issue for issue in result["issues"]
        if "toBeInstanceOf(Array)" in issue["message"]
    )
    assert issue["file"] == "backend/tests/assets.test.js"
    assert issue["line"] == 3
    assert issue["severity"] == "error"
    assert "Array.isArray" in issue["fix_hint"]


def test_collaboration_accepts_jest_esm_array_is_array_matcher() -> None:
    result = check_layer4_collaboration([
        (
            "backend/package.json",
            '{"type":"module","scripts":{"test":"NODE_OPTIONS=--experimental-vm-modules jest"},'
            '"devDependencies":{"jest":"^29","@jest/globals":"^29"}}',
        ),
        (
            "backend/tests/assets.test.js",
            "import { expect, test } from '@jest/globals';\n"
            "import { app } from '../src/server.js';\n"
            "test('lists assets', () => expect(Array.isArray(app.assets)).toBe(true));\n",
        ),
        ("backend/src/server.js", "export const app = { assets: [] };\n"),
    ])

    assert not any(
        "toBeInstanceOf(Array)" in issue["message"] for issue in result["issues"]
    )


def test_collaboration_accepts_tests_that_dynamically_import_production_source() -> None:
    result = check_layer4_collaboration([
        (
            "backend/package.json",
            '{"type":"module","scripts":{"test":"NODE_OPTIONS=--experimental-vm-modules jest"},'
            '"devDependencies":{"jest":"^29","@jest/globals":"^29"}}',
        ),
        (
            "backend/tests/assets.test.js",
            "import { test } from '@jest/globals';\n"
            "test('real routes', async () => { await import('../src/routes/assets.js'); });\n",
        ),
        ("backend/src/routes/assets.js", "export default {};\n"),
    ])

    assert not any("does not import production source" in issue["message"] for issue in result["issues"])


def test_collaboration_accepts_tests_that_spawn_production_entrypoint() -> None:
    result = check_layer4_collaboration([
        (
            "package.json",
            '{"scripts":{"start":"node src/server.js","test":"node --test"}}',
        ),
        (
            "tests/todos.test.js",
            "const { spawn } = require('child_process');\n"
            "const path = require('path');\n"
            "const server = spawn('node', ["
            "path.join(__dirname, '..', 'src', 'server.js')]);\n"
            "server.kill();\n",
        ),
        ("src/server.js", "require('http').createServer().listen(3000);\n"),
    ])

    assert not any(
        "does not import production source" in issue["message"]
        for issue in result["issues"]
    )


def test_collaboration_rejects_root_script_that_calls_child_cli_after_cd() -> None:
    result = check_layer4_collaboration([
        (
            "package.json",
            '{"scripts":{"test":"cd backend && NODE_OPTIONS=--experimental-vm-modules jest"}}',
        ),
        (
            "backend/package.json",
            '{"type":"module","scripts":{"test":"NODE_OPTIONS=--experimental-vm-modules jest"},'
            '"devDependencies":{"jest":"^29"}}',
        ),
    ])

    issue = next(issue for issue in result["issues"] if "root npm PATH" in issue["message"])
    assert issue["file"] == "package.json"
    assert "npm --prefix backend test" in issue["fix_hint"]


def test_collaboration_rejects_mock_only_tests_and_broken_tsconfig() -> None:
    result = check_layer4_collaboration([
        (
            "backend/package.json",
            '{"type":"module","devDependencies":{"jest":"1","typescript":"1","ts-jest":"1","express":"1"},'
            '"jest":{"transform":{"^.+\\\\.ts$":"ts-jest"}}}',
        ),
        ("backend/src/server.js", "export function createApp() { return {}; }"),
        (
            "backend/tests/setup.ts",
            "import express from 'express';\nconst testApp = express();\ntestApp.get('/api/health', () => {});",
        ),
        (
            "backend/tests/tsconfig.json",
            '{"extends":"../tsconfig.json","compilerOptions":{"types":["jest"]}}',
        ),
    ])

    messages = [issue["message"] for issue in result["issues"]]
    assert result["passed"] is False
    assert any("standalone mock routes" in message for message in messages)
    assert any("does not import production source" in message for message in messages)
    assert any("指向不存在" in message for message in messages)
    assert any("esModuleInterop" in message for message in messages)
    production_issue = next(issue for issue in result["issues"] if "does not import production source" in issue["message"])
    assert production_issue["file"] == "backend/tests/setup.ts"


def test_collaboration_accepts_commonjs_tests_importing_production_source() -> None:
    result = check_layer4_collaboration([
        (
            "backend/package.json",
            '{"dependencies":{"express":"1","supertest":"1"},"devDependencies":{"jest":"1"}}',
        ),
        ("backend/src/server.js", "module.exports = { createApp() { return {}; } };"),
        (
            "backend/tests/auth.test.js",
            "const request = require('supertest');\n"
            "const { createApp } = require('../src/server');\n"
            "test('health', async () => request(createApp()).get('/api/health'));",
        ),
    ])

    assert not any(
        "does not import production source" in issue["message"]
        for issue in result["issues"]
    )


def test_collaboration_rejects_commonjs_typescript_tests_for_esm_package() -> None:
    result = check_layer4_collaboration([
        (
            "backend/package.json",
            '{"type":"module","devDependencies":{"jest":"1","typescript":"1","ts-jest":"1"},'
            '"jest":{"transform":{"^.+\\\\.ts$":"ts-jest"}}}',
        ),
        ("backend/src/server.js", "export function createApp() { return {}; }"),
        ("backend/tests/setup.ts", "import { createApp } from '../src/server.js';\nexport const app = createApp();"),
        (
            "backend/tests/tsconfig.json",
            '{"compilerOptions":{"module":"commonjs","esModuleInterop":true}}',
        ),
    ])

    messages = [issue["message"] for issue in result["issues"]]
    assert any("compile as CommonJS" in message for message in messages)
    assert any("does not treat TypeScript tests as ESM" in message for message in messages)


def test_collaboration_rejects_common_fullstack_runtime_contract_holes() -> None:
    result = check_layer4_collaboration([
        (
            "frontend/package.json",
            '{"scripts":{"build":"tsc && vite build"},"dependencies":{"react":"1"}}',
        ),
        (
            "frontend/tsconfig.json",
            '{"compilerOptions":{"noUnusedLocals":true}}',
        ),
        (
            "frontend/src/App.tsx",
            "import { useState } from 'react';\n"
            "interface User { id: number }\n"
            "export default function App(){ const [user, setUser] = useState(null); "
            "setUser(null); return <main />; }",
        ),
        (
            "backend/package.json",
            '{"dependencies":{"better-sqlite3":"1"},"devDependencies":{"jest":"1"}}',
        ),
        (
            "backend/src/server.js",
            "const JWT_SECRET = process.env.JWT_SECRET || 'dev-secret';\n"
            "const db = new Database(process.env.DATABASE_PATH);\n"
            "const { description, status, priority, due_date } = req.body;\n"
            "const s = status && validStatuses.includes(status) ? status : 'todo';\n"
            "const p = priority && validPriorities.includes(priority) ? priority : 'medium';\n"
            "db.prepare('INSERT INTO tasks (due_date) VALUES (?)');\n"
            "stmt.run(description || '', s, p, due_date || null);\n"
            "module.exports = app;",
        ),
        (
            "backend/tests/api.test.js",
            "const app = require('../src/server');\n"
            "afterAll(() => fs.unlinkSync(process.env.DATABASE_PATH));",
        ),
    ])

    messages = [issue["message"] for issue in result["issues"]]
    assert result["passed"] is False
    assert any("JWT secret" in message for message in messages)
    assert any("silently accepts invalid status" in message for message in messages)
    assert any("silently accepts invalid priority" in message for message in messages)
    assert any("due_date" in message for message in messages)
    assert any("description" in message for message in messages)
    assert any("SQLite connection" in message for message in messages)
    assert any("unused React state" in message for message in messages)
    assert any("unused type/interface `User`" in message for message in messages)


def test_collaboration_accepts_validated_dates_and_closed_sqlite_cleanup() -> None:
    result = check_layer4_collaboration([
        ("backend/package.json", '{"dependencies":{"better-sqlite3":"1"},"devDependencies":{"jest":"1"}}'),
        (
            "backend/src/server.js",
            "const db = new Database(process.env.DATABASE_PATH);\n"
            "const { description, due_date } = req.body;\n"
            "if (description !== undefined && typeof description !== 'string') return res.status(400).json({});\n"
            "if (due_date && !/^\\\\d{4}-\\\\d{2}-\\\\d{2}$/.test(due_date)) return res.status(400).json({});\n"
            "db.prepare('INSERT INTO tasks (description, due_date) VALUES (?, ?)').run(description || '', due_date);\n"
            "module.exports = app;",
        ),
        (
            "backend/tests/api.test.js",
            "const app = require('../src/server');\n"
            "afterAll(() => { app.closeDatabase(); fs.unlinkSync(process.env.DATABASE_PATH); });",
        ),
    ])

    messages = [issue["message"] for issue in result["issues"]]
    assert not any("due_date" in message for message in messages)
    assert not any("SQLite connection" in message for message in messages)
    assert not any("description" in message for message in messages)
