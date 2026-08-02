from core.mcp_server import MCPToolHandler, handle_mcp_request


def test_mcp_initialize_negotiates_protocol():
    response = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        MCPToolHandler({}, None, "user-1"),
    )
    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert response["result"]["capabilities"]["tools"]["listChanged"] is False


def test_mcp_tools_list_exposes_callable_tools():
    response = handle_mcp_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        MCPToolHandler({}, None, "user-1"),
    )
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert {"task_execute", "file_operate", "agent_query"}.issubset(names)
