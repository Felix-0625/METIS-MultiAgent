import asyncio
from types import SimpleNamespace

from core import app_state, database, persistence
from core.user_scope import current_user_id


def _isolated_database(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setattr(database, "_sqlite_path", str(tmp_path / "idea.db"))
    monkeypatch.setattr(database, "_pg_pool", None)
    monkeypatch.setattr(database, "_pg_unavailable", False)
    database.init_db()
    app_state._idea_landing_agents.clear()


def test_idea_landing_round_trips_across_restart_and_isolates_users(monkeypatch, tmp_path):
    _isolated_database(monkeypatch, tmp_path)
    token = current_user_id.set("user-a")
    try:
        agent = app_state._get_idea_landing()
        conv = agent.new_conversation(title="持久化记录", tags=["真实需求"])
        agent.user_memory.background = "用户 A 的背景"
        asyncio.run(app_state._persist_idea_landing())
    finally:
        current_user_id.reset(token)

    app_state._idea_landing_agents.clear()
    token = current_user_id.set("user-a")
    try:
        restored = app_state._get_idea_landing()
        assert restored.get_conversation(conv.conv_id).title == "持久化记录"
        assert restored.user_memory.background == "用户 A 的背景"
        assert restored.active_conv_id == conv.conv_id
    finally:
        current_user_id.reset(token)

    token = current_user_id.set("user-b")
    try:
        other = app_state._get_idea_landing()
        assert other.conversations == {}
        assert other.user_memory.background == ""
    finally:
        current_user_id.reset(token)
        app_state._idea_landing_agents.clear()


def test_only_admin_can_claim_legacy_global_idea_state(monkeypatch, tmp_path):
    _isolated_database(monkeypatch, tmp_path)
    legacy = {
        "active_conv_id": "conv-old",
        "conversations": {
            "conv-old": {
                "conv_id": "conv-old",
                "title": "旧记录",
                "tags": [],
                "messages": [],
            }
        },
    }
    persistence.save_idea_landing(legacy)

    from core import auth
    monkeypatch.setattr(auth, "get_user_by_id", lambda user_id: SimpleNamespace(
        user_id=user_id,
        role="admin" if user_id == "admin-id" else "user",
    ))

    token = current_user_id.set("ordinary-id")
    try:
        assert app_state._get_idea_landing().conversations == {}
        assert persistence.load_idea_landing() == legacy
    finally:
        current_user_id.reset(token)

    token = current_user_id.set("admin-id")
    try:
        claimed = app_state._get_idea_landing()
        assert claimed.get_conversation("conv-old").title == "旧记录"
        assert persistence.load_idea_landing() == {}
        assert persistence.load_idea_landing("admin-id")["active_conv_id"] == "conv-old"
    finally:
        current_user_id.reset(token)
        app_state._idea_landing_agents.clear()
