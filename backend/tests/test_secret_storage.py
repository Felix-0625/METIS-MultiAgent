import json

import pytest
from cryptography.fernet import Fernet

from core import database, persistence
from core.secret_storage import (
    ENCRYPTED_PREFIX,
    SecretStorageUnavailable,
    clear_sensitive_values,
    protect_config,
    restore_config,
)


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setattr(database, "_sqlite_path", str(tmp_path / "secrets.db"))
    monkeypatch.setattr(database, "_pg_pool", None)
    monkeypatch.setattr(database, "_pg_unavailable", False)
    database.init_db()
    return tmp_path


def test_secret_config_is_encrypted_and_round_trips(monkeypatch):
    monkeypatch.setenv("METIS_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    config = {"model": "test", "api_key": "sk-live-secret", "nested": {"clientSecret": "nested-secret"}}

    stored = protect_config(config)
    restored = restore_config(stored)

    assert isinstance(stored, str) and stored.startswith(ENCRYPTED_PREFIX)
    assert "sk-live-secret" not in stored
    assert "nested-secret" not in stored
    assert restored.value == config
    assert restored.replacement is None


def test_missing_key_clears_secrets_before_persistence(monkeypatch):
    monkeypatch.delenv("METIS_DATA_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    config = {"api_key": "must-not-persist", "model": "safe", "headers": {"Authorization": "Bearer secret"}}

    stored = protect_config(config)

    assert stored == {"api_key": "", "model": "safe", "headers": {"Authorization": ""}}
    assert "must-not-persist" not in repr(stored)


def test_encrypted_config_is_retained_but_not_loaded_without_key(monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("METIS_DATA_ENCRYPTION_KEY", key)
    stored = protect_config({"api_key": "recoverable-secret"})
    monkeypatch.delenv("METIS_DATA_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)

    with pytest.raises(SecretStorageUnavailable):
        restore_config(stored)


def test_max_tokens_is_not_mistaken_for_a_secret(monkeypatch):
    monkeypatch.delenv("METIS_DATA_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)

    stored = protect_config({"max_tokens": 8192, "model": "safe"})

    assert stored == {"max_tokens": 8192, "model": "safe"}


def test_legacy_plaintext_is_migrated_to_ciphertext(isolated_db, monkeypatch):
    monkeypatch.setenv("METIS_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    database.kv_set("default_api_config", {"api_key": "legacy-secret", "model": "m"})

    loaded = persistence.load_default_api_config()
    raw = database.kv_get("default_api_config")

    assert loaded == {"api_key": "legacy-secret", "model": "m"}
    assert isinstance(raw, str) and raw.startswith(ENCRYPTED_PREFIX)
    assert "legacy-secret" not in raw


def test_legacy_plaintext_is_cleared_when_key_is_missing(isolated_db, monkeypatch):
    monkeypatch.delenv("METIS_DATA_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    database.kv_set("user_api_configs", {"user-1": {"api_key": "legacy-secret", "model": "m"}})

    loaded = persistence.load_user_api_configs()
    raw = database.kv_get("user_api_configs")

    expected = {"user-1": {"api_key": "", "model": "m"}}
    assert loaded == expected
    assert raw == expected
    assert "legacy-secret" not in repr(raw)


def test_atomic_application_state_encrypts_all_config_buckets(isolated_db, monkeypatch):
    monkeypatch.setenv("METIS_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))

    persistence.save_application_state(
        {
            "projects": {"project-1": {"name": "safe"}},
            "agents_config": {"agent-1": {"api_key": "agent-secret"}},
            "default_api_config": {"api_key": "default-secret"},
            "user_api_configs": {"user-1": {"api_key": "user-secret"}},
        }
    )

    for key, secret in (
        ("agents_config", "agent-secret"),
        ("default_api_config", "default-secret"),
        ("user_api_configs", "user-secret"),
    ):
        raw = database.kv_get(key)
        assert isinstance(raw, str) and raw.startswith(ENCRYPTED_PREFIX)
        assert secret not in raw
    assert database.kv_get("projects") == {"project-1": {"name": "safe"}}


def test_export_snapshot_clears_agent_credentials(isolated_db, monkeypatch):
    monkeypatch.chdir(isolated_db)

    path = persistence.export_snapshot({}, {"agent": {"api_key": "snapshot-secret", "model": "m"}}, {})
    snapshot = json.loads((isolated_db / path).read_text(encoding="utf-8"))

    assert snapshot["agents_config"] == {"agent": {"api_key": "", "model": "m"}}
    assert "snapshot-secret" not in (isolated_db / path).read_text(encoding="utf-8")


def test_clear_sensitive_values_does_not_mutate_source():
    original = {"api_key": "secret", "models": ["a", "b"]}

    cleared = clear_sensitive_values(original)

    assert cleared == {"api_key": "", "models": ["a", "b"]}
    assert original["api_key"] == "secret"


def test_stable_jwt_secret_encrypts_per_user_configs_across_restart(isolated_db, monkeypatch):
    monkeypatch.delenv("METIS_DATA_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("JWT_SECRET", "stable-render-jwt-secret-with-sufficient-entropy")
    configs = {
        "user-1": {"model": "model-a", "api_key": "user-one-secret"},
        "user-2": {"model": "model-b", "api_key": "user-two-secret"},
    }

    persistence.save_user_api_configs(configs)
    raw = database.kv_get("user_api_configs")
    restored = persistence.load_user_api_configs()

    assert isinstance(raw, str) and raw.startswith(ENCRYPTED_PREFIX)
    assert "user-one-secret" not in raw
    assert "user-two-secret" not in raw
    assert restored == configs
