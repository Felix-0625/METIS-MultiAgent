import asyncio
from types import SimpleNamespace

from api import routes_config
from models.schemas import DefaultApiConfigRequest


def test_user_api_config_update_does_not_overwrite_other_users(monkeypatch):
    saved = dict(routes_config.user_api_configs)
    routes_config.user_api_configs.clear()
    routes_config.user_api_configs.update({
        "user-a": {
            **routes_config.DEFAULT_API_CONFIG,
            "model": "model-a",
            "api_key": "key-a",
        },
        "user-b": {
            **routes_config.DEFAULT_API_CONFIG,
            "model": "model-b",
            "api_key": "key-b",
        },
    })

    async def no_persist():
        return None

    monkeypatch.setattr(routes_config, "_persist_all_async", no_persist)
    try:
        asyncio.run(routes_config.update_default_api_config(
            DefaultApiConfigRequest(model="model-a2"),
            SimpleNamespace(user_id="user-a"),
        ))

        assert routes_config.user_api_configs["user-a"]["model"] == "model-a2"
        assert routes_config.user_api_configs["user-a"]["api_key"] == "key-a"
        assert routes_config.user_api_configs["user-b"]["model"] == "model-b"
        assert routes_config.user_api_configs["user-b"]["api_key"] == "key-b"
    finally:
        routes_config.user_api_configs.clear()
        routes_config.user_api_configs.update(saved)


def test_get_user_api_config_returns_only_current_users_masked_key():
    saved = dict(routes_config.user_api_configs)
    routes_config.user_api_configs.clear()
    routes_config.user_api_configs.update({
        "user-a": {
            **routes_config.DEFAULT_API_CONFIG,
            "model": "model-a",
            "api_key": "key-a",
        },
        "user-b": {
            **routes_config.DEFAULT_API_CONFIG,
            "model": "model-b",
            "api_key": "key-b",
        },
    })
    try:
        response = asyncio.run(routes_config.get_default_api_config(
            SimpleNamespace(user_id="user-a"),
        ))
        assert response["model"] == "model-a"
        assert response["api_key"] == "*****"
        assert "model-b" not in str(response)
        assert "key-a" not in str(response)
        assert "key-b" not in str(response)
    finally:
        routes_config.user_api_configs.clear()
        routes_config.user_api_configs.update(saved)
