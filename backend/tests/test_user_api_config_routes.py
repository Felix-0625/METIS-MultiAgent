"""Security contract for per-user LLM configuration responses."""

from api import routes_config


def test_api_config_mask_never_returns_plaintext_key() -> None:
    secret = "sk-user-specific-secret"

    masked = routes_config._mask_api_config({
        "model": "test-model",
        "api_base": "https://example.invalid/v1",
        "api_key": secret,
    })

    assert masked["api_key"] == "*****"
    assert secret not in repr(masked)


def test_api_error_message_redacts_configured_key_and_credential_url() -> None:
    secret = "sk-user-specific-secret"

    message = routes_config._safe_api_message(
        f"request failed for {secret} at https://user:password@example.invalid/v1",
        secret,
    )

    assert secret not in message
    assert "password" not in message
    assert "[REDACTED]" in message


def test_legacy_config_is_exposed_with_generator_and_reviewer_defaults() -> None:
    normalized = routes_config._normalize_api_config({
        "model": "legacy-model",
        "temperature": 0.9,
    })

    assert normalized["generator"] == {
        "model": "legacy-model", "temperature": 0.1,
    }
    assert normalized["reviewer"] == {
        "model": "legacy-model", "temperature": 0.0,
    }
