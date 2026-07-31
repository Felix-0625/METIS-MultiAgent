"""
Supervisor 团队 Agent 实现 v2
职责重新设计：
  Supervisor 成员：动态监督各阶段代码（语法/逻辑/任务完成度），有问题自动反馈给专家
  Supervisor 组长：监督所有PM规划能否落地，项目最终质检
  预置10个Supervisor成员，记忆独立
"""

import time
import uuid
from typing import Dict, List, Optional, Any
from .base.hermes_agent import AgentBase, AgentType, Task, AGENT_PRINCIPLES
from core.hermes_client import Message, MessageRole, chat_for_purpose, chat_for_json
from core.json_utils import extract_first_json_object


class SupervisorMemberAgent:
    """
    Supervisor 成员 Agent
    职责：动态监督某阶段的代码质量
    - 检查语法和逻辑问题
    - 检查是否完成阶段任务
    - 有问题自动反馈给对应专家（不生成建议，直接报告问题）
    - 在 Supervisor 窗口动态显示问题状态
    记忆独立
    """

    def __init__(self, member_id: str, name: str, hermes_client):
        self.member_id = member_id
        self.name = name
        self.hermes = hermes_client
        self.agent_id = f"sup-member-{member_id}"
        # 角色描述（与组长能力一致）
        self.role_description = (
            "Supervisor 团队成员，负责动态监督某阶段的代码质量。"
            "检查语法/逻辑问题，检查是否完成阶段任务，有问题直接反馈给对应专家（不生成建议）。"
        )
        self.assigned_phase_id: Optional[str] = None
        self.assigned_phase_name: Optional[str] = None
        self.status: str = "available"
        self.issues: List[Dict] = []
        self.review_log: List[Dict] = []
        self.phase_passed: bool = False

    def assign_phase(self, phase_id: str, phase_name: str) -> None:
        self.assigned_phase_id = phase_id
        self.assigned_phase_name = phase_name
        self.status = "working"
        self.issues = []
        self.review_log = []
        self.phase_passed = False

    def dynamic_review(
        self,
        subproject_id: str,
        subproject_name: str,
        file_contents: Dict[str, str],
        phase_task_description: str,
        agent_id: str,
        agent_role: str,
    ) -> Dict[str, Any]:
        """
        动态质检：检查代码语法/逻辑/任务完成度
        有问题直接生成反馈任务（不生成建议）
        """
        if not file_contents:
            return {"issues": [], "passed": True, "subproject_id": subproject_id}

        files_text = ""
        for path, content in list(file_contents.items())[:5]:
            snippet = content[:1500] + ("..." if len(content) > 1500 else "")
            files_text += f"\n=== {path} ===\n{snippet}\n"

        system_prompt = (
            f"{AGENT_PRINCIPLES}\n\n---\n\n"
            f"你是 Supervisor 成员【{self.name}】，负责动态质检阶段代码。\n\n"
            "【质检规则】：\n"
            "1. 检查语法错误（import错误、缩进错误、语法错误）\n"
            "2. 检查逻辑问题（空函数体、未实现的TODO、逻辑矛盾）\n"
            "3. 检查是否完成了阶段任务要求的功能\n\n"
            "【输出规则】：\n"
            "- 只报告具体问题，不生成修改建议\n"
            "- 每个问题必须指明：文件路径、问题描述、严重程度(error/warning)\n"
            "- 输出严格 JSON：\n"
            '{"passed": true/false, "issues": [{"file_path":"...","message":"...","severity":"error|warning","line":0}]}'
        )
        messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(role=MessageRole.USER, content=(
                f"阶段任务：{phase_task_description[:300]}\n"
                f"子项目：{subproject_name}\n"
                f"代码文件：{files_text}"
            )),
        ]
        try:
            resp = chat_for_json(self.hermes, messages, purpose="reviewer")
            content = resp.get("content", "")
            result = extract_first_json_object(content)
            if not result:
                result = {"passed": True, "issues": []}
        except Exception:
            result = {"passed": True, "issues": []}

        # 为每个问题生成 ID 并补全标准字段（与 _run_qc_for_subproject 的 issues_detail 格式一致）
        import hashlib as _hl
        for iss in result.get("issues", []):
            msg = iss.get("message", "")
            fp = iss.get("file_path", "")
            iss["id"] = "issue-" + _hl.sha256(f"{msg[:60]}|{fp}".encode("utf-8", errors="replace")).hexdigest()[:6]
            iss["subproject_id"] = subproject_id
            iss["subproject_name"] = subproject_name
            iss["responsible_agent_id"] = agent_id
            iss["responsible_agent_role"] = agent_role
            iss["status"] = "open"
            iss["created_at"] = time.time()
            iss["phase_id"] = self.assigned_phase_id
            # 补全缺失字段，保证格式统一
            iss.setdefault("fix_hint", "")
            iss.setdefault("layer", "dynamic_review")
            iss.setdefault("line", 0)
            iss.setdefault("fix_rounds", 0)

        new_issues = result.get("issues", [])
        self.issues.extend(new_issues)

        review_entry = {
            "subproject_id": subproject_id,
            "reviewed_at": time.time(),
            "passed": result.get("passed", True),
            "issue_count": len(new_issues),
        }
        self.review_log.append(review_entry)

        return {
            "passed": result.get("passed", True),
            "issues": new_issues,
            "subproject_id": subproject_id,
            "member_id": self.member_id,
        }

    def check_phase_completion(
        self,
        phase_task_description: str,
        completed_subprojects: List[Dict],
    ) -> Dict[str, Any]:
        """
        阶段完成后检查：是否有遗漏的阶段任务
        """
        completed_text = "\n".join(
            f"- {sp.get('name','')}: {sp.get('description','')[:100]}"
            for sp in completed_subprojects
        )
        system_prompt = (
            f"{AGENT_PRINCIPLES}\n\n---\n\n"
            "你是 Supervisor 成员，检查阶段任务是否全部完成。\n\n"
            "输出严格 JSON：\n"
            '{"all_completed": true/false, "missing_tasks": ["遗漏任务描述"], "summary": "..."}'
        )
        messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(role=MessageRole.USER, content=(
                f"阶段任务要求：\n{phase_task_description[:500]}\n\n"
                f"已完成的子项目：\n{completed_text}"
            )),
        ]
        try:
            resp = chat_for_json(self.hermes, messages, purpose="reviewer")
            content = resp.get("content", "")
            parsed = extract_first_json_object(content)
            if parsed:
                return parsed
        except Exception:
            pass
        return {"all_completed": True, "missing_tasks": [], "summary": "阶段任务检查完成"}

    def get_open_issues(self) -> List[Dict]:
        return [i for i in self.issues if i.get("status") == "open"]

    def mark_issue_fixed(self, issue_id: str) -> bool:
        for iss in self.issues:
            if iss.get("id") == issue_id:
                iss["status"] = "fixed"
                iss["fixed_at"] = time.time()
                return True
        return False

    def mark_issue_fixing(self, issue_id: str) -> bool:
        for iss in self.issues:
            if iss.get("id") == issue_id:
                iss["status"] = "fixing"
                return True
        return False

    def can_phase_proceed(self) -> Dict[str, Any]:
        open_errors = [i for i in self.issues if i.get("status") == "open" and i.get("severity") == "error"]
        can = len(open_errors) == 0
        if can:
            self.phase_passed = True
        return {
            "can_proceed": can,
            "open_errors": len(open_errors),
            "total_issues": len(self.issues),
            "message": "质检通过，可以进入下一阶段" if can else f"还有 {len(open_errors)} 个严重问题未修复",
        }

    def to_dict(self) -> Dict:
        open_issues = self.get_open_issues()
        return {
            "member_id": self.member_id,
            "agent_id": self.agent_id,
            "name": self.name,
            "type": "supervisor_member",
            "status": self.status,
            "assigned_phase_id": self.assigned_phase_id,
            "assigned_phase_name": self.assigned_phase_name,
            "phase_passed": self.phase_passed,
            "total_issues": len(self.issues),
            "open_issues": len(open_issues),
            "issues": self.issues,
        }

    def to_persist(self) -> Dict:
        return {
            "member_id": self.member_id,
            "name": self.name,
            "agent_id": self.agent_id,
            "assigned_phase_id": self.assigned_phase_id,
            "assigned_phase_name": self.assigned_phase_name,
            "status": self.status,
            "issues": self.issues,
            "review_log": self.review_log[-20:],
            "phase_passed": self.phase_passed,
        }

    def from_persist(self, data: Dict) -> None:
        self.assigned_phase_id = data.get("assigned_phase_id")
        self.assigned_phase_name = data.get("assigned_phase_name")
        self.status = data.get("status", "available")
        self.issues = data.get("issues", [])
        self.review_log = data.get("review_log", [])
        self.phase_passed = data.get("phase_passed", False)


class SupervisorLeaderAgent(AgentBase):
    """
    Supervisor 组长 Agent
    职责：
    1. 监督所有PM规划能否落地实现需求
    2. 项目最终质检（所有阶段完成后）
    3. 与用户对话（审查窗口）
    4. 管理10个预置Supervisor成员
    """

    ESSENTIAL_CAPABILITIES = ["规划审核", "项目质检", "阶段监督", "用户审查对话"]
    COMPRESS_THRESHOLD = 10
    KEEP_RECENT = 6
    PRESET_MEMBER_COUNT = 10

    def __init__(self, *args, project_id: Optional[str] = None, **kwargs):
        self.project_id = project_id or "default"
        self.members: Dict[str, SupervisorMemberAgent] = {}
        self.conversation_history: List[Dict] = []
        self.context_summary: str = ""
        self.current_phase_id: Optional[str] = None
        self._capabilities = list(self.ESSENTIAL_CAPABILITIES)
        # 总体规划（由 PM 组长 final_plan 传入，注入到监督对话的 system prompt）
        self.project_background: str = ""
        self.final_plan: Optional[Dict] = None
        super().__init__(*args, agent_type=AgentType.SUPERVISOR, **kwargs)
        self._init_preset_members()

    def load_project_plan(self, final_plan: Dict) -> None:
        """
        接收 PM 组长的 final_plan，存储为项目背景。
        在 confirm_pm_plan 路由确认后由 main.py 调用，确保监督 Leader 掌握总体规划。
        """
        if not isinstance(final_plan, dict):
            raise TypeError("final_plan must be a mapping")
        self.final_plan = final_plan
        overview = str(final_plan.get("project_overview") or "")
        raw_features = final_plan.get("core_features") or []
        if isinstance(raw_features, str):
            raw_features = [raw_features]
        features = "、".join(
            str(item).strip() for item in raw_features
            if str(item).strip()
        )

        tech = final_plan.get("tech_stack") or []
        if isinstance(tech, dict):
            labels = {
                "frontend": "前端",
                "backend": "后端",
                "database": "数据库",
            }
            tech_parts = []
            for key, value in tech.items():
                values = value if isinstance(value, (list, tuple, set)) else [value]
                rendered = "、".join(
                    str(item).strip() for item in values
                    if str(item).strip()
                )
                if rendered:
                    tech_parts.append(f"{labels.get(str(key), str(key))} {rendered}")
            tech_str = " / ".join(tech_parts)
        elif isinstance(tech, str):
            tech_str = tech.strip()
        elif isinstance(tech, (list, tuple, set)):
            tech_str = "、".join(
                str(item).strip() for item in tech
                if str(item).strip()
            )
        else:
            tech_str = str(tech).strip()

        phases = final_plan.get("phases") or []
        if not isinstance(phases, list):
            phases = []
        phases_text = "\n".join(
            f"  - 阶段{p.get('name','')}: {p.get('description','')[:80]}"
            for p in phases if isinstance(p, dict)
        )
        self.project_background = (
            f"【项目概述】{overview}\n"
            f"【核心功能】{features}\n"
            f"【技术栈】{tech_str}\n"
            f"【开发阶段规划】\n{phases_text}"
        )

    def _init_preset_members(self) -> None:
        if self.members:
            return
        for i in range(1, self.PRESET_MEMBER_COUNT + 1):
            mid = f"preset-{i:02d}"
            self.members[mid] = SupervisorMemberAgent(
                member_id=mid,
                name=f"Supervisor成员{i:02d}",
                hermes_client=self.hermes,
            )

    def add_member(self) -> SupervisorMemberAgent:
        new_id = f"custom-{uuid.uuid4().hex[:6]}"
        # 用当前自定义成员数量计算编号，避免与预置成员编号冲突
        custom_count = sum(1 for mid in self.members if mid.startswith("custom-"))
        idx = custom_count + 1
        member = SupervisorMemberAgent(
            member_id=new_id,
            name=f"Supervisor成员-新增{idx:02d}",
            hermes_client=self.hermes,
        )
        self.members[new_id] = member
        return member

    def remove_member(self, member_id: str) -> Dict[str, Any]:
        if member_id not in self.members:
            return {"success": False, "message": f"成员 {member_id} 不存在"}
        if len(self.members) <= 4:
            return {"success": False, "message": "Supervisor团队成员数不得少于4人，无法删除"}
        del self.members[member_id]
        return {"success": True, "message": f"成员 {member_id} 已删除"}

    def assign_phase_to_member(self, phase_id: str, phase_name: str) -> Optional[SupervisorMemberAgent]:
        for member in self.members.values():
            if member.status == "available":
                member.assign_phase(phase_id, phase_name)
                return member
        for member in self.members.values():
            if member.status == "completed":
                member.assign_phase(phase_id, phase_name)
                return member
        first = next(iter(self.members.values()))
        first.assign_phase(phase_id, phase_name)
        return first

    def get_member_for_phase(self, phase_id: str) -> Optional[SupervisorMemberAgent]:
        for member in self.members.values():
            if member.assigned_phase_id == phase_id:
                return member
        return None

    def get_members_info(self) -> List[Dict]:
        return [m.to_dict() for m in self.members.values()]

    # ── 阶段监督接口（供 main.py 调用）──────────────────────────────────────────

    @property
    def phase_supervisors(self) -> Dict[str, SupervisorMemberAgent]:
        """返回 phase_id → SupervisorMemberAgent 的映射（兼容旧接口）"""
        return {m.assigned_phase_id: m for m in self.members.values() if m.assigned_phase_id}

    def create_phase_supervisor(self, phase_id: str, phase_name: str) -> SupervisorMemberAgent:
        """
        为阶段创建/获取监督 Agent。
        如果该阶段已有分配的成员，直接返回；否则从成员池中分配一个。
        """
        existing = self.get_member_for_phase(phase_id)
        if existing:
            return existing
        # 从成员池分配
        member = self.assign_phase_to_member(phase_id, phase_name)
        return member

    def get_phase_supervisor(self, phase_id: str) -> Optional[SupervisorMemberAgent]:
        """获取负责某阶段的监督成员"""
        return self.get_member_for_phase(phase_id)

    def review_phase(
        self,
        phase_id: str,
        phase_name: str,
        subprojects: List[Dict],
        agents: Dict,
        qc_results: Dict,
    ) -> Dict[str, Any]:
        """
        触发阶段审查：
        1. 汇总本阶段的质检结果
        2. 判断是否通过（无 open error）
        3. 生成审查报告
        """
        member = self.get_member_for_phase(phase_id)
        if not member:
            member = self.create_phase_supervisor(phase_id, phase_name)

        # 从 qc_results 中同步本阶段的问题到 member.issues
        # 策略（v2）：
        #   1. 已有问题：按 ID 更新状态（qc 说 fixed 就改 fixed，不保留旧 open）
        #   2. 新问题（qc 发现但 member.issues 里没有）：只追加 open 状态的
        # 这样 member.issues 始终反映最新质检结果，一键返工不会提交历史已修复的问题
        existing_by_id = {i.get("id"): i for i in member.issues}
        for sp in subprojects:
            sp_id = sp["id"]
            qc = qc_results.get(sp_id, {})
            for iss_detail in qc.get("issues_detail", []):
                iss_id = iss_detail.get("id")
                new_status = iss_detail.get("status", "open")
                if iss_id and iss_id in existing_by_id:
                    # 已有问题：同步最新状态（只允许向前推进，不允许 fixed→open 回退）
                    old_status = existing_by_id[iss_id].get("status", "open")
                    status_order = {"open": 0, "fixing": 1, "fixed": 2, "verified": 3, "needs_manual": 4}
                    if status_order.get(new_status, 0) >= status_order.get(old_status, 0):
                        existing_by_id[iss_id]["status"] = new_status
                        if new_status == "fixed":
                            existing_by_id[iss_id]["fixed_at"] = iss_detail.get("fixed_at", time.time())
                elif new_status == "open":
                    # 新问题：只追加 open 状态的（fixed/verified 的不追加，避免噪音）
                    iss_copy = dict(iss_detail)
                    iss_copy["phase_id"] = phase_id
                    member.issues.append(iss_copy)
                    existing_by_id[iss_id] = iss_copy

        # 判断是否通过
        open_errors = [i for i in member.issues if i.get("status") == "open" and i.get("severity") == "error"]
        open_warnings = [i for i in member.issues if i.get("status") == "open" and i.get("severity") == "warning"]
        passed = len(open_errors) == 0

        if passed:
            member.phase_passed = True

        # 记录审查日志
        member.review_log.append({
            "reviewed_at": time.time(),
            "passed": passed,
            "open_errors": len(open_errors),
            "open_warnings": len(open_warnings),
        })

        # 生成审查报告文本
        if passed:
            report = f"✅ 阶段「{phase_name}」质检通过，可以进入下一阶段。"
            if open_warnings:
                report += f"\n⚠️ 有 {len(open_warnings)} 个警告（不影响推进）。"
        else:
            issues_text = "\n".join(
                f"- [{i.get('severity','error').upper()}] {i.get('file_path','未知文件')}：{i.get('message','')}"
                for i in open_errors[:10]
            )
            report = (
                f"❌ 阶段「{phase_name}」质检未通过，有 {len(open_errors)} 个严重问题需修复：\n"
                f"{issues_text}"
            )

        return {
            "phase_id": phase_id,
            "phase_name": phase_name,
            "passed": passed,
            "report": report,
            "issues": member.issues,
            "error_count": len(open_errors),
            "warning_count": len(open_warnings),
            "can_proceed": passed,
        }

    def get_fix_tasks_for_phase(self, phase_id: str) -> List[Dict]:
        """获取某阶段的修复任务列表（按 Agent 分组）"""
        member = self.get_member_for_phase(phase_id)
        if not member:
            return []
        open_issues = [i for i in member.issues if i.get("status") in ("open", "fixing")]
        # 按 responsible_agent_id 分组
        by_agent: Dict[str, List[Dict]] = {}
        for iss in open_issues:
            agent_id = iss.get("responsible_agent_id", "unknown")
            if agent_id not in by_agent:
                by_agent[agent_id] = []
            by_agent[agent_id].append(iss)
        return [
            {
                "agent_id": agent_id,
                "agent_role": issues[0].get("responsible_agent_role", ""),
                "issues": issues,
                "issue_count": len(issues),
            }
            for agent_id, issues in by_agent.items()
        ]

    def get_issue(self, phase_id: str, issue_id: str) -> Optional[Dict]:
        """获取某阶段的某个问题详情"""
        member = self.get_member_for_phase(phase_id)
        if not member:
            return None
        for iss in member.issues:
            if iss.get("id") == issue_id:
                return iss
        return None

    def mark_issue_fixing(self, phase_id: str, issue_id: str, pm_analysis: str = "") -> Dict[str, Any]:
        """标记问题为修复中，并记录 PM 分析"""
        member = self.get_member_for_phase(phase_id)
        if not member:
            return {"success": False, "message": "阶段不存在"}
        for iss in member.issues:
            if iss.get("id") == issue_id:
                iss["status"] = "fixing"
                iss["pm_analysis"] = pm_analysis
                iss["fixing_at"] = time.time()
                return {"success": True, "issue_id": issue_id}
        return {"success": False, "message": "问题不存在"}

    @property
    def current_issues(self) -> List[Dict]:
        """返回当前所有阶段的所有问题（兼容旧接口）"""
        all_issues = []
        for member in self.members.values():
            all_issues.extend(member.issues)
        return all_issues

    def review_pm_plan(self, plan: Dict) -> Dict[str, Any]:
        """监督PM规划能否落地"""
        phases_text = "\n".join(
            f"- {p.get('name','')}: {p.get('description','')[:100]}"
            for p in plan.get("phases", [])
        )
        system_prompt = (
            f"{AGENT_PRINCIPLES}\n\n---\n\n"
            "你是 Supervisor 组长，审核 PM 规划的可行性。\n\n"
            "检查：\n"
            "1. 阶段划分是否合理，能否落地\n"
            "2. 技术选型是否可行\n"
            "3. 子项目是否覆盖了所有需求\n"
            "4. 是否有明显遗漏或矛盾\n\n"
            "输出 JSON：\n"
            '{"feasible": true/false, "issues": ["问题描述"], "suggestions": ["建议"], "summary": "..."}'
        )
        messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(role=MessageRole.USER, content=(
                f"项目概述：{plan.get('project_overview','')[:300]}\n"
                f"阶段划分：\n{phases_text}\n"
                f"技术栈：{plan.get('tech_stack', {})}"
            )),
        ]
        try:
            resp = chat_for_json(self.hermes, messages, purpose="reviewer")
            content = resp.get("content", "")
            parsed = extract_first_json_object(content)
            if parsed:
                return parsed
        except Exception:
            pass
        return {"feasible": True, "issues": [], "suggestions": [], "summary": "规划审核通过"}

    def final_project_review(
        self,
        all_phases: List[Dict],
        all_files: Dict[str, str],
        project_requirements: str,
    ) -> Dict[str, Any]:
        """项目最终质检（所有阶段完成后）"""
        files_summary = "\n".join(f"- {k}" for k in list(all_files.keys())[:20])
        system_prompt = (
            f"{AGENT_PRINCIPLES}\n\n---\n\n"
            "你是 Supervisor 组长，对整个项目进行最终质检。\n\n"
            "检查：\n"
            "1. 项目是否完整实现了所有需求\n"
            "2. 各阶段交付物是否一致\n"
            "3. 是否有运行问题（导入错误、配置缺失等）\n"
            "4. 哪些文件有问题，对应哪个专家\n\n"
            "输出 JSON：\n"
            '{"passed": true/false, "issues": [{"file_path":"...","message":"...","responsible_role":"...","severity":"error|warning"}], "summary": "..."}'
        )
        messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(role=MessageRole.USER, content=(
                f"项目需求：{project_requirements[:400]}\n\n"
                f"已完成阶段：{[p.get('name','') for p in all_phases]}\n\n"
                f"项目文件列表：\n{files_summary}"
            )),
        ]
        try:
            resp = chat_for_json(self.hermes, messages, purpose="reviewer")
            content = resp.get("content", "")
            parsed = extract_first_json_object(content)
            if parsed:
                return parsed
        except Exception:
            pass
        return {"passed": True, "issues": [], "summary": "项目质检完成"}

    def chat_with_user(
        self,
        user_input: str,
        phase_id: Optional[str] = None,
        history: Optional[List[Dict]] = None,
        context_summary: Optional[str] = None,
        progress_data: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        hist = history or self.conversation_history
        new_summary = context_summary or self.context_summary

        if len(hist) > self.COMPRESS_THRESHOLD:
            to_compress = hist[:-self.KEEP_RECENT]
            recent_raw = hist[-self.KEEP_RECENT:]
            compressed = self._compress_history(to_compress)
            new_summary = (new_summary + "\n\n【新增摘要】\n" + compressed) if new_summary else compressed
        else:
            recent_raw = hist

        phase_ctx = ""
        if phase_id:
            member = self.get_member_for_phase(phase_id)
            if member:
                check = member.can_phase_proceed()
                phase_ctx = (
                    f"\n\n【当前阶段：{member.assigned_phase_name}】\n"
                    f"可进入下一阶段：{'是' if check['can_proceed'] else '否'}\n"
                    f"未解决问题：{check['open_errors']} 个"
                )

        progress_ctx = ""
        if progress_data:
            d = progress_data
            progress_ctx = (
                f"\n\n【项目进度】总任务：{d.get('total_tasks','-')} | "
                f"已完成：{d.get('completed_tasks','-')} | "
                f"进度：{d.get('overall_progress',0)}%"
            )

        bg_ctx = ""
        if self.project_background:
            bg_ctx = f"\n\n【项目总体规划（PM 组长确认）】\n{self.project_background[:600]}"

        system_prompt = (
            f"{AGENT_PRINCIPLES}\n\n---\n\n"
            "你是项目 Supervisor 组长，负责阶段审查和质量把控。\n\n"
            "【职责】：\n"
            "1. 基于审查报告与用户沟通，解释问题\n"
            "2. 只有所有严重问题修复后，才允许进入下一阶段\n"
            "3. 如果用户要求修改功能，分析影响范围\n\n"
            "【规则】：\n"
            "- 不能主动允许跳过问题\n"
            "- 问题修复后需重新确认\n"
            "- 回复简洁，不超过 400 字"
            + bg_ctx + phase_ctx + progress_ctx
        )

        messages = [Message(role=MessageRole.SYSTEM, content=system_prompt)]
        if new_summary:
            messages.append(Message(role=MessageRole.SYSTEM, content=f"对话摘要：\n{new_summary}"))
        for h in recent_raw:
            role = MessageRole.USER if h.get("role") == "user" else MessageRole.ASSISTANT
            if h.get("content"):
                messages.append(Message(role=role, content=h["content"]))
        messages.append(Message(role=MessageRole.USER, content=user_input))

        try:
            resp = chat_for_purpose(self.hermes, messages, purpose="reviewer")
            reply = resp.get("content", "")
        except Exception:
            reply = f"【Supervisor组长 离线模式】\n已收到：{user_input[:200]}\n请配置 API Key。"

        self.conversation_history.append({"role": "user", "content": user_input})
        self.conversation_history.append({"role": "assistant", "content": reply})
        if new_summary:
            self.context_summary = new_summary
        if len(self.conversation_history) > self.KEEP_RECENT * 2:
            self.conversation_history = self.conversation_history[-(self.KEEP_RECENT * 2):]

        return {"reply": reply, "success": True, "summary": new_summary or ""}

    def check_phase_can_proceed(self, phase_id: str) -> Dict[str, Any]:
        member = self.get_member_for_phase(phase_id)
        if not member:
            return {"can_proceed": False, "message": "该阶段尚未分配Supervisor成员", "reviewed": False}
        result = member.can_phase_proceed()
        result["reviewed"] = len(member.review_log) > 0
        return result

    def mark_issue_fixed(self, phase_id: str, issue_id: str) -> Dict[str, Any]:
        member = self.get_member_for_phase(phase_id)
        if not member:
            return {"success": False, "message": "阶段不存在"}
        success = member.mark_issue_fixed(issue_id)
        return {"success": success, "message": "已标记为修复" if success else "问题不存在"}

    def get_all_phase_status(self) -> List[Dict]:
        return [m.to_dict() for m in self.members.values()]

    def get_status(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "type": "supervisor_leader",
            "project_id": self.project_id,
            "state": self.state.value,
            "members_count": len(self.members),
            "phases": self.get_all_phase_status(),
        }

    def to_persist(self) -> Dict:
        return {
            "project_id": self.project_id,
            "conversation_history": self.conversation_history[-40:],
            "context_summary": self.context_summary,
            "current_phase_id": self.current_phase_id,
            "project_background": self.project_background,
            "final_plan": self.final_plan,
            "members": {mid: m.to_persist() for mid, m in self.members.items()},
        }

    def from_persist(self, data: Dict) -> None:
        self.conversation_history = data.get("conversation_history", [])
        self.context_summary = data.get("context_summary", "")
        self.current_phase_id = data.get("current_phase_id")
        self.project_background = data.get("project_background", "")
        self.final_plan = data.get("final_plan")
        for mid, mdata in data.get("members", {}).items():
            if mid in self.members:
                self.members[mid].from_persist(mdata)
            else:
                m = SupervisorMemberAgent(member_id=mid, name=mdata.get("name", mid), hermes_client=self.hermes)
                m.from_persist(mdata)
                self.members[mid] = m

    def _do_execute(self, task: Task) -> Any:
        if task.title == "review_pm_plan":
            return self.review_pm_plan(task.metadata.get("plan", {}))
        elif task.title == "chat":
            return self.chat_with_user(task.description, phase_id=task.metadata.get("phase_id"))
        elif task.title == "final_review":
            return self.final_project_review(
                task.metadata.get("phases", []),
                task.metadata.get("files", {}),
                task.metadata.get("requirements", ""),
            )
        return {"error": f"Unknown task: {task.title}"}


# 向后兼容别名
PhaseSupervisionAgent = SupervisorMemberAgent


# ─── Supervisor 团队（容器） ──────────────────────────────────────────────────

class SupervisorTeam:
    """Supervisor 团队容器，持有组长并代理成员管理操作"""

    def assign_phase_to_member(self, phase_id: str, phase_name: str) -> "Optional[SupervisorMemberAgent]":
        """将阶段任务分配给一个空闲的Supervisor成员（代理到组长）"""
        return self.leader.assign_phase_to_member(phase_id, phase_name)

    def __init__(self, hermes_client):
        from agents.base.memory import HybridMemory
        memory = HybridMemory("memory/global_supervisor_team")
        self.leader = SupervisorLeaderAgent(
            hermes_client=hermes_client,
            memory_store=memory,
            project_id="global",
        )

    def add_member(self) -> SupervisorMemberAgent:
        return self.leader.add_member()

    def remove_member(self, member_id: str) -> Dict[str, Any]:
        return self.leader.remove_member(member_id)

    def get_members_info(self) -> List[Dict]:
        return self.leader.get_members_info()
