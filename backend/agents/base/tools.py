"""
工具基类
定义 Agent 可使用的标准工具
"""

import os
import json
import subprocess
from typing import List, Dict, Any, Optional, Callable
from abc import ABC, abstractmethod
from pathlib import Path
import hashlib
import time


class ToolBase(ABC):
    """
    工具基类
    
    所有 Agent 工具的抽象基类
    """

    name: str = ""
    description: str = ""
    
    def __init__(self):
        self.enabled = True

    @abstractmethod
    def execute(self, **kwargs) -> Any:
        """执行工具"""
        pass

    def validate(self, **kwargs) -> bool:
        """验证参数"""
        return True

    def get_schema(self) -> Dict:
        """获取工具参数 schema"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {"type": "object", "properties": {}}
        }


class FileTool(ToolBase):
    """文件操作工具"""

    name = "file_operations"
    description = "读取、写入、搜索文件"

    def __init__(self, workspace_path: str = "projects"):
        super().__init__()
        self.workspace = Path(workspace_path)

    def _resolve_within_workspace(self, path: str) -> Path:
        workspace = self.workspace.resolve()
        target_path = (self.workspace / path).resolve()
        try:
            target_path.relative_to(workspace)
        except ValueError:
            raise ValueError(f"路径越界：{path}")
        return target_path

    def execute(self, operation: str, path: str, content: str = None, **kwargs) -> Any:
        """执行文件操作"""
        try:
            target_path = self._resolve_within_workspace(path)
        except ValueError as e:
            return {"error": str(e), "path": path}

        if operation == "read":
            if not target_path.exists():
                return {"error": f"File not found: {path}"}
            with open(target_path, "r", encoding="utf-8") as f:
                return {"content": f.read(), "path": path}

        elif operation == "write":
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(content or "")
            return {"success": True, "path": path}

        elif operation == "exists":
            return {"exists": target_path.exists()}

        elif operation == "list":
            if not target_path.is_dir():
                return {"error": "Not a directory", "path": path}
            return {
                "files": [
                    {"name": p.name, "type": "dir" if p.is_dir() else "file"}
                    for p in target_path.iterdir()
                ]
            }

        elif operation == "delete":
            if target_path.exists():
                if target_path.is_dir():
                    import shutil
                    shutil.rmtree(target_path)
                else:
                    target_path.unlink()
            return {"success": True, "path": path}

        return {"error": f"Unknown operation: {operation}"}

    def get_schema(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["read", "write", "exists", "list", "delete"]
                    },
                    "path": {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["operation", "path"]
            }
        }


class ShellTool(ToolBase):
    """Shell 命令执行工具"""

    name = "shell"
    description = "执行 Shell 命令"

    def __init__(self, cwd: str = "."):
        super().__init__()
        self.cwd = Path(cwd)

    def execute(self, command: str, timeout: int = 60, **kwargs) -> Dict:
        """执行命令（使用 shell=False 避免注入，参数用 shlex 安全分割）"""
        import shlex
        try:
            # 安全解析命令字符串为参数列表（非 shell 模式，防命令注入）
            cmd_list = shlex.split(command)
            result = subprocess.run(
                cmd_list,
                shell=False,
                cwd=str(self.cwd),
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode
            }
        except subprocess.TimeoutExpired:
            return {"error": "Command timed out", "timeout": timeout}
        except Exception as e:
            return {"error": str(e)}

    def get_schema(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "default": 60}
                },
                "required": ["command"]
            }
        }


class GlobTool(ToolBase):
    """文件搜索工具"""

    name = "glob"
    description = "按模式搜索文件"

    def __init__(self, workspace_path: str = "projects"):
        super().__init__()
        self.workspace = Path(workspace_path)

    def execute(self, pattern: str, path: str = "", **kwargs) -> Dict:
        """搜索文件"""
        import glob

        try:
            workspace = self.workspace.resolve()
            search_path = (self.workspace / path).resolve() if path else workspace
            search_path.relative_to(workspace)
        except ValueError:
            return {"error": f"路径越界：{path}", "matches": [], "count": 0}
        matches = list(search_path.glob(pattern))

        return {
            "matches": [str(p.relative_to(workspace)) for p in matches],
            "count": len(matches)
        }

    def get_schema(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"}
                },
                "required": ["pattern"]
            }
        }


class GrepTool(ToolBase):
    """文本搜索工具"""

    name = "grep"
    description = "在文件中搜索文本"

    def __init__(self, workspace_path: str = "projects"):
        super().__init__()
        self.workspace = Path(workspace_path)

    def execute(self, pattern: str, path: str = "", recursive: bool = True, **kwargs) -> Dict:
        """搜索文本"""
        results = []
        try:
            workspace = self.workspace.resolve()
            search_path = (self.workspace / path).resolve() if path else workspace
            search_path.relative_to(workspace)
        except ValueError:
            return {"error": f"路径越界：{path}", "results": [], "count": 0}

        if recursive and search_path.is_dir():
            for txt_file in search_path.rglob("*"):
                if txt_file.is_file():
                    try:
                        content = txt_file.read_text(encoding="utf-8")
                        for i, line in enumerate(content.split("\n"), 1):
                            if pattern in line:
                                results.append({
                                    "file": str(txt_file.relative_to(workspace)),
                                    "line": i,
                                    "content": line.strip()
                                })
                    except Exception:
                        pass
        else:
            try:
                content = search_path.read_text(encoding="utf-8")
                for i, line in enumerate(content.split("\n"), 1):
                    if pattern in line:
                        results.append({
                            "file": str(search_path.relative_to(workspace)),
                            "line": i,
                            "content": line.strip()
                        })
            except Exception:
                pass

        return {"results": results, "count": len(results)}


class WebSearchTool(ToolBase):
    """网络搜索工具"""

    name = "web_search"
    description = "搜索网络信息"

    def __init__(self):
        super().__init__()

    def execute(self, query: str, num_results: int = 5, **kwargs) -> Dict:
        """
        搜索网络
        
        实际应用中需要接入真实的搜索 API
        """
        # TODO: 实现真实搜索
        return {
            "query": query,
            "results": [
                {"title": "示例结果 1", "url": "https://example.com/1", "snippet": "..."},
                {"title": "示例结果 2", "url": "https://example.com/2", "snippet": "..."},
            ],
            "count": num_results
        }

    def get_schema(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "num_results": {"type": "integer", "default": 5}
                },
                "required": ["query"]
            }
        }


class WebFetchTool(ToolBase):
    """网页获取工具"""

    name = "web_fetch"
    description = "获取网页内容"

    def __init__(self):
        super().__init__()

    def execute(self, url: str, **kwargs) -> Dict:
        """
        获取网页
        
        实际应用中需要处理重定向、编码等
        """
        # TODO: 实现真实网页获取
        return {
            "url": url,
            "content": "网页内容...",
            "status": 200
        }


class ToolRegistry:
    """
    工具注册表
    
    管理所有可用工具，支持按 Agent 类型分配
    """

    # 工具分组
    BASIC_TOOLS = ["file_operations", "shell", "glob", "grep"]
    WEB_TOOLS = ["web_search", "web_fetch"]

    def __init__(self):
        self._tools: Dict[str, ToolBase] = {}
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """注册默认工具"""
        self.register(FileTool())
        self.register(ShellTool())
        self.register(GlobTool())
        self.register(GrepTool())
        self.register(WebSearchTool())
        self.register(WebFetchTool())

    def register(self, tool: ToolBase) -> None:
        """注册工具"""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[ToolBase]:
        """获取工具"""
        return self._tools.get(name)

    def get_all(self) -> List[ToolBase]:
        """获取所有工具"""
        return list(self._tools.values())

    def get_for_agent(self, agent_type: str) -> List[ToolBase]:
        """根据 Agent 类型获取工具"""
        if agent_type == "employee":
            return [self._tools[t] for t in self.BASIC_TOOLS if t in self._tools]
        elif agent_type == "supervisor":
            return list(self._tools.values())
        else:
            return []

    def list_names(self) -> List[str]:
        """列出工具名"""
        return list(self._tools.keys())


class SelfCheckTool:
    """
    Self-Check 自检工具
    
    员工 Agent 内置的阻断式自检
    """

    def __init__(self):
        self.blockers: List[str] = []

    def check(self, task_result: Dict) -> Dict[str, Any]:
        """
        执行自检
        
        Args:
            task_result: 任务执行结果
            
        Returns:
            检查结果
        """
        self.blockers = []

        # 检查边界条件处理
        self._check_edge_cases(task_result)

        # 检查错误处理
        self._check_error_handling(task_result)

        # 检查性能隐患
        self._check_performance(task_result)

        if self.blockers:
            return {
                "passed": False,
                "blockers": self.blockers,
                "message": f"发现 {len(self.blockers)} 个阻断项"
            }

        return {
            "passed": True,
            "blockers": [],
            "message": "自检通过"
        }

    def _check_edge_cases(self, result: Dict) -> None:
        """检查边界条件"""
        # TODO: 实现边界条件检查
        pass

    def _check_error_handling(self, result: Dict) -> None:
        """检查错误处理"""
        # TODO: 实现错误处理检查
        pass

    def _check_performance(self, result: Dict) -> None:
        """检查性能隐患"""
        # TODO: 实现性能检查
        pass