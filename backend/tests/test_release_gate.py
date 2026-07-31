import asyncio
import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api import routes_files, routes_supervisor
from core.workspace_integrity import (
    build_delivery_manifest_from_files,
    compute_delivery_manifest,
    compute_workspace_digest,
    is_sensitive_archive_path,
    release_gate_error,
    workspace_persistence_issues,
)


def _approved_qc(workspace):
    manifest = compute_delivery_manifest(workspace)
    return {
        "__whole_project__": {
            "qa": {
                "passed": True,
                "status": "passed",
                "workspace_digest": compute_workspace_digest(workspace),
                "delivery_manifest": manifest,
                "runtime_acceptance": {
                    "enabled": True,
                    "passed": True,
                    "status": "passed",
                    "artifact_sha256": manifest["artifact_sha256"],
                    "artifact_manifest_rule_version": manifest["rule_version"],
                },
            }
        }
    }


def _mock_signoff_documents(monkeypatch):
    monkeypatch.setattr(
        routes_supervisor,
        "load_final_qa_scope",
        lambda **_kwargs: {
            "available": True,
            "issues": [],
            "files": [],
        },
    )
    monkeypatch.setattr(
        routes_supervisor,
        "_signoff_adjustments",
        lambda _project_id: [],
    )


def test_release_gate_rejects_stale_workspace(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("print('approved')\n", encoding="utf-8")
    qc_results = _approved_qc(tmp_path)
    source.write_text("raise RuntimeError('changed')\n", encoding="utf-8")

    error = release_gate_error(qc_results, tmp_path)

    assert error == "Project files changed after Final QA; run Final QA again"


def test_delivery_manifest_excludes_nested_build_outputs(tmp_path):
    source = tmp_path / "frontend" / "src" / "main.tsx"
    bundle = tmp_path / "frontend" / "dist" / "assets" / "app.js"
    source.parent.mkdir(parents=True)
    bundle.parent.mkdir(parents=True)
    source.write_text("export const app = true;\n", encoding="utf-8")
    bundle.write_text("first build\n", encoding="utf-8")

    before = compute_delivery_manifest(tmp_path)
    bundle.write_text("second build\n", encoding="utf-8")
    after = compute_delivery_manifest(tmp_path)

    assert before["rule_version"] == "delivery-manifest-v2"
    assert before["artifact_sha256"] == after["artifact_sha256"]
    assert "frontend/dist/assets/app.js" not in {
        entry["path"] for entry in after["files"]
    }


def test_release_gate_requires_runtime_acceptance(tmp_path):
    (tmp_path / "app.py").write_text("print('approved')\n", encoding="utf-8")
    qc_results = _approved_qc(tmp_path)
    qc_results["__whole_project__"]["qa"]["runtime_acceptance"]["passed"] = False

    assert release_gate_error(qc_results, tmp_path) == (
        "Runtime acceptance has not passed for the current project files"
    )


def test_release_gate_rejects_unrelated_runtime_artifact(tmp_path):
    (tmp_path / "app.py").write_text("print('approved')\n", encoding="utf-8")
    qc_results = _approved_qc(tmp_path)
    qc_results["__whole_project__"]["qa"]["runtime_acceptance"][
        "artifact_sha256"
    ] = "unrelated-artifact"

    assert release_gate_error(qc_results, tmp_path) == (
        "Runtime acceptance has not passed for the current project files"
    )


def test_sensitive_runtime_file_does_not_change_delivery_artifact(tmp_path):
    (tmp_path / "app.py").write_text("print('approved')\n", encoding="utf-8")
    qc_results = _approved_qc(tmp_path)
    (tmp_path / ".env").write_text("TOKEN=rotated\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "runtime.json").write_text('{"state": 2}', encoding="utf-8")

    assert release_gate_error(qc_results, tmp_path) is None


def test_manifest_keeps_nested_data_and_explicit_required_build_output(tmp_path):
    nested = tmp_path / "src" / "data" / "countries.json"
    nested.parent.mkdir(parents=True)
    nested.write_text('["CN"]', encoding="utf-8")
    required_build = tmp_path / "build" / "release.js"
    required_build.parent.mkdir()
    required_build.write_text("export const ready = true;\n", encoding="utf-8")
    runtime_data = tmp_path / "data"
    runtime_data.mkdir()
    (runtime_data / "state.db").write_bytes(b"runtime")

    manifest = compute_delivery_manifest(
        tmp_path, required_paths=["build/release.js"]
    )
    paths = {item["path"] for item in manifest["files"]}

    assert "src/data/countries.json" in paths
    assert "build/release.js" in paths
    assert "data/state.db" not in paths


def test_manifest_from_bytes_requires_safe_required_entry_subset():
    manifest = build_delivery_manifest_from_files(
        {"app.py": b"print('ok')\n"},
        required_paths=["app.py"],
    )
    assert manifest["files"][0]["sha256"]

    with pytest.raises(ValueError, match="missing"):
        build_delivery_manifest_from_files(
            {"app.py": b"print('ok')\n"},
            required_paths=["missing.py"],
        )
    with pytest.raises(ValueError, match="unsafe"):
        build_delivery_manifest_from_files(
            {"app.py": b"print('ok')\n"},
            required_paths=["../outside.py"],
        )


def test_workspace_digest_uses_same_root_aware_sensitive_exclusions(tmp_path):
    (tmp_path / "app.py").write_text("approved\n", encoding="utf-8")
    before = compute_workspace_digest(tmp_path)
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    nested = tmp_path / "src" / "data"
    nested.mkdir(parents=True)
    (nested / "countries.json").write_text("[]\n", encoding="utf-8")

    assert compute_workspace_digest(tmp_path) != before
    (tmp_path / "src" / "data" / "countries.json").unlink()
    assert compute_workspace_digest(tmp_path) == before


def test_release_gate_blocks_files_that_render_snapshot_would_drop(tmp_path):
    oversized = tmp_path / "src" / "generated.bin"
    oversized.parent.mkdir()
    oversized.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    qc_results = _approved_qc(tmp_path)

    issues = workspace_persistence_issues(tmp_path)

    assert issues == [
        "src/generated.bin: file exceeds the durable snapshot limit"
    ]
    assert release_gate_error(qc_results, tmp_path) == (
        "Project cannot be durably persisted: "
        "src/generated.bin: file exceeds the durable snapshot limit"
    )


@pytest.mark.parametrize(
    "path",
    [".env", ".env.production", "private.pem", "state.sqlite3", "output/run.log"],
)
def test_sensitive_archive_paths_are_excluded(path):
    from pathlib import Path

    assert is_sensitive_archive_path(Path(path)) is True


def test_archive_download_is_blocked_without_current_release_evidence(monkeypatch, tmp_path):
    project = SimpleNamespace(
        workspace=tmp_path,
        qc_results={},
        name="blocked",
    )
    monkeypatch.setattr(routes_files, "_get_project", lambda _project_id: project)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_files.download_project_archive("proj-test"))

    assert exc.value.status_code == 409


def test_archive_excludes_secrets_and_includes_integrity_manifest(monkeypatch, tmp_path):
    (tmp_path / "app.py").write_text("print('approved')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=do-not-package\n", encoding="utf-8")
    (tmp_path / "state.sqlite3").write_bytes(b"database")
    project = SimpleNamespace(
        workspace=tmp_path,
        qc_results=_approved_qc(tmp_path),
        name="approved",
    )
    monkeypatch.setattr(routes_files, "_get_project", lambda _project_id: project)

    async def download():
        response = await routes_files.download_project_archive("proj-test")
        return b"".join([chunk async for chunk in response.body_iterator])

    archive_bytes = asyncio.run(download())
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("archive_manifest.json"))

    assert "app.py" in names
    assert ".env" not in names
    assert "state.sqlite3" not in names
    expected = compute_delivery_manifest(tmp_path)
    assert manifest["artifact_sha256"] == expected["artifact_sha256"]
    assert manifest["artifact_manifest_rule_version"] == expected["rule_version"]


def test_signoff_is_blocked_when_workspace_no_longer_matches_qa(monkeypatch, tmp_path):
    _mock_signoff_documents(monkeypatch)
    source = tmp_path / "app.py"
    source.write_text("print('approved')\n", encoding="utf-8")
    qc_results = _approved_qc(tmp_path)
    source.write_text("raise RuntimeError('changed')\n", encoding="utf-8")
    project = SimpleNamespace(
        workspace=tmp_path,
        qc_results=qc_results,
        supervisor=SimpleNamespace(sign_off=lambda _project_id: {"success": True}),
        status="active",
    )
    monkeypatch.setattr(routes_supervisor, "_get_project", lambda _project_id: project)

    response = asyncio.run(routes_supervisor.sign_off("proj-test"))
    assert response.status_code == 409
    assert project.status == "active"


def test_signoff_is_blocked_when_phase_state_regresses_after_final_qa(
    monkeypatch, tmp_path
):
    _mock_signoff_documents(monkeypatch)
    (tmp_path / "app.py").write_text("print('approved')\n", encoding="utf-8")
    project = SimpleNamespace(
        workspace=tmp_path,
        qc_results=_approved_qc(tmp_path),
        supervisor=SimpleNamespace(
            sign_off=lambda _project_id: {"success": True}
        ),
        supervisor_quality_runs={
            "phase-1": {
                "status": "completed",
                "completion_gate": {"passed": True},
            }
        },
        agents={
            "agent-1": {"status": "completed", "progress": 100}
        },
        subprojects=[{
            "id": "sp-1",
            "agent_id": "agent-1",
            "status": "completed",
            "progress": 100,
        }],
        status="active",
    )
    phase_manager = SimpleNamespace(phases=[{
        "phase_id": "phase-1",
        "status": "needs_rework",
        "user_confirmed": True,
    }])
    monkeypatch.setattr(routes_supervisor, "_get_project", lambda _project_id: project)
    monkeypatch.setitem(
        routes_supervisor._phase_managers, "proj-test", phase_manager
    )

    response = asyncio.run(routes_supervisor.sign_off("proj-test"))
    assert response.status_code == 409
    payload = json.loads(response.body)
    assert "Phase phase-1" in payload["blockers"][0]["message"]
    assert project.status == "active"


def test_signoff_is_blocked_when_supervisor_gate_regresses_after_final_qa(
    monkeypatch, tmp_path
):
    _mock_signoff_documents(monkeypatch)
    (tmp_path / "app.py").write_text("print('approved')\n", encoding="utf-8")
    project = SimpleNamespace(
        workspace=tmp_path,
        qc_results=_approved_qc(tmp_path),
        supervisor=SimpleNamespace(
            sign_off=lambda _project_id: {"success": True}
        ),
        supervisor_quality_runs={
            "phase-1": {
                "status": "needs_rework",
                "completion_gate": {"passed": False},
            }
        },
        agents={
            "agent-1": {"status": "completed", "progress": 100}
        },
        subprojects=[],
        status="active",
    )
    phase_manager = SimpleNamespace(phases=[{
        "phase_id": "phase-1",
        "status": "completed",
        "user_confirmed": True,
    }])
    monkeypatch.setattr(routes_supervisor, "_get_project", lambda _project_id: project)
    monkeypatch.setitem(
        routes_supervisor._phase_managers, "proj-test", phase_manager
    )

    response = asyncio.run(routes_supervisor.sign_off("proj-test"))
    assert response.status_code == 409
    payload = json.loads(response.body)
    assert "Supervisor quality state" in payload["blockers"][0]["message"]
    assert project.status == "active"


def test_signoff_blocks_unassigned_delivery_subproject_after_final_qa(
    monkeypatch, tmp_path
):
    _mock_signoff_documents(monkeypatch)
    (tmp_path / "app.py").write_text("print('approved')\n", encoding="utf-8")
    project = SimpleNamespace(
        workspace=tmp_path,
        qc_results=_approved_qc(tmp_path),
        supervisor=SimpleNamespace(sign_off=lambda _project_id: {"success": True}),
        supervisor_quality_runs={
            "phase-1": {
                "status": "completed",
                "completion_gate": {"passed": True},
            }
        },
        agents={},
        subprojects=[
            {
                "id": "phase-1", "phase_id": "phase-1",
                "status": "pending", "progress": 0,
            },
            {
                "id": "task-orphan", "phase_id": "phase-1",
                "agent_role": "backend",
                "status": "pending", "progress": 0,
            },
        ],
        status="active",
    )
    phase_manager = SimpleNamespace(phases=[{
        "phase_id": "phase-1",
        "status": "completed",
        "user_confirmed": True,
    }])
    monkeypatch.setattr(routes_supervisor, "_get_project", lambda _project_id: project)
    monkeypatch.setitem(
        routes_supervisor._phase_managers, "proj-test", phase_manager
    )

    response = asyncio.run(routes_supervisor.sign_off("proj-test"))
    assert response.status_code == 409
    payload = json.loads(response.body)
    assert "Subproject task-orphan" in payload["blockers"][0]["message"]
    assert project.status == "active"
