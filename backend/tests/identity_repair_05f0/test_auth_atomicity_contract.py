"""Red contracts for registration and email-verification atomicity.

These tests intentionally describe the required production semantics.  They use
the real SQLite-backed auth implementation and only replace scheduling/failure
seams; no external email service is involved.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core import auth, database


PASSWORD = "Atomicity-contract-9!"


def _keys(prefix: str) -> list[str]:
    return sorted(database.kv_keys_prefix(prefix))


@pytest.fixture(autouse=True)
def _clean_auth_records():
    database.init_db()
    for prefix in (auth.USER_PREFIX, auth.VERIFY_PREFIX):
        for key in database.kv_keys_prefix(prefix):
            database.kv_delete(key)
    yield
    for prefix in (auth.USER_PREFIX, auth.VERIFY_PREFIX):
        for key in database.kv_keys_prefix(prefix):
            database.kv_delete(key)


def test_duplicate_registration_is_one_atomic_winner(monkeypatch):
    """B: concurrent duplicate username/email must create exactly one user."""
    gate = threading.Barrier(2)
    original = auth.get_user_by_username

    def synchronized_lookup(username: str):
        result = original(username)
        gate.wait(timeout=5)
        return result

    monkeypatch.setattr(auth, "get_user_by_username", synchronized_lookup)

    def register():
        try:
            return auth.create_user("same-user", PASSWORD, email="same@example.test")
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: register(), range(2)))

    monkeypatch.setattr(auth, "get_user_by_username", original)
    assert sum(user is not None for user in results) == 1
    primary = [key for key in _keys(auth.USER_PREFIX) if "index:" not in key]
    assert len(primary) == 1
    assert auth.get_user_by_username("same-user").user_id == auth.get_user_by_email("same@example.test").user_id


@pytest.mark.parametrize("failure_point", ["before", "during", "lost_ack"])
def test_registration_failure_has_all_or_nothing_retry_semantics(monkeypatch, failure_point):
    """B: before/during/lost-ack failures cannot leave partial users or indexes."""
    original = auth.kv_set
    calls = 0

    def faulted_set(key, value):
        nonlocal calls
        calls += 1
        if failure_point == "before" and calls == 1:
            raise OSError("injected before write")
        if failure_point == "during" and calls == 3:
            raise OSError("injected during multi-record write")
        original(key, value)
        if failure_point == "lost_ack" and calls == 3:
            raise OSError("injected lost acknowledgement")

    monkeypatch.setattr(auth, "kv_set", faulted_set)
    with pytest.raises((OSError, RuntimeError)):
        auth.create_user("fault-user", PASSWORD, email="fault@example.test")

    # A failed create is retryable with the same identity and has no ghost rows.
    assert _keys(auth.USER_PREFIX) == []
    monkeypatch.setattr(auth, "kv_set", original)
    assert auth.create_user("fault-user", PASSWORD, email="fault@example.test")


def test_verify_write_failure_preserves_code_for_safe_retry(monkeypatch):
    """D/E: consuming the code and marking the user verified is one transaction."""
    email = "retry@example.test"
    code = "123456"
    user = auth.create_user("retry-user", PASSWORD, email=email, email_verified=False)
    auth.save_verification_code(email, code)
    original_save = auth._save_user
    monkeypatch.setattr(auth, "_save_user", lambda _user: (_ for _ in ()).throw(OSError("injected user write")))

    ok, error = auth.verify_email_code(email, code)
    assert ok, error
    with pytest.raises(OSError):
        auth.verify_user_email(email)

    # Required postcondition: failed user write rolls code consumption back.
    assert database.kv_get(f"{auth.VERIFY_PREFIX}{email}", None) is not None
    assert auth.get_user_by_id(user.user_id).email_verified is False
    monkeypatch.setattr(auth, "_save_user", original_save)
    ok, error = auth.verify_email_code(email, code)
    assert ok, error
    auth.verify_user_email(email)
    assert auth.get_user_by_id(user.user_id).email_verified is True


def test_concurrent_verification_has_exactly_one_consumer(monkeypatch):
    """D: two matching-code requests cannot both consume the same code."""
    email = "race@example.test"
    code = "654321"
    auth.create_user("race-user", PASSWORD, email=email, email_verified=False)
    auth.save_verification_code(email, code)
    gate = threading.Barrier(2)
    original_get = auth.kv_get
    verify_key = f"{auth.VERIFY_PREFIX}{email}"

    def synchronized_get(key, default=None):
        value = original_get(key, default)
        if key == verify_key:
            gate.wait(timeout=5)
        return value

    monkeypatch.setattr(auth, "kv_get", synchronized_get)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: auth.verify_email_code(email, code)[0], range(2)))

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 1


@pytest.mark.parametrize(
    "legacy_record",
    [
        {"code": "123456"},
        {"code_hash": "invalid-old-hash", "created_at": 0},
        {"code_hash": "", "expires_at": 32503680000},
    ],
)
def test_legacy_verification_schema_fails_closed_without_verifying_user(legacy_record):
    """D/E: incomplete/obsolete records are rejected without state promotion."""
    email = "legacy@example.test"
    user = auth.create_user("legacy-user", PASSWORD, email=email, email_verified=False)
    database.kv_set(f"{auth.VERIFY_PREFIX}{email}", legacy_record)

    ok, _ = auth.verify_email_code(email, "123456")

    assert ok is False
    assert auth.get_user_by_id(user.user_id).email_verified is False
