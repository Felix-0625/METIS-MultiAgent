import asyncio
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agents.fullstack_engineer_agent import FullStackEngineerAgent
from api import routes_engineer, routes_phases, routes_team
from core import app_state
from core.project_write_fence import ProjectWriteFenceConflict
from core.workspace_integrity import (
    compute_delivery_manifest,
    compute_workspace_digest,
    iter_integrity_files,
)


def _patch_engineer_route(monkeypatch, project, agent):
    # Most route tests exercise an already-actionable canonical defect.  A
    # rule/path bucket alone is intentionally low-confidence in production, so
    # give those fixtures an explicit stable subject instead of weakening the
    # identity gate.
    for value in getattr(project, "qc_results", {}).values():
        qa = value.get("qa", value) if isinstance(value, dict) else {}
        for issue in qa.get("issues_detail", []) if isinstance(qa, dict) else []:
            if (
                isinstance(issue, dict)
                and issue.get("rule_id")
                and not any(
                    issue.get(field)
                    for field in (
                        "symbol", "symbol_name", "subject", "endpoint",
                        "expected", "actual",
                    )
                )
                and issue.get("requires_identity_review") is not True
            ):
                issue["symbol"] = "test_actionable_target"

    async def persist():
        return None

    async def reinspection(_project_id, _ctx, _defect):
        return {"success": True, "status": {"status": "starting", "running": True}}

    monkeypatch.setattr(routes_engineer, "_get_project", lambda _project_id: project)
    monkeypatch.setattr(routes_engineer, "_get_engineer", lambda _project_id: agent)
    monkeypatch.setattr(routes_engineer, "_build_engineer_context", lambda *_args: None)
    monkeypatch.setattr(routes_engineer, "_persist_all_async", persist)
    monkeypatch.setattr(routes_engineer, "_start_authoritative_reinspection", reinspection)
    monkeypatch.setattr(
        routes_engineer,
        "_register_authoritative_reinspection",
        lambda *_args, **_kwargs: None,
    )
    default_target = Path(project.workspace) / "app.py"
    if default_target.is_file():
        agent._test_baseline_sha256 = hashlib.sha256(
            default_target.read_bytes()
        ).hexdigest()
        agent._test_baseline_artifact_sha256 = compute_delivery_manifest(
            Path(project.workspace)
        )["artifact_sha256"]


def _request(**overrides):
    values = {
        "defect_id": "issue-1",
        "file_path": "app.py",
        "new_content": "print('new')\n",
        "run_qa": True,
        "defect_info": {},
        "confirmed_plan_version": "plan-v1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _defect_id(issue):
    if (
        issue.get("rule_id")
        and not any(
            issue.get(field)
            for field in (
                "symbol", "symbol_name", "subject", "endpoint",
                "expected", "actual",
            )
        )
        and issue.get("requires_identity_review") is not True
    ):
        issue["symbol"] = "test_actionable_target"
    return routes_engineer.canonicalize_issue(issue)["defect_id"]


class _ConfirmedPlanMixin:
    def validate_confirmed_fix_plan(self, *_args):
        return True, ""

    def get_confirmed_fix_authorization(self, *_args):
        return {"mode": "whole_file"}

    def get_confirmed_fix_plan(self, *_args):
        return {
            "baseline_sha256": self._test_baseline_sha256,
            "target_baseline_artifact_sha256": (
                self._test_baseline_artifact_sha256
            ),
        }


def test_engineer_apply_fix_requires_final_qa(monkeypatch, tmp_path):
    target = tmp_path / "app.py"
    target.write_text("print('old')\n", encoding="utf-8")
    issue = {
        "rule_id": "python.output",
        "id": "issue-1",
        "status": "needs_manual",
        "file_path": "app.py",
        "symbol": "test_actionable_target",
    }
    project = SimpleNamespace(
        workspace=tmp_path,
        subprojects=[],
        qc_results={"__whole_project__": {"qa": {"issues_detail": [issue]}}},
    )

    class Agent(_ConfirmedPlanMixin):
        file_edit_counter = {}

        def apply_fix(self, **_kwargs):
            target.write_text("print('new')\n", encoding="utf-8")
            return {"success": True, "backup_path": ""}

    agent = Agent()
    _patch_engineer_route(monkeypatch, project, agent)
    result = asyncio.run(routes_engineer.engineer_apply_fix(
        "proj-1", _request(defect_id=_defect_id(issue))
    ))
    assert issue["status"] == "pending_verification"
    assert issue["repair_workspace_digest"] == compute_workspace_digest(tmp_path)
    assert result["requires_final_qa"] is True
    assert result["authoritative_reinspection"]["status"]["running"] is True


def test_engineer_uses_canonical_issue_id_for_listing_and_apply_fix(
    monkeypatch, tmp_path
):
    target = tmp_path / "app.py"
    target.write_text("print('old')\n", encoding="utf-8")
    issue = {
        "issue_id": "issue-stable",
        "id": "legacy-id",
        "fingerprint": "fingerprint-id",
        "rule_id": "python.output",
        "message": "broken",
        "file_path": "app.py",
        "status": "needs_manual",
    }
    project = SimpleNamespace(
        workspace=tmp_path,
        subprojects=[],
        qc_results={"__whole_project__": {"qa": {"issues_detail": [issue]}}},
    )

    class Agent(_ConfirmedPlanMixin):
        file_edit_counter = {}

        def apply_fix(self, **_kwargs):
            target.write_text("print('new')\n", encoding="utf-8")
            return {"success": True, "backup_path": ""}

    _patch_engineer_route(monkeypatch, project, Agent())
    listed = asyncio.run(routes_engineer.engineer_get_all_defects("proj-1"))
    canonical_id = listed["defects"][0]["id"]
    assert canonical_id.startswith("issue-")
    assert listed["defects"][0]["source_issue_id"] == "issue-stable"

    with pytest.raises(HTTPException) as legacy:
        asyncio.run(routes_engineer.engineer_apply_fix(
            "proj-1", _request(defect_id="issue-stable")
        ))
    assert legacy.value.status_code == 404

    request = _request(defect_id=canonical_id)
    asyncio.run(routes_engineer.engineer_apply_fix("proj-1", request))
    assert issue["status"] == "pending_verification"


def test_engineer_fallback_issue_id_is_deterministic():
    issue = {
        "message": "broken",
        "file_path": "src/app.py",
        "layer": "syntax",
        "line_no": 42,
    }
    assert routes_engineer._canonical_issue_id(issue) == (
        routes_engineer._canonical_issue_id(dict(issue))
    )
    assert routes_engineer._canonical_issue_id(issue).startswith("issue-")


def test_all_defects_prefers_latest_reopened_observation_over_verified(monkeypatch):
    old = {
        "id": "issue-1",
        "status": "verified",
        "file_path": "src/app.py",
        "message": "expected 200 actual 500",
    }
    reopened = {
        **old,
        "status": "open",
    }
    project = SimpleNamespace(
        workspace=Path("."),
        subprojects=[],
        qc_results={
            "old": {"qa": {"checked_at": 1, "issues_detail": [old]}},
            "new": {"qa": {"checked_at": 2, "issues_detail": [reopened]}},
        },
    )
    monkeypatch.setattr(routes_engineer, "_get_project", lambda _project_id: project)
    result = asyncio.run(routes_engineer.engineer_get_all_defects("proj-1"))
    assert result["total"] == 1
    assert result["defects"][0]["status"] == "open"


def test_all_defects_exposes_server_action_and_authoritative_identity(monkeypatch):
    issues = [
        {
            "rule_id": "python.output",
            "status": "open",
            "file_path": "src/open.py",
            "expected": "ok",
            "actual": "broken",
        },
        {
            "rule_id": "python.output",
            "status": "pending_verification",
            "file_path": "src/pending.py",
            "expected": "ok",
            "actual": "broken",
            "repair_artifact_digest": "artifact-1",
            "authoritative_reinspection_registration": {
                "supervisor_run_id": "run-1",
                "artifact_digest": "artifact-1",
            },
        },
        {
            "status": "needs_manual",
            "file_path": "src/identity.py",
            "message": "unstructured legacy warning",
        },
    ]
    project = SimpleNamespace(
        workspace=Path("."),
        subprojects=[],
        qc_results={
            "phase-1": {"qa": {"checked_at": 1, "issues_detail": issues}},
        },
    )
    monkeypatch.setattr(routes_engineer, "_get_project", lambda _project_id: project)
    result = asyncio.run(routes_engineer.engineer_get_all_defects("proj-1"))
    by_status = {item["status"]: item for item in result["defects"]}
    assert by_status["open"]["action_allowed"] is True
    assert by_status["open"]["blocked_reason"] == ""
    pending = by_status["pending_verification"]
    assert pending["action_allowed"] is False
    assert pending["blocked_reason"] == "authoritative_reinspection_pending"
    assert pending["authoritative_run_identity"] == {
        "kind": "supervisor",
        "registration_id": "",
        "run_id": "run-1",
        "artifact_sha256": "artifact-1",
    }
    identity_review = next(
        item for item in result["defects"]
        if item["file_path"] == "src/identity.py"
    )
    assert identity_review["action_allowed"] is False
    assert identity_review["blocked_reason"] == "identity_review_required"


def test_pending_verification_cannot_trigger_a_second_repair(monkeypatch, tmp_path):
    target = tmp_path / "app.py"
    target.write_text("print('repair')\n", encoding="utf-8")
    issue = {
        "rule_id": "python.output",
        "status": "pending_verification",
        "file_path": "app.py",
        "expected": "old",
        "actual": "broken",
    }
    project = SimpleNamespace(
        workspace=tmp_path,
        subprojects=[],
        qc_results={"phase-1": {"qa": {"issues_detail": [issue]}}},
    )

    class Agent(_ConfirmedPlanMixin):
        file_edit_counter = {}

        def apply_fix(self, **_kwargs):
            raise AssertionError("pending verification must not invoke apply_fix")

    _patch_engineer_route(monkeypatch, project, Agent())
    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_engineer.engineer_apply_fix(
            "proj-1", _request(defect_id=_defect_id(issue))
        ))
    assert caught.value.status_code == 409
    assert target.read_text(encoding="utf-8") == "print('repair')\n"


def test_engineer_backup_is_runner_owned_and_not_deliverable(tmp_path):
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("print('old')\n", encoding="utf-8")
    agent = FullStackEngineerAgent.__new__(FullStackEngineerAgent)
    agent.workspace = tmp_path
    agent.file_edit_counter = {}
    agent.file_last_score = {}
    agent.pending_fixes = {}
    agent.repair_history = {}
    agent.FILE_EDIT_LIMIT = 3
    agent._quick_static_check = lambda *_args: {"passed": True, "score": 100}
    agent.add_project_memory = lambda *_args, **_kwargs: None
    result = agent.apply_fix(
        defect_id="issue-1", file_path="src/app.py", new_content="print('new')\n",
    )
    backup = Path(result["backup_path"])
    assert result["success"] is True
    assert backup.relative_to(tmp_path).parts[:2] == (".project", "backups")
    assert backup.read_text(encoding="utf-8") == "print('old')\n"
    assert all(".project" not in path for path, _ in iter_integrity_files(tmp_path))


@pytest.mark.parametrize(
    ("issue", "request_overrides", "expected_status"),
    [
        (None, {}, 404),
        (
            {"id": "issue-1", "status": "needs_manual", "file_path": "src/app.py"},
            {"file_path": "src/other.py"},
            409,
        ),
        (
            {"id": "issue-1", "status": "verified", "file_path": "src/app.py"},
            {"file_path": "src/app.py"},
            409,
        ),
    ],
)
def test_apply_fix_rejects_unknown_verified_and_wrong_file(
    monkeypatch, tmp_path, issue, request_overrides, expected_status
):
    issues = [] if issue is None else [issue]
    project = SimpleNamespace(
        workspace=tmp_path,
        subprojects=[],
        qc_results={"__whole_project__": {"qa": {"issues_detail": issues}}},
    )

    class Agent(_ConfirmedPlanMixin):
        file_edit_counter = {}

    _patch_engineer_route(monkeypatch, project, Agent())
    if issue is not None:
        request_overrides = {
            "defect_id": _defect_id(issue),
            **request_overrides,
        }
    request = _request(**request_overrides)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_engineer.engineer_apply_fix("proj-1", request))
    assert caught.value.status_code == expected_status


def test_apply_fix_rejects_unconfirmed_plan_version(monkeypatch, tmp_path):
    issue = {"id": "issue-1", "status": "needs_manual", "file_path": "app.py"}
    project = SimpleNamespace(
        workspace=tmp_path,
        subprojects=[],
        qc_results={"__whole_project__": {"qa": {"issues_detail": [issue]}}},
    )

    class Agent:
        file_edit_counter = {}

        def validate_confirmed_fix_plan(self, *_args):
            return False, "整改方案版本缺失、过期或不匹配"

        def get_confirmed_fix_authorization(self, *_args):
            return {}

    _patch_engineer_route(monkeypatch, project, Agent())
    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_engineer.engineer_apply_fix(
            "proj-1", _request(defect_id=_defect_id(issue))
        ))
    assert caught.value.status_code == 409


def test_apply_fix_rejects_file_changed_after_plan_confirmation(
    monkeypatch, tmp_path
):
    target = tmp_path / "app.py"
    target.write_text("print('baseline')\n", encoding="utf-8")
    issue = {
        "rule_id": "python.output",
        "status": "needs_manual",
        "file_path": "app.py",
        "expected": "baseline",
        "actual": "broken",
    }
    project = SimpleNamespace(
        workspace=tmp_path,
        subprojects=[],
        qc_results={"__whole_project__": {"qa": {"issues_detail": [issue]}}},
    )

    class Agent(_ConfirmedPlanMixin):
        file_edit_counter = {}

        def apply_fix(self, **_kwargs):
            raise AssertionError("baseline CAS must reject before apply_fix")

    agent = Agent()
    _patch_engineer_route(monkeypatch, project, agent)
    target.write_text("print('changed-after-confirm')\n", encoding="utf-8")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_engineer.engineer_apply_fix(
            "proj-1", _request(defect_id=_defect_id(issue))
        ))
    assert caught.value.status_code == 409
    assert "baseline" in str(caught.value.detail)


def test_llm_receives_complete_long_file_without_4000_character_truncation(tmp_path):
    marker = "TAIL-MARKER-MUST-BE-PRESENT"
    old_content = ("value = 1\n" * 700) + marker + "\n"
    captured = {}

    class Hermes:
        def chat(self, messages):
            captured["content"] = messages[-1].content
            return {"content": old_content}

    agent = FullStackEngineerAgent.__new__(FullStackEngineerAgent)
    agent.workspace = tmp_path
    agent.hermes = Hermes()
    agent.project_background = ""
    result = agent._llm_generate_fixed_content(
        file_path="large.py",
        old_content=old_content,
        defect_info={"line_no": 700, "message": "fix tail"},
        repair_history=[],
    )
    assert marker in captured["content"]
    assert result == old_content.strip()


def test_chat_magic_string_never_confirms_a_fix_plan(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("print('old')\n", encoding="utf-8")

    class Hermes:
        def chat(self, _messages):
            return {"content": "方案内容\n【整改方案已确认】"}

    agent = FullStackEngineerAgent.__new__(FullStackEngineerAgent)
    agent.workspace = tmp_path
    agent.hermes = Hermes()
    agent.project_background = ""
    agent.file_edit_counter = {}
    agent.repair_history = {}
    agent.pending_fix_proposals = {}
    agent.confirmed_fix_plans = {}
    agent.FILE_EDIT_LIMIT = 3
    agent._find_callers = lambda *_args: []
    agent._build_memory_system_prompt = lambda: ""
    result = agent.chat_repair(
        "canonical-1",
        "请给出方案",
        defect_info={
            "defect_id": "canonical-1",
            "file_path": "app.py",
            "line_no": 1,
            "observation_id": "observation-1",
        },
        all_defects=[],
    )
    assert result["confirmed"] is False
    assert result["proposal_digest"]
    assert agent.confirmed_fix_plans == {}


def test_chat_route_binds_proposal_digest_to_artifact_actor_scope_and_generation(
    monkeypatch, tmp_path
):
    target = tmp_path / "app.py"
    target.write_text("print('old')\n", encoding="utf-8")
    issue = {
        "rule_id": "python.output",
        "status": "needs_manual",
        "file_path": "app.py",
        "expected": "old",
        "actual": "broken",
    }
    canonical = routes_engineer.canonicalize_issue(issue)
    project = SimpleNamespace(
        workspace=tmp_path,
        owner_user_id="owner-7",
        subprojects=[],
        qc_results={"phase-1": {"qa": {"issues_detail": [issue]}}},
    )

    class Agent:
        pending_fix_proposals = {}

        def chat_repair(self, **_kwargs):
            old_digest = "unbound-proposal"
            self.pending_fix_proposals[old_digest] = {
                "proposal_digest": old_digest,
                "defect_id": canonical["defect_id"],
                "file_path": "app.py",
                "issue_version": canonical["observation_id"],
                "baseline_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "reply": "replace output",
                "authorization": {
                    "mode": "scoped",
                    "line_start": 1,
                    "line_end": 1,
                },
            }
            return {"success": True, "proposal_digest": old_digest}

    agent = Agent()
    _patch_engineer_route(monkeypatch, project, agent)
    response = asyncio.run(routes_engineer.engineer_chat_repair(
        "proj-1",
        SimpleNamespace(
            defect_id=canonical["defect_id"],
            message="propose",
            defect_info=None,
            all_defects=None,
        ),
    ))
    digest = response["proposal_digest"]
    assert digest != "unbound-proposal"
    assert set(agent.pending_fix_proposals) == {digest}
    proposal = agent.pending_fix_proposals[digest]
    assert proposal["proposal_digest"] == digest
    assert proposal["proposal_generation"] == digest
    assert proposal["actor"] == "owner-7"
    assert proposal["scope"] == proposal["authorization"]
    assert proposal["target_baseline_file_sha256"] == hashlib.sha256(
        target.read_bytes()
    ).hexdigest()
    assert proposal["target_baseline_artifact_sha256"] == (
        compute_delivery_manifest(tmp_path)["artifact_sha256"]
    )


def test_whole_file_requires_explicit_confirmation_and_js_symbol_needs_scope():
    agent = FullStackEngineerAgent.__new__(FullStackEngineerAgent)
    agent.pending_fix_proposals = {
        "proposal-1": {
            "defect_id": "issue-1",
            "file_path": "app.txt",
            "issue_version": "observation-1",
            "baseline_sha256": "abc",
            "authorization": {"mode": "whole_file", "valid": True},
            "confirmable": True,
        }
    }
    agent.confirmed_fix_plans = {}
    agent.add_project_memory = lambda *_args, **_kwargs: None
    rejected = agent.confirm_fix_proposal(
        defect_id="issue-1",
        proposal_digest="proposal-1",
        issue_version="observation-1",
        file_path="app.txt",
        baseline_sha256="abc",
        allow_whole_file=False,
    )
    assert rejected["success"] is False
    accepted = agent.confirm_fix_proposal(
        defect_id="issue-1",
        proposal_digest="proposal-1",
        issue_version="observation-1",
        file_path="app.txt",
        baseline_sha256="abc",
        allow_whole_file=True,
    )
    assert accepted["success"] is True
    js_authorization = agent._build_fix_authorization({
        "file_path": "src/client.ts",
        "symbol": "request",
    })
    assert js_authorization["valid"] is False


def test_confirm_fix_plan_route_binds_observation_path_and_baseline(
    monkeypatch, tmp_path
):
    target = tmp_path / "app.py"
    target.write_text("print('old')\n", encoding="utf-8")
    issue = {
        "rule_id": "python.output",
        "status": "needs_manual",
        "file_path": "app.py",
        "line_no": 1,
        "expected": "old",
        "actual": "broken",
    }
    canonical = routes_engineer.canonicalize_issue(issue)
    project = SimpleNamespace(
        workspace=tmp_path,
        subprojects=[],
        qc_results={"__whole_project__": {"qa": {"issues_detail": [issue]}}},
    )
    agent = FullStackEngineerAgent.__new__(FullStackEngineerAgent)
    agent.pending_fix_proposals = {
        "proposal-1": {
            "defect_id": canonical["defect_id"],
            "file_path": "app.py",
            "issue_version": canonical["observation_id"],
            "baseline_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "target_baseline_artifact_sha256": compute_delivery_manifest(
                tmp_path
            )["artifact_sha256"],
            "proposal_digest": "proposal-1",
            "actor": "owner-1",
            "scope": {
                "mode": "scoped",
                "line_start": 1,
                "line_end": 1,
                "diff_budget": 3,
                "valid": True,
            },
            "proposal_generation": "proposal-1",
            "authorization": {
                "mode": "scoped",
                "line_start": 1,
                "line_end": 1,
                "diff_budget": 3,
                "valid": True,
            },
            "confirmable": True,
        }
    }
    agent.confirmed_fix_plans = {}
    agent.add_project_memory = lambda *_args, **_kwargs: None
    _patch_engineer_route(monkeypatch, project, agent)
    request = SimpleNamespace(
        defect_id=canonical["defect_id"],
        proposal_digest="proposal-1",
        observation_id=canonical["observation_id"],
        file_path="app.py",
        allow_whole_file=False,
    )
    result = asyncio.run(
        routes_engineer.engineer_confirm_fix_plan("proj-1", request)
    )
    assert result["success"] is True
    assert result["confirmed_plan_version"]
    confirmed = agent.confirmed_fix_plans[canonical["defect_id"]]
    assert confirmed["confirmation_digest"] == result["confirmed_plan_version"]
    assert confirmed["confirmation_generation"] == result["confirmed_plan_version"]
    assert confirmed["target_baseline_artifact_sha256"] == (
        compute_delivery_manifest(tmp_path)["artifact_sha256"]
    )
    assert confirmed["target_baseline_file_sha256"] == hashlib.sha256(
        target.read_bytes()
    ).hexdigest()
    assert confirmed["actor"] == "project-owner"
    assert confirmed["scope"] == confirmed["authorization"]


def test_chat_source_window_covers_real_line_500():
    content = "\n".join(f"line-{index}" for index in range(1, 701))
    window, start, end = FullStackEngineerAgent._defect_source_window(
        content, {"line_no": 500}
    )
    assert start < 500 < end
    assert "line-500" in window
    assert "line-1\n" not in window


def test_apply_fix_rejects_truncated_long_file_and_preserves_original(tmp_path):
    target = tmp_path / "large.py"
    original = "\n".join(f"value_{index} = {index}" for index in range(200)) + "\n"
    target.write_text(original, encoding="utf-8")
    agent = FullStackEngineerAgent.__new__(FullStackEngineerAgent)
    agent.workspace = tmp_path
    agent.file_edit_counter = {}
    agent.file_last_score = {}
    agent.pending_fixes = {}
    agent.repair_history = {}
    agent.FILE_EDIT_LIMIT = 3
    agent._quick_static_check = lambda *_args: {"passed": True, "score": 100}
    agent.add_project_memory = lambda *_args, **_kwargs: None
    result = agent.apply_fix(
        defect_id="issue-1",
        file_path="large.py",
        new_content="value_0 = 999\n",
    )
    assert result["success"] is False
    assert result["preflight_failed"] is True
    assert target.read_text(encoding="utf-8") == original


def test_replacement_contract_enforces_confirmed_line_scope(tmp_path):
    agent = FullStackEngineerAgent.__new__(FullStackEngineerAgent)
    old = "\n".join(f"value_{index} = {index}" for index in range(10)) + "\n"
    changed = old.replace("value_8 = 8", "value_8 = 900")
    result = agent._validate_replacement_contract(
        str(tmp_path / "app.py"),
        old,
        changed,
        fix_authorization={
            "mode": "scoped",
            "line_start": 2,
            "line_end": 2,
            "diff_budget": 1,
        },
    )
    assert result["passed"] is False
    assert any("超出已确认的行范围" in issue for issue in result["issues"])


def test_engineer_run_qa_delegates_once_without_independent_observation(
    monkeypatch, tmp_path
):
    project = SimpleNamespace(workspace=tmp_path, subprojects=[], qc_results={})
    calls = []

    class Agent:
        def run_qa_inspection(self, **_kwargs):
            raise AssertionError("independent engineer QA must never run")

    async def reinspection(project_id, ctx, defect):
        calls.append((project_id, ctx, defect))
        return {"success": True, "status": {"status": "starting"}}

    monkeypatch.setattr(routes_engineer, "_get_project", lambda _project_id: project)
    monkeypatch.setattr(routes_engineer, "_get_engineer", lambda _project_id: Agent())
    monkeypatch.setattr(routes_engineer, "_build_engineer_context", lambda *_args: None)
    monkeypatch.setattr(routes_engineer, "_start_authoritative_reinspection", reinspection)
    request = SimpleNamespace(
        subproject_id="phase-1",
        output_files=["ignored.py"],
        subproject_description="ignored",
        subproject_name="ignored",
        is_final_phase=False,
    )
    result = asyncio.run(routes_engineer.engineer_run_qa("proj-1", request))
    assert result["success"] is True
    assert len(calls) == 1
    assert calls[0][2]["detected_phase"] == "phase-1"


def test_apply_fix_returns_409_when_project_write_fence_is_active(
    monkeypatch, tmp_path
):
    issue = {"id": "issue-1", "status": "needs_manual", "file_path": "app.py"}
    project = SimpleNamespace(
        workspace=tmp_path,
        subprojects=[],
        qc_results={"__whole_project__": {"qa": {"issues_detail": [issue]}}},
    )

    class Agent(_ConfirmedPlanMixin):
        file_edit_counter = {}

    def blocked_guard(*_args, **_kwargs):
        raise ProjectWriteFenceConflict("Final QA active")

    _patch_engineer_route(monkeypatch, project, Agent())
    monkeypatch.setattr(routes_engineer, "project_write_guard", blocked_guard)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_engineer.engineer_apply_fix(
            "proj-1", _request(defect_id=_defect_id(issue))
        ))
    assert caught.value.status_code == 409
    assert "Final QA active" in str(caught.value.detail)


def test_failed_authoritative_schedule_restores_file_issue_and_counter(
    monkeypatch, tmp_path
):
    target = tmp_path / "app.py"
    target.write_text("print('old')\n", encoding="utf-8")
    issue = {
        "rule_id": "python.output",
        "id": "issue-1",
        "status": "needs_manual",
        "file_path": "app.py",
    }
    project = SimpleNamespace(
        workspace=tmp_path,
        subprojects=[],
        qc_results={"__whole_project__": {"qa": {"issues_detail": [issue]}}},
    )
    agent = FullStackEngineerAgent.__new__(FullStackEngineerAgent)
    agent.workspace = tmp_path
    agent.file_edit_counter = {}
    agent.file_last_score = {}
    agent.pending_fixes = {}
    agent.repair_history = {}
    canonical_id = _defect_id(issue)
    agent.confirmed_fix_plans = {
        canonical_id: {
            "file_path": "app.py",
            "version": "plan-v1",
            "issue_version": routes_engineer.canonicalize_issue(issue)["observation_id"],
            "authorization": {"mode": "whole_file"},
            "baseline_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "target_baseline_artifact_sha256": compute_delivery_manifest(
                tmp_path
            )["artifact_sha256"],
        }
    }
    agent.FILE_EDIT_LIMIT = 3
    agent._quick_static_check = lambda *_args: {"passed": True, "score": 100}
    agent.add_project_memory = lambda *_args, **_kwargs: None

    scheduling_attempts = 0

    async def flaky_reinspection(*_args):
        nonlocal scheduling_attempts
        scheduling_attempts += 1
        if scheduling_attempts == 1:
            raise HTTPException(status_code=409, detail="QA run conflict")
        return {"success": True, "status": {"status": "starting", "running": True}}

    _patch_engineer_route(monkeypatch, project, agent)
    monkeypatch.setattr(
        routes_engineer, "_start_authoritative_reinspection", flaky_reinspection
    )
    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_engineer.engineer_apply_fix(
            "proj-1", _request(defect_id=canonical_id)
        ))
    assert caught.value.status_code == 409
    assert target.read_text(encoding="utf-8") == "print('old')\n"
    assert issue == {
        "rule_id": "python.output",
        "id": "issue-1",
        "status": "needs_manual",
        "file_path": "app.py",
        "symbol": "test_actionable_target",
    }
    assert str(target.resolve()) not in agent.file_edit_counter
    retry = asyncio.run(routes_engineer.engineer_apply_fix(
        "proj-1", _request(defect_id=canonical_id)
    ))
    assert retry["success"] is True
    assert scheduling_attempts == 2
    assert issue["status"] == "pending_verification"


def test_phase_reinspection_schedule_failure_restores_supervisor_ownership(
    monkeypatch, tmp_path
):
    phase_id = "phase-1"
    key = f"proj-1-{phase_id}"
    phase = {"id": phase_id, "status": "waiting_engineer"}
    prior_run = {"run_id": "run-old", "status": "waiting_engineer"}
    prior_state = {"status": "awaiting_manual_fix", "running": False}
    prior_config = {"provider": "old"}
    ctx = SimpleNamespace(
        project_id="proj-1",
        workspace=tmp_path,
        supervisor_quality_runs={phase_id: dict(prior_run)},
    )

    class PhaseManager:
        def get_phase(self, wanted):
            return phase if wanted == phase_id else None

    class Machine:
        state = "waiting_engineer"

        def __init__(self):
            self.payload = {
                "run_id": "run-old",
                "status": "waiting_engineer",
                "scope": {},
            }

        def to_dict(self):
            return dict(self.payload)

    machine = Machine()
    monkeypatch.setitem(routes_phases._phase_managers, "proj-1", PhaseManager())
    monkeypatch.setitem(routes_phases._auto_repair_states, key, dict(prior_state))
    monkeypatch.setitem(
        routes_phases._auto_repair_api_configs, key, dict(prior_config)
    )
    monkeypatch.setattr(
        routes_phases,
        "_supervisor_scope_snapshot",
        lambda *_args: {"artifact_digest": "artifact-1"},
    )
    monkeypatch.setattr(
        routes_phases, "_supervisor_quality_machine", lambda *_args: machine
    )

    def prepare(_ctx, _phase_id, prepared_machine):
        prepared_machine.payload["scope"] = {
            "artifact_digest": "artifact-1",
            "scope_digest": "scope-1",
        }

    def store(store_ctx, store_phase, stored_machine, state):
        payload = stored_machine.to_dict()
        payload["status"] = "verifying"
        store_ctx.supervisor_quality_runs[store_phase] = payload
        state["supervisor_run"] = payload
        return payload

    monkeypatch.setattr(routes_phases, "_prepare_supervisor_verification", prepare)
    monkeypatch.setattr(routes_phases, "_store_supervisor_quality_machine", store)
    monkeypatch.setattr(routes_phases, "_persist_all", lambda: None)
    monkeypatch.setattr(
        routes_phases,
        "_safe_create_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("schedule")),
    )

    with pytest.raises(RuntimeError, match="schedule"):
        routes_phases.register_authoritative_reinspection(
            ctx, phase_id, "artifact-1"
        )
    assert ctx.supervisor_quality_runs[phase_id] == prior_run
    assert routes_phases._auto_repair_states[key] == prior_state
    assert routes_phases._auto_repair_api_configs[key] == prior_config
    assert phase == {"id": phase_id, "status": "waiting_engineer"}


def test_failed_schedule_does_not_rollback_over_later_workspace_write(
    monkeypatch, tmp_path
):
    target = tmp_path / "app.py"
    target.write_text("print('old')\n", encoding="utf-8")
    issue = {
        "rule_id": "python.output",
        "id": "legacy-1",
        "status": "needs_manual",
        "file_path": "app.py",
        "expected": "old",
        "actual": "broken",
    }
    canonical_id = _defect_id(issue)
    project = SimpleNamespace(
        workspace=tmp_path,
        subprojects=[],
        qc_results={"__whole_project__": {"qa": {"issues_detail": [issue]}}},
    )

    class Agent(_ConfirmedPlanMixin):
        file_edit_counter = {}
        file_last_score = {}
        pending_fixes = {}

        def apply_fix(self, **_kwargs):
            target.write_text("print('repair')\n", encoding="utf-8")
            return {"success": True, "backup_path": ""}

    async def failed_after_late_write(*_args):
        late = tmp_path / "frontend" / "src" / "data" / "client.ts"
        late.parent.mkdir(parents=True)
        late.write_text("do not overwrite\n", encoding="utf-8")
        raise HTTPException(status_code=409, detail="QA scheduling failed")

    _patch_engineer_route(monkeypatch, project, Agent())
    monkeypatch.setattr(
        routes_engineer,
        "_start_authoritative_reinspection",
        failed_after_late_write,
    )
    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_engineer.engineer_apply_fix(
            "proj-1", _request(defect_id=canonical_id)
        ))
    assert caught.value.status_code == 409
    assert target.read_text(encoding="utf-8") == "print('repair')\n"
    assert (
        tmp_path / "frontend" / "src" / "data" / "client.ts"
    ).read_text(encoding="utf-8") == "do not overwrite\n"
    assert issue["status"] == "needs_manual"
    assert "rollback_refused_artifact_digest" in issue
    assert "rollback_refused_target_sha256" in issue


def test_confirmed_plan_restores_from_app_state_and_can_apply(
    monkeypatch, tmp_path
):
    target = tmp_path / "app.py"
    target.write_text("print('old')\n", encoding="utf-8")
    issue = {
        "rule_id": "python.output",
        "status": "needs_manual",
        "file_path": "app.py",
        "expected": "old",
        "actual": "broken",
    }
    canonical = routes_engineer.canonicalize_issue(issue)
    artifact = compute_delivery_manifest(tmp_path)["artifact_sha256"]
    project = SimpleNamespace(
        project_id="proj-restore",
        workspace=tmp_path,
        owner_user_id="owner-1",
        subprojects=[],
        qc_results={"phase-1": {"qa": {"issues_detail": [issue]}}},
    )

    original = FullStackEngineerAgent.__new__(FullStackEngineerAgent)
    original.project_id = "proj-restore"
    original.workspace = tmp_path
    original.project_background = ""
    original.final_plan = None
    original.phase_info = []
    original.qa_history = []
    original.doc_history = []
    original.repair_history = {}
    original.archive_result = None
    original.pending_fix_proposals = {
        "proposal-1": {
            "proposal_digest": "proposal-1",
            "defect_id": canonical["defect_id"],
            "file_path": "app.py",
            "issue_version": canonical["observation_id"],
            "baseline_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "target_baseline_file_sha256": hashlib.sha256(
                target.read_bytes()
            ).hexdigest(),
            "target_baseline_artifact_sha256": artifact,
            "actor": "owner-1",
            "scope": {"mode": "whole_file"},
            "proposal_generation": "proposal-1",
        }
    }
    original.confirmed_fix_plans = {
        canonical["defect_id"]: {
            **original.pending_fix_proposals["proposal-1"],
            "authorization": {"mode": "whole_file"},
            "version": "plan-v1",
            "confirmation_digest": "plan-v1",
            "confirmation_generation": "plan-v1",
        }
    }
    monkeypatch.setattr(
        routes_team, "_engineer_agents", {"proj-restore": original}
    )
    monkeypatch.setattr(app_state, "projects", {"proj-restore": project})
    saved = app_state._persistable_engineer_agents()

    restored = FullStackEngineerAgent.__new__(FullStackEngineerAgent)
    restored.project_id = "proj-restore"
    restored.workspace = tmp_path
    restored.project_background = ""
    restored.final_plan = None
    restored.phase_info = []
    restored.qa_history = []
    restored.doc_history = []
    restored.repair_history = {}
    restored.archive_result = None
    restored.pending_fix_proposals = {}
    restored.confirmed_fix_plans = {}
    restored.file_edit_counter = {}
    restored.file_last_score = {}
    restored.pending_fixes = {}
    restored.FILE_EDIT_LIMIT = 3
    restored._quick_static_check = lambda *_args: {"passed": True, "score": 100}
    restored.add_project_memory = lambda *_args, **_kwargs: None

    def factory(project_id):
        assert project_id == "proj-restore"
        routes_team._engineer_agents[project_id] = restored
        return restored

    monkeypatch.setattr(routes_team, "_get_engineer", factory)
    monkeypatch.setattr(routes_team, "_build_engineer_context", lambda *_args: None)
    assert app_state._restore_engineer_agents(saved) == 1
    record = restored.confirmed_fix_plans[canonical["defect_id"]]
    assert record["confirmation_digest"] == "plan-v1"
    assert record["target_baseline_artifact_sha256"] == artifact
    assert record["target_baseline_file_sha256"] == hashlib.sha256(
        target.read_bytes()
    ).hexdigest()
    assert record["actor"] == "owner-1"
    assert record["scope"] == {"mode": "whole_file"}

    _patch_engineer_route(monkeypatch, project, restored)
    result = asyncio.run(routes_engineer.engineer_apply_fix(
        "proj-restore",
        _request(
            defect_id=canonical["defect_id"],
            confirmed_plan_version="plan-v1",
        ),
    ))
    assert result["success"] is True
    assert issue["status"] == "pending_verification"


def test_apply_rejects_unrelated_artifact_drift_after_confirmation(
    monkeypatch, tmp_path
):
    target = tmp_path / "app.py"
    target.write_text("print('old')\n", encoding="utf-8")
    issue = {
        "rule_id": "python.output",
        "status": "needs_manual",
        "file_path": "app.py",
        "expected": "old",
        "actual": "broken",
    }
    project = SimpleNamespace(
        workspace=tmp_path,
        subprojects=[],
        qc_results={"phase-1": {"qa": {"issues_detail": [issue]}}},
    )

    class Agent(_ConfirmedPlanMixin):
        file_edit_counter = {}

        def apply_fix(self, **_kwargs):
            raise AssertionError("artifact CAS must reject before apply_fix")

    agent = Agent()
    _patch_engineer_route(monkeypatch, project, agent)
    unrelated = tmp_path / "src" / "unrelated.py"
    unrelated.parent.mkdir()
    unrelated.write_text("changed = True\n", encoding="utf-8")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_engineer.engineer_apply_fix(
            "proj-1", _request(defect_id=_defect_id(issue))
        ))
    assert caught.value.status_code == 409
    assert "artifact" in str(caught.value.detail)
    assert target.read_text(encoding="utf-8") == "print('old')\n"


def test_low_confidence_identity_review_survives_restart_and_becomes_actionable(
    monkeypatch, tmp_path
):
    target = tmp_path / "app.py"
    target.write_text("print('old')\n", encoding="utf-8")
    issue = {
        "id": "legacy-low",
        "status": "needs_manual",
        "file_path": "app.py",
        "message": "output mismatch",
        "needs_manual_reason": "automatic identity was ambiguous",
    }
    old_id = _defect_id(issue)
    project = SimpleNamespace(
        workspace=tmp_path,
        owner_user_id="owner-1",
        subprojects=[],
        qc_results={"phase-1": {"qa": {"issues_detail": [issue]}}},
    )

    class Agent:
        pending_fix_proposals = {
            "old-proposal": {"defect_id": old_id},
        }
        confirmed_fix_plans = {old_id: {"version": "old-plan"}}

    agent = Agent()
    _patch_engineer_route(monkeypatch, project, agent)
    request = SimpleNamespace(
        rule_id="python.output",
        symbol="render_output",
        location="app.py:1",
        expected="old",
        actual="broken",
        review_reason="manually matched the failing symbol",
    )
    response = asyncio.run(routes_engineer.engineer_complete_identity_review(
        "proj-1", old_id, request
    ))
    reviewed = response["defect"]
    assert reviewed["defect_id"] != old_id
    assert old_id in reviewed["identity_aliases"]
    assert reviewed["observation_history"][-1]["defect_id"] == old_id
    assert reviewed["identity_confidence"] == "high"
    assert reviewed["requires_identity_review"] is False
    assert reviewed["action_allowed"] is True
    assert agent.pending_fix_proposals == {}
    assert old_id not in agent.confirmed_fix_plans

    restarted_project = SimpleNamespace(
        workspace=tmp_path,
        owner_user_id="owner-1",
        subprojects=[],
        qc_results=copy.deepcopy(project.qc_results),
    )
    monkeypatch.setattr(
        routes_engineer, "_get_project", lambda _project_id: restarted_project
    )
    listed = asyncio.run(routes_engineer.engineer_get_all_defects("proj-1"))
    assert listed["total"] == 1
    restored = listed["defects"][0]
    assert restored["defect_id"] == reviewed["defect_id"]
    assert restored["action_allowed"] is True
    assert old_id in restored["identity_aliases"]
    assert restored["observation_history"][-1]["observation_id"]


def test_two_concurrent_apply_requests_allow_exactly_one_transaction(
    monkeypatch, tmp_path
):
    target = tmp_path / "app.py"
    target.write_text("print('old')\n", encoding="utf-8")
    issue = {
        "rule_id": "python.output",
        "status": "needs_manual",
        "file_path": "app.py",
        "expected": "old",
        "actual": "broken",
    }
    project = SimpleNamespace(
        workspace=tmp_path,
        subprojects=[],
        qc_results={"phase-1": {"qa": {"issues_detail": [issue]}}},
    )

    class Agent(_ConfirmedPlanMixin):
        file_edit_counter = {}
        file_last_score = {}
        pending_fixes = {}
        apply_calls = 0

        def apply_fix(self, **_kwargs):
            self.apply_calls += 1
            target.write_text("print('repair')\n", encoding="utf-8")
            return {"success": True, "backup_path": ""}

    agent = Agent()
    _patch_engineer_route(monkeypatch, project, agent)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def held_reinspection(*_args):
        entered.set()
        await release.wait()
        return {"success": True, "status": {"status": "starting", "running": True}}

    monkeypatch.setattr(
        routes_engineer, "_start_authoritative_reinspection", held_reinspection
    )

    async def scenario():
        request = _request(defect_id=_defect_id(issue))
        first = asyncio.create_task(
            routes_engineer.engineer_apply_fix("proj-1", request)
        )
        await entered.wait()
        with pytest.raises(HTTPException) as second:
            await routes_engineer.engineer_apply_fix("proj-1", request)
        release.set()
        first_result = await first
        return first_result, second.value

    first_result, second_error = asyncio.run(scenario())
    assert first_result["success"] is True
    assert second_error.status_code == 409
    assert agent.apply_calls == 1
    assert target.read_text(encoding="utf-8") == "print('repair')\n"


def test_build_engineer_context_includes_locked_contract_and_whole_project_qa(
    monkeypatch, tmp_path
):
    final_plan = {
        "project_overview": "project",
        "core_features": [],
        "tech_stack": {},
    }
    project = SimpleNamespace(
        description="project",
        subprojects=[{
            "id": "sp-1",
            "name": "API",
            "status": "completed",
            "locked_tasks": [{"id": "task-1"}],
            "acceptance_criteria": ["HTTP 200"],
            "dependencies": ["phase-0"],
            "source_ids": ["REQ-7"],
            "required_deliverables": ["src/api.py"],
        }],
        qc_results={
            "__whole_project__": {
                "qa": {
                    "checked_at": 9,
                    "status": "needs_manual",
                    "issues_detail": [{
                        "id": "issue-final",
                        "status": "needs_manual",
                        "file_path": "src/api.py",
                    }],
                }
            }
        },
    )

    class PhaseManager:
        def get_all_phases(self):
            return [{
                "id": "phase-1",
                "name": "Delivery",
                "tasks": [{"id": "task-1"}],
                "locked_tasks": [{"id": "task-1"}],
                "acceptance_criteria": ["HTTP 200"],
                "dependencies": ["phase-0"],
                "source_ids": ["REQ-7"],
                "required_deliverables": ["src/api.py"],
            }]

    captured = {}

    class Agent:
        def load_project_context(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(routes_team, "_get_project", lambda _project_id: project)
    monkeypatch.setattr(
        routes_team, "_pm_teams", {"proj-1": SimpleNamespace(final_plan=final_plan)}
    )
    monkeypatch.setattr(routes_team, "_phase_managers", {"proj-1": PhaseManager()})
    routes_team._build_engineer_context("proj-1", Agent())
    assert captured["phase_info"][0]["locked_tasks"] == [{"id": "task-1"}]
    assert captured["phase_info"][0]["acceptance_criteria"] == ["HTTP 200"]
    assert captured["phase_info"][0]["dependencies"] == ["phase-0"]
    assert captured["phase_info"][0]["source_ids"] == ["REQ-7"]
    assert "__whole_project__" in captured["qc_results"]
    assert captured["qc_results"]["__whole_project__"]["status"] == "needs_manual"
