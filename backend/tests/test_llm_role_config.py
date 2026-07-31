from core.hermes_client import (
    HermesClient,
    Message,
    MessageRole,
    REVIEWER_NON_AUTHORITATIVE_INSTRUCTION,
    current_user_api_config,
)


def test_legacy_config_uses_role_temperature_defaults() -> None:
    client = HermesClient(api_key="fallback", model="fallback-model")
    token = current_user_api_config.set({
        "api_key": "user-key",
        "model": "legacy-model",
        "temperature": 0.8,
    })
    try:
        generator = client._get_effective_config("generator")
        reviewer = client._get_effective_config("reviewer")
    finally:
        current_user_api_config.reset(token)

    assert generator["model"] == "legacy-model"
    assert generator["temperature"] == 0.1
    assert reviewer["model"] == "legacy-model"
    assert reviewer["temperature"] == 0.0


def test_role_config_selects_independent_models_and_temperatures() -> None:
    client = HermesClient(api_key="fallback")
    token = current_user_api_config.set({
        "api_key": "user-key",
        "model": "legacy-model",
        "generator": {"model": "code-model", "temperature": 0.2},
        "reviewer": {"model": "audit-model", "temperature": 0},
    })
    try:
        assert client._get_effective_config("generator")["model"] == "code-model"
        assert client._get_effective_config("generator")["temperature"] == 0.2
        assert client._get_effective_config("reviewer")["model"] == "audit-model"
        assert client._get_effective_config("reviewer")["temperature"] == 0
    finally:
        current_user_api_config.reset(token)


def test_reviewer_call_is_advisory_and_purpose_is_not_sent_to_provider(monkeypatch) -> None:
    client = HermesClient(api_key="test", model="base")
    captured = {}

    def fake_send(payload, api_key, base_url):
        captured.update(payload)
        return {"content": '{"passed": true}'}

    monkeypatch.setattr(client, "_send_request", fake_send)
    token = current_user_api_config.set({
        "api_key": "user-key",
        "reviewer": {"model": "audit-model", "temperature": 0},
    })
    try:
        client.chat(
            [Message(role=MessageRole.USER, content="review this")],
            purpose="reviewer",
            use_cache=False,
        )
    finally:
        current_user_api_config.reset(token)

    assert captured["model"] == "audit-model"
    assert captured["temperature"] == 0
    assert "purpose" not in captured
    assert captured["messages"][0]["content"] == REVIEWER_NON_AUTHORITATIVE_INSTRUCTION
    assert "deterministic execution evidence" in captured["messages"][0]["content"]


def test_disabled_thinking_is_forwarded_to_provider(monkeypatch) -> None:
    client = HermesClient(api_key="test", model="base")
    captured = {}

    def fake_send(payload, api_key, base_url):
        captured.update(payload)
        return {"content": "ok"}

    monkeypatch.setattr(client, "_send_request", fake_send)
    token = current_user_api_config.set({
        "api_key": "user-key",
        "model": "deepseek-v4-flash",
        "thinking": {"type": "disabled"},
    })
    try:
        client.chat(
            [Message(role=MessageRole.USER, content="ping")],
            use_cache=False,
        )
    finally:
        current_user_api_config.reset(token)

    assert captured["model"] == "deepseek-v4-flash"
    assert captured["thinking"] == {"type": "disabled"}
