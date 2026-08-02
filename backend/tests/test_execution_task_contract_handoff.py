import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from agents.execution_agent import ExecutionAgent
from api.routes_phases import _locked_task_artifact_policy
from api import routes_execution
from core.execution_runs import LeaseConflict
from core.phase_execution_contract import build_phase_dispatch_plan


class CapturingHermes:
    def __init__(self, content: str):
        self.content = content
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        return {"content": self.content}


class SequencedHermes:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        return {"content": self.contents.pop(0)}


def test_file_lease_contention_waits_and_resumes_without_failing_run(monkeypatch):
    calls = []
    sleeps = []
    ctx = SimpleNamespace(agents={"agent-1": {"id": "agent-1"}})

    def claim(*_args, **_kwargs):
        calls.append("claim")
        if len(calls) == 1:
            raise LeaseConflict("file scope is owned by another task")
        return {"success": True, "lock_id": "lock-2"}

    async def no_wait(delay):
        sleeps.append(delay)

    monkeypatch.setattr(routes_execution, "_ensure_agent_run_file_lease", claim)
    monkeypatch.setattr(routes_execution, "_assert_current_phase_attempt", lambda _payload: None)
    monkeypatch.setattr(routes_execution.asyncio, "sleep", no_wait)

    lease = asyncio.run(routes_execution._wait_for_agent_run_file_lease(
        ctx, "agent-1", "run-1", {"planned_files": ["src/App.vue"]},
        {"project_id": "p1"}, threading.Event(),
    ))

    assert lease["lock_id"] == "lock-2"
    assert calls == ["claim", "claim"]
    assert sleeps == [0.25]
    assert ctx.agents["agent-1"]["recovery_status"] == "resumed_after_file_lock"
    assert "file_lock_wait_reason" not in ctx.agents["agent-1"]


def test_file_lease_wait_honors_run_cancellation(monkeypatch):
    cancel = threading.Event()
    ctx = SimpleNamespace(agents={"agent-1": {"id": "agent-1"}})

    def always_conflicts(*_args, **_kwargs):
        raise LeaseConflict("busy")

    async def cancel_while_waiting(_delay):
        cancel.set()

    monkeypatch.setattr(routes_execution, "_ensure_agent_run_file_lease", always_conflicts)
    monkeypatch.setattr(routes_execution, "_assert_current_phase_attempt", lambda _payload: None)
    monkeypatch.setattr(routes_execution.asyncio, "sleep", cancel_while_waiting)

    with pytest.raises(RuntimeError, match="cancelled"):
        asyncio.run(routes_execution._wait_for_agent_run_file_lease(
            ctx, "agent-1", "run-1", {}, {"project_id": "p1"}, cancel,
        ))


def test_new_task_policy_treats_pm_files_as_planning_hints():
    policy = _locked_task_artifact_policy(
        {"kind": "runnable"},
        task_id="phase-2-task-1",
        task_dependencies=[],
        required_files=[
            "frontend/src/main.js",
            "frontend/src/router/index.js",
        ],
        rebuild_file_specs=[],
    )

    assert policy["planned_files"] == [
        "frontend/src/main.js",
        "frontend/src/router/index.js",
    ]
    assert policy["required_files"] == []
    assert policy["allowed_path_prefixes"] == ["frontend/"]
    assert policy["dynamic_delivery_scope"] is True


def test_rebuild_policy_keeps_exact_file_contract():
    policy = _locked_task_artifact_policy(
        {"kind": "runnable"},
        task_id="phase-2-task-1",
        task_dependencies=[],
        required_files=["frontend/src/main.js"],
        rebuild_file_specs=[{
            "path": "frontend/src/main.js",
            "mode": "patch",
        }],
    )

    assert policy["required_files"] == ["frontend/src/main.js"]
    assert policy["allowed_path_prefixes"] == ["frontend/src/main.js"]
    assert policy["dynamic_delivery_scope"] is False


def test_new_task_can_add_missing_import_targets_inside_authorized_tree(tmp_path):
    hermes = SequencedHermes([
        json.dumps({"files": [
            {
                "path": "frontend/src/main.js",
                "content": "import App from './App.vue';\nexport default App;\n",
            },
            {
                "path": "frontend/src/router/index.js",
                "content": "import View from '../views/InventoryView.vue';\nexport default View;\n",
            },
        ]}),
        json.dumps({"files": [
            {
                "path": "frontend/src/App.vue",
                "content": "<template><main>Inventory</main></template>\n",
            },
            {
                "path": "frontend/src/views/InventoryView.vue",
                "content": "<template><section>Items</section></template>\n",
            },
        ]}),
    ])
    agent = ExecutionAgent(
        agent_id="agent-frontend",
        role="Frontend Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["frontend/"],
        required_output_files=[],
        artifact_policy={
            "kind": "runnable",
            "planned_files": [
                "frontend/src/main.js",
                "frontend/src/router/index.js",
            ],
            "required_files": [],
            "allowed_path_prefixes": ["frontend/"],
            "dynamic_delivery_scope": True,
        },
        immutable_path_scope=True,
    )

    result = agent.execute_task(
        subproject_id="sp-phase-2-001",
        subproject_name="Build frontend skeleton",
        description="Build a coherent Vue application skeleton.",
        tech_stack=["Vue 3", "Vite"],
    )

    assert result["success"] is True
    assert (tmp_path / "frontend/src/App.vue").is_file()
    assert (tmp_path / "frontend/src/views/InventoryView.vue").is_file()


def _phase_plan():
    return {
        "schema_version": "phase-plan/v1",
        "phase_id": "phase-1",
        "summary": "Build the browser task editor",
        "effective_technical_requirements": [
            {"requirement": "React", "source": "total_plan"},
            {"requirement": "localStorage", "source": "phase_requirements"},
        ],
        "tasks": [{
            "task_id": "phase-1-task-1",
            "name": "Task editor",
            "objective": "Create and persist tasks",
            "functional_details": [
                "Users can add tasks",
                "Tasks survive a page reload",
            ],
            "implementation": "Use controlled React state synchronized to localStorage",
            "dependencies": [],
            "acceptance_criteria": [
                "Adding a task renders it immediately",
                "Reloading restores saved tasks",
            ],
        }],
        "assignments": [{
            "expert_id": "expert-frontend",
            "task_ids": ["phase-1-task-1"],
            "responsibility": "Implement the task editor exactly as planned",
        }],
        "expert_pool_revision": 3,
    }


def test_dispatch_preserves_confirmed_phase_plan_execution_details():
    phase_plan = _phase_plan()
    phase = {
        "phase_id": "phase-1",
        "roles_needed": ["Frontend Developer"],
        "phase_plan": phase_plan,
        "task_contract": [{
            "task_id": "phase-1-task-1",
            "name": "Task editor",
            "description": "Create and persist tasks",
            "implementation": "Use controlled React state synchronized to localStorage",
            "roles": ["Frontend Developer"],
            "required_files": [],
            "dependencies": [],
            "acceptance_criteria": phase_plan["tasks"][0]["acceptance_criteria"],
        }],
        "expert_requirements": [{
            "task_id": "phase-1-task-1",
            "task_name": "Task editor",
            "task_description": "Create and persist tasks",
            "required_role": "Frontend Developer",
            "required_files": [],
            "dependencies": [],
            "acceptance_criteria": phase_plan["tasks"][0]["acceptance_criteria"],
        }],
    }

    dispatch = build_phase_dispatch_plan(
        [phase],
        "phase-1",
        [{
            "agent_id": "agent-frontend",
            "required_role": "Frontend Developer",
            "task_ids": ["phase-1-task-1"],
        }],
    )

    task = dispatch["waves"][0][0]
    assert task["name"] == "Task editor"
    assert task["objective"] == "Create and persist tasks"
    assert task["functional_details"] == [
        "Users can add tasks",
        "Tasks survive a page reload",
    ]
    assert task["implementation"] == (
        "Use controlled React state synchronized to localStorage"
    )
    assert task["effective_technical_requirements"] == (
        phase_plan["effective_technical_requirements"]
    )
    assert "deliverable_files" not in task
    assert task["assignment"] == phase_plan["assignments"][0]
    assert task["required_files"] == []


def test_pathless_tasks_are_serialized_across_different_agents():
    phase_plan = _phase_plan()
    second = {
        **dict(phase_plan["tasks"][0]),
        "task_id": "phase-1-task-2",
        "name": "Task list",
        "dependencies": [],
    }
    phase_plan["tasks"].append(second)
    phase_plan["assignments"].append({
        "expert_id": "expert-second",
        "task_ids": ["phase-1-task-2"],
        "responsibility": "Implement the task list",
    })
    phase = {
        "phase_id": "phase-1",
        "roles_needed": ["Frontend Developer"],
        "phase_plan": phase_plan,
        "task_contract": [
            {
                "task_id": task["task_id"],
                "name": task["name"],
                "roles": ["Frontend Developer"],
                "required_files": [],
                "dependencies": [],
                "acceptance_criteria": task["acceptance_criteria"],
            }
            for task in phase_plan["tasks"]
        ],
        "expert_requirements": [
            {
                "task_id": task["task_id"],
                "task_name": task["name"],
                "required_role": "Frontend Developer",
                "required_files": [],
                "dependencies": [],
                "acceptance_criteria": task["acceptance_criteria"],
            }
            for task in phase_plan["tasks"]
        ],
    }

    dispatch = build_phase_dispatch_plan(
        [phase],
        "phase-1",
        [
            {
                "agent_id": "agent-first",
                "required_role": "Frontend Developer",
                "task_ids": ["phase-1-task-1"],
            },
            {
                "agent_id": "agent-second",
                "required_role": "Frontend Developer",
                "task_ids": ["phase-1-task-2"],
            },
        ],
    )

    assert [
        wave[0]["task_id"] for wave in dispatch["waves"]
    ] == ["phase-1-task-1", "phase-1-task-2"]
    assert all(len(wave) == 1 for wave in dispatch["waves"])


def test_execution_prompt_marks_every_required_file_as_mandatory(tmp_path):
    hermes = CapturingHermes(json.dumps({
        "files": [{
            "path": "src/app.py",
            "content": "def run():\n    return 'ok'\n",
        }],
    }))
    agent = ExecutionAgent(
        agent_id="agent-test",
        role="Backend Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["src/app.py"],
        required_output_files=["src/app.py"],
        artifact_policy={
            "kind": "runnable",
            "required_files": ["src/app.py"],
            "allowed_path_prefixes": ["src/app.py"],
        },
        immutable_path_scope=True,
    )

    result = agent.execute_task(
        subproject_id="sp-1",
        subproject_name="Backend task",
        description=(
            "CURRENT LOCKED TASK:\n"
            '{"objective":"Create the required module","implementation":'
            '"Return the complete Python module"}'
        ),
    )

    assert result["success"] is True
    system_prompt = hermes.calls[0][0].content
    assert "TASK CONTRACT PRECEDENCE" in system_prompt
    assert "MANDATORY DELIVERY FILES" in system_prompt
    assert "src/app.py" in system_prompt


def test_execution_prompt_accepts_structured_technical_requirements(tmp_path):
    hermes = CapturingHermes(json.dumps({
        "files": [{
            "path": "src/app.py",
            "content": "def run():\n    return 'ok'\n",
        }],
    }))
    agent = ExecutionAgent(
        agent_id="agent-test",
        role="Backend Developer",
        workspace=tmp_path,
        hermes_client=hermes,
        allowed_path_prefixes=["src/app.py"],
        required_output_files=["src/app.py"],
        immutable_path_scope=True,
    )

    result = agent.execute_task(
        subproject_id="sp-1",
        subproject_name="Backend task",
        description="Create the required Python module",
        tech_stack=[{
            "requirement": "Python",
            "source": "phase_requirements",
        }],
    )

    assert result["success"] is True
    assert "技术栈：Python" in hermes.calls[0][0].content
