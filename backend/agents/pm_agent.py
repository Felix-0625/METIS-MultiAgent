"""
PM Agent 实现
项目经理 Agent，负责需求分析和项目规划
"""

import time
import logging
from typing import Dict, List, Optional, Any
from .base.hermes_agent import AgentBase, AgentType, Task, AgentState
from core.hermes_client import Message, MessageRole
from core.expert_pool import AGENT_CORE_PRINCIPLES
from core.project_contract import attach_contract, validate_plan
from core.json_utils import extract_first_json_object

logger = logging.getLogger(__name__)


class PMAgent(AgentBase):
    """
    PM Agent
    
    职责：
    - 需求分析
    - 方案设计
    - 流程建模
    - RACI 矩阵
    - 逐项目与用户确认
    """

    # 基本能力极值
    ESSENTIAL_CAPABILITIES = [
        "需求分析",
        "方案设计",
        "流程建模",
        "RACI矩阵",
        "逐项目确认"
    ]

    def __init__(self, *args, **kwargs):
        self.current_plan: Optional[Dict] = None
        self.subprojects: List[Dict] = []
        self._capabilities = list(self.ESSENTIAL_CAPABILITIES)
        # 对话历史（用于多轮上下文记忆）
        self.conversation_history: List[Dict] = []
        # 压缩后的上下文摘要
        self.context_summary: str = ""
        self.project_contract: Dict[str, Any] = {}
        super().__init__(*args, agent_type=AgentType.PM, **kwargs)

    # 压缩阈值：超过 N 条原始消息时触发摘要压缩
    COMPRESS_THRESHOLD = 10   # 5 轮对话后压缩
    KEEP_RECENT = 6           # 压缩后保留最近 6 条原文（3 轮）

    def analyze_requirement(
        self,
        user_input: str,
        history: Optional[List[Dict]] = None,
        context_summary: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        需求分析（支持多轮对话上下文 + 自动压缩历史）
        
        Args:
            user_input: 当前用户输入
            history: 前端传来的历史消息列表 [{"role": "user"/"assistant", "content": "..."}]
            context_summary: 前端传来的已压缩摘要（如果有）
        
        Returns:
            dict with keys: analysis, success, summary (新摘要，供前端缓存)
        """
        system_prompt = f"""{AGENT_CORE_PRINCIPLES}

你是一个经验丰富的项目经理（PM Agent）。你负责需求分析和项目规划。

【角色职责】
- 需求分析：深度理解用户意图，拆解为可执行的功能点
- 方案设计：给出完整、可落地的技术方案
- 子项目规划：将项目拆分为独立可交付的子项目
- 团队协调：明确各角色职责（RACI 矩阵）

【工作流程 — 严格遵守】
收到需求时，先在内部完成以下三步确认（不明确直接提问）：
1. 执行什么具体任务（需求的核心是什么）
2. 对象是谁（用户群体、使用场景）
3. 执行标准是什么（验收条件、质量要求）

在对话中：
1. 记住之前讨论过的所有需求和决策（已在上下文摘要中提供）
2. 基于上下文给出连贯的回答，不重复已确认的内容
3. 主动追问不清晰的需求点，不猜测
4. 需求明确后，提供结构化分析结果

【输出规范】
- 当用户描述了项目需求时，必须在回复末尾给出结构化摘要：
  **项目类型**：xxx
  **核心功能**：功能1、功能2、功能3
  **技术约束**：xxx（如无则写"无特殊约束"）
  **建议技术栈**：前端/后端/数据库
- 当用户要求生成规划时，必须输出包含 phases 和 subprojects 的 JSON 结构
- 不要只给建议，要给出可执行的具体方案
- 不确定的内容直接说不确定，不用模糊答案充数
【项目管理技能框架 — Senior PM + Scrum Master】

工作方式：
- 需求确认优先：收到任务先明确「做什么/对谁做/验收标准」，不明确直接提问
- 规划先于执行：内部完整规划后再输出，不边想边写
- 量化驱动：用数据说话，风险用概率×影响量化，进度用完成率表达

项目规划规范：
1. 项目健康度评估：范围/时间/成本/质量/风险五维评分（0-100）
2. 风险分析：识别风险→EMV量化（概率×影响）→制定应对策略（规避/转移/缓解/接受）
3. 资源规划：按角色分配工作量，识别关键路径，标记资源冲突
4. 里程碑设计：每个阶段有明确的可交付物和验收标准
5. WSJF优先级：（业务价值+时间紧迫度+风险降低）÷ 工作量

Sprint 管理规范：
1. Sprint 健康度：速度趋势/承诺完成率/阻塞率/技术债比例
2. 速度预测：基于历史3个Sprint的蒙特卡洛模拟，给出置信区间
3. 回顾会议：识别「做得好/待改进/行动项」，追踪上次行动项完成率
4. 阻塞处理：阻塞超过2小时必须上报，超过4小时触发 CCB

输出规范：
- 项目规划：JSON格式（phases/subprojects/dependencies/risks）
- 状态报告：结构化摘要（健康度评分+关键风险+下一步行动）
- 不确定的内容直接说不确定，不用模糊答案充数"""

        hist = history or self.conversation_history
        new_summary = context_summary or self.context_summary

        # 判断是否需要压缩：历史条数超过阈值
        if len(hist) > self.COMPRESS_THRESHOLD:
            # 压缩较旧的部分，保留最近 KEEP_RECENT 条原文
            to_compress = hist[:-self.KEEP_RECENT]
            recent_raw = hist[-self.KEEP_RECENT:]
            compressed = self._compress_history(to_compress)
            # 合并旧摘要 + 新压缩内容
            if new_summary:
                new_summary = new_summary + "\n\n【新增摘要】\n" + compressed
            else:
                new_summary = compressed
        else:
            recent_raw = hist

        # 构建消息列表
        messages = [Message(role=MessageRole.SYSTEM, content=system_prompt)]

        # 注入摘要（作为 system 消息的补充）
        if new_summary:
            messages.append(Message(
                role=MessageRole.SYSTEM,
                content=f"以下是之前对话的压缩摘要，请基于此保持上下文连贯：\n\n{new_summary}"
            ))

        # 注入最近几轮原文
        for h in recent_raw:
            role = MessageRole.USER if h.get("role") == "user" else MessageRole.ASSISTANT
            content = h.get("content", "")
            if content:
                messages.append(Message(role=role, content=content))

        # 当前用户输入
        messages.append(Message(role=MessageRole.USER, content=user_input))

        try:
            response = self.hermes.chat(messages)
            reply = response.get("content", "")
        except Exception as e:
            reply = ""

        # 无 API Key 或 LLM 调用失败时，返回结构化 fallback（让流程继续）
        if not reply:
            reply = self._fallback_reply(user_input)

        # 更新内部状态
        self.conversation_history.append({"role": "user", "content": user_input})
        self.conversation_history.append({"role": "assistant", "content": reply})
        if new_summary:
            self.context_summary = new_summary
        # 内部历史只保留最近 KEEP_RECENT 条
        if len(self.conversation_history) > self.KEEP_RECENT * 2:
            self.conversation_history = self.conversation_history[-(self.KEEP_RECENT * 2):]

        return {
            "reply": reply,       # 前端/测试期望的字段名
            "analysis": reply,    # 兼容旧字段名
            "success": True,
            "summary": new_summary or "",   # 返回给前端缓存，下次带回来
        }

    def _fallback_reply(self, user_input: str) -> str:
        """
        无 API Key 时的 fallback 回复（让流程可以继续测试）
        """
        return (
            f"【PM Agent 离线模式】\n\n"
            f"已收到您的需求：「{user_input[:200]}」\n\n"
            f"📋 **初步分析**\n"
            f"- 项目类型：Web 应用系统\n"
            f"- 核心功能：根据描述自动识别\n"
            f"- 建议技术栈：React + FastAPI + PostgreSQL\n\n"
            f"⚠️ 当前未配置 API Key，使用离线模式。\n"
            f"请在「设置」页面配置 LLM API Key 后，PM Agent 将提供完整的智能需求分析。\n\n"
            f"您可以继续点击「生成规划书」，系统将基于您的描述自动生成子项目列表。"
        )

    def design_solution(self, requirements: Dict) -> Dict[str, Any]:
        """
        方案设计
        
        根据需求设计整体解决方案，返回结构化 modules 列表供 create_subproject_list 使用
        """
        system_prompt = """基于以下需求，设计完整的解决方案。

请严格按照以下 JSON 格式输出（不要有其他文字）：
{
  "overview": "整体方案描述",
  "architecture": "系统架构说明",
  "tech_stack": ["技术1", "技术2"],
  "mermaid_flow": "stateDiagram-v2\\n  [*] --> 规划\\n  规划 --> 执行",
  "modules": [
    {
      "name": "模块名称",
      "description": "模块描述",
      "requirements": ["需求1", "需求2"],
      "tech_stack": ["React", "TypeScript"],
      "priority": "high"
    }
  ]
}

要求：
1. modules 数组必须包含 2-6 个子模块
2. 每个模块的 tech_stack 要具体（如 React、FastAPI、PostgreSQL）
3. mermaid_flow 使用 stateDiagram-v2 格式
4. priority 取值：high / normal / low"""

        messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(role=MessageRole.USER, content=str(requirements))
        ]

        # 方案设计必须与需求分析保持一致：未配置 LLM 时返回可用的离线草案，
        # 而不是把 ValueError 冒泡成 500，阻断用户的项目流程。
        try:
            response = self.hermes.chat(messages)
            content = response.get("content", "")
        except Exception as exc:
            logger.warning("方案设计进入离线模式: %s", exc)
            content = (
                f"已收到需求：{requirements}\n"
                "当前未配置 API Key，已生成基础离线方案；配置 LLM 后可重新生成完整方案。"
            )

        # 健壮解析：raw_decode 逐位扫描，兼容 markdown 包裹/尾随文字
        solution_data = extract_first_json_object(content)

        # fail-fast：解析失败不再注入假模块（会污染下游规划）。返回明确错误，
        # 让调用方决定重试或报错，而非把占位结构当真实方案消费。
        if not solution_data or not solution_data.get("modules"):
            return {
                "success": False,
                "error": "design_solution_failed",
                "message": (
                    "方案设计未能解析出合法结构化结果。"
                    "请检查 LLM 配置或重试，不要将占位方案当作真实规划消费。"
                ),
                "raw_content_prefix": (content or "")[:200],
            }

        # Keep the legacy PM path behind the same deterministic boundary as
        # PMLeaderAgent; otherwise /design could bypass the confirmed plan.
        requirement_text = requirements.get("requirements", "") if isinstance(requirements, dict) else str(requirements)
        wrapped_plan = attach_contract({
            "tech_stack": solution_data.get("tech_stack", []),
            "phases": solution_data.get("phases") or [{"phase_id": "legacy", "description": solution_data.get("overview", "")}],
            "subprojects": solution_data.get("modules", []),
        }, requirement_text)
        violations = validate_plan(wrapped_plan, wrapped_plan.get("project_contract"))
        if violations:
            self.project_contract = wrapped_plan.get("project_contract", {})
            return {"solution": solution_data, "success": False, "status": "plan_contract_violation", "violations": violations}
        self.project_contract = wrapped_plan.get("project_contract", {})
        solution_data["project_contract"] = self.project_contract
        return {"solution": solution_data, "success": True}

    def build_raci_matrix(self, subprojects: List[Dict]) -> Dict[str, Any]:
        """
        构建 RACI 矩阵
        
        定义各角色的职责
        """
        # 角色列表
        roles = ["PM", "HR", "SM", "PG", "Lead", "Employee", "QA", "Supervisor"]

        raci_options = ["R", "A", "C", "I", "-"]
        matrix = []

        for subproject in subprojects:
            row = {
                "subproject": subproject.get("name", ""),
                "responsibilities": {}
            }
            for role in roles:
                # 简化逻辑，实际应基于规则生成
                if role == "PM":
                    row["responsibilities"][role] = "A"
                elif role == "Lead":
                    row["responsibilities"][role] = "R"
                elif role == "Employee":
                    row["responsibilities"][role] = "R"
                elif role == "QA":
                    row["responsibilities"][role] = "C"
                else:
                    row["responsibilities"][role] = "I"

            matrix.append(row)

        return {
            "matrix": matrix,
            "roles": roles,
            "success": True
        }

    def create_subproject_list(self, solution: Dict) -> List[Dict]:
        """
        创建子项目清单
        
        将解决方案拆解为可管理的子项目。
        solution 可以是 design_solution() 返回的完整 dict（含 solution 键），
        也可以是直接的 modules 列表容器。
        """
        subprojects = []

        # 兼容 design_solution() 返回的 {"solution": {...}, "success": True} 结构
        if "solution" in solution and isinstance(solution["solution"], dict):
            modules = solution["solution"].get("modules", [])
        else:
            modules = solution.get("modules", [])

        for i, module in enumerate(modules):
            subproject = {
                "id": f"SP-{i+1:03d}",
                "name": module.get("name", f"子项目 {i+1}"),
                "description": module.get("description", ""),
                "status": "pending",  # pending -> confirmed
                "requirements": module.get("requirements", []),
                "tech_stack": module.get("tech_stack", []),
                "roles_needed": self._infer_roles(module.get("tech_stack", [])),
                "priority": module.get("priority", "normal")
            }
            subprojects.append(subproject)

        self.subprojects = subprojects
        return subprojects

    def confirm_subproject(self, subproject_id: str, confirmed: bool, modifications: str = "") -> Dict:
        """
        确认子项目
        
        用户确认或修改子项目
        """
        for sp in self.subprojects:
            if sp["id"] == subproject_id:
                if confirmed:
                    sp["status"] = "confirmed"
                    sp["confirmed_at"] = time.time()
                else:
                    sp["status"] = "modified"
                    sp["modifications"] = modifications
                return {"success": True, "subproject": sp}

        return {"success": False, "error": "Subproject not found"}

    def generate_plan(self) -> Dict[str, Any]:
        """
        生成最终规划书
        
        所有子项目确认后，生成完整规划书。
        若子项目为空，自动从对话历史中提取或生成 fallback 子项目。
        """
        # 若没有子项目，尝试从对话历史中提取
        if not self.subprojects:
            self.subprojects = self._generate_fallback_subprojects()

        # 仅当用户已确认或修改时才允许生成规划书
        unconfirmed = [sp for sp in self.subprojects if sp.get("status") not in ("confirmed", "modified")]
        if unconfirmed:
            return {
                "success": False,
                "error": f"还有 {len(unconfirmed)} 个子项目未确认",
                "unconfirmed": [sp["id"] for sp in unconfirmed]
            }

        # 生成规划书
        raci = self.build_raci_matrix(self.subprojects)

        plan = {
            "project_id": self.agent_id,
            "status": "approved",
            "subprojects": self.subprojects,
            "raci_matrix": raci.get("matrix", []),
            "roles": raci.get("roles", []),
            "team_requirements": self._extract_team_requirements(),
            "generated_at": time.time()
        }

        self.current_plan = plan
        return {"success": True, "plan": plan}

    def _extract_team_requirements(self) -> List[Dict]:
        """提取团队需求"""
        requirements = []

        # 分析各子项目的技术栈和复杂度
        for sp in self.subprojects:
            tech_stack = sp.get("tech_stack", [])
            # 简化：每个子项目需要 1-3 人
            size = min(3, max(1, len(tech_stack) // 2))

            requirements.append({
                "subproject_id": sp["id"],
                "roles_needed": self._infer_roles(tech_stack),
                "team_size": size,
                "skills_required": tech_stack
            })

        return requirements

    def _generate_fallback_subprojects(self) -> List[Dict]:
        """
        从对话历史中提取子项目，或生成基于项目描述的 fallback 子项目。
        无 API Key 时直接用关键词解析对话历史。
        """
        # 尝试从对话历史中提取关键词
        all_text = " ".join(
            h.get("content", "") for h in self.conversation_history
        )

        # 关键词 → 子项目映射
        keyword_map = [
            (["登录", "注册", "用户", "认证", "auth"], "用户认证模块", ["用户登录", "用户注册", "密码重置"], ["FastAPI", "JWT"]),
            (["商品", "产品", "展示", "搜索", "sku"], "商品管理模块", ["商品列表", "商品详情", "搜索过滤"], ["React", "Elasticsearch"]),
            (["购物车", "订单", "支付", "结算"], "订单支付模块", ["购物车", "下单", "支付集成"], ["FastAPI", "Stripe"]),
            (["后台", "管理", "admin", "dashboard"], "管理后台模块", ["数据统计", "用户管理", "内容管理"], ["React", "Ant Design"]),
            (["部署", "运维", "docker", "k8s", "ci"], "DevOps 模块", ["容器化", "CI/CD", "监控"], ["Docker", "GitHub Actions"]),
        ]

        matched = []
        for keywords, name, reqs, tech in keyword_map:
            if any(kw in all_text for kw in keywords):
                matched.append((name, reqs, tech))

        if not matched:
            # 完全没有历史，生成通用子项目
            matched = [
                ("核心功能模块", ["主要业务逻辑", "数据处理"], ["Python", "FastAPI"]),
                ("前端界面模块", ["用户界面", "交互设计"], ["React", "TypeScript"]),
            ]

        result = []
        for i, (name, reqs, tech) in enumerate(matched):
            result.append({
                "id": f"SP-{i+1:03d}",
                "name": name,
                "description": f"负责{name}的开发与实现",
                "status": "pending",
                "requirements": reqs,
                "tech_stack": tech,
                "roles_needed": self._infer_roles(tech),
                "priority": "high" if i == 0 else "normal",
            })
        return result

    def _infer_roles(self, tech_stack: List[str]) -> List[str]:
        """根据技术栈推断需要的角色"""
        roles = []

        for tech in tech_stack:
            tech_lower = tech.lower()
            if "frontend" in tech_lower or "react" in tech_lower or "vue" in tech_lower:
                if "前端开发" not in roles:
                    roles.append("前端开发")
            if "backend" in tech_lower or "api" in tech_lower or "node" in tech_lower:
                if "后端开发" not in roles:
                    roles.append("后端开发")
            if "database" in tech_lower or "sql" in tech_lower:
                if "数据库开发" not in roles:
                    roles.append("数据库开发")
            if "security" in tech_lower or "crypto" in tech_lower:
                if "安全工程师" not in roles:
                    roles.append("安全工程师")
            if "devops" in tech_lower or "docker" in tech_lower or "k8s" in tech_lower:
                if "DevOps工程师" not in roles:
                    roles.append("DevOps工程师")

        if not roles:
            roles.append("开发工程师")

        return roles

    def _do_execute(self, task: Task) -> Any:
        """执行任务"""
        if task.title == "analyze_requirement":
            return self.analyze_requirement(task.description)
        elif task.title == "design_solution":
            return self.design_solution(task.metadata.get("requirements", {}))
        elif task.title == "confirm_subproject":
            return self.confirm_subproject(
                task.metadata.get("subproject_id", ""),
                task.metadata.get("confirmed", False),
                task.metadata.get("modifications", "")
            )
        elif task.title == "generate_plan":
            return self.generate_plan()
        else:
            return {"error": f"Unknown task: {task.title}"}

    def get_status(self) -> Dict[str, Any]:
        """获取 PM Agent 状态"""
        return {
            "agent_id": self.agent_id,
            "type": "pm",
            "state": self.state.value,
            "subprojects_count": len(self.subprojects),
            "confirmed_count": len([sp for sp in self.subprojects if sp.get("status") == "confirmed"]),
            "plan_ready": self.current_plan is not None
        }
