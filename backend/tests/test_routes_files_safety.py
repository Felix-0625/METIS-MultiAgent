"""Adversarial coverage for the project workspace file routes."""

from __future__ import annotations

import asyncio
import io
import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, UploadFile

from api import routes_files
from core.workspace_integrity import compute_delivery_manifest
from models.schemas import (
    FileCommitRequest,
    FileRollbackRequest,
    FileStagingRequest,
    FileWriteRequest,
)


async def _no_persist() -> None:
    return None


def _project(workspace: Path, *, project_id: str = "files-safety") -> SimpleNamespace:
    return SimpleNamespace(
        project_id=project_id,
        workspace=workspace,
        name="File safety",
        status="running",
        qc_results={},
    )


def _install_project(monkeypatch: pytest.MonkeyPatch, project: Any) -> None:
    monkeypatch.setattr(routes_files, "_get_project", lambda _project_id: project)
    monkeypatch.setattr(routes_files, "_persist_all_async", _no_persist)
    monkeypatch.setattr(routes_files, "_required_delivery_paths", lambda _ctx: [])
    monkeypatch.setattr(routes_files, "_phase_managers", {})


def _flatten_keys(tree: list[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for item in tree:
        keys.append(str(item.get("key") or ""))
        keys.extend(_flatten_keys(item.get("children") or []))
    return keys


@pytest.mark.parametrize(
    "guessed_path",
    [
        ".project/write_fence.json",
        ".project/backups/state.json",
        ".project/transactions/recovery.json",
        ".env",
        "data/state.db",
        "temp/request.txt",
    ],
)
@pytest.mark.parametrize("route_name", ["read_project_file", "download_project_file"])
def test_read_and_download_reject_guessed_internal_runtime_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    guessed_path: str,
    route_name: str,
) -> None:
    target = tmp_path / guessed_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("must-not-leak", encoding="utf-8")
    project = _project(tmp_path)
    _install_project(monkeypatch, project)

    route = getattr(routes_files, route_name)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(route(project.project_id, guessed_path))

    assert exc_info.value.status_code == 403
    assert "must-not-leak" not in str(exc_info.value.detail)


def test_list_and_read_never_follow_directory_symlinks_or_expose_host_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside-secret", encoding="utf-8")
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        # Windows CI may not grant symlink privileges. Keep the policy test
        # deterministic by making this exact directory report as a symlink.
        link.mkdir()
        (link / "secret.txt").write_text("simulated-link-secret", encoding="utf-8")
        original_is_symlink = Path.is_symlink
        link_absolute = link.absolute()

        def simulated_is_symlink(path: Path) -> bool:
            if path.absolute() == link_absolute:
                return True
            return original_is_symlink(path)

        monkeypatch.setattr(Path, "is_symlink", simulated_is_symlink)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "visible.txt").write_text("visible", encoding="utf-8")
    project = _project(tmp_path)
    _install_project(monkeypatch, project)

    result = asyncio.run(routes_files.list_project_files(project.project_id))

    assert result["workspace"] == "."
    assert result["workspace_rel"] == "."
    keys = _flatten_keys(result["tree"])
    assert "src" in keys
    assert "src/visible.txt" in keys
    assert all(not key.startswith("linked") for key in keys)
    serialized = json.dumps(result, ensure_ascii=False)
    assert str(tmp_path.resolve()) not in serialized
    assert str(outside.resolve()) not in serialized

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            routes_files.read_project_file(
                project.project_id,
                "linked/secret.txt",
            )
        )
    assert exc_info.value.status_code == 403


def test_atomic_write_replace_failure_preserves_previous_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_bytes(b"old-bytes")
    project = _project(tmp_path)
    _install_project(monkeypatch, project)

    def fail_replace(_source: os.PathLike[str], _target: os.PathLike[str]) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(routes_files.os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        asyncio.run(
            routes_files.write_project_file(
                project.project_id,
                FileWriteRequest(path="src/app.py", content="new-bytes"),
            )
        )

    assert target.read_bytes() == b"old-bytes"
    assert list(target.parent.glob(".app.py.*.tmp")) == []


def test_stage_corrupt_index_fails_closed_without_discarding_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    index = tmp_path / ".project" / "staging_index.json"
    index.parent.mkdir(parents=True)
    index.write_bytes(b"{not-json")
    project = _project(tmp_path)
    _install_project(monkeypatch, project)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            routes_files.stage_file(
                project.project_id,
                FileStagingRequest(path="src/app.py", content="new"),
            )
        )

    assert exc_info.value.status_code == 409
    assert index.read_bytes() == b"{not-json"
    assert not (tmp_path / ".project" / "staging" / "src" / "app.py").exists()


def test_stage_index_replace_failure_restores_old_stage_and_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _install_project(monkeypatch, project)
    request = FileStagingRequest(path="src/app.py", content="first")
    asyncio.run(routes_files.stage_file(project.project_id, request))
    staged = tmp_path / ".project" / "staging" / "src" / "app.py"
    index = tmp_path / ".project" / "staging_index.json"
    old_stage = staged.read_bytes()
    old_index = index.read_bytes()
    real_replace = routes_files.os.replace
    failed = False

    def fail_index_once(source: os.PathLike[str], target: os.PathLike[str]) -> None:
        nonlocal failed
        if Path(target) == index and not failed:
            failed = True
            raise OSError("injected index replace failure")
        real_replace(source, target)

    monkeypatch.setattr(routes_files.os, "replace", fail_index_once)

    with pytest.raises(OSError, match="injected index replace failure"):
        asyncio.run(
            routes_files.stage_file(
                project.project_id,
                FileStagingRequest(path="src/app.py", content="second"),
            )
        )

    assert staged.read_bytes() == old_stage
    assert index.read_bytes() == old_index


def test_stage_and_commit_reject_path_traversal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _install_project(monkeypatch, project)

    with pytest.raises(HTTPException) as stage_exc:
        asyncio.run(
            routes_files.stage_file(
                project.project_id,
                FileStagingRequest(path="../../outside.txt", content="escape"),
            )
        )
    assert stage_exc.value.status_code == 403

    index = tmp_path / ".project" / "staging_index.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(
        json.dumps({"../../outside.txt": {"size": 6}}),
        encoding="utf-8",
    )
    with pytest.raises(HTTPException) as commit_exc:
        asyncio.run(
            routes_files.commit_staged(
                project.project_id,
                FileCommitRequest(message="malicious index"),
            )
        )
    assert commit_exc.value.status_code in {403, 409}
    assert not (tmp_path.parent / "outside.txt").exists()


def _create_full_snapshot_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    required_paths: list[str],
) -> tuple[SimpleNamespace, dict[str, Any]]:
    project = _project(tmp_path)
    _install_project(monkeypatch, project)
    monkeypatch.setattr(
        routes_files,
        "_required_delivery_paths",
        lambda _ctx: required_paths,
    )
    asyncio.run(
        routes_files.stage_file(
            project.project_id,
            FileStagingRequest(path="src/a.txt", content="snapshot-a"),
        )
    )
    asyncio.run(
        routes_files.stage_file(
            project.project_id,
            FileStagingRequest(path="src/b.txt", content="snapshot-b"),
        )
    )
    result = asyncio.run(
        routes_files.commit_staged(
            project.project_id,
            FileCommitRequest(message="full delivery snapshot"),
        )
    )
    assert result["success"] is True
    return project, result


def test_full_snapshot_rollback_uses_manifest_required_paths_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    required = tmp_path / "build" / "release.js"
    required.parent.mkdir()
    required.write_text("required-v1", encoding="utf-8")
    project, commit = _create_full_snapshot_version(
        monkeypatch,
        tmp_path,
        required_paths=["build/release.js"],
    )
    expected = json.loads(
        (
            tmp_path
            / ".project"
            / "versions"
            / str(commit["version"])
            / "commit.json"
        ).read_text(encoding="utf-8")
    )["delivery_manifest"]
    (tmp_path / "src" / "a.txt").write_text("mutated-a", encoding="utf-8")
    required.write_text("required-v2", encoding="utf-8")

    result = asyncio.run(
        routes_files.rollback_file_version(
            project.project_id,
            FileRollbackRequest(version=commit["version"]),
        )
    )

    assert result["success"] is True
    assert compute_delivery_manifest(
        tmp_path,
        required_paths=["build/release.js"],
    )["artifact_sha256"] == expected["artifact_sha256"]
    assert required.read_text(encoding="utf-8") == "required-v1"


def test_rollback_second_replace_failure_compensates_entire_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project, commit = _create_full_snapshot_version(
        monkeypatch,
        tmp_path,
        required_paths=[],
    )
    (tmp_path / "src" / "a.txt").write_text("current-a", encoding="utf-8")
    (tmp_path / "src" / "b.txt").write_text("current-b", encoding="utf-8")
    (tmp_path / "src" / "current-only.txt").write_text(
        "current-only",
        encoding="utf-8",
    )
    before = compute_delivery_manifest(tmp_path)["artifact_sha256"]
    real_replace = routes_files.os.replace
    failed = False
    second_target = (tmp_path / "src" / "b.txt").resolve()

    def fail_second_target_once(
        source: os.PathLike[str],
        target: os.PathLike[str],
    ) -> None:
        nonlocal failed
        if Path(target).resolve() == second_target and not failed:
            failed = True
            raise OSError("injected second file failure")
        real_replace(source, target)

    monkeypatch.setattr(routes_files.os, "replace", fail_second_target_once)

    with pytest.raises(OSError, match="injected second file failure"):
        asyncio.run(
            routes_files.rollback_file_version(
                project.project_id,
                FileRollbackRequest(version=commit["version"]),
            )
        )

    assert compute_delivery_manifest(tmp_path)["artifact_sha256"] == before
    assert (tmp_path / "src" / "a.txt").read_text(encoding="utf-8") == "current-a"
    assert (tmp_path / "src" / "b.txt").read_text(encoding="utf-8") == "current-b"
    assert (tmp_path / "src" / "current-only.txt").exists()


def _passed_final_qa(tmp_path: Path) -> dict[str, Any]:
    manifest = compute_delivery_manifest(tmp_path)
    return {
        "__whole_project__": {
            "qa": {
                "passed": True,
                "status": "passed",
                "delivery_manifest": manifest,
                "artifact_sha256": manifest["artifact_sha256"],
                "runtime_acceptance": {
                    "passed": True,
                    "status": "passed",
                    "artifact_sha256": manifest["artifact_sha256"],
                    "artifact_manifest_rule_version": manifest["rule_version"],
                },
            }
        }
    }


def test_manual_write_revokes_previous_final_qa_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("approved", encoding="utf-8")
    project = _project(tmp_path)
    project.qc_results = _passed_final_qa(tmp_path)
    _install_project(monkeypatch, project)

    asyncio.run(
        routes_files.write_project_file(
            project.project_id,
            FileWriteRequest(path="src/app.py", content="manual-change"),
        )
    )

    qa = project.qc_results["__whole_project__"]["qa"]
    assert qa["passed"] is False
    assert qa["status"] == "stale"
    assert "delivery_manifest" not in qa


def test_manual_commit_revokes_previous_final_qa_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("approved", encoding="utf-8")
    project = _project(tmp_path)
    project.qc_results = _passed_final_qa(tmp_path)
    _install_project(monkeypatch, project)
    asyncio.run(
        routes_files.stage_file(
            project.project_id,
            FileStagingRequest(path="src/app.py", content="committed-change"),
        )
    )

    result = asyncio.run(
        routes_files.commit_staged(
            project.project_id,
            FileCommitRequest(message="manual commit"),
        )
    )

    assert result["success"] is True
    qa = project.qc_results["__whole_project__"]["qa"]
    assert qa["passed"] is False
    assert qa["status"] == "stale"
    assert "delivery_manifest" not in qa


def _docx_bytes(text: str, *, compression: int = zipfile.ZIP_STORED) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
        f"{text}"
        "</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def _upload(filename: str, content: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=filename)


def test_docx_internal_text_over_limit_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _install_project(monkeypatch, project)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            routes_files.upload_file_for_agent(
                project.project_id,
                agent_type="pm",
                files=[_upload("requirements.docx", _docx_bytes("x" * 50_001))],
            )
        )

    assert exc_info.value.status_code == 413
    assert exc_info.value.detail["code"] == "attachment_text_too_large"
    assert exc_info.value.detail["complete"] is False


def test_combined_attachment_tail_is_rejected_instead_of_truncated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _install_project(monkeypatch, project)
    final_constraint = "MUST-PRESERVE-TAIL-CONSTRAINT"

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            routes_files.upload_file_for_agent(
                project.project_id,
                agent_type="pm",
                files=[
                    _upload("outline.txt", b"a" * 25_000),
                    _upload(
                        "tail.txt",
                        ("b" * 25_000 + final_constraint).encode("utf-8"),
                    ),
                ],
            )
        )

    assert exc_info.value.status_code == 413
    assert exc_info.value.detail["code"] == "combined_attachment_text_too_large"
    assert exc_info.value.detail["complete"] is False
    assert "combined_text" not in exc_info.value.detail


def test_docx_zip_bomb_ratio_fails_before_extraction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _install_project(monkeypatch, project)
    bomb = _docx_bytes("z" * 200_000, compression=zipfile.ZIP_DEFLATED)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            routes_files.upload_file_for_agent(
                project.project_id,
                agent_type="pm",
                files=[_upload("bomb.docx", bomb)],
            )
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "attachment_extraction_incomplete"
    assert exc_info.value.detail["complete"] is False
    assert "compression ratio" in exc_info.value.detail["error"].lower()
