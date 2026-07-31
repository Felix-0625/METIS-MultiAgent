"""Global pytest safety barriers.

The application imports its SQLite path at module load time.  Set an isolated
session directory before test collection imports any application module so a
local test run can never overwrite a developer or production database.
"""

import os
import shutil
import tempfile
from pathlib import Path


_TEST_STATE_ROOT = Path(tempfile.mkdtemp(prefix="metis-pytest-")).resolve()
os.environ["DATABASE_URL"] = ""
os.environ["REQUIRE_DATABASE_URL"] = "false"
os.environ["SQLITE_PATH"] = str(_TEST_STATE_ROOT / "dagent-test.db")
os.environ["METIS_DATA_DIR"] = str(_TEST_STATE_ROOT / "data")
os.environ["METIS_WORKSPACE_ROOT"] = str(_TEST_STATE_ROOT / "projects")


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TEST_STATE_ROOT, ignore_errors=True)


# These are executable online smoke scripts, not pytest modules.  They perform
# requests at import time and are exercised with an authenticated browser-like
# session by test_integration_scripts.py.
collect_ignore = [
    "test_viz_project_flow.py",
]
