"""
任务调度集成层 — 将 TaskDispatcher/SupervisorDispatcher 接入执行流
参考设计文档第 14.3 节
"""

import logging
import uuid
from typing import Dict, Optional, Any

from core.task_dispatcher import (
    TaskDispatcher, SupervisorDispatcher, TaskQueue,
    Task, TaskPriority,
)
from agents.base.hermes_agent import AgentType

logger = logging.getLogger(__name__)

# 项目级别的调度器缓存
_dispatchers: Dict[str, SupervisorDispatcher] = {}


def get_or_create_dispatcher(project_id: str) -> SupervisorDispatcher:
    """获取或创建项目的 SupervisorDispatcher 单例"""
    if project_id not in _dispatchers:
        _dispatchers[project_id] = SupervisorDispatcher(
            supervisor_id=f"sup-{project_id}",
            project_id=project_id,
        )
    return _dispatchers[project_id]


def register_phase_agents(project_id: str, phase_id: str, agents: list[dict]) -> int:
    """
    将阶段创建的 Agent 注册到调度器并创建任务。
    返回创建的任务数。
    """
    dispatcher = get_or_create_dispatcher(project_id)
    task_count = 0

    for agent_info in agents:
        agent_id = agent_info.get("id", f"agent-{uuid.uuid4().hex[:6]}")
        role_label = agent_info.get("role", "")

        # 映射 Agent 类型
        expert_type = agent_info.get("expert_type", "pg")
        agent_type_map = {
            "frontend": AgentType.PG,
            "backend": AgentType.PG,
            "database": AgentType.PG,
            "architecture": AgentType.PG,
            "devops": AgentType.PG,
            "security": AgentType.SEC,
            "qa": AgentType.QA,
            "data": AgentType.PG,
        }
        agent_type = agent_type_map.get(expert_type, AgentType.PG)

        # 创建任务
        task = dispatcher.create_task(
            title=f"执行子项目：{agent_info.get('subproject_name', '未知')}",
            description=f"专家 {role_label} 负责 {agent_info.get('subproject_name', '阶段任务')}",
            agent_type=agent_type,
            priority=TaskPriority.NORMAL,
        )

        # 记录任务元数据
        task.metadata = {
            "agent_id": agent_id,
            "phase_id": phase_id,
            "project_id": project_id,
            "expert_type": expert_type,
            "lock_id": agent_info.get("lock_id"),
        }
        task_count += 1

    logger.info("已为项目 %s 阶段 %s 注册 %d 个任务", project_id, phase_id, task_count)
    return task_count


def get_dispatch_status(project_id: str) -> dict:
    """获取项目调度状态"""
    dispatcher = _dispatchers.get(project_id)
    if not dispatcher:
        return {"project_id": project_id, "initialized": False}
    return {
        **dispatcher.get_status(),
        "initialized": True,
    }


def get_ready_tasks(project_id: str) -> list:
    """获取就绪任务列表"""
    dispatcher = _dispatchers.get(project_id)
    if not dispatcher:
        return []
    ready = dispatcher.get_ready_tasks()
    return [
        {
            "task_id": t.id,
            "title": t.title,
            "status": t.status.value,
            "agent_type": t.agent_type.value,
            "metadata": getattr(t, "metadata", {}),
        }
        for t in ready
    ]