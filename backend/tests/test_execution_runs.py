import hashlib
import sqlite3

import pytest

from core import database
from core.execution_runs import (
    DurableRunRegistry,
    IdempotencyConflict,
    IdempotencyStore,
    InvalidTransition,
    LeaseConflict,
    VersionConflict,
)


class Clock:
    def __init__(self, value=1_000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@pytest.fixture
def run_db(tmp_path, monkeypatch):
    path = tmp_path / "runs.db"
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setattr(database, "_sqlite_path", str(path))
    database.init_db()
    return path


def test_schema_has_unique_run_and_generic_idempotency_keys(run_db):
    with sqlite3.connect(run_db) as conn:
        run_indexes = conn.execute("PRAGMA index_list(execution_runs)").fetchall()
        idem_pk = conn.execute("PRAGMA table_info(idempotency_records)").fetchall()

    assert any(row[2] for row in run_indexes), "execution_runs must have a unique index"
    assert [row[1] for row in idem_pk if row[5]] == ["scope", "actor_id", "key"]


def test_idempotency_reserve_replay_complete_and_payload_conflict(run_db):
    clock = Clock()
    store = IdempotencyStore(clock)
    request = {"name": "same project", "template": "api"}

    first = store.reserve("projects.create", "user-1", "request-123", request)
    replay = store.reserve("projects.create", "user-1", "request-123", request)
    completed = store.complete(
        "projects.create",
        "user-1",
        "request-123",
        request,
        resource_type="project",
        resource_id="project-1",
        response={"id": "project-1"},
    )
    replay_after_completion = store.reserve("projects.create", "user-1", "request-123", request)

    assert first["acquired"] is True
    assert replay["acquired"] is False
    assert completed["status"] == "completed"
    assert replay_after_completion["response"] == {"id": "project-1"}
    with pytest.raises(IdempotencyConflict):
        store.reserve("projects.create", "user-1", "request-123", {"name": "different"})


def test_long_idempotency_key_is_stored_within_postgres_width_and_replays(run_db):
    store = IdempotencyStore(Clock())
    key = (
        "phase:phase-1:task:task-1:generation:generation-1:"
        f"contract:sha256:{'a' * 64}:revision:revision-1:"
        f"baseline:sha256:{'b' * 64}:"
        + ("x" * 128)
    )
    request = {"task_id": "task-1", "phase_id": "phase-1"}

    first = store.reserve("agent.execute", "project-1:agent-1", key, request)
    replay = store.reserve("agent.execute", "project-1:agent-1", key, request)
    completed = store.complete(
        "agent.execute",
        "project-1:agent-1",
        key,
        request,
        resource_type="execution_run",
        resource_id="run-1",
        response={"run_id": "run-1"},
    )

    with sqlite3.connect(run_db) as conn:
        stored_key = conn.execute(
            "SELECT key FROM idempotency_records WHERE scope = ? AND actor_id = ?",
            ("agent.execute", "project-1:agent-1"),
        ).fetchone()[0]

    assert len(key) > 256
    assert stored_key.startswith("sha256:")
    assert len(stored_key) <= 256
    assert first["acquired"] is True
    assert replay["acquired"] is False
    assert completed["status"] == "completed"
    assert store.get("agent.execute", "project-1:agent-1", key)["response"] == {
        "run_id": "run-1",
    }
    assert store.reserve("agent.execute", "project-1:agent-1", key, request)[
        "response"
    ] == {"run_id": "run-1"}
    with pytest.raises(IdempotencyConflict):
        store.reserve(
            "agent.execute",
            "project-1:agent-1",
            key,
            {"task_id": "different", "phase_id": "phase-1"},
        )


def test_create_or_get_claim_heartbeat_and_success(run_db):
    clock = Clock()
    registry = DurableRunRegistry(clock)
    created, was_created = registry.create_or_get_run(
        idempotency_key="execute:user-1:task-1",
        run_type="agent.execute",
        actor_id="user-1",
        timeout_seconds=60,
        payload={"task_id": "task-1"},
    )
    replay, replay_created = registry.create_or_get_run(
        idempotency_key="execute:user-1:task-1",
        run_type="agent.execute",
        actor_id="user-1",
        timeout_seconds=60,
        payload={"task_id": "task-1"},
    )

    assert was_created is True
    assert replay_created is False
    assert replay["run_id"] == created["run_id"]

    running = registry.claim(created["run_id"], "worker-1", lease_seconds=20)
    assert running["status"] == "running"
    assert running["attempt_count"] == 1
    with pytest.raises(LeaseConflict):
        registry.claim(created["run_id"], "worker-2", lease_seconds=20)

    clock.advance(5)
    heartbeat = registry.heartbeat(created["run_id"], "worker-1", lease_seconds=20)
    assert heartbeat["heartbeat_at"] == clock.value
    succeeded = registry.succeed(created["run_id"], "worker-1", result={"ok": True})
    assert succeeded["status"] == "succeeded"
    assert succeeded["result"] == {"ok": True}
    assert [event["to_status"] for event in registry.events(created["run_id"])] == [
        "pending", "running", "succeeded",
    ]


def test_cancel_unleased_is_versioned_and_never_cancels_a_live_lease(run_db):
    clock = Clock()
    registry = DurableRunRegistry(clock)
    pending, _ = registry.create_or_get_run(
        idempotency_key="startup-stale-pending",
        run_type="phase.dispatch",
        actor_id="phase-coordinator",
        timeout_seconds=60,
    )

    with pytest.raises(VersionConflict):
        registry.cancel_unleased(
            pending["run_id"],
            reason="superseded",
            actor="startup-reconcile",
            expected_version=pending["version"] + 1,
        )

    running = registry.claim(
        pending["run_id"], "worker-1", lease_seconds=30,
    )
    with pytest.raises(InvalidTransition):
        registry.cancel_unleased(
            running["run_id"],
            reason="superseded",
            actor="startup-reconcile",
            expected_version=running["version"],
        )
    assert registry.get(running["run_id"])["status"] == "running"

    blocked, _ = registry.create_or_get_run(
        idempotency_key="startup-stale-blocked",
        run_type="agent.execute",
        actor_id="agent-1",
        timeout_seconds=60,
    )
    blocked = registry.block(blocked["run_id"], reason="stale attempt")
    assert blocked["last_error"] == "stale attempt"
    cancelled = registry.cancel_unleased(
        blocked["run_id"],
        reason="parent superseded",
        actor="startup-reconcile",
        expected_version=blocked["version"],
    )
    assert cancelled["status"] == "cancelled"
    assert cancelled["last_error"] == "parent superseded"


@pytest.mark.parametrize("lease_expires_at", [None, 2_000.0])
def test_cancel_unleased_fails_closed_for_malformed_owned_idle_run(
    run_db, lease_expires_at,
):
    clock = Clock()
    registry = DurableRunRegistry(clock)
    run, _ = registry.create_or_get_run(
        idempotency_key=f"malformed-idle-lease-{lease_expires_at}",
        run_type="phase.dispatch",
        actor_id="phase-coordinator",
        timeout_seconds=60,
    )
    with sqlite3.connect(run_db) as conn:
        conn.execute(
            """
            UPDATE execution_runs
               SET lease_owner = ?, lease_expires_at = ?
             WHERE run_id = ?
            """,
            ("unexpected-worker", lease_expires_at, run["run_id"]),
        )

    with pytest.raises(LeaseConflict):
        registry.cancel_unleased(
            run["run_id"],
            reason="superseded",
            actor="startup-reconcile",
            expected_version=run["version"],
        )
    assert registry.get(run["run_id"])["status"] == "pending"


def test_durable_run_long_idempotency_keys_use_stable_distinct_storage_digests(
    run_db,
):
    registry = DurableRunRegistry(Clock())
    generation = "generation:" + ("g" * 175)
    attempt_digest = "a" * 64
    raw_key = (
        f"phase.dispatch:proj-66b64f:phase-1:{generation}:"
        f"attempt:{attempt_digest}"
    )
    other_raw_key = raw_key[:-1] + "b"
    expected_key = (
        "sha256:" + hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    )
    expected_other_key = (
        "sha256:" + hashlib.sha256(other_raw_key.encode("utf-8")).hexdigest()
    )
    create_args = {
        "run_type": "phase.dispatch",
        "actor_id": "phase-coordinator:proj-66b64f:phase-1",
        "project_id": "proj-66b64f",
        "timeout_seconds": 60,
        "payload": {
            "project_id": "proj-66b64f",
            "phase_id": "phase-1",
            "execution_generation": generation,
            "dispatch_attempt_digest": attempt_digest,
        },
    }

    first, first_created = registry.create_or_get_run(
        idempotency_key=raw_key,
        **create_args,
    )
    replay, replay_created = registry.create_or_get_run(
        idempotency_key=raw_key,
        **create_args,
    )
    distinct, distinct_created = registry.create_or_get_run(
        idempotency_key=other_raw_key,
        **{
            **create_args,
            "payload": {
                **create_args["payload"],
                "dispatch_attempt_digest": other_raw_key[-64:],
            },
        },
    )

    with sqlite3.connect(run_db) as conn:
        stored_keys = {
            row[0]
            for row in conn.execute(
                "SELECT idempotency_key FROM execution_runs",
            ).fetchall()
        }

    assert len(raw_key) > 256
    assert first_created is True
    assert replay_created is False
    assert replay["run_id"] == first["run_id"]
    assert distinct_created is True
    assert distinct["run_id"] != first["run_id"]
    assert expected_key != expected_other_key
    assert first["idempotency_key"] == expected_key
    assert replay["idempotency_key"] == expected_key
    assert distinct["idempotency_key"] == expected_other_key
    assert stored_keys == {expected_key, expected_other_key}
    assert all(len(key) <= 256 for key in stored_keys)


def test_failure_retries_with_exponential_backoff_then_stays_failed(run_db):
    clock = Clock()
    registry = DurableRunRegistry(clock)
    run, _ = registry.create_or_get_run(
        idempotency_key="retry-run",
        run_type="agent.execute",
        actor_id="user-1",
        timeout_seconds=30,
        max_retries=2,
        retry_backoff=3,
    )

    registry.claim(run["run_id"], "worker", lease_seconds=10)
    retry_one = registry.fail(run["run_id"], "worker", error="attempt one")
    assert retry_one["status"] == "pending"
    assert retry_one["next_attempt_at"] == clock.value + 3
    with pytest.raises(LeaseConflict):
        registry.claim(run["run_id"], "worker", lease_seconds=10)

    clock.advance(3)
    registry.claim(run["run_id"], "worker", lease_seconds=10)
    retry_two = registry.fail(run["run_id"], "worker", error="attempt two")
    assert retry_two["status"] == "pending"
    assert retry_two["next_attempt_at"] == clock.value + 6

    clock.advance(6)
    registry.claim(run["run_id"], "worker", lease_seconds=10)
    terminal = registry.fail(run["run_id"], "worker", error="attempt three")
    assert terminal["status"] == "failed"
    assert terminal["attempt_count"] == 3
    with pytest.raises(InvalidTransition):
        registry.succeed(run["run_id"], "worker", result={"false_positive": True})


def test_hard_timeout_retries_then_becomes_timeout(run_db):
    clock = Clock()
    registry = DurableRunRegistry(clock)
    run, _ = registry.create_or_get_run(
        idempotency_key="timeout-run",
        run_type="agent.execute",
        actor_id="user-1",
        timeout_seconds=5,
        max_retries=1,
        retry_backoff=0,
    )
    registry.claim(run["run_id"], "worker", lease_seconds=30)

    clock.advance(5)
    first = registry.enforce_timeouts()
    assert first[0]["status"] == "pending"
    registry.claim(run["run_id"], "worker", lease_seconds=30)
    clock.advance(5)
    second = registry.enforce_timeouts()
    assert second[0]["status"] == "timeout"


def test_startup_recovery_retries_expired_lease_or_blocks_when_exhausted(run_db):
    clock = Clock()
    registry = DurableRunRegistry(clock)
    retryable, _ = registry.create_or_get_run(
        idempotency_key="recovery-retry",
        run_type="agent.execute",
        actor_id="user-1",
        timeout_seconds=100,
        max_retries=1,
        retry_backoff=2,
    )
    exhausted, _ = registry.create_or_get_run(
        idempotency_key="recovery-block",
        run_type="agent.execute",
        actor_id="user-1",
        timeout_seconds=100,
        max_retries=0,
    )
    registry.claim(retryable["run_id"], "dead-worker", lease_seconds=5)
    registry.claim(exhausted["run_id"], "dead-worker", lease_seconds=5)

    clock.advance(6)
    recovered = {item["run_id"]: item for item in registry.recover_startup()}
    assert recovered[retryable["run_id"]]["status"] == "pending"
    assert recovered[retryable["run_id"]]["next_attempt_at"] == clock.value + 2
    assert recovered[exhausted["run_id"]]["status"] == "blocked"

    taken_over = registry.take_over(exhausted["run_id"], "human-operator", lease_seconds=20)
    assert taken_over["status"] == "running"
    assert taken_over["lease_owner"] == "human-operator"


def test_cancel_and_critical_failure_propagate_without_false_parent_success(run_db):
    clock = Clock()
    registry = DurableRunRegistry(clock)
    parent, _ = registry.create_or_get_run(
        idempotency_key="parent",
        run_type="workflow",
        actor_id="user-1",
        timeout_seconds=100,
    )
    registry.claim(parent["run_id"], "orchestrator", lease_seconds=50)
    child, _ = registry.create_or_get_run(
        idempotency_key="critical-child",
        run_type="agent.execute",
        actor_id="user-1",
        timeout_seconds=20,
        parent_run_id=parent["run_id"],
        critical=True,
    )
    registry.claim(child["run_id"], "worker", lease_seconds=20)
    registry.fail(child["run_id"], "worker", error="build failed", retryable=False)

    assert registry.get(child["run_id"])["status"] == "failed"
    assert registry.get(parent["run_id"])["status"] == "failed"
    with pytest.raises(InvalidTransition):
        registry.succeed(parent["run_id"], "orchestrator")

    cancellable, _ = registry.create_or_get_run(
        idempotency_key="cancel-run",
        run_type="agent.execute",
        actor_id="user-1",
        timeout_seconds=20,
    )
    registry.cancel(cancellable["run_id"], reason="user stopped it", actor="user-1")
    assert registry.get(cancellable["run_id"])["status"] == "cancelled"


def test_parent_cannot_succeed_while_critical_child_is_pending(run_db):
    clock = Clock()
    registry = DurableRunRegistry(clock)
    parent, _ = registry.create_or_get_run(
        idempotency_key="parent-pending-child",
        run_type="workflow",
        actor_id="user-1",
        timeout_seconds=100,
    )
    registry.claim(parent["run_id"], "orchestrator", lease_seconds=50)
    registry.create_or_get_run(
        idempotency_key="pending-child",
        run_type="agent.execute",
        actor_id="user-1",
        timeout_seconds=20,
        parent_run_id=parent["run_id"],
        critical=True,
    )

    with pytest.raises(InvalidTransition, match="critical child"):
        registry.succeed(parent["run_id"], "orchestrator")
