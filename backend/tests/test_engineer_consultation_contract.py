from api import routes_engineer
import asyncio
from types import SimpleNamespace


def test_mentioned_role_prefers_explicit_longest_match():
    roles = ["PM组长", "前端工程师", "高级前端工程师"]
    assert routes_engineer._mentioned_role("请 @高级前端工程师 回答", roles) == "高级前端工程师"
    assert routes_engineer._mentioned_role("没有指定成员", roles) is None


def test_consultation_sessions_are_durable(monkeypatch):
    store = {}
    monkeypatch.setattr(routes_engineer, "kv_get", lambda key, default=None: store.get(key, default))
    monkeypatch.setattr(routes_engineer, "kv_set", lambda key, value: store.__setitem__(key, value))
    sessions = [{"id": "consult-1", "mode": "inquiry", "messages": []}]
    routes_engineer._save_consultation_sessions("project-1", sessions)
    assert routes_engineer._consultation_sessions("project-1") == sessions


def test_send_message_uses_engineer_background_without_ctx_final_plan(monkeypatch):
    sessions = [{
        "id": "consult-1", "mode": "inquiry", "title": "问答",
        "messages": [], "created_at": 1, "updated_at": 1,
    }]
    ctx = SimpleNamespace(name="Demo", description="Demo project", agents={})
    agent = SimpleNamespace(project_background="confirmed plan")
    saved = {}
    monkeypatch.setattr(routes_engineer, "_get_project", lambda _pid: ctx)
    monkeypatch.setattr(routes_engineer, "_get_engineer", lambda _pid: agent)
    monkeypatch.setattr(routes_engineer, "_build_engineer_context", lambda *_args: None)
    monkeypatch.setattr(routes_engineer, "_consultation_sessions", lambda _pid: sessions)
    monkeypatch.setattr(routes_engineer, "_save_consultation_sessions", lambda _pid, value: saved.setdefault("sessions", value))
    monkeypatch.setattr(routes_engineer, "_get_hermes", lambda _pid: SimpleNamespace(chat=lambda _messages: {"content": "回答"}))

    result = asyncio.run(routes_engineer.engineer_send_consultation_message(
        "project-1", "consult-1",
        routes_engineer.EngineerConsultationMessageRequest(message="@PM组长 这是什么？"),
    ))
    assert result["responder"] == "PM组长"
    assert result["session"]["messages"][-1]["content"] == "回答"
    assert saved["sessions"][0]["messages"][0]["content"].startswith("@PM组长")
