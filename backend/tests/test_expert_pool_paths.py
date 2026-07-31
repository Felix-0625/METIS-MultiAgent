from pathlib import Path

import core.expert_pool as expert_pool
import core.global_agent_pool as global_agent_pool


def test_default_expert_pool_path_is_independent_of_working_directory(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.delenv("METIS_DATA_DIR", raising=False)
    module_path = tmp_path / "backend" / "core" / "expert_pool.py"
    module_path.parent.mkdir(parents=True)
    monkeypatch.setattr(expert_pool, "__file__", str(module_path))
    monkeypatch.chdir(tmp_path / "backend")
    pool = expert_pool.ExpertPool()

    assert pool._data_dir == tmp_path / "backend" / "data"
    assert "backend/backend" not in pool._data_dir.as_posix()


def test_default_global_agent_pool_path_is_container_safe(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("METIS_DATA_DIR", raising=False)
    module_path = tmp_path / "backend" / "core" / "global_agent_pool.py"
    module_path.parent.mkdir(parents=True)
    monkeypatch.setattr(global_agent_pool, "__file__", str(module_path))
    monkeypatch.chdir(tmp_path / "backend")

    pool = global_agent_pool.GlobalAgentPool()

    assert pool.data_dir == tmp_path / "backend" / "data"
    assert "backend/backend" not in pool.data_dir.as_posix()
