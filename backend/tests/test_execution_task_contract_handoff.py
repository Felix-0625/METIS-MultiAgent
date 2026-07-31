import json

from agents.execution_agent import ExecutionAgent
from core.phase_execution_contract import build_phase_dispatch_plan


class CapturingHermes:
    def __init__(self, content: str):
        self.content = content
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        return {"content": self.content}


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
