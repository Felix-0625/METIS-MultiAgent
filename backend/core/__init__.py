"""
AI Multi-Agent System - Core Package

核心模块：Hermes 客户端、任务调度、配置加载
"""

from .hermes_client import (
    HermesClient,
    Message,
    MessageRole,
    Tool,
    ToolCall,
    SubAgentContext
)

from .config_loader import ConfigLoader

# 延迟导入 task_dispatcher 避免循环依赖
def __getattr__(name):
    if name == "TaskQueue":
        from .task_dispatcher import TaskQueue
        return TaskQueue
    elif name == "TaskDispatcher":
        from .task_dispatcher import TaskDispatcher
        return TaskDispatcher
    elif name == "DispatchResult":
        from .task_dispatcher import DispatchResult
        return DispatchResult
    elif name == "StateMachine":
        from .task_dispatcher import StateMachine
        return StateMachine
    elif name == "ProjectStateMachine":
        from .task_dispatcher import ProjectStateMachine
        return ProjectStateMachine
    elif name == "SupervisorContext":
        from .task_dispatcher import SupervisorContext
        return SupervisorContext
    elif name == "SupervisorDispatcher":
        from .task_dispatcher import SupervisorDispatcher
        return SupervisorDispatcher
    raise AttributeError(f"module 'core' has no attribute '{name}'")

__all__ = [
    "HermesClient",
    "Message",
    "MessageRole",
    "Tool",
    "ToolCall",
    "SubAgentContext",
    "TaskQueue",
    "TaskDispatcher",
    "DispatchResult",
    "StateMachine",
    "ProjectStateMachine",
    "SupervisorContext",
    "SupervisorDispatcher",
    "ConfigLoader"
]