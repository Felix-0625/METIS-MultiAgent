"""
Employee Agent 实现
员工 Agent，负责执行具体任务，带 Self-Check 自检
"""

import time
from typing import Dict, List, Optional, Any
from .base.hermes_agent import AgentBase, AgentType, Task, AgentState
from .base.tools import SelfCheckTool


class EmployeeAgent(AgentBase):
    """
    Employee Agent
    
    职责：
    - 执行具体任务
    - Self-Check 自检（阻断式）
    - 汇报给组长
    
    约束：
    - 禁止直接通信（与其他员工）
    - 禁止越级上报
    - 禁止创建子代理
    - 阻塞超 30 分钟必须向小组长上报
    """

    # 基本能力
    ESSENTIAL_CAPABILITIES = [
        "任务执行",
        "Self-Check自检",
        "结果上报"
    ]

    def __init__(self, *args, role: str = "developer", group_id: str = None, **kwargs):
        self.role = role
        self.group_id = group_id
        self.skills: List[str] = []
        self.self_check = SelfCheckTool()
        self.execution_count = 0
        self._capabilities = list(self.ESSENTIAL_CAPABILITIES)
        super().__init__(*args, agent_type=AgentType.EMPLOYEE, **kwargs)

    def execute_task(self, task: Task) -> Dict[str, Any]:
        """
        执行任务（带自检）
        
        流程：
        1. 执行任务
        2. Self-Check 自检
        3. 通过 → 汇报组长
        4. 不通过 → 打回自修
        """
        self.update_state(AgentState.WORKING)
        self.execution_count += 1

        try:
            # 执行任务
            result = self._do_execute(task)

            # Self-Check 自检
            check_result = self.self_check.check({
                "task_id": task.id,
                "result": result,
                "role": self.role
            })

            if not check_result["passed"]:
                # 阻断：打回自修
                self.update_state(AgentState.BLOCKED)
                return {
                    "success": False,
                    "blocked": True,
                    "blockers": check_result["blockers"],
                    "message": check_result["message"]
                }

            # 通过自检，返回结果
            self.update_state(AgentState.COMPLETED)
            return {
                "success": True,
                "result": result,
                "check_passed": True,
                "ready_for_lead_review": True
            }

        except Exception as e:
            self.update_state(AgentState.FAILED)
            return {
                "success": False,
                "error": str(e)
            }

    def _do_execute(self, task: Task) -> Any:
        """员工 Agent 执行逻辑"""
        # 实际应用中，这里会调用 Hermes LLM 执行任务
        # 目前返回模拟结果

        if task.title == "implement":
            return self._implement(task)
        elif task.title == "test":
            return self._test(task)
        elif task.title == "document":
            return self._document(task)
        else:
            return {"status": "completed", "task": task.title}

    def _implement(self, task: Task) -> Dict:
        """实现功能"""
        return {
            "files_created": ["src/module.ts"],
            "lines_of_code": 150,
            "test_files": ["test/module.test.ts"]
        }

    def _test(self, task: Task) -> Dict:
        """执行测试"""
        return {
            "tests_run": 10,
            "tests_passed": 9,
            "tests_failed": 1
        }

    def _document(self, task: Task) -> Dict:
        """生成文档"""
        return {
            "documents": ["docs/guide.md"],
            "api_docs_updated": True
        }

    def attach_skill(self, skill_id: str) -> None:
        """挂载技能"""
        if skill_id not in self.skills:
            self.skills.append(skill_id)

    def detach_skill(self, skill_id: str) -> None:
        """卸载技能"""
        if skill_id in self.skills:
            self.skills.remove(skill_id)

    def report_to_lead(self, task_id: str, result: Dict) -> Dict[str, Any]:
        """
        向组长汇报
        
        实际应用中通过消息队列或 Supervisor 中转
        """
        return {
            "reported": True,
            "task_id": task_id,
            "employee_id": self.agent_id,
            "timestamp": time.time(),
            "summary": f"Employee {self.agent_id} completed task {task_id}"
        }

    def check_blockage(self) -> bool:
        """检查是否阻塞超时（30分钟），超时则向小组长上报"""
        if not self.current_task or not self.current_task.started_at:
            return False

        elapsed = time.time() - self.current_task.started_at
        return elapsed > 1800  # 30 分钟

    def report_blockage(self) -> Dict[str, Any]:
        """上报阻塞"""
        if not self.check_blockage():
            return {"blocked": False}

        return {
            "blocked": True,
            "employee_id": self.agent_id,
            "task_id": self.current_task.id if self.current_task else None,
            "duration": time.time() - self.current_task.started_at,
            "message": f"Task {self.current_task.id if self.current_task else 'unknown'} blocked for over 30 minutes, reporting to team lead"
        }

    def get_status(self) -> Dict[str, Any]:
        """获取员工状态"""
        return {
            "agent_id": self.agent_id,
            "type": "employee",
            "role": self.role,
            "group_id": self.group_id,
            "state": self.state.value,
            "skills": self.skills,
            "execution_count": self.execution_count,
            "is_blocked": self.check_blockage() if self.current_task else False
        }


# 员工工厂函数
def create_employee(
    role: str,
    group_id: str,
    hermes_client=None,
    memory=None
) -> EmployeeAgent:
    """创建员工 Agent"""
    return EmployeeAgent(
        role=role,
        group_id=group_id,
        hermes_client=hermes_client,
        memory_store=memory
    )