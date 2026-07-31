"""Versioned deterministic templates for critical generated runtime files."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "templates"


def select_runtime_template(technology_stack: Iterable[str]) -> str | None:
    values = {str(item).strip().lower() for item in technology_stack if str(item).strip()}
    node_markers = {"node", "node.js", "nodejs", "express", "react", "javascript", "typescript"}
    python_markers = {"python", "fastapi", "django", "flask"}
    if values & node_markers:
        return "node_react_express"
    if values and values <= python_markers:
        return None
    return None


def load_template_manifest(template_name: str) -> dict[str, Any]:
    root = (TEMPLATE_ROOT / template_name).resolve()
    try:
        root.relative_to(TEMPLATE_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("template path escapes template root") from exc
    manifest_path = root / "template.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if data.get("name") != template_name or int(data.get("version") or 0) < 1:
        raise ValueError("invalid runtime template manifest")
    return data


def render_template_files(
    template_name: str,
    *,
    project_name: str,
    port: int = 3000,
) -> tuple[dict[str, str], dict[str, Any]]:
    manifest = load_template_manifest(template_name)
    root = TEMPLATE_ROOT / template_name
    replacements = {
        "{{PROJECT_NAME}}": project_name.strip() or "generated-application",
        "{{PORT}}": str(int(port)),
    }
    rendered: dict[str, str] = {}
    for relative in manifest.get("files") or []:
        normalized = str(relative).replace("\\", "/")
        path = Path(normalized)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe template file: {relative}")
        content = (root / path).read_text(encoding="utf-8")
        for marker, value in replacements.items():
            content = content.replace(marker, value)
        output_path = normalized[:-4] if normalized.endswith(".tpl") else normalized
        rendered[output_path] = content
    evidence = {
        "template": manifest["name"].replace("_", "-"),
        "template_version": int(manifest["version"]),
    }
    return rendered, evidence


def materialize_missing_template_files(
    workspace: Path,
    files: dict[str, str],
) -> list[str]:
    root = Path(workspace).resolve()
    created: list[str] = []
    for relative, content in files.items():
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"template target escapes workspace: {relative}") from exc
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        created.append(relative)
    return created


def materialize_create_mode_templates(
    workspace: Path,
    entries: Iterable[dict[str, Any]],
    *,
    technology_stack: Iterable[str] = (),
    project_name: str = "generated-application",
    port: int = 3000,
) -> dict[str, dict[str, Any]]:
    """Materialize only missing, create-mode files from a matching template.

    When the caller has no explicit stack (legacy rebuild manifests), require
    the contract paths to describe an unambiguous Node full-stack delivery.
    Existing preserve/patch targets are never passed to the materializer.
    """
    rows = [dict(entry) for entry in entries]
    paths = {str(entry.get("path") or "").replace("\\", "/") for entry in rows}
    stack = tuple(technology_stack)
    template_name = select_runtime_template(stack) if stack else None
    if template_name is None and {
        "package.json",
        "Dockerfile",
    }.issubset(paths) and paths.intersection({
        "backend/package.json",
        "frontend/package.json",
    }):
        template_name = "node_react_express"
    if template_name is None:
        return {}

    rendered, evidence = render_template_files(
        template_name,
        project_name=project_name,
        port=port,
    )
    allowed = {
        str(entry.get("path") or "").replace("\\", "/")
        for entry in rows
        if str(entry.get("mode") or "").casefold() == "create"
    }
    selected = {path: content for path, content in rendered.items() if path in allowed}
    created = set(materialize_missing_template_files(workspace, selected))
    root = Path(workspace).resolve()
    return {
        path: {
            **evidence,
            "template_digest": "sha256:"
            + hashlib.sha256((root / path).read_bytes()).hexdigest(),
        }
        for path in created
    }


__all__ = [
    "load_template_manifest",
    "materialize_missing_template_files",
    "materialize_create_mode_templates",
    "render_template_files",
    "select_runtime_template",
]
