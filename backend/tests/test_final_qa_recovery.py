from pathlib import Path
from types import SimpleNamespace

from core.agent_lifecycle import transition_agent
from api import routes_adjustments
from api.routes_adjustments import (
    _apply_final_qa_external_issues,
    _format_final_qa_rework_issue,
    _final_qa_rework_scope,
    _snapshot_final_qa_rework_files,
    _final_qa_snapshot_changed,
    _restore_final_qa_rework_snapshot,
    _final_qa_issue_signature,
    _final_qa_rework_failures,
    _final_qa_state_blockers,
    _final_qa_phase_blockers,
    _final_qa_manual_issue,
    _runtime_acceptance_issue,
    _runtime_acceptance_required,
    _previous_passed_runtime_result,
    _runtime_failure_actionable,
    _scan_final_qa_source_files,
    _source_issue_is_disproven,
    _normalize_final_qa_issue,
    FINAL_QA_ISSUE_FIELDS,
)


def test_final_qa_ignores_historical_agents_and_blocks_current_adjustment_receipt():
    project_id = "final-qa-current-adjustment"
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=Path("."),
        agents={"agent-1": {"status": "pending", "progress": 0}},
        subprojects=[{
            "id": "sp-1", "agent_id": "agent-1", "status": "ready", "progress": 0,
        }],
    )
    adjustments = routes_adjustments._get_adjustments(project_id)
    adjustments.clear()
    adjustments.append({
        "id": "adj-1",
        "status": "awaiting_final_qa",
    })
    try:
        assert _final_qa_state_blockers(ctx) == [
            "adjustment adj-1: missing active run",
        ]
    finally:
        adjustments.clear()


def test_final_qa_issue_contract_has_fixed_fields_and_null_defaults():
    issue = _normalize_final_qa_issue({
        "severity": "ERROR",
        "file_path": "src/app.py",
        "message": "response contract mismatch",
        "fix_hint": "return user_id",
        "criterion_id": "invented",
        "expected": "user_id",
    }, valid_criteria={"AC-1"})

    assert tuple(issue) == FINAL_QA_ISSUE_FIELDS
    assert issue["severity"] == "error"
    assert issue["file"] == "src/app.py"
    assert issue["line"] is None
    assert issue["criterion"] is None
    assert issue["expected"] is None
    assert issue["actual"] is None
    assert issue["fix"] == "return user_id"
    assert issue["related_files"] == []
    assert issue["id"]


def test_final_qa_issue_contract_keeps_only_verified_optional_values():
    issue = _normalize_final_qa_issue({
        "id": "d-1",
        "severity": "critical",
        "file": "src/app.py",
        "line": 7,
        "criterion": "AC-1",
        "message": "wrong response",
        "expected": "user_id",
        "actual": "id",
        "fix": "rename the response field",
        "related_files": ["src/client.ts", "src/client.ts", "../escape.ts"],
    }, valid_criteria={"AC-1"})

    assert issue == {
        "id": "d-1",
        "severity": "critical",
        "file": "src/app.py",
        "line": 7,
        "criterion": "AC-1",
        "message": "wrong response",
        "expected": "user_id",
        "actual": "id",
        "fix": "rename the response field",
        "related_files": ["src/client.ts"],
    }


def test_final_qa_blocks_invalid_current_adjustment_contract_not_historical_subprojects():
    project_id = "final-qa-invalid-adjustment-contract"
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=Path("."),
        agents={},
        subprojects=[
            {
                "id": "phase-1", "phase_id": "phase-1",
                "status": "pending", "progress": 0,
            },
            {
                "id": "task-1", "phase_id": "phase-1",
                "deliverables": ["src/app.py"],
                "status": "pending", "progress": 0,
            },
        ],
    )
    adjustments = routes_adjustments._get_adjustments(project_id)
    adjustments.clear()
    adjustments.append({
        "id": "adj-1",
        "status": "awaiting_final_qa",
        "active_run": {"execution_contract": {"schema_version": "invalid"}},
    })
    try:
        assert _final_qa_state_blockers(ctx) == [
            "adjustment adj-1: invalid execution contract",
        ]
    finally:
        adjustments.clear()


def test_final_qa_requires_confirmed_completed_phase():
    assert _final_qa_phase_blockers([
        {"phase_id": "p1", "status": "pending", "user_confirmed": True},
        {"phase_id": "p2", "status": "completed", "user_confirmed": False},
    ]) == ["phase p1: pending", "phase p2: not confirmed"]


def test_final_qa_manual_issue_keeps_repair_contract_fields():
    manual = _final_qa_manual_issue({
        "id": "issue-1", "message": "broken", "file_path": "src/app.py",
        "line_no": 7, "severity": "error", "layer": "syntax",
        "fix_hint": "repair it", "responsible_agent_id": "agent-1",
        "evidence": {"command": "pytest"},
    })
    assert manual["status"] == "needs_manual"
    assert manual["severity"] == "error"
    assert manual["fix_hint"] == "repair it"
    assert manual["responsible_agent_id"] == "agent-1"
    assert manual["evidence"] == {"command": "pytest"}


def test_final_qa_signature_prefers_stable_issue_id():
    first = {"id": "issue-1", "message": "Failure on line 10"}
    reworded = {"id": "issue-1", "message": "Different explanation at line 99"}
    assert _final_qa_issue_signature(first) == _final_qa_issue_signature(reworded)
from api.routes_supervisor import (
    _deduplicate_routed_issues,
    _has_blocking_qc_issues,
    _qc_issue_fingerprint,
    _reconcile_needs_manual_qc_issue,
    _reconcile_pending_verification_qc_issue,
    _refresh_issue_routing,
    _should_append_qc_issue,
    _should_reopen_fixed_issue,
    _stable_qc_issue_id,
)


def test_pending_verification_reopens_same_issue_when_reproduced():
    old = {
        "id": "issue-stable",
        "fingerprint": "syntax|src/app.py|missing",
        "message": "missing",
        "file_path": "src/app.py",
        "layer": "syntax",
        "status": "pending_verification",
        "fix_rounds": 2,
        "repair_workspace_digest": "workspace-1",
    }
    reproduced = {
        "id": "issue-reworded",
        "message": "missing",
        "file_path": "src/app.py",
        "layer": "syntax",
    }
    result = _reconcile_pending_verification_qc_issue(old, reproduced)
    assert result["id"] != "issue-stable"
    assert result["legacy_id"] == "issue-stable"
    assert result["status"] == "pending_verification"
    assert result["fix_rounds"] == 2
    assert result["identity_review_required"] is True
    assert result["verification_result"] == "reproduced"
    assert result["verification_evidence"]["repair_workspace_digest"] == "workspace-1"


def test_pending_verification_closes_same_issue_with_verification_evidence():
    old = {
        "id": "issue-stable",
        "status": "pending_verification",
        "repair_workspace_digest": "workspace-1",
    }
    result = _reconcile_pending_verification_qc_issue(old, None)
    assert result["id"] != "issue-stable"
    assert result["legacy_id"] == "issue-stable"
    assert result["status"] == "pending_verification"
    assert result["identity_review_required"] is True
    assert result["verification_result"] == "not_reproduced"
    assert result["verification_evidence"]["repair_workspace_digest"] == "workspace-1"


def test_warning_only_qc_does_not_block_phase_progress() -> None:
    assert _has_blocking_qc_issues([
        {"severity": "warning", "status": "open"},
        {"severity": "warning", "status": "needs_manual"},
    ]) is False


def test_final_qa_rework_snapshot_detects_and_restores_file_changes(tmp_path) -> None:
    target = tmp_path / "src" / "app.js"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"before")
    issues = [{"file_path": "src/app.js", "fix_hint": ""}]

    snapshot = _snapshot_final_qa_rework_files(tmp_path, issues)
    target.write_bytes(b"after")

    assert _final_qa_snapshot_changed(snapshot) is True
    _restore_final_qa_rework_snapshot(snapshot)
    assert target.read_bytes() == b"before"


def test_runtime_acceptance_is_required_by_default_on_render(monkeypatch) -> None:
    monkeypatch.delenv("RUNTIME_ACCEPTANCE_REQUIRED", raising=False)
    monkeypatch.setenv("RENDER", "true")

    assert _runtime_acceptance_required() is True


def test_runtime_acceptance_requirement_can_be_disabled_for_local_dev(monkeypatch) -> None:
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("RUNTIME_ACCEPTANCE_REQUIRED", "false")

    assert _runtime_acceptance_required() is False


def test_final_qa_discards_source_claims_directly_disproven_by_code(tmp_path) -> None:
    files = {
        "frontend/src/App.tsx": (
            "import React from 'react';\nconst App: React.FC = () => <div />;\n"
        ),
        "backend/src/routes/index.js": (
            "const express = require('express');\nconst router = express.Router();\n"
        ),
        "backend/src/routes/dashboard.js": (
            "const auth = require('../middleware/auth');\nmodule.exports = auth;\n"
        ),
        "backend/src/middleware/auth.js": "module.exports = function auth() {};\n",
        "backend/src/index.js": "require('dotenv').config();\nconst app = require('./app');\n",
        "backend/package.json": '{"dependencies":{"dotenv":"^16.3.1"}}',
    }
    for relative, content in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    claims = [
        {"file_path": "frontend/src/App.tsx", "message": "React.FC 未导入 React"},
        {"file_path": "frontend/src/App.tsx", "message": "React 导入未使用"},
        {"file_path": "backend/src/routes/index.js", "message": "express.Router 未导入 express"},
        {"file_path": "backend/src/routes/dashboard.js", "message": "auth 中间件未导出为模块"},
        {"file_path": "backend/src/index.js", "message": "dotenv 未在 package.json 中声明"},
    ]

    assert all(_source_issue_is_disproven(issue, tmp_path) for issue in claims)
    assert _source_issue_is_disproven({
        "file_path": "backend/src/index.js",
        "message": "./app 文件不存在",
    }, tmp_path) is False


def test_final_qa_convergence_signature_ignores_runtime_log_tail() -> None:
    first = {
        "layer": "runtime_acceptance",
        "file_path": "backend/src/db.js",
        "message": "Runtime startup failed\nbuild: first deploy id",
    }
    second = {
        **first,
        "message": "Runtime startup failed\nbuild: different deploy id",
    }

    assert _final_qa_issue_signature(first) == _final_qa_issue_signature(second)


def test_final_qa_reuses_passed_phase_runtime_evidence() -> None:
    phase_evidence = {
        "enabled": True,
        "passed": True,
        "status": "passed",
        "artifact_sha256": "same-workspace",
    }
    ctx = type("Ctx", (), {})()
    ctx.qc_results = {
        "__whole_project__": {
            "qa": {"runtime_acceptance": {"passed": False, "status": "update_failed"}}
        },
        "phase-4": {"qa": {"runtime_acceptance": phase_evidence}},
    }

    assert _previous_passed_runtime_result(ctx) is phase_evidence


def test_runtime_acceptance_failure_becomes_blocking_rework_issue() -> None:
    issue = _runtime_acceptance_issue({
        "status": "build_failed",
        "summary": "npm test failed in backend/tests/tickets.test.js",
        "logs": [
            "snapshot pushed",
            "build: /app/backend/tests/tickets.test.js:1",
            "build: SyntaxError: Cannot use import statement outside a module",
            "build: Tests: 2 failed",
            "build: npm test exited 1",
        ],
    })

    assert issue["severity"] == "error"
    assert issue["status"] == "open"
    assert issue["layer"] == "runtime_acceptance"
    assert issue["file_path"] == "backend/tests/tickets.test.js"
    assert "Tests: 2 failed" in issue["message"]
    assert "SyntaxError" in issue["message"]
    assert "snapshot pushed" not in issue["message"]
    assert _has_blocking_qc_issues([
        {"severity": "error", "status": "fixed"},
    ]) is False
    assert _has_blocking_qc_issues([
        {"severity": "error", "status": "open"},
    ]) is True


def test_runtime_acceptance_issue_uses_compact_final_qa_contract() -> None:
    issue = _runtime_acceptance_issue(
        {
            "status": "build_failed",
            "summary": "Cannot find module foo",
            "logs": ["build: Error: Cannot find module foo"],
        },
        scope_files={"backend/app.py": {"agent_id": "agent-1"}},
        valid_criteria={"runtime.build"},
    )

    assert tuple(issue) == FINAL_QA_ISSUE_FIELDS
    assert issue["file"] is None
    assert issue["line"] is None
    assert issue["criterion"] == "runtime.build"
    assert issue["related_files"] == []
    assert len(issue["message"]) <= 160
    assert len(issue["expected"]) <= 120
    assert len(issue["actual"]) <= 240
    assert len(issue["fix"]) <= 200


def test_runtime_acceptance_issue_rejects_file_outside_final_qa_scope() -> None:
    issue = _runtime_acceptance_issue(
        {
            "status": "test_failed",
            "summary": "test failed",
            "file_path": "tests/obsolete.test.js",
            "logs": [],
        },
        scope_files={"backend/app.py": {"agent_id": "agent-1"}},
        valid_criteria={"runtime.test"},
    )

    assert issue["file"] is None
    assert issue["line"] is None


def test_runtime_acceptance_issue_keeps_file_inside_final_qa_scope() -> None:
    issue = _runtime_acceptance_issue(
        {
            "status": "build_failed",
            "summary": "TypeScript compilation failed",
            "file_path": "frontend/src/app.ts",
            "logs": ["build: frontend/src/app.ts:17 error TS2304"],
        },
        scope_files={"frontend/src/app.ts": {"agent_id": "agent-1"}},
        valid_criteria={"runtime.build"},
    )

    assert issue["file"] == "frontend/src/app.ts"
    assert issue["line"] == 17
    assert issue["criterion"] == "runtime.build"


def test_business_runtime_issue_preserves_exact_target_and_guidance() -> None:
    issue = _runtime_acceptance_issue({
        "status": "failed",
        "summary": "business API /api/dashboard/stats did not return JSON",
        "file_path": "backend/src/routes/dashboard.js",
        "fix_hint": "Make `/api/dashboard/stats` return dashboard statistics as JSON.",
    })

    assert issue["file_path"] == "backend/src/routes/dashboard.js"
    assert issue["fix_hint"] == (
        "Make `/api/dashboard/stats` return dashboard statistics as JSON."
    )


def test_infrastructure_failure_is_not_actionable_rework() -> None:
    assert _runtime_failure_actionable({
        "error_category": "infrastructure_unavailable",
        "actionable": False,
    }) is False
    assert _runtime_failure_actionable({
        "error_category": "infrastructure_transient",
        "actionable": False,
    }) is False


def test_runtime_issue_never_targets_generated_dependency_directories() -> None:
    issue = _runtime_acceptance_issue({
        "status": "build_failed",
        "summary": "dependency build failed",
        "file_path": "node_modules/native-addon/build/index.js",
        "logs": ["build: node_modules/native-addon/build/index.js:1 failed"],
    })

    assert "node_modules" not in issue["file_path"]
    assert "/dist/" not in f"/{issue['file_path']}/"


def test_external_runtime_issue_rewrites_final_qa_verdict() -> None:
    entry = {
        "passed": True,
        "status": "passed",
        "score": 100,
        "error_count": 0,
        "warning_count": 0,
        "issues_detail": [],
        "user_report": "**总体结论**：✅ 通过  |  **综合评分**：99/100\n\nStatic checks passed.",
    }
    issue = {
        "id": "runtime-1",
        "severity": "error",
        "status": "open",
        "message": "build failed",
    }

    _apply_final_qa_external_issues(entry, [issue])
    _apply_final_qa_external_issues(entry, [issue])

    assert entry["passed"] is False
    assert entry["status"] == "failed"
    assert entry["score"] == 90
    assert entry["error_count"] == 1
    assert len(entry["issues_detail"]) == 1
    assert "❌ 未通过" in entry["user_report"]
    assert "90/100" in entry["user_report"]
    assert "✅ 通过" not in entry["user_report"]


def test_final_qa_rework_issue_contains_machine_readable_target_path() -> None:
    text = _format_final_qa_rework_issue({
        "severity": "error",
        "message": "TypeScript build failed",
        "file_path": "frontend/src/App.tsx",
        "fix_hint": "Remove the unused interface",
    })

    assert "Target file: `frontend/src/App.tsx`" in text
    assert "Remove the unused interface" in text


def test_final_qa_rework_includes_dependencies_named_by_repair_guidance() -> None:
    issue = {
        "severity": "error",
        "message": "backend/src/index.js requires a missing application module",
        "file_path": "backend/src/index.js",
        "fix_hint": "Create backend/src/app.js with the Express application",
    }

    text = _format_final_qa_rework_issue(issue)

    assert "Target file: `backend/src/index.js`" in text
    assert "Related target file: `backend/src/app.js`" in text
    assert _final_qa_rework_scope({}, [issue]) == [
        "backend/src/index.js",
        "backend/src/app.js",
    ]


def test_final_qa_rework_leases_exact_issue_paths_instead_of_overlapping_agent_scopes() -> None:
    agent = {"allowed_path_prefixes": ["backend/"]}
    issues = [
        {"file_path": "backend/src/routes/auth.js"},
        {"file_path": "backend/src/routes/auth.js"},
        {"file_path": "node_modules/pkg/index.js"},
    ]

    assert _final_qa_rework_scope(agent, issues) == ["backend/src/routes/auth.js"]


def test_runtime_acceptance_routes_relative_jest_stack_to_backend_test() -> None:
    issue = _runtime_acceptance_issue({
        "status": "build_failed",
        "summary": "npm test failed",
        "logs": [
            "build: expect(res.data.data).toBeInstanceOf(Array);",
            "build: Received constructor: Array",
            "build: at Object.<anonymous> (tests/assets.test.js:198:27)",
            "build: Tests: 3 failed, 11 passed, 14 total",
        ],
    })

    assert issue["file_path"] == "backend/tests/assets.test.js"
    assert "Array.isArray" in issue["fix_hint"]


def test_runtime_acceptance_names_missing_relative_module_as_repair_target() -> None:
    issue = _runtime_acceptance_issue({
        "status": "update_failed",
        "summary": "Runtime deploy did not become live",
        "logs": [
            "build: Error: Cannot find module './app'",
            "build: at Object.<anonymous> (/app/backend/src/index.js:3:13)",
        ],
    })

    assert issue["file_path"] == "backend/src/index.js"
    assert "`backend/src/app.js`" in issue["fix_hint"]
    assert _final_qa_rework_scope({}, [issue]) == [
        "backend/src/index.js",
        "backend/src/app.js",
    ]


def test_runtime_acceptance_routes_container_build_failure_to_dockerfile() -> None:
    issue = _runtime_acceptance_issue({
        "status": "build_failed",
        "summary": "Runtime deploy did not become live",
        "logs": [
            'build: failed to solve: process "/bin/sh -c cd frontend && npm ci" '
            "did not complete successfully",
            "build: 8 | RUN cd frontend && npm ci",
        ],
    })

    assert issue["file_path"] == "Dockerfile"


def test_missing_release_dockerfile_enters_exact_rework_scope() -> None:
    issue = _runtime_acceptance_issue({
        "status": "failed",
        "summary": "workspace is missing the release Dockerfile",
        "error_category": "project_defect",
        "actionable": True,
        "logs": ["workspace is missing the release Dockerfile"],
    })

    assert issue["file_path"] == "Dockerfile"
    assert _final_qa_rework_scope({}, [issue]) == ["Dockerfile"]
    assert "Target file: `Dockerfile`" in _format_final_qa_rework_issue(issue)


def test_runtime_acceptance_routes_frontend_typescript_error_to_source_file() -> None:
    issue = _runtime_acceptance_issue({
        "status": "build_failed",
        "summary": "Runtime deploy did not become live",
        "logs": [
            "build: > tsc && vite build",
            "build: #15 1.666 src/App.tsx(38,10): error TS6133: 'user' is declared but its value is never read.",
            'build: process "/bin/sh -c npm run build --prefix frontend" exited 2',
        ],
    })

    assert issue["file_path"] == "frontend/src/App.tsx"


def test_runtime_acceptance_explains_http_server_request_api_misuse() -> None:
    issue = _runtime_acceptance_issue({
        "status": "update_failed",
        "summary": "runtime process exited",
        "logs": [
            "build: TypeError: req.connect is not a function",
            "build: at check (file:///app/integration/start.js:59:11)",
        ],
    })

    assert issue["file_path"] == "integration/start.js"
    assert "http.createServer" in issue["fix_hint"]
    assert "http.get" in issue["fix_hint"]


def test_runtime_acceptance_explains_missing_http_namespace_import() -> None:
    issue = _runtime_acceptance_issue({
        "status": "update_failed",
        "summary": "runtime process exited",
        "logs": [
            "build: ReferenceError: http is not defined",
            "build: at check (file:///app/integration/start.js:41:19)",
        ],
    })

    assert issue["file_path"] == "integration/start.js"
    assert "from `node:http`" in issue["fix_hint"]
    assert "call `get(url, callback)` directly" in issue["fix_hint"]


def test_runtime_acceptance_explains_authorization_mismatch_with_test_line() -> None:
    issue = _runtime_acceptance_issue({
        "status": "build_failed",
        "summary": "npm test failed",
        "logs": [
            "build: Expected: 200",
            "build: Received: 403",
            "build: at Object.toBe (tests/tickets.test.js:191:24)",
            "build: Tests: 1 failed, 48 passed, 49 total",
        ],
    })

    assert issue["file_path"] == "backend/tests/tickets.test.js"
    assert "tests/tickets.test.js:191:24" in issue["message"]
    assert "assign that ticket to the technician first" in issue["fix_hint"]


def test_final_qa_recognizes_single_file_html_delivery(tmp_path) -> None:
    (tmp_path / "index.html").write_text(
        "<!doctype html>\n<html>\n<body>\n<script>\n"
        + "\n".join(f"const value{i} = {i};" for i in range(60))
        + "\n</script>\n</body>\n</html>\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# delivery\n", encoding="utf-8")
    generated = tmp_path / "node_modules" / "cached"
    generated.mkdir(parents=True)
    (generated / "ignored.js").write_text("throw new Error('ignore me')\n", encoding="utf-8")

    source_files, total_lines = _scan_final_qa_source_files(tmp_path)

    assert source_files == ["index.html"]
    assert total_lines >= 60


def test_final_qa_can_reopen_agent_after_previous_fix_limit() -> None:
    agent = {"status": "fix_limit_reached"}

    transition_agent(agent, "fix_required", progress=0, message="new final QA cycle")

    assert agent["status"] == "fix_required"
    assert agent["progress"] == 0


def test_final_qa_rework_detects_returned_failures_and_invalid_results() -> None:
    failures = _final_qa_rework_failures([
        {"success": True, "status": "completed"},
        {"success": False, "status": "failed", "error": "delivery rejected"},
        RuntimeError("provider failed"),
        None,
    ])

    assert failures == ["delivery rejected", "provider failed", "invalid rework result: NoneType"]


def test_final_qa_does_not_treat_historical_execution_rows_as_current_receipts() -> None:
    ctx = type("Ctx", (), {})()
    ctx.project_id = "final-qa-historical-rows"
    ctx.workspace = Path(".")
    ctx.agents = {
        "completed": {"status": "completed"},
        "failed": {"status": "failed"},
        "working": {"status": "working"},
    }
    ctx.subprojects = [
        {"id": "phase-row", "status": "failed"},
        {"id": "sp-ok", "agent_id": "completed", "status": "completed"},
        {"id": "sp-bad", "agent_id": "failed", "status": "failed"},
    ]

    adjustments = routes_adjustments._get_adjustments(ctx.project_id)
    adjustments.clear()
    assert _final_qa_state_blockers(ctx) == []


def test_final_qa_does_not_use_historical_agent_progress_as_completion_gate() -> None:
    ctx = type("Ctx", (), {})()
    ctx.project_id = "final-qa-historical-progress"
    ctx.workspace = Path(".")
    ctx.agents = {"agent-1": {"status": "completed", "progress": 0}}
    ctx.subprojects = [{
        "id": "sp-1", "agent_id": "agent-1", "status": "completed", "progress": 100,
    }]

    adjustments = routes_adjustments._get_adjustments(ctx.project_id)
    adjustments.clear()
    assert _final_qa_state_blockers(ctx) == []


def test_later_qc_round_adds_new_deterministic_error() -> None:
    issue = {"severity": "error", "layer": "syntax"}

    assert _should_append_qc_issue(issue, is_first_qc=False) is True


def test_later_qc_round_adds_new_api_contract_error() -> None:
    issue = {"severity": "error", "layer": "api_contract"}

    assert _should_append_qc_issue(issue, is_first_qc=False) is True


def test_later_qc_round_adds_new_functionality_error() -> None:
    issue = {"severity": "error", "layer": "functionality"}

    assert _should_append_qc_issue(issue, is_first_qc=False) is True


def test_later_qc_round_does_not_expand_llm_warning_baseline() -> None:
    issue = {"severity": "warning", "layer": "functionality"}

    assert _should_append_qc_issue(issue, is_first_qc=False) is False


def test_recheck_refreshes_issue_file_and_owner() -> None:
    old = {"message": "missing dependency", "file_path": "tests/a.ts", "responsible_agent_id": "test-agent"}
    new = {"message": "missing dependency", "file_path": "package.json", "responsible_agent_id": "backend-agent"}

    refreshed = _refresh_issue_routing(old, new)

    assert refreshed["file_path"] == "package.json"
    assert refreshed["responsible_agent_id"] == "backend-agent"


def test_rerouted_duplicate_issues_are_aggregated() -> None:
    issues = [
        {"message": "missing supertest", "file_path": "backend/package.json", "id": "one"},
        {"message": "missing supertest", "file_path": "backend/package.json", "id": "two"},
    ]

    assert len(_deduplicate_routed_issues(issues)) == 1


def test_functionality_wording_and_line_changes_keep_stable_identity() -> None:
    first = {
        "layer": "functionality",
        "file_path": "Frontend\\src\\App.tsx",
        "message": "The delete button is not implemented at line 42",
    }
    rephrased = {
        "layer": "functionality",
        "file_path": "frontend/src/App.tsx",
        "message": "Delete button remains unimplemented on line 57",
    }

    assert _qc_issue_fingerprint(first) == _qc_issue_fingerprint(rephrased)
    assert _stable_qc_issue_id(first) == _stable_qc_issue_id(rephrased)
    assert len(_deduplicate_routed_issues([first, rephrased])) == 1


def test_deterministic_line_number_change_keeps_stable_identity() -> None:
    first = {
        "layer": "syntax",
        "file_path": "backend/app.py",
        "message": "Syntax error at line 12: unexpected token",
    }
    rechecked = {
        "layer": "syntax",
        "file_path": "backend/app.py",
        "message": "Unexpected token syntax error on line 18",
    }

    assert _qc_issue_fingerprint(first) == _qc_issue_fingerprint(rechecked)
    assert _stable_qc_issue_id(first) == _stable_qc_issue_id(rechecked)


def test_low_confidence_needs_manual_issue_requires_review_when_not_reproduced() -> None:
    old = {
        "id": "issue-original",
        "fingerprint": "functionality|frontend/src/app.tsx|button delete missing",
        "layer": "functionality",
        "file_path": "frontend/src/App.tsx",
        "message": "Delete button is not implemented at line 42",
        "status": "needs_manual",
        "detected_at": 123.0,
        "fix_rounds": 3,
    }

    closed = _reconcile_needs_manual_qc_issue(old, None)

    assert closed["status"] == "needs_manual"
    assert closed["id"] != "issue-original"
    assert closed["legacy_id"] == "issue-original"
    assert closed["detected_at"] == 123.0
    assert closed["fix_rounds"] == 3
    assert closed["identity_review_required"] is True


def test_needs_manual_issue_preserves_history_when_reproduced() -> None:
    old = {
        "id": "issue-original",
        "layer": "functionality",
        "file_path": "frontend/src/App.tsx",
        "message": "Delete button is not implemented at line 42",
        "status": "needs_manual",
        "detected_at": 123.0,
        "fix_rounds": 3,
    }
    reproduced = {
        "id": "issue-new",
        "layer": "functionality",
        "file_path": "frontend/src/App.tsx",
        "message": "Delete button remains unimplemented on line 57",
        "status": "open",
        "detected_at": 999.0,
        "fix_rounds": 0,
        "fix_hint": "Wire the click handler",
    }

    kept = _reconcile_needs_manual_qc_issue(old, reproduced)

    assert kept["status"] == "needs_manual"
    assert kept["id"] != "issue-original"
    assert kept["legacy_id"] == "issue-original"
    assert kept["detected_at"] == 123.0
    assert kept["fix_rounds"] == 3
    assert kept["fix_hint"] == "Wire the click handler"


def test_fixed_deterministic_regression_reopens() -> None:
    assert _should_reopen_fixed_issue({
        "severity": "error", "layer": "collaboration",
        "rule_id": "scope.owner_violation",
        "subject": "src/app.py owner assignment",
    }) is True
    assert _should_reopen_fixed_issue({"severity": "warning", "layer": "functionality"}) is False
