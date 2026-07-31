"""Versioned persistence metadata for application records.

The business owners of PM planning and quality control remain responsible for
their rules.  This module only adds stable storage metadata and conservative
normalisation so old snapshots can be restored without being mistaken for new
records.
"""

from __future__ import annotations

import copy
import time
from typing import Any, Dict


PROJECT_SCHEMA_VERSION = 1
PHASE_SCHEMA_VERSION = 1
TASK_SCHEMA_VERSION = 1
QUALITY_RESULT_SCHEMA_VERSION = 1


def _version_mapping(value: Any, schema_version: int) -> Any:
    if not isinstance(value, dict):
        return value
    result = copy.deepcopy(value)
    result.setdefault("schema_version", schema_version)
    result.setdefault("record_version", 1)
    return result


def migrate_project_record(raw: Dict[str, Any], *, now: float | None = None) -> Dict[str, Any]:
    """Upgrade a persisted project record without changing business meaning."""
    if not isinstance(raw, dict):
        raise TypeError("project record must be an object")
    record = copy.deepcopy(raw)
    version = int(record.get("schema_version") or 0)
    if version > PROJECT_SCHEMA_VERSION:
        raise ValueError(
            f"project schema {version} is newer than supported {PROJECT_SCHEMA_VERSION}"
        )
    if version < 1:
        record["schema_version"] = 1
        record.setdefault("record_version", 1)
        record.setdefault("updated_at", record.get("created_at") or now or time.time())
        # Old snapshots are real projects, not fixtures.  Test data must opt in
        # explicitly rather than being inferred from a name such as "test".
        record.setdefault("record_scope", "production")

    record["subprojects"] = [
        _version_mapping(item, TASK_SCHEMA_VERSION)
        for item in (record.get("subprojects") or [])
        if isinstance(item, dict)
    ]
    quality = record.get("qc_results")
    if isinstance(quality, dict):
        record["qc_results"] = {
            str(key): _version_mapping(value, QUALITY_RESULT_SCHEMA_VERSION)
            for key, value in quality.items()
            if isinstance(value, dict)
        }
    return record


def version_phase_manager_record(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Attach storage versions to phase/task data without changing decisions."""
    record = copy.deepcopy(raw)
    record.setdefault("schema_version", PHASE_SCHEMA_VERSION)
    record["record_version"] = int(record.get("record_version") or 0) + 1
    record["updated_at"] = time.time()
    if isinstance(record.get("phases"), list):
        record["phases"] = [
            _version_mapping(phase, PHASE_SCHEMA_VERSION)
            for phase in record["phases"]
            if isinstance(phase, dict)
        ]
    return record


__all__ = [
    "PHASE_SCHEMA_VERSION",
    "PROJECT_SCHEMA_VERSION",
    "QUALITY_RESULT_SCHEMA_VERSION",
    "TASK_SCHEMA_VERSION",
    "migrate_project_record",
    "version_phase_manager_record",
]
