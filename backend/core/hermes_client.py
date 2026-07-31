"""
Hermes API 客户端封装 — 统一调用层，提供与 Hermes LLM 服务交互的能力

三层缓存架构（参考 Reasonix）：
1. 精确缓存（Layer 1）：相同 (model + messages hash) 直接返回，不调 API
2. 语义缓存白名单（Layer 2）：仅低风险类型进入，SHA256+MD5 双哈希
3. Prompt Cache（Layer 3）：system prompt prefix 匹配

TTL：精确缓存默认 10 分钟，质检/分析类语义缓存可延长到 30 分钟

用户级 API Key 隔离：
- 使用 contextvars 存储当前请求用户的 API 配置
- chat() 调用时优先使用用户级配置，实现多用户 Key 隔离
"""

import json
import time
import hashlib
import threading
import contextvars
from typing import Optional, Dict, List, Any, Callable, Tuple
from dataclasses import dataclass, field
from enum import Enum
import os

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


# 缓存 Key 前缀
_CACHE_PREFIX_EXACT = "exact:"
_CACHE_PREFIX_SEMANTIC = "sem:"
_CACHE_PREFIX_PROMPT = "prompt:"

# Layer 2 语义缓存白名单：仅低风险类型进入
_LOW_RISK_KEYWORDS = [
    "质量检查", "代码审计", "审查", "分析", "检查", "检测", "质检", "格式检查",
    "review", "inspect", "analyze", "audit", "lint", "check", "verify",
    "validate", "安全检查", "静态分析", "代码审查",
]

# ── 用户级 API 配置上下文变量 ────────────────────────────────────────────────
# 由认证中间件设置，hermes_client.chat() 优先使用此配置
current_user_api_config: contextvars.ContextVar[Optional[Dict[str, Any]]] = (
    contextvars.ContextVar("current_user_api_config", default=None)
)

_PURPOSE_TEMPERATURE_DEFAULTS = {"generator": 0.1, "reviewer": 0.0}


def _normalize_thinking_config(value: Any) -> Optional[Dict[str, str]]:
    """Keep only the provider's documented thinking mode values."""
    if not isinstance(value, dict):
        return None
    mode = str(value.get("type") or "").strip()
    return {"type": mode} if mode in {"enabled", "disabled"} else None


REVIEWER_NON_AUTHORITATIVE_INSTRUCTION = (
    "Reviewer constraint: model output is advisory only. Never claim that tests, "
    "builds, Docker runtime, health checks, or API acceptance passed unless the "
    "prompt contains matching deterministic execution evidence. Never manufacture "
    "commands, exit codes, logs, or pass evidence."
)


def chat_for_purpose(client, messages, *, purpose: str, **kwargs):
    """Use role routing while tolerating legacy test/provider adapters."""
    try:
        return client.chat(messages, purpose=purpose, **kwargs)
    except TypeError as exc:
        if "purpose" not in str(exc) or "unexpected keyword" not in str(exc):
            raise
        return client.chat(messages, **kwargs)


def chat_for_json(client, messages, *, purpose: Optional[str] = None, **kwargs):
    """Request strict JSON output with automatic downgrade.

    Injects ``response_format={"type": "json_object"}`` so the model emits a
    standalone JSON document instead of free text. If the backend rejects the
    parameter (400 / unsupported), ``_send_request`` strips it and retries once,
    so callers never hard-fail on provider capability differences.
    """
    kwargs.setdefault("response_format", {"type": "json_object"})
    try:
        if purpose is not None:
            return chat_for_purpose(client, messages, purpose=purpose, **kwargs)
        return client.chat(messages, **kwargs)
    except TypeError as exc:
        if (
            "response_format" not in str(exc)
            or "unexpected keyword" not in str(exc)
        ):
            raise
        # Legacy/local adapters may expose only ``chat(messages)``. Keep the
        # prompt-level JSON contract instead of failing before model I/O.
        kwargs.pop("response_format", None)
        if purpose is not None:
            return chat_for_purpose(client, messages, purpose=purpose, **kwargs)
        return client.chat(messages, **kwargs)



class MessageRole(Enum):
    """消息角色枚举"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class Message:
    """消息数据结构"""
    role: MessageRole
    content: str
    name: Optional[str] = None
    tool_calls: Optional[List[Dict]] = None
    tool_call_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        result = {"role": self.role.value, "content": self.content}
        if self.name:
            result["name"] = self.name
        if self.tool_calls:
            result["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            result["tool_call_id"] = self.tool_call_id
        return result

    @classmethod
    def from_dict(cls, data: Dict) -> "Message":
        return cls(
            role=MessageRole(data.get("role", "user")),
            content=data.get("content", ""),
            name=data.get("name"),
            tool_calls=data.get("tool_calls"),
            tool_call_id=data.get("tool_call_id"),
            metadata=data.get("metadata", {})
        )


@dataclass
class ToolCall:
    """工具调用数据结构"""
    id: str
    name: str
    arguments: Dict[str, Any]

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments)
            }
        }


@dataclass
class Tool:
    """工具定义"""
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema


class HermesClient:
    """
    Hermes API 客户端
    
    提供统一的 LLM 调用接口，支持：
    - 基础对话
    - 工具调用
    - 流式输出
    - 上下文管理
    - 用户级 API Key 隔离（通过 contextvars）
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 20480,
        temperature: float = 0.7,
        timeout: int = 120
    ):
        """
        初始化 Hermes 客户端
        
        Args:
            base_url: Hermes API 服务地址，默认从环境变量读取
            api_key: API 密钥，默认从环境变量读取（仅作为兜底，优先使用用户级配置）
            model: 模型名称
            max_tokens: 最大生成 token 数
            temperature: 温度参数
            timeout: 请求超时时间（秒）
        """
        self.base_url = base_url or os.getenv("HERMES_API_URL", "http://localhost:11434")
        self.api_key = api_key or os.getenv("HERMES_API_KEY", "")
        self.model = model or os.getenv("HERMES_MODEL", "hermes-3")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self._fallback_api_key = self.api_key  # 兜底 Key（环境变量）
        
        # 统计信息
        self.total_tokens = 0
        self.total_requests = 0

        # ── 本地缓存层（Reasonix 风格）────────────────────────────────────────
        # 结构：{cache_key: {"result": {...}, "expires_at": float, "hits": int}}
        self._cache: Dict[str, Dict] = {}
        self._cache_lock = threading.Lock()
        # 默认 TTL（秒）：普通对话 10 分钟，质检/分析类 30 分钟
        self.DEFAULT_TTL = 600
        self.ANALYSIS_TTL = 1800
        # 缓存统计
        self._cache_hits = 0
        self._cache_misses = 0
        # 最大缓存条目数（防止内存无限增长）
        self._max_cache_size = 500
        # 默认尊重系统代理设置（HTTPS_PROXY/HTTP_PROXY 环境变量）
        # 如需绕过代理，设置环境变量 HERMES_NO_PROXY=1
        self._http_session = _requests.Session()
        self._http_session.trust_env = not bool(int(os.environ.get("HERMES_NO_PROXY", "0")))

    # ── 用户级 API 配置解析 ──────────────────────────────────────────────────

    def _get_effective_config(self, purpose: Optional[str] = None) -> Dict[str, Any]:
        """
        获取当前请求的有效 API 配置。
        优先级：用户配置（contextvar）> 环境变量兜底 > 默认空值
        
        修复：确保用户配置和全局配置都能正确回退，避免空 API Key
        """
        user_cfg = current_user_api_config.get()
        if user_cfg and user_cfg.get("api_key"):
            effective = {
                "api_key": user_cfg.get("api_key", ""),
                "api_base": user_cfg.get("api_base", "") or self.base_url,
                "model": user_cfg.get("model", "") or self.model,
                "max_tokens": user_cfg.get("max_tokens", self.max_tokens),
                "temperature": user_cfg.get("temperature", self.temperature),
            }
            thinking = _normalize_thinking_config(user_cfg.get("thinking"))
            if thinking is not None:
                effective["thinking"] = thinking
            if purpose in _PURPOSE_TEMPERATURE_DEFAULTS:
                role = user_cfg.get(purpose)
                role = role if isinstance(role, dict) else {}
                effective["model"] = role.get("model") or effective["model"]
                role_temperature = role.get("temperature")
                effective["temperature"] = (
                    role_temperature
                    if role_temperature is not None
                    else _PURPOSE_TEMPERATURE_DEFAULTS[purpose]
                )
            return effective
        # 兜底：使用环境变量或初始化参数
        # 修复：确保 api_key 不为空，否则抛出明确错误
        fallback_key = self._fallback_api_key or ""
        if not fallback_key:
            # 如果全局配置也没有 API Key，抛出明确的错误信息
            raise ValueError(
                "API Key 未配置。请在以下任一位置配置：\n"
                "1. 用户个人设置（推荐）\n"
                "2. 全局默认配置（管理员）\n"
                "3. 环境变量 HERMES_API_KEY"
            )
        effective = {
            "api_key": fallback_key,
            "api_base": self.base_url,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if purpose in _PURPOSE_TEMPERATURE_DEFAULTS:
            effective["temperature"] = _PURPOSE_TEMPERATURE_DEFAULTS[purpose]
        return effective

    # ── 缓存工具方法 ──────────────────────────────────────────────────────────


    def _compute_exact_key(
        self, messages: List[Message], model: str, profile: str = "",
    ) -> str:
        """Layer 1 exact cache key: SHA256(model + messages JSON)"""
        data = json.dumps({
            "model": model,
            "profile": profile,
            "messages": [m.to_dict() for m in messages]
        }, sort_keys=True, ensure_ascii=False)
        return _CACHE_PREFIX_EXACT + hashlib.sha256(data.encode("utf-8")).hexdigest()

    def _compute_semantic_key(
        self, messages: List[Message], model: str = "", profile: str = "",
    ) -> str:
        """Layer 2: hash the complete system and user evidence.

        Code review prompts share the same requirements prefix while their
        source payload changes later in the message.  Prefix-only keys reused
        stale QC findings after repairs, so the complete evidence must
        participate in the cache key.
        """
        sys_content = ""
        user_contents = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                sys_content = msg.content
            elif msg.role == MessageRole.USER:
                user_contents.append(msg.content)
        sys_hash = hashlib.sha256(sys_content.encode("utf-8")).hexdigest()
        user_evidence = "\n".join(user_contents)
        user_hash = hashlib.sha256(user_evidence.encode("utf-8")).hexdigest()
        config_hash = hashlib.sha256(
            f"{model}\0{profile}".encode("utf-8")
        ).hexdigest()[:20]
        return _CACHE_PREFIX_SEMANTIC + f"{config_hash}:{sys_hash}:{user_hash}"

    def _compute_prompt_key(
        self, messages: List[Message], model: str = "", profile: str = "",
    ) -> str:
        """Layer 3: sys prefix 200 chars + exact user hash"""
        sys_content = ""
        user_contents = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                sys_content = msg.content
            elif msg.role == MessageRole.USER:
                user_contents.append(msg.content)
        prefix = sys_content[:200].rstrip()
        prefix_hash = hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:12]
        user_raw = "\n".join(user_contents)
        user_hash = hashlib.sha256(user_raw.encode("utf-8")).hexdigest()[:20]
        config_hash = hashlib.sha256(
            f"{model}\0{profile}".encode("utf-8")
        ).hexdigest()[:20]
        return _CACHE_PREFIX_PROMPT + f"{config_hash}:{prefix_hash}:{user_hash}"

    def _is_low_risk(self, messages: List[Message]) -> bool:
        """低风险类型可进入语义缓存白名单"""
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                return any(kw in msg.content for kw in _LOW_RISK_KEYWORDS)
        return False
    def _get_cache(self, key: str) -> Optional[Dict]:
        """从缓存中取结果，过期则删除"""
        with self._cache_lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            if time.time() > entry["expires_at"]:
                del self._cache[key]
                return None
            entry["hits"] += 1
            self._cache_hits += 1
            return entry["result"]

    def _set_cache(self, key: str, result: Dict, ttl: int) -> None:
        """写入缓存，超出最大条目数时淘汰最旧的 10%"""
        with self._cache_lock:
            if len(self._cache) >= self._max_cache_size:
                # 按过期时间排序，淘汰最旧的 50 条
                sorted_keys = sorted(self._cache.keys(),
                                     key=lambda k: self._cache[k]["expires_at"])
                for k in sorted_keys[:50]:
                    del self._cache[k]
            self._cache[key] = {
                "result": result,
                "expires_at": time.time() + ttl,
                "hits": 0,
                "created_at": time.time(),
            }

    def _is_cacheable(self, messages: List[Message], kwargs: Dict) -> Tuple[bool, int]:
        """
        判断本次请求是否可缓存，返回 (can_cache, ttl)。

        不缓存的场景：
        - 有 tool_calls（工具调用结果不确定）
        - temperature > 0.5（高随机性，结果不稳定）
        - 消息中包含时间戳/随机 ID（每次不同）

        延长 TTL 的场景（质检/分析类）：
        - system prompt 包含「质检」「代码审查」「分析」关键词
        """
        # 有工具调用不缓存
        if kwargs.get("tools"):
            return False, 0
        # 高温度不缓存（结果随机性太高）
        if kwargs.get("temperature", self.temperature) > 0.5:
            return False, 0
        # 检查是否是分析/质检类（延长 TTL）
        sys_content = ""
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                sys_content = msg.content
                break
        is_analysis = any(kw in sys_content for kw in _LOW_RISK_KEYWORDS)
        ttl = self.ANALYSIS_TTL if is_analysis else self.DEFAULT_TTL
        return True, ttl

    def invalidate_cache(self, prefix: str = "") -> int:
        """手动清除缓存（可按前缀清除语义缓存）"""
        with self._cache_lock:
            if not prefix:
                count = len(self._cache)
                self._cache.clear()
                return count
            keys_to_del = [k for k in self._cache if k.startswith(prefix)]
            for k in keys_to_del:
                del self._cache[k]
            return len(keys_to_del)

    def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计（供 /stats 接口暴露）"""
        with self._cache_lock:
            total = self._cache_hits + self._cache_misses
            hit_rate = round(self._cache_hits / total * 100, 1) if total > 0 else 0.0
            now = time.time()
            active = sum(1 for e in self._cache.values() if e["expires_at"] > now)
            return {
                "hits": self._cache_hits,
                "misses": self._cache_misses,
                "hit_rate_pct": hit_rate,
                "total_entries": len(self._cache),
                "active_entries": active,
                "max_size": self._max_cache_size,
            }

    def chat(
        self,
        messages: List[Message],
        tools: Optional[List[Tool]] = None,
        tool_choice: Optional[str] = None,
        use_cache: bool = True,
        purpose: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        发送聊天请求（含三层缓存 + 用户级 API Key 隔离）。

        缓存策略（Reasonix 风格）：
        1. 精确缓存：完全相同的请求直接返回（命中率最高）
        2. 语义缓存：system prompt 相同 + user 内容前 120 字相同时复用
            （适合质检同一段代码的多次调用）
        3. Prompt Cache 友好：system prompt 固定前置，内容稳定，
            利用 OpenAI/Claude API 侧的 prefix cache 减少 input token 计费

        API Key 隔离：
        优先使用 contextvars 中当前用户的 API 配置，
        实现多用户独立 Key 互不干扰。

        Args:
            messages: 消息列表（system prompt 应放在最前面）
            tools: 可用工具列表
            tool_choice: 工具选择策略
            use_cache: 是否使用本地缓存（默认 True）
            **kwargs: 其他参数
        """
        # ── 获取当前用户的有效 API 配置 ──────────────────────────────────
        request_timeout = kwargs.pop("request_timeout", None)
        if purpose not in (None, "generator", "reviewer"):
            raise ValueError(f"unsupported LLM purpose: {purpose}")
        effective_cfg = self._get_effective_config(purpose)
        effective_api_key = effective_cfg["api_key"]
        effective_model = effective_cfg["model"]
        effective_base_url = effective_cfg["api_base"].rstrip("/")

        if purpose == "reviewer":
            messages = [
                Message(
                    role=MessageRole.SYSTEM,
                    content=REVIEWER_NON_AUTHORITATIVE_INSTRUCTION,
                ),
                *messages,
            ]

        # ── Prompt Cache 友好：确保 system prompt 在最前面 ──────────────────
        # OpenAI/Claude 的 prefix cache 要求 system prompt 位置固定且内容稳定
        # 如果调用方没有把 system 放第一位，这里自动重排
        if messages and messages[0].role != MessageRole.SYSTEM:
            sys_msgs = [m for m in messages if m.role == MessageRole.SYSTEM]
            other_msgs = [m for m in messages if m.role != MessageRole.SYSTEM]
            if sys_msgs:
                messages = sys_msgs + other_msgs

        # ── 判断是否可缓存 ────────────────────────────────────────────────────
        can_cache, ttl = self._is_cacheable(
            messages,
            {"tools": tools, "temperature": effective_cfg["temperature"], **kwargs},
        )
        can_cache = can_cache and use_cache
        # 缓存 key 加入用户标识以避免跨用户缓存混淆
        cache_model = effective_model
        thinking_profile = json.dumps(
            effective_cfg.get("thinking") or {}, sort_keys=True, separators=(",", ":")
        )
        cache_profile = f"{purpose or 'default'}:{effective_base_url}:{thinking_profile}"
        # response_format 决定输出模式（JSON vs 自由文本），必须进缓存 key，
        # 否则普通文本调用与 JSON 调用会互相污染缓存。
        if "response_format" in kwargs:
            cache_profile += ":json_mode"

        # Layer 1: 精确缓存
        if can_cache:
            exact_key = self._compute_exact_key(messages, cache_model, cache_profile)
            cached = self._get_cache(exact_key)
            if cached:
                return {**cached, "cache_hit": "exact"}

            # Layer 2: 语义缓存白名单（仅低风险类型）
            if self._is_low_risk(messages):
                sem_key = self._compute_semantic_key(
                    messages, cache_model, cache_profile,
                )
                cached = self._get_cache(sem_key)
                if cached:
                    return {**cached, "cache_hit": "semantic"}

            # Layer 3: Prompt Cache（system prefix 匹配）
            prompt_key = self._compute_prompt_key(
                messages, cache_model, cache_profile,
            )
            cached = self._get_cache(prompt_key)
            if cached:
                return {**cached, "cache_hit": "prompt"}

        self._cache_misses += 1

        # 构建请求
        payload = {
            "model": effective_model,
            "messages": [msg.to_dict() for msg in messages],
            "max_tokens": effective_cfg["max_tokens"],
            "temperature": effective_cfg["temperature"],
            **kwargs
        }
        if effective_cfg.get("thinking"):
            payload["thinking"] = effective_cfg["thinking"]

        if tools:
            payload["tools"] = [self._tool_to_dict(t) for t in tools]
        if tool_choice:
            payload["tool_choice"] = tool_choice

        request_args = (payload, effective_api_key, effective_base_url)
        if request_timeout is None:
            response = self._send_request(*request_args)
        else:
            response = self._send_request(
                *request_args,
                timeout=request_timeout,
            )
        self.total_requests += 1

        # 写入三层缓存（只缓存成功响应，不缓存错误）
        if (
            can_cache
            and not response.get("error")
            and not response.get("truncated")
            and response.get("content")
        ):
            # Layer 1: 精确缓存
            self._set_cache(exact_key, response, ttl)
            # Layer 2: 语义缓存（仅低风险类型）
            if self._is_low_risk(messages):
                self._set_cache(sem_key, response, ttl)
            # Layer 3: Prompt Cache
            self._set_cache(prompt_key, response, ttl)

        return response

    def chat_stream(
        self,
        messages: List[Message],
        tools: Optional[List[Tool]] = None,
        callback: Optional[Callable[[str], None]] = None,
        purpose: Optional[str] = None,
    ) -> str:
        """
        流式聊天请求
        
        Args:
            messages: 消息列表
            tools: 可用工具列表
            callback: 每个 token 的回调函数
            
        Returns:
            完整的响应内容
        """
        effective_cfg = self._get_effective_config(purpose)
        effective_model = effective_cfg["model"]
        effective_base_url = effective_cfg["api_base"].rstrip("/")
        effective_api_key = effective_cfg["api_key"]

        if purpose == "reviewer":
            messages = [
                Message(role=MessageRole.SYSTEM, content=REVIEWER_NON_AUTHORITATIVE_INSTRUCTION),
                *messages,
            ]

        # 构建请求
        payload = {
            "model": effective_model,
            "messages": [msg.to_dict() for msg in messages],
            "max_tokens": effective_cfg["max_tokens"],
            "temperature": effective_cfg["temperature"],
            "stream": True
        }
        if effective_cfg.get("thinking"):
            payload["thinking"] = effective_cfg["thinking"]
        
        if tools:
            payload["tools"] = [self._tool_to_dict(t) for t in tools]

        # 流式请求
        return self._send_stream_request(payload, effective_api_key, effective_base_url, callback)

    def _tool_to_dict(self, tool: Tool) -> Dict:
        """将 Tool 对象转换为 API 格式"""
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters
            }
        }

    def update_config(self, cfg: Dict) -> None:
        """动态更新客户端配置（用于注入默认/独立 API 配置）"""
        if cfg.get("model"):
            self.model = cfg["model"]
        if cfg.get("api_base"):
            self.base_url = cfg["api_base"].rstrip("/")
        if cfg.get("api_key"):
            self.api_key = cfg["api_key"]
            self._fallback_api_key = cfg["api_key"]
        if cfg.get("max_tokens"):
            self.max_tokens = cfg["max_tokens"]
        if cfg.get("temperature") is not None:
            self.temperature = cfg["temperature"]

    def _post_once(
        self,
        url: str,
        payload: Dict,
        headers: Dict,
        timeout_tuple: Any,
    ) -> Dict:
        """Single HTTP POST to the OpenAI-compatible endpoint.

        Returns the parsed JSON body. Raises on non-2xx so the caller can
        decide whether to retry (e.g. response_format downgrade).
        """
        if _HAS_REQUESTS:
            resp = self._http_session.post(
                url, json=payload, headers=headers, timeout=timeout_tuple
            )
            resp.raise_for_status()
            return resp.json()
        elif _HAS_HTTPX:
            import httpx as _httpx
            _timeout = _httpx.Timeout(timeout_tuple[1], connect=timeout_tuple[0])
            with _httpx.Client(timeout=_timeout) as client:
                resp = client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                return resp.json()
        else:
            import urllib.request
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers=headers,
                method="POST",
            )
            # URL scheme and hostname are validated above.
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # nosec B310
                return json.loads(r.read())

    def _send_request(
        self,
        payload: Dict,
        api_key: str,
        base_url: str,
        timeout: Optional[int] = None,
    ) -> Dict:
        """
        发送 HTTP 请求（OpenAI 兼容接口）

        支持 OpenAI / DeepSeek / Kimi / Qwen / Claude(via proxy) / Gemini(via proxy) 等
        任何兼容 /v1/chat/completions 的接口均可使用。

        修复：API Key 为空时抛出明确错误（在 _get_effective_config 中已处理）
        若 payload 含 response_format 且后端不支持（400），自动降级去掉该参数重试一次，
        避免因后端能力差异导致关键调用 400 失败。
        """
        # 理论上不会到这里（_get_effective_config 已检查），但保留双重校验
        if not api_key:
            raise ValueError(
                "API Key 未配置。请在以下任一位置配置：\n"
                "1. 用户个人设置（推荐）\n"
                "2. 全局默认配置（管理员）\n"
                "3. 环境变量 HERMES_API_KEY"
            )

        # 构建请求 URL（兼容 /v1/chat/completions）
        base = base_url.rstrip("/")
        if not base.endswith("/chat/completions"):
            url = f"{base}/chat/completions"
        else:
            url = base
        from urllib.parse import urlparse
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            raise ValueError("API Base URL must use http:// or https://")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        # Claude 需要额外的 anthropic-version header（通过代理时不需要）
        # Gemini 通过 OpenAI 兼容层时不需要特殊处理

        # timeout 元组：(连接超时, 读取超时)
        # requests 传单个整数时只限制连接超时，读取超时无限 → 会卡住
        # 传元组才能同时限制两者
        read_timeout = self.timeout if timeout is None else max(1, int(timeout))
        connect_timeout = min(10, read_timeout)          # 连接最多 10s
        timeout_tuple = (connect_timeout, read_timeout)

        try:
            try:
                data = self._post_once(url, payload, headers, timeout_tuple)
            except Exception as first_exc:
                # 降级：若请求带 response_format 且后端不支持，去掉后重试一次。
                if "response_format" in payload and self._is_unsupported_param(first_exc, "response_format"):
                    import logging
                    logging.getLogger(__name__).warning(
                        "[HermesClient] response_format 不被后端支持，降级重试：url=%s model=%s",
                        url, payload.get("model"),
                    )
                    downgraded = {k: v for k, v in payload.items() if k != "response_format"}
                    data = self._post_once(url, downgraded, headers, timeout_tuple)
                else:
                    raise first_exc

            # 解析 OpenAI 格式响应
            choice = data.get("choices", [{}])[0]
            msg = choice.get("message", {})
            content = msg.get("content") or msg.get("reasoning_content") or ""
            tool_calls = msg.get("tool_calls")
            usage = data.get("usage", {})
            finish_reason = str(choice.get("finish_reason") or "")

            self.total_tokens += usage.get("total_tokens", 0)

            return {
                "content": content,
                "tool_calls": tool_calls,
                "usage": usage,
                "finish_reason": finish_reason,
                "truncated": finish_reason.lower() in {"length", "max_tokens"},
            }

        except Exception as e:
            error_msg = str(e)

            # 记录详细错误日志（包含完整堆栈）
            import logging
            logger = logging.getLogger(__name__)
            logger.error(
                "[HermesClient API调用失败] url=%s model=%s error=%s",
                url, payload.get("model"), e, exc_info=True
            )

            # 常见错误友好提示
            if "401" in error_msg or "Unauthorized" in error_msg:
                hint = "API Key 无效或已过期，请在「系统设置」中重新配置。"
            elif "404" in error_msg:
                hint = f"API Endpoint 不存在，请检查 Base URL 是否正确（当前：{base_url}）。"
            elif "Connection" in error_msg or "connect" in error_msg.lower():
                hint = f"无法连接到 API 服务（{base_url}），请检查网络或 Endpoint 地址。"
            elif "429" in error_msg:
                hint = "请求频率超限（Rate Limit），请稍后重试。"
            else:
                hint = f"API 调用失败：{error_msg}"

            return {
                "content": f"❌ {hint}",
                "tool_calls": None,
                "usage": {},
                "error": error_msg,
            }

    @staticmethod
    def _is_unsupported_param(exc: Exception, param: str) -> bool:
        """Heuristic: did the backend reject the request because of ``param``?

        Any 400 while the payload carries ``response_format`` is treated as a
        candidate for downgrade: providers reject it for varied reasons
        (unsupported parameter, prompt must contain the word "json", schema
        mismatch, …). Stripping it and retrying always leaves the robust
        ``extract_first_json`` parser as a safety net.
        """
        text = str(exc).lower()
        if "400" in text or "bad request" in text:
            return True
        markers = (
            param,
            "response_format",
            "unrecognized",
            "unknown argument",
            "unexpected",
            "is not supported",
            "not support",
        )
        return any(m in text for m in markers)


    def _send_stream_request(
        self,
        payload: Dict,
        api_key: str,
        base_url: str,
        callback: Optional[Callable[[str], None]]
    ) -> str:
        """流式请求（复用非流式实现，后续可扩展为真正的 SSE）"""
        payload_copy = {k: v for k, v in payload.items() if k != "stream"}
        result = self._send_request(payload_copy, api_key, base_url)
        content = result.get("content", "")
        if callback:
            for char in content:
                callback(char)
        return content

    def create_subagent_context(self) -> List[Message]:
        """
        创建子代理的空白上下文
        
        子代理初始化时使用空消息列表，确保上下文干净
        """
        return []

    def truncate_context(
        self,
        messages: List[Message],
        max_messages: int = 50
    ) -> List[Message]:
        """
        截断上下文，保留最近的 N 条消息
        
        用于防止上下文过长导致性能问题
        """
        if len(messages) <= max_messages:
            return messages
        
        # 保留系统消息和最近的 max_messages 条
        system_msgs = [m for m in messages if m.role == MessageRole.SYSTEM]
        other_msgs = [m for m in messages if m.role != MessageRole.SYSTEM]
        
        return system_msgs + other_msgs[-max_messages:]

    def get_stats(self) -> Dict[str, Any]:
        """获取使用统计"""
        return {
            "total_requests": self.total_requests,
            "total_tokens": self.total_tokens,
            "model": self.model
        }


class SubAgentContext:
    """
    子代理上下文管理器
    
    确保子代理具有：
    - 空白上下文（messages = []）
    - 无 task 工具（禁止递归）
    - 30 轮上限
    - 只返回纯文本摘要
    """

    def __init__(
        self,
        hermes_client: HermesClient,
        max_rounds: int = 30,
        task_id: Optional[str] = None
    ):
        self.hermes = hermes_client
        self.max_rounds = max_rounds
        self.task_id = task_id
        
        # 空白上下文
        self.messages: List[Message] = []
        
        # 轮次计数
        self.round_count = 0
        
        # 执行历史（用于生成摘要）
        self.execution_log: List[Dict] = []

    def add_message(self, role: MessageRole, content: str) -> None:
        """添加消息到上下文"""
        self.messages.append(Message(role=role, content=content))

    def execute_round(self, user_content: str) -> str:
        """
        执行一轮对话
        
        Returns:
            模型响应内容
        """
        if self.round_count >= self.max_rounds:
            raise RuntimeError(f"超出最大轮次限制 {self.max_rounds}")

        self.add_message(MessageRole.USER, user_content)
        response = self.hermes.chat(self.messages)
        
        content = response.get("content", "")
        self.add_message(MessageRole.ASSISTANT, content)
        self.round_count += 1
        
        return content

    def log_action(self, action: str, result: str) -> None:
        """记录执行动作"""
        self.execution_log.append({
            "round": self.round_count,
            "action": action,
            "result": result,
            "timestamp": time.time()
        })

    def generate_summary(self) -> str:
        """
        生成执行摘要
        
        子代理完成任务后，只返回纯文本摘要给父代理
        """
        if not self.execution_log:
            return "任务完成，无执行记录"
        
        summary_parts = []
        summary_parts.append(f"任务ID: {self.task_id or 'unknown'}")
        summary_parts.append(f"执行轮次: {self.round_count}/{self.max_rounds}")
        summary_parts.append("")
        summary_parts.append("执行摘要:")
        
        for log in self.execution_log[-5:]:  # 只保留最后5条
            summary_parts.append(f"  - {log['action']}: {log['result']}")
        
        return "\n".join(summary_parts)

    def is_expired(self) -> bool:
        """检查是否已超出轮次限制"""
        return self.round_count >= self.max_rounds
