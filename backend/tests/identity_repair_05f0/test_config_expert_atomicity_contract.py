"""First-red contract tests for user config and expert-pool atomicity.

These tests intentionally describe the required post-fix behaviour.  They do
not patch production code and use only temporary storage and synthetic keys.
"""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from api import routes_config
from core import app_state, expert_pool
from core.auth import UserModel
from core.expert_pool import ExpertPool, ExpertProfile
from models.schemas import DefaultApiConfigRequest


def _user(user_id: str = "config-owner") -> UserModel:
    return UserModel(
        user_id=user_id,
        username=user_id,
        email=f"{user_id}@invalid.test",
        password_hash="synthetic-not-a-password",
    )


@pytest.fixture(autouse=True)
def _restore_config_globals():
    original = dict(app_state.user_api_configs)
    app_state.user_api_configs.clear()
    yield
    app_state.user_api_configs.clear()
    app_state.user_api_configs.update(original)


def test_same_user_disjoint_concurrent_updates_must_merge(monkeypatch):
    """C: two PATCH-like updates based on one revision must not lose a field."""
    app_state.user_api_configs["config-owner"] = routes_config._normalize_api_config(
        {"model": "base-model", "api_base": "https://stub.invalid", "api_key": "synthetic"}
    )
    barrier = threading.Barrier(2)
    real_get = routes_config._get_user_api_config

    def simultaneous_read(user_id):
        value = real_get(user_id)
        barrier.wait(timeout=5)
        return value

    async def no_persist():
        return None

    monkeypatch.setattr(routes_config, "_get_user_api_config", simultaneous_read)
    monkeypatch.setattr(routes_config, "_persist_all_async", no_persist)
    errors = []

    def save(request):
        try:
            asyncio.run(routes_config.update_default_api_config(request, _user()))
        except BaseException as exc:  # retain thread failures for the assertion
            errors.append(exc)

    threads = [
        threading.Thread(target=save, args=(DefaultApiConfigRequest(model="model-new"),)),
        threading.Thread(target=save, args=(DefaultApiConfigRequest(max_tokens=7777),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert app_state.user_api_configs["config-owner"]["model"] == "model-new"
    assert app_state.user_api_configs["config-owner"]["max_tokens"] == 7777


def test_config_lost_ack_must_reconcile_memory_with_committed_state(monkeypatch):
    """C lost-ack: a committed snapshot followed by an exception cannot roll RAM back."""
    app_state.user_api_configs["config-owner"] = routes_config._normalize_api_config(
        {"model": "old", "api_base": "https://stub.invalid", "api_key": "synthetic"}
    )
    durable = {}

    async def commit_then_lose_ack():
        durable.update(json.loads(json.dumps(app_state.user_api_configs)))
        raise TimeoutError("synthetic lost acknowledgement")

    monkeypatch.setattr(routes_config, "_persist_all_async", commit_then_lose_ack)
    with pytest.raises(Exception):
        asyncio.run(
            routes_config.update_default_api_config(
                DefaultApiConfigRequest(model="committed"), _user()
            )
        )

    assert durable["config-owner"] == app_state.user_api_configs["config-owner"]


def test_config_before_write_failure_keeps_previous_state(monkeypatch):
    """C before-write: a rejected save must leave the prior revision active."""
    previous = routes_config._normalize_api_config(
        {"model": "old", "api_base": "https://stub.invalid", "api_key": "synthetic"}
    )
    app_state.user_api_configs["config-owner"] = previous

    async def fail_before_write():
        raise OSError("synthetic pre-write failure")

    monkeypatch.setattr(routes_config, "_persist_all_async", fail_before_write)
    with pytest.raises(Exception):
        asyncio.run(
            routes_config.update_default_api_config(
                DefaultApiConfigRequest(model="must-not-appear"), _user()
            )
        )
    assert app_state.user_api_configs["config-owner"] == previous


def _profile(expert_id: str, name: str = "same-name") -> ExpertProfile:
    return ExpertProfile(expert_id=expert_id, name=name, role="tester")


def test_expert_create_during_write_failure_rolls_back_memory(monkeypatch, tmp_path):
    """F during-write: failed atomic replacement must not publish a ghost."""
    pool = ExpertPool(str(tmp_path / "owner-a"))
    candidate = _profile("candidate")
    monkeypatch.setattr(expert_pool.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk")))

    # The current cleanup path also calls os.unlink(..., missing_ok=True), which
    # is invalid for os.unlink on supported Python versions.  Either exception
    # still represents the same failed write; the contract assertion is state.
    with pytest.raises(Exception):
        pool.create_expert(candidate)

    assert pool.get_expert(candidate.expert_id) is None


def test_expert_update_failure_restores_object_not_only_mapping(monkeypatch, tmp_path):
    """F: in-place profile mutation also requires a deep rollback."""
    pool = ExpertPool(str(tmp_path / "owner-a"))
    pool.create_expert(_profile("existing", "before"))
    monkeypatch.setattr(pool, "save", lambda: (_ for _ in ()).throw(OSError("disk")))

    with pytest.raises(OSError):
        pool.update_expert("existing", {"name": "ghost-name"})

    assert pool.get_expert("existing").name == "before"


def test_cross_user_same_name_isolated_and_survives_restart(tmp_path):
    """F compatibility: identical display names are legal across scoped pools."""
    owner_a_dir, owner_b_dir = tmp_path / "a", tmp_path / "b"
    pool_a, pool_b = ExpertPool(str(owner_a_dir)), ExpertPool(str(owner_b_dir))
    pool_a.create_expert(_profile("a-id"))
    pool_b.create_expert(_profile("b-id"))

    restarted_a, restarted_b = ExpertPool(str(owner_a_dir)), ExpertPool(str(owner_b_dir))
    assert restarted_a.get_expert("a-id") is not None
    assert restarted_a.get_expert("b-id") is None
    assert restarted_b.get_expert("b-id") is not None
    assert restarted_b.get_expert("a-id") is None


def test_expert_lost_ack_keeps_memory_equal_to_committed_file(monkeypatch, tmp_path):
    """F lost-ack: once replace committed, RAM and restart view must agree."""
    pool = ExpertPool(str(tmp_path / "owner-a"))
    real_save = pool.save

    def commit_then_lose_ack():
        real_save()
        raise TimeoutError("synthetic lost acknowledgement")

    monkeypatch.setattr(pool, "save", commit_then_lose_ack)
    with pytest.raises(TimeoutError):
        pool.create_expert(_profile("committed-id"))

    assert pool.get_expert("committed-id") is not None
    assert ExpertPool(str(tmp_path / "owner-a")).get_expert("committed-id") is not None


def test_legacy_expert_schema_load_does_not_cross_owner_boundary(tmp_path):
    """F old schema: a legacy pool file is consumed only by its selected owner path."""
    legacy_dir, other_dir = tmp_path / "legacy-owner", tmp_path / "other-owner"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "expert_memories").mkdir()
    (legacy_dir / "expert_pool.json").write_text(
        json.dumps({"experts": [_profile("legacy-id").to_dict()]}), encoding="utf-8"
    )

    assert ExpertPool(str(legacy_dir)).get_expert("legacy-id") is not None
    assert ExpertPool(str(other_dir)).get_expert("legacy-id") is None
