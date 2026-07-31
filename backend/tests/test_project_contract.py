from core.project_contract import (
    _task_mapping_is_substantiated,
    _unit_requires_task_substantiation,
    artifact_metadata,
    attach_contract,
    canonical_phase_requirements,
    deterministic_phase_fallback,
    deterministic_plan_fallback,
    extract_project_contract,
    finalize_project_contract,
    freeze_confirmed_project_contract,
    classify_phase_requirements,
    parse_project_contract,
    phase_task_contract,
    validate_phase_plan_layers,
    validate_phase_requirements,
    validate_plan,
    validate_plan_layers,
)
from agents.pm_team import PMLeaderAgent
import copy
import json
import pytest


REQUIREMENTS = """
技术栈固定为 Node.js + Express + TypeScript + SQLite + Prisma + React + Vite。
严格四阶段；只支持 admin 和 employee，不实现公开注册，不建立 roles table 或 permissions table。
"""


def test_contract_rejects_plan_stack_drift_and_forbidden_scope():
    plan = attach_contract(
        {
            "tech_stack": {
                "frontend": "React + TypeScript + Vite",
                "backend": "Python + FastAPI",
                "database": "PostgreSQL",
            },
            "phases": [{"phase_id": "phase-1", "name": "基础"}],
        },
        REQUIREMENTS,
    )
    violations = validate_plan(plan, plan["project_contract"])
    assert any("fastapi" in item.lower() for item in violations)
    assert any("postgresql" in item.lower() for item in violations)


def test_contract_accepts_confirmed_stack_and_phase_count():
    plan = attach_contract(
        {
            "tech_stack": {
                "frontend": "React + TypeScript + Vite",
                "backend": "Node.js + Express + Prisma",
                "database": "SQLite",
            },
            "phases": [{"phase_id": f"phase-{i}"} for i in range(1, 5)],
        },
        REQUIREMENTS,
    )
    assert validate_plan(plan, plan["project_contract"]) == []


@pytest.mark.parametrize(
    "requirements",
    [
        "不要使用 React，技术栈由 PM 选择。",
        "技术栈由专家选择，可考虑 React 或 Vue。",
        "Use React only as an example; PM may choose the stack.",
    ],
)
def test_optional_or_negative_technology_mentions_do_not_lock_stack(
    requirements,
):
    assert parse_project_contract(requirements).technology_stack == ()


def test_negative_technology_scope_is_forbidden_not_required():
    requirements = (
        "Build a locally runnable Node.js service. Do not add a frontend, "
        "database, authentication, Docker, CI, deployment, or third-party "
        "runtime dependencies."
    )

    contract = parse_project_contract(requirements)

    assert contract.technology_stack == ("node.js",)
    assert "docker" in contract.forbidden_scope
    result = validate_plan_layers(
        {
            "technical_requirements": ["Node.js", "Docker"],
            "phases": [{
                "phase_id": "phase-1",
                "name": "Service",
                "technical_requirements": ["Node.js", "Docker"],
            }],
        },
        contract,
    )
    assert "forbidden_scope" in {issue.code for issue in result.issues}


def test_forbidden_scope_negative_acknowledgement_is_not_rejected():
    contract = parse_project_contract("禁止公开注册。")
    result = validate_plan_layers(
        {
            "phases": [{
                "phase_id": "phase-1",
                "name": "Authentication",
                "description": "不提供公开注册，仅允许管理员邀请用户。",
            }],
        },
        contract,
    )

    assert "forbidden_scope" not in {issue.code for issue in result.issues}


def test_contract_extracts_locked_phase_task_count_from_structured_requirement():
    contract = extract_project_contract(
        REQUIREMENTS + " 阶段一锁定任务清单（共4项，顺序固定）。"
    )

    assert contract["phase_task_counts"] == {"phase-1": 4}


def test_contract_extracts_locked_count_from_normal_utf8_chinese():
    requirements = "\u9636\u6bb5\u4e00\u9501\u5b9a\u4efb\u52a1\u6e05\u5355\u5171\u0034\u9879\uff0c\u987a\u5e8f\u56fa\u5b9a"
    contract = extract_project_contract(requirements)
    assert contract["phase_task_counts"] == {"phase-1": 4}


def test_contract_extracts_strict_four_task_boundary_without_count_word():
    requirements = "\u9636\u6bb5\u4e00\u4efb\u52a1\u8fb9\u754c\uff08\u4e25\u683c4\u4e2a\uff0c\u4e0d\u5f97\u62c6\u5206\uff09"
    assert extract_project_contract(requirements)["phase_task_counts"] == {"phase-1": 4}


def test_real_utf8_requirement_blocks_three_task_total_plan():
    requirements = "\u9636\u6bb5\u4e00\u9501\u5b9a\u4efb\u52a1\u6e05\u5355\u5171\u0034\u9879\uff0c\u987a\u5e8f\u56fa\u5b9a"
    plan = {"phases": [{"phase_id": "phase-1", "task_contract": [
        {"task_id": "phase-1-task-1"},
        {"task_id": "phase-1-task-2"},
        {"task_id": "phase-1-task-3"},
    ]}]}
    contract = extract_project_contract(requirements)
    violations = validate_plan(plan, contract)
    assert any("locked task count 3 does not match required 4" in item for item in violations)


def test_validate_plan_rejects_locked_phase_contract_count_drift():
    requirements = REQUIREMENTS + " 阶段一锁定任务清单（共4项，顺序固定）。"
    plan = attach_contract(
        {
            "tech_stack": {
                "frontend": "React + TypeScript + Vite",
                "backend": "Node.js + Express + Prisma",
                "database": "SQLite",
            },
            "phases": [{
                "phase_id": "phase-1",
                "name": "基础",
                "roles_needed": ["后端工程师"],
                "task_contract": [
                    {"task_id": f"phase-1-task-{index}", "name": f"任务{index}"}
                    for index in range(1, 6)
                ],
            }],
        },
        requirements,
    )

    violations = validate_plan(plan, plan["project_contract"])

    assert any("locked task count 5 does not match required 4" in item for item in violations)


def test_phase_task_rejects_out_of_contract_technology():
    contract = extract_project_contract(REQUIREMENTS)
    phase = {
        "phase_id": "phase-1",
        "name": "基础设施与认证",
        "description": "Node.js Express TypeScript SQLite Prisma",
        "roles_needed": ["backend engineer"],
        "deliverables": ["认证模块"],
    }
    tasks = [{
        "task_id": "t1",
        "task_name": "使用 FastAPI 和 PostgreSQL 实现认证",
        "task_description": "建立 roles table",
        "required_role": "backend engineer",
    }]
    violations = validate_phase_requirements(phase, tasks, contract)
    assert any("fastapi" in item.lower() for item in violations)
    assert any("postgresql" in item.lower() for item in violations)
    assert any("roles table" in item.lower() for item in violations)


def test_explicit_phase_task_count_rejects_unbounded_expansion():
    contract = extract_project_contract(REQUIREMENTS + " 任务一、任务二、任务三、任务四")
    phase = {"phase_id": "phase-1", "name": "基础", "description": "Node.js Express"}
    tasks = [
        {"task_id": f"t{i}", "task_name": "Express task", "required_role": "backend engineer"}
        for i in range(1, 10)
    ]
    violations = validate_phase_requirements(phase, tasks, contract)
    assert any("locked count 4" in item for item in violations)


def test_phase_pm_cannot_change_locked_task_count():
    contract = extract_project_contract(REQUIREMENTS + " 任务一、任务二、任务三、任务四")
    phase = {"phase_id": "phase-1", "name": "基础", "description": "Node.js Express"}
    tasks = [
        {"task_id": f"t{i}", "task_name": "Express task", "required_role": "backend engineer"}
        for i in range(1, 10)
    ]
    result = classify_phase_requirements(phase, tasks, contract)
    assert any("locked count 4" in item for item in result["hard"])
    assert result["warnings"] == []


def test_canonical_phase_requirements_inherit_locked_ids_and_roles():
    phase = {
        "phase_id": "phase-1",
        "roles_needed": ["fullstack engineer"],
        "task_contract": [
            {"task_id": f"phase-1-task-{index}", "name": f"锁定任务 {index}"}
            for index in range(1, 6)
        ],
    }

    requirements = canonical_phase_requirements(phase)

    assert [item["task_id"] for item in requirements] == [
        f"phase-1-task-{index}" for index in range(1, 6)
    ]
    assert {item["required_role"] for item in requirements} == {"fullstack engineer"}
    assert validate_phase_requirements(phase, requirements, {}) == []


def test_phase_task_contract_assigns_stable_ids_to_deliverables():
    phase = {"phase_id": "phase-2", "deliverables": ["导入文件", "版本控制"]}

    assert [item["task_id"] for item in phase_task_contract(phase)] == [
        "phase-2-deliverable-1",
        "phase-2-deliverable-2",
    ]


def test_phase_plan_rejects_unresolved_technology_choices_even_without_stack_lock():
    phase = {
        "phase_id": "phase-1",
        "roles_needed": ["后端开发工程师"],
        "task_contract": [{"task_id": "phase-1-task-1", "name": "项目骨架"}],
    }
    requirements = [{
        "task_id": "phase-1-task-1",
        "task_name": "项目骨架",
        "task_description": "使用 Vue/React 和 better-sqlite3 或 sequelize",
        "required_role": "后端开发工程师",
    }]

    violations = validate_phase_requirements(phase, requirements, {})

    assert any("unresolved technology choice" in item for item in violations)


def test_phase_plan_rejects_inconsistent_roles_and_default_credentials():
    phase = {
        "phase_id": "phase-1",
        "roles_needed": ["后端开发工程师"],
        "task_contract": [
            {"task_id": "phase-1-task-1", "name": "登录"},
            {"task_id": "phase-1-task-2", "name": "初始化管理员"},
        ],
    }
    requirements = [
        {
            "task_id": "phase-1-task-1",
            "task_name": "登录",
            "task_description": "支持管理员和员工登录",
            "required_role": "后端开发工程师",
        },
        {
            "task_id": "phase-1-task-2",
            "task_name": "初始化管理员",
            "task_description": "创建默认管理员账号，用户名 admin，密码加密",
            "required_role": "后端开发工程师",
        },
    ]

    requirements[0]["task_description"] += "，权限模型为 admin/user"
    violations = validate_phase_requirements(phase, requirements, {})

    assert any("inconsistent role model" in item for item in violations)
    assert any("environment variables" in item for item in violations)


class _Hermes:
    def __init__(self, plan):
        self.plan = plan

    def chat(self, _messages):
        return {"content": json.dumps(self.plan)}


def test_pm_leader_rejects_invalid_llm_plan_and_uses_valid_fallback():
    plan = {
        "tech_stack": {"frontend": "React", "backend": "Python + FastAPI", "database": "PostgreSQL"},
        "phases": [{"phase_id": f"phase-{i}"} for i in range(1, 5)],
    }
    leader = PMLeaderAgent(hermes_client=_Hermes(plan))
    result = leader.synthesize_plan_fast(REQUIREMENTS)
    assert result["success"] is True
    assert result["status"] == "saved"
    assert result["can_confirm"] is True
    assert result["generation"]["model_status"] == "validation_failed"
    assert result["draft_plan"]["source"] == "deterministic_contract_fallback"
    assert leader.draft_plan is result["draft_plan"]


REAL_FOUR_PHASE_REQUIREMENT = """
技术栈固定为 Node.js + Express + TypeScript + SQLite + Prisma + React + Vite。
严格四阶段。
角色集合：产品经理、前端工程师、后端工程师、测试工程师
禁止范围：公开注册、roles table
来源约束：所有交付物必须引用原始需求

阶段一：项目基础
角色集合：产品经理、前端工程师、后端工程师
任务一：仓库骨架 | 角色：后端工程师 | 验收：可以启动 | 来源：原始需求-1
任务二：数据模型 | 角色：后端工程师 | 验收：迁移可重复执行 | 依赖：phase-1-task-1 | 来源：原始需求-2
任务三：登录页面 | 角色：前端工程师 | 验收：登录成功 | 依赖：phase-1-task-2 | 来源：原始需求-3
任务四：联调基线 | 角色：产品经理 | 验收：接口联通 | 依赖：phase-1-task-3 | 来源：原始需求-4

阶段二：业务 API
角色集合：后端工程师
任务一：业务接口 | 角色：后端工程师 | 依赖：phase-1-task-4 | 来源：原始需求-5

阶段三：用户界面
角色集合：前端工程师
任务一：业务页面 | 角色：前端工程师 | 依赖：phase-2-task-1 | 来源：原始需求-6

阶段四：验收发布
角色集合：测试工程师
任务一：验收测试 | 角色：测试工程师 | 依赖：phase-3-task-1 | 来源：原始需求-7
"""


def test_real_utf8_chinese_requirement_becomes_immutable_complete_contract():
    model = parse_project_contract(REAL_FOUR_PHASE_REQUIREMENT)
    contract = extract_project_contract(REAL_FOUR_PHASE_REQUIREMENT)

    assert model.contract_version == 3
    assert model.phase_count == 4
    assert model.technology_stack == (
        "node.js", "express", "typescript", "sqlite", "prisma", "react", "vite"
    )
    assert [phase.phase_id for phase in model.phases] == ["phase-1", "phase-2", "phase-3", "phase-4"]
    assert [task.task_id for task in model.phases[0].tasks] == [
        "phase-1-task-1", "phase-1-task-2", "phase-1-task-3", "phase-1-task-4"
    ]
    assert [task.name for task in model.phases[0].tasks] == ["仓库骨架", "数据模型", "登录页面", "联调基线"]
    assert model.phases[0].tasks[1].dependencies == ("phase-1-task-1",)
    assert model.phases[0].tasks[0].source_constraints == ("原始需求-1",)
    assert "公开注册" in model.forbidden_scope
    assert contract["phase_task_counts"]["phase-1"] == 4

    with pytest.raises(TypeError, match="immutable"):
        contract["required_phase_count"] = 3


def test_three_layer_plan_validation_rejects_4_to_3_and_locked_field_drift():
    contract = parse_project_contract(REAL_FOUR_PHASE_REQUIREMENT)
    plan = deterministic_plan_fallback(contract)
    assert validate_plan_layers(plan, contract).valid is True

    plan["phases"] = plan["phases"][:3]
    plan["phases"][0]["task_contract"][0]["task_id"] = "t1"
    plan["phases"][0]["task_contract"][1]["name"] = "模型建议的新名称"
    result = validate_plan_layers(plan, contract)

    assert result.valid is False
    assert {issue.layer for issue in result.issues} >= {"project_contract", "phase_contract"}
    assert {issue.code for issue in result.issues} >= {
        "phase_count_drift", "phase_id_or_order_drift", "task_id_or_order_drift", "task_name_drift"
    }


def test_phase_validation_rejects_role_stack_option_and_dependency_drift():
    contract = parse_project_contract(REAL_FOUR_PHASE_REQUIREMENT)
    phase = deterministic_plan_fallback(contract)["phases"][1]
    requirements = deterministic_phase_fallback(phase, contract)
    assert validate_phase_plan_layers(phase, requirements, contract).valid is True

    requirements[0]["required_role"] = "前端工程师"
    requirements[0]["task_description"] = "使用 Vue/React 实现，并切换到 PostgreSQL"
    requirements[0]["dependencies"] = ["phase-9-task-1"]
    result = validate_phase_plan_layers(phase, requirements, contract)

    assert result.valid is False
    codes = {issue.code for issue in result.issues}
    assert {"role_out_of_scope", "unresolved_option", "technology_conflict", "dependency_drift", "dependency_untraceable"} <= codes


def test_phase_validation_accepts_declared_cross_phase_dependency_but_not_undeclared_one():
    contract = parse_project_contract(REAL_FOUR_PHASE_REQUIREMENT)
    plan = deterministic_plan_fallback(contract)
    phase = plan["phases"][1]
    requirements = deterministic_phase_fallback(phase, contract)

    assert requirements[0]["dependencies"] == ["phase-1-task-4"]
    assert validate_phase_plan_layers(phase, requirements, contract).valid is True

    requirements[0]["dependencies"] = ["phase-1-task-3"]
    result = validate_phase_plan_layers(phase, requirements, contract)
    assert any(issue.code in {"dependency_drift", "cross_phase_dependency"} for issue in result.issues)


def test_phase_validation_uses_confirmed_manifest_when_projection_omits_files():
    plan = {
        "phases": [{
            "phase_id": "phase-1",
            "name": "Backend foundation",
            "roles_needed": ["backend"],
            "tasks": [{
                "task_id": "phase-1-task-1",
                "name": "Create runtime",
                "description": "Create the runnable backend files.",
                "implementation": "Implement and verify the Node.js service.",
                "tech_stack": ["Node.js"],
                "roles": ["backend"],
                "responsibilities": ["backend: implement and verify"],
                "personnel_count": 1,
                "personnel_allocation": ["backend: 1"],
                "acceptance_criteria": ["runtime files exist"],
                "required_files": [
                    ".gitignore",
                    "package.json",
                    "server.js",
                ],
            }],
        }],
    }
    contract = finalize_project_contract(
        parse_project_contract("Build a small Node.js backend."),
        plan,
    )
    projected_phase = {
        **plan["phases"][0],
        "task_contract": [{
            key: value
            for key, value in plan["phases"][0]["tasks"][0].items()
            if key != "required_files"
        }],
    }
    projected_phase.pop("tasks")
    candidate = canonical_phase_requirements(projected_phase)

    result = validate_phase_plan_layers(
        projected_phase, candidate, contract,
    )
    drift = next(
        issue for issue in result.issues
        if issue.code == "required_files_drift"
    )

    assert set(drift.expected) == {
        ".gitignore",
        "package.json",
        "README.md",
        "server.js",
    }
    assert drift.actual == []
    assert deterministic_phase_fallback(
        projected_phase, contract,
    )[0]["required_files"] == [
        ".gitignore",
        "package.json",
        "README.md",
        "server.js",
    ]


def test_json_schema_layer_and_metadata_are_structured_and_deterministic():
    contract = parse_project_contract(REAL_FOUR_PHASE_REQUIREMENT)
    phase = deterministic_plan_fallback(contract)["phases"][0]
    invalid = [{"task_id": "phase-1-task-1", "task_name": "仓库骨架", "required_role": "后端工程师"}]

    result = validate_phase_plan_layers(phase, invalid, contract)
    metadata = artifact_metadata(
        "phase_plan", 3, "llm", result,
        corrections=[{"path": "$[0].priority", "from": None, "to": "normal"}],
    )

    assert any(issue.layer == "json_schema" and issue.path == "$[0].task_description" for issue in result.issues)
    assert metadata["version"] == 3
    assert metadata["source"] == "llm"
    assert metadata["contract_version"] == 3
    assert metadata["validation"]["valid"] is False
    assert metadata["auto_corrections"][0]["path"] == "$[0].priority"


def test_deterministic_fallback_is_valid_when_model_json_is_missing_or_invalid():
    contract = parse_project_contract(REAL_FOUR_PHASE_REQUIREMENT)
    fallback = deterministic_plan_fallback(contract)

    assert fallback == deterministic_plan_fallback(contract)
    assert fallback["source"] == "deterministic_contract_fallback"
    assert validate_plan_layers(fallback, contract).valid is True
    for phase in fallback["phases"]:
        phase_fallback = deterministic_phase_fallback(phase, contract)
        assert validate_phase_plan_layers(phase, phase_fallback, contract).valid is True


def test_exact_phase_count_empty_skeleton_builds_executable_fallback():
    requirements = (
        "严格划分为2个阶段。构建团队工单平台；支持工单创建与分派；"
        "提供状态流转和操作历史；包含响应式界面、运行说明和自动化测试。"
    )
    contract = parse_project_contract(requirements)
    fallback = deterministic_plan_fallback(contract)

    assert contract.phase_count == 2
    assert all(not phase.tasks for phase in contract.phases)
    assert len(fallback["phases"]) == 2
    assert validate_plan_for_confirmation(
        fallback,
        contract,
        expected_source_requirements=requirements,
    ).valid is True

    source_ids = {
        unit.unit_id
        for unit in contract.requirement_units
        if _unit_requires_task_substantiation(unit)
    }
    covered_ids = {
        source_id
        for phase in fallback["phases"]
        for task in phase["task_contract"]
        for source_id in task["source_requirement_ids"]
    }
    assert covered_ids == source_ids

    previous_task_id = None
    for phase in fallback["phases"]:
        assert phase["task_contract"]
        assert phase["roles_needed"]
        assert phase["tech_stack"]
        assert phase["agent_count"] >= 1
        assert phase["responsibilities"]
        assert phase["personnel_allocation"]
        assert phase["acceptance_criteria"]
        for task in phase["task_contract"]:
            assert task["implementation"]
            assert task["tech_stack"]
            assert task["roles"]
            assert task["personnel_count"] >= 1
            assert task["responsibilities"]
            assert task["personnel_allocation"]
            assert task["acceptance_criteria"]
            assert task["dependencies"] == (
                [previous_task_id] if previous_task_id else []
            )
            previous_task_id = task["task_id"]


def test_domain_stage_counts_do_not_lock_project_phase_count():
    contract = parse_project_contract(
        "系统支持3阶段审批，并展示三阶段状态流转。"
    )

    assert contract.phase_count is None


def test_phase_roles_and_data_counts_do_not_lock_task_count():
    contract = parse_project_contract(
        "严格规划1阶段；阶段1包含3个角色和2个数据表。"
    )

    assert contract.phase_count == 1
    assert len(contract.phases[0].tasks) == 0


def test_explicit_phase_task_count_is_still_locked():
    contract = parse_project_contract(
        "严格规划1阶段；阶段1共2个任务。"
    )

    assert len(contract.phases[0].tasks) == 2


def test_minimum_phase_count_with_modifiers_remains_a_lower_bound():
    requirements = (
        "请规划至少 6 个职责清晰、依赖合理的阶段。"
        "构建团队工单平台并提供自动化测试。"
    )
    contract = parse_project_contract(requirements)
    fallback = deterministic_plan_fallback(contract)

    assert contract.phase_count is None
    assert contract.minimum_phase_count == 6
    assert len(fallback["phases"]) >= 6
    assert all(phase["task_contract"] for phase in fallback["phases"])
    assert validate_plan_for_confirmation(
        fallback,
        contract,
        expected_source_requirements=requirements,
    ).valid is True


def test_confirmation_rejects_incomplete_and_placeholder_execution_fields():
    requirements = "必须实现工单创建。"
    contract = parse_project_contract(requirements)
    source_id = contract.requirement_units[0].unit_id
    plan = {
        "plan_version": 1,
        "tech_stack": ["Node.js"],
        "phases": [{
            "phase_id": "phase-1",
            "name": "工单实现",
            "description": "待定",
            "implementation": "TBD",
            "tech_stack": [],
            "roles_needed": ["待阶段PM分配"],
            "responsibilities": [],
            "agent_count": None,
            "personnel_allocation": None,
            "acceptance_criteria": ["待确认"],
            "task_contract": [{
                "task_id": "phase-1-task-1",
                "name": "工单创建",
                "description": "待补充",
                "implementation": "todo",
                "tech_stack": [],
                "roles": ["待分配"],
                "responsibilities": [],
                "personnel_count": 0,
                "personnel_allocation": [],
                "source_requirement_ids": [source_id],
                "acceptance_criteria": ["待定"],
                "dependencies": [],
            }],
        }],
        "project_contract": contract.as_mapping(),
    }

    result = validate_plan_for_confirmation(plan, contract)
    codes = {issue.code for issue in result.issues}

    assert {
        "phase_implementation_details_missing",
        "phase_implementation_method_missing",
        "phase_technology_stack_missing",
        "phase_role_missing",
        "phase_responsibilities_missing",
        "phase_personnel_missing",
        "phase_acceptance_criteria_missing",
        "task_implementation_details_missing",
        "task_implementation_method_missing",
        "task_technology_stack_missing",
        "task_role_missing",
        "task_responsibilities_missing",
        "task_personnel_missing",
        "task_acceptance_criteria_missing",
    } <= codes


def test_phase_fallback_contains_rich_executable_task_fields():
    contract = parse_project_contract(
        "严格划分为2个阶段。必须实现工单创建和自动化测试。"
    )
    phase = deterministic_plan_fallback(contract)["phases"][0]
    requirements = deterministic_phase_fallback(phase, contract)

    assert validate_phase_plan_layers(
        phase, requirements, contract
    ).valid is True
    for task in requirements:
        assert task["task_description"]
        assert task["implementation"]
        assert task["tech_stack"]
        assert task["required_role"]
        assert task["responsibilities"]
        assert task["personnel_count"] >= 1
        assert task["personnel_allocation"]
        assert task["acceptance_criteria"]


def test_deterministic_fallback_hydrates_explicit_four_phase_traceability():
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
角色集合：测试工程师
任务一：完成全流程验收并发布
"""
    contract = parse_project_contract(requirements)
    fallback = deterministic_plan_fallback(contract)

    result = validate_plan_for_confirmation(
        fallback, contract, expected_source_requirements=requirements
    )

    assert result.valid is True, result.to_dict()
    assert len(fallback["phases"]) == 4
    for phase in fallback["phases"]:
        assert phase["acceptance_criteria"]
        assert phase["task_contract"]
        for task in phase["task_contract"]:
            assert task["source_requirement_ids"]
            assert task["acceptance_criteria"]

    binding_ids = {
        unit.unit_id
        for unit in contract.requirement_units
        if unit.binding and _unit_requires_task_substantiation(unit)
    }
    covered_ids = {
        source_id
        for phase in fallback["phases"]
        for task in phase["task_contract"]
        for source_id in task["source_requirement_ids"]
    }
    assert binding_ids <= covered_ids


def test_industrial_acceptance_requirement_has_valid_deterministic_fallback():
    from scripts.industrial_acceptance import PROJECT_REQUIREMENTS

    contract = parse_project_contract(PROJECT_REQUIREMENTS)
    fallback = deterministic_plan_fallback(contract)
    result = validate_plan_for_confirmation(
        fallback,
        contract,
        expected_source_requirements=PROJECT_REQUIREMENTS,
    )

    assert result.valid is True, result.to_dict()
    assert len(fallback["phases"]) == 4
    assert [len(phase["task_contract"]) for phase in fallback["phases"]] == [
        3, 1, 2, 3
    ]


def test_plan_validation_normalizes_contract_technology_case():
    contract = extract_project_contract(
        "Use Node.js 20, Express, SQLite, React, TypeScript and Vite."
    )
    plan = {
        "technology_stack": [
            "Node.js 20", "Express", "SQLite", "React", "TypeScript", "Vite"
        ],
        "phases": [],
    }

    result = validate_plan_layers(plan, contract)

    assert not any(issue.code == "technology_missing" for issue in result.issues)


def test_model_selected_phase_technologies_validate_after_contract_finalization():
    plan = {
        "phases": [{
            "phase_id": "phase-1",
            "name": "Delivery",
            "roles_needed": ["fullstack engineer"],
            "tech_stack": [
                "HTML5", "CSS3", "Node.js", "Jest", "Supertest", "Cypress",
            ],
            "tasks": [{
                "task_id": "phase-1-task-1",
                "name": "Build and test the app",
                "roles": ["fullstack engineer"],
                "tech_stack": [
                    "HTML5", "CSS3", "Node.js", "Jest", "Supertest", "Cypress",
                ],
            }],
        }],
    }
    locked = finalize_project_contract(
        parse_project_contract("Build and test a small web application."),
        plan,
    )

    result = validate_plan_layers(plan, locked)

    assert not any(issue.code == "technology_missing" for issue in result.issues)


def test_phase_fallback_preserves_existing_role_when_locked_role_is_empty():
    raw_plan = {
        "technology_stack": ["Node.js", "Express"],
        "phases": [{
            "phase_id": "1",
            "name": "Foundation",
            "tasks": [{"task_id": "1", "name": "Create backend"}],
        }],
    }
    contract = finalize_project_contract(
        parse_project_contract("Use Node.js and Express."), raw_plan
    )
    phase = {
        **raw_plan["phases"][0],
        "roles_needed": ["backend"],
    }

    requirements = deterministic_phase_fallback(phase, contract)

    assert requirements[0]["required_role"] == "backend"
    assert validate_phase_plan_layers(phase, requirements, contract).valid is True
from core.project_contract import (
    extract_requirement_units,
    parse_project_contract,
    validate_plan_for_confirmation,
    _shared_package_contract_issues,
    normalize_shared_package_contract,
)


def _valid_freeform_plan(requirements: str):
    contract = parse_project_contract(requirements)
    source_ids = [item.unit_id for item in contract.requirement_units]
    plan = {
        "plan_version": 1,
        "tech_stack": ["Node.js"],
        "phases": [{
            "phase_id": "phase-1",
            "name": "实现",
            "description": "实现库存录入与双人审批的完整业务流程",
            "implementation": "使用 Node.js 分层实现并通过自动化测试验证",
            "tech_stack": ["Node.js"],
            "roles_needed": ["backend"],
            "responsibilities": ["backend：负责实现、测试和交付"],
            "agent_count": 1,
            "personnel_allocation": ["backend：1 人"],
            "acceptance_criteria": ["全部任务通过"],
            "task_contract": [{
                "task_id": "task-1",
                "name": "实现库存审批",
                "description": "实现库存录入和双人审批",
                "implementation": "实现库存服务并自动验证双人审批规则",
                "tech_stack": ["Node.js"],
                "roles": ["backend"],
                "responsibilities": ["backend：负责库存审批实现和验证"],
                "personnel_count": 1,
                "personnel_allocation": ["backend：1 人"],
                "source_requirement_ids": source_ids,
                "acceptance_criteria": ["库存可录入且审批需要两人"],
                "dependencies": [],
            }],
        }],
        "project_contract": contract.as_mapping(),
    }
    return contract, plan


def test_freeform_chinese_outline_becomes_stable_requirement_units():
    requirements = "先实现库存录入，再实现审批。审批必须双人复核！最后允许出库。"
    first = extract_requirement_units(requirements)
    second = extract_requirement_units(requirements)

    assert first == second
    assert len(first) == 3
    assert [item.order for item in first] == [1, 2, 3]
    assert all(item.unit_id.startswith(f"req-{index:03d}-") for index, item in enumerate(first, 1))
    assert all(item.binding for item in first)


def test_freeform_outline_does_not_require_phase_task_labels():
    requirements = "先实现库存录入，再实现审批。审批必须双人复核。"
    contract, plan = _valid_freeform_plan(requirements)

    result = validate_plan_for_confirmation(
        plan, contract, expected_source_requirements=requirements
    )

    assert result.valid is True


def test_confirmation_rejects_uncovered_binding_requirement():
    requirements = "先实现库存录入。最后必须支持出库。"
    contract, plan = _valid_freeform_plan(requirements)
    plan["phases"][0]["task_contract"][0]["source_requirement_ids"] = [
        contract.requirement_units[0].unit_id
    ]

    result = validate_plan_for_confirmation(plan, contract)

    assert "binding_requirement_uncovered" in {issue.code for issue in result.issues}


def test_confirmation_rejects_unsubstantiated_binding_source_mapping():
    requirements = "必须实现库存审批。"
    contract, plan = _valid_freeform_plan(requirements)
    task = plan["phases"][0]["task_contract"][0]
    task["name"] = "用户登录"
    task["description"] = "创建登录表单和会话"
    task["implementation"] = "实现登录表单和用户会话"
    task["implementation_method"] = "编写用户登录代码"
    task["responsibilities"] = ["负责用户登录功能"]
    task["required_files"] = ["src/login.py"]
    task["acceptance_criteria"] = ["用户可以登录"]

    result = validate_plan_for_confirmation(plan, contract)

    assert "task_source_mapping_unsubstantiated" in {
        issue.code for issue in result.issues
    }


def test_task_implementation_fields_substantiate_source_mapping():
    requirements = "Build an Express TODO API with POST /api/todos."
    contract, plan = _valid_freeform_plan(requirements)
    task = plan["phases"][0]["task_contract"][0]
    task["name"] = "Backend implementation"
    task["description"] = "Create the server."
    task["acceptance_criteria"] = ["The server starts successfully."]
    task["implementation"] = (
        "Use Express to implement the TODO API and POST /api/todos."
    )
    task["implementation_method"] = "Define and validate the Express route."
    task["required_files"] = ["src/server.js"]

    result = validate_plan_for_confirmation(plan, contract)

    assert "task_source_mapping_unsubstantiated" not in {
        issue.code for issue in result.issues
    }


def test_task_traceability_ignores_plan_governance_and_negative_scope_clauses():
    requirement = parse_project_contract(
        "Build a locally runnable Node.js and Express TODO REST API with no "
        "frontend and no Docker. Plan exactly 2 dependency-ordered phases. "
        "Phase 1 owns package.json, src/server.js, and README.md;"
    ).requirement_units[0]
    task = {
        "name": "Implement TODO REST API",
        "description": "Build the Node.js and Express TODO REST API.",
        "implementation": "Create package.json and src/server.js.",
        "required_files": ["package.json", "src/server.js", "README.md"],
    }

    assert _task_mapping_is_substantiated(requirement, task) is True


def test_outline_requirement_can_be_substantiated_by_phase_aggregate():
    requirements = (
        "Build a locally runnable Node.js and Express TODO REST API with no "
        "frontend and no Docker. Plan exactly 1 phase. Phase 1 owns "
        "package.json, src/server.js, and README.md;"
    )
    contract, plan = _valid_freeform_plan(requirements)
    phase = plan["phases"][0]
    phase["name"] = "Node.js Express TODO REST API"
    phase["description"] = requirements
    phase["tech_stack"] = ["Node.js", "Express"]
    task = phase["task_contract"][0]
    task["name"] = "Backend implementation"
    task["description"] = "Implement the phase deliverable."
    task["implementation"] = "Create the locked files."
    task["required_files"] = ["package.json", "src/server.js", "README.md"]

    result = validate_plan_for_confirmation(plan, contract)

    assert "task_source_mapping_unsubstantiated" not in {
        issue.code for issue in result.issues
    }


def test_compound_binding_requirement_requires_evidence_for_each_obligation():
    requirements = (
        "The system must encrypt sensitive data at rest and record every access "
        "in the audit log."
    )
    contract, plan = _valid_freeform_plan(requirements)
    task = plan["phases"][0]["task_contract"][0]
    task["name"] = "Audit logging"
    task["description"] = "Record every access in the audit log."
    task["acceptance_criteria"] = ["Every access produces an audit entry."]

    result = validate_plan_for_confirmation(plan, contract)

    assert "task_source_mapping_unsubstantiated" in {
        issue.code for issue in result.issues
    }


def test_short_generic_binding_requirement_fails_closed_without_evidence():
    requirements = "必须好用。"
    contract, plan = _valid_freeform_plan(requirements)
    task = plan["phases"][0]["task_contract"][0]
    task["name"] = "完成核心流程"
    task["description"] = "交付用户要求的核心流程"

    result = validate_plan_for_confirmation(plan, contract)

    assert "task_source_mapping_unsubstantiated" in {
        issue.code for issue in result.issues
    }


def test_each_task_requires_source_and_acceptance_criteria():
    requirements = "必须实现库存录入。"
    contract, plan = _valid_freeform_plan(requirements)
    task = plan["phases"][0]["task_contract"][0]
    task["source_requirement_ids"] = []
    task["acceptance_criteria"] = []

    result = validate_plan_for_confirmation(plan, contract)
    codes = {issue.code for issue in result.issues}

    assert "task_source_requirements_empty" in codes
    assert "task_acceptance_criteria_empty" in codes


def test_single_shared_package_owner_must_declare_later_test_dependencies():
    phases = [
        {
            "phase_id": "phase-1",
            "tasks": [{
                "task_id": "phase-1-task-1",
                "description": "Create the Express backend manifest.",
                "tech_stack": ["Express", "better-sqlite3"],
                "required_files": ["package.json", "server.js"],
            }],
        },
        {
            "phase_id": "phase-2",
            "tasks": [{
                "task_id": "phase-2-task-1",
                "description": "Create API integration tests.",
                "tech_stack": ["Jest", "Supertest"],
                "required_files": ["test/api.test.js"],
            }],
        },
        {
            "phase_id": "phase-3",
            "tasks": [{
                "task_id": "phase-3-task-1",
                "description": "Create browser tests.",
                "tech_stack": ["Cypress"],
                "required_files": ["test/e2e/todos.cy.js"],
            }],
        },
    ]

    issues = _shared_package_contract_issues(phases)

    assert [issue.code for issue in issues] == [
        "shared_package_dependency_contract_incomplete"
    ]
    assert "jest" in issues[0].message
    assert "supertest" in issues[0].message
    assert "cypress" in issues[0].message

    phases[0]["tasks"][0]["description"] += (
        " Include Jest, Supertest and Cypress dependencies and scripts."
    )
    assert _shared_package_contract_issues(phases) == []


def test_shared_package_owner_must_declare_later_npm_scripts():
    phases = [
        {
            "phase_id": "phase-1",
            "tasks": [{
                "task_id": "phase-1-task-1",
                "description": "Create package.json with the npm start script.",
                "tech_stack": ["Node.js", "Express"],
                "required_files": ["package.json", "src/server.js"],
            }],
        },
        {
            "phase_id": "phase-2",
            "tasks": [{
                "task_id": "phase-2-task-1",
                "description": "Create tests run by the npm test script.",
                "tech_stack": ["Node.js", "node:test"],
                "required_files": ["tests/todos.test.js"],
            }],
        },
    ]

    issues = _shared_package_contract_issues(phases)

    assert [issue.code for issue in issues] == [
        "shared_package_dependency_contract_incomplete"
    ]
    assert "script:test" in issues[0].message

    phases[0]["tasks"][0]["description"] += (
        " Preconfigure the npm test script for the later test phase."
    )
    assert _shared_package_contract_issues(phases) == []


def test_shared_package_owner_accepts_structured_manifest_declarations():
    phases = [
        {
            "phase_id": "phase-1",
            "tasks": [{
                "task_id": "phase-1-task-1",
                "description": "Create the Express backend manifest.",
                "tech_stack": ["Node.js", "Express"],
                "package_dependencies": ["express"],
                "package_scripts": {
                    "start": "node src/server.js",
                    "test": "node --test tests/todos.test.js",
                },
                "required_files": ["package.json", "src/server.js"],
            }],
        },
        {
            "phase_id": "phase-2",
            "tasks": [{
                "task_id": "phase-2-task-1",
                "description": "Run npm test for the Node.js API.",
                "tech_stack": ["Node.js", "node:test"],
                "required_files": ["tests/todos.test.js"],
            }],
        },
    ]

    assert _shared_package_contract_issues(phases) == []


def test_shared_package_normalizer_enriches_owner_from_later_npm_commands():
    phases = [
        {
            "phase_id": "phase-1",
            "tasks": [{
                "task_id": "phase-1-task-1",
                "description": "Create the Express backend manifest.",
                "tech_stack": ["Node.js", "Express"],
                "required_files": ["package.json", "src/server.js"],
            }],
        },
        {
            "phase_id": "phase-2",
            "tasks": [{
                "task_id": "phase-2-task-1",
                "description": "Verify npm start and npm test.",
                "tech_stack": ["Node.js", "node:test"],
                "required_files": ["tests/todos.test.js"],
            }],
        },
    ]
    plan = {"phases": phases}

    assert [issue.code for issue in _shared_package_contract_issues(phases)] == [
        "shared_package_dependency_contract_incomplete"
    ]

    normalize_shared_package_contract(plan)

    owner = phases[0]["tasks"][0]
    assert owner["package_dependencies"] == ["express"]
    assert owner["package_scripts"] == {
        "start": "node src/server.js",
        "test": "node --test tests/todos.test.js",
    }
    assert _shared_package_contract_issues(phases) == []


def test_node_plan_requires_exactly_one_root_package_owner():
    phases = [{
        "phase_id": "phase-1",
        "tech_stack": ["Node.js", "Express"],
        "tasks": [{
            "task_id": "phase-1-task-1",
            "description": "Implement the Express API.",
            "tech_stack": ["Node.js", "Express"],
            "required_files": ["backend/server.js"],
        }],
    }]

    assert [issue.code for issue in _shared_package_contract_issues(phases)] == [
        "shared_package_owner_missing"
    ]

    phases[0]["tasks"][0]["required_files"].append("package.json")
    phases.append({
        "phase_id": "phase-2",
        "tasks": [{
            "task_id": "phase-2-task-1",
            "description": "Own the duplicate root manifest.",
            "required_files": ["package.json"],
        }],
    })
    assert [issue.code for issue in _shared_package_contract_issues(phases)] == [
        "shared_package_owner_ambiguous"
    ]


def test_nested_package_manifests_are_not_root_package_owners():
    phases = [{
        "phase_id": "phase-1",
        "tech_stack": ["Node.js"],
        "tasks": [{
            "task_id": "phase-1-task-1",
            "description": "Implement both application packages.",
            "required_files": [
                "backend/package.json",
                "frontend/package.json",
            ],
        }],
    }]

    assert [issue.code for issue in _shared_package_contract_issues(phases)] == [
        "shared_package_owner_missing"
    ]


def test_dependency_cycle_and_future_phase_are_rejected():
    requirements = "必须先完成基础，再实现业务。"
    contract = parse_project_contract(requirements)
    source_ids = [item.unit_id for item in contract.requirement_units]
    plan = {
        "tech_stack": ["Node.js"],
        "phases": [
            {
                "phase_id": "phase-1", "name": "基础", "roles_needed": ["backend"],
                "acceptance_criteria": ["基础完成"],
                "task_contract": [{
                    "task_id": "task-1", "name": "基础", "roles": ["backend"],
                    "source_requirement_ids": source_ids,
                    "acceptance_criteria": ["基础通过"], "dependencies": ["task-2"],
                }],
            },
            {
                "phase_id": "phase-2", "name": "业务", "roles_needed": ["backend"],
                "acceptance_criteria": ["业务完成"],
                "task_contract": [{
                    "task_id": "task-2", "name": "业务", "roles": ["backend"],
                    "source_requirement_ids": source_ids,
                    "acceptance_criteria": ["业务通过"], "dependencies": ["task-1"],
                }],
            },
        ],
        "project_contract": contract.as_mapping(),
    }

    result = validate_plan_for_confirmation(plan, contract)
    codes = {issue.code for issue in result.issues}

    assert "dependency_future_phase" in codes
    assert "dependency_cycle" in codes


def test_task_ids_must_be_globally_unique_across_phases():
    requirements = "The system must provide inventory management."
    contract = parse_project_contract(requirements)
    source_ids = [item.unit_id for item in contract.requirement_units]
    task = {
        "task_id": "shared-task",
        "name": "Inventory management",
        "roles": ["backend"],
        "source_requirement_ids": source_ids,
        "acceptance_criteria": ["Inventory management passes acceptance."],
    }
    plan = {
        "tech_stack": ["Python"],
        "phases": [
            {
                "phase_id": "phase-1",
                "name": "Foundation",
                "roles_needed": ["backend"],
                "acceptance_criteria": ["Foundation complete"],
                "task_contract": [dict(task)],
            },
            {
                "phase_id": "phase-2",
                "name": "Delivery",
                "roles_needed": ["backend"],
                "acceptance_criteria": ["Delivery complete"],
                "task_contract": [dict(task)],
            },
        ],
        "project_contract": contract.as_mapping(),
    }

    result = validate_plan_for_confirmation(plan, contract)

    assert "task_id_duplicate" in {issue.code for issue in result.issues}


def test_plain_outline_requirement_unit_cannot_be_omitted():
    requirements = """
阶段一：账户交付
角色集合：后端工程师
任务一：账户登录
普通需求：订单状态查询
"""
    contract = parse_project_contract(requirements)
    plan = deterministic_plan_fallback(contract)
    task = plan["phases"][0]["task_contract"][0]
    omitted = task["source_requirement_ids"].pop()
    result = validate_plan_for_confirmation(
        plan, contract, expected_source_requirements=requirements
    )
    assert omitted
    assert "requirement_unit_uncovered" in {issue.code for issue in result.issues}


def test_plain_requirement_ids_cannot_be_claimed_by_unrelated_task():
    requirements = "1. 用户登录 2. 订单查询 3. 报表导出"
    contract = parse_project_contract(requirements)
    source_ids = [unit.unit_id for unit in contract.requirement_units]
    plan = {
        "tech_stack": ["Python"],
        "phases": [{
            "phase_id": "phase-1",
            "name": "Delivery",
            "roles_needed": ["backend"],
            "acceptance_criteria": ["Delivery complete"],
            "task_contract": [{
                "task_id": "login",
                "name": "用户登录",
                "description": "实现用户登录会话",
                "roles": ["backend"],
                "source_requirement_ids": source_ids,
                "acceptance_criteria": ["用户可以登录"],
            }],
        }],
        "project_contract": contract.as_mapping(),
    }

    result = validate_plan_for_confirmation(plan, contract)

    assert len(contract.requirement_units) == 3
    assert "task_source_mapping_unsubstantiated" in {
        issue.code for issue in result.issues
    }


def test_requirement_units_split_numbered_semicolon_and_markdown_items_stably():
    requirements = """
1. 用户登录 2. 订单查询；3. 报表导出
| 编号 | 需求 |
| --- | --- |
| 4 | 权限审计 |
| 5 | 数据备份 |
"""
    first = extract_requirement_units(requirements)
    second = extract_requirement_units(requirements)

    assert first == second
    assert [unit.exact_text.rstrip("；;") for unit in first] == [
        "用户登录",
        "订单查询",
        "报表导出",
        "权限审计",
        "数据备份",
    ]
    assert [unit.order for unit in first] == [1, 2, 3, 4, 5]
    assert all(unit.kind == "outline" for unit in first)


def test_fallback_does_not_inject_unrelated_csv_requirement_into_login_task():
    requirements = "1. 用户登录 2. 导出 CSV"
    seed = {
        "contract_version": 3,
        "phases": [{
            "phase_id": "phase-1",
            "name": "Account delivery",
            "roles_needed": ["backend"],
            "acceptance_criteria": ["Login passes"],
            "task_contract": [{
                "task_id": "login",
                "name": "用户登录",
                "description": "实现登录会话",
                "roles": ["backend"],
                "acceptance_criteria": ["用户可以登录"],
            }],
        }],
    }
    contract = parse_project_contract(requirements, seed)
    fallback = deterministic_plan_fallback(contract)
    task = fallback["phases"][0]["task_contract"][0]
    export_unit = next(
        unit for unit in contract.requirement_units if "CSV" in unit.exact_text
    )

    assert export_unit.unit_id not in task.get("source_requirement_ids", [])
    assert "CSV" not in str(task.get("description") or "")
    result = validate_plan_for_confirmation(fallback, contract)
    assert "requirement_unit_uncovered" in {
        issue.code for issue in result.issues
    }


def test_fallback_maps_requirements_to_real_semantically_matching_tasks():
    requirements = "1. 用户登录 2. 导出 CSV"
    seed = {
        "contract_version": 3,
        "phases": [{
            "phase_id": "phase-1",
            "name": "Delivery",
            "roles_needed": ["backend"],
            "acceptance_criteria": ["Delivery passes"],
            "task_contract": [
                {
                    "task_id": "login",
                    "name": "用户登录",
                    "roles": ["backend"],
                    "acceptance_criteria": ["用户登录通过"],
                },
                {
                    "task_id": "csv-export",
                    "name": "导出 CSV",
                    "roles": ["backend"],
                    "acceptance_criteria": ["CSV 导出通过"],
                },
            ],
        }],
    }
    contract = parse_project_contract(requirements, seed)
    fallback = deterministic_plan_fallback(contract)
    tasks = {
        task["task_id"]: task
        for task in fallback["phases"][0]["task_contract"]
    }
    units = {
        unit.exact_text: unit.unit_id
        for unit in contract.requirement_units
    }

    assert tasks["login"]["source_requirement_ids"] == [units["用户登录"]]
    assert tasks["csv-export"]["source_requirement_ids"] == [units["导出 CSV"]]
    assert validate_plan_for_confirmation(fallback, contract).valid is True


def test_fallback_creates_explicit_task_when_unlocked_phase_can_carry_requirement():
    requirements = "导出 CSV"
    contract = parse_project_contract(
        requirements,
        {"contract_version": 3, "phases": [{
            "phase_id": "phase-1",
            "name": "Delivery",
            "roles_needed": ["backend"],
        }]},
    )

    fallback = deterministic_plan_fallback(contract)
    task = fallback["phases"][0]["task_contract"][0]

    assert task["task_id"].startswith("phase-1-requirement-")
    assert task["source_requirement_ids"] == [
        contract.requirement_units[0].unit_id
    ]
    assert task["deliverable"] == "导出 CSV"
    assert task["acceptance_criteria"]
    assert validate_plan_for_confirmation(fallback, contract).valid is True


def test_exact_per_phase_task_count_does_not_turn_planning_rules_into_tasks():
    requirements = (
        "构建一个提供健康检查接口的小型 FastAPI 服务。"
        "严格规划3阶段，每阶段1任务；"
        "每阶段必须包含任务、实现细节、实现方式、技术栈、职责、人员分配和验收标准；"
        "不得扩展。"
    )
    contract = parse_project_contract(requirements)
    fallback = deterministic_plan_fallback(contract)

    assert contract.phase_count == 3
    assert [len(phase.tasks) for phase in contract.phases] == [1, 1, 1]
    assert [len(phase["task_contract"]) for phase in fallback["phases"]] == [1, 1, 1]
    task_text = " ".join(
        str(task.get(field) or "")
        for phase in fallback["phases"]
        for task in phase["task_contract"]
        for field in ("name", "description", "deliverable", "implementation")
    )
    assert "每阶段必须包含" not in task_text
    assert "不得扩展" not in task_text
    assert validate_plan_for_confirmation(
        fallback,
        contract,
        expected_source_requirements=requirements,
    ).valid is True


def test_count_only_placeholders_accept_then_lock_pm_dependency_dag():
    requirements = (
        "构建一个提供健康检查接口的小型 FastAPI 服务。"
        "严格规划3阶段，每阶段1任务；阶段职责清晰、依赖合理。"
    )
    contract = parse_project_contract(requirements)
    candidate = deterministic_plan_fallback(contract)
    phases = candidate["phases"]
    phases[0]["name"] = "基础设施"
    phases[1]["name"] = "核心接口"
    phases[2]["name"] = "验收交付"

    assert [
        phase["dependencies"] for phase in phases
    ] == [[], ["phase-1"], ["phase-2"]]
    assert [
        phase["task_contract"][0]["dependencies"] for phase in phases
    ] == [[], ["phase-1-task-1"], ["phase-2-task-1"]]
    assert validate_plan_for_confirmation(
        candidate,
        contract,
        expected_source_requirements=requirements,
    ).valid is True

    unknown = copy.deepcopy(candidate)
    unknown["phases"][1]["task_contract"][0]["dependencies"] = ["missing-task"]
    assert "dependency_unknown" in {
        issue.code
        for issue in validate_plan_for_confirmation(
            unknown,
            contract,
            expected_source_requirements=requirements,
        ).issues
    }

    cyclic = copy.deepcopy(candidate)
    cyclic["phases"][0]["task_contract"][0]["dependencies"] = [
        "phase-3-task-1",
    ]
    assert {
        "dependency_future_phase",
        "dependency_cycle",
    }.issubset({
        issue.code
        for issue in validate_plan_for_confirmation(
            cyclic,
            contract,
            expected_source_requirements=requirements,
        ).issues
    })

    locked = finalize_project_contract(contract, candidate)
    reloaded = freeze_confirmed_project_contract(locked.as_mapping())
    assert all(
        not task.get("planning_placeholder", False)
        for phase in reloaded["phases"]
        for task in phase["tasks"]
    )
    assert all(
        not phase.planning_placeholder
        for phase in locked.phases
    )
    assert [phase.name for phase in locked.phases] == [
        "基础设施", "核心接口", "验收交付",
    ]
    assert [
        list(phase.tasks[0].dependencies) for phase in locked.phases
    ] == [[], ["phase-1-task-1"], ["phase-2-task-1"]]
    assert validate_plan_for_confirmation(
        {**candidate, "project_contract": locked.as_mapping()},
        locked,
        expected_source_requirements=requirements,
    ).valid is True

    candidate["phases"][2]["task_contract"][0]["dependencies"] = []
    assert "dependency_drift" in {
        issue.code
        for issue in validate_plan_for_confirmation(
            {**candidate, "project_contract": locked.as_mapping()},
            locked,
            expected_source_requirements=requirements,
        ).issues
    }


def test_planning_constraints_are_not_task_coverage_requirements():
    requirements = (
        "创建一个最小团队待办 Web 应用，支持新增、完成和筛选待办。"
        "严格规划3阶段，每阶段1任务；"
        "每阶段必须包含任务、实现细节、实现方式、技术栈、职责、"
        "人员数量与分配、验收标准。"
        "不得扩展需求。"
    )
    contract = parse_project_contract(requirements)
    units = {unit.order: unit for unit in contract.requirement_units}
    candidate = deterministic_plan_fallback(contract)

    assert len(candidate["phases"]) == 3
    assert [len(phase["task_contract"]) for phase in candidate["phases"]] == [
        1, 1, 1,
    ]
    assert all(
        task["source_requirement_ids"] == [units[1].unit_id]
        for phase in candidate["phases"]
        for task in phase["task_contract"]
    )
    assert all(
        units[index].unit_id not in task["source_requirement_ids"]
        for index in (2, 3, 4)
        for phase in candidate["phases"]
        for task in phase["task_contract"]
    )
    assert validate_plan_for_confirmation(
        candidate,
        contract,
        expected_source_requirements=requirements,
    ).valid is True

    constraint_only = copy.deepcopy(candidate)
    for phase in constraint_only["phases"]:
        phase["task_contract"][0]["source_requirement_ids"] = [
            units[2].unit_id,
        ]
    constraint_codes = {
        issue.code
        for issue in validate_plan_for_confirmation(
            constraint_only,
            contract,
            expected_source_requirements=requirements,
        ).issues
    }
    assert "task_functional_source_missing" in constraint_codes
    assert "requirement_unit_uncovered" in constraint_codes

    unknown = copy.deepcopy(candidate)
    unknown["phases"][0]["task_contract"][0][
        "source_requirement_ids"
    ].append("req-unknown")
    assert "requirement_unit_unknown" in {
        issue.code
        for issue in validate_plan_for_confirmation(
            unknown,
            contract,
            expected_source_requirements=requirements,
        ).issues
    }


def test_qa_task_can_bind_only_its_mixed_test_and_technology_requirement():
    requirements = (
        "Implement GET /health returning 200 JSON.\n"
        "Use node:test to verify GET /health and probe the running API. "
        "Use Node.js and node:test."
    )
    contract, candidate = _valid_freeform_plan(requirements)
    implementation_unit, qa_unit = contract.requirement_units
    phase = candidate["phases"][0]
    implementation_task = phase["task_contract"][0]
    implementation_task.update({
        "name": "Implement health endpoint",
        "description": implementation_unit.exact_text,
        "implementation": "Implement GET /health returning 200 JSON.",
        "source_requirement_ids": [implementation_unit.unit_id],
        "acceptance_criteria": ["GET /health returns 200 JSON"],
    })
    qa_task = copy.deepcopy(implementation_task)
    qa_task.update({
        "task_id": "task-2",
        "name": "Verify health endpoint",
        "description": qa_unit.exact_text,
        "implementation": (
            "Run node:test against the Phase 1 server and probe GET /health."
        ),
        "roles": ["qa"],
        "responsibilities": ["qa: verify the running API"],
        "personnel_allocation": ["qa: 1"],
        "source_requirement_ids": [qa_unit.unit_id],
        "acceptance_criteria": ["node:test passes against the running API"],
        "dependencies": ["task-1"],
    })
    phase["task_contract"].append(qa_task)
    phase["roles_needed"] = ["backend", "qa"]
    phase["responsibilities"].append("qa: verify the running API")
    phase["personnel_allocation"].append("qa: 1")
    phase["agent_count"] = 2

    result = validate_plan_for_confirmation(
        candidate,
        contract,
        expected_source_requirements=requirements,
    )

    assert result.valid is True, [issue.to_dict() for issue in result.issues]


def test_qa_task_substantiates_mixed_test_schema_technology_and_runtime_unit():
    requirement = parse_project_contract(
        "use the Node.js built-in test runner to verify health, empty-list, "
        "create, and invalid-input behavior. Every phase and task must contain "
        "implementation details, implementation method, technology stack, "
        "responsibilities, personnel count and allocation, acceptance criteria, "
        "dependencies, and explicit required_files. Use Node.js, Express, and "
        "node:test. The locked runtime profile must run npm install, npm test, "
        "npm start, then probe GET /health and the TODO API."
    ).requirement_units[0]
    task = {
        "name": "Run Node TODO API integration tests",
        "description": (
            "QA verifies health, empty-list, create, and invalid-input behavior "
            "with the Node.js built-in test runner."
        ),
        "implementation": (
            "Run node --test tests/todos.test.js, start the API, then probe "
            "GET /health and the TODO API."
        ),
        "required_files": ["tests/todos.test.js"],
        "acceptance_criteria": [
            "node:test passes and the running API probes return expected results"
        ],
    }

    assert _task_mapping_is_substantiated(requirement, task) is True
    assert _task_mapping_is_substantiated(requirement, {
        "name": "Export CSV reports",
        "implementation": "Generate downloadable CSV files from report data.",
        "acceptance_criteria": ["CSV export succeeds"],
    }) is False


def test_task_schema_and_expert_choice_instructions_are_not_executable_units():
    requirements = (
        "构建一个轻量团队待办 Web 应用，支持创建、完成和删除待办。"
        "规划恰好 3 个阶段，每阶段恰好 1 个任务；"
        "每个任务必须明确 required_files、实现细节、实现方式、技术栈、"
        "职责、人员分配和验收标准。"
        "共享配置文件归最早需要它的阶段所有，后续阶段不得重复声明，"
        "但必须通过依赖使用它；"
        "测试任务必须明确测试源码和测试配置文件。"
        "除上述约束外，具体技术方案由 PM/专家选择。"
    )
    contract = parse_project_contract(requirements)
    candidate = deterministic_plan_fallback(contract)
    functional_unit = contract.requirement_units[0].unit_id

    assert {
        source_id
        for phase in candidate["phases"]
        for task in phase["task_contract"]
        for source_id in task["source_requirement_ids"]
    } == {functional_unit}
    assert validate_plan_for_confirmation(
        candidate,
        contract,
        expected_source_requirements=requirements,
    ).valid is True
