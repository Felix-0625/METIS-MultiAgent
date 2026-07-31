"""
ExecutionAgent —— 真正执行任务的 Agent
接收子项目描述 → 调用 LLM 生成代码/文档 → 写入项目文件夹 → 更新进度
支持独立 API 配置（无配置则继承默认 HermesClient）
支持专家池：注入角色记忆 + 项目记忆 + 行为规范 + CoT
"""

import time
import json
import os
import re
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Callable, Dict, List, Optional, Any, Tuple
from core.hermes_client import HermesClient, Message, MessageRole, chat_for_json
from core.rebuild_policy import (
    RebuildPolicyError,
    assert_preserved_files_unchanged,
    assert_write_authorized,
    policy_by_path,
)
from core.project_write_fence import (
    ProjectWriteFenceConflict,
    RevokedExecutionGuard,
)
from core.delivery_documents import (
    DeliveryContentionError,
    validate_delivery_write_intents,
)
from agents.base.hermes_agent import AGENT_PRINCIPLES


class ExecutionAborted(RuntimeError):
    """Raised cooperatively before a cancelled/timed-out run can mutate files."""


class ModelOutputFormatError(ValueError):
    """Bounded delivery-format retries were exhausted."""

    def __init__(self, errors: List[str], attempts: int):
        super().__init__("Model output format retries exhausted")
        self.errors = tuple(error[:300] for error in errors)
        self.attempts = attempts


def _normalize_relative_path(value: Any) -> str:
    normalized = str(value).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


# 运行时二进制/数据文件：LLM 无法生成（也不该生成），不作为强制交付项。
# 这些文件由应用运行时创建，PM 误列时执行侧容错跳过，避免整任务判失败。
_RUNTIME_ARTIFACT_SUFFIXES = (
    ".db", ".sqlite", ".sqlite3", ".db-journal", ".bin", ".dat",
    ".pyc", ".o", ".so", ".dll", ".exe", ".png", ".jpg", ".jpeg",
    ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".pdf", ".zip",
)


def _is_runtime_artifact(path: Any) -> bool:
    normalized = _normalize_relative_path(path).lower()
    return any(normalized.endswith(suf) for suf in _RUNTIME_ARTIFACT_SUFFIXES)


def _canonical_project_path(value: Any) -> str:
    normalized = _normalize_relative_path(value)
    parts = normalized.split("/", 1)
    if parts and parts[0].lower() in {"frontend", "backend"}:
        parts[0] = parts[0].lower()
        normalized = "/".join(parts)
    return normalized


def _source_declares_api_route(method: str, path: str, source_corpus: str) -> bool:
    quote = r"['\"`]"
    if re.search(rf"{quote}{re.escape(path)}{quote}", source_corpus):
        return True

    segments = [segment for segment in path.split("/") if segment]
    for split_at in range(1, len(segments) + 1):
        mount_path = "/" + "/".join(segments[:split_at])
        route_path = (
            "/" + "/".join(segments[split_at:])
            if split_at < len(segments)
            else "/"
        )
        mount_declared = re.search(
            rf"\.\s*use\s*\(\s*{quote}{re.escape(mount_path)}{quote}\s*,",
            source_corpus,
            re.IGNORECASE,
        )
        if not mount_declared:
            continue
        direct_route = re.search(
            rf"\.\s*{re.escape(method)}\s*\(\s*"
            rf"{quote}{re.escape(route_path)}{quote}",
            source_corpus,
            re.IGNORECASE,
        )
        chained_route = re.search(
            rf"\.\s*route\s*\(\s*{quote}{re.escape(route_path)}{quote}\s*\)"
            rf"[\s\S]{{0,160}}?\.\s*{re.escape(method)}\s*\(",
            source_corpus,
            re.IGNORECASE,
        )
        if direct_route or chained_route:
            return True
    return False


def _source_files_declare_api_route(
    method: str,
    path: str,
    source_files: Dict[str, str],
) -> bool:
    """Validate framework routes, mounts, or Node HTTP request branches."""
    quote = r"['\"`]"
    direct_pattern = re.compile(
        rf"\.\s*{re.escape(method)}\s*\(\s*{quote}{re.escape(path)}{quote}",
        re.IGNORECASE,
    )
    chained_pattern = re.compile(
        rf"\.\s*route\s*\(\s*{quote}{re.escape(path)}{quote}\s*\)"
        rf"[\s\S]{{0,160}}?\.\s*{re.escape(method)}\s*\(",
        re.IGNORECASE,
    )
    if any(
        direct_pattern.search(content) or chained_pattern.search(content)
        for content in source_files.values()
    ):
        return True

    comparison = r"(?:===|==)"
    method_value = rf"{quote}{re.escape(method)}{quote}"
    path_value = rf"{quote}{re.escape(path)}{quote}"
    method_ref = r"(?:req\s*\.\s*)?method"
    path_ref = r"(?:req\s*\.\s*)?(?:url|path|pathname)"
    method_patterns = (
        re.compile(
            rf"\b{method_ref}\s*{comparison}\s*{method_value}",
            re.IGNORECASE,
        ),
        re.compile(
            rf"{method_value}\s*{comparison}\s*{method_ref}\b",
            re.IGNORECASE,
        ),
    )
    path_patterns = (
        re.compile(
            rf"\b{path_ref}\s*{comparison}\s*{path_value}",
            re.IGNORECASE,
        ),
        re.compile(
            rf"{path_value}\s*{comparison}\s*{path_ref}\b",
            re.IGNORECASE,
        ),
    )
    for content in source_files.values():
        if not re.search(r"\bcreateServer\s*\(", content):
            continue
        for branch in re.finditer(
            r"\bif\s*\((?P<condition>[\s\S]{1,320}?)\)\s*\{?",
            content,
        ):
            condition = branch.group("condition")
            if (
                any(pattern.search(condition) for pattern in method_patterns)
                and any(pattern.search(condition) for pattern in path_patterns)
            ):
                return True

    extensions = ("", ".js", ".jsx", ".ts", ".tsx")
    segments = [segment for segment in path.split("/") if segment]
    for mount_rel_path, mount_content in source_files.items():
        for split_at in range(1, len(segments) + 1):
            mount_path = "/" + "/".join(segments[:split_at])
            route_path = (
                "/" + "/".join(segments[split_at:])
                if split_at < len(segments)
                else "/"
            )
            for mount in re.finditer(
                rf"\.\s*use\s*\(\s*{quote}{re.escape(mount_path)}{quote}"
                rf"\s*,\s*([A-Za-z_$]\w*)",
                mount_content,
                re.IGNORECASE,
            ):
                binding = mount.group(1)
                module_match = re.search(
                    rf"\b(?:const|let|var)\s+{re.escape(binding)}\s*=\s*"
                    rf"require\s*\(\s*{quote}([^'\"`]+){quote}\s*\)"
                    rf"|(?:import\s+{re.escape(binding)}\s+from\s*"
                    rf"{quote}([^'\"`]+){quote})",
                    mount_content,
                )
                if not module_match:
                    continue
                module_ref = module_match.group(1) or module_match.group(2)
                if not module_ref.startswith("."):
                    continue
                mount_parent = Path(mount_rel_path).parent
                module_base = (mount_parent / module_ref).as_posix()
                candidates = [
                    f"{module_base}{extension}" for extension in extensions
                ] + [
                    f"{module_base}/index{extension}"
                    for extension in extensions[1:]
                ]
                route_content = next(
                    (
                        source_files[candidate]
                        for candidate in candidates
                        if candidate in source_files
                    ),
                    None,
                )
                if route_content is None:
                    continue
                child_direct = re.search(
                    rf"\.\s*{re.escape(method)}\s*\(\s*"
                    rf"{quote}{re.escape(route_path)}{quote}",
                    route_content,
                    re.IGNORECASE,
                )
                child_chained = re.search(
                    rf"\.\s*route\s*\(\s*{quote}{re.escape(route_path)}{quote}\s*\)"
                    rf"[\s\S]{{0,160}}?\.\s*{re.escape(method)}\s*\(",
                    route_content,
                    re.IGNORECASE,
                )
                if child_direct or child_chained:
                    return True
    return False


def _node_test_listener_issues(rel_path: str, content: str) -> List[str]:
    """Reject Node test listeners that can collide with a shared dev server."""
    normalized_path = _normalize_relative_path(rel_path).lower()
    is_node_test = (
        normalized_path.endswith((".js", ".jsx", ".ts", ".tsx"))
        and (
            "/tests/" in f"/{normalized_path}"
            or "/test/" in f"/{normalized_path}"
            or "/__tests__/" in f"/{normalized_path}"
            or bool(re.search(r"\.(?:test|spec)\.[cm]?[jt]sx?$", normalized_path))
        )
    )
    if not is_node_test or not re.search(r"\.\s*listen\s*\(", content):
        return []

    issues: List[str] = []
    has_port_zero_listener = bool(re.search(
        r"\.\s*listen\s*\(\s*(?:0\b|\{\s*port\s*:\s*0\b)",
        content,
    ))
    fixed_port_listener = bool(re.search(
        r"\.\s*listen\s*\(\s*[1-9]\d*\b",
        content,
    ))
    fixed_port_listener = fixed_port_listener or bool(re.search(
        r"\.\s*listen\s*\(\s*[^\n)]*(?:\|\||\?\?)\s*[1-9]\d*\b",
        content,
    ))
    static_port_names = {
        match.group(1)
        for match in re.finditer(
            r"\b(?:const|let|var)\s+([A-Za-z_$]\w*)\s*=\s*[1-9]\d*\b",
            content,
        )
    }
    fixed_port_listener = fixed_port_listener or any(
        re.search(rf"\.\s*listen\s*\(\s*{re.escape(name)}\b", content)
        for name in static_port_names
    )
    if fixed_port_listener or not has_port_zero_listener:
        issues.append(
            f"{rel_path}: Node tests that open a listener must bind to port 0, never a fixed/configured port."
        )
    if not re.search(r"\.\s*(?:on|once)\s*\(\s*['\"]error['\"]", content):
        issues.append(
            f"{rel_path}: Node test listeners must register an error handler before waiting for requests."
        )
    return issues


class ExecutionAgent:
    """
    执行 Agent
    - 接收子项目描述和技术栈
    - 调用 LLM 生成代码/文档/测试
    - 把产出写入项目 workspace
    - 更新进度状态
    - 支持专家池：注入角色记忆 + 项目记忆 + 行为规范 + CoT
    """

    def __init__(
        self,
        agent_id: str,
        role: str,
        workspace: Path,
        hermes_client: HermesClient,
        skill_names: Optional[List[str]] = None,
        api_config: Optional[Dict] = None,
        # 专家池支持
        expert_id: Optional[str] = None,
        project_id: Optional[str] = None,
        phase_id: Optional[str] = None,
        expert_pool=None,  # ExpertPool 实例（避免循环导入用 Any）
        allowed_path_prefixes: Optional[List[str]] = None,
        required_output_files: Optional[List[str]] = None,
        artifact_policy: Optional[Dict[str, Any]] = None,
        rebuild_file_specs: Optional[List[Dict[str, Any]]] = None,
        execution_guard: Optional[Callable[[], None]] = None,
        immutable_path_scope: bool = False,
    ):
        self.agent_id = agent_id
        self.role = role
        self.workspace = workspace
        self.skill_names = skill_names or []
        self.status = "idle"
        self.progress = 0
        self.logs: List[str] = []
        self.output_files: List[str] = []

        # 专家池绑定
        self.expert_id = expert_id
        self.project_id = project_id
        self.phase_id = phase_id
        self._expert_pool = expert_pool
        self.allowed_path_prefixes = [
            _canonical_project_path(value)
            for value in (allowed_path_prefixes or [])
            if str(value).strip()
        ]
        self.required_output_files = [
            _canonical_project_path(value)
            for value in (required_output_files or [])
            if str(value).strip()
        ]
        self.artifact_policy = dict(artifact_policy or {"kind": "runnable"})
        self.rebuild_file_specs = [dict(item) for item in (rebuild_file_specs or [])]
        self._rebuild_policy = policy_by_path(self.rebuild_file_specs)
        self._execution_guard = execution_guard
        self._immutable_path_scope = bool(immutable_path_scope)
        self._fully_exposed_existing_paths: set[str] = set()

        # 独立 API 配置：克隆一个新的 HermesClient，注入独立配置
        if api_config:
            generator_config = api_config.get("generator")
            generator_config = generator_config if isinstance(generator_config, dict) else {}
            self._hermes = HermesClient(
                base_url=api_config.get("api_base") or hermes_client.base_url,
                api_key=api_config.get("api_key") or hermes_client.api_key,
                model=generator_config.get("model") or api_config.get("model") or hermes_client.model,
                max_tokens=api_config.get("max_tokens") or hermes_client.max_tokens,
                temperature=(generator_config.get("temperature")
                             if generator_config.get("temperature") is not None else 0.1),
            )
        else:
            # 无独立配置，直接使用全局默认 client
            self._hermes = hermes_client

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.logs.append(entry)

    def _check_execution_guard(self) -> None:
        if self._execution_guard is not None:
            self._execution_guard()

    def _chat(self, messages):
        """Guard both sides of blocking model I/O.

        A Python worker thread cannot be killed safely.  The orchestration
        timeout therefore revokes this guard; when model I/O returns, the
        response is discarded before parsing or workspace writes.
        """
        self._check_execution_guard()
        response = chat_for_json(self._hermes, messages, purpose="generator")
        self._check_execution_guard()
        return response

    def _path_is_allowed(self, rel_path: str) -> bool:
        rel_path = _canonical_project_path(rel_path)
        return (
            not self.allowed_path_prefixes
            or (
                not self._immutable_path_scope
                and rel_path.startswith("output/")
            )
            or (
                not self._immutable_path_scope
                and rel_path in getattr(self, "_active_repair_targets", set())
            )
            or any(
                rel_path == allowed
                or (allowed.endswith("/") and rel_path.startswith(allowed))
                for allowed in self.allowed_path_prefixes
            )
        )

    def _write_file(
        self, rel_path: str, content: str, *,
        declared_baseline_digest: Optional[str] = None,
    ) -> str:
        """写入文件到 workspace，返回相对路径"""
        write_guard = (
            self._execution_guard.write_guard()
            if self._execution_guard is not None
            and hasattr(self._execution_guard, "write_guard")
            else nullcontext()
        )
        with write_guard:
            self._check_execution_guard()
            rel_path = _canonical_project_path(str(rel_path or "").strip())
            if self._rebuild_policy and not rel_path.startswith("output/"):
                entry = self._rebuild_policy.get(rel_path)
                if entry is None:
                    raise RebuildPolicyError(
                        f"Rebuild delivery included unauthorized path: {rel_path}"
                    )
                assert_write_authorized(
                    self.workspace, entry,
                    declared_baseline_digest=declared_baseline_digest,
                )
            if not self._path_is_allowed(rel_path):
                raise ValueError(
                    f"输出路径不属于当前专家职责范围：{rel_path}；"
                    f"允许范围：{', '.join(self.allowed_path_prefixes)}"
                )
            root = self.workspace.resolve()
            target = root
            for part in Path(rel_path).parts:
                target = target / part
                is_junction = getattr(target, "is_junction", lambda: False)
                if target.is_symlink() or (
                    target.exists() and is_junction()
                ):
                    raise ValueError(f"输出路径包含符号链接或联接点：{rel_path}")
            # 安全检查（用 relative_to 严格判断，避免 /proj 误放行 /proj_evil）
            try:
                target.parent.resolve().relative_to(root)
            except ValueError:
                raise ValueError(f"路径越界：{rel_path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(content, encoding="utf-8")
                self._check_execution_guard()
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        if rel_path not in self.output_files:
            self.output_files.append(rel_path)
        self._log(f"写入文件：{rel_path}（{len(content)} 字符）")
        return rel_path

    @staticmethod
    def _execution_log_path(subproject_id: str) -> str:
        """Return one bounded runner-owned path, never a model-selected path."""
        raw_id = str(subproject_id or "").strip()
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw_id).strip(".-")
        if not safe_id:
            safe_id = "task"
        if safe_id != raw_id or len(safe_id) > 96:
            suffix = uuid.uuid5(uuid.NAMESPACE_URL, raw_id).hex[:12]
            safe_id = f"{safe_id[:80]}-{suffix}"
        return f"output/{safe_id}_execution.log"

    def _write_execution_log(self, subproject_id: str, content: str) -> str:
        """Atomically write runner metadata without widening delivery scope.

        Execution logs are generated by this runner after delivery validation.
        They are not expert-authored project files and must never be added to
        ``output_files`` or delivery evidence.
        """
        rel_path = self._execution_log_path(subproject_id)
        if not re.fullmatch(r"output/[A-Za-z0-9_.-]+_execution\.log", rel_path):
            raise ValueError("Unsafe runner execution-log path")

        write_guard = (
            self._execution_guard.write_guard()
            if self._execution_guard is not None
            and hasattr(self._execution_guard, "write_guard")
            else nullcontext()
        )
        with write_guard:
            self._check_execution_guard()
            root = self.workspace.resolve()
            target = root
            for part in Path(rel_path).parts:
                target = target / part
                is_junction = getattr(target, "is_junction", lambda: False)
                if target.is_symlink() or (target.exists() and is_junction()):
                    raise ValueError(
                        f"Runner execution-log path crosses a link: {rel_path}"
                    )
            try:
                target.parent.resolve().relative_to(root)
            except ValueError:
                raise ValueError(f"Runner execution-log path escapes workspace: {rel_path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(content, encoding="utf-8")
                self._check_execution_guard()
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        self._log(f"Runner execution log written: {rel_path}")
        return rel_path

    def _extract_code_blocks(self, text: str) -> List[Dict[str, str]]:
        """
        从 LLM 回复中提取代码块
        格式：```语言\n代码\n```  或  ```文件名\n代码\n```
        """
        pattern = r'```([^\n]*)\n(.*?)```'
        matches = re.findall(pattern, text, re.DOTALL)
        blocks = []
        for lang_or_file, code in matches:
            lang_or_file = lang_or_file.strip()
            blocks.append({"lang": lang_or_file, "code": code.strip()})
        return blocks

    def _sanitize_declared_path(self, value: str) -> Optional[str]:
        """
        校验并清洗 LLM 声明的输出路径。

        只接受明确像文件路径的值（如 index.html、src/main.py、docs/README.md）。
        README 文本、Markdown 标题、shell 命令、绝对路径、路径穿越等都返回 None，
        避免把说明文档内容误当成文件名触发路径越界或写入异常位置。
        """
        raw = str(value or "").strip().strip('`\'"')
        if not raw:
            return None

        # Render 日志里可能把 \n 展示成 /n；真实值也可能包含转义换行。
        if any(token in raw for token in ("\n", "\r", "```")):
            return None
        lowered_raw = raw.lower()
        if lowered_raw.startswith(("/n", "\\n")):
            return None
        if raw.startswith(('/', '\\', '~')):
            return None
        if re.search(r'(^|/)\.\.(/|$)', raw.replace('\\', '/')):
            return None
        if len(raw) > 180:
            return None
        if raw.lstrip().startswith(('#', '<', '{', '[')):
            return None

        normalized = _canonical_project_path(raw)
        if not normalized:
            return None
        # 过滤明显不是路径的说明性文本或命令。
        if re.search(r'\s', normalized):
            return None
        if normalized.startswith(('$', 'pip ', 'npm ', 'python ', 'uvicorn ')):
            return None

        suffix = Path(normalized).suffix.lower()
        # 黑名单：只拒绝二进制/不可交付扩展名，其余放行。
        # 之前的白名单（allowed_exts）会因 PM 规划生成新路径形态
        # （.gitkeep、无扩展名入口等）被反复丢弃，触发 path mismatch。
        blocked_exts = {
            ".exe", ".dll", ".so", ".dylib", ".o", ".a", ".lib",
            ".pyc", ".pyo", ".class", ".wasm",
            ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp",
            ".ico", ".woff", ".woff2", ".ttf", ".otf", ".eot",
            ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
            ".mp3", ".mp4", ".avi", ".mov", ".pdf",
            ".db", ".sqlite", ".sqlite3",
            # 非交付产物（编译/缓存/临时/日志），进交付集会干扰 artifact digest
            ".log", ".tmp", ".temp", ".cache", ".bak", ".swp", ".swo",
            ".map", ".pid",
            # 注意：.lock 不入黑名单——yarn.lock/Cargo.lock/composer.lock/poetry.lock
            # 等主流 lockfile 后缀即 .lock，误拒会导致构建不可复现。临时锁文件
            # （如 foo.pid 已含）由 known_filenames 或具体场景处理。
        }
        basename = Path(normalized).name.lower()
        known_filenames = {
            "dockerfile", "makefile", "readme", "license",
            ".env", ".env.example", ".gitignore", ".dockerignore", ".npmrc",
            # 无扩展名可执行入口（Express 生成器 backend/bin/www 等）
            "www", "server", "app", "main", "index", "start", "run",
        }
        parent_name = Path(normalized).parent.name.lower()
        in_bin_dir = parent_name == "bin"
        # dotfile（.gitkeep/.editorconfig/.gitattributes 等）一律放行：
        # 都是配置/占位文件，PM 规划常列，不应被路径清洗丢弃导致 mismatch。
        is_dotfile = basename.startswith(".") and len(basename) > 1
        if suffix in blocked_exts and basename not in known_filenames and not is_dotfile:
            return None

        # 二次确认 resolve 后仍位于 workspace 内。
        target = (self.workspace / normalized).resolve()
        try:
            target.relative_to(self.workspace.resolve())
        except ValueError:
            return None
        return normalized

    def _is_command_block(self, lang: str, code: str, description: str) -> bool:
        """判断 fenced block 是否只是 README 中的命令示例，而非交付脚本。"""
        lang_lower = (lang or "").strip().lower()
        if lang_lower not in {"bash", "sh", "shell", "console", "terminal", "cmd", "powershell", "ps1"}:
            return False
        desc_lower = (description or "").lower()
        expects_script = any(token in desc_lower for token in ("script", "shell", "bash", "脚本", ".sh", "powershell"))
        if expects_script:
            return False
        first_line = (code or "").strip().splitlines()[0] if (code or "").strip() else ""
        return bool(first_line) and not first_line.startswith(("#!", "set -", "function "))

    def _extract_structured_files(self, text: str) -> List[Dict[str, Any]]:
        """Extract files from the execution delivery JSON object."""
        candidates: List[str] = []
        for block in self._extract_code_blocks(text):
            lang = str(block.get("lang") or "").lower()
            if lang in ("json", "application/json", "deliverable_json"):
                candidates.append(block.get("code") or "")
        candidates.append(text.strip())

        decoder = json.JSONDecoder()
        decoded_candidates: List[Any] = []
        for raw in candidates:
            raw = raw.strip()
            if not raw:
                continue
            try:
                decoded_candidates.append(json.loads(raw))
            except Exception:
                # Models commonly add a short preface/trailer around otherwise
                # valid JSON.  Decode a complete JSON value from each possible
                # opening token instead of using a greedy regex (which breaks
                # when trailing prose contains braces).
                for match in re.finditer(r"[\{\[]", raw):
                    try:
                        data, _end = decoder.raw_decode(raw, match.start())
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    decoded_candidates.append(data)
                    break

        for data in decoded_candidates:
            # Some providers return the requested JSON as a JSON string. Unwrap
            # it once, but never interpret arbitrary prose or Python literals.
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    continue
            if isinstance(data, dict) and data.get("complete") is False:
                continue
            files = data.get("files") if isinstance(data, dict) else None
            if not isinstance(files, list):
                continue
            result: List[Dict[str, Any]] = []
            for item in files:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "").strip()
                content = item.get("content")
                if path and isinstance(content, str):
                    result.append({
                        "path": path,
                        "content": content,
                        "baseline_digest": item.get("baseline_digest"),
                        "issue_ids": list(item.get("issue_ids") or []),
                    })
            if result:
                return result
        return []

    def _looks_truncated(self, text: str) -> bool:
        """Detect common truncated LLM output before it is treated as deliverable."""
        fence_count = text.count("```")
        if fence_count % 2 == 1:
            return True
        stripped = text.rstrip()
        if "<html" in text.lower() and "</html>" not in text.lower():
            return True
        if "<script" in text.lower() and "</script>" not in text.lower():
            return True
        return stripped.endswith((".", ",", ":", "{", "[", "(", "="))

    @staticmethod
    def _is_retryable_delivery_error(error: Exception) -> bool:
        """Whether regenerating the model delivery can recover this parser error."""
        retryable_fragments = (
            "truncated", "unclosed code fences", "No deliverable files",
            "No valid deliverable file paths", "structured LLM output",
            "No structured deliverable files", "issue_ids mismatch",
            "response baseline_digest mismatch",
            "missing required delivery files", "delivery batch path mismatch",
            "existing_file_rebase_required", "unrelated_file_conflict",
        )
        message = str(error).lower()
        return any(fragment.lower() in message for fragment in retryable_fragments)

    @staticmethod
    def _allows_empty_file(path: str) -> bool:
        return Path(path).name == "__init__.py"

    def _normalize_output_path(self, path: str, lang: str, index: int, subproject_name: str, description: str) -> str:
        """Choose a stable deliverable path instead of hiding runnable files in docs."""
        safe_path = self._sanitize_declared_path(path)
        if safe_path:
            return safe_path
        safe_lang_path = self._sanitize_declared_path(lang)
        if safe_lang_path:
            return safe_lang_path
        lang_lower = (lang or "").lower()
        desc_lower = description.lower()
        if lang_lower == "html" and ("index.html" in desc_lower or "single-file" in desc_lower or "单文件" in description):
            return "index.html"
        return self._infer_filename(lang, index, subproject_name)

    def _deliverable_files(self, files: Optional[List[str]] = None) -> List[str]:
        """Return files that count as actual project output, excluding logs/docs metadata."""
        candidates = files if files is not None else self.output_files
        policy_kind = str(self.artifact_policy.get("kind") or "runnable")
        deliverable_exts = {
            ".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".py", ".json",
            ".yaml", ".yml", ".sql", ".sh", ".md", ".vue", ".svelte",
            ".env", ".example", ".dockerfile",
        }
        result = []
        for rel_path in candidates:
            p = Path(rel_path)
            normalized = rel_path.replace("\\", "/")
            if normalized.startswith("output/") or normalized.endswith(".log"):
                continue
            if policy_kind == "architecture_document":
                if (
                    normalized.startswith("docs/architecture/")
                    and p.suffix.lower() in {".md", ".txt"}
                ):
                    result.append(rel_path)
                continue
            if normalized.startswith("docs/") and p.suffix.lower() in (".md", ".txt"):
                continue
            if p.suffix.lower() in deliverable_exts or p.name.lower() in {
                "dockerfile", ".env", ".env.example",
            }:
                result.append(rel_path)
        return result

    def _missing_relative_imports(self, rel_path: str, content: str) -> List[str]:
        """Return relative JS/TS imports that do not resolve inside the workspace."""
        missing: List[str] = []
        source_path = Path(rel_path)
        for imp_path in re.findall(
            r"(?:from\s+|import\s*|require\s*\(\s*)['\"]([^'\"]+)['\"]",
            content,
        ):
            if not imp_path.startswith(("./", "../")):
                continue
            base = (self.workspace / source_path.parent / imp_path).resolve()
            try:
                base.relative_to(self.workspace.resolve())
            except ValueError:
                missing.append(imp_path)
                continue
            candidates = [base]
            module_suffixes = (".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte")
            if base.suffix in set(module_suffixes):
                stem = base.with_suffix("")
                candidates.extend(stem.with_suffix(suffix) for suffix in module_suffixes)
            elif not base.suffix:
                candidates.extend(Path(str(base) + suffix) for suffix in (*module_suffixes, ".json"))
                candidates.extend(base / f"index{suffix}" for suffix in module_suffixes)
            if not any(candidate.is_file() for candidate in candidates):
                missing.append(imp_path)
        return missing

    def _deterministic_missing_repair_content(self, rel_path: str) -> Optional[str]:
        """Build the conventional Express app module only when the workspace proves it."""
        normalized = _normalize_relative_path(rel_path)
        if normalized != "backend/src/app.js":
            return None
        manifest_path = self.workspace / "backend" / "package.json"
        routes_path = self.workspace / "backend" / "src" / "routes" / "index.js"
        frontend_manifest = self.workspace / "frontend" / "package.json"
        if not manifest_path.is_file() or not routes_path.is_file():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        dependencies = manifest.get("dependencies") or {}
        if "express" not in dependencies:
            return None

        lines = [
            "const path = require('path');",
            "const express = require('express');",
        ]
        if "cors" in dependencies:
            lines.append("const cors = require('cors');")
        lines.extend(["const routes = require('./routes');", "", "const app = express();"])
        if "cors" in dependencies:
            lines.append("app.use(cors());")
        lines.extend([
            "app.use(express.json());",
            "app.use('/api', routes);",
        ])
        if frontend_manifest.is_file():
            lines.extend([
                "",
                "const frontendDir = path.join(__dirname, '../../frontend/dist');",
                "app.use(express.static(frontendDir));",
                "app.get('*', (req, res) => res.sendFile(path.join(frontendDir, 'index.html')));",
            ])
        lines.extend(["", "module.exports = app;", ""])
        return "\n".join(lines)

    def _validate_new_output(
        self,
        description: str,
        output_files: Optional[List[str]] = None,
        tech_stack: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """Validate newly generated deliverables before reporting success."""
        issues: List[str] = []
        warnings: List[str] = []
        deliverables = self._deliverable_files(output_files)
        produced = {
            _normalize_relative_path(path)
            for path in (output_files if output_files is not None else self.output_files)
        }
        missing_required = [
            path for path in self.required_output_files
            if path not in produced and not _is_runtime_artifact(path)
        ]
        if missing_required:
            issues.append("Missing required delivery files: " + ", ".join(missing_required))
        if not deliverables:
            if str(self.artifact_policy.get("kind") or "runnable") == "architecture_document":
                issues.append(
                    "No architecture contract deliverable was written under docs/architecture/."
                )
            else:
                issues.append("No runnable/source deliverable was written. Expected real files such as index.html or src/*.")
            return {
                "valid": False,
                "issues": issues,
                "warnings": warnings,
                "deliverable_files": deliverables,
                "missing_required_files": missing_required,
            }

        if str(self.artifact_policy.get("kind") or "runnable") == "architecture_document":
            for rel_path in deliverables:
                target = self.workspace / rel_path
                if not target.is_file():
                    issues.append(f"{rel_path}: file does not exist after write.")
                    continue
                content = target.read_text(encoding="utf-8", errors="replace")
                if not content.strip():
                    issues.append(f"{rel_path}: file is empty.")
                if "\ufffd" in content:
                    issues.append(
                        f"{rel_path}: file contains replacement characters, likely encoding corruption."
                    )
            return {
                "valid": not issues,
                "issues": issues,
                "warnings": warnings,
                "deliverable_files": deliverables,
                "missing_required_files": missing_required,
            }

        contract_text = description + "\n" + " ".join(str(item) for item in (tech_stack or []))
        desc_lower = contract_text.lower()
        expects_html = "html" in desc_lower or "index.html" in desc_lower or "单文件" in description
        expects_local_storage = "localstorage" in desc_lower
        task_domain = bool(re.search(
            r"\b(?:todo|to-do|task\s+(?:app|list|manager|management))\b|待办|任务管理|任务列表|任务清单",
            description,
            re.IGNORECASE,
        ))
        task_add_required = bool(re.search(r"\b(?:add|create|new)\b|新增|添加", description, re.IGNORECASE))
        task_delete_required = bool(re.search(r"\b(?:delete|remove)\b|删除", description, re.IGNORECASE))
        expects_tasks = task_domain and task_add_required and task_delete_required
        expects_timer = "timer" in desc_lower or "番茄" in description or "start/pause/reset" in desc_lower

        normalized_deliverables = [path.replace("\\", "/").lower() for path in deliverables]
        delivered_paths = set(normalized_deliverables)
        expects_react = "react" in desc_lower
        expects_typescript = "typescript" in desc_lower or " type script" in desc_lower
        required_paths = {
            _normalize_relative_path(path)
            for path in (self.required_output_files or [])
        }
        required_package_paths = {
            path for path in required_paths
            if Path(path).name.casefold() == "package.json"
        }
        package_contract_target = (
            next(iter(required_package_paths))
            if len(required_package_paths) == 1
            else ""
        )
        description_lower = str(description or "").lower()
        frontend_framework_task = any(
            marker in description_lower
            for marker in (
                "build frontend",
                "frontend foundation",
                "frontend scaffold",
                "frontend application",
                "react app",
            )
        )
        explicit_frontend_contract = "frontend/package.json" in required_paths
        explicit_frontend_app_contract = any(
            path in required_paths
            for path in (
                "frontend/src/main.tsx",
                "frontend/src/main.jsx",
                "frontend/src/App.tsx",
                "frontend/src/App.jsx",
            )
        )
        validate_frontend_framework = explicit_frontend_contract or frontend_framework_task
        if expects_react:
            if (
                validate_frontend_framework
                and
                self._path_is_allowed("frontend/package.json")
                and "frontend/package.json" not in delivered_paths
            ):
                issues.append("React was required, but frontend/package.json was not delivered.")
            if (
                validate_frontend_framework
                and (
                    frontend_framework_task
                    or any(
                    _normalize_relative_path(path) in required_paths
                    for path in ("frontend/src/main.tsx", "frontend/src/main.jsx")
                    )
                )
                and
                any(self._path_is_allowed(path) for path in (
                    "frontend/src/main.tsx", "frontend/src/main.jsx",
                ))
                and not any(path.endswith(("/main.tsx", "/main.jsx")) for path in delivered_paths)
            ):
                issues.append("React was required, but no frontend main.tsx/main.jsx entry was delivered.")
            if (
                validate_frontend_framework
                and (
                    frontend_framework_task
                    or any(
                    _normalize_relative_path(path) in required_paths
                    for path in ("frontend/src/App.tsx", "frontend/src/App.jsx")
                    )
                )
                and
                any(self._path_is_allowed(path) for path in (
                    "frontend/src/App.tsx", "frontend/src/App.jsx",
                ))
                and not any(path.endswith(("/app.tsx", "/app.jsx")) for path in delivered_paths)
            ):
                issues.append("React was required, but no frontend App.tsx/App.jsx root was delivered.")
            incompatible = [
                path for path in delivered_paths
                if path.startswith("frontend/") and path.endswith((".vue", ".svelte"))
            ]
            if incompatible:
                issues.append(
                    "React was required, but incompatible framework files were delivered: "
                    + ", ".join(sorted(incompatible)[:5])
                )
            if (
                (frontend_framework_task or explicit_frontend_app_contract)
                and self._path_is_allowed("frontend/index.html")
                and "frontend/index.html" not in delivered_paths
                and not (self.workspace / "frontend" / "index.html").is_file()
            ):
                issues.append("React was required, but frontend/index.html was not delivered.")
        if (
            expects_typescript
            and (
                frontend_framework_task
                or explicit_frontend_app_contract
                or any(
                    path.startswith("frontend/tsconfig") and path.endswith(".json")
                    for path in required_paths
                )
            )
            and self._path_is_allowed("frontend/tsconfig.json")
            and not any(
                path.startswith("frontend/tsconfig") and path.endswith(".json")
                for path in delivered_paths
            )
            and not any((self.workspace / "frontend").glob("tsconfig*.json"))
        ):
            issues.append("TypeScript was required, but no frontend tsconfig*.json was delivered.")
        fastapi_source_in_scope = any(self._path_is_allowed(path) for path in (
            "backend/main.py",
            "backend/src/main.py",
            "backend/app/main.py",
        ))
        if "fastapi" in desc_lower and fastapi_source_in_scope:
            backend_python = [
                path for path in normalized_deliverables
                if path.startswith("backend/") and path.endswith(".py")
            ]
            backend_javascript = [
                path for path in normalized_deliverables
                if path.startswith("backend/")
                and (path.endswith((".js", ".jsx", ".ts", ".tsx")) or path == "backend/package.json")
            ]
            if not backend_python:
                issues.append(
                    "FastAPI (Python) was required, but no Python backend source file was delivered."
                )
            if backend_javascript:
                issues.append(
                    "FastAPI was required, but JavaScript/TypeScript backend files were delivered: "
                    + ", ".join(backend_javascript[:5])
                )

        expects_pytest = "pytest" in desc_lower or "testclient" in desc_lower
        pytest_in_scope = any(self._path_is_allowed(path) for path in (
            "backend/tests/test_app.py",
            "tests/test_app.py",
        ))
        if expects_pytest and pytest_in_scope:
            python_tests = [
                path for path in normalized_deliverables
                if path.endswith(".py")
                and ("/tests/" in f"/{path}" or Path(path).name.startswith("test_"))
            ]
            if not python_tests:
                issues.append(
                    "pytest/FastAPI TestClient coverage was required, but no Python test file was delivered."
                )

        html_files = [f for f in deliverables if f.lower().endswith(".html")]
        html_in_scope = any(self._path_is_allowed(path) for path in (
            "index.html",
            "frontend/index.html",
        ))
        if expects_html and html_in_scope and not html_files:
            issues.append("Expected an HTML deliverable, but no .html file was written.")

        # Feature requirements belong to the browser application as a whole,
        # not to index.html in isolation. React/Vue/Svelte keep behavior in
        # components, contexts and services while index.html is only a mount
        # shell. Build a deterministic corpus across the delivered web source
        # and validate the requirement once after per-file syntax checks.
        web_suffixes = (".html", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte")
        web_deliverables = [
            path for path in deliverables
            if path.lower().endswith(web_suffixes)
            and (
                path.replace("\\", "/").lower().startswith("frontend/")
                or path.lower().endswith(".html")
                or not any(item.startswith("backend/") for item in normalized_deliverables)
            )
        ]
        web_corpus_parts: List[str] = []
        source_corpus_parts: List[str] = []
        route_source_files: Dict[str, str] = {}
        route_source_paths: List[str] = []

        for rel_path in deliverables:
            target = self.workspace / rel_path
            if not target.exists() or not target.is_file():
                issues.append(f"{rel_path}: file does not exist after write.")
                continue
            content = target.read_text(encoding="utf-8", errors="replace")
            lowered = content.lower()
            if rel_path.lower().endswith(
                (".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".html")
            ):
                source_corpus_parts.append(content)
                normalized_route_path = _normalize_relative_path(rel_path)
                route_source_paths.append(normalized_route_path)
                route_source_files[normalized_route_path] = content
            if rel_path in web_deliverables:
                web_corpus_parts.append(lowered)
            # Empty Python package markers are valid and commonly generated.
            is_empty_package_marker = self._allows_empty_file(rel_path)
            if not content.strip() and not is_empty_package_marker:
                issues.append(f"{rel_path}: file is empty.")
            if "\ufffd" in content:
                issues.append(f"{rel_path}: file contains replacement characters, likely encoding corruption.")
            if rel_path.lower().endswith(".json"):
                try:
                    json_data = json.loads(content)
                except json.JSONDecodeError as exc:
                    issues.append(
                        f"{rel_path}: invalid strict JSON at line {exc.lineno}: {exc.msg}. "
                        "Do not append comments or explanatory text."
                    )
                else:
                    if Path(rel_path).name == "package.json":
                        scripts = json_data.get("scripts") or {}
                        for script_name, command in scripts.items():
                            if re.search(r"\bnode(?:\.exe)?\s+[^\n]*node_modules[/\\]\.bin[/\\]", str(command)):
                                issues.append(
                                    f"{rel_path}: npm script `{script_name}` must invoke the CLI directly; "
                                    "do not execute a node_modules/.bin shell shim with node."
                                )
                            nested_cli = re.match(
                                r"^\s*cd\s+(backend|frontend)\s*&&\s*(?!npm(?:\s|$))(.+)$",
                                str(command),
                            )
                            if rel_path == "package.json" and nested_cli:
                                child = nested_cli.group(1)
                                issues.append(
                                    f"{rel_path}: root npm script `{script_name}` must use "
                                    f"`npm --prefix {child} {script_name}` instead of entering the child "
                                    "directory and invoking its CLI directly."
                                )
                        if rel_path == package_contract_target:
                            declared_dependencies = {
                                str(name).casefold()
                                for section in (
                                    "dependencies", "devDependencies",
                                    "peerDependencies", "optionalDependencies",
                                )
                                for name in (
                                    json_data.get(section) or {}
                                    if isinstance(json_data.get(section), dict)
                                    else {}
                                )
                            }
                            dependency_contracts = {
                                "express": {"express"},
                                "nedb": {"nedb", "nedb-promises"},
                                "jest": {"jest"},
                                "supertest": {"supertest"},
                                "playwright": {"playwright", "@playwright/test"},
                            }
                            missing_dependencies = [
                                label
                                for label, aliases in dependency_contracts.items()
                                if re.search(
                                    rf"(?<![A-Za-z0-9_-]){re.escape(label)}"
                                    r"(?![A-Za-z0-9_-])",
                                    description_lower,
                                    re.IGNORECASE,
                                )
                                and not aliases.intersection(declared_dependencies)
                            ]
                            if missing_dependencies:
                                issues.append(
                                    f"{rel_path}: declared task dependencies are missing: "
                                    + ", ".join(missing_dependencies)
                                )
                            requires_real_tests = bool(re.search(
                                r"\b(?:jest|supertest|playwright|test scripts?|"
                                r"automated tests?)\b|自动化测试|测试脚本",
                                description,
                                re.IGNORECASE,
                            ))
                            test_script = str(scripts.get("test") or "").strip()
                            if requires_real_tests and (
                                not test_script
                                or re.search(
                                    r"(?:no test specified|not implemented|"
                                    r"\bexit\s+1\b|^\s*echo\b)",
                                    test_script,
                                    re.IGNORECASE,
                                )
                            ):
                                issues.append(
                                    f"{rel_path}: declared automated tests require a real, "
                                    "non-placeholder test script."
                                )
            normalized_path = rel_path.replace("\\", "/").lower()
            if "/tests/" in f"/{normalized_path}" and rel_path.endswith((".js", ".jsx", ".ts", ".tsx")):
                imports_production_source = bool(re.search(
                    r"(?:from\s+|import\s*)['\"][^'\"]*src/"
                    r"|require\s*\(\s*['\"][^'\"]*src/",
                    content,
                ))
                mock_app = re.search(r"\b(?:const|let|var)\s+(\w+)\s*=\s*express\s*\(\s*\)", content)
                if mock_app and re.search(
                    rf"\b{re.escape(mock_app.group(1))}\s*\.\s*(?:get|post|put|patch|delete)\s*\(",
                    content,
                ) and not imports_production_source:
                    issues.append(
                        f"{rel_path}: tests construct standalone mock routes instead of exercising production src code."
                    )
            issues.extend(_node_test_listener_issues(rel_path, content))
            if rel_path.endswith((".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte")):
                for missing_import in self._missing_relative_imports(rel_path, content):
                    issues.append(f"{rel_path}: relative import `{missing_import}` does not resolve to an existing file.")
            if rel_path.lower().endswith(".html"):
                if "<!doctype html" not in lowered and "<html" not in lowered:
                    issues.append(f"{rel_path}: missing HTML document root.")
                if "</html>" not in lowered:
                    issues.append(f"{rel_path}: missing closing </html>; output is likely truncated.")
                if "<script" in lowered and "</script>" not in lowered:
                    issues.append(f"{rel_path}: missing closing </script>; output is likely truncated.")

        # Route declarations are frequently split across files, for example
        # app.use('/tasks', router) in app.js and router.post('/') in a task
        # delivery. Validate the composed workspace, not only files written by
        # the current Agent. Exclude tests and generated/vendor directories so
        # mock routes cannot satisfy production contracts.
        seen_route_sources = set(route_source_paths)
        source_bytes = sum(
            len(part.encode("utf-8", errors="replace"))
            for part in source_corpus_parts
        )
        excluded_source_parts = {
            ".git", ".project", "node_modules", "output", "docs", "dist",
            "build", "__pycache__", "test", "tests", "__tests__",
        }
        for target in sorted(self.workspace.rglob("*")):
            if len(seen_route_sources) >= 256 or source_bytes >= 2_000_000:
                break
            if not target.is_file() or target.suffix.lower() not in {
                ".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
            }:
                continue
            rel_path = target.relative_to(self.workspace).as_posix()
            if rel_path in seen_route_sources or any(
                part.casefold() in excluded_source_parts
                for part in Path(rel_path).parts[:-1]
            ):
                continue
            content = target.read_text(encoding="utf-8", errors="replace")
            encoded_size = len(content.encode("utf-8", errors="replace"))
            if encoded_size > 256_000 or source_bytes + encoded_size > 2_000_000:
                continue
            source_corpus_parts.append(content)
            route_source_paths.append(rel_path)
            route_source_files[rel_path] = content
            seen_route_sources.add(rel_path)
            source_bytes += encoded_size

        route_contract_text = "\n".join(
            line
            for line in description.splitlines()
            if "runtime http check" not in line.casefold()
        )
        declared_api_routes = list(dict.fromkeys(
            (
                match.group(1).lower(),
                match.group(2).rstrip(".,;:，。；："),
            )
            for match in re.finditer(
                r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+"
                r"(/[A-Za-z0-9_./:{}@?=&%-]+)",
                route_contract_text,
                re.IGNORECASE,
            )
        ))
        if declared_api_routes and source_corpus_parts:
            missing_api_routes = [
                f"{method.upper()} {path}"
                for method, path in declared_api_routes
                if not _source_files_declare_api_route(
                    method, path, route_source_files,
                )
            ]
            if missing_api_routes:
                feedback_path = next((
                    path for path in route_source_paths
                    if path in {
                        _normalize_relative_path(item) for item in deliverables
                    }
                    and Path(path).suffix.lower() in {
                        ".py", ".js", ".jsx", ".ts", ".tsx",
                    }
                ), "")
                prefix = f"{feedback_path}: " if feedback_path else ""
                issues.append(
                    prefix
                    + "declared API paths are missing after composing "
                    "workspace mounts and routers: "
                    + ", ".join(missing_api_routes)
                )

        production_node_corpus = "\n".join(
            content
            for path, content in route_source_files.items()
            if not any(
                part.casefold() in {"test", "tests", "__tests__"}
                for part in Path(path).parts[:-1]
            )
        )
        if (
            re.search(r"\.\s*listen\s*\(", production_node_corpus)
            and not re.search(
                r"process\s*\.\s*env"
                r"(?:\s*\.\s*PORT|\s*\[\s*['\"]PORT['\"]\s*\])",
                production_node_corpus,
            )
        ):
            issues.append(
                "Node.js production listener must honor process.env.PORT; "
                "a fixed port cannot be the only runtime listener port."
            )

        if web_deliverables:
            web_corpus = "\n".join(web_corpus_parts)
            if expects_local_storage and "localstorage" not in web_corpus:
                warnings.append("Browser application: required localStorage persistence is not evident.")
            if expects_tasks:
                has_add = bool(re.search(
                    r"\b(?:add|create|new)[a-z0-9_]*\b|添加|新增|method\s*:\s*['\"]post['\"]",
                    web_corpus,
                    re.IGNORECASE,
                ))
                has_delete = bool(re.search(
                    r"\b(?:delete|remove)[a-z0-9_]*\b|删除|method\s*:\s*['\"]delete['\"]",
                    web_corpus,
                    re.IGNORECASE,
                ))
                if not (has_add and has_delete):
                    warnings.append(
                        "Browser application: task add/delete behavior is not evident across delivered source files."
                    )
            if expects_timer and not all(token in web_corpus for token in ("start", "reset")):
                warnings.append(
                    "Browser application: timer start/reset behavior is not evident across delivered source files."
                )

        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "warnings": warnings,
            "deliverable_files": deliverables,
            "missing_required_files": missing_required,
        }

    def _validate_fix_output(
        self,
        written_files: List[str],
        description: str,
        prev_snapshots: Dict[str, Optional[str]],
        required_changed_files: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        验证修复输出的有效性（代码层面，不依赖 LLM 自觉）。

        检查三件事：
        1. 文件是否真的有改动（diff 对比）
        2. 文件中是否还有 pass/TODO/NotImplementedError（未实现标记）
        3. 描述中提到的关键函数/方法是否在文件中存在

        返回：
        {
          "valid": bool,
          "issues": [str],   # 发现的问题列表
          "changed_files": [str],  # 真正有改动的文件
          "unchanged_files": [str],  # 没有改动的文件（可能修复无效）
        }
        """
        issues: List[str] = []
        warnings: List[str] = []
        changed_files: List[str] = []
        unchanged_files: List[str] = []
        unverified_files: List[str] = []

        for rel_path in written_files:
            # 跳过日志文件
            if rel_path.startswith("output/") or rel_path.endswith(".log"):
                continue

            target = self.workspace / rel_path
            if not target.exists():
                continue

            try:
                new_content = target.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            if rel_path.lower().endswith(".json"):
                try:
                    json_data = json.loads(new_content)
                except json.JSONDecodeError as exc:
                    issues.append(
                        f"{rel_path}：JSON 语法错误（第 {exc.lineno} 行：{exc.msg}），"
                        "禁止在 JSON 后追加注释或说明文字"
                    )
                else:
                    if Path(rel_path).name == "package.json":
                        for script_name, command in (json_data.get("scripts") or {}).items():
                            if re.search(r"\bnode(?:\.exe)?\s+[^\n]*node_modules[/\\]\.bin[/\\]", str(command)):
                                issues.append(
                                    f"{rel_path}：npm script `{script_name}` 必须直接调用 CLI，"
                                    "禁止通过 node 执行 node_modules/.bin shell shim"
                                )
                            nested_cli = re.match(
                                r"^\s*cd\s+(backend|frontend)\s*&&\s*(?!npm(?:\s|$))(.+)$",
                                str(command),
                            )
                            if rel_path == "package.json" and nested_cli:
                                child = nested_cli.group(1)
                                issues.append(
                                    f"{rel_path}：root npm script `{script_name}` 必须使用 "
                                    f"`npm --prefix {child} {script_name}`，禁止进入子目录后直接调用子项目 CLI"
                                )
            normalized_path = rel_path.replace("\\", "/").lower()
            if "/tests/" in f"/{normalized_path}" and rel_path.endswith((".js", ".jsx", ".ts", ".tsx")):
                imports_production_source = bool(re.search(
                    r"(?:from\s+|import\s*)['\"][^'\"]*src/"
                    r"|require\s*\(\s*['\"][^'\"]*src/",
                    new_content,
                ))
                mock_app = re.search(r"\b(?:const|let|var)\s+(\w+)\s*=\s*express\s*\(\s*\)", new_content)
                if mock_app and re.search(
                    rf"\b{re.escape(mock_app.group(1))}\s*\.\s*(?:get|post|put|patch|delete)\s*\(",
                    new_content,
                ) and not imports_production_source:
                    issues.append(f"{rel_path}：测试禁止自建假路由，必须调用生产 src 中的真实应用")
            issues.extend(_node_test_listener_issues(rel_path, new_content))
            if rel_path.endswith((".js", ".jsx", ".ts", ".tsx")):
                for missing_import in self._missing_relative_imports(rel_path, new_content):
                    issues.append(f"{rel_path}：相对导入 `{missing_import}` 指向的文件不存在")

            # 1. 差异对比：和修复前的快照比较
            if rel_path not in prev_snapshots:
                unverified_files.append(rel_path)
                warnings.append(f"{rel_path}：缺少修复前快照，无法确认是否发生改动")
            elif prev_snapshots[rel_path] is None:
                changed_files.append(rel_path)
            elif new_content.strip() == prev_snapshots[rel_path].strip():
                unchanged_files.append(rel_path)
                warnings.append(f"{rel_path}：文件内容未发生任何改变")
            else:
                changed_files.append(rel_path)
                previous_content = prev_snapshots[rel_path] or ""
                if (
                    rel_path.endswith((".py", ".js", ".jsx", ".ts", ".tsx"))
                    and len(previous_content) >= 1000
                    and len(new_content) < len(previous_content) * 0.55
                ):
                    issues.append(
                        f"{rel_path}：修复后源码长度骤降，疑似响应截断；拒绝覆盖完整文件"
                    )

            # 2. 检查未实现标记（pass/TODO/NotImplementedError）
            lines = new_content.split("\n")
            stub_lines = []
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                # 只检查函数体内的 pass（不检查 if/else/try 中的 pass）
                if stripped == "pass" and i > 1:
                    prev_line = lines[i - 2].strip() if i >= 2 else ""
                    if prev_line.endswith(":") and any(
                        kw in prev_line for kw in ("def ", "async def ", "class ")
                    ):
                        stub_lines.append(f"第{i}行: pass（函数体为空）")
                elif "NotImplementedError" in stripped and "raise" in stripped:
                    stub_lines.append(f"第{i}行: raise NotImplementedError（未实现）")
                elif "TODO" in line.upper() and "def " in lines[i - 2].strip() if i >= 2 else False:
                    stub_lines.append(f"第{i}行: TODO（未完成）")
            if stub_lines:
                issues.append(f"{rel_path}：仍有未实现的函数体 — {'; '.join(stub_lines[:3])}")

            # 3. 检查描述中提到的关键函数是否存在
            # 从描述中提取 def xxx / function xxx 等函数名
            declared_func_names = set(re.findall(
                r'(?m)^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\b', description
            ))
            declared_func_names.update(re.findall(
                r'(?m)^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$]\w*)\s*\(',
                description,
            ))
            func_names = list(declared_func_names)
            # Natural-language requirements may name a function inline.  Keep
            # only quoted or identifier-shaped names in the filter below so a
            # report marker such as "function truncated (...)" is not treated
            # as an implementation contract.
            func_names += re.findall(
                r'\bfunction\s+[`\'\"]?([A-Za-z_$]\w*)[`\'\"]?\s*\(', description
            )
            # 也从 fix_hint 风格的描述中提取（如"实现 create_xxx 方法"）
            func_names += re.findall(
                r'(?:实现|添加|补充|缺少)\s*[`\'"]?(\w+)[`\'"]?\s*(?:方法|函数|接口)',
                description
            )
            # English repair prose often contains phrases such as
            # "function with ...".  Those words are not function identifiers
            # and must not turn a valid repair into a false validation error.
            prose_stopwords = {
                "with", "the", "this", "that", "from", "into", "for", "and",
                "file", "files", "method", "function", "interface", "must",
                "should", "ensure", "implement", "update", "fix",
            }
            func_names = list(
                set(f for f in func_names if len(f) > 3 and f.lower() not in prose_stopwords)
            )
            func_names = list(
                set(
                    f
                    for f in func_names
                    if "_" in f
                    or any(ch.isupper() for ch in f[1:])
                    or f in declared_func_names
                    or re.search(rf"[`'\"]{re.escape(f)}[`'\"]", description)
                )
            )

            if func_names and rel_path.endswith((".py", ".ts", ".tsx", ".js", ".jsx")):
                missing_funcs = []
                for fname in func_names[:5]:  # 最多检查5个
                    # 检查函数名是否在文件中（def fname / function fname / fname(）
                    patterns = [
                        rf'\bdef\s+{re.escape(fname)}\b',
                        rf'\bfunction\s+{re.escape(fname)}\b',
                        rf'\b{re.escape(fname)}\s*[=:]\s*(?:async\s+)?(?:function|\()',
                        rf'\b{re.escape(fname)}\s*\(',
                    ]
                    found = any(re.search(p, new_content) for p in patterns)
                    if not found:
                        missing_funcs.append(fname)
                if missing_funcs:
                    issues.append(
                        f"{rel_path}：描述中要求的函数/方法未找到 — {', '.join(missing_funcs)}"
                    )

        required_changed = {
            _normalize_relative_path(path)
            for path in (required_changed_files or [])
        }
        changed_normalized = {
            _normalize_relative_path(path) for path in changed_files
        }
        required_creations = {
            path for path in required_changed
            if prev_snapshots.get(path) is None
            and (
                f"Related target file: `{path}`" in description
                or f"Target file: `{path}`" in description
            )
        }
        missing_creations = sorted(required_creations - changed_normalized)
        if missing_creations:
            issues.append(
                "Required missing repair targets were not created: "
                + ", ".join(missing_creations)
            )
        if required_changed and required_changed.isdisjoint(changed_normalized):
            issues.append("修复未改动缺陷描述明确指向的任何文件")
        elif not changed_files:
            issues.append("修复未对任何文件产生实质改动")
        valid = len(issues) == 0
        return {
            "valid": valid,
            "issues": issues,
            "warnings": warnings,
            "changed_files": changed_files,
            "unchanged_files": unchanged_files,
            "unverified_files": unverified_files,
        }

    def _infer_filename(self, lang: str, index: int, subproject_name: str) -> str:
        """根据语言/标注推断文件名"""
        # 防御：限制 lang 长度，避免文件名过长
        lang = (lang or "").strip()[:100]  # 最多取前100字符
        
        # 如果 lang 看起来像文件路径，必须通过统一路径校验才可使用。
        safe_declared = self._sanitize_declared_path(lang)
        if safe_declared:
            return safe_declared
        
        # 否则按语言推断
        ext_map = {
            'python': 'py', 'py': 'py',
            'javascript': 'js', 'js': 'js',
            'typescript': 'ts', 'ts': 'ts',
            'tsx': 'tsx', 'jsx': 'jsx',
            'html': 'html', 'css': 'css',
            'sql': 'sql', 'sh': 'sh', 'bash': 'sh',
            'yaml': 'yaml', 'yml': 'yaml',
            'json': 'json', 'markdown': 'md', 'md': 'md',
            'java': 'java', 'go': 'go', 'rust': 'rs',
            'cpp': 'cpp', 'c': 'c',
        }
        ext = ext_map.get(lang.lower()[:20], 'txt')  # 限制lang用于匹配的长度
        # 严格限制 subproject_name 长度为20字符
        safe_name = re.sub(r'[^\w\-]', '_', subproject_name)[:20]
        return f"src/{safe_name}_{index + 1}.{ext}"

    def _read_existing_files(self, priority_files: Optional[List[str]] = None) -> str:
        """
        读取 workspace 中已有的源码文件，供修复任务使用。
        priority_files: 优先读取的文件路径列表（来自质检问题的 file_path），
                        这些文件会完整读取，其余文件按上下文预算截断。
        """
        existing: List[str] = []
        self._fully_exposed_existing_paths = set()
        exts = {
            ".py", ".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte",
            ".java", ".go", ".rs", ".cpp", ".c", ".cs", ".html", ".css",
        }
        seen: set = set()
        normalized_priority = {
            str(path).replace("\\", "/").lstrip("./")
            for path in (priority_files or []) if path
        }
        accepted_paths: set[str] = set()
        registry_available = False
        try:
            from core.app_state import _phase_managers
            phase_manager = _phase_managers.get(self.project_id or "")
            if phase_manager:
                registry_available = bool(phase_manager.file_registry)
                accepted_paths = {
                    str(path).replace("\\", "/")
                    for path, owner in (phase_manager.file_registry or {}).items()
                    if owner.get("phase_id") != self.phase_id
                }
                # A repair or integration agent must see files already
                # accepted in its own phase.  Excluding them makes the model
                # recreate entrypoints and produce duplicate module trees.
                accepted_paths.update(
                    str(path).replace("\\", "/")
                    for path, owner in (phase_manager.file_registry or {}).items()
                    if owner.get("phase_id") == self.phase_id
                )
        except Exception:
            accepted_paths = set()
            registry_available = False

        # Accepted artifact ownership is authoritative. Failed generations can
        # leave orphan files on disk; never feed those stale frameworks into a
        # later frontend or integration Agent when the registry has a verified
        # dependency manifest.
        def dependency_priority(rel_path: str) -> Tuple[int, str]:
            lowered = rel_path.lower()
            score = 0
            if any(token in lowered for token in (
                "/schemas/", "/models/", "/routers/", "/routes/",
                "/api/", "/services/", "/types/",
            )):
                score += 80
            if Path(lowered).name in {
                "main.py", "app.py", "package.json", "requirements.txt",
                "pyproject.toml", "tsconfig.json", "app.tsx",
            }:
                score += 100
            return -score, lowered

        for rel_path in sorted(accepted_paths, key=dependency_priority)[:30]:
            if rel_path.lstrip("./") in normalized_priority:
                continue
            candidate = self.workspace / rel_path
            if not candidate.is_file() or candidate in seen:
                continue
            seen.add(candidate)
            try:
                content = candidate.read_text(encoding="utf-8", errors="replace")
                snippet = content[:2400] + ("\n... (truncated)" if len(content) > 2400 else "")
                if len(content) <= 2400:
                    self._fully_exposed_existing_paths.add(
                        _canonical_project_path(rel_path).casefold()
                    )
                existing.append(f"=== {rel_path} [已验收依赖] ===\n{snippet}")
            except Exception:
                pass

        # Integration agents must see the actual project layout and framework
        # before generating glue code.  Without these manifests a later phase
        # can accidentally create a second frontend/backend tree in a different
        # framework even though the earlier phase already delivered one.
        manifest_candidates = (
            "package.json", "tsconfig.json", "vite.config.ts", "vite.config.js",
            "frontend/package.json", "frontend/tsconfig.json",
            "frontend/vite.config.ts", "frontend/vite.config.js",
            "backend/package.json", "README.md",
        )
        for rel_path in manifest_candidates:
            if accepted_paths and rel_path not in accepted_paths:
                continue
            candidate = self.workspace / rel_path
            if not candidate.is_file() or candidate in seen:
                continue
            seen.add(candidate)
            try:
                content = candidate.read_text(encoding="utf-8", errors="replace")
                existing.append(f"=== {rel_path} [项目清单] ===\n{content[:4000]}")
                if len(content) <= 4000:
                    self._fully_exposed_existing_paths.add(
                        _canonical_project_path(rel_path).casefold()
                    )
            except Exception:
                pass

        # 1. 优先读取问题文件（完整内容）
        if priority_files:
            for fpath in priority_files:
                if not fpath:
                    continue
                p = Path(fpath)
                candidate = p if p.is_absolute() else (self.workspace / fpath)
                if not candidate.exists():
                    # 尝试在 src/ 下查找同名文件
                    candidates = list((self.workspace / "src").rglob(p.name)) if (self.workspace / "src").exists() else []
                    candidate = candidates[0] if candidates else None
                if candidate and candidate.is_file() and candidate not in seen:
                    seen.add(candidate)
                    try:
                        content = candidate.read_text(encoding="utf-8", errors="replace")
                        rel = str(candidate.relative_to(self.workspace)).replace("\\", "/")
                        # Repair tasks must see the whole target.  Supplying
                        # only the first 4K while asking for a complete file
                        # caused otherwise valid files to lose their tail.
                        existing.append(f"=== {rel} [优先修复] ===\n{content}")
                        self._fully_exposed_existing_paths.add(
                            _canonical_project_path(rel).casefold()
                        )
                    except Exception:
                        pass

            # Test repairs need the real package entry points before generic
            # directory snippets, otherwise the model tends to recreate mocks.
            for fpath in priority_files:
                normalized = fpath.replace("\\", "/")
                if "/tests/" not in f"/{normalized}":
                    continue
                package_root = normalized.split("/tests/", 1)[0]
                for candidate_name in ("src/server.js", "src/index.js", "src/app.js", "src/server.ts", "src/app.ts"):
                    candidate = self.workspace / package_root / candidate_name
                    if not candidate.is_file() or candidate in seen:
                        continue
                    seen.add(candidate)
                    try:
                        content = candidate.read_text(encoding="utf-8", errors="replace")
                        rel = str(candidate.relative_to(self.workspace)).replace("\\", "/")
                        existing.append(f"=== {rel} [生产入口] ===\n{content[:4000]}")
                    except Exception:
                        pass

        # 2. Legacy projects without an accepted registry fall back to a disk
        # scan. Once a registry exists, scanning unregistered files would
        # reintroduce rejected/failed artifacts into later phases.
        search_dirs = [
            self.workspace / "src",
            self.workspace / "frontend" / "src",
            self.workspace / "backend" / "src",
            self.workspace / "tests",
            self.workspace / "docs",
        ]
        for d in ([] if registry_available else search_dirs):
            if not d.exists():
                continue
            for f in sorted(d.rglob("*"))[:20]:
                if f.is_file() and f.suffix in exts and f not in seen:
                    seen.add(f)
                    try:
                        content = f.read_text(encoding="utf-8", errors="replace")
                        rel = str(f.relative_to(self.workspace)).replace("\\", "/")
                        snippet = content[:800] + ("\n... (truncated)" if len(content) > 800 else "")
                        existing.append(f"=== {rel} ===\n{snippet}")
                    except Exception:
                        pass
            if len(existing) >= 15:  # 最多 15 个文件，避免 token 超限
                break

        return "\n\n".join(existing) if existing else ""

    def execute_task(
        self,
        subproject_id: str,
        subproject_name: str,
        description: str,
        tech_stack: Optional[List[str]] = None,
        project_context: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        执行子项目任务：调用 LLM 生成代码/文档，写入 workspace

        修复任务（description 含「质检修复任务」）时，先读取现有文件，
        基于现有代码做定向修复，而不是重新生成，确保不覆盖好的部分。

        Args:
            subproject_id: 子项目 ID
            subproject_name: 子项目名称
            description: 子项目描述
            tech_stack: 技术栈列表
            project_context: 项目整体背景（PM 规划摘要）

        Returns:
            执行结果，包含 output_files、summary、status
        """
        self.status = "working"
        self.progress = 0
        self.output_files = []
        self.logs = []
        self._active_repair_targets: set[str] = set()

        is_fix_task = "质检修复任务" in description or "【质检修复任务】" in description
        integration_text = f"{self.role} {subproject_name} {description}".lower().replace("-", " ")
        is_integration_task = not is_fix_task and any(
            token in integration_text
            for token in ("full stack", "fullstack", "全栈", "联调", "integration", "integrate")
        )
        self._log(f"{'修复任务' if is_fix_task else '新建任务'}：{subproject_name}")

        normalized_tech_stack = []
        for item in tech_stack or []:
            value = item.get("requirement") if isinstance(item, dict) else item
            value = str(value or "").strip()
            if value:
                normalized_tech_stack.append(value)
        tech_stack = normalized_tech_stack
        tech_str = "、".join(tech_stack) if tech_stack else "通用技术栈"
        skill_str = "、".join(self.skill_names) if self.skill_names else "通用开发技能"

        # 修复任务：从 description 中提取涉及文件路径，优先读取这些文件
        existing_code_context = ""
        priority_files: List[str] = []
        explicit_repair_targets: List[str] = []
        # 修复前保存文件快照（用于后续 diff 验证）
        prev_snapshots: Dict[str, Optional[str]] = {}
        if is_fix_task:
            for match in re.finditer(
                r'(?:^|\n)\s*(?:Target file|Related target file)\s*:\s*`?([^`\n,]+)`?',
                description,
            ):
                declared = match.group(1).strip().strip('`\'"()[]')
                safe_declared = self._sanitize_declared_path(declared)
                if safe_declared:
                    explicit_repair_targets.append(safe_declared)
            # 从描述中提取文件路径（格式：「涉及文件：xxx」或「修改 `xxx`」）
            # 「文件：」/「涉及文件：」是自动质检修复下发的权威目标；
            # fix_hint 中的反引号路径只用于上下文，不能扩大本次可写范围。
            for pat in (
                r'(?:^|\n)\s*涉及文件[：:]\s*([^\n,，]+)',
                r'(?:^|\n)\s*文件[：:]\s*([^\n,，]+)',
            ):
                for match in re.finditer(pat, description):
                    declared = match.group(1).strip().strip('`\'"()（）[]【】')
                    safe_declared = self._sanitize_declared_path(declared)
                    if safe_declared:
                        explicit_repair_targets.append(safe_declared)
            file_patterns = [
                r'涉及文件[：:]\s*([^\n,，]+)',
                r'修改\s*[`\'"]([^`\'"]+)[`\'"]',
                r'文件[：:]\s*([^\n,，]+)',
                r'`([^`]+\.[a-zA-Z]{1,5})`',  # 反引号包裹的文件名
            ]
            for pat in file_patterns:
                for m in re.finditer(pat, description):
                    fp = m.group(1).strip().strip('`\'"()（）[]【】')
                    if fp and '.' in fp and len(fp) < 200:
                        priority_files.append(fp)
            # 去重
            seen_fp: set = set()
            priority_files = [f for f in priority_files if not (f in seen_fp or seen_fp.add(f))]  # type: ignore
            priority_files = [
                safe_path
                for path in priority_files
                if (safe_path := self._sanitize_declared_path(path))
            ]
            if explicit_repair_targets:
                priority_files = list(dict.fromkeys(explicit_repair_targets))
            repair_target_files = set(priority_files)
            self._active_repair_targets = set(repair_target_files)

            existing_code_context = self._read_existing_files(priority_files=priority_files or None)
            if existing_code_context:
                self._log(f"已读取现有代码文件（优先文件：{priority_files[:3]}），将基于现有代码进行定向修复")

            # 保存修复前的文件快照（用于 diff 验证）
            for rel_path in priority_files:
                safe_path = self._sanitize_declared_path(rel_path)
                if not safe_path:
                    continue
                target = self.workspace / safe_path
                if target.is_file():
                    try:
                        prev_snapshots[safe_path] = target.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        pass
                else:
                    prev_snapshots[safe_path] = None

            src_dir = self.workspace / "src"
            if src_dir.exists():
                for f in src_dir.rglob("*"):
                    if f.is_file() and f.suffix in {".py", ".ts", ".tsx", ".js", ".jsx"}:
                        try:
                            rel = str(f.relative_to(self.workspace)).replace("\\", "/")
                            prev_snapshots[rel] = f.read_text(encoding="utf-8", errors="replace")
                        except Exception:
                            pass
        else:
            repair_target_files = set()
            existing_code_context = self._read_existing_files()
            if existing_code_context:
                if is_integration_task:
                    self._log("已读取已验收的前序阶段代码与项目清单，将基于现有技术栈进行跨模块联调")
                else:
                    self._log("已读取已验收的前序阶段依赖与接口契约，将基于真实项目上下文实现当前任务")

        # ── 从专家池注入角色记忆 + 项目记忆 ──────────────────────────────────
        expert_memory_block = ""
        if self._expert_pool and self.expert_id and self.project_id:
            try:
                scope = "phase" if self.phase_id else "project"
                expert_memory_block = self._expert_pool.build_system_prompt(
                    expert_id=self.expert_id,
                    project_id=self.project_id,
                    scope=scope,
                    phase_id=self.phase_id or "",
                )
            except Exception:
                pass

        if is_fix_task:
            system_prompt = f"""{AGENT_PRINCIPLES}

---

你是一个专业的{self.role}，正在修复质检发现的具体问题。

【职责边界】
- 只修复质检指出的具体问题，不重构无关代码
- 只评价功能正确性，不评价代码风格（除非质检明确指出风格问题）
- 如果描述不够精确，结合现有代码定位最小安全修复；始终按下方 JSON 文件协议返回

你的技能：{skill_str}
技术栈：{tech_str}
{expert_memory_block}

【输出规范 - 严格遵守】
1. 只输出严格 JSON：{{"files":[{{"path":"原文件路径","content":"修改后的完整文件内容"}}],"complete":true}}
2. 文件路径必须和【现有代码】中的路径完全一致
3. 结果中禁止输出思考过程、尝试方法、执行命令、Markdown、修复说明或额外注释
4. JSON、YAML 等配置文件必须保持其格式合法，禁止在文件末尾追加说明文字

【修复质量要求】
- 缺少方法/函数：必须实现完整的方法体，不能只写 pass 或 TODO
- 导入错误：检查模块是否存在，不存在则移除导入或创建对应模块
- 逻辑错误：找到根因，修改最小范围的代码
- 空函数体：必须实现真实逻辑，不能用 raise NotImplementedError 敷衍

【禁止行为】
- 禁止重新生成与现有代码完全不同的新代码（会引入新问题）
- 禁止删除现有的正确功能
- 禁止改变已有的 API 接口签名（除非问题明确要求）
- 禁止只输出"修复建议"而不输出实际代码
- 禁止输出 pass、TODO、NotImplementedError 作为修复结果"""
        else:
            system_prompt = f"""{AGENT_PRINCIPLES}

---

你是一个专业的{self.role}，正在执行项目子任务。

【职责边界】
- 只实现任务描述中明确要求的功能，不过度设计
- 如果任务描述较宽泛，选择职责范围内最小但完整、可运行的实现；始终按下方 JSON 文件协议返回
- 输出必须是可运行的代码，不接受伪代码或占位符

你的技能：{skill_str}
技术栈：{tech_str}
{expert_memory_block}

【输出规范】
1. 只输出严格 JSON：{{"files":[{{"path":"文件路径","content":"完整文件内容"}}],"complete":true}}
2. 结果中禁止输出思考过程、尝试方法、执行命令、Markdown 或实现说明
3. 根据任务描述生成完整、可运行的代码；需要测试时生成测试文件
4. JSON、YAML 等配置文件必须保持其格式合法，禁止追加说明文字"""
            system_prompt += (
                "\n5. Node.js test isolation: any test that opens an HTTP/Express listener must use "
                "`app.listen(0, '127.0.0.1')`, register `server.once('error', reject)`, wait "
                "for `listening`, read `server.address().port` for every request, and close the "
                "listener. Never hard-code `3000` or any fixed/configured port in a test. If a "
                "Node.js test imports a service entry, that entry must use a `require.main === module` "
                "guard; otherwise use child_process and close it after the test."
                "\n6. Node.js runtime isolation: every production HTTP listener must bind "
                "`process.env.PORT` with an optional local fallback (for example "
                "`process.env.PORT || 3000`). Never make a fixed port the only production "
                "listener port."
            )

        user_content = f"""子项目 ID：{subproject_id}
子项目名称：{subproject_name}
任务描述：{description}
"""
        missing_repair_targets = [
            path for path in priority_files
            if prev_snapshots.get(path) is None
        ] if is_fix_task else []
        if is_fix_task and priority_files:
            system_prompt += (
                "\n\nREPAIR TARGET CONTRACT:\n"
                "- Authorized repair targets: " + ", ".join(priority_files) + ".\n"
                "- An authorized target may not exist yet. You may create it when the defect "
                "or repair guidance requires that exact file.\n"
                "- For a missing relative import, either create the named missing target with "
                "a complete implementation or correct the import to an existing compatible module.\n"
                "- Return only authorized targets; do not modify unrelated files."
            )
        if missing_repair_targets:
            user_content += (
                "\nAUTHORIZED MISSING REPAIR TARGETS (these paths do not exist yet):\n"
                + "\n".join(f"- {path}" for path in missing_repair_targets)
                + "\nMANDATORY: return complete content for every missing target listed above.\n"
            )
        if project_context:
            user_content += f"\n项目背景：\n{project_context}\n"

        if existing_code_context:
            context_title = "现有代码（请基于此修复，不要重新生成）" if is_fix_task else "前序阶段现有代码与项目清单"
            user_content += f"\n【{context_title}】\n{existing_code_context}\n"

        if is_fix_task:
            user_content += "\n请基于现有代码进行定向修复，输出修改后的完整文件内容。"
        elif is_integration_task:
            user_content += (
                "\n请在现有项目上完成联调：保持已有框架、语言、目录和入口文件；"
                "禁止创建平行的第二套 frontend/backend 工程，禁止把 React 改成 Vue（或反之）。"
                "优先修改现有文件，仅在确实缺少时创建同一技术栈的新文件。"
            )
        else:
            user_content += "\n请开始实现，生成完整的代码文件。"

        if not is_fix_task:
            system_prompt += """

DELIVERABLE CONTRACT:
- Return exactly one JSON object: {"files":[{"path":"index.html","content":"FULL_FILE_CONTENT"}],"complete":true}.
- Output strict JSON only. Never output thinking, reasoning, attempted approaches, shell commands, or commentary.
- Every file must be complete and directly runnable. Do not truncate, omit endings, use "...", or leave TODO placeholders.
- In package.json scripts, invoke local CLIs directly (for example "jest" or "vite"); never run node_modules/.bin shell shims through "node".
- If the user asks for a single-file HTML app, write the runnable file as "index.html".
- Do not place the runnable product only in README/docs. Runnable/source files are mandatory.
"""
            system_prompt += (
                "\nTASK CONTRACT PRECEDENCE:\n"
                "- The CURRENT LOCKED TASK in the user message is the authoritative execution plan.\n"
                "- Implement every objective, functional detail, implementation instruction, "
                "technology requirement, file purpose, and acceptance criterion exactly as provided.\n"
                "- Do not omit, simplify, replace, or invent task requirements. Do not perform "
                "another task or another expert's work.\n"
            )
            mandatory_delivery_files = [
                path for path in self.required_output_files
                if not _is_runtime_artifact(path)
            ]
            if mandatory_delivery_files:
                system_prompt += (
                    "\nMANDATORY DELIVERY FILES:\n"
                    + "\n".join(
                        f"- {path}" for path in mandatory_delivery_files
                    )
                    + "\n- Return complete content for every listed file in this response. "
                    "An allowed path is not optional; omitting any listed file is a delivery failure.\n"
                )
            if self.allowed_path_prefixes:
                scope_text = ", ".join(self.allowed_path_prefixes)
                system_prompt += (
                    "\nFILE OWNERSHIP CONTRACT:\n"
                    f"- You may write files only in these paths: {scope_text}.\n"
                    "- Do not recreate or modify files owned by another expert.\n"
                    "- Every returned path must satisfy this ownership contract.\n"
                )
            role_lower = (self.role or "").lower()
            if "devops" in role_lower:
                system_prompt += (
                    "\nDEVOPS HARD BOUNDARY:\n"
                    "- Produce only deployment/configuration artifacts in your allowed paths "
                    "(Dockerfile, README.md, .env.example, deploy/ or docker-compose.yml).\n"
                    "- Never return backend or frontend source files, tests, or package implementation "
                    "files, even when the project description mentions them.\n"
                    "- Always return the exact mandatory config paths listed in the delivery contract.\n"
                )
            elif "backend" in role_lower:
                system_prompt += (
                    "\nBACKEND HARD BOUNDARY:\n"
                    "- Produce only backend implementation files and explicitly listed root/config files.\n"
                    "- Do not return deployment-only files owned by a DevOps expert unless the delivery "
                    "contract explicitly lists them for you.\n"
                )
            if is_integration_task:
                system_prompt += (
                    "\nEXISTING PROJECT INTEGRATION CONTRACT:\n"
                    "- Treat the provided manifests and source files as authoritative.\n"
                    "- Preserve the existing framework, language, directory layout, and entry points.\n"
                    "- Do not create a duplicate frontend/backend application beside existing code.\n"
                    "- Resolve imports against existing files before inventing a new component or module.\n"
                )
            user_content += (
                "\n\nSTRICT DELIVERY REQUIREMENTS:\n"
                "- Return actual files, not only explanation.\n"
                "- Return JSON: {\"files\":[{\"path\":\"...\",\"content\":\"...\"}],\"complete\":true}.\n"
                "- For single-file HTML tasks, path must be index.html.\n"
                "- Output must be complete and directly runnable.\n"
            )

        if is_fix_task and self.allowed_path_prefixes:
            scope_text = ", ".join(self.allowed_path_prefixes)
            priority_text = ", ".join(priority_files) or "(use the files named in the defect report)"
            system_prompt += (
                "\nREPAIR FILE OWNERSHIP CONTRACT:\n"
                f"- You may return only files under: {scope_text}.\n"
                f"- Prefer and modify only these defect-target files: {priority_text}.\n"
                "- Do not return backend, test, or documentation files unless they are explicitly in that list.\n"
                "- Ignore files outside your ownership and return complete fixes only for owned target files.\n"
            )

        messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(role=MessageRole.USER, content=user_content),
        ]

        self.progress = 20
        self._log("调用 LLM 生成代码...")

        # Delivery writes are transactional for this execution attempt. The
        # model may emit a coherent-looking project that later fails framework,
        # import or contract validation. Persisting those rejected files used
        # to poison every subsequent phase and manual rerun.
        delivery_snapshots: Dict[str, Optional[bytes]] = {}

        def _write_delivery_file(
            rel_path: str, content: str, *,
            declared_baseline_digest: Optional[str] = None,
        ) -> None:
            normalized = _canonical_project_path(rel_path)
            if normalized not in delivery_snapshots:
                target = self.workspace / normalized
                delivery_snapshots[normalized] = (
                    target.read_bytes() if target.is_file() else None
                )
                if is_fix_task and normalized not in prev_snapshots:
                    previous = delivery_snapshots[normalized]
                    prev_snapshots[normalized] = (
                        previous.decode("utf-8", errors="replace")
                        if previous is not None
                        else None
                    )
            self._write_file(
                normalized, content,
                declared_baseline_digest=declared_baseline_digest,
            )

        def _rollback_delivery_writes() -> None:
            rollback_guard = (
                self._execution_guard.write_guard()
                if self._execution_guard is not None
                and hasattr(self._execution_guard, "write_guard")
                else nullcontext()
            )
            try:
                # Hold the exact execution generation across the whole restore.
                # A revoked worker must not restore stale bytes over a successor.
                with rollback_guard:
                    self._check_execution_guard()
                    for rel_path, previous in reversed(list(delivery_snapshots.items())):
                        target = self.workspace / rel_path
                        try:
                            if previous is None:
                                if target.is_file():
                                    target.unlink()
                            else:
                                target.parent.mkdir(parents=True, exist_ok=True)
                                target.write_bytes(previous)
                        except OSError as rollback_error:
                            self._log(f"交付回滚失败：{rel_path}: {rollback_error}")
                    # Remove empty directories created only by the rejected delivery.
                    for rel_path, previous in reversed(list(delivery_snapshots.items())):
                        if previous is not None:
                            continue
                        parent = (self.workspace / rel_path).parent
                        while parent != self.workspace:
                            try:
                                parent.rmdir()
                            except OSError:
                                break
                            parent = parent.parent
                    if delivery_snapshots:
                        rejected = set(delivery_snapshots)
                        self.output_files = [
                            path for path in self.output_files
                            if _normalize_relative_path(path) not in rejected
                            and (self.workspace / _normalize_relative_path(path)).is_file()
                        ]
                        self._log(f"已回滚未通过验证的交付文件：{len(delivery_snapshots)} 个")
            except (ProjectWriteFenceConflict, RevokedExecutionGuard) as rollback_error:
                # The orchestration transaction owns recovery after revocation.
                # Worker-side rollback must leave successor bytes untouched.
                self._log(
                    "Delivery rollback deferred to orchestration because the "
                    f"execution generation is no longer authorized: {rollback_error}"
                )

        def _enforce_repair_targets(paths: List[str]) -> None:
            """Keep an automatic repair response inside its explicit per-run targets."""
            if not is_fix_task or not repair_target_files:
                return
            normalized = {_canonical_project_path(path) for path in paths}
            unexpected = sorted(normalized - repair_target_files)
            if unexpected:
                raise ValueError(
                    "Repair delivery included files outside the explicit targets: "
                    + ", ".join(unexpected)
                )

        def _validate_delivery_intents(
            intents: List[Dict[str, str]],
        ) -> None:
            if not self.artifact_policy.get("task_id"):
                return
            registry: Dict[str, Dict[str, Any]] = {}
            try:
                from core.app_state import _phase_managers
                phase_manager = _phase_managers.get(self.project_id or "")
                if phase_manager is not None:
                    registry = dict(phase_manager.file_registry or {})
            except Exception:
                registry = {}
            try:
                validate_delivery_write_intents(
                    workspace=self.workspace,
                    intents=intents,
                    file_registry=registry,
                    task_id=str(
                        self.artifact_policy.get("task_id")
                        or subproject_id
                        or ""
                    ),
                    dependencies=list(
                        self.artifact_policy.get("task_dependencies") or []
                    ),
                    fully_exposed_paths=(
                        set(self._fully_exposed_existing_paths)
                        | {
                            _canonical_project_path(path).casefold()
                            for path in delivery_snapshots
                        }
                    ),
                )
            except DeliveryContentionError as exc:
                if exc.code == "existing_file_rebase_required" and exc.path:
                    # The bounded retry prompt below now contains the complete
                    # current bytes, so only that exact path is rebase-authorized.
                    self._fully_exposed_existing_paths.add(
                        _canonical_project_path(exc.path).casefold()
                    )
                detail = str(exc)
                if exc.repair_context:
                    detail += "\n" + exc.repair_context
                raise ValueError(detail) from exc

        try:
            # Large required-file manifests can exceed a single model response.
            # Generate bounded slices while retaining one transactional snapshot.
            chunk_targets = (
                list(dict.fromkeys(self.required_output_files))
                if not is_fix_task and len(self.required_output_files) > 3
                else []
            )
            generation_expected_paths: Optional[set[str]] = None
            generation_allowed_paths: Optional[set[str]] = (
                set(chunk_targets) if chunk_targets else None
            )
            generation_delivered_paths: set[str] = set()
            response = self._chat(messages) if not chunk_targets else {"content": ""}
            reply = response.get("content", "")
            if response.get("truncated"):
                self._log("LLM 响应达到 token 上限，转入交付重试，不写入截断内容")
                reply = "TRUNCATED_DELIVERY_RETRY"

            if not chunk_targets and (
                not reply or reply.startswith("⚠️") or reply.startswith("❌")
            ):
                self.status = "failed"
                self._log(f"LLM 调用失败：{reply[:100]}")
                return {
                    "success": False,
                    "agent_id": self.agent_id,
                    "subproject_id": subproject_id,
                    "error": reply,
                    "logs": self.logs,
                }

            self.progress = 60
            self._log("LLM 响应完成，提取代码块...")

            def _write_blocks(reply_text: str) -> None:
                """提取代码块并写入文件（可复用于重试）"""
                written_count = 0
                structured_files = self._extract_structured_files(reply_text)
                if structured_files:
                    pending_files = []
                    declared_paths = []
                    for item in structured_files:
                        safe_path = self._sanitize_declared_path(item.get("path", ""))
                        if not safe_path:
                            self._log(f"跳过无效输出路径：{str(item.get('path', ''))[:80]}")
                            continue
                        declared_paths.append(safe_path)
                        if not self._path_is_allowed(safe_path):
                            if self._rebuild_policy:
                                raise ValueError(f"Delivery included out-of-scope file: {safe_path}")
                            self._log(f"Skipped out-of-scope file: {safe_path}")
                            continue
                        rebuild_entry = self._rebuild_policy.get(safe_path)
                        if rebuild_entry and rebuild_entry.get("mode") == "patch":
                            expected_issue_ids = set(rebuild_entry.get("issue_ids") or [])
                            declared_issue_ids = set(item.get("issue_ids") or [])
                            if not declared_issue_ids:
                                # The server-side rebuild manifest is canonical.
                                # Normalize omitted identity metadata instead of
                                # asking the model to reproduce deterministic IDs.
                                item["issue_ids"] = sorted(expected_issue_ids)
                            elif declared_issue_ids != expected_issue_ids:
                                raise RebuildPolicyError(
                                    f"Patch response issue_ids mismatch for {safe_path}: "
                                    f"expected {sorted(expected_issue_ids)}, got {sorted(declared_issue_ids)}"
                                )
                            if not item.get("baseline_digest"):
                                item["baseline_digest"] = rebuild_entry.get(
                                    "baseline_digest"
                                )
                        pending_files.append((
                            safe_path, item["content"], item.get("baseline_digest"),
                        ))
                    _validate_delivery_intents([
                        {"path": safe_path, "content": content}
                        for safe_path, content, _baseline_digest in pending_files
                    ])
                    if generation_expected_paths is not None:
                        returned_paths = set(declared_paths)
                        # 运行时二进制/数据文件（.db 等）LLM 无法生成，不参与
                        # mismatch 判定，否则永远判 missing 导致整任务失败。
                        expected_for_check = {
                            p for p in generation_expected_paths
                            if not _is_runtime_artifact(p)
                        }
                        # 目录级容错：PM 规划有时把目录（如 "src"）列为 required_file，
                        # 但模型正确地生成了目录下的文件（如 "src/index.js"）。
                        # 此时 "src" 不应判为 missing，"src/index.js" 也不应判为 extra。
                        def _covered_by_returned(expect: str) -> bool:
                            if expect in returned_paths:
                                return True
                            prefix = expect.rstrip("/") + "/"
                            return any(p.startswith(prefix) for p in returned_paths)
                        def _covered_by_allowed(actual: str) -> bool:
                            allowed = generation_allowed_paths or expected_for_check
                            if actual in allowed:
                                return True
                            for exp in allowed:
                                ap = exp.rstrip("/") + "/"
                                if actual.startswith(ap):
                                    return True
                            return False
                        missing = sorted(
                            p for p in (expected_for_check - returned_paths)
                            if not _covered_by_returned(p)
                        )
                        extra = sorted(
                            p for p in returned_paths
                            if not _covered_by_allowed(p)
                        )
                        if missing or extra:
                            raise ValueError(
                                "Delivery batch path mismatch; missing: "
                                + (", ".join(missing) or "(none)")
                                + "; extra: "
                                + (", ".join(extra) or "(none)")
                            )
                    _enforce_repair_targets(declared_paths)
                    for safe_path, content, baseline_digest in pending_files:
                        _write_delivery_file(
                            safe_path, content,
                            declared_baseline_digest=baseline_digest,
                        )
                        generation_delivered_paths.add(safe_path)
                        written_count += 1
                    if written_count:
                        return
                    raise ValueError("No valid deliverable file paths found in structured LLM output.")

                # 兼容模型返回“以明确文件路径标注的 fenced code blocks”。
                # 只接收可验证的路径，不接收纯语言标签，避免把思考或命令误写成文件。
                fenced_files = []
                for block in self._extract_code_blocks(reply_text):
                    safe_path = self._sanitize_declared_path(block.get("lang", ""))
                    if safe_path and isinstance(block.get("code"), str):
                        fenced_files.append((safe_path, block["code"]))
                if fenced_files and not self._looks_truncated(reply_text):
                    _enforce_repair_targets([path for path, _content in fenced_files])
                    _validate_delivery_intents([
                        {"path": safe_path, "content": content}
                        for safe_path, content in fenced_files
                    ])
                    for safe_path, content in fenced_files:
                        if not self._path_is_allowed(safe_path):
                            if self._rebuild_policy:
                                raise ValueError(f"Delivery included out-of-scope file: {safe_path}")
                            self._log(f"Skipped out-of-scope file: {safe_path}")
                            continue
                        _write_delivery_file(safe_path, content)
                        written_count += 1
                    if written_count:
                        return

                # Common model format: a Markdown filename heading followed by
                # a language-labelled fence (for example, `### backend/app.js`
                # then ```js).  The old parser discarded these otherwise valid
                # deliveries because the fence label was a language, not a path.
                labelled_pattern = re.compile(
                    r'(?:^|\n)\s*(?:#{1,6}\s*)?(?:file(?:name)?\s*:\s*)?'
                    r'[`*\"\']*(?P<path>(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+)'
                    r'[`*\"\']*\s*:?\s*\r?\n```[^\n]*\r?\n(?P<code>.*?)```',
                    re.IGNORECASE | re.DOTALL,
                )
                if not self._looks_truncated(reply_text):
                    labelled_files = []
                    labelled_paths = []
                    for match in labelled_pattern.finditer(reply_text):
                        safe_path = self._sanitize_declared_path(match.group("path"))
                        if not safe_path:
                            continue
                        labelled_paths.append(safe_path)
                        if not self._path_is_allowed(safe_path):
                            if self._rebuild_policy:
                                raise ValueError(f"Delivery included out-of-scope file: {safe_path}")
                            continue
                        labelled_files.append((safe_path, match.group("code").strip()))
                    _enforce_repair_targets(labelled_paths)
                    _validate_delivery_intents([
                        {"path": safe_path, "content": content}
                        for safe_path, content in labelled_files
                    ])
                    for safe_path, content in labelled_files:
                        _write_delivery_file(safe_path, content)
                        written_count += 1
                    if written_count:
                        return

                if self._looks_truncated(reply_text):
                    raise ValueError("LLM output appears truncated or has unclosed code fences; refusing output.")
                raise ValueError("No structured deliverable files found. Strict JSON is required.")

            def _write_with_generation_retries(
                initial_reply: str,
                max_retries: int = 2,
                preserve_existing: bool = False,
                retry_base_content: Optional[str] = None,
            ) -> str:
                """Retry malformed/truncated model deliveries before failing the agent."""
                candidate = initial_reply
                existing_output_files = list(self.output_files)
                parser_errors: List[str] = []
                for attempt in range(max_retries + 1):
                    try:
                        _write_blocks(candidate)
                        return candidate
                    except ValueError as delivery_error:
                        retryable = self._is_retryable_delivery_error(delivery_error)
                        if not retryable:
                            raise
                        parser_errors.append(str(delivery_error))
                        if attempt >= max_retries:
                            raise ModelOutputFormatError(
                                parser_errors, attempt + 1,
                            ) from delivery_error

                        if is_fix_task:
                            # A retry is a new transaction attempt. Restore the
                            # pre-execution workspace before accepting it so a
                            # file omitted by the retry cannot survive unseen.
                            _rollback_delivery_writes()
                        self.output_files = list(existing_output_files) if preserve_existing else []
                        self._log(
                            f"交付解析失败，自动重新生成（{attempt + 1}/{max_retries}）："
                            f"{delivery_error}"
                        )
                        retry_content = (
                            f"{retry_base_content or user_content}\n\n"
                            "DELIVERY RETRY — the previous response was truncated or malformed.\n"
                            f"Parser error: {delivery_error}\n"
                            "Regenerate the assigned deliverable from scratch under these hard constraints:\n"
                            "1. Return strict JSON only: "
                            "{\"files\":[{\"path\":\"...\",\"content\":\"...\"}],\"complete\":true}.\n"
                            "2. Keep the implementation concise and within the response limit.\n"
                            "3. Include only the smallest coherent set of essential files for this assigned task.\n"
                            "4. Do not include Markdown fences, commentary, duplicated code, or ellipses.\n"
                            "5. Every returned file must be complete and syntactically closed."
                        )
                        if self.allowed_path_prefixes:
                            retry_content += (
                                "\n6. FILE OWNERSHIP: return only paths under "
                                + ", ".join(self.allowed_path_prefixes)
                                + ". Do not return files owned by another expert."
                            )
                        if is_fix_task and priority_files:
                            retry_content += (
                                "\n7. REPAIR TARGETS: prefer only these files: "
                                + ", ".join(priority_files)
                                + "."
                            )
                        retry_content += (
                            "\nJSON SCHEMA: "
                            '{"files":[{"path":"authorized/relative/path",'
                            '"content":"complete file content","baseline_digest":null,'
                            '"issue_ids":[]}]}. Return declared and authorized files only.'
                        )
                        retry_response = self._chat([
                            Message(role=MessageRole.SYSTEM, content=system_prompt),
                            Message(role=MessageRole.USER, content=retry_content),
                        ])
                        candidate = (
                            "TRUNCATED_DELIVERY_RETRY"
                            if retry_response.get("truncated")
                            else retry_response.get("content", "")
                        )
                        if not candidate:
                            raise ValueError("LLM returned an empty delivery during automatic retry.")

                raise ValueError("Deliverable generation retries exhausted.")

            if chunk_targets:
                batch_replies: List[str] = []
                for offset in range(0, len(chunk_targets), 1):
                    batch = chunk_targets[offset:offset + 1]
                    if set(batch) <= generation_delivered_paths:
                        continue
                    generation_expected_paths = set(batch)
                    batch_content = (
                        f"{user_content}\n\n"
                        "BOUNDED DELIVERY BATCH\n"
                        "Generate complete content for exactly these paths, no others:\n"
                        + "\n".join(f"- {path}" for path in batch)
                        + "\nReturn strict JSON only using the delivery schema. "
                        "This is one slice of a larger atomic delivery; preserve all "
                        "interfaces described in the project context."
                    )
                    batch_response = self._chat([
                        Message(role=MessageRole.SYSTEM, content=system_prompt),
                        Message(role=MessageRole.USER, content=batch_content),
                    ])
                    batch_reply = (
                        "TRUNCATED_DELIVERY_RETRY"
                        if batch_response.get("truncated")
                        else batch_response.get("content", "")
                    )
                    if not batch_reply:
                        raise ValueError("LLM returned an empty bounded delivery batch.")
                    batch_replies.append(_write_with_generation_retries(
                        batch_reply,
                        preserve_existing=True,
                        retry_base_content=batch_content,
                    ))
                generation_expected_paths = None
                reply = "\n".join(batch_replies)
            else:
                reply = _write_with_generation_retries(reply)

            # ── 修复任务：代码层面验证输出有效性 ──────────────────────────────
            validation_result: Optional[Dict[str, Any]] = None
            if is_fix_task:
                # 只验证源码文件（排除日志）
                src_files = [f for f in self.output_files if not f.startswith("output/")]
                validation_result = self._validate_fix_output(
                    src_files,
                    description,
                    prev_snapshots,
                    required_changed_files=priority_files,
                )
                self._log(f"修复验证：{'通过' if validation_result['valid'] else '未通过'} — {validation_result['issues'][:2]}")

                if not validation_result["valid"] and validation_result["issues"]:
                    # 验证失败：构造更明确的重试 prompt，把验证发现的问题告诉 LLM
                    validation_feedback = "\n".join(f"- {iss}" for iss in validation_result["issues"])
                    retry_user_content = (
                        f"{user_content}\n\n"
                        f"【上次修复验证失败，请重新修复】\n"
                        f"验证发现以下问题：\n{validation_feedback}\n\n"
                        f"请重新输出修复后的完整文件，确保：\n"
                        f"1. 文件内容与修复前不同（有实质改动）\n"
                        f"2. 所有函数体都有真实实现（不能是 pass/TODO/NotImplementedError）\n"
                        f"3. 描述中要求的函数/方法必须存在于文件中\n"
                        f"4. 若修复 Jest ESM，test script 必须精确使用 "
                        f"`NODE_OPTIONS=--experimental-vm-modules jest`；禁止 node_modules/.bin，"
                        f"也不能退回普通 `jest`\n"
                        f"5. root package.json 调用子项目时必须使用 `npm --prefix backend test` "
                        f"或 `npm --prefix frontend run build`；禁止 `cd 子目录 && 直接调用 CLI`"
                    )
                    if missing_repair_targets:
                        retry_user_content += (
                            "\n6. MANDATORY MISSING TARGETS: return complete implementations for "
                            "every exact path below; the repair is rejected if any is omitted:\n"
                            + "\n".join(f"- {path}" for path in missing_repair_targets)
                        )
                    retry_messages = [
                        Message(role=MessageRole.SYSTEM, content=system_prompt),
                        Message(role=MessageRole.USER, content=retry_user_content),
                    ]
                    self._log("验证失败，触发一次重试...")
                    try:
                        retry_response = self._chat(retry_messages)
                        retry_reply = (
                            "TRUNCATED_DELIVERY_RETRY"
                            if retry_response.get("truncated")
                            else retry_response.get("content", "")
                        )
                        if retry_reply and not retry_reply.startswith("⚠️") and not retry_reply.startswith("❌"):
                            # 验证重试必须从本次执行前的工作区重新开始，不能让
                            # 首次响应中未再次返回的文件成为未验证的残留改动。
                            _rollback_delivery_writes()
                            self.output_files = []
                            reply = _write_with_generation_retries(retry_reply)
                            # 重新验证
                            src_files2 = [f for f in self.output_files if not f.startswith("output/")]
                            validation_result = self._validate_fix_output(
                                src_files2,
                                description,
                                prev_snapshots,
                                required_changed_files=priority_files,
                            )
                            self._log(f"重试验证：{'通过' if validation_result['valid'] else '仍未通过'}")
                    except Exception as retry_err:
                        self._log(f"重试失败：{retry_err}")

            # 写入执行日志（包含验证结果）
                if validation_result and not validation_result.get("valid"):
                    still_missing = [
                        path for path in missing_repair_targets
                        if not (self.workspace / path).is_file()
                    ]
                    if still_missing:
                        deterministic = {
                            path: content
                            for path in still_missing
                            if (content := self._deterministic_missing_repair_content(path))
                        }
                        for path, content in deterministic.items():
                            _write_delivery_file(path, content)
                        still_missing = [
                            path for path in still_missing if path not in deterministic
                        ]
                        if deterministic:
                            validation_result = self._validate_fix_output(
                                [f for f in self.output_files if not f.startswith("output/")],
                                description,
                                prev_snapshots,
                                required_changed_files=priority_files,
                            )
                            self._log(
                                "Deterministic missing-file repair validation: "
                                + ("passed" if validation_result["valid"] else "still invalid")
                            )
                    if still_missing:
                        precise_prompt = (
                            "PRECISE MISSING-FILE REPAIR. Return strict JSON only with complete "
                            "implementations for EXACTLY these missing authorized paths, each once:\n"
                            + "\n".join(f"- {path}" for path in still_missing)
                            + "\n\nUse the existing project framework, routes, middleware, and repair guidance. "
                            "Do not return an existing file, commentary, placeholders, or Markdown.\n\n"
                            + user_content
                        )
                        try:
                            precise_response = self._chat([
                                Message(role=MessageRole.SYSTEM, content=system_prompt),
                                Message(role=MessageRole.USER, content=precise_prompt),
                            ])
                            precise_reply = precise_response.get("content", "")
                            structured = self._extract_structured_files(precise_reply)
                            returned: Dict[str, str] = {}
                            for item in structured:
                                safe_path = self._sanitize_declared_path(item.get("path", ""))
                                if not safe_path or safe_path in returned:
                                    raise ValueError("precise repair returned an invalid or duplicate path")
                                returned[safe_path] = item["content"]
                            missing_returned = sorted(set(still_missing) - set(returned))
                            if missing_returned:
                                raise ValueError(
                                    "precise repair omitted missing targets: "
                                    + ", ".join(missing_returned)
                                )
                            # The model may resend an existing primary target despite the
                            # exact instruction. Ignore that safe redundancy and commit only
                            # the requested missing files as one narrow transaction.
                            _enforce_repair_targets(still_missing)
                            for path in still_missing:
                                _write_delivery_file(path, returned[path])
                            reply = precise_reply
                            validation_result = self._validate_fix_output(
                                [f for f in self.output_files if not f.startswith("output/")],
                                description,
                                prev_snapshots,
                                required_changed_files=priority_files,
                            )
                            self._log(
                                "Precise missing-file repair validation: "
                                + ("passed" if validation_result["valid"] else "still invalid")
                            )
                        except Exception as precise_error:
                            self._log(f"Precise missing-file repair failed: {precise_error}")

            elif not is_fix_task:
                validation_result = self._validate_new_output(description, tech_stack=tech_stack)
                self._log(f"新建交付验证：{'通过' if validation_result['valid'] else '未通过'} — {validation_result['issues'][:2]}")

                if not validation_result["valid"]:
                    framework_mismatch = any(
                        "fastapi" in str(issue).lower()
                        or "incompatible framework files were delivered" in str(issue).lower()
                        or "react was required" in str(issue).lower()
                        for issue in validation_result.get("issues", [])
                    )
                    if framework_mismatch:
                        # A framework mismatch invalidates the delivery as a
                        # coherent unit. Restore the pre-execution workspace
                        # before retrying so Vue/Express files cannot poison a
                        # React/FastAPI retry.
                        _rollback_delivery_writes()
                    missing_required_files = list(
                        validation_result.get("missing_required_files") or []
                    )
                    invalid_script_files = [
                        str(issue).split(":", 1)[0]
                        for issue in validation_result.get("issues", [])
                        if "npm script" in str(issue) and ":" in str(issue)
                    ]
                    if invalid_script_files:
                        retry_user_content = (
                            "Return strict JSON containing corrected complete content for ONLY these files:\n"
                            + "\n".join(f"- {path}" for path in dict.fromkeys(invalid_script_files))
                            + "\n\nThe exact validation failures are:\n"
                            + "\n".join(
                                f"- {issue}" for issue in validation_result.get("issues", [])
                                if "npm script" in str(issue)
                            )
                            + "\n\nA root package.json must delegate to child packages with portable npm "
                            "commands such as `npm --prefix backend start`, `npm --prefix backend test`, "
                            "or `npm --prefix frontend run build`; never use `cd child && <child CLI>`. "
                            "A child package script should invoke its own CLI directly (for example "
                            "`jest`, `vite`, or `tsc`). Never use `node node_modules/.bin/...` or any "
                            "shell shim. For Jest ESM, use the exact portable Linux command "
                            "`NODE_OPTIONS=--experimental-vm-modules jest`; do not replace it with plain "
                            "`jest`. Preserve all existing dependencies and scripts that are valid."
                        )
                    elif missing_required_files:
                        retry_user_content = (
                            "The previous response successfully produced these files:\n"
                            + "\n".join(f"- {path}" for path in self.output_files)
                            + "\n\nIt omitted these mandatory delivery-contract files:\n"
                            + "\n".join(f"- {path}" for path in missing_required_files)
                            + "\n\nReturn strict JSON containing ONLY the omitted files above. "
                            "Your response is rejected unless every listed path appears exactly once. "
                            "Each path must match exactly and each file must contain complete, runnable content. "
                            "Do not resend, summarize, or modify files that were already accepted."
                        )
                    else:
                        retry_user_content = (
                            f"{user_content}\n\n"
                            "The previous answer failed deliverable validation:\n"
                            + "\n".join(f"- {issue}" for issue in validation_result["issues"])
                            + "\n\nReturn strict JSON containing the smallest set of corrected complete files. "
                            "Preserve every already-valid file and the existing project framework. "
                            "For package.json scripts invoke the CLI directly (for example `jest`, not "
                            "`node node_modules/.bin/jest`). For a missing relative import, either correct "
                            "the import to an existing file or create the exact missing file in the same "
                            "framework and within the ownership contract."
                        )
                    retry_messages = [
                        Message(role=MessageRole.SYSTEM, content=system_prompt),
                        Message(role=MessageRole.USER, content=retry_user_content),
                    ]
                    self._log("新建交付验证失败，触发一次重试...")
                    try:
                        retry_response = self._chat(retry_messages)
                        retry_reply = retry_response.get("content", "")
                        if retry_reply:
                            reply = _write_with_generation_retries(
                                retry_reply,
                                preserve_existing=not framework_mismatch,
                            )
                            validation_result = self._validate_new_output(description, tech_stack=tech_stack)
                            self._log(f"新建交付重试验证：{'通过' if validation_result['valid'] else '仍未通过'}")
                    except Exception as retry_err:
                        self._log(f"新建交付重试失败：{retry_err}")

                    # A retry can return another allowed file while leaving the
                    # actual defect untouched. Give the model one precise
                    # continuation for only the still-broken or still-missing
                    # paths. We accept it only if every target appears exactly
                    # once, so required delivery validation stays fail-closed.
                    if validation_result and not validation_result.get("valid", False):
                        continuation_targets: List[str] = []
                        for path in validation_result.get("missing_required_files") or []:
                            normalized = _normalize_relative_path(path)
                            if normalized and self._path_is_allowed(normalized):
                                continuation_targets.append(normalized)
                        for issue in validation_result.get("issues") or []:
                            match = re.match(r"^([^:\n]+):", str(issue))
                            if not match:
                                continue
                            normalized = _normalize_relative_path(match.group(1).strip())
                            if (
                                normalized
                                and self._path_is_allowed(normalized)
                                and (self.workspace / normalized).is_file()
                            ):
                                continuation_targets.append(normalized)
                        continuation_targets = list(dict.fromkeys(continuation_targets))

                        if continuation_targets:
                            target_context: List[str] = []
                            for path in continuation_targets:
                                target = self.workspace / path
                                if target.is_file():
                                    current = target.read_text(
                                        encoding="utf-8", errors="replace"
                                    )
                                    target_context.append(
                                        f"CURRENT {path}:\n{current[:12000]}"
                                    )
                            continuation_prompt = (
                                "PRECISE DELIVERY CONTINUATION. The prior retry did not resolve "
                                "the remaining validation failures.\n\n"
                                "Return strict JSON with complete corrected content for EXACTLY these paths, "
                                "each exactly once:\n"
                                + "\n".join(f"- {path}" for path in continuation_targets)
                                + "\n\nRemaining validation failures:\n"
                                + "\n".join(
                                    f"- {issue}"
                                    for issue in validation_result.get("issues") or []
                                )
                                + "\n\nDo not return any other file. Do not use placeholders, TODOs, "
                                "pseudo-code, or omit existing valid dependencies and scripts. "
                                "For root package.json delegation use portable commands such as "
                                "`npm --prefix backend start`, `npm --prefix backend test`, and "
                                "`npm --prefix frontend run build`; never use `cd child && <child CLI>`.\n\n"
                                + "\n\n".join(target_context)
                            )
                            self._log(
                                "Delivery retry remained invalid; starting one precise continuation: "
                                + ", ".join(continuation_targets)
                            )
                            try:
                                continuation_response = self._chat([
                                    Message(role=MessageRole.SYSTEM, content=system_prompt),
                                    Message(role=MessageRole.USER, content=continuation_prompt),
                                ])
                                continuation_reply = continuation_response.get("content", "")
                                structured = self._extract_structured_files(continuation_reply)
                                returned: Dict[str, str] = {}
                                for item in structured:
                                    safe_path = self._sanitize_declared_path(item.get("path", ""))
                                    if not safe_path or safe_path in returned:
                                        raise ValueError(
                                            "precise continuation returned an invalid or duplicate path"
                                        )
                                    returned[safe_path] = item["content"]
                                if set(returned) != set(continuation_targets):
                                    raise ValueError(
                                        "precise continuation paths did not exactly match targets: "
                                        f"expected={continuation_targets}, returned={sorted(returned)}"
                                    )
                                for path in continuation_targets:
                                    _write_delivery_file(path, returned[path])
                                reply = continuation_reply
                                validation_result = self._validate_new_output(
                                    description, tech_stack=tech_stack
                                )
                                self._log(
                                    "Precise continuation validation: "
                                    + ("passed" if validation_result["valid"] else "still invalid")
                                )
                            except Exception as continuation_error:
                                self._log(
                                    f"Precise continuation failed: {continuation_error}"
                                )

            validation_summary = ""
            if validation_result:
                validation_summary = (
                    "\n\n--- Deliverable Validation ---\n"
                    + json.dumps(validation_result, ensure_ascii=False, indent=2)
                )
            if False and validation_result:
                validation_summary = (
                    f"\n\n--- 修复验证结果 ---\n"
                    f"有效：{validation_result['valid']}\n"
                    f"改动文件：{validation_result['changed_files']}\n"
                    f"未改动文件：{validation_result['unchanged_files']}\n"
                    f"验证问题：{validation_result['issues']}\n"
                )
            log_content = "\n".join(self.logs) + f"\n\n--- LLM 完整回复 ---\n{reply}" + validation_summary
            self._write_execution_log(subproject_id, log_content)

            if (
                validation_result
                and not validation_result.get("valid", False)
                and is_fix_task
                and not validation_result.get("changed_files")
                and validation_result.get("issues")
                and all("修复未改动" in str(issue) for issue in validation_result["issues"])
            ):
                # A repair can be a legitimate no-op when another agent or an
                # earlier retry already resolved the reported defect. Keep the
                # strict low-level validator intact, but let phase QA make the
                # final runtime decision instead of failing on the diff alone.
                validation_result["valid"] = True
                validation_result["warnings"] = list(validation_result.get("warnings", []))
                validation_result["warnings"].append("Repair produced no diff; deferred to phase QA")
                validation_result["issues"] = []
                self._log("返修未产生差异，当前文件通过静态校验，交由阶段 QA 做最终判定")

            if validation_result and not validation_result.get("valid", False):
                self.progress = 0
                self.status = "failed"
                self._log(f"任务失败：交付验证未通过 {validation_result.get('issues', [])[:3]}")
                _rollback_delivery_writes()
                return {
                    "success": False,
                    "agent_id": self.agent_id,
                    "subproject_id": subproject_id,
                    "subproject_name": subproject_name,
                    "output_files": self.output_files,
                    "summary": reply[:500] + ("..." if len(reply) > 500 else ""),
                    "logs": self.logs,
                    "status": "failed",
                    "error": "; ".join(validation_result.get("issues", [])),
                    "validation": validation_result,
                }

            if self.rebuild_file_specs:
                assert_preserved_files_unchanged(
                    self.workspace, self.rebuild_file_specs,
                )
            self.progress = 100
            self.status = "completed"
            self._log(f"任务完成，共生成 {len(self.output_files)} 个文件")

            return {
                "success": True,
                "agent_id": self.agent_id,
                "subproject_id": subproject_id,
                "subproject_name": subproject_name,
                "output_files": self.output_files,
                "summary": reply[:500] + ("..." if len(reply) > 500 else ""),
                "logs": self.logs,
                "status": "completed",
                "validation": validation_result,  # 供调用方判断修复质量
            }

        except Exception as e:
            model_format_failure = isinstance(e, ModelOutputFormatError)
            self.status = "model_failed" if model_format_failure else "failed"
            self._log(f"执行异常：{e}")
            _rollback_delivery_writes()
            
            # 记录详细错误日志到Python logger
            import logging
            logger = logging.getLogger(__name__)
            logger.error(
                "[ExecutionAgent异常] agent_id=%s subproject=%s error=%s",
                self.agent_id, subproject_id, e, exc_info=True
            )
            
            result = {
                "success": False,
                "agent_id": self.agent_id,
                "subproject_id": subproject_id,
                "error": str(e),
                "logs": self.logs,
                "status": self.status,
            }
            if model_format_failure:
                result.update({
                    "failure_category": "model_failed",
                    "retryable": True,
                    "consumes_business_qa_round": False,
                    "consumes_issue_fix_attempt": False,
                    "model_failure_evidence": {
                        "kind": "model_output_format",
                        "attempts": e.attempts,
                        "max_automatic_retries": e.attempts - 1,
                        "parser_errors": list(e.errors),
                        "output_files_written": 0,
                    },
                })
            return result
