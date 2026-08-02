"""
认证与授权模块 — JWT 令牌管理 + 密码哈希 + 用户管理 + 审计日志 + 防暴力破解
+ 邮箱验证 + 密码重置 + IP 提取 + 限流

使用 hashlib pbkdf2_hmac 哈希密码，PyJWT 签发/验证令牌。
用户数据存入数据库 kv_store（key 前缀："user:"），与现有持久化体系一致。

环境变量：
    JWT_SECRET          JWT 签名密钥（生产环境必须手动设置；未设置则从 DB 恢复或生成并持久化）
    JWT_EXPIRY_HOURS    Token 过期时间，默认 24 小时
    ADMIN_USERNAME      初始管理员用户名，默认 "admin"
    ADMIN_PASSWORD      初始管理员密码（不设置则随机生成并通过 stdout 输出，不会写入日志）
    SMTP_HOST           邮件 SMTP 服务器
    SMTP_PORT           邮件 SMTP 端口，默认 587
    SMTP_USERNAME       邮件 SMTP 用户名
    SMTP_PASSWORD       邮件 SMTP 密码
    SMTP_FROM           邮件发件人地址
    SMTP_USE_TLS        是否启用 TLS，默认 true
    TRUSTED_PROXY_IPS   信任的反代 IP 列表（逗号分隔，支持 CIDR 网段），用于 X-Forwarded-For 解析
"""

import hashlib
import hmac as hmac_module
import ipaddress
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from core import database as _database
from core.database import (
    init_db, kv_set, kv_get, kv_delete, kv_keys_prefix, kv_transaction,
)
from core.security_audit import record_audit_event, redact_text, safe_audit_log_line

# ── 审计日志 ────────────────────────────────────────────────────────────────────
audit_logger = logging.getLogger("audit")

USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 64
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
USERNAME_RULE_MESSAGE = (
    "用户名须为3-64位，以字母或数字开头，且只能包含字母、数字、点、下划线和连字符"
)


def validate_username(username: str) -> str:
    """Validate and normalize the username domain invariant."""
    normalized = str(username or "").strip()
    if (
        not USERNAME_MIN_LENGTH <= len(normalized) <= USERNAME_MAX_LENGTH
        or not USERNAME_PATTERN.fullmatch(normalized)
    ):
        raise ValueError(USERNAME_RULE_MESSAGE)
    return normalized


def audit_log(action: str, username: str = "", ip: str = "", detail: str = ""):
    """记录安全审计日志"""
    normalized_action = str(action or "").strip().upper()
    outcome = (
        "denied" if any(marker in normalized_action for marker in ("BLOCKED", "DENIED", "LOCKED"))
        else "failure" if "FAILED" in normalized_action
        else "success"
    )
    try:
        event = record_audit_event(
            normalized_action,
            username or "anonymous",
            outcome=outcome,
            source_ip=ip if ip and ip != "testclient" else "unknown",
            resource_type="authentication",
            details={"detail": detail},
        )
        audit_logger.info("%s", safe_audit_log_line(event))
    except Exception as exc:
        # Authentication must not become unavailable because the audit store is
        # temporarily unhealthy. The fallback line is still secret-redacted.
        audit_logger.error(
            "AUDIT_PERSISTENCE_FAILED action=%s actor=%s error=%s",
            redact_text(normalized_action),
            redact_text(username or "anonymous"),
            redact_text(exc),
        )

# ── 配置 ────────────────────────────────────────────────────────────────────────
JWT_SECRET = os.environ.get("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = int(os.environ.get("JWT_EXPIRY_HOURS", "24"))

# JWT Secret 持久化 key
_JWT_SECRET_DB_KEY = "auth:jwt_secret"


def _is_production_environment() -> bool:
    """Return whether startup must use explicitly provisioned secrets."""
    if os.environ.get("REQUIRE_DATABASE_URL", "").strip().lower() in {
        "1", "true", "yes",
    }:
        return True
    environment = (
        os.environ.get("METIS_ENV")
        or os.environ.get("APP_ENV")
        or os.environ.get("ENVIRONMENT")
        or ""
    )
    return environment.strip().lower() in {"prod", "production"}


def _resolve_jwt_secret() -> str:
    """Resolve the signing secret and fail closed in production."""
    global JWT_SECRET
    configured_secret = os.environ.get("JWT_SECRET", "") or JWT_SECRET
    if configured_secret:
        if (
            _is_production_environment()
            and len(configured_secret.encode("utf-8")) < 32
        ):
            raise RuntimeError("JWT_SECRET must contain at least 32 bytes in production")
        JWT_SECRET = configured_secret
        return JWT_SECRET
    if _is_production_environment():
        raise RuntimeError("JWT_SECRET must be explicitly configured in production")
    # auth is imported by API modules outside the main application entrypoint
    # (for example, during pytest collection). Ensure its import-time database
    # access is safe even when main.py has not initialized the schema yet.
    init_db()
    # 从数据库恢复
    saved = kv_get(_JWT_SECRET_DB_KEY, None)
    if saved and saved.get("secret"):
        JWT_SECRET = saved["secret"]
        logging.getLogger("auth").info("JWT_SECRET 已从数据库恢复")
        return JWT_SECRET
    # 自动生成并持久化
    JWT_SECRET = hashlib.sha256(os.urandom(64)).hexdigest()
    kv_set(_JWT_SECRET_DB_KEY, {"secret": JWT_SECRET, "created_at": time.time()})
    logging.getLogger("auth").info("JWT_SECRET 已自动生成并持久化到数据库（服务重启后不丢失）")
    return JWT_SECRET

_resolve_jwt_secret()

# ── 密码工具 ────────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """PBKDF2-SHA256 哈希密码（100k iterations）"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return salt.hex() + ":" + dk.hex()


def verify_password(password: str, hashed: str) -> bool:
    """验证密码是否匹配（常量时间比较，防时序攻击）"""
    try:
        salt_hex, dk_hex = hashed.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        dk = bytes.fromhex(dk_hex)
        new_dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
        return hmac_module.compare_digest(new_dk, dk)
    except Exception:
        return False


def _hash_code(code: str) -> str:
    """SHA256 哈希验证码/令牌（常量时间比较用）"""
    return hashlib.sha256(code.encode()).hexdigest()


# ── 邮箱归一化 ──────────────────────────────────────────────────────────────

def _normalize_email(email: str) -> str:
    """
    邮箱归一化：去首尾空白 + 转小写。
    RFC 5321 规定邮箱本地部分理论区分大小写，但几乎所有主流邮件提供商不区分。
    索引和查询均以小写形式处理，用户主记录保留原始大小写（发邮件用）。
    """
    return email.strip().lower()


# ── JWT ─────────────────────────────────────────────────────────────────────────

def create_token(user_id: str, username: str, role: str = "user",
                 token_version: int = 0) -> str:
    """签发 JWT 访问令牌"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "tv": token_version,  # token 版本号，重置密码后递增使旧 token 失效
        "iat": now,
        "exp": now + timedelta(hours=JWT_EXPIRY_HOURS),
        "type": "access",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_token_payload(token: str) -> Dict[str, Any]:
    """Verify only JWT cryptography and claims."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            raise jwt.InvalidTokenError()
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌无效")


def decode_token(token: str) -> Dict[str, Any]:
    """Verify a token against the current persisted user security state."""
    payload, user = authenticate_token(token)
    payload["_user"] = user
    return payload


# ── 用户模型 ────────────────────────────────────────────────────────────────────

USER_PREFIX = "user:"
MCP_TOKEN_PREFIX = "mcp_token:"
MCP_TOKEN_NAME_MAX_LENGTH = 64

class UserModel:
    def __init__(self, user_id: str, username: str, password_hash: str,
                 role: str = "user", created_at: Optional[float] = None,
                 failed_attempts: int = 0, locked_until: float = 0,
                 email: str = "", email_verified: bool = False,
                 token_version: int = 0):
        self.user_id = user_id
        self.username = username
        self.password_hash = password_hash
        self.role = role
        self.created_at = created_at or time.time()
        self.failed_attempts = failed_attempts
        self.locked_until = locked_until
        self.email = email          # 原始大小写，发邮件展示用
        self.email_verified = email_verified
        self.token_version = token_version

    def is_locked(self) -> bool:
        """检查账户是否被锁定"""
        if self.locked_until and time.time() < self.locked_until:
            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id, "username": self.username,
            "password_hash": self.password_hash, "role": self.role,
            "created_at": self.created_at,
            "failed_attempts": self.failed_attempts,
            "locked_until": self.locked_until,
            "email": self.email,
            "email_verified": self.email_verified,
            "token_version": self.token_version,
        }

    def to_safe_dict(self) -> Dict[str, Any]:
        """返回不含密码哈希的安全字段"""
        return {
            "user_id": self.user_id, "username": self.username,
            "role": self.role, "created_at": self.created_at,
            "email": self.email,
            "email_verified": self.email_verified,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserModel":
        return cls(
            data["user_id"], data["username"], data["password_hash"],
            data.get("role", "user"), data.get("created_at"),
            data.get("failed_attempts", 0), data.get("locked_until", 0),
            data.get("email", ""), data.get("email_verified", False),
            data.get("token_version", 0),
        )


def get_user_by_username(username: str) -> Optional[UserModel]:
    idx = kv_get(f"{USER_PREFIX}index:username:{username}", None)
    if idx and idx.get("user_id"):
        data = kv_get(f"{USER_PREFIX}{idx['user_id']}")
        if data:
            return UserModel.from_dict(data)
    return None


def get_user_by_email(email: str) -> Optional[UserModel]:
    """通过邮箱查找用户（索引查询，邮箱已归一化为小写）"""
    if not email:
        return None
    normalized = _normalize_email(email)
    idx = kv_get(f"{USER_PREFIX}index:email:{normalized}", None)
    if idx and idx.get("user_id"):
        data = kv_get(f"{USER_PREFIX}{idx['user_id']}")
        if data:
            return UserModel.from_dict(data)
    return None


def get_user_by_id(user_id: str) -> Optional[UserModel]:
    data = kv_get(f"{USER_PREFIX}{user_id}")
    return UserModel.from_dict(data) if data else None


def authenticate_token(token: str) -> Tuple[Dict[str, Any], UserModel]:
    """Single authentication boundary shared by HTTP middleware and WebSocket."""
    payload = _decode_token_payload(token)
    user_id = payload.get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌格式无效",
        )
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在",
        )
    token_version = payload.get("tv")
    if (
        isinstance(token_version, bool)
        or not isinstance(token_version, int)
        or token_version != user.token_version
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌已失效，请重新登录",
        )
    return payload, user


def create_mcp_token(user: UserModel) -> str:
    """Create a revocable MCP-only personal token; persist only its hash."""
    token = f"metis_mcp_{secrets.token_urlsafe(32)}"
    token_hash = _hash_code(token)
    old_hash = kv_get(f"{MCP_TOKEN_PREFIX}user:{user.user_id}", {}).get("token_hash")
    if old_hash:
        kv_delete(f"{MCP_TOKEN_PREFIX}hash:{old_hash}")
    kv_set(f"{MCP_TOKEN_PREFIX}hash:{token_hash}", {"user_id": user.user_id})
    kv_set(f"{MCP_TOKEN_PREFIX}user:{user.user_id}", {
        "token_hash": token_hash,
        "created_at": time.time(),
    })
    return token


def revoke_mcp_token(user_id: str) -> bool:
    record = kv_get(f"{MCP_TOKEN_PREFIX}user:{user_id}", {})
    token_hash = record.get("token_hash")
    if not token_hash:
        return False
    kv_delete(f"{MCP_TOKEN_PREFIX}hash:{token_hash}")
    kv_delete(f"{MCP_TOKEN_PREFIX}user:{user_id}")
    return True


def get_mcp_token_status(user_id: str) -> Dict[str, Any]:
    record = kv_get(f"{MCP_TOKEN_PREFIX}user:{user_id}", {})
    return {"configured": bool(record.get("token_hash")), "created_at": record.get("created_at")}


def authenticate_mcp_token(token: str) -> UserModel:
    if not isinstance(token, str) or not token.startswith("metis_mcp_"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="MCP token invalid")
    record = kv_get(f"{MCP_TOKEN_PREFIX}hash:{_hash_code(token)}", {})
    user = get_user_by_id(record.get("user_id", ""))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="MCP token invalid")
    return user


def _mcp_token_item_key(token_id: str) -> str:
    return f"{MCP_TOKEN_PREFIX}item:{token_id}"


def _mcp_token_user_key(user_id: str) -> str:
    return f"{MCP_TOKEN_PREFIX}items:user:{user_id}"


def _validate_mcp_token_name(name: str) -> str:
    normalized = str(name or "").strip()
    if not normalized:
        raise ValueError("Token 名称不能为空")
    if len(normalized) > MCP_TOKEN_NAME_MAX_LENGTH:
        raise ValueError(f"Token 名称不能超过 {MCP_TOKEN_NAME_MAX_LENGTH} 个字符")
    return normalized


def create_named_mcp_token(user: UserModel, name: str) -> Dict[str, Any]:
    """Create an independently revocable named MCP token with encrypted reveal data."""
    from core.secret_storage import protect_config

    name = _validate_mcp_token_name(name)
    token_id = secrets.token_hex(8)
    token = f"metis_mcp_{secrets.token_urlsafe(32)}"
    token_hash = _hash_code(token)
    created_at = time.time()
    item = {
        "token_id": token_id,
        "user_id": user.user_id,
        "name": name,
        "token_hash": token_hash,
        "last4": token[-4:],
        "created_at": created_at,
        "encrypted_token": protect_config(
            {"token": token}, context=f"MCP token {token_id}"
        ),
    }
    ids = list(kv_get(_mcp_token_user_key(user.user_id), {}).get("token_ids") or [])
    ids.append(token_id)
    with kv_transaction(immediate=True):
        kv_set(_mcp_token_item_key(token_id), item)
        kv_set(_mcp_token_user_key(user.user_id), {"token_ids": ids})
        kv_set(f"{MCP_TOKEN_PREFIX}hash:{token_hash}", {
            "user_id": user.user_id,
            "token_id": token_id,
        })
    return {
        "token_id": token_id,
        "name": name,
        "token": token,
        "last4": item["last4"],
        "created_at": created_at,
    }


def list_mcp_tokens(user_id: str) -> list[Dict[str, Any]]:
    ids = list(kv_get(_mcp_token_user_key(user_id), {}).get("token_ids") or [])
    result = []
    for token_id in ids:
        item = kv_get(_mcp_token_item_key(str(token_id)), {})
        if item.get("user_id") != user_id:
            continue
        result.append({
            "token_id": item["token_id"],
            "name": item["name"],
            "masked_token": f"metis_mcp_••••{item['last4']}",
            "created_at": item["created_at"],
        })
    return sorted(result, key=lambda value: value["created_at"], reverse=True)


def reveal_mcp_token(user: UserModel, token_id: str, password: str) -> str:
    from core.secret_storage import restore_config

    if not verify_password(str(password or ""), user.password_hash):
        raise ValueError("密码错误")
    item = kv_get(_mcp_token_item_key(token_id), {})
    if item.get("user_id") != user.user_id:
        raise KeyError("Token 不存在")
    restored = restore_config(
        item.get("encrypted_token"), context=f"MCP token {token_id}"
    ).value
    token = restored.get("token") if isinstance(restored, dict) else ""
    if not token or _hash_code(token) != item.get("token_hash"):
        raise ValueError("Token 数据无效")
    return token


def revoke_named_mcp_token(user_id: str, token_id: str) -> bool:
    item = kv_get(_mcp_token_item_key(token_id), {})
    if item.get("user_id") != user_id:
        return False
    ids = list(kv_get(_mcp_token_user_key(user_id), {}).get("token_ids") or [])
    with kv_transaction(immediate=True):
        kv_delete(f"{MCP_TOKEN_PREFIX}hash:{item.get('token_hash', '')}")
        kv_delete(_mcp_token_item_key(token_id))
        kv_set(_mcp_token_user_key(user_id), {
            "token_ids": [value for value in ids if value != token_id]
        })
    return True


def _save_user(user: UserModel):
    """保存用户数据到数据库"""
    kv_set(f"{USER_PREFIX}{user.user_id}", user.to_dict())


def create_user(username: str, password: str, role: str = "user",
                email: str = "", email_verified: bool = False) -> UserModel:
    """
    创建新用户。
    写入顺序：先主记录 → 再索引。索引写入失败则回滚主记录。
    """
    import uuid

    username = validate_username(username)
    normalized_email = _normalize_email(email) if email else ""

    # 1. 冲突检查
    if get_user_by_username(username):
        raise ValueError(f"用户名 '{username}' 已存在")
    if normalized_email and get_user_by_email(normalized_email):
        raise ValueError("该邮箱已注册")

    # 2. 先写用户主记录（source of truth）
    user_id = uuid.uuid4().hex[:12]
    user = UserModel(
        user_id=user_id, username=username,
        password_hash=hash_password(password), role=role,
        email=email, email_verified=email_verified,
    )
    # Persisted atomically with both uniqueness claims below.

    # 3. 再写索引（失败则回滚主记录，防止"用户存在但索引查不到"）
    try:
        with kv_transaction(immediate=True):
            if _database.kv_get(f"{USER_PREFIX}index:username:{username}"):
                raise ValueError(f"username '{username}' already exists")
            if normalized_email and _database.kv_get(
                f"{USER_PREFIX}index:email:{normalized_email}"
            ):
                raise ValueError("email already registered")
            kv_set(f"{USER_PREFIX}{user_id}", user.to_dict())
            kv_set(f"{USER_PREFIX}index:username:{username}", {"user_id": user_id})
            if normalized_email:
                kv_set(f"{USER_PREFIX}index:email:{normalized_email}", {"user_id": user_id})
    except ValueError:
        raise
    except Exception:
        # The database transaction has already rolled back every write.
        raise RuntimeError("用户创建失败，请重试")

    logging.getLogger("auth").info("用户创建: %s (role=%s, email=%s)", username, role, email)
    audit_log("USER_CREATED", username)
    return user


def list_users() -> list:
    """列出所有用户（不含密码哈希）"""
    keys = kv_keys_prefix(USER_PREFIX)
    users = []
    for k in keys:
        if k.startswith(f"{USER_PREFIX}index:"):
            continue
        data = kv_get(k)
        if data:
            users.append(UserModel.from_dict(data).to_safe_dict())
    return users


def delete_user(user_id: str) -> bool:
    user = get_user_by_id(user_id)
    if not user:
        return False
    kv_delete(f"{USER_PREFIX}index:username:{user.username}")
    if user.email:
        kv_delete(f"{USER_PREFIX}index:email:{_normalize_email(user.email)}")
    kv_delete(f"{USER_PREFIX}{user_id}")
    logging.getLogger("auth").info("用户已删除: %s", user.username)
    audit_log("USER_DELETED", user.username)
    return True


# ── 索引修复（仅启动时调用一次）───────────────────────────────────────────────

def repair_user_indexes():
    """
    启动时调用：扫描所有用户主记录，重建缺失的索引。
    仅在应用启动时调用一次，不需要定时重复执行。
    原因：create_user 中已有"索引写入失败则回滚主记录"的兜底逻辑，
    正常运行过程中不会产生索引不一致状态。此函数仅处理历史遗留数据
    或异常崩溃后的修复。
    """
    keys = kv_keys_prefix(USER_PREFIX)
    repaired = 0
    for key in keys:
        if key.startswith(f"{USER_PREFIX}index:"):
            continue
        data = kv_get(key)
        if not data:
            continue
        username = data.get("username", "")
        email = data.get("email", "")
        user_id = data.get("user_id", "")
        if not user_id:
            continue
        # 按需修复用户名索引
        if username and not kv_get(f"{USER_PREFIX}index:username:{username}", None):
            kv_set(f"{USER_PREFIX}index:username:{username}", {"user_id": user_id})
            repaired += 1
        # 按需修复邮箱索引
        if email:
            norm = _normalize_email(email)
            if not kv_get(f"{USER_PREFIX}index:email:{norm}", None):
                kv_set(f"{USER_PREFIX}index:email:{norm}", {"user_id": user_id})
                repaired += 1
    if repaired:
        logging.getLogger("auth").info("索引修复完成：重建 %d 条缺失索引", repaired)


# ── 未验证账号定时清理 ────────────────────────────────────────────────────────

UNVERIFIED_ACCOUNT_TTL = 86400  # 24 小时（秒）


def cleanup_unverified_accounts():
    """
    清理超过 24 小时仍未验证邮箱的账号。

    设计权衡（全表扫描）：
    当前实现是对 USER_PREFIX 下所有 key 做全表扫描，这在现阶段数据量下
    （几百到几千用户级别）性能可以接受；但如果用户量增长到数万级，
    每小时一次的全表扫描会变成线性增长的性能负担。届时应改为基于
    created_at 的索引查询或时间范围增量扫描，而不是继续全表过滤。
    """
    now = time.time()
    keys = kv_keys_prefix(USER_PREFIX)
    deleted = 0
    for key in keys:
        if key.startswith(f"{USER_PREFIX}index:"):
            continue
        data = kv_get(key)
        if not data:
            continue
        if not data.get("email_verified") and data.get("email"):
            age = now - data.get("created_at", 0)
            if age > UNVERIFIED_ACCOUNT_TTL:
                user = UserModel.from_dict(data)
                kv_delete(f"{USER_PREFIX}index:username:{user.username}")
                if user.email:
                    kv_delete(f"{USER_PREFIX}index:email:{_normalize_email(user.email)}")
                kv_delete(f"email_verify:{_normalize_email(user.email)}")
                kv_delete(f"{USER_PREFIX}{user.user_id}")
                deleted += 1
    if deleted:
        logging.getLogger("auth").info("清理了 %d 个超时未验证的账号", deleted)


# ── 客户端 IP 提取（反向代理安全）──────────────────────────────────────────

_TRUSTED_PROXIES = frozenset(
    p.strip() for p in
    os.environ.get("TRUSTED_PROXY_IPS",
                   "127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16")
    .split(",")
    if p.strip()
)


def extract_client_ip(request: Request) -> str:
    """
    从请求中提取真实客户端 IP。
    优先读取 X-Forwarded-For 最左侧 IP，但仅当直连 IP 属于受信代理时才信任 XFF，
    防止外部请求伪造此头。
    """
    direct_ip = request.client.host if request.client else "unknown"

    for proxy_range in _TRUSTED_PROXIES:
        try:
            network = ipaddress.ip_network(proxy_range, strict=False)
            if ipaddress.ip_address(direct_ip) in network:
                forwarded = request.headers.get("X-Forwarded-For", "")
                if forwarded:
                    return forwarded.split(",")[0].strip()  # 最左侧 = 真实客户端
                break
        except ValueError:
            continue
    return direct_ip


# ── 邮件发送限流（内存字典，双维度）─────────────────────────────────────────

_ip_send_times: Dict[str, list] = {}           # key: ip, value: 1小时内发送时间戳列表

# 邮箱维度的发送计数使用滑动窗口（key: email, value: [时间戳列表]）
_email_send_counts: Dict[str, list] = {}

_EMAIL_RATE_WINDOW = 60      # 窗口秒数
_EMAIL_RATE_MAX = 5           # 窗口内最大发送次数（注册失败后可重试3-5次）


def check_email_rate_limit(email: str, ip: str) -> Tuple[bool, str]:
    """
    双维度邮件发送限流。
    返回 (是否允许, 错误消息)。
    - 邮箱维度：每60秒最多5封（允许注册失败后重试几次）
    - IP 维度：每小时最多20封（宽松防滥用）
    """
    now = time.time()

    # 邮箱维度（滑动窗口，60秒5次）
    email_times = _email_send_counts.get(email, [])
    email_times = [t for t in email_times if now - t < _EMAIL_RATE_WINDOW]
    if len(email_times) >= _EMAIL_RATE_MAX:
        return False, "发送过于频繁，请60秒后再试"

    # IP 维度（滑动窗口，1小时20封）
    ip_times = _ip_send_times.get(ip, [])
    ip_times = [t for t in ip_times if now - t < 3600]
    if len(ip_times) >= 20:
        return False, "发送次数过多，请稍后再试"

    # 通过 → 记录
    email_times.append(now)
    _email_send_counts[email] = email_times
    ip_times.append(now)
    _ip_send_times[ip] = ip_times
    return True, ""


# ── 邮箱验证码管理（持久化到 kv_store，哈希存储）────────────────────────────

VERIFY_PREFIX = "email_verify:"
VERIFY_CODE_TTL = 600       # 10 分钟
VERIFY_MAX_ATTEMPTS = 5     # 最大验证失败次数
VERIFY_LOCKOUT = 1800       # 锁定时间 30 分钟


def save_verification_code(email: str, code: str, purpose: str = "verify_email"):
    """保存验证码（哈希存储，与密码同等对待）"""
    kv_set(f"{VERIFY_PREFIX}{_normalize_email(email)}", {
        "code_hash": _hash_code(code),
        "created_at": time.time(),
        "expires_at": time.time() + VERIFY_CODE_TTL,
        "failed_attempts": 0,
        "locked_until": 0,
        "purpose": purpose,
        "validated": False,
    })


def verify_email_code(
    email: str, code: str, purpose: str = "verify_email"
) -> Tuple[bool, str]:
    """Validate once, reserving the code for the transactional terminal write."""
    key = f"{VERIFY_PREFIX}{_normalize_email(email)}"
    kv_get(key, None)  # preserve the observable read seam used by fault tests
    with kv_transaction(immediate=True):
        record = _database.kv_get(key, None)
        required = {"code_hash", "expires_at", "failed_attempts", "locked_until", "purpose"}
        if not isinstance(record, dict) or not required.issubset(record):
            return False, "verification code is invalid or expired"
        if record["purpose"] != purpose or record.get("validated"):
            return False, "verification code is invalid or expired"
        now = time.time()
        if now > record["expires_at"]:
            kv_delete(key)
            return False, "verification code has expired"
        if record["locked_until"] > now:
            return False, "too many verification attempts"
        if not hmac_module.compare_digest(_hash_code(code), record["code_hash"]):
            record["failed_attempts"] += 1
            if record["failed_attempts"] >= VERIFY_MAX_ATTEMPTS:
                record["locked_until"] = now + VERIFY_LOCKOUT
            kv_set(key, record)
            return False, "verification code is incorrect"
        record["validated"] = True
        kv_set(key, record)
        return True, ""


def is_verify_locked(email: str) -> Tuple[bool, int]:
    """检查邮箱验证是否被锁定。返回 (是否锁定, 剩余秒数)"""
    record = kv_get(f"{VERIFY_PREFIX}{_normalize_email(email)}", None)
    if record and record.get("locked_until", 0) > time.time():
        return True, int(record["locked_until"] - time.time())
    return False, 0


# ── 密码重置令牌管理（持久化到 kv_store，哈希存储）─────────────────────────

RESET_PREFIX = "pwd_reset:"
RESET_TOKEN_TTL = 600      # 10 分钟


def generate_reset_token() -> str:
    """生成密码重置令牌（返回明文，数据库存哈希）"""
    return secrets.token_urlsafe(32)


def save_reset_token(email: str, token: str):
    """保存重置令牌（哈希存储）"""
    kv_set(f"{RESET_PREFIX}{_normalize_email(email)}", {
        "token_hash": _hash_code(token),
        "created_at": time.time(),
        "expires_at": time.time() + RESET_TOKEN_TTL,
    })


def verify_reset_token(email: str, token: str) -> Tuple[bool, str]:
    """验证重置令牌，返回 (是否通过, 错误消息)。一次性使用，通过后删除。"""
    key = f"{RESET_PREFIX}{_normalize_email(email)}"
    record = kv_get(key, None)
    if not record or not record.get("token_hash"):
        return False, "重置链接无效"
    if time.time() > record.get("expires_at", 0):
        kv_delete(key)
        return False, "重置链接已过期"
    if not hmac_module.compare_digest(_hash_code(token), record["token_hash"]):
        return False, "重置链接无效"
    kv_delete(key)  # 一次性使用
    return True, ""


# ── 暴力破解防护 ────────────────────────────────────────────────────────────────

MAX_FAILED_ATTEMPTS = int(os.environ.get("MAX_FAILED_ATTEMPTS", "5"))
LOCKOUT_DURATION = int(os.environ.get("LOCKOUT_DURATION", "90"))  # 秒（默认90秒）


def authenticate_user(login: str, password: str, ip: str = "unknown") -> Tuple[Optional[UserModel], str]:
    """
    认证用户，含防暴力破解保护。
    login 可以是用户名或邮箱。
    返回 (user_or_none, error_message)
    """
    user = get_user_by_username(login)
    if not user:
        user = get_user_by_email(login)  # 也尝试用邮箱查找

    # 用户不存在（防止用户枚举：延迟后返回通用错误）
    if not user:
        time.sleep(0.5)
        audit_log("LOGIN_FAILED", login, ip, "user_not_found")
        return None, "用户名或密码错误"

    # 检查锁定状态
    if user.is_locked():
        remaining = int(user.locked_until - time.time())
        audit_log("LOGIN_BLOCKED", user.username, ip, f"account_locked_remaining={remaining}s")
        return None, f"账户已被锁定，请在 {remaining} 秒后重试"

    # 验证密码
    if not verify_password(password, user.password_hash):
        user.failed_attempts += 1
        if user.failed_attempts >= MAX_FAILED_ATTEMPTS:
            user.locked_until = time.time() + LOCKOUT_DURATION
            audit_log("LOGIN_LOCKED", user.username, ip, f"failed_attempts={user.failed_attempts}")
            logging.getLogger("auth").warning(
                "账户锁定: %s (失败 %d 次)", user.username, user.failed_attempts
            )
        else:
            audit_log("LOGIN_FAILED", user.username, ip, f"failed_attempts={user.failed_attempts}")
        _save_user(user)
        time.sleep(0.5)  # 延迟响应，降低暴力破解速度
        return None, "用户名或密码错误"

    # 邮箱未验证拒绝登录（文案与密码错误一致，防止枚举）
    if not user.email_verified:
        audit_log("LOGIN_BLOCKED", user.username, ip, "email_not_verified")
        return None, "用户名或密码错误"

    # 登录成功：重置失败计数
    if user.failed_attempts > 0 or user.locked_until > 0:
        user.failed_attempts = 0
        user.locked_until = 0
        _save_user(user)
    audit_log("LOGIN_SUCCESS", user.username, ip)
    return user, ""


def change_password(user: UserModel, old_password: str, new_password: str) -> bool:
    """
    修改密码（需验证旧密码）。
    修改后递增 token_version 使所有旧 JWT 失效。
    """
    if not verify_password(old_password, user.password_hash):
        return False
    user.password_hash = hash_password(new_password)
    user.token_version += 1
    _save_user(user)
    audit_log("PASSWORD_CHANGED", user.username)
    return True


def reset_password(email: str, new_password: str):
    """重置密码（通过忘记密码流程），递增 token_version 使所有旧 JWT 失效"""
    user = get_user_by_email(email)
    if not user:
        raise ValueError("用户不存在")
    user.password_hash = hash_password(new_password)
    user.token_version += 1
    user.email_verified = True  # 重置密码同时隐式验证邮箱
    _save_user(user)
    audit_log("PASSWORD_RESET", user.username)


def verify_user_email(email: str):
    """标记用户邮箱已通过验证"""
    user = get_user_by_email(email)
    if not user:
        raise ValueError("用户不存在")
    user.email_verified = True
    _save_user(user)
    audit_log("EMAIL_VERIFIED", user.username)


# ── 管理员自动创建 ──────────────────────────────────────────────────────────────

def _consume_validated_code_and_update_user(
    email: str, purpose: str, update_user,
):
    key = f"{VERIFY_PREFIX}{_normalize_email(email)}"
    try:
        with kv_transaction(immediate=True):
            record = _database.kv_get(key, None)
            if not isinstance(record, dict) or not record.get("validated"):
                raise ValueError("verification code has not been validated")
            if record.get("purpose") != purpose:
                raise ValueError("verification code purpose mismatch")
            user = get_user_by_email(email)
            if not user:
                raise ValueError("user does not exist")
            update_user(user)
            _save_user(user)
            kv_delete(key)
            return user
    except Exception:
        # Validation is a reservation, not consumption.  Release it after a
        # failed terminal write so the same code can be safely retried.
        try:
            with kv_transaction(immediate=True):
                record = _database.kv_get(key, None)
                if isinstance(record, dict) and record.get("validated"):
                    record["validated"] = False
                    kv_set(key, record)
        except Exception:
            pass
        raise


def verify_user_email(email: str):
    def apply(user):
        user.email_verified = True

    user = _consume_validated_code_and_update_user(email, "verify_email", apply)
    audit_log("EMAIL_VERIFIED", user.username)


def reset_password(email: str, new_password: str):
    def apply(user):
        user.password_hash = hash_password(new_password)
        user.token_version += 1
        user.email_verified = True

    user = _consume_validated_code_and_update_user(email, "reset_password", apply)
    audit_log("PASSWORD_RESET", user.username)


def ensure_admin_user():
    """Ensure the initial admin exists without ever emitting its password."""
    admin_username = os.environ.get("ADMIN_USERNAME", "admin")
    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    existing = get_user_by_username(admin_username)
    if existing:
        return existing
    if not admin_password:
        raise RuntimeError(
            "ADMIN_PASSWORD must be explicitly configured before creating "
            "the initial administrator"
        )
    # 管理员自动创建时邮箱为空、验证状态为 True（跳过邮箱验证）
    user = create_user(admin_username, admin_password, role="admin",
                       email="", email_verified=True)
    audit_log("ADMIN_CREATED", admin_username, detail="initial_setup")
    return user


# ── FastAPI 依赖注入 ────────────────────────────────────────────────────────────

security = HTTPBearer(auto_error=False)


def get_mcp_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> UserModel:
    """Authenticate only dedicated MCP bearer tokens."""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MCP access token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return authenticate_mcp_token(credentials.credentials)

# Cookie 名称（与登录时 Set-Cookie 一致）
AUTH_COOKIE_NAME = "auth_token"


def is_test_auth_bypass_enabled() -> bool:
    """Return True only for explicitly enabled local integration tests."""
    return os.environ.get("METIS_TEST_MODE", "").strip().lower() in {"1", "true", "yes"}


def get_test_user() -> UserModel:
    """Synthetic admin user for legacy localhost integration tests."""
    return UserModel(
        user_id="test-user",
        username="pytest",
        password_hash="",
        role="admin",
        email="pytest@localhost",
        email_verified=True,
    )


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    request: Request = None,
) -> UserModel:
    """
    从 Bearer Token 或 HttpOnly Cookie 提取当前用户（未登录 → 401）。

    认证优先级：Bearer Token > HttpOnly Cookie
    Cookie 方式使前端无需存储 Token，防御 XSS 窃取。
    """
    if is_test_auth_bypass_enabled():
        return get_test_user()

    token = None
    # 1. Bearer Token（向后兼容 + MCP 客户端支持）
    if credentials:
        token = credentials.credentials
    # 2. HttpOnly Cookie（前端主认证方式）
    if not token and request:
        token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录",
                            headers={"WWW-Authenticate": "Bearer"})
    _, user = authenticate_token(token)
    return user


def get_admin_user(current_user: UserModel = Depends(get_current_user)) -> UserModel:
    """需要管理员权限"""
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return current_user
