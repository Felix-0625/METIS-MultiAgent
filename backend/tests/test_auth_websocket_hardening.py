import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from core import auth, database


@pytest.fixture()
def isolated_auth(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setattr(database, "_pg_pool", None)
    monkeypatch.setattr(database, "_sqlite_path", str(tmp_path / "auth.db"))
    monkeypatch.setattr(auth, "JWT_SECRET", "audit-test-secret-" * 4)
    monkeypatch.delenv("REQUIRE_DATABASE_URL", raising=False)
    monkeypatch.delenv("METIS_ENV", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    database.init_db()
    return tmp_path


def _make_user(username: str = "security_user"):
    return auth.create_user(
        username,
        "OldPass#2026",
        email=f"{username}@example.invalid",
        email_verified=True,
    )


def _load_websocket_module():
    path = Path(__file__).parents[1] / "api" / "websocket.py"
    spec = importlib.util.spec_from_file_location("isolated_websocket_module", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Project:
    def __init__(self, owner_user_id: str):
        self.owner_user_id = owner_user_id


class _Socket:
    def __init__(self, ws_module, *, origin: str, cookie: str = "", block=False):
        self._ws_module = ws_module
        self._block = block
        self.headers = {"origin": origin, "cookie": cookie}
        self.client_state = ws_module.WebSocketState.CONNECTED
        self.accepted = False
        self.closed = []
        self.sent = []

    async def accept(self):
        self.accepted = True

    async def close(self, code=None, reason=None):
        self.closed.append((code, reason))

    async def receive_text(self):
        if self._block:
            await asyncio.Event().wait()
        raise self._ws_module.WebSocketDisconnect()

    async def send_text(self, value):
        self.sent.append(value)


def test_password_change_invalidates_all_token_entry_points(isolated_auth):
    user = _make_user()
    old_token = auth.create_token(
        user.user_id, user.username, user.role, user.token_version
    )

    assert auth.change_password(user, "OldPass#2026", "NewPass#2026")

    with pytest.raises(HTTPException) as decode_error:
        auth.decode_token(old_token)
    assert decode_error.value.status_code == 401

    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=old_token
    )
    with pytest.raises(HTTPException) as dependency_error:
        auth.get_current_user(credentials, None)
    assert dependency_error.value.status_code == 401

    current = auth.get_user_by_id(user.user_id)
    new_token = auth.create_token(
        current.user_id, current.username, current.role, current.token_version
    )
    payload = auth.decode_token(new_token)
    assert payload["sub"] == user.user_id
    assert payload["_user"].user_id == user.user_id


def test_production_requires_explicit_strong_jwt_secret(monkeypatch):
    monkeypatch.setenv("REQUIRE_DATABASE_URL", "true")
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setattr(auth, "JWT_SECRET", "")

    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        auth._resolve_jwt_secret()

    monkeypatch.setenv("JWT_SECRET", "short")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        auth._resolve_jwt_secret()


def test_initial_admin_password_is_never_printed(
    isolated_auth, monkeypatch, capsys
):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("REQUIRE_DATABASE_URL", "true")

    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        auth.ensure_admin_user()

    captured = capsys.readouterr()
    assert "密码:" not in captured.out
    assert "password:" not in captured.out.lower()


def test_websocket_rejects_bad_origin_query_token_and_ownerless_project(
    isolated_auth, monkeypatch
):
    asyncio.run(_rejects_bad_origin_query_token_and_ownerless_project(
        isolated_auth, monkeypatch,
    ))


async def _rejects_bad_origin_query_token_and_ownerless_project(
    isolated_auth, monkeypatch
):
    ws = _load_websocket_module()
    user = _make_user("ws_user")
    token = auth.create_token(
        user.user_id, user.username, user.role, user.token_version
    )
    fake_state = types.ModuleType("core.app_state")
    monkeypatch.setitem(sys.modules, "core.app_state", fake_state)
    monkeypatch.setenv("WS_ALLOWED_ORIGINS", "https://good.example")
    monkeypatch.delenv("WS_ALLOW_QUERY_TOKEN", raising=False)

    fake_state.projects = {"p": _Project(user.user_id)}
    evil = _Socket(
        ws,
        origin="https://evil.example",
        cookie=f"{auth.AUTH_COOKIE_NAME}={token}",
    )
    await ws.websocket_endpoint(evil, "p", None)
    assert not evil.accepted
    assert evil.closed

    query = _Socket(ws, origin="https://good.example")
    await ws.websocket_endpoint(query, "p", token)
    assert not query.accepted
    assert query.closed

    fake_state.projects = {"p": _Project("")}
    ownerless = _Socket(
        ws,
        origin="https://good.example",
        cookie=f"{auth.AUTH_COOKIE_NAME}={token}",
    )
    await ws.websocket_endpoint(ownerless, "p", None)
    assert not ownerless.accepted
    assert ownerless.closed

    fake_state.projects = {}
    missing = _Socket(
        ws,
        origin="https://good.example",
        cookie=f"{auth.AUTH_COOKIE_NAME}={token}",
    )
    await ws.websocket_endpoint(missing, "p", None)
    assert not missing.accepted
    assert missing.closed


def test_websocket_accepts_owned_cookie_and_rechecks_revocation(
    isolated_auth, monkeypatch
):
    asyncio.run(_accepts_owned_cookie_and_rechecks_revocation(
        isolated_auth, monkeypatch,
    ))


async def _accepts_owned_cookie_and_rechecks_revocation(
    isolated_auth, monkeypatch
):
    ws = _load_websocket_module()
    user = _make_user("ws_recheck_user")
    token = auth.create_token(
        user.user_id, user.username, user.role, user.token_version
    )
    fake_state = types.ModuleType("core.app_state")
    fake_state.projects = {"p": _Project(user.user_id)}
    monkeypatch.setitem(sys.modules, "core.app_state", fake_state)
    monkeypatch.setenv("WS_ALLOWED_ORIGINS", "https://good.example")
    monkeypatch.setattr(ws, "WS_AUTH_RECHECK_SECONDS", 0.01)

    socket = _Socket(
        ws,
        origin="https://good.example",
        cookie=f"{auth.AUTH_COOKIE_NAME}={token}",
        block=True,
    )

    async def revoke():
        await asyncio.sleep(0.02)
        assert auth.change_password(user, "OldPass#2026", "NewPass#2026")

    await asyncio.wait_for(
        asyncio.gather(ws.websocket_endpoint(socket, "p", None), revoke()),
        timeout=1,
    )
    assert socket.accepted
    assert socket.closed
    assert socket.closed[-1][0] == ws.status.WS_1008_POLICY_VIOLATION
