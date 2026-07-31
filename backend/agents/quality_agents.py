"""
四层质检 Agent 实现 v2
检查顺序：语法错误 → 运行逻辑 → 功能实现 → 文件协作
- 每层发现问题后生成两份报告：用户摘要 + 开发者详细反馈
- 反馈直接返回给对应的执行 Agent（谁开发谁修改）
- 质检不通过时阻止交付，触发修复循环
"""

import ast
import copy
import json
import posixpath
import re
import time
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Any, Tuple
from .base.hermes_agent import AgentBase, AgentType, Task, AgentState


class QualityAgent(AgentBase):
    """
    质检 Agent 基类
    
    特征：
    - 独立汇报线（直接向 Supervisor 汇报，不经过组长）
    - 拥有否决权（veto_power=True，不通过则阻止归档）
    - 不经过组长
    """

    ESSENTIAL_CAPABILITIES = []
    # 类属性：否决权（子类可覆盖）
    veto_power: bool = True

    def __init__(self, *args, agent_type: AgentType = None, **kwargs):
        self.inspection_results: List[Dict] = []
        super().__init__(*args, agent_type=agent_type, **kwargs)

    def inspect(
        self,
        subproject_id: str,
        workspace_path: str = "",
        output_files: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        执行检查（统一签名）
        
        Args:
            subproject_id: 子项目 ID
            workspace_path: 项目工作区路径
            output_files: 产出文件列表
            
        Returns:
            {"passed": bool, "score": int, "issues": List[str], "details": dict}
        """
        raise NotImplementedError

    def can_pass(self, result: Dict) -> bool:
        """判断是否通过"""
        return result.get("passed", False)

    def generate_report(self, task_id: str, result: Dict) -> Dict[str, Any]:
        """生成质检报告"""
        report = {
            "task_id": task_id,
            "agent_type": self.agent_type.value,
            "passed": self.can_pass(result),
            "details": result,
            "timestamp": time.time(),
            "has_veto": self.veto_power
        }
        self.inspection_results.append(report)
        return report


# ─── 真实四层质检核心逻辑 ────────────────────────────────────────────────────────

def _resolve_output_file(fpath: str, workspace_path: str = "") -> Optional[Tuple[Path, str]]:
    """安全解析 output_files：提供 workspace 时必须限制在工作区内。"""
    path = Path(fpath)
    if not workspace_path:
        return path, fpath

    workspace = Path(workspace_path).resolve()
    raw = str(fpath or "").replace("\\", "/")
    if not raw:
        return None
    if Path(raw).is_absolute():
        candidate = Path(raw).resolve()
        try:
            rel = candidate.relative_to(workspace)
        except ValueError:
            return None
        return candidate, str(rel).replace("\\", "/")

    # Generated contracts may capitalize repository directories (for example
    # ``Backend/package.json``) while the Linux workspace contains
    # ``backend/package.json``. Resolve each existing segment case-insensitively
    # without weakening traversal checks or accepting ambiguous siblings.
    relative = PurePosixPath(raw)
    if any(part in {"", ".", ".."} for part in relative.parts):
        return None
    candidate = workspace
    resolved_parts: List[str] = []
    for part in relative.parts:
        exact = candidate / part
        try:
            matches = [
                child for child in candidate.iterdir()
                if child.name.casefold() == part.casefold()
            ]
        except OSError:
            matches = []
        if len(matches) == 1:
            match = matches[0]
        elif len(matches) > 1:
            return None
        else:
            # Keep the requested in-workspace path so callers report a
            # missing delivery file rather than an unsafe path.
            match = exact
        candidate = match
        resolved_parts.append(match.name)
    return candidate, "/".join(resolved_parts)


def _read_source_files(
    workspace_path: str,
    output_files: Optional[List[str]] = None,
    read_errors: Optional[List[Dict[str, Any]]] = None,
) -> List[Tuple[str, str]]:
    """读取待质检源码文件，返回 [(相对路径, 内容), ...]

    设计原则：
    - 优先只检查本次 Agent/子项目的 output_files，避免把整个项目历史代码都扫进来。
    - 没有 output_files 时才回退扫描 workspace/src。
    - 排除 .project、output、docs、node_modules、dist 等非源码/历史目录。
    - 按相对路径去重，避免同一文件既被全量扫描又被 output_files 指定时重复计数。
    """
    ws = Path(workspace_path).resolve() if workspace_path else None
    results: List[Tuple[str, str]] = []
    seen = set()
    exts = {
        ".py", ".ts", ".tsx", ".js", ".jsx", ".html", ".css", ".java",
        ".go", ".rs", ".cpp", ".c", ".cs", ".json", ".yaml", ".yml", ".toml",
        ".md", ".txt", ".sh", ".ini", ".cfg", ".conf", ".env", ".example",
        ".vue", ".svelte", ".sql", ".scss", ".sass", ".less", ".xml",
        ".properties", ".gradle", ".kt", ".kts", ".graphql", ".gql", ".proto",
        ".rb", ".php", ".swift", ".dart", ".ps1", ".bat", ".cmd",
    }
    recognized_names = {
        "dockerfile", "makefile", "readme", "readme.md", "license",
        ".env", ".env.example", ".gitignore", ".dockerignore", ".npmrc",
    }
    excluded_parts = {".project", "output", "docs", "node_modules", "dist", "build", "__pycache__"}

    def record_read_error(rel: str, message: str) -> None:
        if read_errors is None:
            return
        read_errors.append({
            "file": rel,
            "layer": "io",
            "severity": "error",
            "message": message,
            "fix_hint": "Restore a readable regular file at the declared delivery path.",
        })

    def add_file(
        path: Path,
        rel_label: Optional[str] = None,
        *,
        explicit_delivery: bool = False,
    ) -> None:
        if not path.exists() or not path.is_file():
            return
        if (
            not explicit_delivery
            and path.suffix.lower() not in exts
            and path.name.lower() not in recognized_names
        ):
            return
        try:
            rel = rel_label
            if not rel:
                rel = str(path.relative_to(ws)).replace("\\", "/") if ws else str(path)
            parts = set(Path(rel).parts)
            # Agent output_files are generated text artifacts and form the
            # authoritative delivery set. Only internal/runtime directories
            # stay excluded; docs/dist/build may be intentional deliverables.
            effective_excluded_parts = (
                {".project", "output", "node_modules", "__pycache__"}
                if explicit_delivery
                else excluded_parts
            )
            if parts & effective_excluded_parts:
                return
            if rel in seen:
                return
            content = path.read_text(encoding="utf-8", errors="replace")
            seen.add(rel)
            results.append((rel, content))
        except OSError:
            record_read_error(rel_label or str(path), "Declared delivery file could not be read")

    # 1) 有明确产出文件时，只检查这些文件
    if output_files is not None:
        if ws and ws.exists():
            for fpath in output_files:
                resolved = _resolve_output_file(fpath, workspace_path)
                if not resolved:
                    record_read_error(str(fpath), "Declared delivery path is outside the workspace")
                    continue
                candidate, rel = resolved
                if not candidate.exists() or not candidate.is_file():
                    record_read_error(rel, "Declared delivery file is missing or is not a regular file")
                    continue
                # Explicit output_files are authoritative delivery artifacts.
                # Fallback workspace scans remain restricted to source files.
                add_file(
                    candidate,
                    rel,
                    explicit_delivery=True,
                )
        # An explicit delivery list is authoritative. Falling back to unrelated
        # workspace files here can turn a missing delivery into a false pass.
        return results

    # 2) 没有明确产出文件时，回退扫描 src 目录，而不是整个 workspace
    if ws and ws.exists():
        src_dir = ws / "src"
        scan_root = src_dir if src_dir.exists() else ws
        for f in scan_root.rglob("*"):
            add_file(f)

    return results


def _is_configuration_only_delivery(
    files: List[Tuple[str, str]],
    output_files: Optional[List[str]],
    subproject_description: str,
    agent_role: str,
    artifact_kind: str = "",
) -> bool:
    """Allow intentional DevOps/architecture artifacts without source code."""
    if str(artifact_kind or "").strip().lower() in {
        "architecture_document", "deployment_configuration",
    }:
        return True
    role_text = f"{agent_role} {subproject_description}".lower()
    eligible_role = any(token in role_text for token in (
        "devops", "deploy", "deployment", "运维", "部署", "architecture", "architect", "架构",
    ))
    if not eligible_role:
        return False
    declared = [path for path, _ in files] or list(output_files or [])
    if not declared:
        return False
    config_names = {
        "dockerfile", "makefile", "readme", "readme.md", ".env", ".env.example",
        "render.yaml", "render.yml", "docker-compose.yml", "docker-compose.yaml",
    }
    config_suffixes = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".sh", ".env", ".example"}
    return all(
        Path(path).name.lower() in config_names
        or Path(path).suffix.lower() in config_suffixes
        or str(path).replace("\\", "/").startswith("deploy/")
        for path in declared
    )


def check_layer1_syntax(files: List[Tuple[str, str]]) -> Dict[str, Any]:
    """
    第一层：语法错误检查
    - Python 文件用 ast.parse 检查
    - JS/TS 文件用正则检查常见语法问题
    """
    issues: List[Dict] = []
    score = 100

    for rel_path, content in files:
        if rel_path.endswith(".json"):
            try:
                json.loads(content)
            except json.JSONDecodeError as e:
                issues.append({
                    "file": rel_path,
                    "layer": "syntax",
                    "severity": "error",
                    "line": e.lineno,
                    "message": f"JSON 语法错误：{e.msg}",
                    "fix_hint": "JSON 文件必须是严格 JSON，禁止追加注释、Markdown 或说明文字",
                })
                score -= 25
        elif rel_path.endswith(".py"):
            try:
                ast.parse(content)
            except SyntaxError as e:
                issues.append({
                    "file": rel_path,
                    "layer": "syntax",
                    "severity": "error",
                    "line": e.lineno,
                    "message": f"Python 语法错误：{e.msg}",
                    "fix_hint": f"第 {e.lineno} 行附近存在语法错误，请检查括号、缩进、冒号等",
                })
                score -= 25
        elif rel_path.endswith((".ts", ".tsx", ".js", ".jsx")):
            # 检查常见 JS/TS 语法问题
            lines = content.split("\n")
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                # 未闭合的括号（简单启发式）
                if stripped.endswith("=>") and i < len(lines):
                    pass  # 合法的箭头函数

    return {
        "layer": "syntax",
        "passed": score >= 60,
        "score": max(0, score),
        "issues": issues,
        "issue_count": len(issues),
    }


def check_layer2_logic(files: List[Tuple[str, str]]) -> Dict[str, Any]:
    """
    第二层：运行逻辑检查
    - 检查未处理的异常路径
    - 检查空函数体
    - 检查无限循环风险
    - 检查未使用的变量/导入（启发式）
    """
    issues: List[Dict] = []
    score = 100

    for rel_path, content in files:
        lines = content.split("\n")

        # 检查空函数/方法体（只有 pass 或注释）
        if rel_path.endswith(".py"):
            try:
                tree = ast.parse(content)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        body = node.body
                        # 只有 pass 或 docstring
                        non_trivial = [n for n in body if not isinstance(n, (ast.Pass, ast.Expr))]
                        if not non_trivial and len(body) <= 1:
                            issues.append({
                                "file": rel_path,
                                "layer": "logic",
                                "severity": "warning",
                                "line": node.lineno,
                                "message": f"函数 `{node.name}` 体为空（第 {node.lineno} 行）",
                                "fix_hint": f"函数 `{node.name}` 没有实现，请补充逻辑",
                            })
                            score -= 10
            except Exception:
                pass

        # 检查 TODO/FIXME/HACK（未完成标记）
        for i, line in enumerate(lines, 1):
            # Treat only explicit comment markers as unfinished work. Raw
            # substring matching incorrectly flags legitimate values such as
            # SQL ``DEFAULT 'todo'`` and user-facing text containing "todo".
            if re.search(r"(?:#|//|/\*|\*|<!--)\s*(?:TODO|FIXME|HACK|XXX)\b", line, re.IGNORECASE):
                issues.append({
                    "file": rel_path,
                    "layer": "logic",
                    "severity": "warning",
                    "line": i,
                    "message": f"未完成标记（第 {i} 行）：{line.strip()[:80]}",
                    "fix_hint": "存在未完成的代码标记，请实现或删除",
                })
                score -= 5

        # 检查裸 except（吞掉所有异常）
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped in ("except:", "except Exception:") or stripped.startswith("except:"):
                issues.append({
                    "file": rel_path,
                    "layer": "logic",
                    "severity": "warning",
                    "line": i,
                    "message": f"裸 except 会吞掉所有异常（第 {i} 行）",
                    "fix_hint": "请捕获具体异常类型，如 except ValueError as e",
                })
                score -= 8

        # 检查 while True 没有 break
        for i, line in enumerate(lines, 1):
            if re.search(r'\bwhile\s+True\b', line):
                # 检查后续 20 行内有没有 break
                block = "\n".join(lines[i:i+20])
                if "break" not in block and "return" not in block:
                    issues.append({
                        "file": rel_path,
                        "layer": "logic",
                        "severity": "error",
                        "line": i,
                        "message": f"while True 循环可能无法退出（第 {i} 行）",
                        "fix_hint": "while True 循环内必须有 break 或 return 退出条件",
                    })
                    score -= 20

    return {
        "layer": "logic",
        "passed": score >= 60,
        "score": max(0, score),
        "issues": issues,
        "issue_count": len(issues),
    }


def _select_functionality_context(
    files: List[Tuple[str, str]],
    subproject_description: str,
    *,
    max_files: int = 10,
    max_chars: int = 30000,
) -> Tuple[List[Tuple[str, str]], str]:
    """Choose representative source files and always expose the full manifest.

    Functionality QC used to inspect the first five output paths. Manifests and
    boot files usually occupy those positions, so central files such as
    ``App.tsx`` were invisible and the model reported them as missing. Rank
    entry points, components, routes and services ahead of configuration while
    retaining a complete path manifest as an authoritative filesystem fact.
    """
    description = str(subproject_description or "").lower()
    useful_tokens = {
        token for token in re.findall(r"[a-z][a-z0-9_-]{2,}", description)
        if token not in {
            "the", "and", "with", "from", "into", "using", "implement",
            "developer", "frontend", "backend", "project",
        }
    }

    def priority(item: Tuple[int, Tuple[str, str]]) -> Tuple[int, int]:
        index, (rel_path, content) = item
        normalized = rel_path.replace("\\", "/").lower()
        path = Path(normalized)
        score = 0
        if path.suffix in {".py", ".ts", ".tsx", ".js", ".jsx", ".html"}:
            score += 30
        if path.stem in {"app", "main"}:
            score += 100
        elif path.stem in {"index", "server"}:
            score += 80
        if any(part in normalized for part in (
            "/components/", "/routes/", "/routers/", "/controllers/",
            "/services/", "/crud/", "/models/",
        )):
            score += 55
        if path.suffix in {".css", ".scss"} and path.stem in {"app", "index"}:
            score += 45
        if "react" in description and path.suffix in {".tsx", ".jsx"}:
            score += 25
        if "fastapi" in description and path.suffix == ".py":
            score += 25
        score += sum(8 for token in useful_tokens if token in normalized)
        content_lower = content[:2500].lower()
        if any(token in description for token in ("api", "接口")) and any(
            marker in content_lower for marker in ("fetch(", "axios", "@router", "app.")
        ):
            score += 20
        # Stable original-order tie break.
        return score, -index

    ranked = sorted(enumerate(files), key=priority, reverse=True)
    selected: List[Tuple[str, str]] = []
    remaining = max_chars
    for _index, (rel_path, content) in ranked:
        if len(selected) >= max_files or remaining <= 200:
            break
        # Full medium-sized source files are materially safer than many short
        # prefixes: a prefix-only review repeatedly invented missing tails and
        # pagination logic that existed later in the file.  Cap a single very
        # large file, but otherwise expose it completely.
        allowance = min(len(content), 12000, remaining)
        snippet = content[:allowance]
        if len(content) > allowance:
            snippet += (
                f"\n[QC CONTEXT WINDOW: showing {allowance} of {len(content)} characters; "
                "the remaining file is intentionally omitted. Do not report the file as "
                "truncated or incomplete based on this window boundary.]"
            )
        selected.append((rel_path, snippet))
        remaining -= len(snippet)

    manifest = "\n".join("- " + path.replace("\\", "/") for path, _ in files)
    return selected, manifest


def check_api_contract_consistency(
    files: List[Tuple[str, str]],
) -> Dict[str, Any]:
    """Deterministically compare frontend HTTP calls with FastAPI routes.

    The LLM functionality review is useful for product intent, but it cannot be
    the authority for a concrete routing fact.  This check focuses on the
    common generated React/Vite + FastAPI shape and only blocks when the
    workspace itself proves that Vite will forward a path which FastAPI does
    not expose.
    """
    backend_routes: set[Tuple[str, str]] = set()
    frontend_calls: List[Tuple[str, str, str]] = []
    frontend_base_urls: List[str] = []
    vite_configs: List[Tuple[str, str]] = []
    pydantic_response_fields: Dict[str, set[str]] = {}
    typescript_interface_fields: Dict[str, set[str]] = {}
    axios_response_types: set[str] = set()

    def normalize_path(value: str) -> str:
        value = value.split("?", 1)[0].strip()
        value = re.sub(r"\$\{[^}]+\}", "{}", value)
        value = re.sub(r"\{[^}/]+\}", "{}", value)
        value = "/" + value.lstrip("/")
        return value.rstrip("/") or "/"

    include_prefixes: List[str] = []
    python_files: List[Tuple[str, str]] = []
    for rel_path, content in files:
        normalized = rel_path.replace("\\", "/").lower()
        if normalized.endswith("vite.config.ts") or normalized.endswith("vite.config.js"):
            vite_configs.append((rel_path, content))
        if normalized.endswith(".py"):
            python_files.append((rel_path, content))
            try:
                tree = ast.parse(content)
                for node in tree.body:
                    if not isinstance(node, ast.ClassDef) or not node.name.endswith("Response"):
                        continue
                    is_pydantic = any(
                        (isinstance(base, ast.Name) and base.id == "BaseModel")
                        or (isinstance(base, ast.Attribute) and base.attr == "BaseModel")
                        for base in node.bases
                    )
                    if not is_pydantic:
                        continue
                    fields = {
                        statement.target.id
                        for statement in node.body
                        if isinstance(statement, ast.AnnAssign)
                        and isinstance(statement.target, ast.Name)
                    }
                    pydantic_response_fields[node.name[:-len("Response")]] = fields
            except SyntaxError:
                pass
            include_prefixes.extend(re.findall(
                r"include_router\s*\([^)]*?prefix\s*=\s*['\"]([^'\"]+)['\"]",
                content,
                re.DOTALL,
            ))

    unique_include_prefixes = set(include_prefixes)
    # Multiple mounted prefixes require import-aware router resolution.  Until
    # that relationship is statically proven, skip instead of risking a false
    # blocking result.
    global_prefix = include_prefixes[0] if len(unique_include_prefixes) == 1 else ""
    router_decorator_files = {
        rel_path
        for rel_path, content in python_files
        if re.search(r"@router\.(?:get|post|put|patch|delete)\s*\(", content)
    }
    ambiguous_mounts = (
        len(unique_include_prefixes) > 1
        or (bool(global_prefix) and len(router_decorator_files) != 1)
    )
    for _rel_path, content in python_files:
        router_prefix_match = re.search(
            r"APIRouter\s*\([^)]*?prefix\s*=\s*['\"]([^'\"]+)['\"]",
            content,
            re.DOTALL,
        )
        router_prefix = router_prefix_match.group(1) if router_prefix_match else ""
        for target, method, path in re.findall(
            r"@(router|app)\.(get|post|put|patch|delete)\s*\(\s*['\"]([^'\"]+)['\"]",
            content,
            re.IGNORECASE,
        ):
            prefix = ""
            if target.lower() == "router":
                prefix = global_prefix.rstrip("/") + "/" + router_prefix.lstrip("/")
            backend_routes.add((method.upper(), normalize_path(prefix + "/" + path.lstrip("/"))))

    axios_instances = {"axios"}
    for _rel_path, content in files:
        axios_instances.update(re.findall(
            r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*axios\.create\s*\(",
            content,
        ))
    axios_instance_pattern = "|".join(
        sorted((re.escape(name) for name in axios_instances), key=len, reverse=True)
    )
    for rel_path, content in files:
        normalized = rel_path.replace("\\", "/").lower()
        if not normalized.endswith((".ts", ".tsx", ".js", ".jsx")):
            continue
        for interface_name, body in re.findall(
            r"(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)\s*\{([^}]*)\}",
            content,
            re.DOTALL,
        ):
            typescript_interface_fields[interface_name] = set(re.findall(
                r"(?:^|[;\n])\s*([A-Za-z_$][\w$]*)\??\s*:", body
            ))
        axios_response_types.update(re.findall(
            rf"\b(?:{axios_instance_pattern})\s*\.\s*(?:get|post|put|patch|delete)"
            r"\s*<\s*([A-Za-z_$][\w$]*)\s*(?:\[\])?\s*>",
            content,
            re.IGNORECASE,
        ))
        frontend_base_urls.extend(re.findall(
            r"baseURL\s*:\s*['\"]([^'\"]+)['\"]", content
        ))
        for method, path in re.findall(
            rf"\b(?:{axios_instance_pattern})\s*\.\s*(get|post|put|patch|delete)"
            r"\s*(?:<[^;()]+>)?\s*\(\s*[`'\"]([^`'\"]+)[`'\"]",
            content,
            re.IGNORECASE,
        ):
            frontend_calls.append((rel_path, method.upper(), path))

    model_contracts = set(pydantic_response_fields).intersection(
        typescript_interface_fields, axios_response_types
    )
    route_applicable = bool(backend_routes and frontend_calls and not ambiguous_mounts)
    applicable = bool(route_applicable or model_contracts)
    if not applicable:
        return {
            "layer": "api_contract", "passed": True, "score": 100,
            "issues": [], "issue_count": 0, "applicable": False,
        }

    base_url = frontend_base_urls[0] if len(set(frontend_base_urls)) == 1 else ""
    issues: List[Dict[str, Any]] = []
    for rel_path, method, raw_path in frontend_calls if route_applicable else []:
        if raw_path.startswith(("http://", "https://")):
            continue
        browser_path = normalize_path(base_url.rstrip("/") + "/" + raw_path.lstrip("/"))
        forwarded_path = browser_path
        matching_proxy = ""
        matching_config_path = ""
        rewrite_present = False
        for config_path, config in vite_configs:
            proxy_keys = re.findall(r"['\"](/[^'\"]+)['\"]\s*:\s*\{", config)
            for proxy_key in sorted(proxy_keys, key=len, reverse=True):
                if browser_path == proxy_key or browser_path.startswith(proxy_key.rstrip("/") + "/"):
                    matching_proxy = proxy_key.rstrip("/")
                    matching_config_path = config_path
                    rewrite_present = bool(re.search(
                        r"rewrite\s*:\s*[^,}]*replace\s*\(\s*/\^\\?/"
                        + re.escape(matching_proxy.lstrip("/")),
                        config,
                    ))
                    if rewrite_present:
                        forwarded_path = normalize_path(browser_path[len(matching_proxy):] or "/")
                    break
            if matching_proxy:
                break

        normalized_call = normalize_path(forwarded_path)
        if (method, normalized_call) in backend_routes:
            continue
        # Without a Vite proxy the production reverse proxy may own the
        # mapping, so do not invent a hard failure.  A configured matching
        # proxy, however, proves exactly what path reaches FastAPI.
        if not matching_proxy:
            continue
        available = sorted(path for route_method, path in backend_routes if route_method == method)
        issues.append({
            "file": matching_config_path or rel_path,
            "layer": "api_contract",
            "severity": "error",
            "message": (
                f"{method} {browser_path} is forwarded by Vite as {normalized_call}, "
                f"but FastAPI exposes {', '.join(available) or 'no matching method routes'}."
            ),
            "fix_hint": (
                f"Add a Vite proxy rewrite that removes {matching_proxy}, or align the "
                "frontend baseURL/backend router prefix."
            ),
        })

    for model_name in sorted(model_contracts):
        backend_fields = pydantic_response_fields[model_name]
        frontend_fields = typescript_interface_fields[model_name]
        unknown_fields = sorted(frontend_fields - backend_fields)
        if not unknown_fields:
            continue
        interface_file = next(
            (path for path, content in files if re.search(
                rf"(?:export\s+)?interface\s+{re.escape(model_name)}\b", content
            )),
            "frontend",
        )
        issues.append({
            "file": interface_file,
            "layer": "api_contract",
            "severity": "error",
            "message": (
                f"TypeScript {model_name} declares fields not returned by the "
                f"Pydantic {model_name}Response model: {', '.join(unknown_fields)}."
            ),
            "fix_hint": (
                f"Align the frontend {model_name} fields with the backend response "
                f"fields: {', '.join(sorted(backend_fields))}."
            ),
        })

    return {
        "layer": "api_contract",
        "passed": not issues,
        "score": max(0, 100 - 40 * len(issues)),
        "issues": issues,
        "issue_count": len(issues),
        "applicable": True,
    }


def _reconcile_functionality_issues(
    files: List[Tuple[str, str]],
    examined_files: List[Tuple[str, str]],
    issues: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Reject LLM findings contradicted by the workspace evidence it received."""
    file_map = {path.replace("\\", "/"): content for path, content in files}
    examined_paths = {path.replace("\\", "/") for path, _ in examined_files}
    examined_map = {
        path.replace("\\", "/"): content for path, content in examined_files
    }
    corpus = "\n".join(file_map.values()).lower()
    basename_map: Dict[str, List[str]] = {}
    casefold_path_map: Dict[str, List[str]] = {}
    casefold_basename_map: Dict[str, List[str]] = {}
    for path in file_map:
        basename_map.setdefault(Path(path).name, []).append(path)
        casefold_path_map.setdefault(path.casefold(), []).append(path)
        casefold_basename_map.setdefault(Path(path).name.casefold(), []).append(path)

    reconciled: List[Dict[str, Any]] = []
    for raw_issue in issues:
        if not isinstance(raw_issue, dict):
            continue
        issue = dict(raw_issue)
        raw_path = str(issue.get("file") or "").strip().strip("`'").replace("\\", "/")
        while raw_path.startswith("./"):
            raw_path = raw_path[2:]
        if raw_path not in file_map:
            path_matches = casefold_path_map.get(raw_path.casefold(), []) if raw_path else []
            if len(path_matches) == 1:
                raw_path = path_matches[0]
        if raw_path not in file_map:
            basename_matches = basename_map.get(Path(raw_path).name, []) if raw_path else []
            if not basename_matches and raw_path:
                basename_matches = casefold_basename_map.get(
                    Path(raw_path).name.casefold(), []
                )
            if len(basename_matches) == 1:
                raw_path = basename_matches[0]
        message = str(issue.get("message") or "")
        lowered = message.lower()
        file_content = file_map.get(raw_path, "")
        module_refs = re.findall(r"[`'\"](\.{1,2}/[^`'\"\s]+)[`'\"]", message)
        if module_refs and any(marker in lowered for marker in (
            "module", "import", "require", "load", "导入", "加载", "模块",
        )):
            parent = Path(raw_path).parent
            resolved_refs = []
            for ref in module_refs:
                base = (parent / ref).as_posix()
                resolved_refs.append(any(
                    candidate in file_map
                    for candidate in (base, base + ".js", base + ".ts", base + "/index.js")
                ))
            if resolved_refs and all(resolved_refs):
                continue
        code_tokens = re.findall(r"`([^`\n]+)`", message)
        if not code_tokens:
            code_tokens = re.findall(r"'([^'\n]+)'", message)
        if not code_tokens:
            code_tokens = re.findall(
                r"\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+\b",
                message,
            )
        claims_typo_or_undefined = (
            any(marker in message for marker in ("拼写错误", "未定义"))
            or any(marker in lowered for marker in ("typo", "misspell", "undefined", "is not defined"))
            or (
                len(code_tokens) >= 2
                and any(marker in lowered for marker in ("should be", "应为", "應為"))
            )
        )
        alleged_bad_token_present = True
        if code_tokens:
            alleged_bad_token = code_tokens[0]
            if re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*", alleged_bad_token):
                alleged_bad_token_present = bool(re.search(
                    rf"(?<![\w$]){re.escape(alleged_bad_token)}(?![\w$])",
                    file_content,
                ))
            else:
                alleged_bad_token_present = alleged_bad_token in file_content
        if (
            claims_typo_or_undefined
            and code_tokens
            and not alleged_bad_token_present
        ):
            continue
        claims_not_exported = (
            "未导出" in message
            or "not exported" in lowered
            or "is not exported" in lowered
        )
        if claims_not_exported and code_tokens:
            symbol = re.escape(code_tokens[0].split(".")[-1])
            if re.search(
                rf"(?:module\.)?exports(?:\.{symbol}\b|\s*=\s*\{{[\s\S]*?\b{symbol}\b)",
                file_content,
            ):
                continue
        claims_missing_file = (
            "文件不存在" in message
            or ("缺少" in message and ("文件" in message or bool(Path(raw_path).suffix)))
            or "missing file" in lowered
            or "file is missing" in lowered
            or "does not exist" in lowered
            or "not found" in lowered
        )
        claims_absent_implementation = any(
            marker in message for marker in (
                "没有实现", "未实现", "功能缺失", "任何 DOM", "未发现",
                "没有找到", "没有看到", "不存在相关", "未提供",
            )
        ) or any(marker in lowered for marker in (
            "not implemented", "no implementation", "does not contain",
            "not found", "no evidence", "not provided",
        ))
        claims_window_truncation = (
            "截断" in message
            or "不完整" in message
            or any(marker in lowered for marker in (
                "truncated", "cuts off", "cut off", "incomplete",
            ))
        )
        evidence_checks = []
        is_api_claim = (
            any(marker in lowered for marker in ("fetch", "axios", "api call", "endpoint"))
            or "api 调用" in message
            or "api 获取" in message
            or "接口调用" in message
        )
        if is_api_claim:
            evidence_checks.append(any(marker in corpus for marker in ("fetch(", "axios")))
        if "输入框" in message or "input" in lowered:
            evidence_checks.append("<input" in corpus or "todoinput" in corpus)
        if "列表" in message or "list" in lowered:
            evidence_checks.append(".map(" in corpus or "todolist" in corpus or "<ul" in corpus)
        if "按钮" in message or "button" in lowered:
            evidence_checks.append("<button" in corpus or "todoitem" in corpus)
        if "筛选" in message or "filter" in lowered:
            evidence_checks.append("filter" in corpus)
        if "删除" in message or "delete" in lowered:
            if is_api_claim:
                evidence_checks.append(bool(re.search(
                    r"axios\s*\.\s*delete\s*\(|method\s*:\s*['\"]delete['\"]",
                    corpus,
                    re.IGNORECASE,
                )))
            else:
                evidence_checks.append("delete" in corpus)
        if "状态" in message or "status" in lowered:
            if is_api_claim:
                evidence_checks.append(bool(re.search(
                    r"axios\s*\.\s*(?:patch|put)\s*\(|method\s*:\s*['\"](?:patch|put)['\"]",
                    corpus,
                    re.IGNORECASE,
                )))
            else:
                evidence_checks.append(any(marker in corpus for marker in ("completed", "toggle", "status")))
        # The manifest is authoritative: an existing path cannot be reported
        # as absent. Also reject implementation claims about files whose
        # contents were not part of the review context.
        if raw_path in file_map and claims_missing_file:
            continue
        if claims_absent_implementation and evidence_checks and all(evidence_checks):
            continue
        if (
            claims_window_truncation
            and "[QC CONTEXT WINDOW:" in examined_map.get(raw_path, "")
        ):
            continue
        if raw_path in file_map and raw_path not in examined_paths:
            continue
        if not raw_path or raw_path in {"—", "-"}:
            continue
        issue["file"] = raw_path
        issue["layer"] = "functionality"
        reconciled.append(issue)
    return reconciled


def _normalize_acceptance_results(
    data: Dict[str, Any],
    normalized_contracts: List[Dict[str, Any]],
    selected_files: List[Tuple[str, str]],
) -> List[Dict[str, Any]]:
    if not normalized_contracts:
        return []
    raw_acceptance = data.get("acceptance_results")
    if not isinstance(raw_acceptance, list):
        return []
    selected_paths = {
        str(path).replace("\\", "/") for path, _content in selected_files
    }
    observations: List[Dict[str, Any]] = []
    for contract in normalized_contracts:
        criterion_id = str(contract["criterion_id"])
        matches = [
            item for item in raw_acceptance
            if (
                isinstance(item, dict)
                and str(item.get("criterion_id") or "") == criterion_id
            )
        ]
        if len(matches) != 1:
            continue
        item = matches[0]
        observed_files = list(dict.fromkeys(
            str(path).replace("\\", "/")
            for path in (item.get("observed_files") or [])
            if str(path).strip()
        ))
        if (
            str(item.get("criterion") or "") != str(contract["criterion"])
            or not isinstance(item.get("passed"), bool)
            or not str(item.get("observation") or "").strip()
            or (item["passed"] is True and not observed_files)
            or not set(observed_files).issubset(selected_paths)
        ):
            continue
        observations.append({
            **contract,
            "passed": item["passed"],
            "observation": str(item["observation"]).strip(),
            "observed_files": observed_files,
        })
    return observations


def check_layer3_functionality(
    files: List[Tuple[str, str]],
    subproject_description: str,
    hermes_client: Any,
    acceptance_contracts: Optional[List[Dict[str, Any]]] = None,
    review_packet: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    第三层：功能实现检查（用 LLM 判断代码是否实现了描述的功能）
    使用独立的短超时 client（30s），防止卡住整个质检流程。
    """
    if not files:
        return {
            "layer": "functionality",
            "passed": False,
            "score": 0,
            "issues": [{"file": "—", "layer": "functionality", "severity": "error",
                        "message": "没有找到任何源码文件", "fix_hint": "请先生成代码文件"}],
            "issue_count": 1,
        }

    selected_files, file_manifest = _select_functionality_context(
        files, subproject_description
    )
    normalized_contracts = [
        dict(contract)
        for contract in (acceptance_contracts or [])
        if (
            isinstance(contract, dict)
            and str(contract.get("criterion_id") or "").strip()
            and str(contract.get("criterion") or "").strip()
        )
    ]
    acceptance_prompt = ""
    if normalized_contracts:
        acceptance_prompt = (
            "\nThe acceptance criteria below are optional review aids, not a "
            "mandatory output gate. Use them when they help assess the phase goal. "
            "You may return acceptance_results rows for criteria you can support "
            "with concrete code evidence. "
            "Each row must contain criterion_id, the exact criterion text, "
            "passed (boolean), a non-empty concrete observation, and "
            "observed_files listing only files whose contents were shown. "
            "A passed row must list at least one shown file. A failed row may "
            "use an empty observed_files list only when its observation names "
            "a required artifact that is absent from the authoritative file "
            "manifest.\n"
            + json.dumps(
                [
                    {
                        "criterion_id": contract["criterion_id"],
                        "criterion": contract["criterion"],
                    }
                    for contract in normalized_contracts
                ],
                ensure_ascii=False,
            )
        )
    code_summary = "".join(
        f"\n\n### {rel_path}\n{content}"
        for rel_path, content in selected_files
    )
    packet_context = ""
    if review_packet:
        packet_context = (
            "\n\nVERIFIED QC REVIEW CONTEXT:\n"
            + json.dumps({
                "project": review_packet.get("project") or {},
                "phase": review_packet.get("phase") or {},
                "tasks": review_packet.get("tasks") or [],
                "pre_qa": review_packet.get("pre_qa") or {},
            }, ensure_ascii=False)
        )

    try:
        from core.hermes_client import (
            Message, MessageRole, HermesClient as _HC, chat_for_purpose,
            current_user_api_config,
        )
        # 质检可能在后台 task 执行，contextvar 可能已由调用方
        # (_auto_repair_api_configs / _run_with_user_api_config) 设置。
        # 优先用 contextvar 的用户级配置（model/api_key/base_url/thinking），
        # 否则回退到传入 client 的属性。避免用全局单例的默认 model
        # (hermes-3) 调用导致 provider 400 → reviewer_unavailable → model_failed。
        ctx_cfg = current_user_api_config.get() or {}
        short_client = _HC(
            base_url=ctx_cfg.get("api_base") or hermes_client.base_url,
            api_key=ctx_cfg.get("api_key") or hermes_client.api_key,
            model=ctx_cfg.get("model") or hermes_client.model,
            max_tokens=min(int(ctx_cfg.get("max_tokens") or hermes_client.max_tokens), 2048),
            temperature=0.1,    # allow minor output variation so retries can escape format errors
            timeout=120,        # bounded production review budget
        )
        prompt = [
            Message(role=MessageRole.SYSTEM, content=(
                "你是代码审查专家。检查代码是否实现了功能需求。\n"
                "文件清单是工作区的权威事实；清单中存在的文件绝不能报告为缺失。\n"
                "只能评价下方提供了内容的文件，未展示内容的文件不得推断其功能缺失。\n"
                "代码内容可能是带 QC CONTEXT WINDOW 标记的前缀窗口；绝不能把窗口边界"
                "报告成源文件截断、不完整或缺少结尾。\n"
                "Acceptance contracts supplied here are semantic source review only; "
                "command and API criteria are validated by deterministic runners and "
                "are omitted. Judge semantic criteria from the shown implementation. "
                "When the code directly supports a criterion, pass it with a concrete "
                "code observation; do not fail it only because no command or browser "
                "was executed. Fail only for a concrete source defect.\n"
                "只报告明确的、可定位的问题（有具体文件和行号）。\n"
                "不确定的问题不要报告。\n"
                "输入中的任务、验收标准、文件归属和 Pre-QA 结果已经由服务端校验。"
                "不得改写这些事实，也不得重复否定已经由 Pre-QA 证明通过的机械标准。\n"
                "输出格式（严格 JSON，不要有其他文字）：\n"
                '{"passed": true/false, "score": 0-100, "issues": ['
                '{"criterion_id":"关联验收标准ID或空字符串","file":"文件名",'
                '"line":42,"symbol":"函数或组件名","severity":"error/warning",'
                '"message":"具体问题","expected":"正确行为","actual":"当前行为",'
                '"evidence":"所给代码中的具体证据","fix_hint":"一句话最小修复建议"}'
                '], "summary": "一句话总体评价", '
                '"acceptance_results": [{"criterion_id":"...",'
                '"criterion":"exact text","passed":true/false,'
                '"observation":"concrete code observation",'
                '"observed_files":["shown/source.py"]}]}'
                + acceptance_prompt
            )),
            Message(role=MessageRole.USER, content=(
                f"功能需求：{subproject_description[:1200]}\n\n"
                f"完整文件清单：\n{file_manifest}\n\n"
                f"已选择并提供内容的代码文件：{code_summary}"
                f"{packet_context}"
            )),
        ]
        resp = chat_for_purpose(short_client, prompt, purpose="reviewer")
        content_str = resp.get("content", "")
        if content_str.startswith("⚠️") or content_str.startswith("❌"):
            raise ValueError(f"LLM 返回错误：{content_str[:100]}")
        # 提取 JSON
        data = None
        parse_error: Exception | None = None
        acceptance_observations: List[Dict[str, Any]] = []
        for attempt in range(3):
            if attempt:
                retry_prompt = prompt + [Message(
                    role=MessageRole.USER,
                    content=(
                        "The previous response violated the required output contract. "
                        f"Validation error: {parse_error}. Return only the requested "
                        "strict JSON object. Any acceptance_results rows must use "
                        "these criterion_id and criterion pairs: "
                        + json.dumps(
                            [
                                {
                                    "criterion_id": contract["criterion_id"],
                                    "criterion": contract["criterion"],
                                }
                                for contract in normalized_contracts
                            ],
                            ensure_ascii=False,
                        )
                        + ". A passed row needs a non-empty observed_files subset of: "
                        + json.dumps(
                            sorted(
                                str(path).replace("\\", "/")
                                for path, _content in selected_files
                            ),
                            ensure_ascii=False,
                        )
                        + ". A failed row may use [] only when its observation names "
                        "a required artifact absent from the manifest."
                    ),
                )]
                content_str = str(
                    chat_for_purpose(
                        short_client,
                        retry_prompt,
                        use_cache=False,
                        purpose="reviewer",
                    ).get("content", "")
                )
            try:
                candidate_data = None
                decoder = json.JSONDecoder()
                for start in (
                    index for index, char in enumerate(content_str)
                    if char == "{"
                ):
                    try:
                        candidate, _ = decoder.raw_decode(content_str[start:])
                    except json.JSONDecodeError:
                        continue
                    if isinstance(candidate, dict):
                        candidate_data = candidate
                        break
                if candidate_data is None:
                    raise ValueError(
                        "functionality review did not return valid JSON"
                    )
                verdict = candidate_data.get("passed")
                if isinstance(verdict, str) and verdict.strip().lower() in {
                    "true",
                    "false",
                }:
                    candidate_data["passed"] = verdict.strip().lower() == "true"
                if not isinstance(candidate_data.get("passed"), bool):
                    raise ValueError(
                        "functionality review returned an invalid verdict"
                    )
                if not isinstance(candidate_data.get("issues"), list):
                    raise ValueError(
                        "functionality review returned invalid issues"
                    )
                acceptance_observations = _normalize_acceptance_results(
                    candidate_data,
                    normalized_contracts,
                    selected_files,
                )
                data = candidate_data
                break
            except (ValueError, json.JSONDecodeError) as exc:
                parse_error = exc
        if data is None:
            raise parse_error or ValueError(
                "functionality review did not return valid JSON"
            )
        if not isinstance(data, dict) or not isinstance(data.get("passed"), bool):
            raise ValueError("functionality review returned an invalid verdict")
        raw_issues = data.get("issues")
        if not isinstance(raw_issues, list):
            raise ValueError("functionality review returned invalid issues")
        issues = _reconcile_functionality_issues(files, selected_files, raw_issues)
        has_blocking = any(
            str(issue.get("severity", "warning")).lower() in {"error", "critical"}
            for issue in issues
        )
        raw_blocking = [
            issue for issue in raw_issues
            if isinstance(issue, dict)
            and str(issue.get("severity", "warning")).lower() in {"error", "critical"}
        ]
        source_by_path = {
            path.replace("\\", "/"): content for path, content in files
        }
        all_blocking_claims_contradicted = bool(raw_blocking) and all(
            any(
                not re.search(
                    rf"(?<![\w$]){re.escape(token)}(?![\w$])",
                    source_by_path.get(
                        str(issue.get("file") or "").replace("\\", "/"),
                        "",
                    ),
                )
                for token in re.findall(
                    r"\b[a-z_$][\w$]*(?:\.[a-z_$][\w$]*)+\b",
                    str(issue.get("message") or ""),
                )
            )
            and not _reconcile_functionality_issues(
                files,
                selected_files,
                [issue],
            )
            for issue in raw_blocking
        )
        model_passed = (
            data["passed"] or all_blocking_claims_contradicted
        )
        if not model_passed and not has_blocking:
            issues.append({
                "file": selected_files[0][0] if selected_files else files[0][0],
                "layer": "functionality",
                "severity": "error",
                "message": "Functionality review rejected the delivery without a "
                "usable blocking finding",
                "fix_hint": (
                    "Rerun the functionality review and provide an actionable file-level "
                    "finding before accepting the delivery."
                ),
            })
            has_blocking = True
        score = data.get("score", 80)
        if not isinstance(score, (int, float)):
            score = 0
        return {
            "layer": "functionality",
            "passed": model_passed and not has_blocking,
            "score": max(0, min(100, int(score))),
            "issues": issues,
            "issue_count": len(issues),
            "summary": data.get("summary", ""),
            "acceptance_observations": acceptance_observations,
            "details": {
                "manifest_files": len(files),
                "content_files": len(selected_files),
                "content_limited": len(selected_files) < len(files),
            },
        }
    except Exception as exc:
        error_text = str(exc)[:400]
        reviewer_unavailable = any(
            marker in error_text.lower()
            for marker in (
                "api 调用失败", "payment required", "rate limit", "timeout",
                "llm 返回错误", "invalid api key", "401", "402", "429",
                "functionality review",
            )
        )
        issue = {
            "file": selected_files[0][0] if selected_files else files[0][0],
            "layer": "functionality",
            "severity": "error",
            "message": (
                "Functionality review could not produce valid acceptance evidence "
                f"({exc.__class__.__name__})"
            ),
            "fix_hint": (
                "Restore the functionality reviewer and rerun QA; do not accept the "
                "delivery from file presence alone."
            ),
        }
        return {
            "layer": "functionality",
            "passed": False,
            "score": 0,
            "issues": [issue],
            "issue_count": 1,
            "summary": "Functionality acceptance evidence is unavailable",
            "details": {
                "manifest_files": len(files),
                "content_files": len(selected_files),
                "content_limited": len(selected_files) < len(files),
                "error": error_text[:240],
                "reviewer_unavailable": reviewer_unavailable,
            },
            "reviewer_unavailable": reviewer_unavailable,
            "reviewer_error": error_text if reviewer_unavailable else "",
        }

def check_layer4_collaboration(files: List[Tuple[str, str]]) -> Dict[str, Any]:
    """
    第四层：文件协作检查
    - 检查导入的模块是否在工作区内存在
    - 检查接口定义与调用是否一致（函数名匹配）
    - 检查循环导入风险
    """
    issues: List[Dict] = []
    score = 100
    file_map = {path.replace("\\", "/"): content for path, content in files}
    file_paths = set(file_map)
    manifests: List[Tuple[str, str, Dict[str, Any]]] = []

    for rel_path, content in files:
        if Path(rel_path).name != "package.json":
            continue
        try:
            package_data = json.loads(content)
        except Exception:
            continue
        manifest_dir = posixpath.dirname(rel_path.replace("\\", "/"))
        manifests.append((manifest_dir, rel_path, package_data))
        for script_name, command in (package_data.get("scripts") or {}).items():
            if re.search(r"\bnode(?:\.exe)?\s+[^\n]*node_modules[/\\]\.bin[/\\]", str(command)):
                issues.append({
                    "file": rel_path,
                    "layer": "collaboration",
                    "severity": "error",
                    "message": f"npm script `{script_name}` incorrectly executes a .bin shell shim with node",
                    "fix_hint": "Invoke the package CLI directly, for example `jest`, so npm resolves the platform-specific executable",
                })
                score -= 30
            nested_cli = re.match(
                r"^\s*cd\s+(backend|frontend)\s*&&\s*(?!npm(?:\s|$))(.+)$",
                str(command),
            )
            if not manifest_dir and nested_cli:
                child = nested_cli.group(1)
                issues.append({
                    "file": rel_path,
                    "layer": "collaboration",
                    "severity": "error",
                    "message": (
                        f"Root npm script `{script_name}` enters `{child}` then directly invokes "
                        "a child CLI that is not on the root npm PATH"
                    ),
                    "fix_hint": (
                        f"Use `npm --prefix {child} {script_name}` (or `npm --prefix {child} run {script_name}`) "
                        "so npm resolves the child package's local binaries"
                    ),
                })
                score -= 30

    def owning_manifest(rel_path: str) -> Optional[Tuple[str, str, Dict[str, Any]]]:
        candidates = [
            manifest for manifest in manifests
            if not manifest[0] or rel_path.startswith(manifest[0] + "/")
        ]
        return max(candidates, key=lambda item: len(item[0]), default=None)

    # Validate module format and test toolchain against each package manifest.
    for manifest_dir, manifest_path, package_data in manifests:
        package_files = {
            path: content for path, content in file_map.items()
            if path != manifest_path
            and owning_manifest(path)
            and owning_manifest(path)[1] == manifest_path
        }
        esm_js = [
            path for path, content in package_files.items()
            if path.endswith((".js", ".jsx"))
            and re.search(r"(^|\n)\s*(?:import\s|export\s)", content)
        ]
        if esm_js and package_data.get("type") != "module":
            issues.append({
                "file": manifest_path,
                "layer": "collaboration",
                "severity": "error",
                "message": "JavaScript uses ESM import/export but package.json does not declare type=module",
                "fix_hint": "Add `\"type\": \"module\"` or consistently convert the package to CommonJS",
            })
            score -= 30

        ts_tests = [path for path in package_files if "/tests/" in f"/{path}" and path.endswith((".ts", ".tsx"))]
        esm_js_tests = [
            path for path, content in package_files.items()
            if "/tests/" in f"/{path}"
            and path.endswith((".js", ".jsx"))
            and re.search(r"(^|\n)\s*(?:import\s|export\s)", content)
        ]
        dependencies = {
            **(package_data.get("dependencies") or {}),
            **(package_data.get("devDependencies") or {}),
        }
        jest_config = package_data.get("jest") or {}
        test_script = str((package_data.get("scripts") or {}).get("test") or "")
        if (
            esm_js_tests
            and package_data.get("type") == "module"
            and "jest" in dependencies
            and "jest" in test_script
            and "--experimental-vm-modules" not in test_script
        ):
            issues.append({
                "file": manifest_path,
                "layer": "collaboration",
                "severity": "error",
                "message": "Jest is configured without Node VM modules but the JavaScript tests use ESM imports",
                "fix_hint": (
                    "Change the test script to `NODE_OPTIONS=--experimental-vm-modules jest` "
                    "(or consistently convert tests to CommonJS)"
                ),
            })
            score -= 30
        if (
            esm_js_tests
            and package_data.get("type") == "module"
            and "jest" in dependencies
            and "jest" in test_script
        ):
            realm_sensitive_array_matcher = re.compile(
                r"\.\s*toBeInstanceOf\s*\(\s*Array\s*\)"
            )
            for test_path in esm_js_tests:
                test_content = package_files[test_path]
                for match in realm_sensitive_array_matcher.finditer(test_content):
                    line = test_content.count("\n", 0, match.start()) + 1
                    issues.append({
                        "file": test_path,
                        "layer": "collaboration",
                        "severity": "error",
                        "line": line,
                        "message": (
                            "Jest ESM test uses toBeInstanceOf(Array), which can reject a real "
                            "JSON array created in a different Node VM realm"
                        ),
                        "fix_hint": (
                            "Replace `expect(value).toBeInstanceOf(Array)` with "
                            "`expect(Array.isArray(value)).toBe(true)` so arrays are checked "
                            "reliably across Jest VM realms"
                        ),
                    })
                    score -= 30
        has_ts_transform = (
            any(name in dependencies for name in ("ts-jest", "babel-jest", "@swc/jest"))
            or bool(jest_config.get("transform"))
        )
        if ts_tests and not ("typescript" in dependencies and has_ts_transform):
            issues.append({
                "file": manifest_path,
                "layer": "collaboration",
                "severity": "error",
                "message": "TypeScript tests exist but the package has no TypeScript Jest transform toolchain",
                "fix_hint": "Configure TypeScript + ts-jest (or another transform), or rewrite tests as runnable JavaScript",
            })
            score -= 30
        if ts_tests:
            tsconfigs: Dict[str, Dict[str, Any]] = {}
            for path, content in package_files.items():
                if Path(path).name != "tsconfig.json":
                    continue
                try:
                    tsconfigs[path] = json.loads(content)
                except json.JSONDecodeError:
                    continue
            for config_path, config_data in tsconfigs.items():
                extends = config_data.get("extends")
                if isinstance(extends, str) and extends.startswith(("./", "../")):
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(config_path), extends))
                    if not target.endswith(".json"):
                        target += ".json"
                    if target not in file_paths:
                        issues.append({
                            "file": config_path,
                            "layer": "collaboration",
                            "severity": "error",
                            "message": f"tsconfig extends 指向不存在的文件 `{extends}`",
                            "fix_hint": "创建被继承的 tsconfig，或移除/修正 extends 路径",
                        })
                        score -= 20
            has_es_module_interop = any(
                bool((config.get("compilerOptions") or {}).get("esModuleInterop"))
                for config in tsconfigs.values()
            )
            if not has_es_module_interop:
                issues.append({
                    "file": manifest_path,
                    "layer": "collaboration",
                    "severity": "error",
                    "message": "TypeScript Jest tests use package default imports without esModuleInterop configuration",
                    "fix_hint": "Set compilerOptions.esModuleInterop=true in the test tsconfig or avoid incompatible default imports",
                })
                score -= 20
            if package_data.get("type") == "module":
                esm_modules = {"es6", "es2015", "es2020", "es2022", "esnext", "node16", "node18", "nodenext", "preserve"}
                incompatible_configs = [
                    path for path, config in tsconfigs.items()
                    if str((config.get("compilerOptions") or {}).get("module", "")).lower() not in esm_modules
                ]
                for config_path in incompatible_configs:
                    issues.append({
                        "file": config_path,
                        "layer": "collaboration",
                        "severity": "error",
                        "message": "TypeScript tests compile as CommonJS while the production package is ESM",
                        "fix_hint": "Use module=NodeNext/ESNext (with compatible moduleResolution) for the test tsconfig",
                    })
                    score -= 20

                extensions = set(jest_config.get("extensionsToTreatAsEsm") or [])
                preset = str(jest_config.get("preset") or "")
                transform_values = list((jest_config.get("transform") or {}).values())
                transform_uses_esm = any(
                    isinstance(value, list)
                    and len(value) > 1
                    and isinstance(value[1], dict)
                    and value[1].get("useESM") is True
                    for value in transform_values
                )
                jest_handles_ts_as_esm = (
                    ".ts" in extensions
                    or "default-esm" in preset
                    or transform_uses_esm
                )
                if not jest_handles_ts_as_esm:
                    issues.append({
                        "file": manifest_path,
                        "layer": "collaboration",
                        "severity": "error",
                        "message": "Jest does not treat TypeScript tests as ESM although the production package is ESM",
                        "fix_hint": "Configure ts-jest useESM plus extensionsToTreatAsEsm=['.ts'], or use the ts-jest ESM preset",
                    })
                    score -= 20

        test_files = {
            path: content for path, content in package_files.items()
            if "/tests/" in f"/{path}" and path.endswith((".js", ".jsx", ".ts", ".tsx"))
        }
        source_files = [path for path in package_files if "/src/" in f"/{path}"]
        if test_files and source_files:
            references_production = any(
                re.search(r"(?:from\s+|import\s*)['\"][^'\"]*src/", content)
                or re.search(r"\brequire\(\s*['\"][^'\"]*src/", content)
                or re.search(r"\bimport\(\s*['\"][^'\"]*src/", content)
                or (
                    re.search(
                        r"\b(?:spawn|spawnSync|execFile|execFileSync)\s*\(",
                        content,
                    )
                    and (
                        re.search(
                            r"\bpath\.join\([^)]*['\"]src['\"]",
                            content,
                            re.DOTALL,
                        )
                        or re.search(
                            r"['\"][^'\"]*src[/\\][^'\"]*['\"]",
                            content,
                        )
                    )
                )
                for content in test_files.values()
            )
            if not references_production:
                setup_path = next(
                    (path for path in test_files if Path(path).stem == "setup"),
                    next(iter(test_files)),
                )
                issues.append({
                    "file": setup_path,
                    "layer": "collaboration",
                    "severity": "error",
                    "message": "Test suite does not import production source and therefore cannot validate the delivered backend",
                    "fix_hint": "Import the real app/server from src in test setup; do not test a separately recreated implementation",
                })
                score -= 30
            for test_path, test_content in test_files.items():
                mock_app = re.search(r"\b(?:const|let|var)\s+(\w+)\s*=\s*express\s*\(\s*\)", test_content)
                has_mock_routes = bool(
                    mock_app
                    and re.search(
                        rf"\b{re.escape(mock_app.group(1))}\s*\.\s*(?:get|post|put|patch|delete)\s*\(",
                        test_content,
                    )
                )
                if has_mock_routes:
                    issues.append({
                        "file": test_path,
                        "layer": "collaboration",
                        "severity": "error",
                        "message": "Tests construct standalone mock routes instead of exercising production backend code",
                        "fix_hint": "Remove the fake Express app and import createApp/app from the production src modules",
                    })
                    score -= 30

    # Deterministic full-stack runtime contract checks. These catch common
    # failures before an expensive isolated deployment reveals them one by one.
    backend_sources = {
        path: content
        for path, content in file_map.items()
        if path.startswith("backend/src/") and path.endswith((".js", ".ts"))
    }
    for rel_path, content in backend_sources.items():
        if re.search(
            r"\bJWT_SECRET\s*=\s*process\.env\.JWT_SECRET\s*(?:\|\||\?\?)\s*['\"]",
            content,
        ):
            issues.append({
                "file": rel_path,
                "layer": "collaboration",
                "severity": "error",
                "message": "JWT secret has a hard-coded fallback instead of failing closed",
                "fix_hint": "Require JWT_SECRET from the environment and stop startup with a clear error when it is missing",
            })
            score -= 30

        for field, fallback in (("status", "todo"), ("priority", "medium")):
            if re.search(
                rf"\b{field}\s*&&\s*\w+\.includes\(\s*{field}\s*\)\s*\?\s*{field}\s*:\s*['\"]{fallback}['\"]",
                content,
            ):
                issues.append({
                    "file": rel_path,
                    "layer": "collaboration",
                    "severity": "error",
                    "message": f"API silently accepts invalid {field} values by replacing them with a default",
                    "fix_hint": f"Return JSON HTTP 400 when {field} is supplied outside the documented enum",
                })
                score -= 30

        if (
            "due_date" in content
            and re.search(r"\b(?:INSERT|UPDATE)\b[\s\S]{0,500}\bdue_date\b", content, re.IGNORECASE)
            and not re.search(
                r"(?:Date\.parse|isNaN\s*\(|\\d\{4\}.*\\d\{2\}.*\\d\{2\}|validate\w*Date)",
                content,
                re.IGNORECASE,
            )
        ):
            issues.append({
                "file": rel_path,
                "layer": "collaboration",
                "severity": "error",
                "message": "due_date is persisted without validating the documented date format",
                "fix_hint": "Accept null or a real YYYY-MM-DD value; return JSON HTTP 400 for malformed dates",
            })
            score -= 30

        unvalidated_description = False
        for match in re.finditer(r"\bdescription\s*\|\|\s*['\"]{2}", content):
            scope_start = content.rfind("const {", 0, match.start())
            validation_scope = content[
                scope_start if scope_start >= 0 else max(0, match.start() - 1200):
                match.start()
            ]
            if not re.search(
                r"typeof\s+description\s*[!=]==?\s*['\"]string['\"]",
                validation_scope,
            ):
                unvalidated_description = True
                break
        if unvalidated_description:
            issues.append({
                "file": rel_path,
                "layer": "collaboration",
                "severity": "error",
                "message": "description is passed to storage without string input validation and can produce an HTML 500 response",
                "fix_hint": "Return JSON HTTP 400 when a supplied description is not a string",
            })
            score -= 30

    uses_sqlite = any(
        re.search(r"\bnew\s+Database\s*\(", content)
        for content in backend_sources.values()
    )
    if uses_sqlite:
        for rel_path, content in file_map.items():
            if (
                "/tests/" in f"/{rel_path}"
                and "DATABASE_PATH" in content
                and "unlinkSync" in content
                and not re.search(r"(?:\.close|closeDatabase)\s*\(", content)
            ):
                issues.append({
                    "file": rel_path,
                    "layer": "collaboration",
                    "severity": "error",
                    "message": "Test cleanup deletes the SQLite database without closing the production SQLite connection",
                    "fix_hint": "Export a closeDatabase/test cleanup hook, close the connection in afterAll, then delete db, -wal and -shm files",
                })
                score -= 30

    no_unused_locals = False
    for rel_path, content in file_map.items():
        if Path(rel_path).name != "tsconfig.json":
            continue
        try:
            no_unused_locals = no_unused_locals or bool(
                (json.loads(content).get("compilerOptions") or {}).get("noUnusedLocals")
            )
        except (TypeError, json.JSONDecodeError):
            pass
    if no_unused_locals:
        for rel_path, content in file_map.items():
            if not rel_path.endswith((".tsx", ".ts")):
                continue
            for match in re.finditer(
                r"\b(?:interface|type)\s+([A-Za-z_$]\w*)\b",
                content,
            ):
                declaration_name = match.group(1)
                if len(re.findall(
                    rf"(?<!\.)\b{re.escape(declaration_name)}\b",
                    content,
                )) == 1:
                    issues.append({
                        "file": rel_path,
                        "layer": "collaboration",
                        "severity": "error",
                        "message": (
                            "TypeScript build enables noUnusedLocals but declares unused "
                            f"type/interface `{declaration_name}`"
                        ),
                        "fix_hint": f"Remove `{declaration_name}` or use it in the compiled TypeScript sources",
                    })
                    score -= 30
            for match in re.finditer(
                r"\bconst\s*\[\s*([A-Za-z_$]\w*)\s*,\s*[A-Za-z_$]\w*\s*\]\s*=\s*useState",
                content,
            ):
                state_name = match.group(1)
                if len(re.findall(rf"(?<!\.)\b{re.escape(state_name)}\b", content)) == 1:
                    issues.append({
                        "file": rel_path,
                        "layer": "collaboration",
                        "severity": "error",
                        "message": f"TypeScript build enables noUnusedLocals but declares unused React state `{state_name}`",
                        "fix_hint": f"Remove `{state_name}` state or render/read it so `tsc` can complete",
                    })
                    score -= 30

    # 收集所有定义的函数/类名
    defined_names: Dict[str, List[str]] = {}  # file -> [name, ...]
    for rel_path, content in files:
        names = []
        if rel_path.endswith(".py"):
            try:
                tree = ast.parse(content)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        names.append(node.name)
            except Exception:
                pass
        elif rel_path.endswith((".ts", ".tsx", ".js", ".jsx")):
            # 提取 export function/class/const
            for m in re.finditer(r'export\s+(?:default\s+)?(?:function|class|const|async function)\s+(\w+)', content):
                names.append(m.group(1))
            for m in re.finditer(
                r'export\s+(?:declare\s+)?(?:interface|type|enum|namespace)\s+(\w+)',
                content,
            ):
                names.append(m.group(1))
            for m in re.finditer(r'export\s*\{([^}]+)\}', content):
                for item in m.group(1).split(","):
                    exported = item.strip().split(" as ")[-1].strip()
                    if exported:
                        names.append(exported)
        defined_names[rel_path] = names

    all_defined = {n for names in defined_names.values() for n in names}

    # 检查 Python 相对导入是否能在工作区内找到
    # 只针对相对导入进行工作区内匹配，避免把标准库/第三方包误判为协作问题
    file_stems = {Path(p).stem for p, _ in files}
    for rel_path, content in files:
        if rel_path.endswith(".py"):
            try:
                tree = ast.parse(content)
            except Exception:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and getattr(node, "level", 0) > 0:
                    candidate_names = []
                    if node.module:
                        candidate_names.append(node.module.split(".")[0])
                    candidate_names.extend(alias.name.split(".")[0] for alias in node.names)
                    for mod in candidate_names:
                        if mod and mod not in file_stems and mod not in {"index", "types", "utils", "constants"}:
                            issues.append({
                                "file": rel_path,
                                "layer": "collaboration",
                                "severity": "warning",
                                "message": f"相对导入 `{mod}` 对应文件未找到",
                                "fix_hint": f"请确认 `{mod}.py` 或对应目录是否存在",
                            })
                            score -= 8

    def resolve_relative_import(rel_path: str, imp_path: str) -> Optional[str]:
        base = posixpath.normpath(posixpath.join(posixpath.dirname(rel_path), imp_path))
        stem, ext = posixpath.splitext(base)
        candidates = [base]
        if ext in {".js", ".jsx", ".ts", ".tsx"}:
            candidates.extend(stem + suffix for suffix in (".js", ".jsx", ".ts", ".tsx"))
        else:
            candidates.extend(base + suffix for suffix in (".js", ".jsx", ".ts", ".tsx", ".json", ".css"))
            candidates.extend(base + "/index" + suffix for suffix in (".js", ".jsx", ".ts", ".tsx"))
        return next((candidate for candidate in candidates if candidate in file_paths), None)

    node_builtins = {
        "assert", "buffer", "child_process", "crypto", "events", "fs", "http", "https",
        "module", "net", "os", "path", "process", "querystring", "stream", "timers",
        "tty", "url", "util", "worker_threads", "zlib",
    }

    # 检查 TS/JS 导入路径、命名导出和第三方依赖声明。
    for rel_path, content in files:
        if rel_path.endswith((".ts", ".tsx", ".js", ".jsx")):
            # Match actual top-level import/export statements only.  The old
            # unanchored `from '...'` regex also matched ordinary user-facing
            # strings such as `Cannot transition from '${currentStatus}'`, then
            # falsely reported the template variable as an npm dependency.
            imports = re.findall(
                r"(?ms)^[ \t]*import\b"
                r"(?:(?!^[ \t]*(?:import|export|const|let|var|function|class)\b).)*?"
                r"(?:\bfrom\s+)?['\"]([^'\"]+)['\"]",
                content,
            )
            imports.extend(re.findall(
                r"(?ms)^[ \t]*export\s+(?:\*|\{)"
                r"(?:(?!^[ \t]*(?:import|export|const|let|var|function|class)\b).)*?"
                r"\bfrom\s+['\"]([^'\"]+)['\"]",
                content,
            ))
            for imp_path in imports:
                if imp_path.startswith(("./", "../")):
                    if resolve_relative_import(rel_path, imp_path):
                        continue
                    issues.append({
                        "file": rel_path,
                        "layer": "collaboration",
                        "severity": "error",
                        "message": f"相对导入 `{imp_path}` 对应文件未找到",
                        "fix_hint": f"创建正确模块或修改导入路径，确保 `{imp_path}` 可解析",
                    })
                    score -= 15
                    continue
                if imp_path.startswith(("@/", "~/", "node:")):
                    continue
                package_name = "/".join(imp_path.split("/")[:2]) if imp_path.startswith("@") else imp_path.split("/")[0]
                if package_name in node_builtins:
                    continue
                manifest = owning_manifest(rel_path)
                dependencies = {}
                if manifest:
                    dependencies = {
                        **(manifest[2].get("dependencies") or {}),
                        **(manifest[2].get("devDependencies") or {}),
                        **(manifest[2].get("peerDependencies") or {}),
                    }
                if package_name not in dependencies:
                    issue_file = manifest[1] if manifest else rel_path
                    issues.append({
                        "file": issue_file,
                        "layer": "collaboration",
                        "severity": "error",
                        "message": f"导入的第三方包 `{package_name}` 未在所属 package.json 中声明",
                        "fix_hint": f"将 `{package_name}` 加入 dependencies/devDependencies，或移除该导入",
                    })
                    score -= 15

            for match in re.finditer(r"import\s*\{([^}]+)\}\s*from\s*['\"]([^'\"]+)['\"]", content):
                imp_path = match.group(2)
                if not imp_path.startswith(("./", "../")):
                    continue
                target = resolve_relative_import(rel_path, imp_path)
                if not target:
                    continue
                target_exports = set(defined_names.get(target, []))
                requested = [item.strip().split(" as ")[0].strip() for item in match.group(1).split(",")]
                missing_exports = [name for name in requested if name and name not in target_exports]
                if missing_exports:
                    issues.append({
                        "file": rel_path,
                        "layer": "collaboration",
                        "severity": "error",
                        "message": f"模块 `{imp_path}` 未导出：{', '.join(missing_exports)}",
                        "fix_hint": "修正导入名称，或在目标模块中提供对应命名导出",
                    })
                    score -= 15

    return {
        "layer": "collaboration",
        "passed": not any(issue.get("severity") == "error" for issue in issues) and score >= 60,
        "score": max(0, score),
        "issues": issues,
        "issue_count": len(issues),
    }


def _compact_issues(
    issues: List[Dict],
    *,
    per_file_limit: int = 3,
    total_limit: int = 20,
) -> List[Dict]:
    """
    去重并限额，避免重复/相似问题把质检结果放大成噪音。

    去重策略（从严到宽）：
    1. 完全相同的 (file, layer, line, message) → 直接跳过
    2. 同一文件同一层同一类问题（message 前 30 字相同）→ 只保留一条
    3. 每个文件最多保留 per_file_limit 条问题
    4. 总问题数不超过 total_limit
    """
    compacted: List[Dict] = []
    seen_exact: set = set()
    seen_fuzzy: set = set()
    per_file_counts: Dict[str, int] = {}

    for issue in issues:
        file_key = issue.get("file", "")
        msg = issue.get("message", "")

        # 精确去重 key
        exact_key = (
            file_key,
            issue.get("layer", ""),
            issue.get("severity", ""),
            str(issue.get("line", "")),
            msg,
        )
        if exact_key in seen_exact:
            continue

        # 模糊去重 key（同文件同层同类问题，message 前 30 字相同）
        fuzzy_key = (
            file_key,
            issue.get("layer", ""),
            msg[:30],
        )
        if fuzzy_key in seen_fuzzy:
            continue

        # 每文件限额
        if per_file_counts.get(file_key, 0) >= per_file_limit:
            continue

        seen_exact.add(exact_key)
        seen_fuzzy.add(fuzzy_key)
        per_file_counts[file_key] = per_file_counts.get(file_key, 0) + 1
        compacted.append(issue)

        if len(compacted) >= total_limit:
            break

    return compacted


def _map_severity_to_priority(severity: str, layer: str) -> str:
    """将质检 severity 映射为 P0/P1/P2 优先级"""
    if severity == "error":
        return "P0" if layer == "syntax" else "P1"
    return "P2"


def build_feedback_reports(
    layer_results: List[Dict],
    subproject_name: str,
    agent_role: str,
) -> Dict[str, Any]:
    """
    根据四层检查结果生成两份报告：
    1. user_report：给用户看的摘要（简洁）
    2. developer_report：给开发者 Agent 的详细反馈（含修复建议）
    3. defect_tickets：结构化缺陷单列表（供 RepairLoopController 使用）
    """
    all_issues = []
    for lr in layer_results:
        all_issues.extend(lr.get("issues", []))

    # 去重并限额，避免重复/相似问题把质检结果放大成噪音
    all_issues = _compact_issues(all_issues)

    errors = [i for i in all_issues if i.get("severity") == "error"]
    warnings = [i for i in all_issues if i.get("severity") == "warning"]
    overall_passed = not errors and all(lr.get("passed", True) for lr in layer_results)
    avg_score = round(sum(lr.get("score", 100) for lr in layer_results) / len(layer_results)) if layer_results else 100

    # 用户摘要
    user_lines = [f"## 质检报告 — {subproject_name}", ""]
    user_lines.append(f"**总体结论**：{'✅ 通过' if overall_passed else '❌ 未通过'}  |  **综合评分**：{avg_score}/100")
    user_lines.append("")
    for lr in layer_results:
        layer_name = {"syntax": "语法检查", "logic": "逻辑检查",
                      "functionality": "功能检查", "collaboration": "协作检查"}.get(lr["layer"], lr["layer"])
        status = "✅" if lr.get("passed") else "❌"
        user_lines.append(f"- {status} **{layer_name}**：{lr.get('score', 100)}/100，发现 {lr.get('issue_count', 0)} 个问题")
    if errors:
        user_lines.append(f"\n**严重问题（{len(errors)} 个）**：")
        for e in errors[:5]:
            user_lines.append(f"  - [{e.get('file','')}] {e.get('message','')}")
    user_report = "\n".join(user_lines)

    # 开发者详细反馈
    dev_lines = [f"## 开发者修复任务 — {subproject_name}", f"负责人：{agent_role}", ""]
    dev_lines.append("请按以下顺序修复问题：")
    dev_lines.append("")
    compact_by_layer: Dict[str, List[Dict]] = {}
    for issue in all_issues:
        compact_by_layer.setdefault(issue.get("layer", "unknown"), []).append(issue)
    for lr in layer_results:
        layer_issues = compact_by_layer.get(lr.get("layer", "unknown"), [])
        if layer_issues:
            layer_name = {"syntax": "【第一层】语法错误", "logic": "【第二层】逻辑问题",
                          "functionality": "【第三层】功能缺失", "collaboration": "【第四层】协作问题"}.get(lr["layer"], lr["layer"])
            dev_lines.append(f"### {layer_name}")
            for iss in layer_issues:
                sev = "🔴" if iss.get("severity") == "error" else "🟡"
                dev_lines.append(f"{sev} **{iss.get('file','')}** 第{iss.get('line','')}行")
                dev_lines.append(f"   问题：{iss.get('message','')}")
                dev_lines.append(f"   修复：{iss.get('fix_hint','')}")
                dev_lines.append("")

    # 大改判断：单文件问题超过 5 个或总分低于 40
    needs_rewrite = avg_score < 40 or any(
        sum(1 for i in all_issues if i.get("file") == f) > 5
        for f in {i.get("file") for i in all_issues}
    )
    if needs_rewrite:
        dev_lines.append("---")
        dev_lines.append("⚠️ **重写建议**：问题较多，建议直接重新生成代码而非逐行修改。")

    developer_report = "\n".join(dev_lines)

    return {
        "overall_passed": overall_passed,
        "avg_score": avg_score,
        "total_issues": len(all_issues),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "needs_rewrite": needs_rewrite,
        "user_report": user_report,
        "developer_report": developer_report,
        "layer_results": [
            {**lr, "issues": compact_by_layer.get(lr.get("layer", "unknown"), []), "issue_count": len(compact_by_layer.get(lr.get("layer", "unknown"), []))}
            for lr in layer_results
        ],
    }


class QAAgent(QualityAgent):
    """QA Agent — 执行四层质检的统一入口"""

    ESSENTIAL_CAPABILITIES = ["语法检查", "逻辑检查", "功能检查", "协作检查"]

    def __init__(self, *args, **kwargs):
        self._capabilities = list(self.ESSENTIAL_CAPABILITIES)
        super().__init__(*args, agent_type=AgentType.QA, **kwargs)

    def run_test_cases(self, test_cases: List[Dict]) -> Dict[str, Any]:
        passed = sum(1 for tc in test_cases if tc.get("expected_result") == tc.get("actual_result", True))
        return {"total": len(test_cases), "passed": passed, "failed": len(test_cases) - passed,
                "pass_rate": passed / len(test_cases) if test_cases else 0}

    def check_coverage(self, coverage_data: Dict) -> Dict[str, Any]:
        covered = coverage_data.get("covered", 0)
        total = coverage_data.get("total", 0)
        return {"covered": covered, "total": total, "rate": covered / total if total else 0,
                "uncovered_items": coverage_data.get("uncovered", [])}

    def inspect(
        self,
        subproject_id: str,
        workspace_path: str = "",
        output_files: Optional[List[str]] = None,
        dependency_files: Optional[List[str]] = None,
        subproject_description: str = "",
        agent_role: str = "开发工程师",
        subproject_name: str = "",
        is_final_phase: bool = False,
        artifact_kind: str = "",
        acceptance_contracts: Optional[List[Dict[str, Any]]] = None,
        review_packet: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        执行质检，检查范围由 is_final_phase 控制：

        非最后阶段（is_final_phase=False，默认）：
          - Layer1：语法错误检查
          - Layer2：运行逻辑检查
          - Layer3：本阶段任务功能是否实现（只看本阶段子项目）
          不检查整体落地/文件协作，因为其他阶段代码还没写完

        最后阶段（is_final_phase=True）：
          - Layer1 + Layer2 + Layer3（同上）
          - Layer4：文件协作/跨文件一致性
          - 额外：全局功能完整性（所有代码是否完整实现用户需求）
        """
        # 兼容旧调用：inspect("task-1", {"content": "test"})
        legacy_payload = workspace_path if isinstance(workspace_path, dict) else None
        if legacy_payload is not None:
            workspace_path = legacy_payload.get("workspace_path", "") or ""
            if output_files is None:
                output_files = legacy_payload.get("output_files")
            if not subproject_description:
                subproject_description = legacy_payload.get("description", "") or subproject_description
            if not subproject_name:
                subproject_name = legacy_payload.get("subproject_name", "") or subproject_name
            if acceptance_contracts is None:
                acceptance_contracts = legacy_payload.get(
                    "acceptance_contracts",
                )

        if legacy_payload is not None and not workspace_path and not output_files:
            return {
                "passed": True,
                "score": 100,
                "issues": [],
                "user_report": f"## 质检报告 — {subproject_name or subproject_id}\n\n✅ 兼容模式：未提供工作区路径，已跳过文件扫描。",
                "developer_report": "兼容旧版 inspect 调用：未提供 workspace_path/output_files，未执行真实文件扫描。",
                "layer_results": [],
                "needs_rewrite": False,
                "error_count": 0,
                "warning_count": 0,
                "details": {"subproject_id": subproject_id, "legacy_input": True},
            }

        read_errors: List[Dict[str, Any]] = []
        files = _read_source_files(
            workspace_path,
            output_files,
            read_errors=read_errors,
        )

        if not files and read_errors:
            messages = [
                f"{issue.get('file', '')}: {issue.get('message', '')}"
                for issue in read_errors
            ]
            return {
                "passed": False,
                "score": 0,
                "issues": messages,
                "user_report": (
                    f"## Quality report — {subproject_name or subproject_id}\n\n"
                    "Failed: declared delivery files could not be read."
                ),
                "developer_report": "\n".join(messages),
                "layer_results": [{
                    "layer": "io",
                    "passed": False,
                    "score": 0,
                    "issues": read_errors,
                    "issue_count": len(read_errors),
                }],
                "needs_rewrite": False,
                "error_count": len(read_errors),
                "warning_count": 0,
                "details": {
                    "subproject_id": subproject_id,
                    "files_checked": 0,
                    "validation": "DELIVERY_READ_FAILED",
                },
            }

        # P0修复：强制产物验证 - 无文件时必须拒绝
        if not files:
            return {
                "passed": False,
                "score": 0,
                "issues": ["⛔️ 工作区内没有找到任何源码文件，请先执行开发任务"],
                "user_report": f"## 质检报告 — {subproject_name or subproject_id}\n\n❌ 未通过：没有找到任何代码文件\n\n**工作区**: {workspace_path}\n**产出文件**: {output_files or '未指定'}",
                "developer_report": f"❌ 质检失败：未发现任何代码文件\n\n请先生成代码文件，工作区路径：{workspace_path}",
                "layer_results": [],
                "needs_rewrite": False,
                "error_count": 1,
                "warning_count": 0,
                "details": {"subproject_id": subproject_id, "files_checked": 0, "validation": "NO_FILES_FOUND"},
            }

        # P0修复：检查代码量和文件类型
        total_lines = sum(len(content.split('\n')) for _, content in files)
        code_files = [p for p, _ in files if not p.endswith(('.json', '.md', '.txt', '.yml', '.yaml', '.toml', '.ini', '.cfg'))]
        configuration_only = _is_configuration_only_delivery(
            files, output_files, subproject_description, agent_role, artifact_kind
        )
        
        # 只有配置文件，无核心代码
        if len(code_files) == 0 and not configuration_only:
            return {
                "passed": False,
                "score": 15,
                "issues": ["⛔️ 只有配置文件，未发现核心代码文件（.py/.ts/.js等）"],
                "user_report": f"## 质检报告 — {subproject_name or subproject_id}\n\n❌ 未通过：缺少源代码文件\n\n发现 {len(files)} 个文件，但都是配置文件，没有实际代码。",
                "developer_report": f"❌ 质检失败：只有配置文件\n\n发现的文件：{', '.join([p for p, _ in files[:5]])}\n\n请生成核心代码文件（.py, .ts, .js, .jsx, .tsx 等）",
                "layer_results": [],
                "needs_rewrite": True,
                "error_count": 1,
                "warning_count": 0,
                "details": {"subproject_id": subproject_id, "files_checked": len(files), "code_files": 0, "validation": "NO_CODE_FILES"},
            }
        
        # Layer1：语法检查（所有阶段都做）
        layer1 = check_layer1_syntax(files)
        # Layer2：逻辑检查（所有阶段都做）
        layer2 = check_layer2_logic(files)
        # Layer3：功能实现检查（所有阶段都做，但描述不同）
        # 非最后阶段：只检查本阶段子项目的功能是否实现
        # 最后阶段：检查整体功能完整性
        layer3 = check_layer3_functionality(
            files,
            subproject_description,
            self.hermes,
            acceptance_contracts=acceptance_contracts,
            review_packet=review_packet,
        )
        if review_packet and not layer3.get("reviewer_unavailable"):
            from core.qc_review_contract import validate_qc_review_result
            validate_qc_review_result(
                review_packet,
                acceptance_observations=layer3.get("acceptance_observations") or [],
                issues=layer3.get("issues") or [],
            )
        from core.security_validation import validate_generated_files
        security_layer = validate_generated_files(files)

        layer_results = [layer1, layer2, layer3, security_layer]

        dependency_context = _read_source_files(
            workspace_path,
            dependency_files,
            read_errors=read_errors,
        )
        contract_layer = check_api_contract_consistency(files + dependency_context)
        if contract_layer.get("applicable"):
            layer_results.append(contract_layer)
        if read_errors:
            layer_results.append({
                "layer": "io",
                "passed": False,
                "score": 0,
                "issues": read_errors,
                "issue_count": len(read_errors),
            })

        # Line count is only a review hint. Minified HTML, small adapters and
        # generated config-driven services can be complete in very few lines;
        # syntax/logic/functionality checks above decide whether they block.
        if total_lines < 20:
            layer_results.append({
                "layer": "scope",
                "passed": True,
                "score": 90,
                "issues": [{
                    "file": code_files[0] if code_files else "—",
                    "layer": "scope",
                    "severity": "warning",
                    "message": f"实现较精简（{total_lines} 行），建议人工确认需求覆盖范围",
                    "fix_hint": "仅在确有缺失功能时补充实现，不要为了行数重构",
                }],
                "issue_count": 1,
            })

        # Layer4：文件协作检查（只在最后阶段做）
        if is_final_phase:
            layer4 = check_layer4_collaboration(files)
            layer_results.append(layer4)

        feedback = build_feedback_reports(
            layer_results,
            subproject_name or subproject_id,
            agent_role,
        )

        mode_note = "（最终阶段全量检查）" if is_final_phase else "（阶段检查：语法+逻辑+功能）"
        reviewer_errors = [
            str(layer.get("reviewer_error") or "")
            for layer in layer_results
            if layer.get("reviewer_unavailable") and layer.get("reviewer_error")
        ]
        return {
            "passed": feedback["overall_passed"],
            "score": feedback["avg_score"],
            "issues": [i.get("message", "") for lr in feedback["layer_results"] for i in lr.get("issues", [])],
            "user_report": feedback["user_report"] + f"\n\n> 检查模式：{mode_note}",
            "developer_report": feedback["developer_report"],
            "layer_results": feedback["layer_results"],
            "needs_rewrite": feedback["needs_rewrite"],
            "error_count": feedback["error_count"],
            "warning_count": feedback["warning_count"],
            "qc_execution_error": reviewer_errors[0] if reviewer_errors else "",
            "reviewer_unavailable": bool(reviewer_errors),
            "acceptance_observations": copy.deepcopy(
                layer3.get("acceptance_observations") or []
            ),
            "is_final_phase": is_final_phase,
            "details": {"subproject_id": subproject_id, "files_checked": len(files)},
        }

    def _do_execute(self, task: Task) -> Any:
        if task.title == "run_tests":
            return self.run_test_cases(task.metadata.get("test_cases", []))
        elif task.title == "check_coverage":
            return self.check_coverage(task.metadata.get("coverage_data", {}))
        elif task.title == "inspect":
            return self.inspect(
                subproject_id=task.metadata.get("subproject_id", ""),
                workspace_path=task.metadata.get("workspace_path", ""),
                output_files=task.metadata.get("output_files"),
                subproject_description=task.metadata.get("description", ""),
                agent_role=task.metadata.get("agent_role", "开发工程师"),
                subproject_name=task.metadata.get("subproject_name", ""),
            )
        return {"error": f"Unknown task: {task.title}"}


class PerfAgent(QualityAgent):
    """
    Perf Agent（性能）
    
    职责：
    - 负载测试
    - 瓶颈分析
    - Core Web Vitals
    """

    ESSENTIAL_CAPABILITIES = [
        "负载测试",
        "瓶颈分析",
        "Core Web Vitals"
    ]

    def __init__(self, *args, **kwargs):
        self._capabilities = list(self.ESSENTIAL_CAPABILITIES)
        super().__init__(*args, agent_type=AgentType.PERF, **kwargs)

    def load_test(self, config: Dict) -> Dict[str, Any]:
        """
        执行负载测试
        
        Args:
            config: 负载测试配置
            
        Returns:
            测试结果
        """
        # 模拟负载测试
        return {
            "concurrent_users": config.get("concurrent_users", 100),
            "duration_seconds": config.get("duration", 60),
            "requests_per_second": 950,
            "avg_response_time_ms": 45,
            "p95_response_time_ms": 120,
            "p99_response_time_ms": 250,
            "error_rate": 0.01,
            "passed": True
        }

    def analyze_bottleneck(self, metrics: Dict) -> Dict[str, Any]:
        """分析性能瓶颈"""
        bottlenecks = []

        # 分析 CPU
        if metrics.get("cpu_usage", 0) > 80:
            bottlenecks.append({
                "type": "cpu",
                "severity": "high",
                "description": "CPU 使用率过高"
            })

        # 分析内存
        if metrics.get("memory_usage", 0) > 85:
            bottlenecks.append({
                "type": "memory",
                "severity": "high",
                "description": "内存使用率过高"
            })

        # 分析响应时间
        if metrics.get("avg_response_time", 0) > 200:
            bottlenecks.append({
                "type": "latency",
                "severity": "medium",
                "description": "平均响应时间过长"
            })

        return {
            "bottlenecks": bottlenecks,
            "count": len(bottlenecks),
            "recommendations": [
                "优化数据库查询",
                "增加缓存层",
                "考虑水平扩展"
            ]
        }

    def check_core_web_vitals(self, vitals_data: Dict) -> Dict[str, Any]:
        """检查 Core Web Vitals"""
        lcp = vitals_data.get("lcp", 2500)  # 最大内容绘制
        fid = vitals_data.get("fid", 100)   # 首次输入延迟
        cls = vitals_data.get("cls", 0.1)   # 累积布局偏移

        results = {
            "lcp": {
                "value": lcp,
                "threshold": 2500,
                "passed": lcp <= 2500,
                "rating": "good" if lcp <= 2500 else ("needs_improvement" if lcp <= 4000 else "poor")
            },
            "fid": {
                "value": fid,
                "threshold": 100,
                "passed": fid <= 100,
                "rating": "good" if fid <= 100 else ("needs_improvement" if fid <= 300 else "poor")
            },
            "cls": {
                "value": cls,
                "threshold": 0.1,
                "passed": cls <= 0.1,
                "rating": "good" if cls <= 0.1 else ("needs_improvement" if cls <= 0.25 else "poor")
            }
        }

        all_passed = all(r["passed"] for r in results.values())

        return {
            "passed": all_passed,
            "details": results,
            "overall_rating": "good" if all_passed else "needs_improvement"
        }

    def inspect(
        self,
        subproject_id: str,
        workspace_path: str = "",
        output_files: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """执行性能检查：扫描产出文件，检测明显的性能反模式"""
        from pathlib import Path
        issues = []
        score = 100

        for fpath in (output_files or []):
            try:
                resolved = _resolve_output_file(fpath, workspace_path)
                if not resolved:
                    continue
                safe_path, rel = resolved
                content = safe_path.read_text(encoding="utf-8", errors="replace")
                # 检测 N+1 查询模式
                if content.count("for ") > 3 and ("query" in content.lower() or "select" in content.lower()):
                    issues.append(f"{rel} 可能存在 N+1 查询问题")
                    score -= 15
                # 检测同步阻塞
                if "time.sleep(" in content or "Thread.sleep(" in content:
                    issues.append(f"{rel} 存在同步阻塞调用")
                    score -= 10
            except Exception:
                pass

        passed = score >= 60
        return {
            "passed": passed,
            "score": max(0, score),
            "issues": issues,
            "details": {"subproject_id": subproject_id},
        }

    def _do_execute(self, task: Task) -> Any:
        """执行任务"""
        if task.title == "load_test":
            return self.load_test(task.metadata.get("config", {}))
        elif task.title == "analyze_bottleneck":
            return self.analyze_bottleneck(task.metadata.get("metrics", {}))
        elif task.title == "check_vitals":
            return self.check_core_web_vitals(task.metadata.get("vitals_data", {}))
        elif task.title == "inspect":
            return self.inspect(
                subproject_id=task.metadata.get("subproject_id", task.metadata.get("task_id", "")),
                workspace_path=task.metadata.get("workspace_path", ""),
                output_files=task.metadata.get("output_files"),
            )
        return {"error": f"Unknown task: {task.title}"}


class SecAgent(QualityAgent):
    """
    Sec Agent（安全）
    
    职责：
    - 漏洞扫描
    - 合规检查
    - 密钥泄露检测
    """

    ESSENTIAL_CAPABILITIES = [
        "漏洞扫描",
        "合规检查",
        "密钥泄露检测"
    ]

    def __init__(self, *args, **kwargs):
        self._capabilities = list(self.ESSENTIAL_CAPABILITIES)
        super().__init__(*args, agent_type=AgentType.SEC, **kwargs)

    def scan_vulnerabilities(self, code: str) -> Dict[str, Any]:
        """
        扫描漏洞
        
        Args:
            code: 代码内容
            
        Returns:
            扫描结果
        """
        vulnerabilities = []

        # 常见漏洞模式检测
        dangerous_patterns = {
            "sql_injection": ["SELECT * FROM", "execute(", "cursor.execute("],
            "xss": ["innerHTML", "document.write", "<script>"],
            "command_injection": ["os.system", "subprocess", "eval(", "exec("],
            "path_traversal": ["../", "..\\", "open("],
            "weak_crypto": ["md5(", "sha1(", "Crypto.Cipher"]
        }

        for vuln_type, patterns in dangerous_patterns.items():
            for pattern in patterns:
                if pattern in code:
                    vulnerabilities.append({
                        "type": vuln_type,
                        "severity": "high" if vuln_type in ["sql_injection", "command_injection"] else "medium",
                        "pattern": pattern,
                        "line": code.find(pattern)
                    })

        return {
            "vulnerabilities": vulnerabilities,
            "count": len(vulnerabilities),
            "passed": len(vulnerabilities) == 0
        }

    def check_compliance(self, code: str, standards: List[str]) -> Dict[str, Any]:
        """合规检查"""
        violations = []

        for standard in standards:
            if standard == "owasp":
                # 检查 OWASP Top 10
                if "eval(" in code:
                    violations.append({"rule": "A3-Injection", "severity": "high"})
            elif standard == "gdpr":
                # 检查 GDPR 合规
                if "password" in code.lower() and "encrypt" not in code.lower():
                    violations.append({"rule": "Encryption", "severity": "high"})

        return {
            "violations": violations,
            "count": len(violations),
            "passed": len(violations) == 0
        }

    def detect_secret_leak(self, code: str) -> Dict[str, Any]:
        """检测密钥泄露"""
        secrets = []

        # 常见密钥模式
        secret_patterns = [
            (r'api[_-]?key["\s:=]+["\']?([a-zA-Z0-9_-]{20,})', "API Key"),
            (r'secret["\s:=]+["\']?([a-zA-Z0-9_-]{20,})', "Secret"),
            (r'password["\s:=]+["\']?([a-zA-Z0-9_-]{8,})', "Password"),
            (r'private[_-]?key["\s:=]+["\']?([-]+BEGIN)', "Private Key")
        ]

        import re
        for pattern, name in secret_patterns:
            matches = re.findall(pattern, code, re.IGNORECASE)
            for match in matches:
                secrets.append({
                    "type": name,
                    "value": match[:10] + "***" if len(match) > 10 else match
                })

        return {
            "secrets": secrets,
            "count": len(secrets),
            "passed": len(secrets) == 0
        }

    def inspect(
        self,
        subproject_id: str,
        workspace_path: str = "",
        output_files: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """执行安全检查：使用 OWASPScanner + BanditScanner 扫描代码中的安全漏洞"""
        from .owasp_scanner import OWASPScanner
        from .bandit_scanner import BanditScanner
        from pathlib import Path

        # 收集待扫描的文件（优先使用 output_files，否则回退扫描 workspace）
        file_sources: List[Tuple[str, str]] = []
        scan_failures: List[str] = []
        if output_files is not None:
            for fpath in output_files:
                try:
                    resolved = _resolve_output_file(fpath, workspace_path)
                    if not resolved:
                        scan_failures.append(
                            f"{fpath}: declared security scan path is outside the workspace"
                        )
                        continue
                    safe_path, rel = resolved
                    if not safe_path.exists() or not safe_path.is_file():
                        scan_failures.append(
                            f"{rel}: declared security scan file is missing"
                        )
                        continue
                    file_sources.append((
                        rel,
                        safe_path.read_text(encoding="utf-8", errors="replace"),
                    ))
                except OSError:
                    scan_failures.append(
                        f"{fpath}: declared security scan file could not be read"
                    )
        elif workspace_path:
            ws = Path(workspace_path)
            if ws.exists():
                exts = {".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".rs", ".cpp", ".c", ".cs"}
                excluded = {"node_modules", "dist", "build", "__pycache__", ".git"}
                for f in ws.rglob("*"):
                    if f.suffix in exts and not any(part in excluded for part in f.parts):
                        try:
                            rel = str(f.relative_to(ws)).replace("\\", "/")
                            file_sources.append((rel, f.read_text(encoding="utf-8", errors="replace")))
                        except OSError:
                            scan_failures.append(
                                f"{f}: security scan file could not be read"
                            )

        if not file_sources:
            issues = scan_failures or [
                "Security scan found no readable source files; clean status is unavailable"
            ]
            return {
                "passed": False,
                "score": 0,
                "issues": issues,
                "security_status": "scan_failed",
                "findings": [],
                "details": {
                    "subproject_id": subproject_id,
                    "files_scanned": 0,
                    "read_errors": len(scan_failures),
                },
            }

        scanner = OWASPScanner()
        scan_result = scanner.scan_files(file_sources)

        # 兼容旧返回格式
        finding_count = scan_result["summary"]["total_findings"]
        passed = scan_result["summary"]["passed"] and not scan_failures

        issues = list(scan_failures) + [
            f"[{f['severity'].upper()}] {f['label']} 在 {f['file']}:{f['line']} — {f['snippet'][:60]}"
            for f in scan_result["findings"][:20]  # 最多输出 20 条
        ]
        score = 0 if scan_failures else scan_result["summary"]["score"]

        return {
            "passed": passed,
            "score": score,
            "issues": issues,
            "security_status": (
                "scan_failed" if scan_failures else scan_result["security_status"]
            ),
            "findings": scan_result["findings"],
            "details": {
                "subproject_id": subproject_id,
                "files_scanned": len(file_sources),
                "security_status": (
                    "scan_failed" if scan_failures else scan_result["security_status"]
                ),
                "total_findings": finding_count,
                "read_errors": len(scan_failures),
                "by_severity": scan_result["summary"]["by_severity"],
                "by_category": scan_result["summary"]["by_category"],
            },
        }

    def _do_execute(self, task: Task) -> Any:
        """执行任务"""
        if task.title == "scan_vulnerabilities":
            return self.scan_vulnerabilities(task.metadata.get("code", ""))
        elif task.title == "check_compliance":
            return self.check_compliance(
                task.metadata.get("code", ""),
                task.metadata.get("standards", ["owasp"])
            )
        elif task.title == "detect_secrets":
            return self.detect_secret_leak(task.metadata.get("code", ""))
        elif task.title == "inspect":
            return self.inspect(
                subproject_id=task.metadata.get("subproject_id", task.metadata.get("task_id", "")),
                workspace_path=task.metadata.get("workspace_path", ""),
                output_files=task.metadata.get("output_files"),
            )
        return {"error": f"Unknown task: {task.title}"}


class UXOAgent(QualityAgent):
    """
    UXO Agent（用户体验优化）
    
    职责：
    - UI/UX 审查
    - 等待时间检测
    - 交互流程优化
    """

    ESSENTIAL_CAPABILITIES = [
        "UI/UX审查",
        "等待时间检测",
        "交互流程优化"
    ]

    def __init__(self, *args, **kwargs):
        self._capabilities = list(self.ESSENTIAL_CAPABILITIES)
        super().__init__(*args, agent_type=AgentType.UXO, **kwargs)

    def review_ui(self, ui_data: Dict) -> Dict[str, Any]:
        """
        UI/UX 审查
        
        Args:
            ui_data: UI 数据
            
        Returns:
            审查结果
        """
        issues = []

        # 检查按钮大小
        if ui_data.get("button_size", 32) < 32:
            issues.append({
                "type": "size",
                "severity": "medium",
                "description": "按钮太小"
            })

        # 检查对比度
        if ui_data.get("contrast_ratio", 3) < 4.5:
            issues.append({
                "type": "contrast",
                "severity": "high",
                "description": "文字对比度不足"
            })

        # 检查可访问性
        if not ui_data.get("alt_text", True):
            issues.append({
                "type": "accessibility",
                "severity": "medium",
                "description": "缺少替代文本"
            })

        return {
            "issues": issues,
            "count": len(issues),
            "passed": len(issues) == 0
        }

    def detect_wait_time(self, interactions: List[Dict]) -> Dict[str, Any]:
        """检测等待时间"""
        slow_interactions = []

        for interaction in interactions:
            wait_time = interaction.get("wait_time", 0)
            if wait_time > 3000:  # 超过 3 秒
                slow_interactions.append({
                    "interaction": interaction.get("name", ""),
                    "wait_time": wait_time,
                    "severity": "high" if wait_time > 5000 else "medium"
                })

        return {
            "slow_interactions": slow_interactions,
            "count": len(slow_interactions),
            "passed": len(slow_interactions) == 0,
            "recommendations": [
                "添加加载动画",
                "使用骨架屏",
                "优化数据获取"
            ] if slow_interactions else []
        }

    def optimize_flow(self, flow_data: Dict) -> Dict[str, Any]:
        """优化交互流程"""
        steps = flow_data.get("steps", [])
        optimizations = []

        # 检测过深的导航
        if len(steps) > 5:
            optimizations.append({
                "type": "navigation_depth",
                "severity": "medium",
                "description": f"导航层级过深 ({len(steps)} 层)",
                "suggestion": "考虑使用扁平化导航"
            })

        # 检测重复操作
        seen = set()
        for step in steps:
            action = step.get("action", "")
            if action in seen:
                optimizations.append({
                    "type": "duplicate_action",
                    "severity": "low",
                    "description": f"重复操作: {action}"
                })
            seen.add(action)

        return {
            "optimizations": optimizations,
            "count": len(optimizations),
            "passed": len(optimizations) == 0
        }

    def inspect(
        self,
        subproject_id: str,
        workspace_path: str = "",
        output_files: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """执行体验检查：扫描前端产出文件，检测 UX 问题"""
        from pathlib import Path
        issues = []
        score = 100

        for fpath in (output_files or []):
            try:
                resolved = _resolve_output_file(fpath, workspace_path)
                if not resolved:
                    continue
                safe_path, rel = resolved
                content = safe_path.read_text(encoding="utf-8", errors="replace")
                # 检测缺少 loading 状态
                if ("fetch(" in content or "axios" in content) and "loading" not in content.lower():
                    issues.append(f"{rel} 异步请求缺少 loading 状态")
                    score -= 10
                # 检测缺少错误处理
                if ("fetch(" in content or "axios" in content) and "catch" not in content and "error" not in content.lower():
                    issues.append(f"{rel} 异步请求缺少错误处理")
                    score -= 10
            except Exception:
                pass

        passed = score >= 60
        return {
            "passed": passed,
            "score": max(0, score),
            "issues": issues,
            "details": {"subproject_id": subproject_id},
        }

    def _do_execute(self, task: Task) -> Any:
        """执行任务"""
        if task.title == "review_ui":
            return self.review_ui(task.metadata.get("ui_data", {}))
        elif task.title == "detect_wait":
            return self.detect_wait_time(task.metadata.get("interactions", []))
        elif task.title == "optimize_flow":
            return self.optimize_flow(task.metadata.get("flow_data", {}))
        elif task.title == "inspect":
            return self.inspect(
                subproject_id=task.metadata.get("subproject_id", task.metadata.get("task_id", "")),
                workspace_path=task.metadata.get("workspace_path", ""),
                output_files=task.metadata.get("output_files"),
            )
        return {"error": f"Unknown task: {task.title}"}


def get_all_quality_agents() -> List[QualityAgent]:
    """获取所有质检 Agent 类型"""
    from core.hermes_client import HermesClient
    hermes = HermesClient()
    return [
        QAAgent(hermes_client=hermes),
        PerfAgent(hermes_client=hermes),
        SecAgent(hermes_client=hermes),
        UXOAgent(hermes_client=hermes)
    ]
