"""
CCB Agent 实现
变更控制委员会 Agent，负责仲裁变更和争议
"""

import time
from typing import Dict, List, Optional, Any
from .base.hermes_agent import AgentBase, AgentType, Task, AgentState
from core.json_utils import extract_first_json_object


class DecisionType:
    """决策选项"""
    FIX = "修"      # 接受变更，打回修改
    RESCHEDULE = "排期"  # 接受变更，放入下一版本
    REJECT = "拒绝"  # 拒绝变更，维持原方案


class TriggerType:
    """触发条件"""
    REQUIREMENT_CHANGE = "需求变更"
    ARCHITECTURE_ADJUSTMENT = "架构调整"
    BLOCK_TIMEOUT = "阻塞超时_2h"
    QUALITY_DISPUTE = "质检争议"


CCB_SKILL_FRAMEWORK = """【变更控制技能框架 — Change Control Board】

变更评估框架：
1. 影响分析：变更影响范围（功能/接口/数据/依赖）→受影响的模块和团队
2. 风险评估：变更引入的新风险（技术风险/进度风险/质量风险）
3. 成本评估：实现变更的工作量估算 vs 不变更的代价
4. 优先级判断：紧急程度（阻塞/高/中/低）× 业务价值

决策规范：
- 修（FIX）：变更合理且紧急，接受变更，打回相关任务重做，更新规格文档
- 排期（RESCHEDULE）：变更合理但不紧急，放入下一迭代，记录需求变更日志
- 拒绝（REJECT）：变更不合理或代价过高，维持原方案，说明拒绝理由

触发条件处理：
- 需求变更：评估影响范围，决定是否接受，更新 RACI 矩阵
- 架构调整：必须有 ADR（架构决策记录），评估技术债务影响
- 阻塞超时（>2h）：分析阻塞原因，决定资源调配或方案调整
- 质检争议：基于五维质检框架做最终裁决

输出规范：
- 每次决策必须记录：触发原因/影响分析/决策结果/执行指令
- 决策不可逆时必须说明回滚方案
- 不确定时说不确定，不用模糊答案充数"""


class CCBAgent(AgentBase):
    """
    CCB Agent（变更控制委员会）
    
    职责：
    - 变更影响分析
    - 风险评估
    - 决策建议
    - 冲突调解
    """

    # 基本能力极值
    ESSENTIAL_CAPABILITIES = [
        "变更影响分析",
        "风险评估",
        "决策建议",
        "冲突调解"
    ]

    def __init__(self, *args, **kwargs):
        self.pending_decisions: List[Dict] = []
        self.decision_history: List[Dict] = []
        self._capabilities = list(self.ESSENTIAL_CAPABILITIES)
        # 待确认的删除请求（专家正在项目中时需要用户二次确认）
        self._pending_delete_requests: Dict[str, Dict] = {}
        super().__init__(*args, agent_type=AgentType.CCB, **kwargs)

    # ── 人员保护逻辑 ──────────────────────────────────────────────────────────

    def check_delete_member(
        self,
        team_type: str,
        member_id: str,
        member_name: str,
        is_leader: bool,
        current_count: int,
        is_in_project: bool = False,
    ) -> Dict[str, Any]:
        """
        CCB 人员删除保护检查。

        规则：
        1. 组长不允许删除（团队必须有组长）
        2. 成员数不得少于4（删除后剩余 < 4 则拒绝）
        3. 专家正在项目中时，禁止删除（需用户强制确认）

        Args:
            team_type: 团队类型（pm / supervisor / expert）
            member_id: 成员 ID
            member_name: 成员名称
            is_leader: 是否是组长
            current_count: 当前成员总数（不含组长）
            is_in_project: 专家是否正在参与项目

        Returns:
            {
                "allowed": bool,
                "require_confirm": bool,  # 需要用户二次确认
                "confirm_token": str,     # 确认令牌（用于二次确认）
                "message": str,
                "reason": str,
            }
        """
        import uuid as _uuid

        # 规则1：组长不可删除
        if is_leader:
            return {
                "allowed": False,
                "require_confirm": False,
                "confirm_token": "",
                "message": f"❌ 无法删除：{member_name} 是团队组长，组长不允许删除",
                "reason": "leader_protected",
            }


        # 规则2：成员数保护（按团队类型设定最低人数）
        _MIN_COUNTS = {"pm_team": 3, "supervisor_team": 3}
        _min_count = _MIN_COUNTS.get(team_type, 1)
        if current_count <= _min_count:
            return {
                "allowed": False,
                "require_confirm": False,
                "confirm_token": "",
                "message": f"❌ 无法删除：{team_type}团队成员数不得少于{_min_count}人（当前 {current_count} 人）",
                "reason": "min_count_protected",
            }


        # 规则3：专家正在项目中，需要用户二次确认
        if is_in_project:
            token = _uuid.uuid4().hex
            self._pending_delete_requests[token] = {
                "team_type": team_type,
                "member_id": member_id,
                "member_name": member_name,
                "created_at": __import__("time").time(),
            }
            return {
                "allowed": False,
                "require_confirm": True,
                "confirm_token": token,
                "message": (
                    f"⚠️ {member_name} 当前正在参与项目，删除可能影响进行中的任务。\n"
                    "确认要删除吗？（此操作不可撤销）"
                ),
                "reason": "in_project_confirm_required",
            }

        # 通过所有检查，允许删除
        return {
            "allowed": True,
            "require_confirm": False,
            "confirm_token": "",
            "message": f"✅ 允许删除 {member_name}",
            "reason": "ok",
        }

    def confirm_delete_member(self, confirm_token: str) -> Dict[str, Any]:
        """
        用户二次确认删除（专家在项目中时的强制删除）。

        Returns:
            {"allowed": True, "member_id": ..., "member_name": ...} 或 {"allowed": False, "message": ...}
        """
        req = self._pending_delete_requests.pop(confirm_token, None)
        if not req:
            return {"allowed": False, "message": "确认令牌无效或已过期"}
        # 检查令牌是否超时（5分钟）
        if __import__("time").time() - req["created_at"] > 300:
            return {"allowed": False, "message": "确认令牌已过期，请重新发起删除请求"}
        return {
            "allowed": True,
            "member_id": req["member_id"],
            "member_name": req["member_name"],
            "team_type": req["team_type"],
            "message": f"✅ 已确认强制删除 {req['member_name']}",
        }

    def check_delete_expert(
        self,
        expert_id: str,
        expert_name: str,
        is_in_project: bool,
        current_project_name: str = "",
        current_count: int = 0,
    ) -> Dict[str, Any]:
        """
        专家删除保护（专家池中的专家）。

        规则：
        - 专家正在项目中：禁止删除，需用户确认
        - 专家空闲：直接允许删除（但给出提示）
        """
        import uuid as _uuid

        # 规则1：专家池最少保留 1 人
        if current_count <= 1:
            return {
                "allowed": False,
                "require_confirm": False,
                "confirm_token": "",
                "message": f"❌ 无法删除：专家池至少需要保留 1 名专家（当前 {current_count} 人）",
                "reason": "min_count_protected",
            }

        if is_in_project:
            token = _uuid.uuid4().hex
            self._pending_delete_requests[token] = {
                "team_type": "expert",
                "member_id": expert_id,
                "member_name": expert_name,
                "created_at": __import__("time").time(),
            }
            project_hint = f"（当前项目：{current_project_name}）" if current_project_name else ""
            return {
                "allowed": False,
                "require_confirm": True,
                "confirm_token": token,
                "message": (
                    f"⚠️ 专家【{expert_name}】正在参与项目{project_hint}，\n"
                    "删除后该专家将无法继续完成当前任务。\n"
                    "确认要删除吗？"
                ),
                "reason": "expert_in_project",
            }

        return {
            "allowed": True,
            "require_confirm": False,
            "confirm_token": "",
            "message": f"✅ 允许删除专家 {expert_name}",
            "reason": "ok",
        }

    # ── 变更分析（原有逻辑保留）──────────────────────────────────────────────

    def analyze_impact(self, change: Dict) -> Dict[str, Any]:
        """
        分析变更影响（优先调用 LLM，失败时降级为规则引擎）
        """
        # 尝试用 LLM 做智能分析
        try:
            from core.hermes_client import Message, MessageRole, chat_for_json
            system_content = (
                "你是变更控制委员会（CCB）的影响分析专家。\n"
                "请分析以下变更对项目的影响，输出严格 JSON：\n"
                '{"schedule_impact":"low/medium/high","resource_impact":"low/medium/high",'
                '"risk_level":"low/medium/high","affected_modules":["模块1"],'
                '"rollback_difficulty":"low/medium/high","summary":"一句话总结"}'
            )
            # 注入 CCB skill 框架到 system 消息（之前误把字符串拼到 messages 列表上）
            if CCB_SKILL_FRAMEWORK and CCB_SKILL_FRAMEWORK not in system_content:
                system_content = CCB_SKILL_FRAMEWORK + "\n\n" + system_content
            prompt = [
                Message(role=MessageRole.SYSTEM, content=system_content),
                Message(role=MessageRole.USER, content=(
                    f"变更类型：{change.get('type', '未知')}\n"
                    f"变更描述：{change.get('description', '')}\n"
                    f"受影响子项目：{change.get('affected_subprojects', [])}\n"
                    f"安全相关：{change.get('security_related', False)}"
                )),
            ]
            resp = chat_for_json(self.hermes, prompt)
            content = resp.get("content", "")
            if content and not content.startswith("⚠️") and not content.startswith("❌"):
                parsed = extract_first_json_object(content)
                if parsed:
                    return parsed
        except Exception:
            pass

        # 降级：规则引擎
        impact = {
            "schedule_impact": "medium",
            "resource_impact": "medium",
            "risk_level": "medium",
            "affected_modules": change.get("affected_subprojects", []),
            "rollback_difficulty": "medium",
            "summary": "规则引擎分析（LLM 不可用）",
        }
        change_type = change.get("type", "")
        if change_type == "requirement":
            impact["schedule_impact"] = "high"
        elif change_type == "architecture":
            impact["resource_impact"] = "high"
            impact["rollback_difficulty"] = "high"
        if change.get("security_related"):
            impact["risk_level"] = "high"
        return impact

    def assess_risk(self, change: Dict) -> Dict[str, Any]:
        """
        风险评估
        
        Args:
            change: 变更内容
            
        Returns:
            风险评估报告
        """
        risks = []

        # 进度风险
        if change.get("type") in ["requirement", "architecture"]:
            risks.append({
                "category": "schedule",
                "severity": "high",
                "description": "可能影响项目交付时间"
            })

        # 技术风险
        if change.get("complexity") == "high":
            risks.append({
                "category": "technical",
                "severity": "medium",
                "description": "实现复杂度较高"
            })

        # 质量风险
        if change.get("scope") == "large":
            risks.append({
                "category": "quality",
                "severity": "medium",
                "description": "变更范围大，可能影响现有功能"
            })

        # 安全风险
        if change.get("security_related"):
            risks.append({
                "category": "security",
                "severity": "high",
                "description": "涉及安全相关变更"
            })

        total_severity = sum(r["severity"] == "high" for r in risks) * 2
        total_severity += sum(r["severity"] == "medium" for r in risks)

        overall = "low" if total_severity < 2 else ("medium" if total_severity < 5 else "high")

        return {
            "risks": risks,
            "count": len(risks),
            "overall_risk": overall,
            "recommendations": self._generate_risk_recommendations(risks)
        }

    def _generate_risk_recommendations(self, risks: List[Dict]) -> List[str]:
        """生成风险应对建议"""
        recommendations = []

        for risk in risks:
            if risk["category"] == "schedule":
                recommendations.append("建议增加缓冲时间或调整里程碑")
            elif risk["category"] == "technical":
                recommendations.append("建议先做技术验证，再实施")
            elif risk["category"] == "quality":
                recommendations.append("建议增加回归测试覆盖")
            elif risk["category"] == "security":
                recommendations.append("建议安全团队提前介入")

        return recommendations

    def generate_decision_options(self, change: Dict) -> List[Dict]:
        """
        生成决策选项
        
        Args:
            change: 变更内容
            
        Returns:
            决策选项列表
        """
        impact = self.analyze_impact(change)
        risk = self.assess_risk(change)

        # 根据影响和风险生成建议
        if risk["overall_risk"] == "low":
            primary = DecisionType.FIX
            primary_reason = "风险低，可以立即处理"
        elif risk["overall_risk"] == "medium":
            if change.get("urgency") == "high":
                primary = DecisionType.FIX
                primary_reason = "紧急需求，优先处理"
            else:
                primary = DecisionType.RESCHEDULE
                primary_reason = "风险可控，但建议排期"
        else:
            primary = DecisionType.REJECT
            primary_reason = "风险过高，建议拒绝"

        options = [
            {
                "decision": DecisionType.FIX,
                "action": "接受变更，打回修改",
                "reason": primary_reason if primary == DecisionType.FIX else "可接受，但有风险",
                "risk_level": risk["overall_risk"]
            },
            {
                "decision": DecisionType.RESCHEDULE,
                "action": "接受变更，放入下一版本",
                "reason": "降低风险，不影响当前进度",
                "risk_level": "low"
            },
            {
                "decision": DecisionType.REJECT,
                "action": "拒绝变更，维持原方案",
                "reason": primary_reason if primary == DecisionType.REJECT else "变更风险过高",
                "risk_level": "low"
            }
        ]

        return options

    def resolve_dispute(
        self,
        dispute_type: str,
        parties: List[Dict],
        evidence: Dict
    ) -> Dict[str, Any]:
        """
        解决争议
        
        Args:
            dispute_type: 争议类型
            parties: 争议各方
            evidence: 证据材料
            
        Returns:
            裁决结果
        """
        # 质检争议处理
        if dispute_type == "quality":
            # 分析证据
            qa_position = parties[0] if len(parties) > 0 else {}
            dev_position = parties[1] if len(parties) > 1 else {}

            # 模拟裁决逻辑
            qa_strength = evidence.get("qa_strength", 0.5)
            dev_strength = evidence.get("dev_strength", 0.5)

            if qa_strength > dev_strength:
                verdict = {
                    "decision": "qa_wins",
                    "reason": "质检证据更充分",
                    "action": "按质检要求修复"
                }
            elif dev_strength > qa_strength:
                verdict = {
                    "decision": "dev_wins",
                    "reason": "开发解释更合理",
                    "action": "接受当前实现"
                }
            else:
                verdict = {
                    "decision": "compromise",
                    "reason": "双方都有道理",
                    "action": "折中方案：部分修复"
                }

            return verdict

        # 其他争议类型
        return {
            "decision": "deferred",
            "reason": "需要更多信息",
            "action": "等待进一步分析"
        }

    def process_change(self, change: Dict) -> Dict[str, Any]:
        """
        处理变更请求
        
        Args:
            change: 变更内容
            
        Returns:
            处理结果，包含决策选项
        """
        # 分析影响
        impact = self.analyze_impact(change)

        # 评估风险
        risk = self.assess_risk(change)

        # 生成决策选项
        options = self.generate_decision_options(change)

        # 保存待决策
        decision_request = {
            "id": change.get("id", ""),
            "change": change,
            "impact": impact,
            "risk": risk,
            "options": options,
            "created_at": time.time()
        }
        self.pending_decisions.append(decision_request)

        return {
            "success": True,
            "change_id": change.get("id", ""),
            "impact": impact,
            "risk": risk,
            "options": options,
            "pending_confirmation": True
        }

    def make_decision(
        self,
        change_id: str,
        decision: str,
        reason: str = ""
    ) -> Dict[str, Any]:
        """
        做出决策
        
        Args:
            change_id: 变更 ID
            decision: 决策（修/排期/拒绝）
            reason: 决策原因
            
        Returns:
            决策结果
        """
        # 查找待决策
        pending = None
        for p in self.pending_decisions:
            if p["id"] == change_id:
                pending = p
                break

        if not pending:
            return {
                "success": False,
                "error": "变更不存在或已处理"
            }

        # 记录决策
        decision_record = {
            "change_id": change_id,
            "decision": decision,
            "reason": reason,
            "timestamp": time.time(),
            "impact_summary": pending["impact"],
            "risk_summary": pending["risk"]
        }
        self.decision_history.append(decision_record)

        # 移除待决策
        self.pending_decisions = [p for p in self.pending_decisions if p["id"] != change_id]

        return {
            "success": True,
            "change_id": change_id,
            "decision": decision,
            "reason": reason,
            "action": self._get_action_description(decision)
        }

    def _get_action_description(self, decision: str) -> str:
        """获取决策操作描述"""
        if decision == DecisionType.FIX:
            return "变更已接受，相关团队需要修改"
        elif decision == DecisionType.RESCHEDULE:
            return "变更已接受，延后到下一版本实施"
        elif decision == DecisionType.REJECT:
            return "变更已拒绝，维持原方案"
        return ""

    def get_pending_decisions(self) -> List[Dict]:
        """获取待决策列表"""
        return self.pending_decisions

    def get_decision_history(self) -> List[Dict]:
        """获取决策历史"""
        return self.decision_history

    def _do_execute(self, task: Task) -> Any:
        """执行任务"""
        if task.title == "process_change":
            return self.process_change(task.metadata.get("change", {}))
        elif task.title == "make_decision":
            return self.make_decision(
                task.metadata.get("change_id", ""),
                task.metadata.get("decision", ""),
                task.metadata.get("reason", "")
            )
        elif task.title == "resolve_dispute":
            return self.resolve_dispute(
                task.metadata.get("dispute_type", ""),
                task.metadata.get("parties", []),
                task.metadata.get("evidence", {})
            )
        elif task.title == "analyze_impact":
            return self.analyze_impact(task.metadata.get("change", {}))
        return {"error": f"Unknown task: {task.title}"}

    def get_status(self) -> Dict[str, Any]:
        """获取 CCB Agent 状态"""
        return {
            "agent_id": self.agent_id,
            "type": "ccb",
            "state": self.state.value,
            "pending_count": len(self.pending_decisions),
            "total_decisions": len(self.decision_history)
        }