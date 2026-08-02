import pytest
from fastapi import HTTPException

from core import auth


def _memory_store(monkeypatch):
    store = {}
    monkeypatch.setattr(auth, "kv_set", lambda key, value: store.__setitem__(key, value))
    monkeypatch.setattr(auth, "kv_get", lambda key, default=None: store.get(key, default))
    monkeypatch.setattr(auth, "kv_delete", lambda key: store.pop(key, None))
    return store


def test_mcp_token_is_hashed_scoped_and_revocable(monkeypatch):
    store = _memory_store(monkeypatch)
    user = auth.UserModel("u1", "felix", "hash")
    monkeypatch.setattr(auth, "get_user_by_id", lambda user_id: user if user_id == "u1" else None)
    token = auth.create_mcp_token(user)
    assert token.startswith("metis_mcp_")
    assert token not in repr(store)
    assert auth.authenticate_mcp_token(token) is user
    assert auth.revoke_mcp_token("u1") is True
    with pytest.raises(HTTPException) as exc:
        auth.authenticate_mcp_token(token)
    assert exc.value.status_code == 401


def test_rotating_mcp_token_revokes_previous_token(monkeypatch):
    _memory_store(monkeypatch)
    user = auth.UserModel("u1", "felix", "hash")
    monkeypatch.setattr(auth, "get_user_by_id", lambda user_id: user if user_id == "u1" else None)
    old_token = auth.create_mcp_token(user)
    new_token = auth.create_mcp_token(user)
    with pytest.raises(HTTPException):
        auth.authenticate_mcp_token(old_token)
    assert auth.authenticate_mcp_token(new_token) is user


def test_named_tokens_are_encrypted_listed_revealed_and_independently_revoked(
    monkeypatch,
):
    store = _memory_store(monkeypatch)
    monkeypatch.setenv("METIS_DATA_ENCRYPTION_KEY", "x" * 32)
    user = auth.UserModel("u1", "felix", auth.hash_password("correct-password"))
    monkeypatch.setattr(auth, "get_user_by_id", lambda user_id: user if user_id == "u1" else None)

    first = auth.create_named_mcp_token(user, "Trae")
    second = auth.create_named_mcp_token(user, "Codex")
    listed = auth.list_mcp_tokens("u1")

    assert [item["name"] for item in listed] == ["Codex", "Trae"]
    assert first["token"] not in repr(store)
    assert second["token"] not in repr(store)
    assert auth.reveal_mcp_token(user, first["token_id"], "correct-password") == first["token"]
    with pytest.raises(ValueError, match="密码错误"):
        auth.reveal_mcp_token(user, first["token_id"], "wrong-password")

    assert auth.revoke_named_mcp_token("u1", first["token_id"]) is True
    with pytest.raises(HTTPException):
        auth.authenticate_mcp_token(first["token"])
    assert auth.authenticate_mcp_token(second["token"]) is user


def test_named_token_cannot_be_revealed_or_revoked_by_another_user(monkeypatch):
    _memory_store(monkeypatch)
    monkeypatch.setenv("METIS_DATA_ENCRYPTION_KEY", "x" * 32)
    owner = auth.UserModel("u1", "felix", auth.hash_password("owner-password"))
    other = auth.UserModel("u2", "other", auth.hash_password("other-password"))
    created = auth.create_named_mcp_token(owner, "Trae")

    with pytest.raises(KeyError):
        auth.reveal_mcp_token(other, created["token_id"], "other-password")
    assert auth.revoke_named_mcp_token("u2", created["token_id"]) is False


@pytest.mark.parametrize("name", ["", "   ", "x" * 65])
def test_named_token_rejects_invalid_names(monkeypatch, name):
    _memory_store(monkeypatch)
    monkeypatch.setenv("METIS_DATA_ENCRYPTION_KEY", "x" * 32)
    user = auth.UserModel("u1", "felix", "hash")
    with pytest.raises(ValueError):
        auth.create_named_mcp_token(user, name)
