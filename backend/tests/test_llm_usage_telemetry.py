from core import llm_usage


def test_usage_is_durable_and_aggregated_per_project(monkeypatch):
    store = {}
    monkeypatch.setattr(llm_usage, "kv_get", lambda key, default=None: store.get(key, default))
    monkeypatch.setattr(llm_usage, "kv_set", lambda key, value: store.__setitem__(key, value))

    llm_usage.record_llm_usage(
        user_id="user-1", project_id="project-a", model="model-x",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    llm_usage.record_llm_usage(
        user_id="user-1", project_id="project-a", model="model-x",
        cache_lookup=True, cache_hit=True,
    )
    llm_usage.record_llm_usage(
        user_id="user-1", project_id="project-b", model="model-y",
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    )

    data = llm_usage.get_user_llm_usage("user-1")
    assert data["projects"]["project-a"]["total_tokens"] == 15
    assert data["projects"]["project-a"]["requests"] == 1
    assert data["projects"]["project-a"]["cache_hits"] == 1
    assert data["projects"]["project-a"]["cache_lookups"] == 1
    assert data["projects"]["project-b"]["total_tokens"] == 10
    assert sum(item["tokens"] for item in data["projects"]["project-a"]["series"]) == 15


def test_archiving_project_retains_tokens_and_drops_runtime_metrics(monkeypatch):
    store = {}
    monkeypatch.setattr(llm_usage, "kv_get", lambda key, default=None: store.get(key, default))
    monkeypatch.setattr(llm_usage, "kv_set", lambda key, value: store.__setitem__(key, value))

    llm_usage.record_llm_usage(
        user_id="user-1", project_id="project-a", model="model-x",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    llm_usage.record_llm_usage(
        user_id="user-1", project_id="project-a", model="model-x",
        cache_lookup=True, cache_hit=True,
    )
    llm_usage.archive_project_llm_usage(
        user_id="user-1", project_id="project-a", project_name="Archived project",
    )

    project = llm_usage.get_user_llm_usage("user-1")["projects"]["project-a"]
    assert project["total_tokens"] == 15
    assert project["project_name"] == "Archived project"
    assert project["deleted"] is True
    assert set(project) == {"project_name", "deleted", "total_tokens"}
    assert "cache_hits" not in project
    assert "series" not in project
