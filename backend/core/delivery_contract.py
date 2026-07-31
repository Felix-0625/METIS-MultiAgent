"""Extract explicit required delivery files from user and PM requirements."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any


_KNOWN_ROOT_FILES = {
    ".gitignore": ".gitignore",
    "package.json": "package.json",
    "readme.md": "README.md",
    ".env.example": ".env.example",
    ".dockerignore": ".dockerignore",
    "dockerfile": "Dockerfile",
    "docker-compose.yml": "docker-compose.yml",
    "docker-compose.yaml": "docker-compose.yaml",
    "requirements.txt": "requirements.txt",
    "pyproject.toml": "pyproject.toml",
}
_FILE_EXTENSIONS = {
    ".css", ".html", ".ini", ".js", ".json", ".jsx", ".md", ".py",
    ".scss", ".sql", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
}
_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"
)
_DECLARED_FILE_FIELDS = {
    "file", "files", "path", "paths", "required_file", "required_files",
}


def _canonicalize_conventional_filename(path: str) -> str:
    candidate = PurePosixPath(path)
    canonical_name = _KNOWN_ROOT_FILES.get(candidate.name.casefold())
    if not canonical_name:
        return str(candidate)
    parent = str(candidate.parent)
    return canonical_name if parent == "." else f"{parent}/{canonical_name}"


def _normalize_declared_file_path(path: Any) -> str:
    """Normalize an explicit file contract without guessing from prose."""
    normalized = str(path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if (
        not normalized
        or "\x00" in normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or ".." in PurePosixPath(normalized).parts
    ):
        return ""
    return _canonicalize_conventional_filename(str(PurePosixPath(normalized)))


def _collect_plan_text(value: Any) -> list[str]:
    """Collect plan strings without JSON escaping newlines into fake paths."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        values: list[str] = []
        for item in value.values():
            values.extend(_collect_plan_text(item))
        return values
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            values.extend(_collect_plan_text(item))
        return values
    return []


def _collect_declared_file_values(value: Any, *, declared: bool = False) -> list[str]:
    if isinstance(value, str):
        return [value] if declared else []
    if isinstance(value, dict):
        if declared and value.get("path"):
            return [str(value["path"])]
        values: list[str] = []
        for key, item in value.items():
            values.extend(_collect_declared_file_values(
                item,
                declared=declared or str(key).casefold() in _DECLARED_FILE_FIELDS,
            ))
        return values
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            values.extend(_collect_declared_file_values(item, declared=declared))
        return values
    return []


def is_delivery_file_path(path: str) -> bool:
    """Return False for runner-owned metadata that agents must never generate."""
    normalized = str(path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    lowered = normalized.lower()
    name = PurePosixPath(lowered).name
    return bool(normalized) and not (
        lowered.startswith(("output/", ".project/"))
        or name.endswith("_execution.log")
        or name in {"commit.json", "execution.log"}
    )


def collect_required_file_paths(requirements: str, plan: Any = None) -> list[str]:
    text = requirements or ""
    if plan:
        text += "\n" + "\n".join(_collect_plan_text(plan))

    lower_text = text.lower()
    required = {
        canonical
        for lowered, canonical in _KNOWN_ROOT_FILES.items()
        if re.search(rf"(?<![\w./-]){re.escape(lowered)}(?![\w/-])", lower_text)
    }
    for match in _PATH_PATTERN.findall(text.replace("\\", "/")):
        normalized = str(PurePosixPath(match))
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = _canonicalize_conventional_filename(normalized)
        suffix = PurePosixPath(normalized).suffix.lower()
        if suffix in _FILE_EXTENSIONS or PurePosixPath(normalized).name.lower() in _KNOWN_ROOT_FILES:
            if is_delivery_file_path(normalized):
                required.add(normalized)
    for declared in _collect_declared_file_values(plan):
        normalized = _normalize_declared_file_path(declared)
        if normalized and is_delivery_file_path(normalized):
            required.add(normalized)
    return sorted(required, key=str.lower)


def required_files_for_scopes(paths: list[str], scopes: list[str]) -> list[str]:
    paths = [path for path in paths if is_delivery_file_path(path)]
    if not scopes:
        return list(paths)
    normalized_scopes = [scope.replace("\\", "/").rstrip("/") for scope in scopes]
    return [
        path
        for path in paths
        if any(path == scope or path.startswith(scope + "/") for scope in normalized_scopes)
    ]
