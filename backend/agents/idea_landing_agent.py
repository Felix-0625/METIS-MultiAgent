"""
IdeaLanding Agent — 想法落地 Agent
═══════════════════════════════════════════════════════════════════════════════

角色定位
─────────────────────────────────────────────────────────────────────────────
你是一位专职"想法落地顾问"。用户带着模糊的想法或产品点子来到这里，
你的使命是帮他把想法从混沌变成清晰的、可执行的需求文档。

工作流程（三阶段，严格分阶段推进）
─────────────────────────────────────────────────────────────────────────────
阶段 1 — 压力测试（Pressure Test）
  参考 start-pressure-test 开源项目逻辑：
  对用户想法进行多维度拷问，识别假设、盲区、矛盾点。
  通过问题+反例+竞品对比，帮用户把想法"压实"。
  用户满意后才进入阶段 2。

阶段 2 — 需求文档（Requirements Doc）
  参考 grill 开源项目的需求挖掘逻辑：
  生成结构化需求文档（目标/用户故事/功能清单/约束/验收标准）。

阶段 3 — 优化补充（Refine & Fill Gaps）
  与用户逐条讨论，补充细节、消除歧义、完善边界条件。
  最终输出可直接提交给开发团队的完整需求文档。

对话记忆
─────────────────────────────────────────────────────────────────────────────
- 每个"对话"独立隔离，不同对话的上下文互不干扰
- 用户有个人记忆（preferences、历史 idea 摘要），在所有对话中共享
- 支持对话置顶、打标签、分类管理
"""

import time
import uuid
import json
from typing import Dict, List, Optional, Any


# ── IdeaPhase 枚举 ──────────────────────────────────────────────────────────────

class IdeaPhase:
    STRESS_TEST = "stress_test"
    REQUIREMENT_DOC = "requirement"
    OPTIMIZATION = "optimization"
    COMPLETED = "completed"

from agents.base.hermes_agent import AgentBase, AgentType, Task, AgentState, AGENT_PRINCIPLES
from core.hermes_client import HermesClient, Message, MessageRole


# ─── Agent 角色 System Prompt ────────────────────────────────────────────────

IDEA_LANDING_SYSTEM_PROMPT = f"""
{AGENT_PRINCIPLES}

════════════════════════════════════════════════════════════════════════
你是「想法落地顾问」（IdeaLanding Agent）
════════════════════════════════════════════════════════════════════════

【角色定位】
你专门帮用户把模糊的产品想法、创业点子、功能设想，变成清晰可执行的需求文档。
你不是开发者，不写代码，不讨论技术实现细节——你只聚焦在"要做什么"和"为什么做"。

【三阶段工作流程 — 严格按顺序推进，不可跳过】

▌阶段 1：压力测试（Pressure Test）
目标：帮用户发现想法中的假设、盲区、矛盾。
方式：
- 提出尖锐但友善的质疑问题（5~8个，分批提出，不要一次全问）
- 用反例和竞品类比挑战用户的假设
- 挖掘"用户说要A，实际需要B"的场景
- 识别想法中最脆弱的一个点，重点测压
结束条件：用户觉得想法经得住拷问，主动说"可以了"/"通过了"/"继续"

▌阶段 2：需求文档
目标：把通过压测的想法转化为结构化需求文档。
格式：
  ## 项目目标
  （一句话说清楚）
  ## 目标用户
  （用户画像 + 核心痛点）
  ## 用户故事
  （As a ... I want to ... So that ...，3~6条）
  ## 核心功能清单
  （按优先级：P0 必做 / P1 重要 / P2 可选）
  ## 约束条件
  （技术约束、时间约束、预算约束）
  ## 验收标准
  （可量化、可测试的完成条件）
  ## 不做什么（Out of Scope）
  （明确边界）

▌阶段 3：优化补充
目标：逐条深化需求，消除歧义，填补细节。
方式：
- 针对每个功能点追问边界条件
- 询问异常场景和降级策略
- 最终输出"可直接提交开发团队"的完整需求文档

【当前阶段标记规则】
- 每次回复末尾必须包含：`[当前阶段: X]`（X=1/2/3）
- 阶段升级时加：`[阶段升级: X→Y]`

【对话风格】
- 直接、专业，像一个经验丰富的产品经理在做需求评审
- 提问时一次最多提 2~3 个问题，不要列一堆
- 不用客套话，不说"好的，我明白了"这种废话
- 对于模糊的表述，直接举例说明你理解的意思，让用户确认或纠正

【收到任务前必须明确的三点】
1. 执行什么具体任务（这个想法是什么）
2. 对象是谁（目标用户是谁）
3. 执行标准是什么（想法落地到什么程度算完成）
——如果用户没说清楚，直接提问，不猜测。
""".strip()


# ─── 对话实体 ─────────────────────────────────────────────────────────────────

class IdeaConversation:
    """单个对话实体——记忆与其他对话完全隔离"""

    PHASES = {
        1: "压力测试",
        2: "需求文档",
        3: "优化补充",
    }

    COMPRESS_THRESHOLD = 12   # 超过 12 条原始消息触发历史压缩
    KEEP_RECENT = 6           # 压缩后保留最近 6 条原文

    def __init__(
        self,
        conv_id: Optional[str] = None,
        title: str = "新对话",
        tags: Optional[List[str]] = None,
        pinned: bool = False,
        category: str = "默认",
        created_at: Optional[float] = None,
    ):
        self.conv_id: str = conv_id or f"conv-{uuid.uuid4().hex[:8]}"
        self.title: str = title
        self.tags: List[str] = tags or []
        self.pinned: bool = pinned
        self.category: str = category
        self.created_at: float = created_at or time.time()
        self.updated_at: float = time.time()

        # 当前阶段：1=压力测试 2=需求文档 3=优化补充
        self.current_phase: int = 1
        # 阶段完成标记
        self.phase_completed: Dict[int, bool] = {1: False, 2: False, 3: False}
        # 当前阶段 IdeaPhase 枚举值
        self.phase: str = "stress_test"

        # 对话历史（当前对话私有）
        self.messages: List[Dict] = []
        # 压缩后摘要
        self.context_summary: str = ""
        # 最终需求文档（阶段3产出）
        self.requirements_doc: str = ""

    # ── 阶段控制 ──────────────────────────────────────────────────────────────

    def advance_phase(self) -> bool:
        """推进到下一阶段，返回是否成功"""
        """推进到下一阶段，返回是否成功"""
        if self.current_phase >= 3:
            return False
        self.phase_completed[self.current_phase] = True
        self.current_phase += 1
        self._sync_phase_enum()
        return True
    def get_phase_name(self) -> str:
        return self.PHASES.get(self.current_phase, "未知阶段")

    def _sync_phase_enum(self) -> None:
        """根据 current_phase 同步更新 phase 枚举字段"""
        ph = {1: "stress_test", 2: "requirement", 3: "optimization", 4: "completed"}
        self.phase = ph.get(self.current_phase, "completed")

    def get_current_phase(self) -> dict:
        """返回当前阶段的详细信息"""
        pn = {1: "\u538b\u529b\u6d4b\u8bd5", 2: "\u9700\u6c42\u6587\u6863", 3: "\u4f18\u5316\u8865\u5145"}
        pk = {1: "stress_test", 2: "requirement", 3: "optimization"}
        return {
            "phase_number": self.current_phase,
            "phase_key": pk.get(self.current_phase, "completed"),
            "phase_name": pn.get(self.current_phase, "\u5df2\u5b8c\u6210"),
            "phase_completed": {str(k): v for k, v in self.phase_completed.items()},
            "is_completed": self.current_phase >= 3 and self.phase_completed.get(3, False),
        }

    # ── 序列化 ────────────────────────────────────────────────────────────────

    def to_dict(self) -> Dict:
        return {
            "conv_id": self.conv_id,
            "title": self.title,
            "tags": self.tags,
            "pinned": self.pinned,
            "category": self.category,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "current_phase": self.current_phase,
            "phase_completed": self.phase_completed,
            "phase": self.phase,
            "messages": self.messages,
            "context_summary": self.context_summary,
            "requirements_doc": self.requirements_doc,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "IdeaConversation":
        c = cls(
            conv_id=data["conv_id"],
            title=data.get("title", "新对话"),
            tags=data.get("tags", []),
            pinned=data.get("pinned", False),
            category=data.get("category", "默认"),
            created_at=data.get("created_at"),
        )
        c.updated_at = data.get("updated_at", c.created_at)
        c.current_phase = data.get("current_phase", 1)
        # phase_completed 的 key 可能是字符串（JSON反序列化），转为 int
        raw_pc = data.get("phase_completed", {})
        c.phase_completed = {int(k): v for k, v in raw_pc.items()} if raw_pc else {1: False, 2: False, 3: False}
        c.phase = data.get("phase", {1: "stress_test", 2: "requirement", 3: "optimization", 4: "completed"}.get(c.current_phase, "stress_test"))
        c.messages = data.get("messages", [])
        c.context_summary = data.get("context_summary", "")
        c.requirements_doc = data.get("requirements_doc", "")
        return c


# ─── 用户个人记忆 ─────────────────────────────────────────────────────────────

class UserMemory:
    """
    跨对话共享的用户个人记忆
    记录：偏好、历史 idea 摘要、常用标签、行业背景
    """

    def __init__(self, user_id: str = "default"):
        self.user_id = user_id
        self.preferences: Dict[str, Any] = {}       # 用户偏好（风格、行业等）
        self.idea_summaries: List[Dict] = []         # 历史 idea 摘要（最多50条）
        self.frequent_tags: List[str] = []           # 常用标签
        self.background: str = ""                    # 用户背景信息
        self.created_at: float = time.time()
        self.updated_at: float = time.time()

    def add_idea_summary(self, conv_id: str, title: str, summary: str, tags: List[str]) -> None:
        """记录一个 idea 的摘要到个人记忆"""
        self.idea_summaries.append({
            "conv_id": conv_id,
            "title": title,
            "summary": summary[:500],   # 截断，不占太多空间
            "tags": tags,
            "recorded_at": time.time(),
        })
        # 只保留最近 50 条
        if len(self.idea_summaries) > 50:
            self.idea_summaries = self.idea_summaries[-50:]
        # 更新常用标签
        for tag in tags:
            if tag not in self.frequent_tags:
                self.frequent_tags.append(tag)
        if len(self.frequent_tags) > 20:
            self.frequent_tags = self.frequent_tags[-20:]
        self.updated_at = time.time()

    def get_context_for_prompt(self) -> str:
        """生成注入 prompt 的个人记忆上下文"""
        parts = []
        if self.background:
            parts.append(f"用户背景：{self.background}")
        if self.preferences:
            prefs = json.dumps(self.preferences, ensure_ascii=False)
            parts.append(f"用户偏好：{prefs}")
        if self.idea_summaries:
            recent = self.idea_summaries[-3:]
            ideas_text = "\n".join(
                f"  - 「{s['title']}」（{s.get('summary', '')[:100]}）"
                for s in recent
            )
            parts.append(f"最近几个想法（供参考，但不要主动提及，除非用户问起）：\n{ideas_text}")
        return "\n".join(parts) if parts else ""

    def to_dict(self) -> Dict:
        return {
            "user_id": self.user_id,
            "preferences": self.preferences,
            "idea_summaries": self.idea_summaries,
            "frequent_tags": self.frequent_tags,
            "background": self.background,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "UserMemory":
        m = cls(user_id=data.get("user_id", "default"))
        m.preferences = data.get("preferences", {})
        m.idea_summaries = data.get("idea_summaries", [])
        m.frequent_tags = data.get("frequent_tags", [])
        m.background = data.get("background", "")
        m.created_at = data.get("created_at", time.time())
        m.updated_at = data.get("updated_at", time.time())
        return m


# ─── IdeaLanding Agent 主体 ───────────────────────────────────────────────────

class IdeaLandingAgent(AgentBase):
    """
    想法落地 Agent

    职责：
    1. 压力测试用户想法（识别盲区/假设/矛盾）
    2. 生成结构化需求文档
    3. 逐条优化补充，产出可交付的完整需求文档

    特性：
    - 每个对话独立隔离（IdeaConversation）
    - 用户个人记忆跨对话共享（UserMemory）
    - 支持对话置顶、标签、分类
    - 开启新对话时旧对话记忆不污染新对话
    """

    ESSENTIAL_CAPABILITIES = [
        "压力测试想法",
        "生成需求文档",
        "优化补充需求",
        "对话隔离管理",
        "个人记忆维护",
    ]

    # 历史压缩阈值
    COMPRESS_THRESHOLD = 12
    KEEP_RECENT = 6

    def __init__(self, *args, **kwargs):
        self._capabilities = list(self.ESSENTIAL_CAPABILITIES)
        super().__init__(*args, agent_type=AgentType.PM, **kwargs)

        # 对话存储：conv_id → IdeaConversation
        self.conversations: Dict[str, IdeaConversation] = {}
        # 用户个人记忆（单实例，跨对话共享）
        self.user_memory: UserMemory = UserMemory()
        # 当前激活的对话 ID
        self.active_conv_id: Optional[str] = None

    # ── 对话管理 ──────────────────────────────────────────────────────────────

    def new_conversation(
        self,
        title: str = "新对话",
        tags: Optional[List[str]] = None,
        category: str = "默认",
    ) -> IdeaConversation:
        """创建并激活一个新的隔离对话"""
        conv = IdeaConversation(title=title, tags=tags or [], category=category)
        self.conversations[conv.conv_id] = conv
        self.active_conv_id = conv.conv_id
        return conv

    def get_conversation(self, conv_id: str) -> Optional[IdeaConversation]:
        return self.conversations.get(conv_id)

    def switch_conversation(self, conv_id: str) -> bool:
        """切换激活对话，返回是否成功"""
        if conv_id not in self.conversations:
            return False
        self.active_conv_id = conv_id
        return True

    def delete_conversation(self, conv_id: str) -> bool:
        if conv_id not in self.conversations:
            return False
        # 删除前把摘要保存到个人记忆
        conv = self.conversations[conv_id]
        if conv.messages:
            summary = conv.context_summary or (conv.messages[-1].get("content", "")[:200] if conv.messages else "")
            self.user_memory.add_idea_summary(
                conv_id=conv_id,
                title=conv.title,
                summary=summary,
                tags=conv.tags,
            )
        del self.conversations[conv_id]
        if self.active_conv_id == conv_id:
            self.active_conv_id = None
        return True

    def pin_conversation(self, conv_id: str, pinned: bool) -> bool:
        """置顶/取消置顶对话"""
        conv = self.conversations.get(conv_id)
        if not conv:
            return False
        conv.pinned = pinned
        return True

    def update_conversation_meta(
        self,
        conv_id: str,
        title: Optional[str] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> bool:
        """更新对话标题/标签/分类"""
        conv = self.conversations.get(conv_id)
        if not conv:
            return False
        if title is not None:
            conv.title = title
        if tags is not None:
            conv.tags = tags
        if category is not None:
            conv.category = category
        conv.updated_at = time.time()
        return True

    def list_conversations(
        self,
        category: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> List[Dict]:
        """
        返回对话列表（置顶的排最前，其余按更新时间倒序）
        可按 category / tag 过滤
        """
        convs = list(self.conversations.values())
        if category:
            convs = [c for c in convs if c.category == category]
        if tag:
            convs = [c for c in convs if tag in c.tags]
        # 置顶排前，同层按 updated_at 倒序
        convs.sort(key=lambda c: (-int(c.pinned), -c.updated_at))
        return [self._conv_to_list_item(c) for c in convs]

    def _conv_to_list_item(self, conv: IdeaConversation) -> Dict:
        """对话列表条目（不含完整消息列表，减少传输量）"""
        last_msg = conv.messages[-1] if conv.messages else None
        return {
            "conv_id": conv.conv_id,
            "title": conv.title,
            "tags": conv.tags,
            "pinned": conv.pinned,
            "category": conv.category,
            "current_phase": conv.current_phase,
            "phase_name": conv.get_phase_name(),
            "phase_completed": conv.phase_completed,
            "message_count": len(conv.messages),
            "last_message": last_msg.get("content", "")[:80] if last_msg else "",
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
            "is_active": conv.conv_id == self.active_conv_id,
            "has_requirements_doc": bool(conv.requirements_doc),
        }

    # ── 核心聊天方法 ──────────────────────────────────────────────────────────

    def chat(
        self,
        user_input: str,
        conv_id: Optional[str] = None,
        history: Optional[List[Dict]] = None,
        context_summary: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        与用户对话（在指定对话中）

        Args:
            user_input: 用户当前输入
            conv_id: 对话 ID，为空则使用当前激活对话；若无激活对话则自动创建
            history: 前端传来的最近消息（用于前端侧上下文管理）
            context_summary: 前端缓存的压缩摘要

        Returns:
            {
                reply: str,
                conv_id: str,
                current_phase: int,
                phase_name: str,
                phase_advanced: bool,   # 本次对话是否触发了阶段升级
                requirements_doc: str,  # 如果已生成需求文档
                summary: str,           # 新压缩摘要（供前端缓存）
            }
        """
        # 确定对话
        if conv_id and conv_id in self.conversations:
            conv = self.conversations[conv_id]
        elif self.active_conv_id and self.active_conv_id in self.conversations:
            conv = self.conversations[self.active_conv_id]
        else:
            # 自动创建新对话
            conv = self.new_conversation(title=user_input[:30] or "新对话")

        self.active_conv_id = conv.conv_id

        # 合并历史：优先使用前端传来的，否则用内部存储的
        hist = history or conv.messages
        new_summary = context_summary or conv.context_summary

        # 判断是否需要压缩历史
        if len(hist) > self.COMPRESS_THRESHOLD:
            to_compress = hist[:-self.KEEP_RECENT]
            recent_raw = hist[-self.KEEP_RECENT:]
            compressed = self._compress_history(to_compress)
            new_summary = (new_summary + "\n\n【新增摘要】\n" + compressed) if new_summary else compressed
        else:
            recent_raw = hist

        # 构建 system prompt（注入阶段 + 个人记忆）
        phase_context = self._build_phase_context(conv)
        user_memory_context = self.user_memory.get_context_for_prompt()

        system_content = IDEA_LANDING_SYSTEM_PROMPT
        if user_memory_context:
            system_content += f"\n\n【用户个人背景（供参考）】\n{user_memory_context}"
        system_content += f"\n\n【当前对话状态】\n{phase_context}"

        messages = [Message(role=MessageRole.SYSTEM, content=system_content)]

        # 注入压缩摘要
        if new_summary:
            messages.append(Message(
                role=MessageRole.SYSTEM,
                content=f"以下是之前对话的压缩摘要：\n\n{new_summary}",
            ))

        # 注入近期原文
        for h in recent_raw:
            role = MessageRole.USER if h.get("role") == "user" else MessageRole.ASSISTANT
            content = h.get("content", "")
            if content:
                messages.append(Message(role=role, content=content))

        # 当前用户输入
        messages.append(Message(role=MessageRole.USER, content=user_input))

        # 调用 LLM
        try:
            response = self.hermes.chat(messages)
            reply = response.get("content", "")
        except Exception as e:
            reply = ""

        if not reply:
            reply = self._fallback_reply(user_input, conv)

        # 检测阶段升级信号
        phase_advanced = False
        if "[阶段升级:" in reply or "[阶段升级：" in reply:
            old_phase = conv.current_phase
            if conv.advance_phase():
                phase_advanced = True
                # 如果升级到阶段 2，把标题更新为用户第一条消息
                if conv.current_phase == 2 and conv.messages:
                    first_user = next((m for m in conv.messages if m.get("role") == "user"), None)
                    if first_user:
                        conv.title = first_user.get("content", "新对话")[:40]

        # 如果阶段 2 完成，提取需求文档
        if conv.current_phase == 2 and "## 项目目标" in reply:
            conv.requirements_doc = reply
            # 把需求文档摘要写入个人记忆
            self.user_memory.add_idea_summary(
                conv_id=conv.conv_id,
                title=conv.title,
                summary=reply[:300],
                tags=conv.tags,
            )

        # 更新对话内部状态
        conv.messages.append({"role": "user", "content": user_input})
        conv.messages.append({"role": "assistant", "content": reply})
        if new_summary:
            conv.context_summary = new_summary
        # 内部消息只保留最近 KEEP_RECENT*2 条
        if len(conv.messages) > self.KEEP_RECENT * 2:
            conv.messages = conv.messages[-(self.KEEP_RECENT * 2):]
        conv.updated_at = time.time()

        return {
            "reply": reply,
            "conv_id": conv.conv_id,
            "current_phase": conv.current_phase,
            "phase_name": conv.get_phase_name(),
            "phase_advanced": phase_advanced,
            "requirements_doc": conv.requirements_doc,
            "summary": new_summary or "",
        }

    def _build_phase_context(self, conv: IdeaConversation) -> str:
        """构建当前阶段的上下文描述，注入到 system prompt"""
        pi = {
            1: "【当前阶段：压力测试】\\n你的核心任务是对用户的想法进行多维度质疑，用反问和竞品对比挑战想法，一次最多提2~3个问题。",
            2: "【当前阶段：需求文档生成】\\n将经过压力测试的想法转化为结构化需求文档，格式必须完整，内容必须具体，不得使用模糊词汇。",
            3: "【当前阶段：优化补充】\\n逐条讨论需求文档内容，追问边界条件和异常场景，最终输出可直接提交开发团队的完整文档。",
        }
        completed = [k for k, v in conv.phase_completed.items() if v]
        parts = [
            "===== 阶段状态 =====",
            pi.get(conv.current_phase, "【当前阶段：已完成】"),
            f"已完成阶段：{completed}",
        ]
        if conv.current_phase >= 2 and conv.requirements_doc:
            parts.append("需求文档：已生成（可在阶段3继续完善）")
        elif conv.current_phase >= 2 and not conv.requirements_doc:
            parts.append("提示：建议先生成需求文档")
        return "\\n".join(parts)

    # ── 当前阶段信息 ────────────────────────────────────────────────────────

    def get_current_phase(self, conv_id: str) -> dict:
        """返回指定对话的当前阶段详细信息"""
        conv = self.conversations.get(conv_id)
        if not conv:
            return {"success": False, "error": "对话不存在"}
        result = conv.get_current_phase()
        result["conv_id"] = conv_id
        result["success"] = True
        return result

    # ── 阶段推进（手动触发） ─────────────────────────────────────────────────

    def advance_phase(self, conv_id: str) -> Dict[str, Any]:
        """手动推进阶段（用户确认当前阶段完成）"""
        conv = self.conversations.get(conv_id)
        if not conv:
            return {"success": False, "error": "对话不存在"}
        old_phase = conv.current_phase
        if not conv.advance_phase():
            return {"success": False, "error": "已是最后阶段"}
        ptm = {
            (1, 2): "压力测试通过！已进入阶段2：需求文档生成。下面我将根据对话内容生成需求文档。",
            (2, 3): "需求文档已就绪！已进入阶段3：优化补充。我们来逐条讨论，补充细节、消除歧义。",
            (3, 4): "恭喜！三阶段工作流已全部完成。最终的需求文档可直接交付开发团队。",
        }
        prompt = ptm.get(
            (old_phase, conv.current_phase),
            f"已进入阶段 {conv.current_phase}：{conv.get_phase_name()}"
        )
        return {
            "success": True,
            "old_phase": old_phase,
            "new_phase": conv.current_phase,
            "phase_name": conv.get_phase_name(),
            "prompt": prompt,
            "phase_info": conv.get_current_phase(),
        }

    def generate_requirements_doc(self, conv_id: str) -> Dict[str, Any]:
        """
        为指定对话生成需求文档
        - 完整整合压力测试阶段的对话内容
        - 使用详细格式化模板，包含压测结论摘要
        """
        conv = self.conversations.get(conv_id)
        if not conv:
            return {"success": False, "error": "对话不存在"}

        # 阶段守卫：仅在阶段 2 及以上可生成
        if conv.current_phase < 2:
            return {"success": False, "error": "当前阶段不允许生成需求文档，请先完成压力测试并进入阶段 2"}

        # ── 分离压力测试阶段与后续对话 ─────────────────────────────────────
        all_messages = conv.messages
        # 尝试找到阶段升级分割点（阶段1→2 的消息位置）
        phase1_messages = []
        phase2_plus_messages = []
        phase_boundary_found = False
        for m in all_messages:
            content = m.get("content", "")
            if not phase_boundary_found and ("[阶段升级: 1→2]" in content or "[阶段升级：1→2]" in content):
                phase_boundary_found = True
                phase1_messages.append(m)  # 包含边界消息
                continue
            if phase_boundary_found:
                phase2_plus_messages.append(m)
            else:
                phase1_messages.append(m)

        # 如果没找到升级标记，把所有消息都当作压测内容
        if not phase_boundary_found:
            phase1_messages = all_messages

        # ── 整理压力测试对话摘要 ─────────────────────────────────────────────
        def _format_messages(msgs: List[Dict], max_chars: int = 3000) -> str:
            lines = []
            total = 0
            for m in msgs:
                role_label = "用户" if m.get("role") == "user" else "顾问"
                content = m.get("content", "").strip()
                if not content:
                    continue
                # 截断过长消息
                if len(content) > 400:
                    content = content[:400] + "..."
                line = f"【{role_label}】{content}"
                total += len(line)
                if total > max_chars:
                    lines.append("（以上为完整压测对话摘要，后续内容已省略）")
                    break
                lines.append(line)
            return "\n\n".join(lines)

        pressure_test_text = _format_messages(phase1_messages, max_chars=2500)
        followup_text = _format_messages(phase2_plus_messages, max_chars=1000) if phase2_plus_messages else ""

        # ── 提取关键假设与质疑点（从压测对话中找 Agent 的问题） ───────────────
        key_challenges = []
        for m in phase1_messages:
            if m.get("role") == "assistant":
                content = m.get("content", "")
                # 简单提取包含问号的句子作为关键质疑
                for sentence in content.split("？"):
                    s = sentence.strip()
                    if len(s) > 10 and len(s) < 150:
                        key_challenges.append(s + "？")
                        if len(key_challenges) >= 5:
                            break
            if len(key_challenges) >= 5:
                break

        challenges_text = "\n".join(f"  - {c}" for c in key_challenges[:5]) if key_challenges else "  （暂无）"

        # ── 上下文摘要补充 ────────────────────────────────────────────────────
        context_note = ""
        if conv.context_summary:
            context_note = f"\n\n【对话上下文摘要（供参考）】\n{conv.context_summary[:500]}"

        # ── 构建生成 prompt ───────────────────────────────────────────────────
        system_prompt = (
            "你是一位资深产品经理，擅长把经过严格压力测试的想法转化为可执行的需求文档。\n\n"
            "【任务】根据以下压力测试对话和后续讨论，生成一份完整、详细、可直接交付给开发团队的需求文档。\n\n"
            "【格式要求 — 必须严格遵守，每个章节都要有实质内容】\n\n"
            "## 项目目标\n"
            "（一句话描述，格式：「为了[目标用户]解决[核心问题]，通过[核心功能]实现[可量化的成果]」）\n\n"
            "## 背景与问题陈述\n"
            "（2-3段：当前痛点是什么，现有方案有什么不足，这个想法如何填补空白）\n\n"
            "## 目标用户\n"
            "（用户画像：年龄/职业/使用场景/核心痛点/技术水平，列出主要用户群和次要用户群）\n\n"
            "## 用户故事\n"
            "（至少4条，格式：「作为[角色]，我希望[功能]，以便[价值]」，附上优先级 P0/P1/P2）\n\n"
            "## 核心功能清单（P0/P1/P2）\n"
            "（按优先级分层列出，P0=MVP必须，P1=重要但可延后，P2=nice-to-have；每项功能附上1-2句描述）\n\n"
            "## 非功能需求\n"
            "（性能：如响应时间、并发量；安全：认证方式；可用性：浏览器/平台兼容性；可维护性等）\n\n"
            "## 约束条件\n"
            "（技术约束：技术栈/平台限制；时间约束：上线时间节点；预算约束；团队规模约束）\n\n"
            "## 验收标准\n"
            "（每个P0功能至少1条验收标准，格式：「给定[条件]，当[操作]，则[可观测结果]」）\n\n"
            "## 压力测试结论摘要\n"
            "（总结压测阶段中发现的核心假设、主要风险点，以及已通过验证的关键决策）\n\n"
            "## 不做什么（Out of Scope）\n"
            "（明确列出本版本不会做的功能，以及不做的理由，防止范围蔓延）\n\n"
            "## 开放问题\n"
            "（尚未决策的问题，标注决策负责人和预计决策时间）\n\n"
            "【写作要求】\n"
            "- 内容必须具体，不得使用「根据需求」「适当」「合理」等模糊词汇\n"
            "- 验收标准必须可测试、可量化\n"
            "- 根据压测对话内容推断并填充合理的细节，不要留空\n"
            "- 如果某项信息在对话中未明确，用 [待确认] 标注并给出建议\n"
        )

        user_content = f"【压力测试对话记录】\n{pressure_test_text}"
        if followup_text:
            user_content += f"\n\n【后续补充讨论】\n{followup_text}"
        user_content += f"\n\n【压测阶段主要质疑点（供参考）】\n{challenges_text}"
        if context_note:
            user_content += context_note
        user_content += "\n\n请基于以上全部内容生成完整需求文档："

        prompt = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(role=MessageRole.USER, content=user_content),
        ]

        try:
            response = self.hermes.chat(prompt)
            doc = response.get("content", "")
        except Exception:
            doc = ""

        if not doc:
            # 离线 fallback：生成结构化占位文档
            doc = self._generate_fallback_doc(conv, pressure_test_text, key_challenges)

        conv.requirements_doc = doc
        conv.updated_at = time.time()

        # 把需求文档摘要写入个人记忆
        self.user_memory.add_idea_summary(
            conv_id=conv.conv_id,
            title=conv.title,
            summary=doc[:300],
            tags=conv.tags,
        )

        return {"success": True, "requirements_doc": doc, "conv_id": conv_id}

    def _generate_fallback_doc(self, conv: "IdeaConversation", pressure_test_text: str, key_challenges: List[str]) -> str:
        """LLM 不可用时生成结构化占位需求文档"""
        challenges_md = "\n".join(f"- {c}" for c in key_challenges) if key_challenges else "- （请配置 API Key 后自动生成）"
        # 从第一条用户消息提取想法描述
        first_idea = ""
        for m in conv.messages:
            if m.get("role") == "user":
                first_idea = m.get("content", "")[:200]
                break

        return f"""## 项目目标

[待确认] 为 [目标用户] 解决 [{first_idea[:50] or '核心问题'}]，通过核心功能实现可量化成果。

## 背景与问题陈述

用户提出的核心想法：{first_idea}

（⚠️ 未配置 LLM API Key，以下内容为占位结构，请配置后重新生成完整文档）

## 目标用户

- 主要用户群：[待确认]
- 核心痛点：[待确认]
- 技术水平：[待确认]

## 用户故事

- P0：作为 [角色]，我希望 [功能]，以便 [价值]
- P0：作为 [角色]，我希望 [功能]，以便 [价值]
- P1：作为 [角色]，我希望 [功能]，以便 [价值]

## 核心功能清单（P0/P1/P2）

**P0 — MVP 必须**
- [待确认]

**P1 — 重要**
- [待确认]

**P2 — 可选**
- [待确认]

## 非功能需求

- 性能：[待确认]
- 安全：[待确认]
- 兼容性：[待确认]

## 约束条件

- 技术约束：[待确认]
- 时间约束：[待确认]

## 验收标准

- 给定 [条件]，当 [操作]，则 [可观测结果]

## 压力测试结论摘要

**主要质疑点（来自压测阶段）：**
{challenges_md}

## 不做什么（Out of Scope）

- [待确认]

## 开放问题

- [待确认] — 负责人：[待确认] — 决策时间：[待确认]
"""

    # ── 用户记忆管理 ──────────────────────────────────────────────────────────

    def update_user_memory(self, background: str = "", preferences: Optional[Dict] = None) -> bool:
        """更新用户个人记忆"""
        if background:
            self.user_memory.background = background
        if preferences:
            self.user_memory.preferences.update(preferences)
        self.user_memory.updated_at = time.time()
        return True

    # ── 持久化 ────────────────────────────────────────────────────────────────

    def to_persist(self) -> Dict:
        return {
            "agent_id": self.agent_id,
            "active_conv_id": self.active_conv_id,
            "conversations": {cid: c.to_dict() for cid, c in self.conversations.items()},
            "user_memory": self.user_memory.to_dict(),
        }

    def from_persist(self, data: Dict) -> None:
        self.agent_id = data.get("agent_id", self.agent_id)
        self.active_conv_id = data.get("active_conv_id")
        raw_convs = data.get("conversations", {})
        self.conversations = {
            cid: IdeaConversation.from_dict(cdata)
            for cid, cdata in raw_convs.items()
        }
        raw_mem = data.get("user_memory")
        if raw_mem:
            self.user_memory = UserMemory.from_dict(raw_mem)

    # ── AgentBase 必须实现 ────────────────────────────────────────────────────

    def _do_execute(self, task: Task) -> Any:
        if task.title == "chat":
            return self.chat(
                user_input=task.description,
                conv_id=task.metadata.get("conv_id"),
            )
        elif task.title == "new_conversation":
            conv = self.new_conversation(
                title=task.metadata.get("title", "新对话"),
                tags=task.metadata.get("tags", []),
                category=task.metadata.get("category", "默认"),
            )
            return {"conv_id": conv.conv_id, "title": conv.title}
        elif task.title == "generate_requirements_doc":
            return self.generate_requirements_doc(task.metadata.get("conv_id", ""))
        else:
            return {"error": f"未知任务: {task.title}"}

    def get_status(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "type": "idea_landing",
            "state": self.state.value,
            "conversations_count": len(self.conversations),
            "active_conv_id": self.active_conv_id,
            "user_memory_background": bool(self.user_memory.background),
        }
