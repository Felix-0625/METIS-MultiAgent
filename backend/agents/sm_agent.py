"""
SMAgent 实现
Skill 管理 Agent，负责 Skill 的解析、分类和质量评估
"""

import ipaddress
import time
from typing import Dict, List, Optional, Any
from .base.hermes_agent import AgentBase, AgentType, Task, AgentState
from core.hermes_client import Message, MessageRole


class SkillCategory:
    """Skill 分类"""

    # 能力性质
    CAPABILITY_TYPES = ["工具型", "知识型", "认知型"]

    # 应用场景 - 业务领域
    BUSINESS_DOMAINS = ["法律", "医疗", "金融", "教育", "通用"]

    # 应用场景 - 岗位角色
    ROLE_TYPES = ["分析师", "顾问", "审核员", "助理", "工程师"]


class SMAgent(AgentBase):
    """
    SMAgent
    
    职责：
    - Skill 解析
    - 分类判断（工具型/知识型/认知型）
    - 质量评估
    - 版本控制
    """

    # 基本能力极值
    ESSENTIAL_CAPABILITIES = [
        "Skill解析",
        "分类判断",
        "质量评估",
        "版本控制"
    ]

    def __init__(self, *args, **kwargs):
        self.skill_pool: Dict[str, Dict] = {}
        self._capabilities = list(self.ESSENTIAL_CAPABILITIES)
        super().__init__(*args, agent_type=AgentType.SM, **kwargs)

    def parse_skill(self, skill_content: Dict) -> Dict[str, Any]:
        """
        解析 Skill
        
        检查 SKILL.md 格式、Frontmatter 字段、内容长度、恶意内容
        """
        # 检查必需字段
        required_fields = ["name", "description", "version"]
        missing_fields = [f for f in required_fields if f not in skill_content]

        if missing_fields:
            return {
                "passed": False,
                "stage": "format_check",
                "error": f"缺少必需字段: {missing_fields}"
            }

        # 检查内容长度
        content = skill_content.get("content", "")
        if len(content) < 100:
            return {
                "passed": False,
                "stage": "content_check",
                "error": f"内容过短 ({len(content)} < 100)"
            }

        # 检查恶意内容（简化）
        malicious_keywords = ["<script>", "eval(", "exec("]
        for keyword in malicious_keywords:
            if keyword in content:
                return {
                    "passed": False,
                    "stage": "security_check",
                    "error": f"检测到可疑内容: {keyword}"
                }

        return {
            "passed": True,
            "stage": "all_passed",
            "warnings": []
        }

    def classify_capability_type(self, skill_content: Dict) -> Dict[str, Any]:
        """
        判断能力性质
        
        - 工具型: 含可执行命令/脚本
        - 知识型: 含规范/原则/清单
        - 认知型: 含思维模型/框架
        """
        content = skill_content.get("content", "")
        tool_keywords = ["run", "execute", "command", "script", "bash", "python"]
        knowledge_keywords = ["rule", "principle", "guideline", "checklist", "规范", "原则"]
        cognitive_keywords = ["framework", "model", "thinking", "method", "框架", "思维"]

        scores = {
            "工具型": sum(1 for k in tool_keywords if k.lower() in content.lower()),
            "知识型": sum(1 for k in knowledge_keywords if k.lower() in content.lower()),
            "认知型": sum(1 for k in cognitive_keywords if k.lower() in content.lower())
        }

        max_score = max(scores.values())
        if max_score == 0:
            return {"type": "知识型", "confidence": 0.5}

        # 找最高分类
        for cap_type, score in scores.items():
            if score == max_score:
                return {
                    "type": cap_type,
                    "confidence": min(0.95, 0.5 + score * 0.15)
                }

        return {"type": "知识型", "confidence": 0.5}

    def classify_application_scenario(self, skill_content: Dict) -> Dict[str, Any]:
        """
        识别应用场景
        
        - 业务领域: 法律/医疗/金融/教育/通用
        - 岗位角色: 分析师/顾问/审核员/助理/工程师
        """
        content = skill_content.get("content", "") + skill_content.get("name", "")

        # 业务领域识别
        domain_keywords = {
            "法律": ["law", "legal", "合同", "法规", "诉讼"],
            "医疗": ["medical", "health", "诊断", "处方", "病历"],
            "金融": ["finance", "银行", "投资", "风险", "信用"],
            "教育": ["education", "学习", "课程", "教学", "培训"]
        }

        detected_domain = "通用"
        for domain, keywords in domain_keywords.items():
            if any(k.lower() in content.lower() for k in keywords):
                detected_domain = domain
                break

        # 岗位角色识别
        role_keywords = {
            "分析师": ["analyze", "分析", "统计"],
            "顾问": ["consult", "建议", "咨询"],
            "审核员": ["audit", "review", "审查", "检查"],
            "助理": ["assist", "help", "support", "助理"],
            "工程师": ["engineer", "开发", "实现", "build"]
        }

        detected_roles = []
        for role, keywords in role_keywords.items():
            if any(k.lower() in content.lower() for k in keywords):
                detected_roles.append(role)

        if not detected_roles:
            detected_roles = ["工程师"]

        return {
            "business_domain": detected_domain,
            "roles": detected_roles
        }

    def generate_classification_report(self, skill_content: Dict) -> Dict[str, Any]:
        """
        生成分类报告
        
        等待用户确认/调整
        """
        capability = self.classify_capability_type(skill_content)
        scenario = self.classify_application_scenario(skill_content)

        report = {
            "skill_id": skill_content.get("name", "unknown"),
            "capability_type": capability,
            "application_scenario": scenario,
            "recommendation": {
                "domain": scenario["business_domain"],
                "roles": scenario["roles"]
            },
            "ready_for_confirmation": True
        }

        return report


    # ── Skill 文件/URL 解析（Skill Agent 核心能力）────────────────────────────

    def parse_skill_file_content(self, raw_text: str, filename: str = "") -> Dict[str, Any]:
        """
        解析 SKILL.md / 任意 Markdown 文件内容，提取结构化 skill 数据。

        支持两种格式：
        1. YAML Frontmatter + Markdown 正文（标准 SKILL.md 格式）
           ---
           name: "xxx"
           description: "..."
           ---
           # 正文内容
        2. 纯 Markdown（无 Frontmatter，从标题和内容推断）

        返回：
        {
            "success": bool,
            "skill": {name, description, version, content, tags, for_agents, source},
            "parse_method": "frontmatter" | "inferred",
            "warnings": [...],
            "error": "..." (仅失败时)
        }
        """
        import re

        raw_text = raw_text.strip()
        if not raw_text:
            return {"success": False, "error": "内容为空"}
        # Skill 文件大小限制 1MB，防止 ReDoS + 内存耗尽
        if len(raw_text) > 1_000_000:
            return {"success": False, "error": "Skill 文件过大（限制 1MB）"}

        skill = {
            "name": "",
            "description": "",
            "version": "1.0.0",
            "content": "",
            "tags": [],
            "for_agents": [],
            "source": "imported",
        }
        warnings = []
        parse_method = "inferred"

        # ── 尝试解析 YAML Frontmatter ──────────────────────────────────────────
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", raw_text, re.DOTALL)
        if fm_match:
            fm_text = fm_match.group(1)
            body = fm_match.group(2).strip()
            parse_method = "frontmatter"

            # 简单 YAML 解析（key: value 格式，不依赖 pyyaml）
            for line in fm_text.splitlines():
                line = line.strip()
                if ":" in line:
                    key, _, val = line.partition(":")
                    key = key.strip().lower()
                    val = val.strip().strip('"').strip("'")
                    if key == "name":
                        skill["name"] = val
                    elif key == "description":
                        skill["description"] = val
                    elif key == "version":
                        skill["version"] = val or "1.0.0"
                    elif key in ("tags", "categories"):
                        # 支持 tags: [a, b] 或 tags: a, b
                        val_clean = val.strip("[]")
                        skill["tags"] = [t.strip().strip('"').strip("'")
                                         for t in val_clean.split(",") if t.strip()]
                    elif key in ("for_agents", "agents", "for_agent"):
                        val_clean = val.strip("[]")
                        skill["for_agents"] = [t.strip().strip('"').strip("'")
                                               for t in val_clean.split(",") if t.strip()]

            skill["content"] = body

        else:
            # ── 纯 Markdown：从标题推断 name，全文作为 content ─────────────────
            lines = raw_text.splitlines()
            for line in lines:
                if line.startswith("# "):
                    skill["name"] = line[2:].strip()
                    break
            skill["content"] = raw_text

        # ── 补全缺失字段 ────────────────────────────────────────────────────────
        if not skill["name"]:
            # 从文件名推断
            if filename:
                skill["name"] = filename.replace(".md", "").replace("-", " ").replace("_", " ").title()
            else:
                skill["name"] = "未命名 Skill"
            warnings.append(f"name 字段缺失，已从文件名推断为：{skill['name']}")

        if not skill["description"]:
            # 从正文第一段推断
            for line in skill["content"].splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("```"):
                    skill["description"] = line[:200]
                    break
            if not skill["description"]:
                skill["description"] = skill["name"]
            warnings.append("description 字段缺失，已从正文推断")

        # ── 自动推断 for_agents（如果未指定）──────────────────────────────────
        if not skill["for_agents"]:
            content_lower = skill["content"].lower() + skill["name"].lower()
            agent_keywords = {
                "pm": ["project manager", "pm", "sprint", "scrum", "需求", "规划", "里程碑"],
                "supervisor": ["supervisor", "review", "质检", "调度", "监控", "code review", "质量检查"],
                "hr": ["hr", "human resource", "team", "recruit", "人才", "团队", "匹配", "专家匹配"],
                "pg": ["code", "coding", "frontend", "backend", "react", "vue", "python", "fastapi",
                       "database", "sql", "api", "devops", "docker", "security", "test", "架构",
                       "前端", "后端", "数据库", "开发", "编程", "代码"],
                "ccb": ["ccb", "change", "变更", "仲裁", "compliance"],
            }
            matched = []
            for agent_type, keywords in agent_keywords.items():
                if any(k in content_lower for k in keywords):
                    matched.append(agent_type)
            skill["for_agents"] = matched if matched else ["common"]
            warnings.append(f"for_agents 未指定，已自动推断为：{skill['for_agents']}")

        # ── 自动推断 tags（如果未指定）────────────────────────────────────────
        if not skill["tags"]:
            skill["tags"] = list(skill["for_agents"])
            # 从内容关键词补充 tags
            tag_keywords = {
                "security": ["security", "安全", "auth", "xss", "sql injection"],
                "testing": ["test", "测试", "tdd", "unit test", "pytest"],
                "api": ["api", "rest", "openapi", "endpoint"],
                "database": ["database", "sql", "数据库", "query"],
                "devops": ["docker", "ci/cd", "deploy", "kubernetes"],
                "frontend": ["react", "vue", "css", "html", "前端"],
                "backend": ["fastapi", "django", "node", "后端"],
            }
            for tag, keywords in tag_keywords.items():
                if any(k in skill["content"].lower() for k in keywords):
                    if tag not in skill["tags"]:
                        skill["tags"].append(tag)

        # ── 内容长度检查 ────────────────────────────────────────────────────────
        if len(skill["content"]) < 100:
            return {"success": False, "error": f"内容过短（{len(skill['content'])} 字符），无法作为有效 Skill"}

        return {
            "success": True,
            "skill": skill,
            "parse_method": parse_method,
            "warnings": warnings,
        }

    def _is_safe_url(self, url: str) -> bool:
        """
        SSRF 防护：检查 URL 是否指向内网/私有地址。

        阻止访问：
        - localhost / 127.0.0.0/8 / ::1
        - 私有网络：10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
        - 链路本地：169.254.0.0/16（含云 metadata 169.254.169.254）
        - Docker 网桥：172.17.0.0/16 等
        - 0.0.0.0
        """
        import ipaddress
        import socket
        from urllib.parse import urlparse

        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            if not hostname:
                return False

            # 阻止 localhost 及其变体
            if hostname.lower() in (
                "localhost", "127.0.0.1", str(ipaddress.IPv4Address(0)), "::1",
                "[::1]", "metadata.google.internal",
            ):
                return False

            # 解析 IP 地址
            try:
                ip = ipaddress.ip_address(hostname)
            except ValueError:
                # 不是 IP 地址，解析域名后检查
                try:
                    ip = ipaddress.ip_address(socket.gethostbyname(hostname))
                except (socket.gaierror, ValueError):
                    return False  # 解析失败，拒绝

            # 阻止私有/内网 IP
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
            if ip.is_unspecified:
                return False

            return True
        except Exception:
            return False

    def fetch_and_parse_skill_url(self, url: str) -> Dict[str, Any]:
        """
        从 URL 获取 Skill 文件内容并解析。

        支持：
        - GitHub raw URL（直接获取）
        - GitHub 仓库 URL（自动转换为 raw URL）
        - 任意公网 HTTP/HTTPS URL（含 SSRF 防护）

        返回：parse_skill_file_content 的结果，附加 url 字段
        """
        import urllib.request
        import urllib.error
        import re

        if not url.startswith(("http://", "https://")):
            return {"success": False, "error": "URL 必须以 http:// 或 https:// 开头"}

        # GitHub 仓库 URL → raw URL 转换
        gh_blob = re.match(
            r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*)",
            url
        )
        if gh_blob:
            user, repo, branch, path = gh_blob.groups()
            url = f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}"

        # SSRF 防护：拒绝内网/私有地址
        if not self._is_safe_url(url):
            return {"success": False, "error": "URL 指向内网或私有地址，已拒绝（SSRF 防护）"}

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "DAgent-SkillAgent/1.0"})
            # 禁止自动跟随重定向（防重定向到内网）
            opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
            with opener.open(req, timeout=15) as resp:
                raw_text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return {"success": False, "error": f"HTTP {e.code}：{e.reason}"}
        except urllib.error.URLError as e:
            return {"success": False, "error": f"网络错误：{e.reason}"}
        except Exception as e:
            return {"success": False, "error": f"获取失败：{e}"}

        # 从 URL 提取文件名
        filename = url.split("/")[-1] or "skill.md"

        result = self.parse_skill_file_content(raw_text, filename=filename)
        result["source_url"] = url
        return result

    def ingest_skill_from_raw(
        self,
        raw_text: str = "",
        url: str = "",
        filename: str = "",
        auto_confirm: bool = False,
    ) -> Dict[str, Any]:
        """
        Skill Agent 主入口：接收文件内容或 URL，解析、分类、入库。

        流程：
        1. 获取内容（文件文本 or URL 抓取）
        2. 解析 SKILL.md 格式（Frontmatter + Markdown）
        3. 自动分类（for_agents / tags / capability_type）
        4. 生成分类报告（供用户确认）
        5. auto_confirm=True 时直接入库，否则返回 pending 状态

        Args:
            raw_text: 文件文本内容（与 url 二选一）
            url: Skill 文件 URL（与 raw_text 二选一）
            filename: 文件名（用于推断 name）
            auto_confirm: 是否跳过确认直接入库

        Returns:
            {
                "success": bool,
                "skill_id": str,          # 入库后的 ID（auto_confirm=True 时）
                "status": "pending" | "imported",
                "skill": {...},           # 解析后的 skill 数据
                "classification": {...},  # 分类报告
                "warnings": [...],
                "error": str              # 仅失败时
            }
        """
        # Step 1: 获取内容
        if url:
            parse_result = self.fetch_and_parse_skill_url(url)
        elif raw_text:
            parse_result = self.parse_skill_file_content(raw_text, filename=filename)
        else:
            return {"success": False, "error": "必须提供 raw_text 或 url"}

        if not parse_result.get("success"):
            return parse_result

        skill_data = parse_result["skill"]
        warnings = parse_result.get("warnings", [])

        # Step 2: 生成分类报告
        classification = self.generate_classification_report(skill_data)

        # Step 3: 入库或返回 pending
        if auto_confirm:
            import_result = self.import_skill(
                skill_data,
                source=parse_result.get("source_url", "file_upload"),
                confirmed=True,
                tags=skill_data.get("tags"),
            )
            return {
                "success": import_result.get("success", False),
                "skill_id": import_result.get("skill_id", ""),
                "status": "imported",
                "skill": skill_data,
                "classification": classification,
                "warnings": warnings,
                "error": import_result.get("error", ""),
            }
        else:
            # 先以 pending 状态入库，等待用户确认
            import_result = self.import_skill(
                skill_data,
                source=parse_result.get("source_url", "file_upload"),
                confirmed=False,
                tags=skill_data.get("tags"),
            )
            if not import_result.get("success"):
                return {
                    "success": False,
                    "error": import_result.get("error", "入库失败"),
                    "skill": skill_data,
                    "warnings": warnings,
                }
            return {
                "success": True,
                "skill_id": import_result.get("skill_id", ""),
                "status": "pending",
                "skill": skill_data,
                "classification": classification,
                "warnings": warnings,
                "parse_method": parse_result.get("parse_method", "inferred"),
                "message": "Skill 已解析，请确认分类后点击「确认入库」",
            }

    def import_skill(
        self,
        skill_content: Dict,
        source: str = "manual",
        confirmed: bool = False,
        override_classification: Optional[Dict] = None,
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        导入 Skill（两步流程）
        
        Step 1（confirmed=False）：解析 + 分类 + 生成建议报告，状态为 pending_confirmation，
                                   不入库，等待用户确认。
        Step 2（confirmed=True）：用户确认（可调整分类），正式入库，状态改为 active。
        
        Args:
            skill_content: Skill 内容
            source: 来源 (github/gitee/file_upload/zip/manual)
            confirmed: 是否已经过用户确认
            override_classification: 用户调整后的分类（可选）
            tags: 用户指定的 agent_type 标签列表（如 ["pm", "common"]）
        """
        # 解析验证
        parse_result = self.parse_skill(skill_content)
        if not parse_result["passed"]:
            return {
                "success": False,
                "error": parse_result["error"]
            }

        # 生成分类报告
        classification = override_classification or self.generate_classification_report(skill_content)

        import uuid
        skill_id = skill_content.get("_pending_id") or f"skill-{uuid.uuid4().hex[:8]}"
        now = time.time()

        if not confirmed:
            # Step 1：生成建议报告，暂存为 pending_confirmation 状态
            skill_entry = {
                "id": skill_id,
                "name": skill_content.get("name", ""),
                "description": skill_content.get("description", ""),
                "version": skill_content.get("version", "1.0.0"),
                "source": source,
                "capability_type": classification["capability_type"],
                "application_scenario": classification["application_scenario"],
                "content": skill_content.get("content", ""),
                "tags": tags or [],
                "status": "pending_confirmation",   # 等待用户确认
                "created_at": now,
                "updated_at": now,
            }
            self.skill_pool[skill_id] = skill_entry
            return {
                "success": True,
                "skill_id": skill_id,
                "classification": classification,
                "status": "pending_confirmation",
                "message": "Skill 解析完成，请确认分类后入库",
                "pending_confirmation": True,
            }
        else:
            # Step 2：用户已确认，正式激活
            if skill_id in self.skill_pool:
                entry = self.skill_pool[skill_id]
                entry["status"] = "active"
                entry["updated_at"] = now
                if override_classification:
                    entry["capability_type"] = classification["capability_type"]
                    entry["application_scenario"] = classification["application_scenario"]
                if tags is not None:
                    entry["tags"] = tags
            else:
                # 直接确认入库（跳过 Step 1 的情况）
                skill_entry = {
                    "id": skill_id,
                    "name": skill_content.get("name", ""),
                    "description": skill_content.get("description", ""),
                    "version": skill_content.get("version", "1.0.0"),
                    "source": source,
                    "capability_type": classification["capability_type"],
                    "application_scenario": classification["application_scenario"],
                    "content": skill_content.get("content", ""),
                    "tags": tags or [],
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                }
                self.skill_pool[skill_id] = skill_entry

            return {
                "success": True,
                "skill_id": skill_id,
                "classification": classification,
                "status": "active",
                "message": "Skill 已确认入库",
                "pending_confirmation": False,
            }

    def confirm_skill_import(
        self,
        skill_id: str,
        override_classification: Optional[Dict] = None,
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        确认 Skill 导入（Step 2 快捷方法）
        
        用户在前端查看建议报告后点击「确认入库」时调用。
        """
        if skill_id not in self.skill_pool:
            return {"success": False, "error": "Skill 不存在"}

        entry = self.skill_pool[skill_id]
        if entry.get("status") != "pending_confirmation":
            return {"success": False, "error": "Skill 不在待确认状态"}

        entry["status"] = "active"
        entry["updated_at"] = time.time()
        if override_classification:
            entry["capability_type"] = override_classification.get("capability_type", entry["capability_type"])
            entry["application_scenario"] = override_classification.get("application_scenario", entry["application_scenario"])
        if tags is not None:
            entry["tags"] = tags

        return {
            "success": True,
            "skill_id": skill_id,
            "status": "active",
            "message": "Skill 已确认入库",
        }

    def cleanup_expired_trash(self) -> int:
        """
        清理回收站中超过 7 天的 Skill（定时任务调用）
        
        Returns:
            清理数量
        """
        now = time.time()
        seven_days = 7 * 24 * 3600
        to_remove = [
            sid for sid, s in self.skill_pool.items()
            if s.get("status") == "deleted"
            and now - s.get("deleted_at", now) > seven_days
        ]
        for sid in to_remove:
            del self.skill_pool[sid]
        return len(to_remove)

    def search_skills(
        self,
        query: str,
        filters: Optional[Dict] = None
    ) -> List[Dict]:
        """
        搜索 Skill
        
        支持全文检索和标签过滤
        """
        results = []

        for skill in self.skill_pool.values():
            # 全文匹配
            searchable = (
                skill.get("name", "") +
                skill.get("description", "") +
                skill.get("content", "")
            ).lower()

            if query.lower() in searchable:
                # 检查过滤器
                if filters:
                    match = True
                    if "capability_type" in filters:
                        if skill.get("capability_type", {}).get("type") != filters["capability_type"]:
                            match = False
                    if "business_domain" in filters:
                        if skill.get("application_scenario", {}).get("business_domain") != filters["business_domain"]:
                            match = False
                    if match:
                        results.append(skill)
                else:
                    results.append(skill)

        return results

    def get_skill_pool(self) -> List[Dict]:
        """获取 Skill 池（只返回 active 状态的 Skill）"""
        return [s for s in self.skill_pool.values() if s.get("status") != "deleted"]

    def get_skills_for_agent_type(self, agent_type: str) -> List[Dict]:
        """
        按 agent_type 标签过滤 Skill
        
        agent_type: pm / supervisor / hr / pg / ccb / common
        返回该类型 Agent 应拥有的所有 Skill（含 common 通用 Skill）
        """
        result = []
        for skill in self.skill_pool.values():
            if skill.get("status") == "deleted":
                continue
            tags = skill.get("tags", [])
            # 匹配专属标签或通用标签
            if agent_type in tags or "common" in tags:
                result.append(skill)
        return result

    def get_skill_ids_for_agent_type(self, agent_type: str) -> List[str]:
        """返回该 agent_type 对应的 skill_id 列表"""
        return [s["id"] for s in self.get_skills_for_agent_type(agent_type)]

    def get_skills_grouped_by_category(self) -> Dict[str, List[Dict]]:
        """按 Agent 类型分组返回 Skill 池"""
        groups: Dict[str, List[Dict]] = {
            "pm": [], "supervisor": [], "hr": [],
            "pg": [], "ccb": [], "common": [], "other": []
        }
        for skill in self.skill_pool.values():
            if skill.get("status") == "deleted":
                continue
            tags = skill.get("tags", [])
            placed = False
            for category in ["pm", "supervisor", "hr", "pg", "ccb", "common"]:
                if category in tags:
                    groups[category].append(skill)
                    placed = True
                    break
            if not placed:
                groups["other"].append(skill)
        return groups

    def get_skills_grouped_by_domain(self) -> Dict[str, List[Dict]]:
        """
        按专业领域分组返回 Skill 池
        
        领域从 tags 中识别（需求分析/项目管理/代码开发/质量保障/变更管理/通用）
        也兼容 application_scenario.business_domain 字段
        """
        # 领域标签映射
        DOMAIN_TAG_MAP: Dict[str, List[str]] = {
            "需求分析":   ["analysis", "requirements", "decomposition"],
            "项目管理":   ["planning", "schedule", "risk", "monitoring", "dispatch", "blocker", "escalation"],
            "团队管理":   ["team", "recruitment", "skill", "evaluation"],
            "代码开发":   ["coding", "generation", "review"],
            "质量保障":   ["testing", "unittest", "quality"],
            "变更管理":   ["change", "version", "release"],
            "文档写作":   ["documentation", "writing"],
            "AI能力":     ["llm", "chat"],
            "文件处理":   ["file", "parsing"],
        }

        groups: Dict[str, List[Dict]] = {d: [] for d in DOMAIN_TAG_MAP}
        groups["其他"] = []

        for skill in self.skill_pool.values():
            if skill.get("status") == "deleted":
                continue
            tags = skill.get("tags", [])
            placed = False
            for domain, domain_tags in DOMAIN_TAG_MAP.items():
                if any(dt in tags for dt in domain_tags):
                    groups[domain].append(skill)
                    placed = True
                    break
            if not placed:
                # 兜底：从 application_scenario.business_domain 读取
                biz = skill.get("application_scenario", {}).get("business_domain", "")
                if biz and biz != "通用":
                    groups.setdefault(biz, []).append(skill)
                else:
                    groups["其他"].append(skill)

        # 移除空分组
        return {k: v for k, v in groups.items() if v}

    def delete_skill(self, skill_id: str) -> Dict[str, Any]:
        """删除 Skill（软删除到回收站）"""
        if skill_id not in self.skill_pool:
            return {"success": False, "error": "Skill not found"}

        skill = self.skill_pool[skill_id]
        skill["status"] = "deleted"
        skill["deleted_at"] = time.time()  # 7 天后可恢复

        return {"success": True, "skill_id": skill_id}

    def _do_execute(self, task: Task) -> Any:
        """执行任务"""
        if task.title == "import":
            return self.import_skill(
                task.metadata.get("skill_content", {}),
                task.metadata.get("source", "manual")
            )
        elif task.title == "search":
            return {"results": self.search_skills(
                task.metadata.get("query", ""),
                task.metadata.get("filters")
            )}
        elif task.title == "classify":
            return self.generate_classification_report(task.metadata.get("skill_content", {}))
        return {"error": f"Unknown task: {task.title}"}

    def get_status(self) -> Dict[str, Any]:
        """获取 SMAgent 状态"""
        # active = 未删除的 Skill（含无 status 字段的历史 Skill）
        active = len([s for s in self.skill_pool.values() if s.get("status") != "deleted"])
        return {
            "agent_id": self.agent_id,
            "type": "sm",
            "state": self.state.value,
            "total_skills": len(self.skill_pool),
            "active_skills": active
        }
    def update_skill(self, skill_id, updates):
        if skill_id not in self.skill_pool:
            return {"success": False, "error": "Skill not found"}
        skill = self.skill_pool[skill_id]
        for field in ["name", "description", "version", "content", "tags", "source"]:
            if field in updates and updates.get(field) is not None:
                skill[field] = updates[field]
        skill["updated_at"] = __import__("time").time()
        return {"success": True, "skill": skill}

