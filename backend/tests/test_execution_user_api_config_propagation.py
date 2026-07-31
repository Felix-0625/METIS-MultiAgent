import asyncio
from types import SimpleNamespace

from api import routes_execution
from core.hermes_client import current_user_api_config


def _context(tmp_path, *agent_ids):
    return SimpleNamespace(
        project_id="config-propagation",
        workspace=tmp_path,
        agents={
            agent_id: {
                "id": agent_id,
                "role": "Repair Engineer" if "repair" in agent_id else "Engineer",
            }
            for agent_id in agent_ids
        },
    )


async def _effective_generator_config_in_executor(agent, user_api_config):
    loop = asyncio.get_running_loop()

    def inspect():
        return (
            current_user_api_config.get(),
            agent._hermes._get_effective_config("generator"),
        )

    return await loop.run_in_executor(
        None,
        lambda: routes_execution._run_with_user_api_config(
            user_api_config,
            inspect,
        ),
    )


def test_make_exec_agent_uses_explicit_user_config_inside_executor(
    monkeypatch,
    tmp_path,
):
    agent_id = "engineer-explicit-config"
    ctx = _context(tmp_path, agent_id)
    monkeypatch.setitem(routes_execution.agents_api_config, agent_id, {
        "api_key": "agent-fallback-key",
        "api_base": "https://agent.invalid/v1",
        "model": "agent-fallback-model",
    })
    user_config = {
        "api_key": "user-explicit-key",
        "api_base": "https://user.invalid/v1",
        "model": "user-default-model",
        "generator": {
            "model": "user-generator-model",
            "temperature": 0.23,
        },
    }

    token = current_user_api_config.set(None)
    try:
        agent = routes_execution._make_exec_agent(
            ctx,
            agent_id,
            user_api_config=user_config,
        )
        worker_context, effective = asyncio.run(
            _effective_generator_config_in_executor(agent, user_config)
        )
    finally:
        current_user_api_config.reset(token)

    assert worker_context["api_key"] == "user-explicit-key"
    assert effective["api_key"] == "user-explicit-key"
    assert effective["api_base"] == "https://user.invalid/v1"
    assert effective["model"] == "user-generator-model"
    assert effective["temperature"] == 0.23


def test_explicit_user_configs_remain_isolated_between_executor_workers(
    monkeypatch,
    tmp_path,
):
    agent_ids = ("engineer-user-a", "repair-user-b")
    ctx = _context(tmp_path, *agent_ids)
    for agent_id in agent_ids:
        monkeypatch.setitem(routes_execution.agents_api_config, agent_id, {
            "api_key": "shared-agent-fallback-key",
            "model": "shared-agent-fallback-model",
        })

    user_a_config = {
        "api_key": "isolated-user-a-key",
        "api_base": "https://user-a.invalid/v1",
        "generator": {"model": "isolated-user-a-generator"},
    }
    user_b_config = {
        "api_key": "isolated-user-b-key",
        "api_base": "https://user-b.invalid/v1",
        "generator": {"model": "isolated-user-b-generator"},
    }
    agent_a = routes_execution._make_exec_agent(
        ctx,
        agent_ids[0],
        user_api_config=user_a_config,
    )
    agent_b = routes_execution._make_exec_agent(
        ctx,
        agent_ids[1],
        user_api_config=user_b_config,
    )

    async def inspect_both():
        return await asyncio.gather(
            _effective_generator_config_in_executor(agent_a, user_a_config),
            _effective_generator_config_in_executor(agent_b, user_b_config),
        )

    token = current_user_api_config.set(None)
    try:
        results = asyncio.run(inspect_both())
    finally:
        current_user_api_config.reset(token)

    assert [
        worker_context["api_key"] for worker_context, _ in results
    ] == ["isolated-user-a-key", "isolated-user-b-key"]
    assert [
        (
            effective["api_key"],
            effective["api_base"],
            effective["model"],
        )
        for _, effective in results
    ] == [
        (
            "isolated-user-a-key",
            "https://user-a.invalid/v1",
            "isolated-user-a-generator",
        ),
        (
            "isolated-user-b-key",
            "https://user-b.invalid/v1",
            "isolated-user-b-generator",
        ),
    ]


def test_durable_worker_resolves_only_the_project_owners_api_config(
    monkeypatch,
):
    owner_config = {
        "api_key": "owner-key",
        "generator": {"model": "owner-generator"},
    }
    monkeypatch.setattr(routes_execution, "user_api_configs", {
        "owner-user": owner_config,
        "other-user": {
            "api_key": "other-key",
            "generator": {"model": "other-generator"},
        },
    })
    monkeypatch.setattr(routes_execution, "DEFAULT_API_CONFIG", {
        "api_key": "default-key",
        "generator": {"model": "default-generator"},
    })

    resolved = routes_execution._project_owner_api_config(
        SimpleNamespace(owner_user_id="owner-user")
    )
    resolved["generator"]["model"] = "mutated-copy"

    assert resolved["api_key"] == "owner-key"
    assert owner_config["generator"]["model"] == "owner-generator"
