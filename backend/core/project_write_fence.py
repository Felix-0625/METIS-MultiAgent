"""Project-wide write fencing for immutable QA and release evidence.

ExpertLock coordinates participating Agents by file scope, but several HTTP
and deterministic-repair writers operate directly on the project workspace.
This module supplies the common capability-token gate those writers must use.
The marker is durable so a process restart fails closed until the lease expires
or an owner holding the token explicitly releases it.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional

from core import expert_lock


DEFAULT_FENCE_TTL_SECONDS = 15 * 60
MARKER_RELATIVE_PATH = Path(".project") / "write_fence.json"

_mutex_registry_guard = threading.Lock()
_project_mutexes: Dict[str, threading.RLock] = {}
_active_writer_owners: Dict[str, tuple[tuple[str, str], int]] = {}
_writer_context_token: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("project_write_context_token", default=None)
)


class ProjectWriteFenceConflict(RuntimeError):
    """Raised when a project workspace is frozen by another writer."""


class RevokedExecutionGuard(RuntimeError):
    """Raised when a timed-out or cancelled worker attempts another write."""


class ProjectExecutionGuard:
    """Fencing generation shared by a worker and its cancellation path."""

    def __init__(
        self,
        project_id: str,
        workspace: Path,
        generation: str,
        *,
        lock_id: str = "",
        authorization_check: Optional[Callable[[], None]] = None,
    ):
        self.project_id = str(project_id)
        self.workspace = Path(workspace)
        self.generation = str(generation)
        self._lock_id = str(lock_id or "")
        self._authorization_check = authorization_check
        self._valid = True

    def bind_expert_lock(self, lock_id: str) -> None:
        """Bind this generation to the exact durable path lease it owns."""
        normalized = str(lock_id or "").strip()
        if not normalized:
            raise ValueError("execution guard requires an ExpertLock id")
        mutex = _project_mutex(self.project_id)
        with mutex:
            if self._lock_id and self._lock_id != normalized:
                raise RevokedExecutionGuard(
                    "Execution generation cannot switch ExpertLock identity"
                )
            self._lock_id = normalized

    def _active_execution_lock(self) -> Optional[Dict[str, Any]]:
        active = expert_lock.get_active_locks(project_id=self.project_id)
        if self._lock_id:
            return next(
                (
                    item
                    for item in active
                    if str(item.get("lock_id") or "") == self._lock_id
                ),
                None,
            )
        # Backward-compatible inference for callers that create the guard just
        # before claiming the run lease.  The generation is ``run_id:attempt``.
        run_id = self.generation.rsplit(":", 1)[0]
        suffix = f":run:{run_id}"
        return next(
            (
                item
                for item in active
                if str(item.get("task_id") or "").endswith(suffix)
            ),
            None,
        )

    def _assert_base_authorized(self) -> None:
        """Validate the immutable run/file lease without phase projection."""
        if not self._valid:
            raise RevokedExecutionGuard(
                f"Execution generation {self.generation} is no longer authorized"
            )
        marker = _active_marker(self.workspace)
        if marker:
            raise ProjectWriteFenceConflict(
                f"Project workspace is frozen for {marker.get('purpose') or 'exclusive QA'}"
            )
        if self._active_execution_lock() is None:
            self._valid = False
            raise RevokedExecutionGuard(
                f"Execution generation {self.generation} lost its ExpertLock lease"
            )

    def __call__(self) -> None:
        mutex = _project_mutex(self.project_id)
        with mutex:
            self._assert_base_authorized()
            if self._authorization_check is not None:
                self._authorization_check()

    @contextmanager
    def write_guard(self) -> Iterator[None]:
        """Make authorization check and one filesystem write indivisible."""
        mutex = _project_mutex(self.project_id)
        with mutex:
            self()
            yield

    @contextmanager
    def compensation_guard(self) -> Iterator[None]:
        """Authorize rollback under the old live file lease only.

        A superseded phase identity must stop new writes, but it must not stop
        the same still-leased attempt from removing its own uncommitted writes.
        """
        mutex = _project_mutex(self.project_id)
        with mutex:
            self._assert_base_authorized()
            yield

    def revoke(self) -> None:
        """Invalidate the generation before orchestration releases file locks."""
        mutex = _project_mutex(self.project_id)
        with mutex:
            self._valid = False

    @property
    def valid(self) -> bool:
        mutex = _project_mutex(self.project_id)
        with mutex:
            return self._valid

    @property
    def lock_id(self) -> str:
        mutex = _project_mutex(self.project_id)
        with mutex:
            return self._lock_id


def _project_mutex(project_id: str) -> threading.RLock:
    with _mutex_registry_guard:
        return _project_mutexes.setdefault(str(project_id), threading.RLock())


def _current_writer_owner(context_token: str) -> tuple[str, str]:
    """Distinguish asyncio tasks even when ContextVars are inherited."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is not None:
        return (context_token, f"task:{id(task)}")
    return (context_token, f"thread:{threading.get_ident()}")


def _token_digest(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _marker_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / MARKER_RELATIVE_PATH


def _read_marker(workspace: Path) -> Optional[Dict[str, Any]]:
    path = _marker_path(workspace)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProjectWriteFenceConflict(
            "Project write-fence marker is unreadable; manual recovery is required"
        ) from exc
    if not isinstance(payload, dict):
        raise ProjectWriteFenceConflict(
            "Project write-fence marker is invalid; manual recovery is required"
        )
    return payload


def _write_marker(workspace: Path, payload: Dict[str, Any]) -> None:
    path = _marker_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _active_marker(workspace: Path, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    marker = _read_marker(workspace)
    if not marker:
        return None
    current = time.time() if now is None else float(now)
    try:
        leased_until = float(marker.get("leased_until") or 0)
    except (TypeError, ValueError):
        raise ProjectWriteFenceConflict(
            "Project write-fence lease is invalid; manual recovery is required"
        )
    if leased_until > current:
        return marker
    # Expiry invalidates both capabilities.  Explicitly release the durable
    # ExpertLock before removing its marker so a crashed generation cannot
    # leave a wildcard lease blocking future writers.
    lock_id = str(marker.get("lock_id") or "")
    if lock_id:
        released = expert_lock.release_lock(lock_id)
        if not released.get("success"):
            raise ProjectWriteFenceConflict(
                "Expired project write fence could not release its ExpertLock"
            )
    _marker_path(workspace).unlink(missing_ok=True)
    return None


def acquire_project_write_fence(
    project_id: str,
    workspace: Path,
    *,
    owner: str,
    purpose: str,
    ttl_seconds: int = DEFAULT_FENCE_TTL_SECONDS,
    writer_capability: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically freeze all project writers and return a capability token."""
    normalized_project = str(project_id).strip()
    if not normalized_project:
        raise ValueError("project_id is required")
    normalized_owner = str(owner or "").strip()
    if not normalized_owner:
        raise ValueError("owner is required")
    ttl = max(30, int(ttl_seconds))
    mutex = _project_mutex(normalized_project)
    with mutex:
        active_writer = _active_writer_owners.get(normalized_project)
        if writer_capability is not None:
            if active_writer is None:
                raise ProjectWriteFenceConflict(
                    "Project writer capability is stale or no longer active"
                )
            handoff_owner = (
                _current_writer_owner(writer_capability)
                if writer_capability else None
            )
            if handoff_owner != active_writer[0]:
                raise ProjectWriteFenceConflict(
                    "Project workspace has an active write transaction"
                )
        elif active_writer is not None:
            raise ProjectWriteFenceConflict(
                "Project workspace has an active write transaction"
            )
        existing = _active_marker(workspace)
        if existing:
            raise ProjectWriteFenceConflict(
                f"Project workspace is frozen for {existing.get('purpose') or 'another operation'}"
            )
        task_id = f"project-write-fence:{purpose}"
        claim = expert_lock.atomic_claim_lock(
            expert_id=normalized_owner,
            project_id=normalized_project,
            task_id=task_id,
            file_scope=["*"],
            ttl_seconds=ttl,
        )
        if not claim.get("success"):
            raise ProjectWriteFenceConflict(
                str(claim.get("error") or "Project workspace has an active writer")
            )
        token = secrets.token_urlsafe(32)
        now = time.time()
        marker = {
            "schema_version": 1,
            "project_id": normalized_project,
            "owner": normalized_owner,
            "purpose": str(purpose or "exclusive_write"),
            "token_sha256": _token_digest(token),
            "lock_id": str(claim.get("lock_id") or ""),
            "created_at": now,
            "leased_until": now + ttl,
        }
        try:
            _write_marker(workspace, marker)
        except Exception:
            expert_lock.release_lock(marker["lock_id"])
            raise
        return {
            **marker,
            "token": token,
        }


def renew_project_write_fence(
    project_id: str,
    workspace: Path,
    token: str,
    *,
    ttl_seconds: int = DEFAULT_FENCE_TTL_SECONDS,
) -> Dict[str, Any]:
    ttl = max(30, int(ttl_seconds))
    mutex = _project_mutex(project_id)
    with mutex:
        marker = _active_marker(workspace)
        if not marker or marker.get("project_id") != str(project_id):
            raise ProjectWriteFenceConflict("Project write fence is no longer active")
        if not secrets.compare_digest(
            str(marker.get("token_sha256") or ""),
            _token_digest(token),
        ):
            raise ProjectWriteFenceConflict("Project write-fence token is invalid")
        renewed = expert_lock.renew_lock(str(marker.get("lock_id") or ""), ttl)
        if not renewed.get("success"):
            raise ProjectWriteFenceConflict(
                "Project write fence lost its underlying lease"
            )
        marker["leased_until"] = time.time() + ttl
        _write_marker(workspace, marker)
        return dict(marker)


def release_project_write_fence(
    project_id: str,
    workspace: Path,
    token: str,
) -> bool:
    mutex = _project_mutex(project_id)
    with mutex:
        marker = _read_marker(workspace)
        if not marker:
            return False
        if marker.get("project_id") != str(project_id) or not secrets.compare_digest(
            str(marker.get("token_sha256") or ""),
            _token_digest(token),
        ):
            raise ProjectWriteFenceConflict("Project write-fence token is invalid")
        lock_id = str(marker.get("lock_id") or "")
        # Keep the marker until the durable wildcard lease is confirmed
        # released.  A database/release failure must leave both writer gates
        # fail-closed rather than briefly exposing an unfenced workspace.
        if lock_id:
            released = expert_lock.release_lock(lock_id)
            if not released.get("success"):
                raise ProjectWriteFenceConflict(
                    "Project write fence ExpertLock release failed"
                )
        _marker_path(workspace).unlink(missing_ok=True)
        return True


def get_project_write_fence(
    project_id: str,
    workspace: Path,
) -> Optional[Dict[str, Any]]:
    """Return non-secret fence metadata for diagnostics and status APIs."""
    mutex = _project_mutex(project_id)
    with mutex:
        marker = _active_marker(workspace)
        if not marker or marker.get("project_id") != str(project_id):
            return None
        return {
            key: value
            for key, value in marker.items()
            if key != "token_sha256"
        }


def revoke_project_write_fence_generation(
    project_id: str,
    workspace: Path,
    *,
    expected_owner: str,
    expected_purpose: str,
    expected_lock_id: str,
) -> bool:
    """Revoke one exactly identified orphaned generation after process restart.

    This deliberately does not accept a broad administrative override.  The
    durable owner, purpose, and ExpertLock id must all match the persisted run
    that is being recovered, so an old recovery request cannot revoke a newer
    Final QA or writer generation.
    """
    mutex = _project_mutex(project_id)
    with mutex:
        marker = _read_marker(workspace)
        if not marker:
            return False
        if (
            marker.get("project_id") != str(project_id)
            or marker.get("owner") != str(expected_owner)
            or marker.get("purpose") != str(expected_purpose)
            or marker.get("lock_id") != str(expected_lock_id)
        ):
            raise ProjectWriteFenceConflict(
                "Project write fence belongs to a different generation"
            )
        lock_id = str(marker.get("lock_id") or "")
        if lock_id:
            released = expert_lock.release_lock(lock_id)
            if not released.get("success"):
                raise ProjectWriteFenceConflict(
                    "Project write fence ExpertLock release failed"
                )
        _marker_path(workspace).unlink(missing_ok=True)
        return True


@contextmanager
def project_write_guard(
    project_id: str,
    workspace: Path,
    token: Optional[str] = None,
) -> Iterator[str]:
    """Own one task-aware project write transaction.

    ``threading.RLock`` alone is insufficient when this synchronous context
    manager spans an ``await``: two asyncio tasks run on the same thread and
    would both be treated as re-entrant.  The registry below uses a task-aware
    owner identity, while ContextVar nesting keeps helper calls in one task
    re-entrant.
    """
    normalized_project = str(project_id)
    context_token = _writer_context_token.get()
    reset_token = None
    if context_token is None:
        context_token = secrets.token_urlsafe(24)
        reset_token = _writer_context_token.set(context_token)
    owner = _current_writer_owner(context_token)
    mutex = _project_mutex(normalized_project)
    entered = False
    try:
        with mutex:
            active_owner = _active_writer_owners.get(normalized_project)
            if active_owner is not None and active_owner[0] != owner:
                raise ProjectWriteFenceConflict(
                    "Project workspace has an active write transaction"
                )
            marker = _active_marker(workspace)
            if marker:
                supplied_digest = _token_digest(token) if token else ""
                if not supplied_digest or not secrets.compare_digest(
                    str(marker.get("token_sha256") or ""),
                    supplied_digest,
                ):
                    raise ProjectWriteFenceConflict(
                        f"Project workspace is frozen for {marker.get('purpose') or 'exclusive QA'}"
                    )
            elif token is not None:
                raise ProjectWriteFenceConflict(
                    "Project write-fence capability is stale or no longer active"
                )
            depth = active_owner[1] + 1 if active_owner is not None else 1
            _active_writer_owners[normalized_project] = (owner, depth)
            entered = True
        yield context_token
    finally:
        if entered:
            with mutex:
                active_owner = _active_writer_owners.get(normalized_project)
                if active_owner is not None and active_owner[0] == owner:
                    if active_owner[1] <= 1:
                        _active_writer_owners.pop(normalized_project, None)
                    else:
                        _active_writer_owners[normalized_project] = (
                            owner,
                            active_owner[1] - 1,
                        )
        if reset_token is not None:
            _writer_context_token.reset(reset_token)
