from pathlib import Path
from types import SimpleNamespace

import pytest

from core.gitee_sync import GiteeSync
from api import routes_gitee


def test_validate_remote_accepts_supported_https_hosts():
    assert GiteeSync.validate_remote("github", "https://github.com/acme/repo.git") == (
        "github",
        "https://github.com/acme/repo.git",
    )
    assert GiteeSync.validate_remote("gitee", "https://gitee.com/acme/repo.git")[0] == "gitee"


def test_default_data_directory_uses_persistent_runtime_path(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("METIS_DATA_DIR", str(tmp_path))
    assert GiteeSync().data_dir == tmp_path


@pytest.mark.parametrize(
    ("provider", "url"),
    [
        ("github", "https://gitee.com/acme/repo.git"),
        ("gitee", "https://github.com/acme/repo.git"),
        ("github", "http://github.com/acme/repo.git"),
        ("github", "https://token@github.com/acme/repo.git"),
        ("unknown", "https://example.com/acme/repo.git"),
    ],
)
def test_validate_remote_rejects_wrong_provider_or_credential_url(provider, url):
    with pytest.raises(ValueError):
        GiteeSync.validate_remote(provider, url)


def test_askpass_reads_token_from_environment_without_writing_it(monkeypatch, tmp_path: Path):
    captured = {}

    class Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        captured["helper_content"] = Path(kwargs["env"]["GIT_ASKPASS"]).read_text(encoding="utf-8")
        return Result()

    monkeypatch.setattr("core.gitee_sync.subprocess.run", fake_run)
    manager = GiteeSync(tmp_path)
    secret = "temporary-test-token"
    ok, _ = manager._run_git("ls-remote", "origin", auth_token=secret)

    assert ok is True
    assert secret not in captured["helper_content"]
    assert captured["env"]["METIS_GIT_TOKEN"] == secret
    assert secret not in " ".join(captured["args"])


def test_set_remote_verifies_connection_before_success(monkeypatch, tmp_path: Path):
    manager = GiteeSync(tmp_path)
    calls = []

    monkeypatch.setattr(manager, "is_git_repo", lambda: True)

    def fake_git(*args, **kwargs):
        calls.append((args, kwargs))
        if args == ("remote",):
            return True, ""
        return True, ""

    monkeypatch.setattr(manager, "_run_git", fake_git)
    result = manager.set_remote("https://github.com/acme/repo.git", "test-token", "github")

    assert result["success"] is True
    assert any(args[:2] == ("ls-remote", "--heads") and kwargs["auth_token"] == "test-token" for args, kwargs in calls)


def test_project_bindings_are_stored_independently(monkeypatch):
    persisted = {}

    monkeypatch.setattr(routes_gitee, "load_gitee_config", lambda: persisted)

    def save(value):
        persisted.clear()
        persisted.update(value)

    monkeypatch.setattr(routes_gitee, "save_gitee_config", save)
    routes_gitee._save_binding("project-a", {"repo_url": "https://github.com/a/one.git", "token": "a"})
    routes_gitee._save_binding("project-b", {"repo_url": "https://gitee.com/b/two.git", "token": "b"})

    assert routes_gitee._binding("project-a")["token"] == "a"
    assert routes_gitee._binding("project-b")["token"] == "b"
    assert persisted["schema_version"] == 2


def test_project_access_rejects_another_users_repository(monkeypatch, tmp_path: Path):
    project = SimpleNamespace(owner_user_id="owner", workspace=tmp_path)
    monkeypatch.setattr(routes_gitee, "_get_project", lambda _project_id: project)

    with pytest.raises(Exception) as exc_info:
        routes_gitee._project_for_user(
            "project-a",
            SimpleNamespace(user_id="other-user", role="user"),
        )

    assert getattr(exc_info.value, "status_code", None) == 403


def test_project_sync_manager_uses_only_selected_project_workspace(tmp_path: Path):
    workspace = tmp_path / "project-a"
    project = SimpleNamespace(workspace=workspace)

    manager = routes_gitee._sync_for(project)

    assert manager.data_dir == workspace
