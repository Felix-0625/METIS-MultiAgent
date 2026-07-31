import pytest
from starlette.middleware.cors import CORSMiddleware

from core import app_state, database, persistence


def test_application_state_persists_adjustments():
    database.init_db()
    payload = {"proj-1": [{"adjustment_id": "adj-1", "status": "queued"}]}

    persistence.save_application_state({"adjustments": payload})

    assert database.kv_get("adjustments") == payload


def test_broken_registry_snapshot_fails_instead_of_erasing_state(monkeypatch):
    class BrokenLeader:
        def to_persist(self):
            raise TypeError("not serializable")

    monkeypatch.setattr(app_state, "_pm_teams", {"proj-broken": BrokenLeader()})
    monkeypatch.setattr(app_state, "_phase_managers", {})
    monkeypatch.setattr(app_state, "_supervisor_leaders", {})
    monkeypatch.setattr(app_state, "projects", {"proj-broken": object()})

    with pytest.raises(RuntimeError, match="PM team state"):
        app_state._do_persist_unlocked()


def test_cors_allows_idempotency_key():
    cors = next(
        middleware
        for middleware in app_state.app.user_middleware
        if middleware.cls is CORSMiddleware
    )
    allowed = {
        value.casefold()
        for value in cors.kwargs["allow_headers"]
    }
    assert "idempotency-key" in allowed
