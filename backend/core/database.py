"""
Database connection layer - PostgreSQL with SQLite fallback
Uses psycopg2 connection pool, provides sync interface.
Environment:
    DATABASE_URL    Full connection string, e.g. postgresql://user:pass@host:5432/dbname
                    (falls back to SQLite for local development)
"""

import json
import base64
import hashlib
import logging
import os
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Sentinel object: distinguishes "caller passed no default" from "caller passed None"
_SENTINEL = object()

DATABASE_URL = os.environ.get("DATABASE_URL", "")
REQUIRE_DATABASE_URL = os.environ.get("REQUIRE_DATABASE_URL", "").lower() in {"1", "true", "yes"}
DB_CONNECT_TIMEOUT_SECONDS = max(
    1, int(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "5"))
)
DB_STATEMENT_TIMEOUT_MS = max(
    1000, int(os.environ.get("DB_STATEMENT_TIMEOUT_MS", "30000"))
)
DB_LOCK_TIMEOUT_MS = max(
    100, int(os.environ.get("DB_LOCK_TIMEOUT_MS", "5000"))
)
DB_STARTUP_MAX_ATTEMPTS = max(
    1, int(os.environ.get("DB_STARTUP_MAX_ATTEMPTS", "6"))
)
DB_STARTUP_MAX_SECONDS = max(
    1.0, float(os.environ.get("DB_STARTUP_MAX_SECONDS", "45"))
)
DB_STARTUP_BASE_DELAY = max(
    0.0, float(os.environ.get("DB_STARTUP_BASE_DELAY", "0.5"))
)
DB_STARTUP_MAX_DELAY = max(
    DB_STARTUP_BASE_DELAY,
    float(os.environ.get("DB_STARTUP_MAX_DELAY", "5")),
)

# --- Connection pool (PostgreSQL) or SQLite fallback ---

_pg_pool = None
# Kept as a compatibility hook for older tests/extensions. Pool failures are
# no longer permanently latched; every bounded startup attempt may recover.
_pg_unavailable = False
_pg_pool_lock = threading.Lock()
_sqlite_path = os.environ.get(
    "SQLITE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "dagent.db"),
)


def _postgres_connection_kwargs() -> Dict[str, Any]:
    """Build bounded PostgreSQL connection/session settings."""
    kwargs: Dict[str, Any] = {
        "connect_timeout": DB_CONNECT_TIMEOUT_SECONDS,
        "options": (
            f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS} "
            f"-c lock_timeout={DB_LOCK_TIMEOUT_MS}"
        ),
    }
    sslmode = os.environ.get("DB_SSLMODE", "").strip()
    if sslmode:
        kwargs["sslmode"] = sslmode
    elif REQUIRE_DATABASE_URL:
        # Production/external deployments must not silently downgrade transport.
        kwargs["sslmode"] = "require"
    return kwargs


def _init_pg_pool():
    global _pg_pool
    if _pg_pool is not None:
        return
    with _pg_pool_lock:
        if _pg_pool is not None:
            return
        from psycopg2 import pool as pg_pool

        started = time.monotonic()
        last_error: Optional[Exception] = None
        for attempt in range(1, DB_STARTUP_MAX_ATTEMPTS + 1):
            candidate = None
            try:
                candidate = pg_pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=10,
                    dsn=DATABASE_URL,
                    **_postgres_connection_kwargs(),
                )
                # Verify the first physical connection before publishing the pool.
                conn = candidate.getconn()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT 1")
                    cur.fetchone()
                    conn.rollback()
                finally:
                    candidate.putconn(conn)
                _pg_pool = candidate
                logger.info("[PostgreSQL pool initialized] attempt=%d", attempt)
                return
            except Exception as exc:
                last_error = exc
                _pg_pool = None
                if candidate is not None:
                    try:
                        candidate.closeall()
                    except Exception:
                        logger.warning(
                            "[PostgreSQL candidate pool cleanup failed] "
                            "attempt=%d",
                            attempt,
                        )
                elapsed = time.monotonic() - started
                if (
                    attempt >= DB_STARTUP_MAX_ATTEMPTS
                    or elapsed >= DB_STARTUP_MAX_SECONDS
                ):
                    break
                delay = min(
                    DB_STARTUP_MAX_DELAY,
                    DB_STARTUP_BASE_DELAY * (2 ** (attempt - 1)),
                )
                remaining = DB_STARTUP_MAX_SECONDS - elapsed
                delay = min(delay + random.uniform(0, max(0.05, delay * 0.2)), remaining)
                if delay > 0:
                    time.sleep(delay)
        logger.error(
            "[PostgreSQL connection failed after bounded retry] attempts=%d",
            DB_STARTUP_MAX_ATTEMPTS,
        )
        raise RuntimeError("PostgreSQL is configured but unavailable") from last_error


def _use_postgres() -> bool:
    normalized = DATABASE_URL.lower()
    return normalized.startswith("postgresql://") or normalized.startswith("postgres://")


@contextmanager
def get_conn():
    """Get database connection (auto-return to pool / close SQLite conn)"""
    if _use_postgres():
        if _pg_pool is None:
            _init_pg_pool()
        if _pg_pool is None:  # defensive guard for patched/mocked pool initializers
            raise RuntimeError("PostgreSQL pool was not initialized")
        conn = _pg_pool.getconn()
        discard = False
        try:
            if getattr(conn, "closed", False):
                discard = True
                raise RuntimeError("PostgreSQL pool returned a closed connection")
            yield conn
            conn.commit()
        except Exception:
            discard = bool(getattr(conn, "closed", False))
            try:
                if not discard:
                    conn.rollback()
            except Exception:
                discard = True
            discard = discard or bool(getattr(conn, "closed", False))
            raise
        finally:
            _pg_pool.putconn(conn, close=discard)
    else:
        # SQLite fallback (local development)
        import pathlib
        pathlib.Path(_sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_sqlite_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        # 启用 WAL 模式 + 优化并发写入
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# --- Table creation (idempotent, called once on startup) ---

CREATE_TABLES_PG = """
CREATE TABLE IF NOT EXISTS kv_store (
    key     VARCHAR(256) PRIMARY KEY,
    value   TEXT         NOT NULL,
    updated_at DOUBLE PRECISION DEFAULT 0
);
"""

CREATE_TABLES_SQLITE = """
CREATE TABLE IF NOT EXISTS kv_store (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated_at REAL DEFAULT 0
);
"""

# expert_locks table: file-level lease locks
CREATE_TABLES_PG += """
CREATE TABLE IF NOT EXISTS expert_locks (
    lock_id     VARCHAR(256) PRIMARY KEY,
    expert_id   VARCHAR(256) NOT NULL,
    project_id  VARCHAR(256) NOT NULL,
    task_id     VARCHAR(256) NOT NULL,
    file_scope  TEXT         NOT NULL,
    leased_until DOUBLE PRECISION NOT NULL,
    released_at DOUBLE PRECISION,
    created_at  DOUBLE PRECISION NOT NULL
);
"""

CREATE_TABLES_SQLITE += """
CREATE TABLE IF NOT EXISTS expert_locks (
    lock_id     TEXT PRIMARY KEY,
    expert_id   TEXT NOT NULL,
    project_id  TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    file_scope  TEXT NOT NULL,
    leased_until REAL NOT NULL,
    released_at REAL,
    created_at  REAL NOT NULL
);
"""

# Durable execution primitives.  These tables intentionally live in the
# database bootstrap instead of route code so workers and maintenance tools
# observe the same schema before accepting work.
CREATE_TABLES_PG += """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     VARCHAR(128) PRIMARY KEY,
    name        VARCHAR(256) NOT NULL,
    checksum    VARCHAR(64) NOT NULL,
    applied_at  DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_runs (
    run_id              VARCHAR(64) PRIMARY KEY,
    idempotency_key     VARCHAR(256) UNIQUE NOT NULL,
    run_type            VARCHAR(128) NOT NULL,
    actor_id            VARCHAR(256) NOT NULL,
    project_id          VARCHAR(256),
    parent_run_id       VARCHAR(64) REFERENCES execution_runs(run_id),
    critical            BOOLEAN NOT NULL DEFAULT FALSE,
    status              VARCHAR(16) NOT NULL,
    payload_json        TEXT NOT NULL DEFAULT '{}',
    result_json         TEXT,
    last_error          TEXT,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    max_retries         INTEGER NOT NULL DEFAULT 0,
    retry_backoff       DOUBLE PRECISION NOT NULL DEFAULT 1,
    next_attempt_at     DOUBLE PRECISION,
    timeout_seconds     DOUBLE PRECISION NOT NULL,
    timeout_at          DOUBLE PRECISION,
    lease_owner         VARCHAR(256),
    lease_expires_at    DOUBLE PRECISION,
    heartbeat_at        DOUBLE PRECISION,
    created_at          DOUBLE PRECISION NOT NULL,
    updated_at          DOUBLE PRECISION NOT NULL,
    started_at          DOUBLE PRECISION,
    finished_at         DOUBLE PRECISION,
    version             INTEGER NOT NULL DEFAULT 1,
    CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'timeout', 'blocked', 'cancelled')),
    CHECK (attempt_count >= 0),
    CHECK (max_retries >= 0),
    CHECK (retry_backoff >= 0),
    CHECK (timeout_seconds > 0)
);
CREATE INDEX IF NOT EXISTS idx_execution_runs_claim
    ON execution_runs(status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS idx_execution_runs_lease
    ON execution_runs(status, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_execution_runs_parent
    ON execution_runs(parent_run_id, critical, status);

CREATE TABLE IF NOT EXISTS execution_run_events (
    event_id       VARCHAR(64) PRIMARY KEY,
    run_id         VARCHAR(64) NOT NULL REFERENCES execution_runs(run_id),
    from_status    VARCHAR(16),
    to_status      VARCHAR(16) NOT NULL,
    reason         TEXT,
    created_at     DOUBLE PRECISION NOT NULL,
    version        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execution_run_events_run
    ON execution_run_events(run_id, created_at);

CREATE TABLE IF NOT EXISTS idempotency_records (
    scope          VARCHAR(128) NOT NULL,
    actor_id       VARCHAR(256) NOT NULL,
    key            VARCHAR(256) NOT NULL,
    request_hash   VARCHAR(128) NOT NULL,
    status         VARCHAR(16) NOT NULL,
    resource_type  VARCHAR(128),
    resource_id    VARCHAR(256),
    response_json  TEXT,
    error_code     VARCHAR(128),
    created_at     DOUBLE PRECISION NOT NULL,
    updated_at     DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (scope, actor_id, key),
    CHECK (status IN ('reserved', 'completed', 'failed'))
);
"""

CREATE_TABLES_SQLITE += """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_runs (
    run_id              TEXT PRIMARY KEY,
    idempotency_key     TEXT UNIQUE NOT NULL,
    run_type            TEXT NOT NULL,
    actor_id            TEXT NOT NULL,
    project_id          TEXT,
    parent_run_id       TEXT REFERENCES execution_runs(run_id),
    critical            INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL,
    payload_json        TEXT NOT NULL DEFAULT '{}',
    result_json         TEXT,
    last_error          TEXT,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    max_retries         INTEGER NOT NULL DEFAULT 0,
    retry_backoff       REAL NOT NULL DEFAULT 1,
    next_attempt_at     REAL,
    timeout_seconds     REAL NOT NULL,
    timeout_at          REAL,
    lease_owner         TEXT,
    lease_expires_at    REAL,
    heartbeat_at        REAL,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,
    started_at          REAL,
    finished_at         REAL,
    version             INTEGER NOT NULL DEFAULT 1,
    CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'timeout', 'blocked', 'cancelled')),
    CHECK (attempt_count >= 0),
    CHECK (max_retries >= 0),
    CHECK (retry_backoff >= 0),
    CHECK (timeout_seconds > 0)
);
CREATE INDEX IF NOT EXISTS idx_execution_runs_claim
    ON execution_runs(status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS idx_execution_runs_lease
    ON execution_runs(status, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_execution_runs_parent
    ON execution_runs(parent_run_id, critical, status);

CREATE TABLE IF NOT EXISTS execution_run_events (
    event_id       TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES execution_runs(run_id),
    from_status    TEXT,
    to_status      TEXT NOT NULL,
    reason         TEXT,
    created_at     REAL NOT NULL,
    version        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execution_run_events_run
    ON execution_run_events(run_id, created_at);

CREATE TABLE IF NOT EXISTS idempotency_records (
    scope          TEXT NOT NULL,
    actor_id       TEXT NOT NULL,
    key            TEXT NOT NULL,
    request_hash   TEXT NOT NULL,
    status         TEXT NOT NULL,
    resource_type  TEXT,
    resource_id    TEXT,
    response_json  TEXT,
    error_code     TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    PRIMARY KEY (scope, actor_id, key),
    CHECK (status IN ('reserved', 'completed', 'failed'))
);
"""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sqlite_sql: str
    postgres_sql: str
    expected_checksum: Optional[str] = None

    def __post_init__(self) -> None:
        if (
            self.expected_checksum is not None
            and self.expected_checksum != self._calculated_checksum()
        ):
            raise RuntimeError(
                f"frozen migration changed: {self.version}"
            )

    def _calculated_checksum(self) -> str:
        payload = "\0".join(
            (self.version, self.name, self.sqlite_sql, self.postgres_sql)
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def checksum(self) -> str:
        return (
            self.expected_checksum
            if self.expected_checksum is not None
            else self._calculated_checksum()
        )


MIGRATIONS = (
    Migration(
        version="0001",
        name="baseline_application_schema",
        sqlite_sql=CREATE_TABLES_SQLITE,
        postgres_sql=CREATE_TABLES_PG,
        expected_checksum=(
            "0e37ade1c412e48064e7be9712e508941"
            "ed3cc052cce29d7e0086b7388df4e9b"
        ),
    ),
    Migration(
        version="0002",
        name="execution_run_idempotency_index",
        sqlite_sql=(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_runs_idempotency "
            "ON execution_runs(idempotency_key);"
        ),
        postgres_sql=(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_runs_idempotency "
            "ON execution_runs(idempotency_key);"
        ),
    ),
    Migration(
        version="0003",
        name="project_file_records",
        sqlite_sql="""
            CREATE TABLE IF NOT EXISTS project_files (
                project_id      TEXT NOT NULL,
                path            TEXT NOT NULL,
                content_base64  TEXT NOT NULL,
                sha256          TEXT NOT NULL,
                size_bytes      INTEGER NOT NULL,
                kind            TEXT NOT NULL,
                phase_id        TEXT,
                task_id         TEXT,
                agent_id        TEXT,
                expert_id       TEXT,
                revision        INTEGER NOT NULL,
                created_at      REAL NOT NULL,
                updated_at      REAL NOT NULL,
                PRIMARY KEY (project_id, path)
            );
            CREATE INDEX IF NOT EXISTS idx_project_files_phase
                ON project_files(project_id, phase_id, task_id);
        """,
        postgres_sql="""
            CREATE TABLE IF NOT EXISTS project_files (
                project_id      VARCHAR(256) NOT NULL,
                path            TEXT NOT NULL,
                content_base64  TEXT NOT NULL,
                sha256          VARCHAR(64) NOT NULL,
                size_bytes      BIGINT NOT NULL,
                kind            VARCHAR(64) NOT NULL,
                phase_id        VARCHAR(256),
                task_id         VARCHAR(256),
                agent_id        VARCHAR(256),
                expert_id       VARCHAR(256),
                revision        INTEGER NOT NULL,
                created_at      DOUBLE PRECISION NOT NULL,
                updated_at      DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (project_id, path)
            );
            CREATE INDEX IF NOT EXISTS idx_project_files_phase
                ON project_files(project_id, phase_id, task_id);
        """,
    ),
)

_migration_lock = threading.RLock()


def _execute_sqlite_statements(cursor, sql: str) -> None:
    """Execute simple DDL statements without sqlite3.executescript auto-commit."""
    for statement in sql.split(";"):
        statement = statement.strip()
        if statement:
            cursor.execute(statement)


def _ensure_migration_history_schema(cursor, *, postgres: bool) -> None:
    if postgres:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(128) PRIMARY KEY,
                name VARCHAR(256) NOT NULL,
                checksum VARCHAR(64),
                applied_at DOUBLE PRECISION NOT NULL
            )
            """
        )
        cursor.execute(
            "ALTER TABLE schema_migrations "
            "ADD COLUMN IF NOT EXISTS checksum VARCHAR(64)"
        )
        return

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT,
            applied_at REAL NOT NULL
        )
        """
    )
    columns = {
        str(row[1])
        for row in cursor.execute("PRAGMA table_info(schema_migrations)")
    }
    if "checksum" not in columns:
        cursor.execute("ALTER TABLE schema_migrations ADD COLUMN checksum TEXT")


_EXECUTION_RUN_SQLITE_COLUMNS = {
    "run_type": "TEXT NOT NULL DEFAULT 'legacy'",
    "actor_id": "TEXT NOT NULL DEFAULT 'system'",
    "project_id": "TEXT",
    "payload_json": "TEXT NOT NULL DEFAULT '{}'",
    "result_json": "TEXT",
    "last_error": "TEXT",
    "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "max_retries": "INTEGER NOT NULL DEFAULT 0",
    "retry_backoff": "REAL NOT NULL DEFAULT 1",
    "timeout_seconds": "REAL NOT NULL DEFAULT 300",
    "timeout_at": "REAL",
    "lease_owner": "TEXT",
    "heartbeat_at": "REAL",
    "updated_at": "REAL NOT NULL DEFAULT 0",
    "started_at": "REAL",
    "finished_at": "REAL",
    "version": "INTEGER NOT NULL DEFAULT 1",
}


def _repair_legacy_execution_runs(cursor, *, postgres: bool) -> None:
    """Upgrade the pre-migration execution_runs shape before creating indexes."""
    if postgres:
        cursor.execute("SELECT to_regclass('public.execution_runs')")
        if not cursor.fetchone()[0]:
            return
        for column, definition in _EXECUTION_RUN_SQLITE_COLUMNS.items():
            pg_definition = (
                definition
                .replace("INTEGER", "INTEGER")
                .replace("REAL", "DOUBLE PRECISION")
            )
            cursor.execute(
                f"ALTER TABLE execution_runs ADD COLUMN IF NOT EXISTS "
                f"{column} {pg_definition}"
            )
        return

    exists = cursor.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='execution_runs'"
    ).fetchone()
    if not exists:
        return
    columns = {
        str(row[1])
        for row in cursor.execute("PRAGMA table_info(execution_runs)")
    }
    for column, definition in _EXECUTION_RUN_SQLITE_COLUMNS.items():
        if column not in columns:
            cursor.execute(
                f"ALTER TABLE execution_runs ADD COLUMN {column} {definition}"
            )


def _run_migrations(conn, *, postgres: bool) -> None:
    cursor = conn.cursor()
    if postgres:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (0x4D65546973,))
        _ensure_migration_history_schema(cursor, postgres=True)
    else:
        _ensure_migration_history_schema(cursor, postgres=False)
        conn.commit()
        cursor.execute("BEGIN IMMEDIATE")

    cursor.execute("SELECT version, name, checksum FROM schema_migrations")
    applied_rows = cursor.fetchall()
    applied = {
        str(row[0]): (str(row[1]), str(row[2] or ""))
        for row in applied_rows
    }
    known = {migration.version: migration for migration in MIGRATIONS}
    unknown = sorted(set(applied) - set(known))
    if unknown:
        raise RuntimeError(
            f"database contains unknown migrations: {', '.join(unknown)}"
        )

    for migration in MIGRATIONS:
        existing = applied.get(migration.version)
        if existing:
            existing_name, checksum = existing
            if checksum != migration.checksum:
                if (
                    migration.version == "0001"
                    and existing_name == "baseline"
                    and not checksum
                ):
                    _repair_legacy_execution_runs(cursor, postgres=postgres)
                    sql = (
                        migration.postgres_sql
                        if postgres
                        else migration.sqlite_sql
                    )
                    if postgres:
                        cursor.execute(sql)
                        cursor.execute(
                            """
                            UPDATE schema_migrations
                            SET name=%s, checksum=%s, applied_at=%s
                            WHERE version=%s
                            """,
                            (
                                migration.name,
                                migration.checksum,
                                time.time(),
                                migration.version,
                            ),
                        )
                    else:
                        _execute_sqlite_statements(cursor, sql)
                        cursor.execute(
                            """
                            UPDATE schema_migrations
                            SET name=?, checksum=?, applied_at=?
                            WHERE version=?
                            """,
                            (
                                migration.name,
                                migration.checksum,
                                time.time(),
                                migration.version,
                            ),
                        )
                    continue
                raise RuntimeError(
                    f"migration checksum mismatch: {migration.version}"
                )
            continue
        if migration.version == "0001":
            _repair_legacy_execution_runs(cursor, postgres=postgres)
        sql = migration.postgres_sql if postgres else migration.sqlite_sql
        if postgres:
            cursor.execute(sql)
            cursor.execute(
                """
                INSERT INTO schema_migrations
                    (version, name, checksum, applied_at)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    time.time(),
                ),
            )
        else:
            _execute_sqlite_statements(cursor, sql)
            cursor.execute(
                """
                INSERT INTO schema_migrations
                    (version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    time.time(),
                ),
            )


def init_db() -> None:
    """Create tables (idempotent). Call on app startup."""
    if DATABASE_URL and not _use_postgres():
        raise RuntimeError("Unsupported DATABASE_URL scheme; expected postgresql:// or postgres://")
    if REQUIRE_DATABASE_URL and not _use_postgres():
        raise RuntimeError("DATABASE_URL is required in this environment; refusing SQLite fallback")
    if _use_postgres() and _pg_pool is None:
        _init_pg_pool()
    if REQUIRE_DATABASE_URL and not _use_postgres():
        raise RuntimeError("DATABASE_URL is required in this environment; PostgreSQL connection unavailable")
    with _migration_lock:
        with get_conn() as conn:
            _run_migrations(conn, postgres=_use_postgres())
    logger.info("[DB] table init done (%s)", "PostgreSQL" if _use_postgres() else "SQLite")


# --- KV operations (all persistent data stored as key -> JSON value) ---

def kv_set(key: str, value: Any) -> None:
    """Store an arbitrary serializable object in kv_store"""
    serialized = json.dumps(value, ensure_ascii=False)
    now = time.time()
    with get_conn() as conn:
        cur = conn.cursor()
        if _use_postgres():
            cur.execute(
                """
                INSERT INTO kv_store (key, value, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (key) DO UPDATE
                  SET value = EXCLUDED.value,
                      updated_at = EXCLUDED.updated_at
                """,
                (key, serialized, now),
            )
        else:
            cur.execute(
                "INSERT OR REPLACE INTO kv_store (key, value, updated_at) VALUES (?, ?, ?)",
                (key, serialized, now),
            )


def kv_many_set(values: Dict[str, Any]) -> None:
    """Store multiple KV entries in one database transaction."""
    if not values:
        return
    now = time.time()
    rows = [(key, json.dumps(value, ensure_ascii=False), now) for key, value in values.items()]
    with get_conn() as conn:
        cur = conn.cursor()
        if _use_postgres():
            cur.executemany(
                """
                INSERT INTO kv_store (key, value, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (key) DO UPDATE
                  SET value = EXCLUDED.value,
                      updated_at = EXCLUDED.updated_at
                """,
                rows,
            )
        else:
            cur.executemany(
                "INSERT OR REPLACE INTO kv_store (key, value, updated_at) VALUES (?, ?, ?)",
                rows,
            )


def kv_get(key: str, default: Any = _SENTINEL) -> Any:
    """Read a value from kv_store; return default if not found.

    Note: default=None is a valid sentinel value — passing None will return
    None (not {}) when the key is absent.  The internal _SENTINEL allows
    distinguishing "caller passed no default" from "caller passed None".
    """
    with get_conn() as conn:
        cur = conn.cursor()
        if _use_postgres():
            cur.execute("SELECT value FROM kv_store WHERE key = %s", (key,))
        else:
            cur.execute("SELECT value FROM kv_store WHERE key = ?", (key,))
        row = cur.fetchone()
    if row is None:
        return {} if default is _SENTINEL else default
    try:
        return json.loads(row[0])
    except Exception as e:
        logger.warning("kv_get deserialization failed key=%s: %s", key, e)
        return {} if default is _SENTINEL else default


def kv_delete(key: str) -> None:
    """Delete a key from kv_store"""
    with get_conn() as conn:
        cur = conn.cursor()
        if _use_postgres():
            cur.execute("DELETE FROM kv_store WHERE key = %s", (key,))
        else:
            cur.execute("DELETE FROM kv_store WHERE key = ?", (key,))


def kv_keys_prefix(prefix: str):
    """List all keys starting with the given prefix"""
    with get_conn() as conn:
        cur = conn.cursor()
        # Avoid LIKE because %, _ and the escape character would otherwise
        # change the meaning of a literal KV prefix.
        if _use_postgres():
            cur.execute("SELECT key FROM kv_store WHERE LEFT(key, %s) = %s", (len(prefix), prefix))
        else:
            cur.execute("SELECT key FROM kv_store WHERE substr(key, 1, ?) = ?", (len(prefix), prefix))
        return [row[0] for row in cur.fetchall()]


def _project_file_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\x00" in path
        or any(part in {"", ".", ".."} for part in parts)
        or (len(parts[0]) == 2 and parts[0][1] == ":")
    ):
        raise ValueError(f"unsafe project file path: {value!r}")
    return "/".join(parts)


def commit_project_files(
    project_id: str,
    records: Dict[str, Dict[str, Any]],
    *,
    expected_sha256: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Atomically persist authoritative project files and their provenance."""
    if not project_id or not records:
        raise ValueError("project_id and records are required")
    normalized = {}
    for raw_path, raw_record in records.items():
        path = _project_file_path(raw_path)
        content = raw_record.get("content")
        if not isinstance(content, bytes):
            raise TypeError(f"project file content must be bytes: {path}")
        normalized[path] = {**raw_record, "content": content}
    expected = {
        _project_file_path(path): value
        for path, value in (expected_sha256 or {}).items()
    }
    now = time.time()
    result: Dict[str, Dict[str, Any]] = {}
    with get_conn() as conn:
        cur = conn.cursor()
        if _use_postgres():
            lock_key = int.from_bytes(
                hashlib.sha256(project_id.encode("utf-8")).digest()[:8],
                "big",
                signed=True,
            )
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
            cur.execute(
                "SELECT path, sha256, revision, created_at FROM project_files "
                "WHERE project_id = %s",
                (project_id,),
            )
        else:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                "SELECT path, sha256, revision, created_at FROM project_files "
                "WHERE project_id = ?",
                (project_id,),
            )
        existing = {
            str(row[0]): {
                "sha256": str(row[1]),
                "revision": int(row[2]),
                "created_at": float(row[3]),
            }
            for row in cur.fetchall()
        }
        for path, expected_hash in expected.items():
            actual = existing.get(path)
            actual_hash = actual["sha256"] if actual else None
            if actual_hash != expected_hash:
                raise RuntimeError(
                    f"project file changed concurrently: {project_id}/{path}"
                )
        for path, record in normalized.items():
            content = record["content"]
            digest = hashlib.sha256(content).hexdigest()
            prior = existing.get(path)
            revision = (
                prior["revision"]
                if prior and prior["sha256"] == digest
                else int(prior["revision"] if prior else 0) + 1
            )
            created_at = prior["created_at"] if prior else now
            values = (
                project_id,
                path,
                base64.b64encode(content).decode("ascii"),
                digest,
                len(content),
                str(record.get("kind") or "agent_output"),
                str(record.get("phase_id") or "") or None,
                str(record.get("task_id") or "") or None,
                str(record.get("agent_id") or "") or None,
                str(record.get("expert_id") or "") or None,
                revision,
                created_at,
                now,
            )
            if _use_postgres():
                cur.execute(
                    """
                    INSERT INTO project_files
                        (project_id, path, content_base64, sha256, size_bytes,
                         kind, phase_id, task_id, agent_id, expert_id, revision,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (project_id, path) DO UPDATE SET
                        content_base64=EXCLUDED.content_base64,
                        sha256=EXCLUDED.sha256,
                        size_bytes=EXCLUDED.size_bytes,
                        kind=EXCLUDED.kind,
                        phase_id=EXCLUDED.phase_id,
                        task_id=EXCLUDED.task_id,
                        agent_id=EXCLUDED.agent_id,
                        expert_id=EXCLUDED.expert_id,
                        revision=EXCLUDED.revision,
                        updated_at=EXCLUDED.updated_at
                    """,
                    values,
                )
            else:
                cur.execute(
                    """
                    INSERT INTO project_files
                        (project_id, path, content_base64, sha256, size_bytes,
                         kind, phase_id, task_id, agent_id, expert_id, revision,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_id, path) DO UPDATE SET
                        content_base64=excluded.content_base64,
                        sha256=excluded.sha256,
                        size_bytes=excluded.size_bytes,
                        kind=excluded.kind,
                        phase_id=excluded.phase_id,
                        task_id=excluded.task_id,
                        agent_id=excluded.agent_id,
                        expert_id=excluded.expert_id,
                        revision=excluded.revision,
                        updated_at=excluded.updated_at
                    """,
                    values,
                )
            result[path] = {
                "path": path,
                "sha256": digest,
                "size_bytes": len(content),
                "revision": revision,
                "kind": str(record.get("kind") or "agent_output"),
            }
    return result


def load_project_files(project_id: str) -> Dict[str, Dict[str, Any]]:
    """Load and integrity-check authoritative project files."""
    with get_conn() as conn:
        cur = conn.cursor()
        placeholder = "%s" if _use_postgres() else "?"
        cur.execute(
            "SELECT path, content_base64, sha256, size_bytes, kind, phase_id, "
            "task_id, agent_id, expert_id, revision FROM project_files "
            f"WHERE project_id = {placeholder}",
            (project_id,),
        )
        rows = cur.fetchall()
    result = {}
    for row in rows:
        path = _project_file_path(row[0])
        content = base64.b64decode(row[1], validate=True)
        digest = hashlib.sha256(content).hexdigest()
        if digest != str(row[2]) or len(content) != int(row[3]):
            raise RuntimeError(f"corrupt project file record: {project_id}/{path}")
        result[path] = {
            "content": content,
            "sha256": digest,
            "size_bytes": len(content),
            "kind": str(row[4]),
            "phase_id": row[5],
            "task_id": row[6],
            "agent_id": row[7],
            "expert_id": row[8],
            "revision": int(row[9]),
        }
    return result


def delete_project_files(project_id: str) -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        placeholder = "%s" if _use_postgres() else "?"
        cur.execute(
            f"DELETE FROM project_files WHERE project_id = {placeholder}",
            (project_id,),
        )


def database_healthcheck() -> Dict[str, Any]:
    """Run the same lightweight readiness probe on SQLite and PostgreSQL."""
    started = time.monotonic()
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            row = cur.fetchone()
        if row is None or int(row[0]) != 1:
            raise RuntimeError("database readiness query returned no row")
        return {
            "healthy": True,
            "backend": "postgresql" if _use_postgres() else "sqlite",
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
        }
    except Exception:
        logger.exception("Database readiness probe failed")
        return {
            "healthy": False,
            "backend": "postgresql" if _use_postgres() else "sqlite",
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
        }
