"""
MCP Server 实现
遵循 Model Context Protocol (MCP) 规范
提供三个核心工具：task_execute / file_operate / agent_query
"""

import json
from typing import Dict, Any, Optional

MCP_VERSION = "2024-11-05"

TOOL_DEFINITIONS = [
    {
        "name": "task_execute",
        "description": "在 AI Agent 系统中执行任务。支持创建项目、分析需求、生成规划书、组建团队、触发质检、签核等操作。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "create_project",
                        "analyze_requirements",
                        "generate_plan",
                        "build_team",
                        "trigger_qc",
                        "sign_off",
                        "create_task"
                    ],
                    "description": "操作类型"
                },
                "project_id": {
                    "type": "string",
                    "description": "项目 ID（create_project 时不需要）"
                },
                "params": {
                    "type": "object",
                    "description": "操作参数",
                    "additionalProperties": True
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_operate",
        "description": "对项目文件进行读取、写入、列出操作，所有操作在项目沙箱目录内进行。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["read", "write", "list"],
                    "description": "文件操作类型"
                },
                "project_id": {
                    "type": "string",
                    "description": "项目 ID"
                },
                "path": {
                    "type": "string",
                    "description": "文件路径（相对于项目目录），list 时可省略"
                },
                "content": {
                    "type": "string",
                    "description": "写入内容（仅 write 需要）"
                }
            },
            "required": ["operation", "project_id"]
        }
    },
    {
        "name": "agent_query",
        "description": "查询 Agent 系统状态，包括项目列表、Agent 状态、任务进度、Skill 池等。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query_type": {
                    "type": "string",
                    "enum": [
                        "list_projects",
                        "get_project",
                        "list_agents",
                        "get_progress",
                        "list_skills",
                        "get_team_status",
                        "list_tasks",
                        "get_qc_results"
                    ],
                    "description": "查询类型"
                },
                "project_id": {
                    "type": "string",
                    "description": "项目 ID（部分查询需要）"
                },
                "params": {
                    "type": "object",
                    "description": "额外查询参数",
                    "additionalProperties": True
                }
            },
            "required": ["query_type"]
        }
    }
]


def _ok(content: Any) -> Dict:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _err(msg: str) -> Dict:
    return {"content": [{"type": "text", "text": f"Error: {msg}"}], "isError": True}


class MCPToolHandler:
    """将 MCP 工具调用转发到后端逻辑"""

    def __init__(self, projects: Dict, global_sm_agent: Any):
        self.projects = projects
        self.sm = global_sm_agent

    # ── task_execute ──────────────────────────────────────────────────────────

    def handle_task_execute(self, args: Dict) -> Dict:
        action = args.get("action", "")
        project_id = args.get("project_id", "")
        params = args.get("params", {})

        try:
            if action == "create_project":
                return _ok({
                    "hint": "请调用 POST /projects 创建项目",
                    "api_endpoint": "POST /projects",
                    "body": {
                        "name": params.get("name", "未命名项目"),
                        "description": params.get("description", "")
                    }
                })

            ctx = self.projects.get(project_id)
            if ctx is None:
                return _err(f"项目 {project_id} 不存在")

            if action == "analyze_requirements":
                result = ctx.pm.analyze_requirement(params.get("requirements", ""))
                return _ok(result)

            elif action == "generate_plan":
                result = ctx.pm.generate_plan()
                return _ok(result)

            elif action == "build_team":
                if not ctx.pm.current_plan:
                    return _err("请先执行 generate_plan")
                result = ctx.hr.build_team(ctx.pm.current_plan)
                return _ok(result)

            elif action == "trigger_qc":
                sp_id = params.get("subproject_id", "")
                result = ctx.supervisor.trigger_all_quality_checks(sp_id)
                return _ok(result)

            elif action == "sign_off":
                result = ctx.supervisor.sign_off(project_id)
                return _ok(result)

            elif action == "create_task":
                from agents.base.hermes_agent import AgentType
                try:
                    agent_type = AgentType(params.get("agent_type", "employee"))
                except ValueError as e:
                    return _err(str(e))
                task = ctx.supervisor.create_task(
                    title=params.get("title", ""),
                    description=params.get("description", ""),
                    agent_type=agent_type,
                    priority=params.get("priority", 2)
                )
                return _ok(task.to_dict())

            return _err(f"未知操作: {action}")

        except Exception as e:
            return _err(str(e))

    # ── file_operate ──────────────────────────────────────────────────────────

    def handle_file_operate(self, args: Dict) -> Dict:
        operation = args.get("operation", "")
        project_id = args.get("project_id", "")
        path = args.get("path", "")
        content = args.get("content", "")

        try:
            ctx = self.projects.get(project_id)
            if ctx is None:
                return _err(f"项目 {project_id} 不存在")

            if operation == "list":
                files = ctx.pg.get_project_files(project_id)
                return _ok({"files": files, "count": len(files)})

            elif operation == "read":
                if not path:
                    return _err("read 操作需要 path 参数")
                return _ok(ctx.pg.read_file(project_id, path))

            elif operation == "write":
                if not path:
                    return _err("write 操作需要 path 参数")
                return _ok(ctx.pg.write_file(project_id, path, content))

            return _err(f"未知操作: {operation}")

        except Exception as e:
            return _err(str(e))

    # ── agent_query ───────────────────────────────────────────────────────────

    def handle_agent_query(self, args: Dict) -> Dict:
        query_type = args.get("query_type", "")
        project_id = args.get("project_id", "")
        params = args.get("params", {})

        try:
            if query_type == "list_projects":
                return _ok({
                    "projects": [
                        {"id": pid, "name": c.name, "status": c.status,
                         "agents_count": len(c.agents), "subprojects_count": len(c.subprojects)}
                        for pid, c in self.projects.items()
                    ]
                })

            elif query_type == "list_skills":
                skills = self.sm.get_skill_pool()
                return _ok({"skills": skills, "count": len(skills)})

            ctx = self.projects.get(project_id)
            if ctx is None:
                return _err(f"项目 {project_id} 不存在，请提供 project_id")

            if query_type == "get_project":
                return _ok(ctx.to_dict())

            elif query_type == "list_agents":
                return _ok({"core_agents": ctx._core_agents_info(), "exec_agents": list(ctx.agents.values())})

            elif query_type == "get_progress":
                return _ok(ctx.supervisor.get_progress())

            elif query_type == "get_team_status":
                return _ok(ctx.hr.get_team_status())

            elif query_type == "list_tasks":
                tasks = ctx.supervisor.dispatcher.dispatcher.queue.list_all()
                return _ok({"tasks": [t.to_dict() for t in tasks]})

            elif query_type == "get_qc_results":
                sp_id = params.get("subproject_id", "")
                tasks = ctx.supervisor.dispatcher.dispatcher.queue.list_all()
                qc = [t.to_dict() for t in tasks
                      if t.title.startswith(("QA-", "Perf-", "Sec-", "UXO-")) and sp_id in t.title]
                return _ok({"subproject_id": sp_id, "qc_tasks": qc})

            return _err(f"未知查询类型: {query_type}")

        except Exception as e:
            return _err(str(e))

    # ── dispatch ──────────────────────────────────────────────────────────────

    def dispatch(self, tool_name: str, arguments: Dict) -> Dict:
        if tool_name == "task_execute":
            return self.handle_task_execute(arguments)
        elif tool_name == "file_operate":
            return self.handle_file_operate(arguments)
        elif tool_name == "agent_query":
            return self.handle_agent_query(arguments)
        return _err(f"未知工具: {tool_name}")


def handle_mcp_request(body: Dict, handler: MCPToolHandler) -> Optional[Dict]:
    """处理 MCP JSON-RPC 2.0 请求"""
    method = body.get("method", "")
    req_id = body.get("id")
    params = body.get("params", {})

    def ok(result: Any) -> Dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code: int, msg: str) -> Dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}

    if method == "initialize":
        return ok({
            "protocolVersion": MCP_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": "ai-agent-system",
                "version": "0.3.0",
                "description": "AI Multi-Agent Project Management System"
            }
        })

    elif method == "ping":
        return ok({})

    elif method == "tools/list":
        return ok({"tools": TOOL_DEFINITIONS})

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        if not tool_name:
            return err(-32602, "缺少 name 参数")
        return ok(handler.dispatch(tool_name, arguments))

    elif method == "notifications/initialized":
        return None  # 通知类消息无需响应

    else:
        return err(-32601, f"Method not found: {method}")
