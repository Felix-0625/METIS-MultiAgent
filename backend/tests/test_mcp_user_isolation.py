import json
from types import SimpleNamespace

from core.mcp_server import MCPToolHandler


def _project(owner_user_id: str, name: str):
    return SimpleNamespace(
        owner_user_id=owner_user_id,
        name=name,
        status="active",
        agents={},
        subprojects={},
    )


def _payload(result):
    return json.loads(result["content"][0]["text"])


def test_list_projects_only_returns_token_owners_projects():
    handler = MCPToolHandler(
        {
            "owned": _project("user-1", "Owned"),
            "foreign": _project("user-2", "Foreign"),
            "legacy": _project("", "Legacy"),
        },
        None,
        "user-1",
    )

    result = handler.handle_agent_query({"query_type": "list_projects"})

    assert [project["id"] for project in _payload(result)["projects"]] == ["owned"]


def test_direct_access_to_foreign_project_is_rejected():
    handler = MCPToolHandler(
        {"foreign": _project("user-2", "Foreign")},
        None,
        "user-1",
    )

    result = handler.handle_agent_query(
        {"query_type": "get_project", "project_id": "foreign"}
    )

    assert result["isError"] is True
