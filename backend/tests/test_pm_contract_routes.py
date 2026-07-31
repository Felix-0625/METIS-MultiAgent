import asyncio
import copy
import io
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from agents.pm_team import (
    MAX_PLAN_MODEL_ATTEMPTS,
    PMLeaderAgent,
    _extract_first_json_object,
)
from api import routes_files, routes_phases, routes_pm, routes_team
from core.project_contract import (
    deterministic_plan_fallback,
    extract_project_contract,
    finalize_project_contract,
    parse_project_contract,
    validate_plan_for_confirmation,
)


REAL_CONTRACT = """
技术栈固定为 Node.js + Express + TypeScript + SQLite + Prisma + React + Vite。
严格四个阶段。
角色集合：全栈工程师
阶段一：基础架构
角色集合：全栈工程师
phase-1-task-1 | 项目骨架 | 角色：全栈工程师
phase-1-task-2 | 数据模型 | 角色：全栈工程师 | 依赖：phase-1-task-1
phase-1-task-3 | 登录接口 | 角色：全栈工程师 | 依赖：phase-1-task-2
phase-1-task-4 | 登录页面 | 角色：全栈工程师 | 依赖：phase-1-task-3
阶段二：业务功能
阶段三：集成测试
阶段四：部署交付
"""

ENGLISH_STRUCTURED_CONTRACT = """
Build a simple task tracker in exactly four phases.
Phase 1: Foundation
Owner: Full-stack engineer
Files: package.json
Tasks:
- Create the application skeleton and health endpoint.
Acceptance: The health endpoint returns a successful response.
Phase 2: Task API
Owner: Full-stack engineer
Files: server.js
Tasks:
- Implement task creation and task listing.
Acceptance: Created tasks appear in the task list.
Phase 3: User interface
Owner: Full-stack engineer
Files: index.html
Tasks:
- Implement the task entry form and task list view.
Acceptance: A user can add and view tasks in the interface.
Phase 4: Delivery
Owner: Full-stack engineer
Files: README.md
Tasks:
- Document startup and verification commands.
Acceptance: The documented commands start and verify the application.
"""


class _Hermes:
    def __init__(self, outputs=None, error=None):
        self.outputs = list(outputs or [])
        self.error = error
        self.calls = 0
        self.messages = []

    def chat(self, messages):
        self.calls += 1
        self.messages.append(messages)
        if self.error:
            raise self.error
        return {"content": self.outputs.pop(0)}


def test_model_json_extractor_skips_prose_fences_and_stray_braces():
    content = """
先说明一下 {这不是 JSON。
```json
{"project_name":"工单平台","phases":[]}
```
后续说明 {"ignored": true}
"""

    assert _extract_first_json_object(content) == {
        "project_name": "工单平台",
        "phases": [],
    }


@pytest.mark.parametrize(
    "content",
    [
        '[{"project_name":"数组不是规划对象"}]',
        '{"project_name":"残缺规划","phases":[',
    ],
)
def test_model_json_extractor_rejects_arrays_and_truncated_json(content):
    assert _extract_first_json_object(content) is None


def test_synthesize_route_uses_total_plan_fallback_without_execution_details(monkeypatch):
    leader = PMLeaderAgent(
        hermes_client=_Hermes(["not json", "still not json", "invalid again"])
    )
    canonical = leader.record_user_requirements(REAL_CONTRACT, source="user")
    persisted = []

    async def persist():
        persisted.append(True)

    monkeypatch.setattr(
        routes_pm, "_get_project",
        lambda _project_id: SimpleNamespace(description=REAL_CONTRACT),
    )
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)
    monkeypatch.setattr(routes_pm, "_persist_all_async", persist)

    result = asyncio.run(routes_pm.synthesize_pm_plan(
        "contract-project", {
            "requirements_revision": canonical["requirements_revision"],
            "requirements_digest": canonical["requirements_digest"],
            "fast_mode": True,
        }
    ))

    assert result["success"] is True
    assert result["status"] == "saved"
    assert result["can_confirm"] is True
    assert result["generation"]["model_status"] == "validation_failed"
    assert len(result["generation"]["attempts"]) == MAX_PLAN_MODEL_ATTEMPTS
    assert leader.hermes.calls == MAX_PLAN_MODEL_ATTEMPTS
    assert leader.draft_plan["schema_version"] == "total-plan/v1"
    assert all(
        set(phase) == {
            "phase_id", "name", "objective", "work_items",
            "technical_requirements", "dependencies",
            "source_requirement_ids",
        }
        for phase in leader.draft_plan["phases"]
    )
    assert persisted


def test_model_exception_is_recorded_while_safe_total_plan_fallback_is_saved():
    leader = PMLeaderAgent(hermes_client=_Hermes(error=RuntimeError("quota exhausted")))

    result = leader.synthesize_plan_fast(REAL_CONTRACT)

    assert result["success"] is True
    assert result["status"] == "saved"
    assert result["can_confirm"] is True
    assert result["generation"]["model_status"] == "model_failed"
    assert result["generation"]["attempts"] == [{
        "attempt": 1, "status": "model_failed",
        "code": "quota_or_rate_limit", "error_type": "RuntimeError",
    }]
    assert leader.hermes.calls == 1
    assert leader.draft_plan["source"] == "deterministic_contract_fallback"
    assert leader.draft_plan["schema_version"] == "total-plan/v1"


def test_model_failure_uses_total_plan_fallback_for_exact_phase_scaffold():
    leader = PMLeaderAgent(hermes_client=_Hermes(error=RuntimeError("api key missing")))

    result = leader.synthesize_plan_fast(
        "Build a Node.js application in exactly four phases."
    )

    assert result["success"] is True
    assert result["status"] == "saved"
    assert result["can_confirm"] is True
    assert result["generation"]["model_status"] == "model_failed"
    assert leader.draft_plan is not None
    assert leader.draft_plan["source"] == "deterministic_contract_fallback"
    assert len(leader.draft_plan["phases"]) == 4
    assert all(phase["work_items"] for phase in leader.draft_plan["phases"])
    assert all("tasks" not in phase for phase in leader.draft_plan["phases"])
    assert all("roles_needed" not in phase for phase in leader.draft_plan["phases"])
    assert all("required_files" not in phase for phase in leader.draft_plan["phases"])
    assert not leader.draft_plan["project_contract"]["required_files"]


def test_unspecified_fields_use_pm_selected_rich_plan_and_prompt_contract():
    requirements = "必须实现库存录入。必须支持库存查询。"
    contract = parse_project_contract(requirements)
    source_ids = [item.unit_id for item in contract.requirement_units]
    model_plan = {
        "project_name": "库存系统",
        "summary": "分阶段完成库存录入与查询",
        "total_duration": "待定",
        "tech_stack": ["Python", "FastAPI", "SQLite"],
        "phases": [{
            "phase_id": "phase-1",
            "name": "库存服务",
            "description": "交付库存录入与查询能力",
            "implementation": "使用 FastAPI API 和 SQLite 持久化实现",
            "duration": "待定",
            "tech_stack": ["Python", "FastAPI", "SQLite"],
            "roles_needed": ["backend"],
            "agent_count": 1,
            "responsibilities": ["backend：负责接口、校验与持久化"],
            "personnel_allocation": ["backend：1人"],
            "tasks": [
                {
                    "task_id": "phase-1-task-1",
                    "name": "实现库存录入",
                    "description": "实现库存录入与数据校验",
                    "implementation": "使用 FastAPI 路由和 SQLite 事务",
                    "duration": "待定",
                    "tech_stack": ["Python", "FastAPI", "SQLite"],
                    "roles": ["backend"],
                    "responsibilities": ["backend：负责录入接口与持久化"],
                    "personnel_count": 1,
                    "personnel_allocation": ["backend：1人"],
                    "acceptance_criteria": ["库存数据可成功录入并持久化"],
                    "dependencies": [],
                    "source_requirement_ids": [source_ids[0]],
                },
                {
                    "task_id": "phase-1-task-2",
                    "name": "实现库存查询",
                    "description": "实现库存列表和条件查询",
                    "implementation": "使用 FastAPI 查询参数和 SQLite 索引",
                    "duration": "待定",
                    "tech_stack": ["Python", "FastAPI", "SQLite"],
                    "roles": ["backend"],
                    "responsibilities": ["backend：负责查询接口与测试"],
                    "personnel_count": 1,
                    "personnel_allocation": ["backend：1人"],
                    "acceptance_criteria": ["可按条件查询已录入库存"],
                    "dependencies": ["phase-1-task-1"],
                    "source_requirement_ids": [source_ids[1]],
                },
            ],
            "acceptance_criteria": ["库存录入和查询通过接口测试"],
            "dependencies": [],
        }],
    }
    model_plan = {
        "schema_version": "total-plan/v1",
        "project_name": "库存系统",
        "summary": "分阶段完成库存录入与查询",
        "phases": [{
            "phase_id": "phase-1",
            "name": "库存能力",
            "objective": "完成库存录入与查询",
            "work_items": ["实现库存录入", "实现库存查询"],
            "technical_requirements": [],
            "dependencies": [],
            "source_requirement_ids": source_ids,
        }],
    }
    hermes = _Hermes([json.dumps(model_plan, ensure_ascii=False)])
    leader = PMLeaderAgent(hermes_client=hermes)

    result = leader.synthesize_plan_fast(requirements)

    assert result["success"] is True, result.get("validation")
    phase = result["draft_plan"]["phases"][0]
    assert result["draft_plan"]["source"] == "model"
    assert phase["work_items"] == ["实现库存录入", "实现库存查询"]
    assert phase["technical_requirements"] == []
    assert not {"tasks", "required_files", "roles_needed", "implementation"} & set(phase)

    system_prompt = hermes.messages[0][0].content
    assert "phases 是动态数组" in system_prompt
    assert '"schema_version":"total-plan/v1"' in system_prompt
    assert '"technical_requirements"' in system_prompt
    assert "不要生成具体任务 ID、实现方式、交付文件、文件路径、角色、人员" in system_prompt
    assert MAX_PLAN_MODEL_ATTEMPTS == 3
    assert "默认四阶段" not in system_prompt
    assert "待阶段PM分配" not in system_prompt


def test_model_repairs_missing_requirement_trace_once_with_exact_prompt_units():
    requirements = "必须实现库存录入。必须支持库存查询。"
    contract = parse_project_contract(requirements)
    source_ids = [item.unit_id for item in contract.requirement_units]
    valid_plan = {
        "schema_version": "total-plan/v1",
        "project_name": "库存系统",
        "summary": "完成库存能力",
        "phases": [{
            "phase_id": "phase-1",
            "name": "库存能力",
            "objective": "完成库存录入与查询",
            "work_items": ["库存录入", "库存查询"],
            "technical_requirements": [],
            "dependencies": [],
            "source_requirement_ids": source_ids,
        }],
    }
    invalid_plan = copy.deepcopy(valid_plan)
    invalid_plan["phases"][0]["source_requirement_ids"] = []
    hermes = _Hermes([
        json.dumps(invalid_plan, ensure_ascii=False),
        json.dumps(valid_plan, ensure_ascii=False),
    ])
    leader = PMLeaderAgent(hermes_client=hermes)

    result = leader.synthesize_plan_fast(requirements)

    assert result["success"] is True, result.get("validation")
    assert result["draft_plan"]["source"] == "model_repaired"
    assert result["generation"]["model_status"] == "generated"
    assert hermes.calls == 2
    assert [attempt["status"] for attempt in result["generation"]["attempts"]] == [
        "validation_failed",
        "generated",
    ]
    system_prompt = hermes.messages[0][0].content
    for unit in contract.requirement_units:
        assert unit.unit_id in system_prompt
        assert unit.exact_text in system_prompt
    previous_candidate = json.loads(hermes.messages[1][-2].content)
    assert previous_candidate["phases"]
    assert previous_candidate["phases"][0]["source_requirement_ids"] == []
    repair_prompt = hermes.messages[1][-1].content
    repair_issues = json.loads(repair_prompt.split("：", 1)[1])
    assert repair_issues == result["generation"]["attempts"][0]["issues"]
    assert {issue["code"] for issue in repair_issues} == {
        "requirement_units_uncovered"
    }
    assert all("layer" not in issue for issue in repair_issues)


def test_total_pm_retries_when_plan_adds_explicitly_forbidden_technology():
    requirements = (
        "Build a locally runnable Node.js service. Do not add Docker."
    )
    contract = parse_project_contract(requirements)
    source_ids = [item.unit_id for item in contract.requirement_units]
    valid_plan = {
        "schema_version": "total-plan/v1",
        "project_name": "Local service",
        "summary": "Deliver the local Node.js service",
        "phases": [{
            "phase_id": "phase-1",
            "name": "Service",
            "objective": "Implement the local service",
            "work_items": ["Implement the Node.js service"],
            "technical_requirements": ["Node.js"],
            "dependencies": [],
            "source_requirement_ids": source_ids,
        }],
    }
    invalid_plan = copy.deepcopy(valid_plan)
    invalid_plan["phases"][0]["technical_requirements"].append("Docker")
    hermes = _Hermes([
        json.dumps(invalid_plan),
        json.dumps(valid_plan),
    ])
    leader = PMLeaderAgent(hermes_client=hermes)

    result = leader.synthesize_plan_fast(requirements)

    assert result["success"] is True
    assert result["draft_plan"]["source"] == "model_repaired"
    assert [item["status"] for item in result["generation"]["attempts"]] == [
        "validation_failed",
        "generated",
    ]
    assert {
        issue["code"]
        for issue in result["generation"]["attempts"][0]["issues"]
    } == {"forbidden_scope"}
    assert "forbidden_scope" in hermes.messages[1][-1].content


def test_total_pm_rejects_manifest_and_package_execution_details():
    requirements = (
        "Build a locally runnable Node.js and Express TODO API. "
        "Plan exactly 1 phase with exactly 1 task. "
        "Use npm start to run it and npm test to verify it."
    )
    contract = parse_project_contract(requirements)
    model_plan = deterministic_plan_fallback(contract)
    task = model_plan["phases"][0]["task_contract"][0]
    task["required_files"] = [
        "package.json",
        "src/server.js",
        "tests/todos.test.js",
    ]
    task["package_dependencies"] = ["express"]
    task.pop("package_scripts", None)
    leader = PMLeaderAgent(
        hermes_client=_Hermes([json.dumps(model_plan, ensure_ascii=False)])
    )

    result = leader.synthesize_plan_fast(requirements)

    assert result["success"] is True, result.get("validation")
    assert result["draft_plan"]["source"] == "deterministic_contract_fallback"
    assert "task_contract" not in result["draft_plan"]["phases"][0]
    assert "required_files" not in result["draft_plan"]["phases"][0]
    assert {
        issue["code"]
        for attempt in result["generation"]["attempts"]
        for issue in attempt.get("issues", [])
    } >= {"unexpected_fields"}


def test_total_pm_rejects_task_dependency_details():
    requirements = (
        "严格规划3个阶段，每阶段恰好1个任务。"
        "必须实现库存录入、库存查询和自动化测试。"
    )
    contract = parse_project_contract(requirements)
    model_plan = deterministic_plan_fallback(contract)
    phases = model_plan["phases"]
    for index, phase in enumerate(phases[1:], 1):
        phase["dependencies"] = []
        tasks = phase.get("tasks") or phase.get("task_contract")
        tasks[0]["dependencies"] = [phases[index - 1]["phase_id"]]
    hermes = _Hermes([json.dumps(model_plan, ensure_ascii=False)])
    leader = PMLeaderAgent(hermes_client=hermes)

    result = leader.synthesize_plan_fast(requirements)

    assert result["success"] is True, result.get("validation")
    assert result["draft_plan"]["source"] == "deterministic_contract_fallback"
    normalized = result["draft_plan"]["phases"]
    for index, phase in enumerate(normalized[1:], 1):
        assert phase["dependencies"] == [
            normalized[index - 1]["phase_id"]
        ]
        assert "tasks" not in phase


def test_synthesis_rejects_manifest_incompatible_model_before_confirmation():
    requirements = (
        "严格规划1个阶段，每阶段恰好1个任务。"
        "必须实现库存查询。"
    )
    contract = parse_project_contract(requirements)
    incompatible = deterministic_plan_fallback(contract)
    phase = incompatible["phases"][0]
    phase["roles_needed"] = ["Backend Developer"]
    phase["roles"] = ["Backend Developer"]
    task = (phase.get("tasks") or phase.get("task_contract"))[0]
    task["roles"] = ["Backend Developer"]
    task["required_files"] = ["Dockerfile"]
    hermes = _Hermes([
        json.dumps(incompatible, ensure_ascii=False)
        for _ in range(MAX_PLAN_MODEL_ATTEMPTS)
    ])
    leader = PMLeaderAgent(hermes_client=hermes)

    result = leader.synthesize_plan_fast(requirements)

    assert result["draft_plan"]["source"] == "deterministic_contract_fallback"
    assert result["generation"]["model_status"] == "validation_failed"
    assert {
        issue["code"]
        for attempt in result["generation"]["attempts"]
        for issue in attempt.get("issues", [])
    } >= {"unexpected_fields"}


def test_invalid_json_uses_valid_fallback_for_english_structured_outline():
    leader = PMLeaderAgent(
        hermes_client=_Hermes(["not json", "still not json", "invalid again"])
    )
    canonical = leader.record_user_requirements(
        ENGLISH_STRUCTURED_CONTRACT, source="user"
    )

    result = leader.synthesize_plan_fast(
        leader.canonical_requirements,
        requirements_revision=canonical["requirements_revision"],
        requirements_digest=canonical["requirements_digest"],
    )

    assert result["success"] is True, [
        (issue["code"], issue["path"], issue["message"])
        for issue in result["validation"]["issues"]
    ]
    assert result["status"] == "saved"
    assert result["generation"]["model_status"] == "validation_failed"
    assert result["draft_plan"]["source"] == "deterministic_contract_fallback"
    assert len(result["draft_plan"]["phases"]) == 4
    assert all(phase["work_items"] for phase in result["draft_plan"]["phases"])
    assert all("roles_needed" not in phase for phase in result["draft_plan"]["phases"])
    assert all("task_contract" not in phase for phase in result["draft_plan"]["phases"])
    assert all("required_files" not in phase for phase in result["draft_plan"]["phases"])


def test_model_failure_uses_valid_traceable_fallback_for_explicit_four_phases():
    requirements = """
必须严格按四个阶段实施。
阶段一：账户基础
角色集合：后端工程师
任务一：实现账户登录并记录安全审计日志
阶段二：业务服务
角色集合：后端工程师
任务一：实现订单创建与状态查询
阶段三：用户界面
角色集合：前端工程师
任务一：实现登录和订单管理页面
阶段四：验收发布
角色集合：测试工程师、运维工程师
任务一：完成全流程验收并发布
"""
    leader = PMLeaderAgent(
        hermes_client=_Hermes(error=RuntimeError("quota exhausted"))
    )
    canonical = leader.record_user_requirements(requirements, source="user")

    synthesized = leader.synthesize_plan_fast(
        leader.canonical_requirements,
        requirements_revision=canonical["requirements_revision"],
        requirements_digest=canonical["requirements_digest"],
    )
    confirmed = leader.confirm_plan(required_files=[{
        "path": "README.md",
        "owner_type": "devops",
        "phase_id": "phase-4",
        "task_id": "phase-4-task-1",
        "criterion": "完成全流程验收并发布",
        "evidence_spec": "registry_byte_digest",
    }],
        expected_revision=canonical["requirements_revision"],
        expected_digest=canonical["requirements_digest"],
    )

    assert synthesized["success"] is True
    assert synthesized["status"] == "saved"
    assert synthesized["generation"]["model_status"] == "model_failed"
    assert synthesized["draft_plan"]["source"] == "deterministic_contract_fallback"
    assert confirmed["success"] is True, [
        issue["code"] for issue in confirmed["validation"]["issues"]
    ]
    assert confirmed["status"] == "confirmed"
    locked_contract = confirmed["plan"]["project_contract"]
    assert locked_contract["requirements_revision"] == 1
    assert locked_contract["requirements_digest"] == canonical["requirements_digest"]
    assert list(locked_contract["requirement_event_ids"]) == [
        leader.requirement_events[0]["event_id"]
    ]
    assert locked_contract["requirement_lineage_digest"].startswith("sha256:")


def test_confirmation_validation_requires_complete_phase_contract():
    requirements = "Use Node.js for the application."
    contract = extract_project_contract(requirements)
    plan = {
        "phases": [{
            "phase_id": "phase-1",
            "name": "Foundation",
            "roles_needed": [],
            "task_contract": [],
            "acceptance_criteria": [],
        }],
        "project_contract": contract,
    }

    result = validate_plan_for_confirmation(
        plan, contract, expected_source_requirements=requirements
    )

    assert result.valid is False
    assert {issue.code for issue in result.issues} >= {
        "phase_tasks_empty",
        "phase_roles_empty",
        "phase_acceptance_criteria_empty",
    }


def test_confirm_route_rejects_incomplete_phase_with_http_422(monkeypatch):
    requirements = "Use Node.js for the application."
    leader = PMLeaderAgent(hermes_client=_Hermes())
    leader.project_contract = dict(extract_project_contract(requirements))
    leader.draft_plan = {
        "phases": [{
            "phase_id": "phase-1",
            "name": "Foundation",
            "roles_needed": [],
            "task_contract": [],
            "acceptance_criteria": [],
        }],
        "project_contract": leader.project_contract,
    }

    async def persist():
        return None

    monkeypatch.setattr(routes_pm, "_get_project", lambda _project_id: SimpleNamespace())
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)
    monkeypatch.setattr(routes_pm, "_persist_all_async", persist)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_pm.confirm_pm_plan(
            "contract-project", routes_pm.PlanConfirmRequest(
                modifications="", requirements_revision=0, requirements_digest="",
            )
        ))

    assert caught.value.status_code == 422
    assert leader.plan_confirmed is False
    assert leader.final_plan is None
    assert leader.project_contract["locked"] is True


def test_confirm_preserves_source_requirements_summary_and_digest():
    requirements = "Use Node.js. Preserve this exact user requirement."
    leader = PMLeaderAgent(hermes_client=_Hermes())
    leader.project_contract = dict(extract_project_contract(requirements))
    leader.draft_plan = {
        "plan_version": 1,
        "tech_stack": ["Node.js"],
        "phases": [{
            "phase_id": "phase-1",
            "name": "Foundation",
            "description": "Build and verify the Node.js service foundation",
            "implementation": "Implement the service and verify its startup path",
            "tech_stack": ["Node.js"],
            "roles_needed": ["backend", "devops"],
            "agent_count": 2,
            "responsibilities": [
                "backend: implement the service",
                "devops: verify startup and delivery",
            ],
            "personnel_allocation": ["backend: 1", "devops: 1"],
            "task_contract": [{
                "task_id": "phase-1-task-1",
                "name": "Use Node.js and preserve the exact user requirement",
                "description": "Implement the exact Node.js requirement",
                "implementation": "Build the service and exercise its startup command",
                "tech_stack": ["Node.js"],
                "roles": ["backend", "devops"],
                "responsibilities": [
                    "backend: implement the service",
                    "devops: verify startup",
                ],
                "personnel_count": 2,
                "personnel_allocation": ["backend: 1", "devops: 1"],
                "acceptance_criteria": ["service starts"],
                "source_requirement_ids": [
                    leader.project_contract["requirement_units"][0]["unit_id"]
                ],
            }],
            "acceptance_criteria": ["service starts"],
        }],
        "project_contract": leader.project_contract,
    }

    required_files = [
        {
            "path": path,
            "owner_type": owner,
            "phase_id": "phase-1",
            "task_id": "phase-1-task-1",
            "criterion": "service starts",
            "evidence_spec": "registry_byte_digest",
        }
        for path, owner in (
            ("backend/package.json", "backend"),
            ("package.json", "devops"),
            ("README.md", "devops"),
        )
    ]
    result = leader.confirm_plan(required_files=required_files)

    assert result["success"] is True
    locked = result["plan"]["project_contract"]
    assert locked["source_requirements"] == requirements
    assert locked["requirements_summary"] == requirements
    assert locked["requirements_digest"].startswith("sha256:")


def test_confirm_blocks_legacy_invalid_draft_with_structured_issues():
    leader = PMLeaderAgent(hermes_client=_Hermes())
    leader.draft_plan = {"phases": []}

    result = leader.confirm_plan()

    assert result["success"] is False
    assert result["status"] == "validation_failed"
    assert result["validation"]["issues"][0]["layer"] == "json_schema"
    assert leader.final_plan is None


def test_confirm_route_never_generates_a_missing_draft(monkeypatch):
    leader = PMLeaderAgent(hermes_client=_Hermes(error=AssertionError("model called")))
    monkeypatch.setattr(
        routes_pm, "_get_project", lambda _project_id: SimpleNamespace()
    )
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)

    result = asyncio.run(routes_pm.confirm_pm_plan(
        "contract-project", routes_pm.PlanConfirmRequest(
            modifications="", requirements_revision=0, requirements_digest="",
        )
    ))

    assert result["success"] is False
    assert result["status"] == "needs_plan"
    assert result["validation"]["issues"][0]["code"] == "missing_draft"
    assert leader.hermes.calls == 0


def test_loading_invalid_legacy_draft_moves_it_to_blocked_history():
    leader = PMLeaderAgent(hermes_client=_Hermes())
    legacy = {"phases": []}

    leader.from_persist({"draft_plan": legacy, "plan_confirmed": False})

    assert leader.draft_plan is None
    assert leader.blocked_draft is legacy
    assert leader.plan_status == "validation_failed"
    assert leader.draft_blocked_reason == "legacy_draft_requires_regeneration"


def test_loading_valid_unconfirmed_legacy_draft_requires_regeneration():
    leader = PMLeaderAgent(hermes_client=_Hermes())
    legacy = {"phases": [{"phase_id": "phase-1"}]}

    leader.from_persist({"draft_plan": legacy, "plan_confirmed": False})

    assert leader.draft_plan is None
    assert leader.blocked_draft is legacy
    assert leader.draft_blocked_reason == "legacy_draft_requires_regeneration"
    assert leader.plan_status == "validation_failed"


def test_loading_confirmed_legacy_contract_is_read_only_and_launch_returns_409(
    monkeypatch,
):
    leader = PMLeaderAgent(hermes_client=_Hermes())
    legacy = {
        "phases": [{"phase_id": "phase-1"}],
        "project_contract": {"contract_version": 2, "locked": True},
    }

    leader.from_persist({
        "draft_plan": legacy,
        "final_plan": legacy,
        "plan_confirmed": True,
        "project_contract": legacy["project_contract"],
    })

    assert leader.plan_confirmed is False
    assert leader.final_plan is None
    assert leader.blocked_draft == legacy
    assert leader.draft_blocked_reason == "legacy_confirmed_requires_regeneration"
    monkeypatch.setattr(
        routes_pm, "_get_project", lambda _project_id: SimpleNamespace()
    )
    monkeypatch.setitem(routes_pm._pm_teams, "legacy-v2", leader)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes_pm.launch_project("legacy-v2"))
    assert exc_info.value.status_code == 409


def test_loading_confirmed_legacy_contract_migrates_only_from_trusted_source():
    source_contract = extract_project_contract(REAL_CONTRACT)
    legacy_plan = deterministic_plan_fallback(source_contract)
    legacy_contract = {
        "contract_version": 2,
        "locked": True,
        "source_requirements": REAL_CONTRACT,
    }
    legacy_plan["contract_version"] = 2
    legacy_plan["project_contract"] = legacy_contract
    leader = PMLeaderAgent(hermes_client=_Hermes())

    leader.from_persist({
        "draft_plan": legacy_plan,
        "final_plan": legacy_plan,
        "plan_confirmed": True,
        "project_contract": legacy_contract,
    })

    assert leader.plan_confirmed is True
    assert leader.project_contract["contract_version"] == 3
    assert leader.project_contract["requirements_revision"] == 1
    assert list(leader.project_contract["requirement_event_ids"]) == [
        leader.requirement_events[0]["event_id"]
    ]
    assert leader.final_plan["requirements_digest"] == leader.requirements_digest
    assert leader.plan_status == "confirmed"


class _PhaseManager:
    def __init__(self, phase, contract):
        self.phase = phase
        self.project_contract = contract

    def get_phase(self, phase_id):
        return self.phase if phase_id == self.phase.get("phase_id") else None


def test_phase_route_rejects_missing_fields_and_keeps_previous_plan(monkeypatch):
    project_id = "phase-missing-contract"
    previous = [{
        "task_id": "historical-task", "task_name": "历史任务",
        "task_description": "历史结果", "required_role": "全栈工程师",
    }]
    phase = {
        "phase_id": "phase-1", "name": "无锁定任务阶段", "roles_needed": ["全栈工程师"],
        "expert_requirements": previous,
    }
    hermes = _Hermes([
        '[{"task_name":"缺少字段","required_role":"全栈工程师"}]',
        '[{"task_name":"仍缺少字段","required_role":"全栈工程师"}]',
    ])

    async def persist():
        return None

    monkeypatch.setattr(routes_phases, "_get_project", lambda _project_id: SimpleNamespace(name="项目"))
    monkeypatch.setitem(routes_phases._phase_managers, project_id, _PhaseManager(phase, {}))
    monkeypatch.setattr(routes_phases.hermes_client, "chat", hermes.chat)
    monkeypatch.setattr(routes_phases, "_persist_all_async", persist)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_phases.plan_experts_for_phase(project_id, "phase-1", {}))

    assert caught.value.status_code == 422
    assert caught.value.detail["status"] == "validation_failed"
    assert phase["expert_requirements"] is previous
    assert phase["plan_status"] == "validation_failed"
    assert hermes.calls == 2


def test_invalid_legacy_phase_without_contract_is_explicitly_blocked():
    phase = {
        "phase_id": "phase-1",
        "expert_requirements": [{
            "task_id": "t1", "task_name": "旧任务", "task_description": "旧数据",
            "required_role": "越界角色",
        }],
    }

    changed = routes_phases._migrate_invalid_phase_plan(phase, {})

    assert changed is True
    assert phase["legacy_plan_blocked"] is True
    assert phase["plan_status"] == "validation_failed"
    assert phase["plan_contract_validated"] is False


def test_locked_phase_recovers_missing_executable_plan_from_task_contract():
    requirements = "Use Node.js and Express."
    raw_plan = {
        "technology_stack": ["Node.js", "Express"],
        "phases": [{
            "phase_id": "1",
            "name": "Foundation",
            "roles": ["backend"],
            "tasks": [{"task_id": "t1", "name": "Create backend"}],
        }],
    }
    contract = finalize_project_contract(
        parse_project_contract(requirements), raw_plan
    ).as_mapping()
    phase = {
        "phase_id": "1",
        "name": "Foundation",
        "roles_needed": ["backend"],
        "task_contract": raw_plan["phases"][0]["tasks"],
        "expert_requirements": [],
    }

    changed = routes_phases._migrate_invalid_phase_plan(phase, contract)

    assert changed is True
    assert phase["plan_contract_validated"] is True
    assert phase["plan_generation_mode"] == "locked_contract_recovery"
    assert phase["expert_requirements"][0]["task_id"] == "t1"


def test_canonical_requirement_events_dedupe_and_supersede():
    leader = PMLeaderAgent(hermes_client=_Hermes())
    first = leader.record_user_requirements("附件第一段\n\n附件第二段", source="attachment")
    duplicate = leader.record_user_requirements("附件第一段\n\n附件第二段", source="attachment")
    leader.record_user_requirements(
        "技术栈改为 Vue", source="chat", replace=True,
        expected_revision=first["requirements_revision"],
        expected_digest=first["requirements_digest"],
    )
    assert duplicate == first
    assert leader.canonical_requirements == "技术栈改为 Vue"
    assert leader.requirement_events[0]["superseded"] is True


def test_plain_continue_chat_does_not_change_canonical_revision(monkeypatch):
    leader = PMLeaderAgent(hermes_client=_Hermes())
    initial = leader.record_user_requirements("最初需求", source="user")

    async def persist():
        return None

    monkeypatch.setattr(
        routes_pm, "_get_project", lambda _project_id: SimpleNamespace(description="")
    )
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)
    monkeypatch.setattr(routes_pm, "_persist_all_async", persist)
    monkeypatch.setattr(
        leader, "chat_with_user",
        lambda **_kwargs: {"success": True, "reply": "继续讨论"},
    )

    result = asyncio.run(routes_pm.pm_team_chat(
        "canonical-project",
        routes_pm.PMTeamChatRequest(message="继续"),
    ))

    assert result["requirements_revision"] == initial["requirements_revision"]
    assert result["requirements_digest"] == initial["requirements_digest"]
    assert leader.canonical_requirements == "最初需求"


def test_first_typed_requirement_revises_before_discussion_chat(monkeypatch):
    leader = PMLeaderAgent(hermes_client=_Hermes())

    async def persist():
        return None

    monkeypatch.setattr(
        routes_pm, "_get_project", lambda _project_id: SimpleNamespace(description="")
    )
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)
    monkeypatch.setattr(routes_pm, "_persist_all_async", persist)
    monkeypatch.setattr(
        leader, "chat_with_user",
        lambda **_kwargs: {"success": True, "reply": "开始澄清"},
    )

    revised = asyncio.run(routes_pm.revise_pm_requirements(
        "typed-project",
        routes_pm.RequirementsRevisionRequest(
            content="构建一个 Vue 任务管理应用",
            expected_revision=0,
            expected_digest="",
            source="user",
        ),
    ))
    discussed = asyncio.run(routes_pm.pm_team_chat(
        "typed-project",
        routes_pm.PMTeamChatRequest(message="请先分析风险"),
    ))

    assert revised["requirements_revision"] == 1
    assert discussed["requirements_revision"] == 1
    assert leader.canonical_requirements == "构建一个 Vue 任务管理应用"
    assert len(leader.requirement_events) == 1


def test_first_attachment_revision_atomically_preserves_project_outline(monkeypatch):
    leader = PMLeaderAgent(hermes_client=_Hermes())

    async def persist():
        return None

    monkeypatch.setattr(
        routes_pm, "_get_project",
        lambda _project_id: SimpleNamespace(description="项目原始大纲"),
    )
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)
    monkeypatch.setattr(routes_pm, "_persist_all_async", persist)
    monkeypatch.setattr(
        leader, "chat_with_user",
        lambda **_kwargs: {"success": True, "reply": "已收到附件"},
    )

    result = asyncio.run(routes_pm.pm_team_chat(
        "canonical-project",
        routes_pm.PMTeamChatRequest(
            message="补充附件",
            requirements_revision=routes_pm.RequirementsRevisionRequest(
                content="附件完整正文",
                expected_revision=0,
                expected_digest="",
                source="attachment",
            ),
        ),
    ))

    assert result["requirements_revision"] == 2
    assert leader.canonical_requirements == "项目原始大纲\n\n附件完整正文"
    assert [event["source"] for event in leader.requirement_events] == [
        "user", "attachment",
    ]


def test_duplicate_content_replace_still_supersedes_conflicting_events():
    leader = PMLeaderAgent(hermes_client=_Hermes())
    first = leader.record_user_requirements("使用 Vue", source="user")
    leader.record_user_requirements(
        "使用 React", source="user",
        expected_revision=first["requirements_revision"],
        expected_digest=first["requirements_digest"],
    )
    before_revision = leader.requirements_revision

    result = leader.record_user_requirements(
        "使用 Vue", source="user", replace=True,
        expected_revision=leader.requirements_revision,
        expected_digest=leader.requirements_digest,
    )

    assert result["requirements_revision"] == before_revision + 1
    assert leader.canonical_requirements == "使用 Vue"
    assert [event["superseded"] for event in leader.requirement_events] == [
        True, True, False,
    ]


def test_identical_canonical_content_is_deduped_across_sources():
    leader = PMLeaderAgent(hermes_client=_Hermes())
    first = leader.record_user_requirements("同一完整正文", source="user")

    duplicate = leader.record_user_requirements(
        "同一完整正文",
        source="attachment",
        expected_revision=first["requirements_revision"],
        expected_digest=first["requirements_digest"],
    )

    assert duplicate == first
    assert leader.requirements_revision == 1
    assert len(leader.requirement_events) == 1


def test_unknown_requirement_event_id_is_rejected_with_http_409(monkeypatch):
    leader = PMLeaderAgent(hermes_client=_Hermes())
    leader.record_user_requirements("使用 React", source="user")

    async def persist():
        raise AssertionError("conflicting revision must not persist")

    monkeypatch.setattr(routes_pm, "_get_project", lambda _project_id: SimpleNamespace())
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)
    monkeypatch.setattr(routes_pm, "_persist_all_async", persist)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_pm.revise_pm_requirements(
            "canonical-project",
            routes_pm.RequirementsRevisionRequest(
                content="改用 Vue",
                expected_revision=leader.requirements_revision,
                expected_digest=leader.requirements_digest,
                supersedes=["missing-event"],
            ),
        ))

    assert caught.value.status_code == 409
    assert leader.canonical_requirements == "使用 React"


def test_structured_revision_route_replaces_canonical_snapshot(monkeypatch):
    leader = PMLeaderAgent(hermes_client=_Hermes())
    current = leader.record_user_requirements("使用 React", source="user")
    persisted = []
    stale_phase_manager = object()

    async def persist():
        persisted.append(True)

    monkeypatch.setattr(routes_pm, "_get_project", lambda _project_id: SimpleNamespace())
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)
    monkeypatch.setattr(routes_pm, "_persist_all_async", persist)
    monkeypatch.setitem(
        routes_pm._phase_managers, "canonical-project", stale_phase_manager
    )

    result = asyncio.run(routes_pm.revise_pm_requirements(
        "canonical-project",
        routes_pm.RequirementsRevisionRequest(
            content="使用 Vue",
            expected_revision=current["requirements_revision"],
            expected_digest=current["requirements_digest"],
            replace=True,
        ),
    ))

    assert result["requirements_revision"] == 2
    assert result["requirements_digest"] == leader.requirements_digest
    assert leader.canonical_requirements == "使用 Vue"
    assert persisted == [True]
    assert "canonical-project" not in routes_pm._phase_managers


def test_requirement_revision_rejects_started_execution_without_orphaning_lifecycle(
    monkeypatch,
):
    """Replanning must not leave Agent and QA records behind a new contract."""
    project_id = "executed-revision-project"
    phase_id = "phase-1"
    leader = PMLeaderAgent(hermes_client=_Hermes())
    current = leader.record_user_requirements("Use React", source="user")
    leader.draft_plan = {
        "requirements_revision": current["requirements_revision"],
        "requirements_digest": current["requirements_digest"],
    }
    leader.final_plan = dict(leader.draft_plan)
    leader.plan_confirmed = True
    leader.plan_status = "confirmed"
    phase = {
        "phase_id": phase_id,
        "status": "reviewing",
        "started_at": 100.0,
        "execution_generation": "generation-1",
    }
    phase_manager = SimpleNamespace(phases=[phase])
    agent = {
        "id": "agent-1",
        "phase_id": phase_id,
        "status": "completed",
    }
    child = {
        "id": "sp-1",
        "phase_id": phase_id,
        "agent_id": "agent-1",
        "status": "completed",
    }
    supervisor_run = {
        "run_id": "supervisor-1",
        "status": "completed",
    }
    ctx = SimpleNamespace(
        description="Use React",
        agents={"agent-1": agent},
        subprojects=[child],
        supervisor_quality_runs={phase_id: supervisor_run},
    )
    repair_key = f"{project_id}-{phase_id}"
    repair_state = {
        "running": False,
        "status": "passed",
        "execution_generation": "generation-1",
    }
    persisted = []

    async def persist():
        persisted.append(True)

    monkeypatch.setattr(routes_pm, "_get_project", lambda _project_id: ctx)
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)
    monkeypatch.setattr(routes_pm, "_persist_all_async", persist)
    monkeypatch.setitem(routes_pm._phase_managers, project_id, phase_manager)
    monkeypatch.setitem(
        routes_phases._auto_repair_states,
        repair_key,
        repair_state,
    )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_pm.revise_pm_requirements(
            project_id,
            routes_pm.RequirementsRevisionRequest(
                content="Use Vue",
                expected_revision=current["requirements_revision"],
                expected_digest=current["requirements_digest"],
                replace=True,
            ),
        ))

    assert caught.value.status_code == 409
    assert leader.canonical_requirements == "Use React"
    assert leader.requirements_revision == current["requirements_revision"]
    assert leader.requirements_digest == current["requirements_digest"]
    assert leader.plan_confirmed is True
    assert leader.final_plan == {
        "requirements_revision": current["requirements_revision"],
        "requirements_digest": current["requirements_digest"],
    }
    assert routes_pm._phase_managers[project_id] is phase_manager
    assert ctx.agents == {"agent-1": agent}
    assert ctx.subprojects == [child]
    assert ctx.supervisor_quality_runs == {phase_id: supervisor_run}
    assert routes_phases._auto_repair_states[repair_key] is repair_state
    assert persisted == []


def test_concurrent_identical_cas_allows_only_one_revision(monkeypatch):
    leader = PMLeaderAgent(hermes_client=_Hermes())
    current = leader.record_user_requirements("使用 React", source="user")

    async def persist():
        return None

    monkeypatch.setattr(routes_pm, "_get_project", lambda _project_id: SimpleNamespace())
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)
    monkeypatch.setattr(routes_pm, "_persist_all_async", persist)
    request = routes_pm.RequirementsRevisionRequest(
        content="使用 Vue",
        expected_revision=current["requirements_revision"],
        expected_digest=current["requirements_digest"],
        replace=True,
    )

    async def run_both():
        return await asyncio.gather(
            routes_pm.revise_pm_requirements("cas-project", request),
            routes_pm.revise_pm_requirements("cas-project", request),
            return_exceptions=True,
        )

    results = asyncio.run(run_both())

    successes = [item for item in results if isinstance(item, dict)]
    conflicts = [
        item for item in results
        if isinstance(item, HTTPException) and item.status_code == 409
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert leader.requirements_revision == 2
    assert leader.canonical_requirements == "使用 Vue"


def test_requirement_persist_failure_rolls_back_plan_and_phase_invalidation(monkeypatch):
    project_id = "persist-failure-project"
    leader = PMLeaderAgent(hermes_client=_Hermes())
    current = leader.record_user_requirements("使用 React", source="user")
    leader.draft_plan = {"requirements_revision": current["requirements_revision"]}
    phase_manager = object()
    monkeypatch.setitem(routes_pm._phase_managers, project_id, phase_manager)

    async def fail_persist():
        raise OSError("disk unavailable")

    monkeypatch.setattr(routes_pm, "_get_project", lambda _project_id: SimpleNamespace())
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)
    monkeypatch.setattr(routes_pm, "_persist_all_async", fail_persist)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_pm.revise_pm_requirements(
            project_id,
            routes_pm.RequirementsRevisionRequest(
                content="使用 Vue",
                expected_revision=current["requirements_revision"],
                expected_digest=current["requirements_digest"],
                replace=True,
            ),
        ))

    assert caught.value.status_code == 503
    assert leader.requirements_revision == current["requirements_revision"]
    assert leader.canonical_requirements == "使用 React"
    assert leader.draft_plan == {
        "requirements_revision": current["requirements_revision"],
    }
    assert routes_pm._phase_managers[project_id] is phase_manager


def test_revision_lineage_dto_omits_requirement_content(monkeypatch):
    leader = PMLeaderAgent(hermes_client=_Hermes())
    leader.record_user_requirements("附件中的秘密正文", source="attachment")
    monkeypatch.setattr(routes_pm, "_get_project", lambda _project_id: SimpleNamespace())
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)

    result = asyncio.run(
        routes_pm.get_pm_requirement_revisions("canonical-project")
    )

    assert result["events"][0]["event_id"].startswith("reqevt-")
    assert result["events"][0]["content_digest"].startswith("sha256:")
    assert "content" not in result["events"][0]
    assert "附件中的秘密正文" not in str(result)


def test_requirement_revision_invalidates_unconfirmed_draft_and_old_cas(monkeypatch):
    leader = PMLeaderAgent(hermes_client=_Hermes())
    original = leader.record_user_requirements(REAL_CONTRACT, source="user")
    leader.draft_plan = {"marker": "draft-from-old-revision"}
    leader.project_contract = {"source_requirements": REAL_CONTRACT}
    leader.plan_generation = {
        "requirements_revision": original["requirements_revision"],
        "requirements_digest": original["requirements_digest"],
    }

    leader.record_user_requirements(
        REAL_CONTRACT.replace("React + Vite", "Vue + Vite"),
        source="user", replace=True,
        expected_revision=original["requirements_revision"],
        expected_digest=original["requirements_digest"],
    )
    assert leader.draft_plan is None
    assert leader.final_plan is None
    assert leader.project_contract == {}
    assert leader.plan_generation == {}

    monkeypatch.setattr(routes_pm, "_get_project", lambda _project_id: SimpleNamespace())
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_pm.confirm_pm_plan(
            "canonical-project",
            routes_pm.PlanConfirmRequest(
                modifications="",
                requirements_revision=original["requirements_revision"],
                requirements_digest=original["requirements_digest"],
            ),
        ))
    assert caught.value.status_code == 409


def test_confirm_plan_maps_unsupported_execution_role_to_422(
    monkeypatch,
) -> None:
    from core.phase_manager import UnsupportedExecutionRoleError

    project_id = "unsupported-role-confirmation"
    leader = PMLeaderAgent(hermes_client=_Hermes())
    lineage = leader.record_user_requirements(
        "构建一个小型应用，具体角色由 PM 选择。",
        source="user",
    )
    leader.draft_plan = {"phases": [{"phase_id": "phase-1"}]}
    ctx = SimpleNamespace(subprojects=[])

    def reject_confirmation(*_args, **_kwargs):
        raise UnsupportedExecutionRoleError(["Frontend+QA"])

    monkeypatch.setattr(leader, "confirm_plan", reject_confirmation)
    monkeypatch.setattr(routes_pm, "_get_project", lambda _project_id: ctx)
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)
    monkeypatch.delitem(routes_pm._phase_managers, project_id, raising=False)
    monkeypatch.delitem(routes_pm._supervisor_leaders, project_id, raising=False)
    monkeypatch.delitem(routes_pm._engineer_agents, project_id, raising=False)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_pm.confirm_pm_plan(
            project_id,
            routes_pm.PlanConfirmRequest(
                requirements_revision=lineage["requirements_revision"],
                requirements_digest=lineage["requirements_digest"],
            ),
        ))

    assert caught.value.status_code == 422
    assert caught.value.detail == {
        "code": "unsupported_execution_role",
        "message": "unsupported or ambiguous execution roles: Frontend+QA",
        "roles": ["Frontend+QA"],
    }
    assert leader.draft_plan == {"phases": [{"phase_id": "phase-1"}]}


def test_three_phase_pm_chain_preserves_contract_and_emits_confirmation_signals(
    monkeypatch, tmp_path,
):
    """Component gate: requirements -> plan -> contract -> downstream signals."""
    from core.phase_manager import PhaseManager

    project_id = "three-phase-component-gate"
    requirements = (
        "构建一个提供健康检查接口的小型 FastAPI 服务。"
        "严格规划3阶段，每阶段1任务；"
        "每阶段必须包含任务、实现细节、实现方式、技术栈、职责、"
        "人员数量/分配及验收标准；不得扩展。"
    )
    leader = PMLeaderAgent(
        hermes_client=_Hermes(error=RuntimeError("component gate offline")),
    )
    lineage = leader.record_user_requirements(requirements, source="user")
    phase_manager = PhaseManager(project_id, tmp_path)
    ctx = SimpleNamespace(
        workspace=tmp_path,
        description=requirements,
        subprojects=[],
        qc_results={},
    )

    class _SignalReceiver:
        def __init__(self, method_name):
            self.received = None
            setattr(self, method_name, self._receive)

        def _receive(self, plan):
            self.received = plan

    supervisor = _SignalReceiver("load_project_plan")
    engineer_agents = {}
    persisted = []

    async def persist():
        persisted.append(True)

    monkeypatch.setattr(routes_pm, "_get_project", lambda _project_id: ctx)
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)
    monkeypatch.setattr(
        routes_pm, "_get_phase_manager", lambda _project_id: phase_manager,
    )
    monkeypatch.setattr(
        routes_pm, "_get_supervisor_leader", lambda _project_id: supervisor,
    )
    monkeypatch.setattr(routes_team, "_engineer_agents", engineer_agents)
    monkeypatch.setattr(routes_pm, "_engineer_agents", engineer_agents)
    monkeypatch.setitem(routes_team.projects, project_id, ctx)
    monkeypatch.setitem(routes_team._pm_teams, project_id, leader)
    monkeypatch.setitem(routes_team._phase_managers, project_id, phase_manager)
    monkeypatch.setattr(routes_pm, "_persist_all_async", persist)

    synthesized = asyncio.run(routes_pm.synthesize_pm_plan(project_id, {
        "requirements_revision": lineage["requirements_revision"],
        "requirements_digest": lineage["requirements_digest"],
        "fast_mode": True,
    }))
    confirmed = asyncio.run(routes_pm.confirm_pm_plan(
        project_id,
        routes_pm.PlanConfirmRequest(
            requirements_revision=lineage["requirements_revision"],
            requirements_digest=lineage["requirements_digest"],
        ),
    ))

    assert synthesized["success"] is True, synthesized.get("validation")
    assert synthesized["generation"]["model_status"] == "model_failed"
    assert confirmed["success"] is True, confirmed.get("validation")
    assert confirmed["status"] == "confirmed"
    assert confirmed["can_launch"] is True

    phases = confirmed["plan"]["phases"]
    assert len(phases) == 3
    assert all(
        phase["objective"]
        and phase["work_items"]
        and "task_contract" not in phase
        and "roles_needed" not in phase
        and "required_files" not in phase
        for phase in phases
    )

    locked = confirmed["plan"]["project_contract"]
    assert locked["locked"] is True
    assert locked["phase_count"] == 3
    assert locked["requirements_revision"] == lineage["requirements_revision"]
    assert locked["requirements_digest"] == lineage["requirements_digest"]
    assert len(phase_manager.phases) == 3
    assert len(ctx.subprojects) == 3
    assert supervisor.received is leader.final_plan
    engineer = engineer_agents[project_id]
    assert engineer.final_plan is leader.final_plan
    assert "fastapi" in engineer.project_background.lower()
    assert "阶段一" in engineer.project_background
    assert len(persisted) >= 2


@pytest.mark.parametrize(
    ("tech_stack", "expected"),
    [
        (["FastAPI", "SQLite"], "FastAPI | SQLite"),
        ({"backend": ["FastAPI"], "database": "SQLite"}, "backend: FastAPI"),
        ("FastAPI + SQLite", "FastAPI + SQLite"),
    ],
)
def test_engineer_context_accepts_planning_field_shapes(
    tmp_path, tech_stack, expected,
):
    from agents.fullstack_engineer_agent import FullStackEngineerAgent

    engineer = FullStackEngineerAgent(
        hermes_client=_Hermes(error=RuntimeError("unused")),
        project_id="engineer-shape-gate",
        workspace=str(tmp_path),
    )
    engineer.load_project_context({
        "summary": "健康检查服务",
        "core_features": "健康检查",
        "tech_stack": tech_stack,
        "phases": {
            "phase-1": {
                "phase_id": "phase-1",
                "name": "实现",
                "task_contract": {
                    "task-1": {
                        "task_id": "task-1",
                        "name": "实现健康检查",
                    },
                },
            },
        },
    })

    assert expected in engineer.project_background
    assert "健康检查服务" in engineer.project_background
    assert "实现健康检查" in engineer.project_background


def test_tampered_persisted_requirements_digest_fails_closed():
    leader = PMLeaderAgent(hermes_client=_Hermes())
    leader.record_user_requirements("可信需求", source="user")
    persisted = leader.to_persist()
    persisted["requirements_digest"] = "sha256:" + ("0" * 64)

    restored = PMLeaderAgent(hermes_client=_Hermes())
    with pytest.raises(ValueError, match="snapshot mismatch"):
        restored.from_persist(persisted)


def test_plan_dto_redacts_canonical_requirement_text(monkeypatch):
    leader = PMLeaderAgent(hermes_client=_Hermes())
    leader.draft_plan = {
        "project_contract": {
            "requirements_digest": "sha256:digest",
            "source_requirements": "secret attachment body",
            "requirements_summary": "secret attachment body",
            "requirement_units": [{"unit_id": "u1", "text": "secret attachment body"}],
        },
        "phases": [],
    }
    monkeypatch.setattr(routes_pm, "_get_project", lambda _project_id: SimpleNamespace())
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)

    result = asyncio.run(routes_pm.get_pm_draft_plan("canonical-project"))

    contract = result["draft_plan"]["project_contract"]
    assert contract == {"requirements_digest": "sha256:digest"}


def test_launch_rejects_plan_bound_to_stale_requirements_revision(monkeypatch):
    project_id = "stale-launch-project"
    leader = PMLeaderAgent(hermes_client=_Hermes())
    leader.record_user_requirements("当前需求", source="user")
    leader.plan_confirmed = True
    leader.final_plan = {
        "requirements_revision": 0,
        "requirements_digest": "",
        "artifact_metadata": {
            "requirements_revision": 0,
            "requirements_digest": "",
        },
        "phases": [],
    }
    monkeypatch.setitem(routes_pm._pm_teams, project_id, leader)
    monkeypatch.setattr(routes_pm, "_get_project", lambda _project_id: SimpleNamespace())

    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_pm.launch_project(project_id))

    assert caught.value.status_code == 409
    assert "stale" in caught.value.detail["message"]


def test_collect_analyses_uses_only_canonical_snapshot_and_cas(monkeypatch):
    leader = PMLeaderAgent(hermes_client=_Hermes())
    current = leader.record_user_requirements("权威需求", source="user")
    seen = []

    async def persist():
        return None

    monkeypatch.setattr(
        routes_pm, "_get_project", lambda _project_id: SimpleNamespace(description="")
    )
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)
    monkeypatch.setattr(routes_pm, "_persist_all_async", persist)
    monkeypatch.setattr(
        leader, "collect_member_analyses",
        lambda requirements: seen.append(requirements) or {
            "success": True, "analyses": {},
        },
    )

    with pytest.raises(HTTPException) as injected:
        asyncio.run(routes_pm.collect_pm_analyses(
            "canonical-project",
            {
                "requirements": "客户端注入",
                "requirements_revision": current["requirements_revision"],
                "requirements_digest": current["requirements_digest"],
            },
        ))
    assert injected.value.status_code == 422

    result = asyncio.run(routes_pm.collect_pm_analyses(
        "canonical-project",
        {
            "requirements_revision": current["requirements_revision"],
            "requirements_digest": current["requirements_digest"],
        },
    ))
    assert result["success"] is True
    assert seen == ["权威需求"]


def test_pm_upload_rejects_truncation_that_would_drop_tail_requirements(monkeypatch):
    tail_requirement = "尾部必须使用 Vue 且不得省略"
    payload = (("x" * 50_000) + tail_requirement).encode("utf-8")
    upload = UploadFile(
        file=io.BytesIO(payload),
        filename="requirements.txt",
    )
    monkeypatch.setattr(routes_files, "_get_project", lambda _project_id: SimpleNamespace())

    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_files.upload_file_for_agent(
            "canonical-project",
            agent_type="pm",
            files=[upload],
        ))

    assert caught.value.status_code == 413
    assert caught.value.detail["canonical_requirements_accepted"] is False
    assert caught.value.detail["total_chars"] > 50_000


def test_synthesize_rejects_body_requirement_injection(monkeypatch):
    leader = PMLeaderAgent(hermes_client=_Hermes(error=AssertionError("model called")))
    monkeypatch.setattr(routes_pm, "_get_project", lambda _project_id: SimpleNamespace(description=""))
    monkeypatch.setattr(routes_pm, "_get_pm_team", lambda _project_id: leader)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_pm.synthesize_pm_plan(
            "canonical-project", {
                "requirements": "injected",
                "requirements_revision": 0,
                "requirements_digest": "",
                "fast_mode": True,
            }
        ))
    assert caught.value.status_code == 422
    assert leader.hermes.calls == 0
