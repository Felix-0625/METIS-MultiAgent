"""Red contracts for locally persisted JWT-backed data encryption.

These tests use synthetic credentials in the pytest-isolated database.  They
document the required recovery behavior without changing production code.
"""

from __future__ import annotations

import uuid

import pytest

from api.routes_config import _mask_api_config
from core import auth, database, persistence
from core.secret_storage import (
    ENCRYPTED_PREFIX,
    SecretStorageUnavailable,
    protect_config,
)


@pytest.fixture
def persisted_local_jwt(monkeypatch):
    """Model local startup where auth persists a generated JWT key in SQLite."""
    monkeypatch.delenv("METIS_DATA_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    synthetic_secret = "test-only-" + uuid.uuid4().hex + uuid.uuid4().hex
    database.kv_set(
        auth._JWT_SECRET_DB_KEY,
        {"secret": synthetic_secret, "created_at": 1.0},
    )
    monkeypatch.setattr(auth, "JWT_SECRET", synthetic_secret)
    return synthetic_secret


def _synthetic_config() -> tuple[dict, str]:
    marker = "test-key-" + uuid.uuid4().hex
    return {"user-a": {"model": "audit-model", "api_key": marker}}, marker


def test_generated_jwt_secret_is_the_data_encryption_fallback(persisted_local_jwt):
    configs, marker = _synthetic_config()

    stored = protect_config(configs, context="user API configuration")

    assert isinstance(stored, str) and stored.startswith(ENCRYPTED_PREFIX)
    assert marker not in stored


def test_save_fails_closed_before_write_when_no_key_exists(monkeypatch):
    monkeypatch.delenv("METIS_DATA_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setattr(auth, "JWT_SECRET", "")
    database.kv_delete(auth._JWT_SECRET_DB_KEY)
    database.kv_delete("user_api_configs")
    configs, _ = _synthetic_config()

    with pytest.raises(SecretStorageUnavailable):
        persistence.save_user_api_configs(configs)

    assert database.kv_get("user_api_configs", None) is None


def test_api_key_survives_restart_with_db_restored_jwt_secret(persisted_local_jwt):
    configs, marker = _synthetic_config()
    persistence.save_user_api_configs(configs)
    raw = database.kv_get("user_api_configs")

    # A fresh process restores auth.JWT_SECRET from this same database before
    # application state is loaded; no environment secret exists in local mode.
    auth.JWT_SECRET = ""
    auth._resolve_jwt_secret()
    restored = persistence.load_user_api_configs()

    assert isinstance(raw, str) and raw.startswith(ENCRYPTED_PREFIX)
    assert marker not in raw
    assert restored == configs


def test_lost_ack_commit_remains_recoverable_after_restart(persisted_local_jwt):
    configs, marker = _synthetic_config()

    # Model a committed DB write followed by transport loss before the caller
    # receives the acknowledgement.  Retry/restart must observe the commit.
    persistence.save_user_api_configs(configs)
    auth.JWT_SECRET = ""
    auth._resolve_jwt_secret()

    restored = persistence.load_user_api_configs()
    assert restored["user-a"]["api_key"] == marker


def test_legacy_plaintext_is_migrated_without_secret_loss(persisted_local_jwt):
    configs, marker = _synthetic_config()
    database.kv_set("user_api_configs", configs)

    restored = persistence.load_user_api_configs()
    migrated = database.kv_get("user_api_configs")

    assert restored == configs
    assert isinstance(migrated, str) and migrated.startswith(ENCRYPTED_PREFIX)
    assert marker not in migrated


def test_recovered_api_key_is_masked_without_mutating_runtime_value():
    configs, marker = _synthetic_config()
    runtime = configs["user-a"]

    response = _mask_api_config(runtime)

    assert response["api_key"] == "*****"
    assert runtime["api_key"] == marker
