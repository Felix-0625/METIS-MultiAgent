"""Version manager for WebSocket broadcast data version tracking"""

from typing import Dict


class VersionManager:
    """版本号管理器，用于 WebSocket 广播时对比数据版本，防止旧数据覆盖新数据"""

    def __init__(self):
        self._versions: Dict[str, int] = {}

    def next_version(self, project_id: str) -> int:
        self._versions[project_id] = self._versions.get(project_id, 0) + 1
        return self._versions[project_id]

    def current_version(self, project_id: str) -> int:
        return self._versions.get(project_id, 0)
