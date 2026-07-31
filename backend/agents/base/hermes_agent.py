"""
Agent 基类实现
提供所有 Agent 的统一抽象层
"""

from dataclasses import dataclass, field
import json
import uuid
import time
from typing import Optional, Dict, List, Any, Type
from abc import ABC, abstractmethod
from enum import Enum

from core.hermes_client import HermesClient, Message, MessageRole, SubAgentContext


# ─── 全局 Agent 行为原则（所有 Agent 共享）────────────────────────────────────
#
# 这是系统级底线，不可被子类覆盖或删除。
# 所有 Agent 的 system prompt 必须包含此内容。
#
AGENT_PRINCIPLES = """
【系统行为原则 — 所有 Agent 必须遵守，不可违反】

一、执行原则
1. Plan Before Execute：收到任务后先在内部完整规划，再输出结果。不边想边写，不自相矛盾。需求审核与执行输出严格分离。
2. Agent-First：先确认任务三要素再动手——①执行什么具体任务 ②对象是谁 ③执行标准是什么。三点不明确直接提问，不猜测执行。
3. Test-Driven：输出前必须模拟验证。代码必须可运行，答案必须可验证，不确定的内容不输出。
4. Immutability：发现错误不打补丁，直接用新的完整版本替换旧状态。
5. Security-First：安全是底线不是选项。不确定就说不确定，不用模糊答案充数。

二、任务接收规范
收到任务时，先明确以下三点，不明确直接提问：
- 执行什么具体任务（做什么）
- 对象是谁（给谁做、影响什么）
- 执行标准是什么（怎么算完成）

三、输出规则
- 字数：用户有明确要求则遵守，没有则按需输出
- 场景匹配：内容必须符合当前使用场景
- 风格：用户未设置则参考当前角色的专业风格
- 额外要求：用户没提默认"没有"，不自行添加
- 格式：用户指定则用指定格式（表格/Markdown/流程图），未指定则按内容选最优格式
- 结构：输出结构分明，层次清晰，突出重点，按人类写作方式，不过分分段

四、背景信息
- 每个 Agent 必须了解当前项目的整体目标、工作逻辑和实现方式
- 项目背景由 PM 组长统一传达，Agent 收到背景信息后必须内化，不得忽略
""".strip()


class AgentType(Enum):
    """Agent 类型枚举"""
    PM = "pm"                    # 项目经理
    HR = "hr"                    # 人力资源
    SM = "sm"                    # Skill管理
    PG = "pg"                    # 项目文件管理
    SUPERVISOR = "supervisor"    # 调度中枢
    LEAD = "lead"                # 组长
    EMPLOYEE = "employee"        # 员工
    QA = "qa"                    # 质量保证
    PERF = "perf"                # 性能
    SEC = "sec"                  # 安全
    UXO = "uxo"                  # 用户体验
    CCB = "ccb"                  # 变更控制委员会


class AgentState(Enum):
    """Agent 状态枚举"""
    IDLE = "idle"               # 空闲
    WORKING = "working"         # 工作中
    WAITING = "waiting"         # 等待中
    BLOCKED = "blocked"         # 阻塞
    COMPLETED = "completed"     # 完成
    FAILED = "failed"           # 失败


class TaskPriority(Enum):
    """任务优先级"""
    LOW = 1
    NORMAL = 2
    HIGH = 3
    URGENT = 4
    CRITICAL = 5


@dataclass
class Task:
    """任务数据结构"""
    id: str
    title: str
    description: str
    agent_type: AgentType
    priority: TaskPriority = TaskPriority.NORMAL
    status: AgentState = AgentState.IDLE
    dependencies: List[str] = field(default_factory=list)
    assigned_to: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: Optional[Dict] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "agent_type": self.agent_type.value,
            "priority": self.priority.value,
            "status": self.status.value,
            "dependencies": self.dependencies,
            "assigned_to": self.assigned_to,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": self.result,
            "error": self.error,
            "metadata": self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "Task":
        return cls(
            id=data["id"],
            title=data["title"],
            description=data["description"],
            agent_type=AgentType(data["agent_type"]),
            priority=TaskPriority(data.get("priority", 2)),
            status=AgentState(data.get("status", "idle")),
            dependencies=data.get("dependencies", []),
            assigned_to=data.get("assigned_to"),
            created_at=data.get("created_at", time.time()),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            result=data.get("result"),
            error=data.get("error"),
            metadata=data.get("metadata", {})
        )


@dataclass
class Certificate:
    """验收证书"""
    id: str
    signer_id: str           # 签发者ID
    signer_type: AgentType   # 签发者类型
    task_id: str             # 关联任务
    content_hash: str        # 内容哈希
    signature: str           # 签名
    issued_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    checks_passed: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "signer_id": self.signer_id,
            "signer_type": self.signer_type.value,
            "task_id": self.task_id,
            "content_hash": self.content_hash,
            "signature": self.signature,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at or (time.time() + 86400),  # 默认24小时
            "checks_passed": self.checks_passed,
            "metadata": self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "Certificate":
        """从字典创建证书"""
        signer_type_str = data.get("signer_type", "employee")
        # 转换字符串到 AgentType
        signer_type = AgentType(signer_type_str) if isinstance(signer_type_str, str) else signer_type_str
        
        return cls(
            id=data["id"],
            signer_id=data["signer_id"],
            signer_type=signer_type,
            task_id=data["task_id"],
            content_hash=data["content_hash"],
            signature=data["signature"],
            issued_at=data.get("issued_at", time.time()),
            expires_at=data.get("expires_at"),
            checks_passed=data.get("checks_passed", []),
            metadata=data.get("metadata", {})
        )


@dataclass
class Project:
    """项目数据结构"""
    id: str
    name: str
    description: str
    status: str = "planning"  # planning -> executing -> completed
    created_at: float = field(default_factory=time.time)
    subprojects: List[Dict] = field(default_factory=list)
    team_members: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "created_at": self.created_at,
            "subprojects": self.subprojects,
            "team_members": self.team_members,
            "skills": self.skills,
            "metadata": self.metadata
        }


class AgentBase(ABC):
    """
    Agent 基类
    
    提供所有 Agent 的通用功能：
    - 唯一标识
    - 状态管理
    - Hermes 客户端
    - 记忆系统
    - 工具注册
    """

    # 基本能力极值（子类必须实现）
    ESSENTIAL_CAPABILITIES: List[str] = []

    def __init__(
        self,
        agent_id: Optional[str] = None,
        agent_type: Optional[AgentType] = None,
        hermes_client: Optional[HermesClient] = None,
        memory_store: Optional["MemoryStore"] = None,
        config: Optional[Dict] = None
    ):
        self.agent_id = agent_id or str(uuid.uuid4())[:8]
        self.agent_type = agent_type or AgentType.EMPLOYEE
        self.hermes = hermes_client or HermesClient()
        self.memory = memory_store
        self.config = config or {}
        
        # 状态
        self.state = AgentState.IDLE
        self.current_task: Optional[Task] = None
        
        # 工具注册表
        self.tools: Dict[str, callable] = {}
        
        # 上下文历史（用于生成摘要）
        self.context_history: List[Dict] = []
        
        # 检查极值
        self._validate_essential_capabilities()

    def _validate_essential_capabilities(self) -> None:
        """验证基本能力极值"""
        if not self.ESSENTIAL_CAPABILITIES:
            return
        
        # 子类必须定义基本能力
        if not hasattr(self, '_capabilities'):
            self._capabilities = []
        
        missing = set(self.ESSENTIAL_CAPABILITIES) - set(self._capabilities)
        if missing:
            raise ValueError(
                f"{self.__class__.__name__} 缺少基本能力: {missing}. "
                f"基本能力是系统极值，不可删除。"
            )

    def register_tool(self, name: str, func: callable, description: str = "") -> None:
        """注册工具"""
        self.tools[name] = func

    def get_tools(self) -> List[Dict]:
        """获取工具列表（用于 Hermes API）"""
        return [
            {
                "name": name,
                "description": desc or f"Tool: {name}",
                "parameters": {"type": "object", "properties": {}}
            }
            for name, desc in self.tools.items()
        ]

    def save_to_memory(self, key: str, value: Any) -> None:
        """保存到记忆系统"""
        if self.memory:
            self.memory.save(self.agent_id, key, value)

    def load_from_memory(self, key: str) -> Optional[Any]:
        """从记忆系统加载"""
        if self.memory:
            return self.memory.load(self.agent_id, key)
        return None

    def update_state(self, new_state: AgentState) -> None:
        """更新状态"""
        self.state = new_state

    def execute(self, task: Task) -> Dict[str, Any]:
        """
        执行任务（模板方法）
        
        子类需要实现 _do_execute 方法
        """
        self.current_task = task
        self.update_state(AgentState.WORKING)
        
        try:
            # 执行前钩子
            self._before_execute(task)
            
            # 执行任务
            result = self._do_execute(task)
            
            # 执行后钩子
            self._after_execute(task, result)
            
            self.update_state(AgentState.COMPLETED)
            return {"success": True, "result": result}
            
        except Exception as e:
            self.update_state(AgentState.FAILED)
            return {"success": False, "error": str(e)}

    @abstractmethod
    def _do_execute(self, task: Task) -> Any:
        """实际执行逻辑（子类必须实现）"""
        pass

    def _before_execute(self, task: Task) -> None:
        """执行前钩子"""
        self.context_history.append({
            "action": "task_start",
            "task_id": task.id,
            "timestamp": time.time()
        })

    def _after_execute(self, task: Task, result: Any) -> None:
        """执行后钩子"""
        self.context_history.append({
            "action": "task_complete",
            "task_id": task.id,
            "result_type": type(result).__name__,
            "timestamp": time.time()
        })

    def _compress_history(self, history: list, role_labels: tuple = ("用户", "Agent")) -> str:
        """
        公共历史压缩方法：调用 LLM 把对话历史压缩为结构化摘要。
        子类可直接调用，无需各自重复实现。

        Args:
            history: [{"role": "user"/"assistant", "content": "..."}, ...]
            role_labels: (用户标签, Agent标签)，默认 ("用户", "Agent")

        Returns:
            压缩后的摘要字符串；LLM 不可用时降级为最近 6 条拼接
        """
        if not history:
            return ""
        user_label, agent_label = role_labels
        lines = [
            f"{user_label if h.get('role') == 'user' else agent_label}：{h.get('content', '')}"
            for h in history
        ]
        try:
            resp = self.hermes.chat([
                Message(role=MessageRole.SYSTEM, content=(
                    "请将以下对话历史压缩为简洁的结构化摘要，"
                    "保留所有关键信息：已确认的需求、技术决策、待解决的问题、重要约束条件。\n"
                    "输出格式：\n"
                    "【已确认需求】...\n【技术决策】...\n【待确认问题】...\n【其他重要信息】..."
                )),
                Message(role=MessageRole.USER, content="\n".join(lines)),
            ])
            return resp.get("content", "")
        except Exception:
            return "\n".join(
                f"{h.get('role', '')}: {h.get('content', '')[:200]}"
                for h in history[-6:]
            )

    def generate_summary(self) -> str:
        """
        生成执行摘要

        父代理只接收纯文本摘要，不保留完整历史
        """
        lines = [
            f"Agent: {self.__class__.__name__} ({self.agent_id})",
            f"Type: {self.agent_type.value}",
            f"State: {self.state.value}",
            f"Current Task: {self.current_task.title if self.current_task else 'None'}",
            "",
            "Recent Actions:"
        ]
        
        for entry in self.context_history[-5:]:
            lines.append(f"  - {entry['action']} at {entry.get('timestamp', 0)}")
        
        return "\n".join(lines)

    def to_dict(self) -> Dict:
        """序列化为字典"""
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type.value,
            "state": self.state.value,
            "current_task": self.current_task.to_dict() if self.current_task else None,
            "tools": list(self.tools.keys()),
            "config": self.config
        }


class SubAgent(AgentBase):
    """
    子代理（Child Agent）
    
    特征：
    - 空白上下文
    - 无 task 工具（禁止递归）
    - 30 轮上限
    - 只返回纯文本摘要
    """

    MAX_ROUNDS = 30

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rounds = 0
        self.execution_log: List[Dict] = []

    def execute(self, task: Task, parent_context: Optional[List[Message]] = None) -> str:
        """
        执行任务并返回摘要
        
        Args:
            task: 任务对象
            parent_context: 父代理上下文（用于初始化）
            
        Returns:
            纯文本摘要
        """
        if self.rounds >= self.MAX_ROUNDS:
            return f"任务失败: 超出最大轮次限制 {self.MAX_ROUNDS}"

        # 创建空白上下文
        context = SubAgentContext(
            hermes_client=self.hermes,
            max_rounds=self.MAX_ROUNDS - self.rounds,
            task_id=task.id
        )

        try:
            # 执行任务
            result = self._do_execute(task)
            
            # 记录日志
            self.execution_log.append({
                "task_id": task.id,
                "result": str(result)[:200],  # 截断
                "rounds_used": context.round_count
            })
            
            self.rounds += context.round_count
            
            return f"Task {task.id} completed. Result: {str(result)[:500]}"
            
        except Exception as e:
            return f"Task {task.id} failed: {str(e)}"

    def _do_execute(self, task: Task) -> Any:
        """子代理执行逻辑"""
        raise NotImplementedError("SubAgent must implement _do_execute")


class LeadAgent(AgentBase):
    """
    组长 Agent
    
    额外职责：
    - 验收组内产出（任务需求全部实现 / 接口规范 / 代码风格 / Self-Check 通过）
    - 签发组内验收证书
    - 向 Supervisor 汇报阻塞/风险
    - 跨组禁止直接通信，通过 Supervisor 中转
    """

    # 基本能力极值
    ESSENTIAL_CAPABILITIES = [
        "组内任务验收",
        "接口规范审查",
        "产出完整性检查",
        "签发组内验收证书",
        "向上汇报阻塞风险",
    ]

    def __init__(self, *args, team_members: Optional[List[str]] = None, **kwargs):
        self._capabilities = list(self.ESSENTIAL_CAPABILITIES)
        super().__init__(*args, agent_type=AgentType.LEAD, **kwargs)
        self.team_members = team_members or []
        self.certificates_issued: List[str] = []  # 证书ID列表
        self.blocked_reports: List[Dict] = []      # 阻塞上报记录

    def validate_deliverable(
        self,
        task_id: str,
        content_hash: str,
        checklist: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        验收产出
        
        检查清单：
        1. 任务需求全部实现（checklist.requirements_met）
        2. 接口符合组内规范（checklist.interface_valid）
        3. 代码风格一致（checklist.style_consistent）
        4. Self-Check 已通过（checklist.self_check_passed）
        5. 文档/注释完整（checklist.docs_complete）
        
        任一不通过 → 打回修改，返回具体意见
        """
        checklist = checklist or {}
        issues = []

        if not checklist.get("requirements_met", True):
            issues.append("任务需求未全部实现")
        if not checklist.get("interface_valid", True):
            issues.append("接口不符合组内规范")
        if not checklist.get("style_consistent", True):
            issues.append("代码风格不一致")
        if not checklist.get("self_check_passed", True):
            issues.append("Self-Check 未通过，禁止提交")
        if not checklist.get("docs_complete", True):
            issues.append("文档/注释不完整")

        passed = len(issues) == 0
        return {
            "passed": passed,
            "task_id": task_id,
            "issues": issues,
            "message": "验收通过" if passed else f"验收不通过，需修改：{'；'.join(issues)}"
        }

    def issue_certificate(self, task_id: str, content_hash: str) -> Certificate:
        """
        签发验收证书
        
        Args:
            task_id: 任务ID
            content_hash: 内容哈希
            
        Returns:
            验收证书
        """
        cert = Certificate(
            id=str(uuid.uuid4()),
            signer_id=self.agent_id,
            signer_type=self.agent_type,
            task_id=task_id,
            content_hash=content_hash,
            signature=self._sign(content_hash),
            checks_passed=[
                "task_completed",
                "interface_valid",
                "style_consistent",
                "self_check_passed",
                "supervisor_accepted",
                "qa_approved",
                "perf_approved",
                "sec_approved",
                "uxo_approved"
            ]
        )
        
        self.certificates_issued.append(cert.id)
        return cert

    def _sign(self, content: str) -> str:
        """生成确定性签名（不含时间戳，保证可重复验证）"""
        import hashlib
        return hashlib.sha256(
            f"{self.agent_id}:{content}".encode()
        ).hexdigest()[:32]

    def _do_execute(self, task: Task) -> Any:
        """组长 Agent 执行逻辑"""
        if task.title == "validate":
            return {"validated": self.validate_deliverable(
                task.metadata.get("task_id", ""),
                task.metadata.get("content_hash", "")
            )}
        elif task.title == "issue_certificate":
            cert = self.issue_certificate(
                task.metadata.get("task_id", ""),
                task.metadata.get("content_hash", "")
            )
            return {"certificate": cert.to_dict()}
        return {"error": f"Unknown task: {task.title}"}

    def get_status(self) -> Dict[str, Any]:
        """获取组长状态"""
        return {
            "agent_id": self.agent_id,
            "type": "lead",
            "state": self.state.value,
            "team_members": self.team_members,
            "certificates_issued": len(self.certificates_issued)
        }


# 类型别名，用于 type hint
MemoryStore = "MemoryStore"