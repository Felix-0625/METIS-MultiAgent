from agents.quality_agents import check_layer2_logic


def test_sql_todo_status_is_not_an_unfinished_marker():
    result = check_layer2_logic([
        ("backend/src/services/database.js", "status TEXT DEFAULT 'todo',\n"),
    ])

    assert result["issues"] == []
    assert result["score"] == 100


def test_explicit_todo_comment_is_reported():
    result = check_layer2_logic([
        ("backend/src/service.js", "// TODO implement tenant filtering\n"),
    ])

    assert len(result["issues"]) == 1
    assert result["issues"][0]["severity"] == "warning"
