import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from core import auth
from api import routes_auth
from models.schemas import LoginRequest, RegisterRequest


@pytest.mark.parametrize("username", ["ab", "中文用户", "has space", "emoji😀", "a" * 65, "_starts"])
def test_register_rejects_username_outside_audit_safe_contract(username):
    with pytest.raises(ValidationError):
        RegisterRequest(username=username, password="password", email="user@example.test")


@pytest.mark.parametrize("username", ["abc", "User_01", "a.b-c"])
def test_register_and_login_share_username_contract(username):
    assert RegisterRequest(
        username=username, password="password", email="user@example.test"
    ).username == username
    assert LoginRequest(login=username, password="password").login == username


def test_login_keeps_email_identifier_support():
    assert LoginRequest(login="user@example.test", password="password").login == "user@example.test"


def test_create_user_enforces_username_contract_before_persistence(monkeypatch):
    persisted = []
    monkeypatch.setattr(auth, "kv_set", lambda *args: persisted.append(args))

    with pytest.raises(ValueError, match="用户名须为3-64位"):
        auth.create_user("非法 用户", "password")

    assert persisted == []


def test_create_user_normalizes_username_before_indexing(monkeypatch):
    records = {}
    monkeypatch.setattr(auth, "kv_get", lambda key, default=None: records.get(key, default))
    monkeypatch.setattr(auth, "kv_set", lambda key, value: records.__setitem__(key, value))
    monkeypatch.setattr(auth, "kv_delete", lambda key: records.pop(key, None))

    user = auth.create_user("  valid_user  ", "password")

    assert user.username == "valid_user"
    assert "user:index:username:valid_user" in records


def test_register_route_returns_422_for_invalid_username():
    app = FastAPI()
    app.include_router(routes_auth.router)
    response = TestClient(app).post(
        "/auth/register",
        json={"username": "非法 用户", "password": "password", "email": "user@example.test"},
    )

    assert response.status_code == 422
    assert "用户名须为3-64位" in response.json()["detail"][0]["msg"]


def test_register_route_returns_409_for_duplicate_username(monkeypatch):
    app = FastAPI()
    app.include_router(routes_auth.router)
    monkeypatch.setattr(routes_auth, "check_email_rate_limit", lambda *_: (True, ""))
    monkeypatch.setattr(routes_auth, "is_verify_locked", lambda *_: (False, 0))
    monkeypatch.setattr(routes_auth, "is_email_configured", lambda: False)
    monkeypatch.setattr(
        routes_auth,
        "create_user",
        lambda **_: (_ for _ in ()).throw(ValueError("用户名 'existing' 已存在")),
    )

    response = TestClient(app).post(
        "/auth/register",
        json={"username": "existing", "password": "password", "email": "user@example.test"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "用户名 'existing' 已存在"


def test_register_route_reports_provider_failure_without_blame_email(monkeypatch):
    class User:
        user_id = "created-user"

    deleted = []
    app = FastAPI()
    app.include_router(routes_auth.router)
    monkeypatch.setattr(routes_auth, "check_email_rate_limit", lambda *_: (True, ""))
    monkeypatch.setattr(routes_auth, "is_verify_locked", lambda *_: (False, 0))
    monkeypatch.setattr(routes_auth, "is_email_configured", lambda: True)
    monkeypatch.setattr(routes_auth, "create_user", lambda **_: User())
    monkeypatch.setattr(routes_auth, "save_verification_code", lambda *_: None)
    monkeypatch.setattr(
        routes_auth,
        "send_verification_email",
        lambda *_: (_ for _ in ()).throw(RuntimeError("provider rejected credentials")),
    )
    monkeypatch.setattr(routes_auth, "delete_user", deleted.append)

    response = TestClient(app).post(
        "/auth/register",
        json={"username": "valid_user", "password": "password", "email": "user@example.test"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "邮件服务暂时不可用，请稍后重试或联系管理员"
    assert deleted == ["created-user"]
