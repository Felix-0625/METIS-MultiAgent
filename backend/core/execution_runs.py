"""Durable, database-backed execution and idempotency primitives.

This module deliberately contains no PM or quality-control policy.  Routes and
workers can build their own orchestration on top of the small state machine,
while retries, leases, timeouts and duplicate suppression remain consistent.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from core.database import get_conn, _use_postgres


RUN_STATUSES = frozenset({
    "pending", "running", "succeeded", "failed", "timeout", "blocked", "cancelled",
})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timeout", "cancelled"})
FAILURE_STATUSES = frozenset({"failed", "timeout", "blocked", "cancelled"})
MAX_IDEMPOTENCY_RECORD_KEY_LENGTH = 256

ALLOWED_TRANSITIONS = {
    "pending": frozenset({"running", "blocked", "cancelled"}),
    "running": frozenset({"pending", "succeeded", "failed", "timeout", "blocked", "cancelled"}),
    "blocked": frozenset({"pending", "running", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "timeout": frozenset(),
    "cancelled": frozenset(),
}


class ExecutionRunError(RuntimeError):
    """Base class for durable-run errors."""


class RunNotFound(ExecutionRunError):
    pass


class InvalidTransition(ExecutionRunError):
    pass


class LeaseConflict(ExecutionRunError):
    pass


class VersionConflict(ExecutionRunError):
    pass


class IdempotencyConflict(ExecutionRunError):
    pass


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(value: Optional[str]) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _request_hash(value: Any) -> str:
    return hashlib.sha256(_json_dump(value).encode("utf-8")).hexdigest()


def _storage_idempotency_key(key: str) -> str:
    """Keep caller keys addressable within the PostgreSQL schema width."""
    if len(key) <= MAX_IDEMPOTENCY_RECORD_KEY_LENGTH:
        return key
    return f"sha256:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"


def _fetchone_dict(cursor) -> Optional[Dict[str, Any]]:
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [item[0] for item in cursor.description]
    return dict(zip(columns, row))


def _fetchall_dict(cursor) -> List[Dict[str, Any]]:
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _decode_run(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    decoded = dict(row)
    decoded["critical"] = bool(decoded["critical"])
    decoded["payload"] = _json_load(decoded.pop("payload_json"))
    decoded["result"] = _json_load(decoded.pop("result_json"))
    return decoded


class IdempotencyStore:
    """Atomic request deduplication shared by project, task and run creation."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock

    @staticmethod
    def hash_request(request: Any) -> str:
        return _request_hash(request)

    def reserve(
        self,
        scope: str,
        actor_id: str,
        key: str,
        request: Any,
    ) -> Dict[str, Any]:
        """Reserve a key, returning ``acquired=False`` for an exact replay.

        Reusing a key for a different request is rejected instead of returning
        another request's resource or response.
        """
        if not scope or not actor_id or not key:
            raise ValueError("scope, actor_id and key are required")
        storage_key = _storage_idempotency_key(key)
        request_hash = _request_hash(request)
        now = self._clock()
        placeholder = "%s" if _use_postgres() else "?"
        values = (scope, actor_id, storage_key, request_hash, "reserved", now, now)
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                INSERT INTO idempotency_records
                    (scope, actor_id, key, request_hash, status, created_at, updated_at)
                VALUES ({','.join([placeholder] * 7)})
                ON CONFLICT (scope, actor_id, key) DO NOTHING
                """,
                values,
            )
            acquired = cur.rowcount == 1
            cur.execute(
                f"""SELECT * FROM idempotency_records
                    WHERE scope = {placeholder} AND actor_id = {placeholder} AND key = {placeholder}""",
                (scope, actor_id, storage_key),
            )
            record = _fetchone_dict(cur)
        if record is None:  # pragma: no cover - database invariant
            raise ExecutionRunError("idempotency reservation disappeared")
        if record["request_hash"] != request_hash:
            raise IdempotencyConflict("idempotency key was already used for a different request")
        record["acquired"] = acquired
        record["response"] = _json_load(record.pop("response_json"))
        return record

    def get(self, scope: str, actor_id: str, key: str) -> Optional[Dict[str, Any]]:
        storage_key = _storage_idempotency_key(key)
        placeholder = "%s" if _use_postgres() else "?"
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""SELECT * FROM idempotency_records
                    WHERE scope = {placeholder} AND actor_id = {placeholder} AND key = {placeholder}""",
                (scope, actor_id, storage_key),
            )
            record = _fetchone_dict(cur)
        if record is not None:
            record["response"] = _json_load(record.pop("response_json"))
        return record

    def complete(
        self,
        scope: str,
        actor_id: str,
        key: str,
        request: Any,
        *,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        response: Any = None,
    ) -> Dict[str, Any]:
        return self._finish(
            scope, actor_id, key, request, "completed",
            resource_type=resource_type, resource_id=resource_id, response=response,
        )

    def fail(
        self,
        scope: str,
        actor_id: str,
        key: str,
        request: Any,
        *,
        error_code: str,
        response: Any = None,
    ) -> Dict[str, Any]:
        if not error_code:
            raise ValueError("error_code is required")
        return self._finish(
            scope, actor_id, key, request, "failed",
            error_code=error_code, response=response,
        )

    def _finish(
        self,
        scope: str,
        actor_id: str,
        key: str,
        request: Any,
        status: str,
        *,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        response: Any = None,
        error_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        storage_key = _storage_idempotency_key(key)
        request_hash = _request_hash(request)
        now = self._clock()
        placeholder = "%s" if _use_postgres() else "?"
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                UPDATE idempotency_records
                   SET status = {placeholder}, resource_type = {placeholder},
                       resource_id = {placeholder}, response_json = {placeholder},
                       error_code = {placeholder}, updated_at = {placeholder}
                 WHERE scope = {placeholder} AND actor_id = {placeholder} AND key = {placeholder}
                   AND request_hash = {placeholder} AND status = 'reserved'
                """,
                (
                    status, resource_type, resource_id, _json_dump(response), error_code, now,
                    scope, actor_id, storage_key, request_hash,
                ),
            )
            if cur.rowcount != 1:
                cur.execute(
                    f"""SELECT request_hash, status FROM idempotency_records
                        WHERE scope = {placeholder} AND actor_id = {placeholder} AND key = {placeholder}""",
                    (scope, actor_id, storage_key),
                )
                existing = _fetchone_dict(cur)
                if existing is None:
                    raise IdempotencyConflict("idempotency key was not reserved")
                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflict("idempotency key belongs to a different request")
                if existing["status"] != status:
                    raise IdempotencyConflict(f"idempotency key is already {existing['status']}")
            cur.execute(
                f"""SELECT * FROM idempotency_records
                    WHERE scope = {placeholder} AND actor_id = {placeholder} AND key = {placeholder}""",
                (scope, actor_id, storage_key),
            )
            record = _fetchone_dict(cur)
        if record is None:  # pragma: no cover - database invariant
            raise ExecutionRunError("idempotency record disappeared")
        record["response"] = _json_load(record.pop("response_json"))
        return record


class DurableRunRegistry:
    """Persistence and concurrency boundary for background execution runs."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock

    def create_or_get_run(
        self,
        *,
        idempotency_key: str,
        run_type: str,
        actor_id: str,
        timeout_seconds: float,
        payload: Any = None,
        project_id: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        critical: bool = False,
        max_retries: int = 0,
        retry_backoff: float = 1.0,
        run_id: Optional[str] = None,
    ) -> tuple[Dict[str, Any], bool]:
        """Create once by idempotency key, or return the exact prior run."""
        if not idempotency_key or not run_type or not actor_id:
            raise ValueError("idempotency_key, run_type and actor_id are required")
        if timeout_seconds <= 0 or max_retries < 0 or retry_backoff < 0:
            raise ValueError("invalid timeout or retry policy")
        run_id = run_id or uuid.uuid4().hex
        storage_key = _storage_idempotency_key(idempotency_key)
        now = self._clock()
        payload_json = _json_dump(payload if payload is not None else {})
        placeholder = "%s" if _use_postgres() else "?"
        values = (
            run_id, storage_key, run_type, actor_id, project_id, parent_run_id,
            bool(critical), "pending", payload_json, int(max_retries), float(retry_backoff),
            now, float(timeout_seconds), now, now,
        )
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                INSERT INTO execution_runs
                    (run_id, idempotency_key, run_type, actor_id, project_id, parent_run_id,
                     critical, status, payload_json, max_retries, retry_backoff,
                     next_attempt_at, timeout_seconds, created_at, updated_at)
                VALUES ({','.join([placeholder] * 15)})
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                values,
            )
            created = cur.rowcount == 1
            if created:
                self._insert_event(cur, run_id, None, "pending", "run created", now, 1)
            cur.execute(
                f"SELECT * FROM execution_runs WHERE idempotency_key = {placeholder}",
                (storage_key,),
            )
            row = _fetchone_dict(cur)
        if row is None:  # pragma: no cover - database invariant
            raise ExecutionRunError("execution run disappeared")
        expected = (
            run_type, actor_id, project_id, parent_run_id, bool(critical), payload_json,
            int(max_retries), float(retry_backoff), float(timeout_seconds),
        )
        actual = (
            row["run_type"], row["actor_id"], row["project_id"], row["parent_run_id"],
            bool(row["critical"]), row["payload_json"], int(row["max_retries"]),
            float(row["retry_backoff"]), float(row["timeout_seconds"]),
        )
        if actual != expected:
            raise IdempotencyConflict("run idempotency key was already used for different input")
        return _decode_run(row), created

    def get(self, run_id: str) -> Dict[str, Any]:
        placeholder = "%s" if _use_postgres() else "?"
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM execution_runs WHERE run_id = {placeholder}", (run_id,))
            row = _fetchone_dict(cur)
        if row is None:
            raise RunNotFound(run_id)
        return _decode_run(row)

    def events(self, run_id: str) -> List[Dict[str, Any]]:
        placeholder = "%s" if _use_postgres() else "?"
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT * FROM execution_run_events WHERE run_id = {placeholder} ORDER BY version, created_at, event_id",
                (run_id,),
            )
            return _fetchall_dict(cur)

    def list_runs(
        self,
        *,
        statuses: Optional[List[str]] = None,
        run_type: Optional[str] = None,
        project_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """List durable runs for recovery, operators and scoped APIs."""
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        normalized = list(dict.fromkeys(statuses or []))
        unknown = set(normalized) - set(RUN_STATUSES)
        if unknown:
            raise ValueError(f"unknown run statuses: {sorted(unknown)}")
        placeholder = "%s" if _use_postgres() else "?"
        clauses: List[str] = []
        params: List[Any] = []
        if normalized:
            clauses.append(f"status IN ({','.join([placeholder] * len(normalized))})")
            params.extend(normalized)
        if run_type is not None:
            clauses.append(f"run_type = {placeholder}")
            params.append(run_type)
        if project_id is not None:
            clauses.append(f"project_id = {placeholder}")
            params.append(project_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT * FROM execution_runs{where} ORDER BY created_at, run_id LIMIT {int(limit)}",
                tuple(params),
            )
            rows = _fetchall_dict(cur)
        return [_decode_run(row) for row in rows]

    def claim(
        self,
        run_id: str,
        owner: str,
        *,
        lease_seconds: float,
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not owner or lease_seconds <= 0:
            raise ValueError("owner and positive lease_seconds are required")
        now = self._clock()
        with get_conn() as conn:
            cur = conn.cursor()
            row = self._select_run(cur, run_id)
            if row["status"] != "pending":
                raise LeaseConflict(f"run is {row['status']}, not pending")
            if row["next_attempt_at"] is not None and row["next_attempt_at"] > now:
                raise LeaseConflict("run retry backoff has not elapsed")
            self._check_version(row, expected_version)
            new_version = row["version"] + 1
            placeholder = "%s" if _use_postgres() else "?"
            cur.execute(
                f"""
                UPDATE execution_runs
                   SET status = 'running', lease_owner = {placeholder}, lease_expires_at = {placeholder},
                       heartbeat_at = {placeholder}, timeout_at = {placeholder}, next_attempt_at = NULL,
                       attempt_count = attempt_count + 1, started_at = COALESCE(started_at, {placeholder}),
                       finished_at = NULL, updated_at = {placeholder}, version = {placeholder}
                 WHERE run_id = {placeholder} AND status = 'pending' AND version = {placeholder}
                """,
                (
                    owner, now + lease_seconds, now, now + row["timeout_seconds"], now,
                    now, new_version, run_id, row["version"],
                ),
            )
            if cur.rowcount != 1:
                raise LeaseConflict("run was claimed concurrently")
            self._insert_event(cur, run_id, "pending", "running", f"lease acquired by {owner}", now, new_version)
        return self.get(run_id)

    def claim_next(self, owner: str, *, lease_seconds: float, run_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Claim the oldest due run; contention is resolved by versioned claim."""
        now = self._clock()
        placeholder = "%s" if _use_postgres() else "?"
        with get_conn() as conn:
            cur = conn.cursor()
            sql = """SELECT run_id FROM execution_runs
                     WHERE status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= {p})""".format(p=placeholder)
            params: List[Any] = [now]
            if run_type is not None:
                sql += f" AND run_type = {placeholder}"
                params.append(run_type)
            sql += " ORDER BY created_at, run_id"
            cur.execute(sql, tuple(params))
            candidates = [item[0] for item in cur.fetchall()]
        for candidate in candidates:
            try:
                return self.claim(candidate, owner, lease_seconds=lease_seconds)
            except LeaseConflict:
                continue
        return None

    def heartbeat(self, run_id: str, owner: str, *, lease_seconds: float) -> Dict[str, Any]:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = self._clock()
        placeholder = "%s" if _use_postgres() else "?"
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                UPDATE execution_runs
                   SET heartbeat_at = {placeholder}, lease_expires_at = {placeholder},
                       updated_at = {placeholder}, version = version + 1
                 WHERE run_id = {placeholder} AND status = 'running' AND lease_owner = {placeholder}
                   AND lease_expires_at >= {placeholder}
                """,
                (now, now + lease_seconds, now, run_id, owner, now),
            )
            if cur.rowcount != 1:
                raise LeaseConflict("run lease is absent, expired, or owned by another worker")
        return self.get(run_id)

    def succeed(self, run_id: str, owner: str, *, result: Any = None) -> Dict[str, Any]:
        now = self._clock()
        with get_conn() as conn:
            cur = conn.cursor()
            row = self._select_run(cur, run_id)
            self._require_owner(row, owner, now)
            incomplete = self._critical_child_failures(cur, run_id)
            if incomplete:
                statuses = ", ".join(f"{item['run_id']}={item['status']}" for item in incomplete)
                raise InvalidTransition(f"critical child runs are not successful: {statuses}")
            self._transition(
                cur, row, "succeeded", now, reason="run succeeded", result_json=_json_dump(result),
            )
        return self.get(run_id)

    def fail(self, run_id: str, owner: str, *, error: str, retryable: bool = True) -> Dict[str, Any]:
        return self._finish_failure(run_id, owner, "failed", error, retryable)

    def mark_timeout(self, run_id: str, owner: str, *, error: str = "execution timed out") -> Dict[str, Any]:
        return self._finish_failure(run_id, owner, "timeout", error, True)

    def cancel(self, run_id: str, *, reason: str, actor: str) -> Dict[str, Any]:
        if not actor or not reason:
            raise ValueError("actor and reason are required")
        now = self._clock()
        with get_conn() as conn:
            cur = conn.cursor()
            row = self._select_run(cur, run_id)
            if row["status"] not in {"pending", "running", "blocked"}:
                raise InvalidTransition(f"cannot cancel a {row['status']} run")
            self._transition(cur, row, "cancelled", now, reason=f"{actor}: {reason}")
            self._propagate_critical_failure(cur, row, "cancelled", now)
        return self.get(run_id)

    def cancel_unleased(
        self,
        run_id: str,
        *,
        reason: str,
        actor: str,
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Atomically cancel only an idle pending/blocked run.

        Startup reconciliation must never steal work from a live worker.  The
        version check closes the race with ``claim`` while the lease check
        fails closed for malformed rows that still look worker-owned.
        """
        if not actor or not reason:
            raise ValueError("actor and reason are required")
        now = self._clock()
        with get_conn() as conn:
            cur = conn.cursor()
            row = self._select_run(cur, run_id)
            if row["status"] not in {"pending", "blocked"}:
                raise InvalidTransition(
                    f"cannot cancel leased/non-idle {row['status']} run"
                )
            lease_owner = str(row.get("lease_owner") or "")
            lease_expires_at = row.get("lease_expires_at")
            if (
                (lease_owner and lease_expires_at is None)
                or (
                    lease_expires_at is not None
                    and float(lease_expires_at) >= now
                )
            ):
                raise LeaseConflict("run still has a valid or indeterminate lease")
            self._check_version(row, expected_version)
            self._transition(
                cur,
                row,
                "cancelled",
                now,
                reason=f"{actor}: {reason}",
                last_error=reason,
            )
            self._propagate_critical_failure(cur, row, "cancelled", now)
        return self.get(run_id)

    def block(self, run_id: str, *, reason: str) -> Dict[str, Any]:
        if not reason:
            raise ValueError("reason is required")
        now = self._clock()
        with get_conn() as conn:
            cur = conn.cursor()
            row = self._select_run(cur, run_id)
            if row["status"] not in {"pending", "running"}:
                raise InvalidTransition(f"cannot block a {row['status']} run")
            self._transition(cur, row, "blocked", now, reason=reason)
            self._propagate_critical_failure(cur, row, "blocked", now)
        return self.get(run_id)

    def retry_blocked(self, run_id: str, *, reason: str) -> Dict[str, Any]:
        now = self._clock()
        with get_conn() as conn:
            cur = conn.cursor()
            row = self._select_run(cur, run_id)
            if row["status"] != "blocked":
                raise InvalidTransition(f"cannot retry a {row['status']} run")
            self._transition(cur, row, "pending", now, reason=reason, next_attempt_at=now)
        return self.get(run_id)

    def take_over(
        self,
        run_id: str,
        human_actor: str,
        *,
        lease_seconds: float,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Acquire a blocked/pending run, or explicitly steal an expired lease."""
        if not human_actor or lease_seconds <= 0:
            raise ValueError("human_actor and positive lease_seconds are required")
        now = self._clock()
        with get_conn() as conn:
            cur = conn.cursor()
            row = self._select_run(cur, run_id)
            if row["status"] == "running":
                if not force and (row["lease_expires_at"] or 0) >= now:
                    raise LeaseConflict("active lease cannot be taken over without force=True")
                from_status = "running"
            elif row["status"] in {"blocked", "pending"}:
                from_status = row["status"]
            else:
                raise InvalidTransition(f"cannot take over a {row['status']} run")
            new_version = row["version"] + 1
            placeholder = "%s" if _use_postgres() else "?"
            cur.execute(
                f"""
                UPDATE execution_runs
                   SET status = 'running', lease_owner = {placeholder}, lease_expires_at = {placeholder},
                       heartbeat_at = {placeholder}, timeout_at = {placeholder}, next_attempt_at = NULL,
                       attempt_count = attempt_count + 1, started_at = COALESCE(started_at, {placeholder}),
                       finished_at = NULL, updated_at = {placeholder}, version = {placeholder}
                 WHERE run_id = {placeholder} AND version = {placeholder}
                """,
                (
                    human_actor, now + lease_seconds, now, now + row["timeout_seconds"], now,
                    now, new_version, run_id, row["version"],
                ),
            )
            if cur.rowcount != 1:
                raise VersionConflict("run changed during takeover")
            self._insert_event(
                cur, run_id, from_status, "running", f"human takeover by {human_actor}", now, new_version,
            )
        return self.get(run_id)

    def enforce_timeouts(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        """Persist timeout/retry decisions for attempts past their hard deadline."""
        now = self._clock()
        placeholder = "%s" if _use_postgres() else "?"
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""SELECT run_id, lease_owner FROM execution_runs
                    WHERE status = 'running' AND timeout_at IS NOT NULL AND timeout_at <= {placeholder}
                    ORDER BY timeout_at LIMIT {int(limit)}""",
                (now,),
            )
            due = cur.fetchall()
        updated = []
        for run_id, owner in due:
            try:
                updated.append(self.mark_timeout(run_id, owner, error="hard timeout exceeded"))
            except (LeaseConflict, InvalidTransition):
                continue
        return updated

    def recover_startup(self, *, limit: int = 1000) -> List[Dict[str, Any]]:
        """Recover expired leases after restart; never report them successful.

        Runs with retry budget return to pending with bounded exponential
        backoff.  Exhausted runs become explicitly blocked for human review.
        """
        now = self._clock()
        placeholder = "%s" if _use_postgres() else "?"
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""SELECT run_id FROM execution_runs
                    WHERE status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at < {placeholder})
                    ORDER BY updated_at LIMIT {int(limit)}""",
                (now,),
            )
            run_ids = [item[0] for item in cur.fetchall()]
        recovered = []
        for run_id in run_ids:
            with get_conn() as conn:
                cur = conn.cursor()
                try:
                    row = self._select_run(cur, run_id)
                except RunNotFound:
                    continue
                if row["status"] != "running" or (row["lease_expires_at"] or 0) >= now:
                    continue
                if self._can_retry(row):
                    delay = self._retry_delay(row)
                    self._transition(
                        cur, row, "pending", now,
                        reason="expired lease recovered after startup",
                        next_attempt_at=now + delay,
                        last_error="worker lease expired during execution",
                    )
                else:
                    self._transition(
                        cur, row, "blocked", now,
                        reason="expired lease with retry budget exhausted",
                        last_error="worker lease expired; human review required",
                    )
                    self._propagate_critical_failure(cur, row, "blocked", now)
            recovered.append(self.get(run_id))
        return recovered

    def _finish_failure(self, run_id: str, owner: str, terminal_status: str, error: str, retryable: bool) -> Dict[str, Any]:
        if terminal_status not in {"failed", "timeout"} or not error:
            raise ValueError("terminal failure status and error are required")
        now = self._clock()
        with get_conn() as conn:
            cur = conn.cursor()
            row = self._select_run(cur, run_id)
            self._require_owner(row, owner, now, allow_expired=True)
            if retryable and self._can_retry(row):
                delay = self._retry_delay(row)
                self._transition(
                    cur, row, "pending", now, reason=f"retry scheduled after {terminal_status}",
                    next_attempt_at=now + delay, last_error=error,
                )
            else:
                self._transition(cur, row, terminal_status, now, reason=error, last_error=error)
                self._propagate_critical_failure(cur, row, terminal_status, now)
        return self.get(run_id)

    @staticmethod
    def _can_retry(row: Dict[str, Any]) -> bool:
        return int(row["attempt_count"]) <= int(row["max_retries"])

    @staticmethod
    def _retry_delay(row: Dict[str, Any]) -> float:
        # attempt_count=1 is the first failed attempt and waits base seconds.
        exponent = max(0, int(row["attempt_count"]) - 1)
        return float(row["retry_backoff"]) * (2 ** exponent)

    @staticmethod
    def _check_version(row: Dict[str, Any], expected_version: Optional[int]) -> None:
        if expected_version is not None and row["version"] != expected_version:
            raise VersionConflict(f"expected version {expected_version}, found {row['version']}")

    @staticmethod
    def _require_owner(row: Dict[str, Any], owner: str, now: float, *, allow_expired: bool = False) -> None:
        if row["status"] != "running":
            raise InvalidTransition(f"run is {row['status']}, not running")
        if row["lease_owner"] != owner:
            raise LeaseConflict("run is leased by another worker")
        if not allow_expired and (row["lease_expires_at"] is None or row["lease_expires_at"] < now):
            raise LeaseConflict("run lease expired")

    @staticmethod
    def _select_run(cur, run_id: str) -> Dict[str, Any]:
        placeholder = "%s" if _use_postgres() else "?"
        cur.execute(f"SELECT * FROM execution_runs WHERE run_id = {placeholder}", (run_id,))
        row = _fetchone_dict(cur)
        if row is None:
            raise RunNotFound(run_id)
        return row

    @staticmethod
    def _insert_event(cur, run_id: str, from_status: Optional[str], to_status: str, reason: str, now: float, version: int) -> None:
        placeholder = "%s" if _use_postgres() else "?"
        cur.execute(
            f"""INSERT INTO execution_run_events
                (event_id, run_id, from_status, to_status, reason, created_at, version)
                VALUES ({','.join([placeholder] * 7)})""",
            (uuid.uuid4().hex, run_id, from_status, to_status, reason, now, version),
        )

    def _transition(
        self,
        cur,
        row: Dict[str, Any],
        to_status: str,
        now: float,
        *,
        reason: str,
        result_json: Optional[str] = None,
        last_error: Optional[str] = None,
        next_attempt_at: Optional[float] = None,
    ) -> None:
        from_status = row["status"]
        if to_status not in ALLOWED_TRANSITIONS.get(from_status, frozenset()):
            raise InvalidTransition(f"invalid run transition: {from_status} -> {to_status}")
        new_version = row["version"] + 1
        is_terminal = to_status in TERMINAL_STATUSES
        placeholder = "%s" if _use_postgres() else "?"
        cur.execute(
            f"""
            UPDATE execution_runs
               SET status = {placeholder}, result_json = COALESCE({placeholder}, result_json),
                   last_error = {placeholder}, next_attempt_at = {placeholder},
                   lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                   timeout_at = NULL, finished_at = {placeholder}, updated_at = {placeholder},
                   version = {placeholder}
             WHERE run_id = {placeholder} AND version = {placeholder}
            """,
            (
                to_status, result_json, last_error, next_attempt_at,
                now if is_terminal else None, now, new_version, row["run_id"], row["version"],
            ),
        )
        if cur.rowcount != 1:
            raise VersionConflict("run changed during transition")
        self._insert_event(cur, row["run_id"], from_status, to_status, reason, now, new_version)
        row.update({"status": to_status, "version": new_version})

    @staticmethod
    def _critical_child_failures(cur, parent_run_id: str) -> List[Dict[str, Any]]:
        placeholder = "%s" if _use_postgres() else "?"
        cur.execute(
            f"""SELECT run_id, status FROM execution_runs
                WHERE parent_run_id = {placeholder} AND critical = {placeholder} AND status <> 'succeeded'""",
            (parent_run_id, True),
        )
        return _fetchall_dict(cur)

    def _propagate_critical_failure(self, cur, child: Dict[str, Any], child_status: str, now: float) -> None:
        if not bool(child["critical"]) or not child["parent_run_id"]:
            return
        parent = self._select_run(cur, child["parent_run_id"])
        if parent["status"] not in {"pending", "running"}:
            return
        parent_status = (
            "failed"
            if parent["status"] == "running" and child_status in {"failed", "timeout"}
            else "blocked"
        )
        reason = f"critical child {child['run_id']} entered {child_status}"
        self._transition(cur, parent, parent_status, now, reason=reason, last_error=reason)


__all__ = [
    "ALLOWED_TRANSITIONS", "FAILURE_STATUSES", "RUN_STATUSES", "TERMINAL_STATUSES",
    "DurableRunRegistry", "ExecutionRunError", "IdempotencyConflict", "IdempotencyStore",
    "InvalidTransition", "LeaseConflict", "RunNotFound", "VersionConflict",
]
