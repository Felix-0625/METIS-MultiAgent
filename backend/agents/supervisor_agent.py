"""
Supervisor Agent 实现
调度中枢 Agent，负责任务派发和进度监控
"""

import time
from typing import Dict, List, Optional, Any, Callable
from .base.hermes_agent import AgentBase, AgentType, Task, AgentState
from core.task_dispatcher import (
    TaskDispatcher, ProjectStateMachine, SupervisorDispatcher, SupervisorContext
)
from core.hermes_client import SubAgentContext, Message
from core.expert_pool import AGENT_CORE_PRINCIPLES


class SupervisorAgent(AgentBase):
    """
    Supervisor Agent
    
    职责：
    - 状态机调度
    - 资源协调
    - 进度监控
    - 质检触发
    - 子代理派发
    
    核心机制：
    - 通过 task 工具创建子代理
    - 子代理具有空白上下文、30轮上限、禁止递归、只返回摘要
    """

    # 基本能力极值
    ESSENTIAL_CAPABILITIES = [
        "状态机调度",
        "资源协调",
        "进度监控",
        "质检触发",
        "子代理派发"
    ]

    # 压缩阈值（与 PMAgent 保持一致）
    COMPRESS_THRESHOLD = 10
    KEEP_RECENT = 6

    def __init__(self, *args, project_id: Optional[str] = None, **kwargs):
        self.project_id = project_id or "default"
        self._capabilities = list(self.ESSENTIAL_CAPABILITIES)
        # 多轮对话历史 + 压缩摘要
        self.conversation_history: List[Dict] = []
        self.context_summary: str = ""
        super().__init__(*args, agent_type=AgentType.SUPERVISOR, **kwargs)
        
        # 调度器
        self.dispatcher = SupervisorDispatcher(
            supervisor_id=self.agent_id,
            project_id=self.project_id
        )
        
        # 子代理工厂
        self._subagent_factory: Optional[Callable] = None
        
        # 注册回调
        self.dispatcher.register_callback("task_complete", self._on_task_complete)

    def set_subagent_factory(self, factory: Callable) -> None:
        """设置子代理工厂"""
        self._subagent_factory = factory
        self.dispatcher.set_subagent_factory(factory)

    def set_pm_plan(self, plan_summary: str) -> None:
        """注入 PM 规划方案摘要，让 Supervisor 知晓整体项目方案"""
        self._pm_plan_summary = plan_summary

    # ─── 多轮对话（含历史压缩）────────────────────────────────────────────────

    def _compress_history(self, history: List[Dict]) -> str:
        """把历史对话压缩为结构化摘要"""
        if not history:
            return ""
        lines = []
        for h in history:
            role_label = "用户" if h.get("role") == "user" else "Supervisor Agent"
            lines.append(f"{role_label}：{h.get('content', '')}")
        history_text = "\n".join(lines)
        from core.hermes_client import MessageRole
        compress_prompt = [
            Message(role=MessageRole.SYSTEM, content=(
                "你是一个项目管理助手。请将以下对话历史压缩为简洁的结构化摘要，"
                "保留所有关键信息：项目进度状态、已触发的质检、阻塞任务、重要决策。"
                "输出格式：\n"
                "【项目进度】...\n【已触发质检】...\n【阻塞任务】...\n【重要决策】..."
            )),
            Message(role=MessageRole.USER, content=f"请压缩以下对话历史：\n\n{history_text}"),
        ]
        try:
            resp = self.hermes.chat(compress_prompt)
            return resp.get("content", "")
        except Exception:
            return "\n".join(f"{h.get('role','')}: {h.get('content','')[:200]}" for h in history[-6:])

    def chat(
        self,
        user_input: str,
        progress_data: Optional[Dict] = None,
        history: Optional[List[Dict]] = None,
        context_summary: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        多轮对话（含历史压缩）
        
        Args:
            user_input: 用户输入
            progress_data: 当前项目进度数据（自动注入到 system prompt）
            history: 前端传来的历史消息
            context_summary: 前端缓存的压缩摘要
        """
        from core.hermes_client import MessageRole

        # 构建进度上下文
        progress_ctx = ""
        if progress_data:
            d = progress_data
            progress_ctx = (
                f"\n\n当前项目进度：\n"
                f"总任务：{d.get('total_tasks', '-')} | 已完成：{d.get('completed_tasks', '-')} | "
                f"进行中：{d.get('in_progress', '-')}\n"
                f"整体进度：{d.get('overall_progress', 0)}%\n"
                + (f"阻塞任务：{', '.join(d.get('blocked_tasks', []))}\n" if d.get('blocked_tasks') else "无阻塞任务\n")
            )

        # 注入 PM 规划摘要（让 Supervisor 知晓整体方案）
        pm_plan_ctx = ""
        if hasattr(self, "_pm_plan_summary") and self._pm_plan_summary:
            pm_plan_ctx = f"\n\n【PM 规划方案摘要】\n{self._pm_plan_summary}"

        system_prompt = (
            AGENT_CORE_PRINCIPLES + "\n\n"
            "你是一个经验丰富的项目监督 Agent（Supervisor Agent）。你负责任务调度、进度监控、质检触发和变更协调。\n\n"
            "【角色职责】\n"
            "1. 知晓并理解 PM Agent 敲定的完整项目方案（已在下方提供）\n"
            "2. 基于实时进度数据给出准确的状态报告，不猜测、不捏造数据\n"
            "3. 主动识别阻塞风险并提出具体可执行的解决建议\n"
            "4. 当用户要求修改功能或新增需求时，先分析影响范围，再告知调整方案\n"
            "5. 协调 HR Agent 重新分配任务（谁开发谁修复原则）\n"
            "6. 质检不通过时，将修复任务分配给原开发 Agent\n"
            "7. 支持用户触发质检、查询进度、处理阻塞\n\n"
            "【工作流程】\n"
            "收到用户请求时，先确认：执行什么任务 / 对象是谁 / 执行标准是什么，不明确直接提问。\n"
            "输出前完成内部规划，不边想边写。\n\n"
            "【调度与质检技能框架 — Senior Code Reviewer + Orchestrator】\n\n调度原则：\n- 任务派发前确认：执行者能力匹配度、任务依赖关系、资源可用性\n- 并行优先：无依赖关系的任务并行执行，最大化吞吐量\n- 失败快速：子任务失败立即上报，不等待超时\n\n代码/产出质检五维框架：\n1. 正确性：产出是否符合需求规格？边界条件是否处理？\n2. 可读性：其他人能否无需解释就理解？命名是否清晰一致？\n3. 架构：是否遵循现有模式？新模式是否有充分理由？\n4. 安全性：是否有注入风险？认证授权是否正确？敏感数据是否保护？\n5. 性能：是否有明显的性能问题？资源使用是否合理？\n\n质检输出格式：\n- PASS：产出符合所有维度要求，附简要说明\n- NEEDS_REVISION：列出具体问题（维度+问题描述+修改建议），打回重做\n- BLOCKED：发现阻塞性问题，触发 CCB 仲裁\n\n监控规范：\n- 每个子任务设置超时阈值（默认30分钟）\n- 进度更新频率：每完成一个子任务立即更新\n- 异常上报：阻塞/超时/质检失败必须记录原因\n"
            + pm_plan_ctx
            + progress_ctx
        )

        hist = history or self.conversation_history
        new_summary = context_summary or self.context_summary

        # 超过阈值时压缩
        if len(hist) > self.COMPRESS_THRESHOLD:
            to_compress = hist[:-self.KEEP_RECENT]
            recent_raw = hist[-self.KEEP_RECENT:]
            compressed = self._compress_history(to_compress)
            new_summary = (new_summary + "\n\n【新增摘要】\n" + compressed) if new_summary else compressed
        else:
            recent_raw = hist

        messages = [Message(role=MessageRole.SYSTEM, content=system_prompt)]
        if new_summary:
            messages.append(Message(
                role=MessageRole.SYSTEM,
                content=f"以下是之前对话的压缩摘要：\n\n{new_summary}"
            ))
        for h in recent_raw:
            role = MessageRole.USER if h.get("role") == "user" else MessageRole.ASSISTANT
            content = h.get("content", "")
            if content:
                messages.append(Message(role=role, content=content))
        messages.append(Message(role=MessageRole.USER, content=user_input))

        try:
            response = self.hermes.chat(messages)
            reply = response.get("content", "")
        except Exception:
            reply = ""

        # LLM 不可用时给出结构化 fallback（不返回空字符串）
        if not reply or reply.startswith("⚠️") or reply.startswith("❌"):
            reply = self._fallback_supervisor_reply(user_input, progress_data)

        # 更新内部状态
        self.conversation_history.append({"role": "user", "content": user_input})
        self.conversation_history.append({"role": "assistant", "content": reply})
        if new_summary:
            self.context_summary = new_summary
        if len(self.conversation_history) > self.KEEP_RECENT * 2:
            self.conversation_history = self.conversation_history[-(self.KEEP_RECENT * 2):]

        return {
            "reply": reply,
            "success": True,
            "summary": new_summary or "",
        }

    def _fallback_supervisor_reply(self, user_input: str, progress_data: Optional[Dict] = None) -> str:
        """LLM 不可用时的结构化 fallback 回复"""
        progress_text = ""
        if progress_data:
            d = progress_data
            progress_text = (
                f"\n\n📊 **当前项目进度**\n"
                f"- 总任务：{d.get('total_tasks', '-')} | 已完成：{d.get('completed_tasks', '-')}\n"
                f"- 整体进度：{d.get('overall_progress', 0)}%\n"
            )
            if d.get('blocked_tasks'):
                progress_text += f"- ⚠️ 阻塞任务：{', '.join(d['blocked_tasks'])}\n"

        return (
            f"【Supervisor Agent 离线模式】\n\n"
            f"已收到：「{user_input[:200]}」"
            f"{progress_text}\n\n"
            f"⚠️ 当前未配置 API Key，无法提供智能分析。\n"
            f"请在「设置」页面配置 LLM API Key 后，Supervisor Agent 将提供完整的进度监控和质检协调功能。\n\n"
            f"**可用操作**：\n"
            f"- 点击「质检」按钮触发代码审查\n"
            f"- 点击「确认阶段完成」推进到下一阶段\n"
            f"- 在问题列表中点击「反馈给PM」处理质检问题"
        )

    def create_task(
        self,
        title: str,
        description: str,
        agent_type: AgentType,
        priority: int = 2,
        dependencies: Optional[List[str]] = None
    ) -> Task:
        """
        创建任务
        
        Args:
            title: 任务标题
            description: 任务描述
            agent_type: 目标 Agent 类型
            priority: 优先级 (1-5)
            dependencies: 依赖任务 ID 列表
            
        Returns:
            创建的任务
        """
        from .base.hermes_agent import TaskPriority
        
        task = self.dispatcher.dispatcher.create_task(
            title=title,
            description=description,
            agent_type=agent_type,
            priority=TaskPriority(priority),
            dependencies=dependencies
        )
        
        return task

    def dispatch_subagent(
        self,
        task: Task,
        instruction: str,
        context_data: Optional[Dict] = None
    ) -> str:
        """
        派发子代理任务
        
        子代理特征：
        - 空白上下文 (messages = [])
        - 无 task 工具（禁止递归）
        - 30 轮上限
        - 只返回纯文本摘要
        
        Args:
            task: 任务对象
            instruction: 给子代理的指令
            context_data: 上下文数据
            
        Returns:
            执行摘要
        """
        # 创建子代理
        subagent = self.dispatcher.create_subagent(task.agent_type, task)
        
        if not subagent:
            return f"Failed to create subagent for task {task.id}"
        
        # 构建初始化消息（role 必须使用 MessageRole 枚举）
        from core.hermes_client import MessageRole
        init_messages = [
            Message(role=MessageRole.SYSTEM, content=instruction)
        ]
        
        if context_data:
            context_str = f"\n\nContext:\n{context_data}"
            init_messages.append(Message(role=MessageRole.USER, content=context_str))
        
        # 创建子代理上下文（空白）
        sub_context = SubAgentContext(
            hermes_client=self.hermes,
            max_rounds=30,
            task_id=task.id
        )
        
        # 添加初始化消息
        for msg in init_messages:
            sub_context.add_message(msg.role, msg.content)
        
        # 执行子代理任务
        try:
            result = subagent.execute(task)
            
            # 生成摘要
            summary = subagent.generate_summary()
            
            # 更新 Supervisor 上下文
            self.dispatcher.context.add_summary(summary)
            
            return summary
            
        except Exception as e:
            error_summary = f"Task {task.id} failed: {str(e)}"
            self.dispatcher.context.add_summary(error_summary)
            return error_summary

    def trigger_qa(self, subproject_id: str) -> Dict[str, Any]:
        """
        触发 QA 质检
        
        Args:
            subproject_id: 子项目 ID
            
        Returns:
            质检任务信息
        """
        # 创建 QA 任务
        qa_task = self.create_task(
            title=f"QA-{subproject_id}",
            description=f"质量保证检查: {subproject_id}",
            agent_type=AgentType.QA,
            priority=3
        )
        
        return {
            "task_id": qa_task.id,
            "subproject_id": subproject_id,
            "type": "qa"
        }

    def trigger_perf(self, subproject_id: str) -> Dict[str, Any]:
        """触发性能质检"""
        perf_task = self.create_task(
            title=f"Perf-{subproject_id}",
            description=f"性能基准测试: {subproject_id}",
            agent_type=AgentType.PERF,
            priority=3
        )
        
        return {
            "task_id": perf_task.id,
            "subproject_id": subproject_id,
            "type": "perf"
        }

    def trigger_sec(self, subproject_id: str) -> Dict[str, Any]:
        """触发安全质检"""
        sec_task = self.create_task(
            title=f"Sec-{subproject_id}",
            description=f"安全审计检查: {subproject_id}",
            agent_type=AgentType.SEC,
            priority=4  # 安全优先级较高
        )
        
        return {
            "task_id": sec_task.id,
            "subproject_id": subproject_id,
            "type": "sec"
        }

    def trigger_uxo(self, subproject_id: str) -> Dict[str, Any]:
        """触发体验优化质检"""
        uxo_task = self.create_task(
            title=f"UXO-{subproject_id}",
            description=f"用户体验检查: {subproject_id}",
            agent_type=AgentType.UXO,
            priority=2
        )
        
        return {
            "task_id": uxo_task.id,
            "subproject_id": subproject_id,
            "type": "uxo"
        }

    def trigger_all_quality_checks(
        self,
        subproject_id: str,
        workspace_path: Optional[str] = None,
        output_files: Optional[List[str]] = None,
    ) -> List[Dict]:
        """
        触发全部四层质检，并真正执行质检 Agent
        
        质检 Agent 独立汇报线：直接向 Supervisor 汇报，不经过组长。
        拥有否决权：任一不通过则标记 vetoed=True，阻止归档。
        
        Args:
            subproject_id: 子项目 ID
            workspace_path: 项目工作区路径（供质检 Agent 扫描文件）
            output_files: 产出文件列表
        """
        from agents.quality_agents import QAAgent, PerfAgent, SecAgent, UXOAgent

        results = []
        veto_triggered = False

        qa_agents = [
            ("qa",   QAAgent,   self.trigger_qa),
            ("perf", PerfAgent, self.trigger_perf),
            ("sec",  SecAgent,  self.trigger_sec),
            ("uxo",  UXOAgent,  self.trigger_uxo),
        ]

        for qc_type, AgentClass, trigger_fn in qa_agents:
            # 1. 创建任务记录
            task_info = trigger_fn(subproject_id)

            # 2. 实例化质检 Agent（独立子代理，空白上下文）
            try:
                agent = AgentClass(hermes_client=self.hermes)
                inspect_result = agent.inspect(
                    subproject_id=subproject_id,
                    workspace_path=workspace_path or "",
                    output_files=output_files or [],
                )
            except Exception as e:
                inspect_result = {"passed": False, "issues": [str(e)], "score": 0}

            passed = inspect_result.get("passed", True)
            if not passed and getattr(AgentClass, "veto_power", False):
                veto_triggered = True

            # 3. 汇报给 Supervisor（独立汇报线，不经过组长）
            summary = (
                f"[{qc_type.upper()}] 子项目 {subproject_id} 质检"
                + ("通过" if passed else f"不通过：{inspect_result.get('issues', [])}")
            )
            self.dispatcher.context.add_summary(summary)

            results.append({
                **task_info,
                "passed": passed,
                "score": inspect_result.get("score", 100 if passed else 0),
                "issues": inspect_result.get("issues", []),
                "veto": not passed and getattr(AgentClass, "veto_power", False),
                "summary": summary,
            })

        return results

    def chat_with_user_isolated(
        self,
        user_input: str,
        history: Optional[List[Dict]] = None,
        context_summary: Optional[str] = None,
        progress_data: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        用户沟通子代理隔离（架构文档要求）
        
        用户反馈/修改需求 → 创建临时沟通子代理（30轮内沟通）
        → 只返回摘要给 Supervisor → 子代理销毁，不污染主项目上下文
        
        实现：在独立的 SubAgentContext 中完成对话，只把摘要写入 Supervisor 上下文。
        """
        from core.hermes_client import MessageRole, SubAgentContext as _SubCtx

        # 创建临时子代理上下文（空白，30轮上限）
        sub_ctx = _SubCtx(
            hermes_client=self.hermes,
            max_rounds=30,
            task_id=f"user-comm-{int(time.time())}",
        )

        # 构建进度上下文
        progress_ctx = ""
        if progress_data:
            d = progress_data
            progress_ctx = (
                f"\n\n当前项目进度：总任务 {d.get('total_tasks','-')} | "
                f"已完成 {d.get('completed_tasks','-')} | "
                f"整体进度 {d.get('overall_progress',0)}%"
            )

        system_content = (
            "你是一个项目监督 Agent（Supervisor Agent）的临时沟通代理。"
            "你的任务是在 30 轮内与用户完成沟通，收集用户的反馈或需求变更，"
            "然后生成一份简洁的摘要返回给 Supervisor。"
            "不要做任何实际的项目修改，只负责沟通和记录。"
            + progress_ctx
        )

        # 注入历史（如果有）
        hist = history or []
        new_summary = context_summary or self.context_summary

        # 超过阈值时压缩
        if len(hist) > self.COMPRESS_THRESHOLD:
            to_compress = hist[:-self.KEEP_RECENT]
            recent_raw = hist[-self.KEEP_RECENT:]
            compressed = self._compress_history(to_compress)
            new_summary = (new_summary + "\n\n【新增摘要】\n" + compressed) if new_summary else compressed
        else:
            recent_raw = hist

        # 在子代理上下文中构建消息
        sub_ctx.add_message(MessageRole.SYSTEM, system_content)
        if new_summary:
            sub_ctx.add_message(MessageRole.SYSTEM, f"之前对话摘要：\n{new_summary}")
        for h in recent_raw:
            role = MessageRole.USER if h.get("role") == "user" else MessageRole.ASSISTANT
            if h.get("content"):
                sub_ctx.add_message(role, h["content"])
        sub_ctx.add_message(MessageRole.USER, user_input)

        # 执行（单次 LLM 调用，子代理上下文）
        try:
            response = self.hermes.chat(sub_ctx.messages)
            reply = response.get("content", "")
        except Exception as e:
            reply = f"沟通子代理执行失败：{e}"

        # 生成摘要（只把摘要写入 Supervisor 上下文，不保留完整历史）
        comm_summary = f"[用户沟通] {user_input[:100]}... → {reply[:200]}..."
        self.dispatcher.context.add_summary(comm_summary)

        # 更新 Supervisor 自身的压缩摘要（不追加原始历史）
        if new_summary:
            self.context_summary = new_summary

        return {
            "reply": reply,
            "success": True,
            "summary": new_summary or "",
            "isolated": True,  # 标记为隔离沟通
        }

    def check_blockage(self) -> List[Dict]:
        """检查阻塞情况"""
        return self.dispatcher.check_blockage()

    def transition_state(self, new_state: str) -> bool:
        """状态机转换"""
        return self.dispatcher.state_machine.transition(new_state)

    def get_project_summary(self) -> str:
        """获取项目摘要"""
        return self.dispatcher.context.get_summary()

    def get_progress(self) -> Dict[str, Any]:
        """获取项目进度"""
        return self.dispatcher.state_machine.get_progress()

    def _on_task_complete(self, task_id: str, result: Any) -> None:
        """任务完成回调"""
        self.dispatcher.context.add_summary(f"Task {task_id} completed")

    def _do_execute(self, task: Task) -> Any:
        """执行任务"""
        if task.title.startswith("QA-"):
            return self.trigger_qa(task.title.replace("QA-", ""))
        elif task.title.startswith("Perf-"):
            return self.trigger_perf(task.title.replace("Perf-", ""))
        elif task.title.startswith("Sec-"):
            return self.trigger_sec(task.title.replace("Sec-", ""))
        elif task.title.startswith("UXO-"):
            return self.trigger_uxo(task.title.replace("UXO-", ""))
        elif task.title == "dispatch":
            return self.dispatch_subagent(
                task,
                task.metadata.get("instruction", ""),
                task.metadata.get("context")
            )
        elif task.title == "check_blockage":
            return {"blocked": self.check_blockage()}
        elif task.title == "progress":
            return self.get_progress()
        else:
            return {"error": f"Unknown task: {task.title}"}

    def sign_off(self, project_id: str) -> Dict[str, Any]:
        """
        签核项目
        
        项目全部完成 + 质检通过 → 签核
        """
        # 检查条件
        progress = self.get_progress()
        
        if progress.get("failed", 0) > 0:
            return {
                "success": False,
                "reason": "有子项目失败"
            }
        
        if progress.get("completed", 0) < progress.get("total", 0):
            return {
                "success": False,
                "reason": "还有子项目未完成"
            }
        
        # 检查阻塞
        blocked = self.check_blockage()
        if blocked:
            return {
                "success": False,
                "reason": f"有 {len(blocked)} 个任务阻塞"
            }
        
        # 签核通过
        self.dispatcher.context.add_decision(
            "项目签核",
            f"项目 {project_id} 全部完成，签核通过"
        )
        
        return {
            "success": True,
            "project_id": project_id,
            "signed_off_at": self.dispatcher.context.decisions[-1]["timestamp"]
        }

    def get_status(self) -> Dict[str, Any]:
        """获取 Supervisor Agent 状态"""
        return {
            "agent_id": self.agent_id,
            "type": "supervisor",
            "project_id": self.project_id,
            "state": self.state.value,
            "project_state": self.dispatcher.state_machine.get_state(),
            "progress": self.get_progress(),
            "blocked_tasks": self.check_blockage(),
            "context_summary": self.get_project_summary()
        }