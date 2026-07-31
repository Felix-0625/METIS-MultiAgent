import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from api import routes_phases
from api.routes_phases import _parse_expert_requirements


def test_parse_expert_requirements_rejects_invalid_payloads():
    assert _parse_expert_requirements("") == []
    assert _parse_expert_requirements("not json") == []
    assert _parse_expert_requirements('[{"task_id":"t1"}]') == []


def test_parse_expert_requirements_normalizes_valid_tasks():
    result = _parse_expert_requirements(
        'prefix [{"task_name":" Build UI ","required_role":" frontend ",'
        '"acceptance_criteria":"invalid","priority":"unexpected"}] suffix'
    )

    assert result == [{
        "task_id": "task-1",
        "task_name": "Build UI",
        "task_description": "",
        "required_role": "frontend",
        "priority": "normal",
        "implementation_method": "",
        "tech_stack": [],
        "responsibilities": [],
        "personnel_count": 0,
        "personnel_allocation": [],
        "acceptance_criteria": ["invalid"],
        "dependencies": [],
    }]


def test_parse_expert_requirements_preserves_locked_dependencies():
    result = _parse_expert_requirements(
        json.dumps([{
            "task_id": "phase-2-task-1",
            "task_name": "Continue delivery",
            "task_description": "Build on phase one",
            "required_role": "backend",
            "dependencies": ["phase-1-task-1"],
        }]),
        strict=True,
    )

    assert result[0]["dependencies"] == ["phase-1-task-1"]


def test_phase_plan_parser_normalizes_aliases_and_server_locks_immutable_fields():
    parsed = _parse_expert_requirements(
        json.dumps([{
            "task_id": "phase-2-task-1",
            "name": "renamed by model",
            "description": "Implement the UI",
            "roles": ["frontend"],
            "technology_stack": "React",
            "duties": "Build and test",
            "staffing": "frontend: 1",
            "criteria": "UI test passes",
            "dependencies": ["wrong-task"],
            "files": ["src/App.jsx"],
        }]),
        strict=True,
    )
    locked = [{
        "task_id": "phase-2-task-1",
        "name": "Locked UI",
        "roles": ["frontend"],
        "dependencies": ["phase-1-task-1"],
        "required_files": ["frontend/src/App.jsx"],
    }]

    merged = routes_phases._merge_locked_phase_task_fields(parsed, locked)

    assert merged == [{
        "task_id": "phase-2-task-1",
        "task_name": "Locked UI",
        "task_description": "Implement the UI",
        "required_role": "frontend",
        "priority": "normal",
        "implementation_method": "",
        "tech_stack": ["React"],
        "responsibilities": ["Build and test"],
        "personnel_count": 0,
        "personnel_allocation": ["frontend: 1"],
        "acceptance_criteria": ["UI test passes"],
        "dependencies": ["phase-1-task-1"],
        "required_files": ["frontend/src/App.jsx"],
    }]


class _PhaseManager:
    def __init__(self, phase, contract):
        self.phase = phase
        self.project_contract = contract

    def get_phase(self, phase_id):
        return self.phase if self.phase["phase_id"] == phase_id else None


def test_phase_plan_repairs_model_drift_before_persisting(monkeypatch):
    project_id = "contract-repair-project"
    phase = {
        "phase_id": "phase-1",
        "name": "基础设施、认证与权限",
        "description": "使用 Node.js、Express、React 完成基础能力",
        "roles_needed": ["fullstack engineer"],
        "task_contract": [
            {"task_id": "phase-1-task-1", "name": "项目骨架搭建"},
            {"task_id": "phase-1-task-2", "name": "用户认证模块"},
        ],
    }
    contract = {"locked": True, "required_tech": ["node.js", "express", "react"]}
    invalid = [{
        "task_id": f"t{index}",
        "task_name": f"Vue 扩展任务 {index}",
        "required_role": "backend engineer",
    } for index in range(1, 12)]
    valid = [
        {
            "task_id": item["task_id"],
            "task_name": item["name"],
            "task_description": "在确认边界内完成",
                "required_role": "fullstack engineer",
                "priority": "high",
                "implementation_method": "按锁定任务边界实现并执行自动化验证",
                "tech_stack": ["Node.js", "Express", "React"],
                "responsibilities": ["全栈工程师负责实现、联调与测试"],
                "personnel_count": 1,
                "personnel_allocation": ["全栈工程师：1 人"],
                "acceptance_criteria": ["自动化测试通过"],
        }
        for item in phase["task_contract"]
    ]
    outputs = [json.dumps(invalid, ensure_ascii=False), json.dumps(valid, ensure_ascii=False)]
    calls = []

    def chat(messages):
        calls.append(messages)
        return {"content": outputs.pop(0)}

    async def persist():
        return None

    monkeypatch.setattr(routes_phases, "_get_project", lambda _project_id: SimpleNamespace(name="测试项目"))
    monkeypatch.setitem(routes_phases._phase_managers, project_id, _PhaseManager(phase, contract))
    monkeypatch.setattr(routes_phases.hermes_client, "chat", chat)
    monkeypatch.setattr(routes_phases, "_persist_all_async", persist)

    result = asyncio.run(routes_phases.plan_experts_for_phase(project_id, "phase-1", {}))

    assert len(calls) == 2
    assert result["auto_corrected"] is True
    assert result["generation_mode"] == "model_repaired"
    assert [item["task_id"] for item in phase["expert_requirements"]] == [
        "phase-1-task-1", "phase-1-task-2"
    ]
    assert phase["plan_contract_validated"] is True


def test_phase_plan_falls_back_to_locked_contract_without_user_intervention(monkeypatch):
    project_id = "contract-fallback-project"
    phase = {
        "phase_id": "phase-1",
        "name": "基础设施、认证与权限",
        "description": "固定阶段",
        "roles_needed": ["fullstack engineer"],
        "task_contract": [
            {"task_id": f"phase-1-task-{index}", "name": f"锁定任务 {index}"}
            for index in range(1, 6)
        ],
    }
    contract = {"locked": True, "required_tech": ["node.js", "express", "react"]}
    invalid = [{
        "task_id": f"t{index}",
        "task_name": f"Vue 越界任务 {index}",
        "required_role": "backend engineer",
    } for index in range(1, 12)]

    async def persist():
        return None

    monkeypatch.setattr(routes_phases, "_get_project", lambda _project_id: SimpleNamespace(name="测试项目"))
    monkeypatch.setitem(routes_phases._phase_managers, project_id, _PhaseManager(phase, contract))
    monkeypatch.setattr(
        routes_phases.hermes_client,
        "chat",
        lambda _messages: {"content": json.dumps(invalid, ensure_ascii=False)},
    )
    monkeypatch.setattr(routes_phases, "_persist_all_async", persist)

    result = asyncio.run(routes_phases.plan_experts_for_phase(project_id, "phase-1", {}))

    assert result["generation_mode"] == "contract_fallback"
    assert result["count"] == 5
    assert [item["task_id"] for item in result["expert_requirements"]] == [
        f"phase-1-task-{index}" for index in range(1, 6)
    ]
    assert {item["required_role"] for item in result["expert_requirements"]} == {"fullstack engineer"}
    assert "plan_contract_violations" not in phase


def test_phase_plan_persist_failure_restores_previous_phase_state(monkeypatch):
    project_id = "phase-plan-persist-rollback"
    phase = {
        "phase_id": "phase-1",
        "name": "API",
        "description": "Build the API",
        "roles_needed": ["backend"],
        "task_contract": [{
            "task_id": "phase-1-task-1",
            "name": "Build endpoint",
            "description": "Implement and verify the endpoint",
            "implementation": "Use FastAPI routing",
            "technology_stack": ["Python", "FastAPI"],
            "roles": ["backend"],
            "responsibilities": ["Implement and test the endpoint"],
            "personnel_count": 1,
            "personnel_allocation": ["backend: 1"],
            "acceptance_criteria": ["API test passes"],
        }],
        "plan_status": "saved",
        "plan_generated": True,
        "expert_requirements": [{"task_id": "old-task"}],
    }
    before = copy.deepcopy(phase)
    contract = {
        "locked": True,
        "required_tech": ["python", "fastapi"],
        "phases": [{
            "phase_id": "phase-1",
            "name": "API",
            "roles": ["backend"],
            "tasks": copy.deepcopy(phase["task_contract"]),
        }],
    }

    async def fail_persist():
        raise OSError("database unavailable")

    monkeypatch.setattr(
        routes_phases,
        "_get_project",
        lambda _project_id: SimpleNamespace(name="Test project"),
    )
    monkeypatch.setitem(
        routes_phases._phase_managers,
        project_id,
        _PhaseManager(phase, contract),
    )
    monkeypatch.setattr(
        routes_phases.hermes_client,
        "chat",
        lambda _messages: {
            "content": json.dumps([{
                "task_id": "phase-1-task-1",
                "task_name": "Build endpoint",
                "task_description": "Implement and verify the endpoint",
                "implementation_method": "Use FastAPI routing",
                "tech_stack": ["Python", "FastAPI"],
                "required_role": "backend",
                "responsibilities": ["Implement and test the endpoint"],
                "personnel_count": 1,
                "personnel_allocation": ["backend: 1"],
                "acceptance_criteria": ["API test passes"],
            }]),
        },
    )
    monkeypatch.setattr(routes_phases, "_persist_all_async", fail_persist)

    with pytest.raises(OSError, match="database unavailable"):
        asyncio.run(routes_phases.plan_experts_for_phase(project_id, "phase-1", {}))

    assert phase == before
