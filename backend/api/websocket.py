"""WebSocket 实时通信广播机制 — 需要 JWT 认证"""
import asyncio
import json
import logging
import os
import time
from http.cookies import CookieError, SimpleCookie
from typing import Dict, List
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
from fastapi.websockets import WebSocketState

from core.auth import AUTH_COOKIE_NAME, authenticate_token

logger = logging.getLogger("websocket")

router = APIRouter(tags=["websocket"])

WS_AUTH_RECHECK_SECONDS = max(
    1.0,
    float(os.environ.get("WS_AUTH_RECHECK_SECONDS", "30")),
)
_LOCAL_WS_ORIGINS = (
    "http://localhost:3000,http://127.0.0.1:3000,"
    "http://localhost:5173,http://127.0.0.1:5173"
)


class WebSocketPolicyError(RuntimeError):
    """Expected fail-closed WebSocket authentication/authorization failure."""


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def _normalize_origin(origin: str) -> str:
    normalized = str(origin or "").strip().rstrip("/")
    if not normalized or normalized == "null":
        return ""
    if not normalized.lower().startswith(("http://", "https://")):
        return ""
    # Origin never contains a path/query/fragment. Reject malformed allowlist
    # entries instead of comparing only a potentially attacker-controlled host.
    remainder = normalized.split("://", 1)[1]
    if any(marker in remainder for marker in ("/", "?", "#", "@")):
        return ""
    return normalized.lower()


def _allowed_origins() -> set[str]:
    raw = (
        os.environ.get("WS_ALLOWED_ORIGINS")
        or os.environ.get("CORS_ORIGINS")
        or _LOCAL_WS_ORIGINS
    )
    return {
        normalized
        for item in raw.split(",")
        if (normalized := _normalize_origin(item))
    }


def _validate_origin(websocket: WebSocket) -> None:
    origin = _normalize_origin(websocket.headers.get("origin", ""))
    if not origin:
        if _env_enabled("WS_ALLOW_MISSING_ORIGIN"):
            return
        raise WebSocketPolicyError("WebSocket Origin 缺失或无效")
    if origin not in _allowed_origins():
        raise WebSocketPolicyError("WebSocket Origin 不在允许列表")


def _cookie_token(websocket: WebSocket) -> str:
    raw_cookie = websocket.headers.get("cookie", "")
    if not raw_cookie:
        return ""
    cookies = SimpleCookie()
    try:
        cookies.load(raw_cookie)
    except CookieError:
        return ""
    morsel = cookies.get(AUTH_COOKIE_NAME)
    return morsel.value if morsel else ""


def _resolve_token(websocket: WebSocket, query_token: str | None) -> str:
    if query_token:
        if not _env_enabled("WS_ALLOW_QUERY_TOKEN"):
            raise WebSocketPolicyError("URL query token 已禁用")
        return query_token
    token = _cookie_token(websocket)
    if not token:
        raise WebSocketPolicyError("未提供认证令牌")
    return token


def _authenticate_project_socket(project_id: str, token: str):
    _, user = authenticate_token(token)
    from core.app_state import projects

    project = projects.get(project_id)
    if project is None:
        raise WebSocketPolicyError("项目不存在")
    owner_user_id = str(getattr(project, "owner_user_id", "") or "").strip()
    if not owner_user_id:
        raise WebSocketPolicyError("项目缺少所有者，拒绝实时连接")
    if user.role != "admin" and owner_user_id != user.user_id:
        raise WebSocketPolicyError("无权访问此项目")
    return user


async def _close_policy(websocket: WebSocket, reason: str) -> None:
    # Keep client-facing reasons stable and free of internal exception details.
    await websocket.close(
        code=status.WS_1008_POLICY_VIOLATION,
        reason=reason[:120],
    )


class ConnectionManager:
    """按项目频道管理 WebSocket 连接"""

    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, project_id: str, websocket: WebSocket):
        await websocket.accept()
        if project_id not in self.active_connections:
            self.active_connections[project_id] = []
        self.active_connections[project_id].append(websocket)
        logger.info(f"WebSocket 连接: project={project_id}, 当前连接数={len(self.active_connections[project_id])}")

    def disconnect(self, project_id: str, websocket: WebSocket):
        if project_id in self.active_connections:
            try:
                self.active_connections[project_id].remove(websocket)
            except ValueError:
                pass
            if not self.active_connections[project_id]:
                del self.active_connections[project_id]
                logger.info(f"WebSocket 断开: project={project_id}（频道的所有连接已关闭）")
            else:
                logger.info(f"WebSocket 断开: project={project_id}, 剩余连接数={len(self.active_connections[project_id])}")

    async def broadcast(self, project_id: str, event_type: str, payload: dict):
        """广播给该项目频道下全部订阅的客户端"""
        if project_id not in self.active_connections:
            return
        if not self.active_connections[project_id]:
            return

        message = json.dumps({
            "type": event_type,
            "payload": payload,
            "timestamp": time.time(),
        })

        # 复制列表以避免迭代时被修改
        dead_connections = []
        for conn in list(self.active_connections[project_id]):
            try:
                if conn.client_state == WebSocketState.CONNECTED:
                    await conn.send_text(message)
            except Exception:
                dead_connections.append(conn)

        # 清理失效连接
        for conn in dead_connections:
            self.disconnect(project_id, conn)

    def get_connection_count(self, project_id: str) -> int:
        """获取项目频道的连接数"""
        return len(self.active_connections.get(project_id, []))


manager = ConnectionManager()


@router.websocket("/ws/{project_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    project_id: str,
    token: str = Query(None),
):
    """
    WebSocket 连接端点（需要 JWT 认证）

    认证方式：
    1. Cookie → auth_token（HttpOnly，默认且推荐）
    2. query string 仅在 WS_ALLOW_QUERY_TOKEN=true 时兼容启用
    心跳：客户端发送 "ping"，服务端回复 "pong"
    """
    try:
        _validate_origin(websocket)
        resolved_token = _resolve_token(websocket, token)
        user = _authenticate_project_socket(project_id, resolved_token)
        logger.info("WebSocket 认证通过: user=%s project=%s", user.username, project_id)
    except WebSocketPolicyError as exc:
        await _close_policy(websocket, str(exc))
        return
    except Exception:
        await _close_policy(websocket, "认证失败")
        return

    await manager.connect(project_id, websocket)
    receive_task = None
    try:
        while True:
            if receive_task is None:
                receive_task = asyncio.create_task(websocket.receive_text())
            done, _ = await asyncio.wait(
                {receive_task},
                timeout=WS_AUTH_RECHECK_SECONDS,
            )
            if not done:
                # Revalidate even while the client is idle so password reset,
                # user deletion and ownership changes revoke existing sockets.
                try:
                    _authenticate_project_socket(project_id, resolved_token)
                except Exception:
                    await _close_policy(websocket, "连接权限已失效")
                    return
                continue

            data = receive_task.result()
            receive_task = None
            try:
                _authenticate_project_socket(project_id, resolved_token)
            except Exception:
                await _close_policy(websocket, "连接权限已失效")
                return
            # 心跳响应
            if data == "ping":
                await websocket.send_text("pong")
            elif data == "pong":
                pass
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning(
            "WebSocket 异常 project=%s type=%s",
            project_id,
            type(exc).__name__,
        )
    finally:
        if receive_task is not None and not receive_task.done():
            receive_task.cancel()
        manager.disconnect(project_id, websocket)
