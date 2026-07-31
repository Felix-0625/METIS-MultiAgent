"""Workspace path utilities for project management"""

import os
import re
from pathlib import Path


def metis_data_root() -> Path | None:
    """Return the configured durable data root, if one was explicitly set."""
    configured = os.environ.get("METIS_DATA_DIR", "").strip()
    return Path(configured).expanduser() if configured else None


def metis_data_path(relative: str | Path, *, legacy: str | Path) -> Path:
    """Resolve runtime state below one durable root without breaking local paths."""
    root = metis_data_root()
    return root / Path(relative) if root is not None else Path(legacy)


WORKSPACE_ROOT = metis_data_path(
    "projects",
    legacy=Path(__file__).parent.parent / "projects",
)
WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)


def _safe_name(name: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|]', '_', name).strip()
    return safe or "project"


def _project_workspace(name: str, project_id: str) -> Path:
    folder = f"{_safe_name(name)}_{project_id}"
    return WORKSPACE_ROOT / folder
