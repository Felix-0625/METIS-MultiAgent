"""Tests for the offline platform verification entrypoint."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("verify_platform", ROOT / "scripts" / "verify_platform.py")
assert SPEC and SPEC.loader
verify_platform = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify_platform
SPEC.loader.exec_module(verify_platform)

RECOVERY_SPEC = importlib.util.spec_from_file_location(
    "postgres_recovery_drill", ROOT / "scripts" / "postgres_recovery_drill.py"
)
assert RECOVERY_SPEC and RECOVERY_SPEC.loader
postgres_recovery_drill = importlib.util.module_from_spec(RECOVERY_SPEC)
sys.modules[RECOVERY_SPEC.name] = postgres_recovery_drill
RECOVERY_SPEC.loader.exec_module(postgres_recovery_drill)


def test_repository_platform_contract_has_no_failures() -> None:
    findings = verify_platform.run_checks(include_compose_cli=False)

    failures = [finding for finding in findings if finding.level == "FAIL"]
    assert failures == []


def test_deprecated_render_env_is_rejected(monkeypatch) -> None:
    original = verify_platform._load_yaml

    def fake_load(relative, findings):
        if relative == "render.yaml":
            return {
                "services": [
                    {
                        "type": "web",
                        "name": "metis",
                        "env": "docker",
                        "dockerfilePath": "Dockerfile",
                        "dockerContext": ".",
                        "healthCheckPath": "/health",
                        "envVars": [
                            {"key": "DATABASE_URL", "sync": False},
                            {"key": "REQUIRE_DATABASE_URL", "value": "true"},
                        ],
                    }
                ]
            }
        return original(relative, findings)

    monkeypatch.setattr(verify_platform, "_load_yaml", fake_load)
    findings = []
    verify_platform.check_blueprint(findings)

    assert any(item.code == "render.deprecated-env" and item.level == "FAIL" for item in findings)


def test_literal_render_secret_is_rejected(monkeypatch) -> None:
    original = verify_platform._load_yaml

    def fake_load(relative, findings):
        if relative == "render.yaml":
            return {
                "services": [
                    {
                        "type": "web",
                        "name": "metis",
                        "runtime": "docker",
                        "dockerfilePath": "Dockerfile",
                        "dockerContext": ".",
                        "healthCheckPath": "/health",
                        "envVars": [
                            {"key": "DATABASE_URL", "sync": False},
                            {"key": "REQUIRE_DATABASE_URL", "value": "true"},
                            {"key": "JWT_SECRET", "value": "committed-secret"},
                        ],
                    }
                ]
            }
        return original(relative, findings)

    monkeypatch.setattr(verify_platform, "_load_yaml", fake_load)
    findings = []
    verify_platform.check_blueprint(findings)

    assert any(item.code == "render.literal-secret" and item.level == "FAIL" for item in findings)


def test_render_runtime_image_keeps_node_toolchain_for_generated_project_qa() -> None:
    dockerfile = ROOT / "Dockerfile"
    content = dockerfile.read_text(encoding="utf-8")

    assert "FROM node:20-bookworm-slim AS node-runtime" in content
    assert "COPY --from=node-runtime /usr/local/ /usr/local/" in content
    assert "build-essential" in content


def test_hung_docker_cli_is_a_visible_nonblocking_warning(monkeypatch) -> None:
    monkeypatch.setattr(verify_platform.shutil, "which", lambda _command: "docker")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["docker", "compose"], timeout=30)

    monkeypatch.setattr(verify_platform.subprocess, "run", timeout)
    findings = []
    verify_platform.check_docker_compose_cli(findings)

    assert findings == [
        verify_platform.Finding(
            "WARN",
            "compose.cli",
            "Docker CLI did not answer within 30 seconds; YAML checks still ran",
        )
    ]


def _backup_fixture(tmp_path: Path) -> tuple[Path, Path]:
    artifact = tmp_path / "metis-postgres-v1-test.dump"
    artifact.write_bytes(b"postgres custom backup fixture")
    manifest = artifact.with_suffix(".dump.manifest.json")
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "kind": "metis-postgres-backup",
        "artifact_format": "postgresql-custom",
        "artifact_file": artifact.name,
        "artifact_size": artifact.stat().st_size,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "created_at": "2026-07-22T00:00:00Z",
    }), encoding="utf-8")
    return artifact, manifest


def test_restore_drill_defaults_to_artifact_validation_only(monkeypatch, tmp_path) -> None:
    artifact, manifest = _backup_fixture(tmp_path)
    commands = []

    def successful(command, **_kwargs):
        commands.append(list(command))
        return {"command": list(command), "exit_code": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(postgres_recovery_drill, "_run", successful)
    evidence = postgres_recovery_drill.run_restore_drill(artifact, manifest)

    assert evidence["status"] == "artifact_validated"
    assert evidence["restore_executed"] is False
    assert commands == [["pg_restore", "--list", str(artifact.resolve())]]


def test_restore_drill_rejects_checksum_mismatch(monkeypatch, tmp_path) -> None:
    artifact, manifest = _backup_fixture(tmp_path)
    artifact.write_bytes(b"tampered")
    monkeypatch.setattr(postgres_recovery_drill, "_run", lambda *_args, **_kwargs: pytest.fail("must fail before pg_restore"))

    with pytest.raises(postgres_recovery_drill.RecoveryError, match="checksum|size"):
        postgres_recovery_drill.validate_artifact(artifact, manifest)


def test_restore_drill_always_rejects_configured_production_target() -> None:
    target = "postgresql://user:secret@db.example/metis"
    with pytest.raises(postgres_recovery_drill.RecoveryError, match="refusing"):
        postgres_recovery_drill.authorize_restore_target(
            target,
            confirm_target_db="metis",
            allow_explicit_target=True,
            environment={"DATABASE_URL": "postgres://production:different@db.example:5432/metis"},
        )


def test_restore_drill_restores_confirmed_temp_target_and_records_evidence(monkeypatch, tmp_path) -> None:
    artifact, manifest = _backup_fixture(tmp_path)
    evidence_path = tmp_path / "restore-evidence.json"
    calls = []

    def successful(command, *, database_url=None, timeout=300):
        calls.append((list(command), database_url))
        return {"command": list(command), "exit_code": 0, "stdout": "verified", "stderr": ""}

    monkeypatch.setattr(postgres_recovery_drill, "_run", successful)
    evidence = postgres_recovery_drill.run_restore_drill(
        artifact,
        manifest,
        execute=True,
        target_url="postgresql://drill:secret@localhost/metis_restore_drill",
        confirm_target_db="metis_restore_drill",
        evidence_out=evidence_path,
        environment={},
    )

    assert evidence["status"] == "restore_verified"
    assert evidence["restore_executed"] is True
    assert [step["name"] for step in evidence["steps"]] == ["restore", "verification_query"]
    assert [call[0][0] for call in calls] == ["pg_restore", "pg_restore", "psql"]
    assert calls[1][0][calls[1][0].index("--dbname") + 1] == "metis_restore_drill"
    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert persisted["target"]["database"] == "metis_restore_drill"
    assert "secret" not in evidence_path.read_text(encoding="utf-8")
