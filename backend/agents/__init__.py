"""
AI Multi-Agent System - Agents Package

导出所有 Agent 类
"""

from .base.hermes_agent import (
    AgentBase,
    AgentType,
    AgentState,
    Task,
    TaskPriority,
    Certificate,
    Project,
    SubAgent,
    LeadAgent
)

from .pm_agent import PMAgent
from .hr_agent import HRAgent
from .pg_agent import PGAgent
from .supervisor_agent import SupervisorAgent
from .employee_agent import EmployeeAgent
from .ccb_agent import CCBAgent

from .quality_agents import (
    QualityAgent,
    QAAgent,
    PerfAgent,
    SecAgent,
    UXOAgent,
    get_all_quality_agents
)

__all__ = [
    # 基类
    "AgentBase",
    "AgentType",
    "AgentState",
    "Task",
    "TaskPriority",
    "Certificate",
    "Project",
    "SubAgent",
    "LeadAgent",
    
    # 核心 Agent
    "PMAgent",
    "HRAgent",
    "PGAgent",
    "SupervisorAgent",
    
    # 执行 Agent
    "EmployeeAgent",
    
    # 质检 Agent
    "QualityAgent",
    "QAAgent",
    "PerfAgent",
    "SecAgent",
    "UXOAgent",
    "get_all_quality_agents",
    
    # 变更控制
    "CCBAgent"
]