import os
import subprocess
import sys
from pathlib import Path


def test_sqlite_default_follows_metis_data_dir(tmp_path: Path):
    env = os.environ.copy()
    env.pop("SQLITE_PATH", None)
    env["METIS_DATA_DIR"] = str(tmp_path)
    backend_dir = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [sys.executable, "-c", "import core.database; print(core.database._sqlite_path)"],
        cwd=backend_dir,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert Path(result.stdout.strip()) == tmp_path / "dagent.db"
