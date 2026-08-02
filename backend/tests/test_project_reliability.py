import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api import routes_projects
from core import database
from core.execution_runs import IdempotencyStore
from core.state_schema import migrate_project_record, version_phase_manager_record
from models.schemas import ProjectRequest


@pytest.fixture
def reliability_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setattr(database, "_sqlite_path", str(tmp_path / "project-reliability.db"))
    database.init_db()
    monkeypatch.setattr(routes_projects, "_idempotency", IdempotencyStore())


def test_project_input_validation_rejects_empty_long_and_control_names():
    with pytest.raises(ValidationError):
        ProjectRequest(name="   ", description="valid")
    with pytest.raises(ValidationError):
        ProjectRequest(name="x" * 121, description="valid")
    with pytest.raises(ValidationError):
        ProjectRequest(name="bad\nname", description="valid")
    with pytest.raises(ValidationError):
        ProjectRequest(name="valid", description="bad\x00description")


def test_project_and_phase_records_gain_versions_without_changing_business_fields():
    project = migrate_project_record({
        "project_id": "proj-old",
        "name": "Legacy",
        "created_at": 123.0,
        "subprojects": [{"id": "task-1", "status": "pending"}],
        "qc_results": {"task-1": {"passed": False}},
    })
    phase = version_phase_manager_record({
        "current_phase_index": 0,
        "phases": [{"phase_id": "phase-1", "status": "pending"}],
    })

    assert project["schema_version"] == 1
    assert project["record_scope"] == "production"
    assert project["subprojects"][0]["schema_version"] == 1
    assert project["qc_results"]["task-1"]["passed"] is False
    assert phase["schema_version"] == 1
    assert phase["phases"][0]["status"] == "pending"


def test_project_creation_idempotency_replays_one_created_resource(reliability_db, monkeypatch):
    calls = []

    def fake_create(request, current_user):
        calls.append(request.name)
        context = SimpleNamespace(to_persist=lambda: {
            "project_id": "proj-once", "name": request.name,
            "owner_user_id": current_user.user_id,
        })
        return "proj-once", context, {
            "project_id": "proj-once", "name": request.name,
        }

    monkeypatch.setattr(routes_projects, "_new_project_response", fake_create)
    user = SimpleNamespace(user_id="user-1")
    request = ProjectRequest(name="Reliable", description="same request")

    first = asyncio.run(routes_projects.create_project(request, user, "create-key"))
    replay = asyncio.run(routes_projects.create_project(request, user, "create-key"))

    assert first["project_id"] == "proj-once"
    assert replay == {
        "project_id": "proj-once",
        "name": "Reliable",
        "idempotent_replay": True,
    }
    assert calls == ["Reliable"]
    routes_projects.projects.pop("proj-once", None)


def test_project_idempotency_key_rejects_different_payload(reliability_db, monkeypatch):
    async def fake_create(request, current_user):
        return {"project_id": "proj-once", "name": request.name}

    monkeypatch.setattr(routes_projects, "_create_project_once", fake_create)
    user = SimpleNamespace(user_id="user-1")
    asyncio.run(routes_projects.create_project(
        ProjectRequest(name="One", description="same"), user, "create-key",
    ))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes_projects.create_project(
            ProjectRequest(name="Two", description="different"), user, "create-key",
        ))
    assert exc_info.value.status_code == 409
