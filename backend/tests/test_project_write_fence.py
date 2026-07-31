import asyncio

import pytest

from core import project_write_fence as fence_module
from core.project_write_fence import (
    ProjectWriteFenceConflict,
    acquire_project_write_fence,
    get_project_write_fence,
    project_write_guard,
    release_project_write_fence,
)


@pytest.fixture
def fake_expert_lock(monkeypatch):
    releases = []
    monkeypatch.setattr(
        fence_module.expert_lock,
        "atomic_claim_lock",
        lambda **_kwargs: {"success": True, "lock_id": "lock-1"},
    )
    monkeypatch.setattr(
        fence_module.expert_lock,
        "release_lock",
        lambda lock_id: releases.append(lock_id) or {"success": True},
    )
    return releases


def test_current_task_can_atomically_handoff_writer_to_durable_fence(
    tmp_path, fake_expert_lock
):
    project_id = "writer-handoff"
    with project_write_guard(project_id, tmp_path) as capability:
        fence = acquire_project_write_fence(
            project_id,
            tmp_path,
            owner="final-qa:writer-handoff",
            purpose="final_qa",
            writer_capability=capability,
        )
        assert get_project_write_fence(project_id, tmp_path)["lock_id"] == "lock-1"

    assert release_project_write_fence(project_id, tmp_path, fence["token"])
    assert fake_expert_lock == ["lock-1"]


def test_inherited_capability_from_another_task_and_stale_capability_are_rejected(
    tmp_path, fake_expert_lock
):
    project_id = "writer-task-aware"

    async def scenario():
        with project_write_guard(project_id, tmp_path) as capability:
            async def other_task():
                with pytest.raises(ProjectWriteFenceConflict):
                    acquire_project_write_fence(
                        project_id,
                        tmp_path,
                        owner="final-qa:other",
                        purpose="final_qa",
                        writer_capability=capability,
                    )

            await asyncio.create_task(other_task())
        with pytest.raises(ProjectWriteFenceConflict, match="stale"):
            acquire_project_write_fence(
                project_id,
                tmp_path,
                owner="final-qa:stale",
                purpose="final_qa",
                writer_capability=capability,
            )

    asyncio.run(scenario())


def test_failed_expert_lock_release_keeps_durable_marker(
    tmp_path, monkeypatch, fake_expert_lock
):
    project_id = "release-fail-closed"
    fence = acquire_project_write_fence(
        project_id,
        tmp_path,
        owner="final-qa:release",
        purpose="final_qa",
    )
    monkeypatch.setattr(
        fence_module.expert_lock,
        "release_lock",
        lambda _lock_id: {"success": False},
    )

    with pytest.raises(ProjectWriteFenceConflict, match="release failed"):
        release_project_write_fence(project_id, tmp_path, fence["token"])

    assert get_project_write_fence(project_id, tmp_path) is not None
