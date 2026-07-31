"""
Supervisor 调度框架
任务派发机制和状态机管理
"""

import time
import uuid
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
from collections import deque

from agents.base.hermes_agent import (
    AgentType, AgentState, Task, TaskPriority, AgentBase
)


class TaskStatus(Enum):
    """任务状态"""
    PENDING = "pending"      # 等待中
    READY = "ready"         # 可执行
    RUNNING = "running"      # 执行中
    COMPLETED = "completed"  # 完成
    FAILED = "failed"        # 失败
    BLOCKED = "blocked"      # 阻塞


@dataclass
class DispatchResult:
    """派发结果"""
    success: bool
    task_id: str
    agent_id: Optional[str] = None
    summary: Optional[str] = None
    error: Optional[str] = None


class TaskQueue:
    """
    任务队列
    
    优先级队列实现，支持任务依赖管理
    """

    def __init__(self):
        self._tasks: Dict[str, Task] = {}
        self._pending: deque = deque()
        self._by_priority: Dict[int, deque] = {
            p.value: deque() for p in TaskPriority
        }

    def add(self, task: Task) -> None:
        """添加任务"""
        self._tasks[task.id] = task
        self._by_priority[task.priority.value].append(task.id)

    def get_ready_tasks(self) -> List[Task]:
        """获取就绪的任务（依赖已满足）"""
        ready = []
        
        for priority in range(TaskPriority.CRITICAL.value, 0, -1):
            queue = self._by_priority[priority]
            while queue:
                task_id = queue[0]
                task = self._tasks.get(task_id)
                
                if not task:
                    queue.popleft()
                    continue

                # 检查依赖是否都已完成（task.status 使用 AgentState 枚举）
                deps_ready = all(
                    self._tasks.get(dep_id) is not None and
                    self._tasks[dep_id].status == AgentState.COMPLETED
                    for dep_id in task.dependencies
                ) if task.dependencies else True

                if deps_ready:
                    queue.popleft()
                    ready.append(task)
                else:
                    # 当前任务依赖未满足，跳过（不 break），继续检查队列中其他任务
                    queue.rotate(-1)
                    # 防止无限循环：若已绕回起点则退出
                    if queue[0] == task_id:
                        break

        return ready

    def remove(self, task_id: str) -> Optional[Task]:
        """移除任务"""
        task = self._tasks.pop(task_id, None)
        return task

    def get(self, task_id: str) -> Optional[Task]:
        """获取任务"""
        return self._tasks.get(task_id)

    def list_all(self) -> List[Task]:
        """列出所有任务"""
        return list(self._tasks.values())

    def get_by_status(self, status: AgentState) -> List[Task]:
        """按状态筛选"""
        return [t for t in self._tasks.values() if t.status == status]


class TaskDispatcher:
    """
    任务调度器
    
    负责任务的创建、派发、状态跟踪
    """

    def __init__(self, supervisor_id: str):
        self.supervisor_id = supervisor_id
        self.queue = TaskQueue()
        
        # Agent 注册表
        self._agents: Dict[str, AgentBase] = {}
        self._agent_types: Dict[AgentType, List[str]] = {
            at: [] for at in AgentType
        }
        
        # 回调函数
        self._callbacks: Dict[str, Callable] = {}

    def register_agent(self, agent: AgentBase) -> None:
        """注册 Agent"""
        self._agents[agent.agent_id] = agent
        self._agent_types[agent.agent_type].append(agent.agent_id)

    def unregister_agent(self, agent_id: str) -> None:
        """注销 Agent"""
        agent = self._agents.pop(agent_id, None)
        if agent:
            self._agent_types[agent.agent_type].remove(agent_id)

    def get_available_agents(self, agent_type: AgentType) -> List[str]:
        """获取可用的 Agent"""
        return [
            aid for aid in self._agent_types.get(agent_type, [])
            if self._agents[aid].state in (AgentState.IDLE, AgentState.WAITING)
        ]

    def create_task(
        self,
        title: str,
        description: str,
        agent_type: AgentType,
        priority: TaskPriority = TaskPriority.NORMAL,
        dependencies: Optional[List[str]] = None
    ) -> Task:
        """创建任务"""
        task = Task(
            id=str(uuid.uuid4())[:8],
            title=title,
            description=description,
            agent_type=agent_type,
            priority=priority,
            dependencies=dependencies or []
        )
        
        self.queue.add(task)
        return task

    def dispatch(self, task_id: str) -> DispatchResult:
        """派发任务"""
        task = self.queue.get(task_id)
        if not task:
            return DispatchResult(
                success=False,
                task_id=task_id,
                error="Task not found"
            )

        # 获取可用 Agent
        available = self.get_available_agents(task.agent_type)
        if not available:
            return DispatchResult(
                success=False,
                task_id=task_id,
                error=f"No available {task.agent_type.value} agent"
            )

        # 选择 Agent（简单策略：选择第一个）
        agent_id = available[0]
        agent = self._agents[agent_id]

        # 派发任务
        try:
            task.assigned_to = agent_id
            task.started_at = time.time()
            
            result = agent.execute(task)
            
            task.completed_at = time.time()
            task.result = result.get("result")
            
            if result.get("success"):
                task.status = AgentState.COMPLETED
            else:
                task.status = AgentState.FAILED
                task.error = result.get("error")

            return DispatchResult(
                success=result.get("success", False),
                task_id=task_id,
                agent_id=agent_id,
                summary=agent.generate_summary()
            )

        except Exception as e:
            task.status = AgentState.FAILED
            task.error = str(e)
            
            return DispatchResult(
                success=False,
                task_id=task_id,
                agent_id=agent_id,
                error=str(e)
            )

    def dispatch_batch(self, task_ids: List[str]) -> List[DispatchResult]:
        """批量派发"""
        results = []
        for task_id in task_ids:
            results.append(self.dispatch(task_id))
        return results

    def register_callback(self, event: str, callback: Callable) -> None:
        """注册回调"""
        self._callbacks[event] = callback

    def on_task_complete(self, task_id: str, result: Any) -> None:
        """任务完成回调"""
        callback = self._callbacks.get("task_complete")
        if callback:
            callback(task_id, result)


class StateMachine:
    """
    项目状态机
    
    管理项目的生命周期状态转换
    """

    # 状态定义
    STATES = {
        "planning": ["executing", "cancelled"],
        "executing": ["paused", "completed", "failed"],
        "paused": ["executing", "cancelled"],
        "completed": [],
        "failed": ["planning"],  # 可重新规划
        "cancelled": []
    }

    def __init__(self, initial_state: str = "planning"):
        self.current_state = initial_state
        self.history: List[Dict] = []
        self._record_transition(initial_state, None, "初始化")

    def transition(self, new_state: str, reason: str = "") -> bool:
        """
        状态转换
        
        Args:
            new_state: 目标状态
            reason: 转换原因
            
        Returns:
            是否转换成功
        """
        allowed = self.STATES.get(self.current_state, [])
        
        if new_state not in allowed:
            return False

        old_state = self.current_state
        self.current_state = new_state
        self._record_transition(new_state, old_state, reason)
        
        return True

    def _record_transition(self, state: str, from_state: Optional[str], reason: str) -> None:
        """记录状态转换"""
        self.history.append({
            "state": state,
            "from": from_state,
            "reason": reason,
            "timestamp": time.time()
        })

    def can_transition(self, new_state: str) -> bool:
        """检查是否可以转换"""
        return new_state in self.STATES.get(self.current_state, [])

    def get_state(self) -> str:
        """获取当前状态"""
        return self.current_state


class ProjectStateMachine(StateMachine):
    """
    项目状态机
    
    继承自基础状态机，增加项目特定的状态转换逻辑
    """

    PROJECT_STATES = [
        "planning",    # 规划中
        "team_building",  # 组建团队
        "initializing",   # 初始化
        "executing",      # 执行中
        "qa_testing",     # QA 测试中
        "deploying",      # 部署中
        "completed",      # 完成
        "failed",         # 失败
        "cancelled"       # 取消
    ]

    def __init__(self):
        super().__init__("planning")
        self.subproject_states: Dict[str, StateMachine] = {}

    def add_subproject(self, subproject_id: str) -> None:
        """添加子项目"""
        self.subproject_states[subproject_id] = StateMachine("planning")

    def update_subproject(self, subproject_id: str, new_state: str) -> bool:
        """更新子项目状态"""
        sub_sm = self.subproject_states.get(subproject_id)
        if sub_sm:
            return sub_sm.transition(new_state)
        return False

    def check_phase_trigger(self) -> Optional[str]:
        """
        检查是否触发下一阶段
        
        当所有子项目达到某个状态时，触发项目状态转换
        """
        if not self.subproject_states:
            return None

        states = [sm.get_state() for sm in self.subproject_states.values()]

        if all(s == "completed" for s in states):
            return "all_completed"

        if any(s == "failed" for s in states):
            return "has_failed"

        return None

    def get_progress(self) -> Dict[str, Any]:
        """获取项目进度"""
        if not self.subproject_states:
            return {"total": 0, "completed": 0, "failed": 0}

        states = [sm.get_state() for sm in self.subproject_states.values()]
        return {
            "total": len(states),
            "completed": states.count("completed"),
            "failed": states.count("failed"),
            "in_progress": states.count("executing"),
            "planning": states.count("planning")
        }


@dataclass
class SupervisorContext:
    """
    Supervisor 上下文
    
    只保留项目全局摘要和关键决策
    """
    project_id: str
    project_summary: str = ""
    
    # 全局状态
    current_phase: str = "planning"
    total_subprojects: int = 0
    completed_subprojects: int = 0
    
    # 关键决策记录
    decisions: List[Dict] = field(default_factory=list)
    
    # 最新动态
    recent_summaries: List[str] = field(default_factory=list)

    def add_decision(self, decision: str, reason: str) -> None:
        """添加决策记录"""
        self.decisions.append({
            "decision": decision,
            "reason": reason,
            "timestamp": time.time()
        })

    def add_summary(self, summary: str) -> None:
        """添加执行摘要"""
        self.recent_summaries.append(summary)
        # 只保留最近 20 条
        if len(self.recent_summaries) > 20:
            self.recent_summaries = self.recent_summaries[-20:]

    def get_summary(self) -> str:
        """获取项目摘要"""
        lines = [
            f"项目: {self.project_id}",
            f"阶段: {self.current_phase}",
            f"进度: {self.completed_subprojects}/{self.total_subprojects}",
            "",
            "最近动态:"
        ]
        for s in self.recent_summaries[-5:]:
            lines.append(f"  {s}")
        
        if self.decisions:
            lines.append("")
            lines.append("关键决策:")
            for d in self.decisions[-3:]:
                lines.append(f"  - {d['decision']}: {d['reason']}")
        
        return "\n".join(lines)


class SupervisorDispatcher:
    """
    Supervisor 调度器
    
    整合任务调度、状态机、上下文管理
    """

    def __init__(self, supervisor_id: str, project_id: str):
        self.dispatcher = TaskDispatcher(supervisor_id)
        self.state_machine = ProjectStateMachine()
        self.context = SupervisorContext(project_id=project_id)
        
        # 子代理工厂
        self._subagent_factory: Optional[Callable] = None

    def register_callback(self, event: str, callback: Callable) -> None:
        """注册回调（代理到内部 dispatcher）"""
        self.dispatcher.register_callback(event, callback)

    def set_subagent_factory(self, factory: Callable) -> None:
        """设置子代理工厂"""
        self._subagent_factory = factory

    def create_task(
        self,
        title: str,
        description: str,
        agent_type: AgentType,
        priority: TaskPriority = TaskPriority.NORMAL,
        dependencies: Optional[List[str]] = None
    ) -> Task:
        """创建任务（代理到内部 dispatcher）"""
        return self.dispatcher.create_task(title, description, agent_type, priority, dependencies)

    def get_ready_tasks(self) -> List[Task]:
        """获取就绪任务"""
        return self.dispatcher.queue.get_ready_tasks()

    def register_agent(self, agent: AgentBase) -> None:
        """注册 Agent"""
        self.dispatcher.register_agent(agent)

    def unregister_agent(self, agent_id: str) -> None:
        """注销 Agent"""
        self.dispatcher.unregister_agent(agent_id)

    def get_available_agents(self, agent_type: AgentType) -> List[str]:
        """获取可用 Agent"""
        return self.dispatcher.get_available_agents(agent_type)

    def create_subagent(
        self,
        agent_type: AgentType,
        task: Task
    ) -> Optional[AgentBase]:
        """
        创建子代理
        
        子代理特征：
        - 空白上下文 (messages = [])
        - 无 task 工具（禁止递归）
        - 30 轮上限
        - 只返回纯文本摘要
        """
        if not self._subagent_factory:
            return None

        subagent = self._subagent_factory(agent_type, task)
        
        # 子代理禁用递归工具
        if hasattr(subagent, 'tools') and 'task' in subagent.tools:
            del subagent.tools['task']

        return subagent

    def dispatch_subagent(
        self,
        task: Task,
        context_msg: Optional[str] = None
    ) -> str:
        """
        派发子代理任务
        
        Args:
            task: 任务对象
            context_msg: 上下文消息（用于初始化子代理）
            
        Returns:
            执行摘要
        """
        # 创建子代理
        subagent = self.create_subagent(task.agent_type, task)
        if not subagent:
            return "Failed to create subagent"

        # 执行任务（子代理模式）
        result = subagent.execute(task)

        # 生成摘要
        summary = subagent.generate_summary()
        
        # 更新 Supervisor 上下文
        self.context.add_summary(summary)

        return summary

    def check_blockage(self) -> List[Dict]:
        """
        检查阻塞
        
        Returns:
            阻塞的任务列表
        """
        blocked = []
        tasks = self.dispatcher.queue.list_all()

        for task in tasks:
            if task.status == AgentState.BLOCKED:
                blocked.append({
                    "task_id": task.id,
                    "title": task.title,
                    "blocked_duration": time.time() - (task.started_at or task.created_at)
                })

        return blocked

    def get_status(self) -> Dict[str, Any]:
        """获取调度器状态"""
        return {
            "project_state": self.state_machine.get_state(),
            "progress": self.state_machine.get_progress(),
            "context_summary": self.context.get_summary(),
            "blocked_tasks": self.check_blockage()
        }