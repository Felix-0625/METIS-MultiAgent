import sqlite3
import threading
from pathlib import Path

import pytest

from core import database
from core.execution_runs import DurableRunRegistry


@pytest.fixture()
def isolated_database(monkeypatch, tmp_path):
    path = tmp_path / "migration.db"
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setattr(database, "_pg_pool", None)
    monkeypatch.setattr(database, "_sqlite_path", str(path))
    return path


def test_versioned_migrations_upgrade_a_drifted_schema(isolated_database):
    with sqlite3.connect(isolated_database) as conn:
        conn.execute(
            """
            CREATE TABLE execution_runs (
                run_id TEXT PRIMARY KEY,
                idempotency_key TEXT,
                status TEXT,
                next_attempt_at REAL,
                created_at REAL,
                lease_expires_at REAL,
                parent_run_id TEXT,
                critical INTEGER
            )
            """
        )
        conn.execute(
            """
            INSERT INTO execution_runs
                (run_id, idempotency_key, status, created_at, critical)
            VALUES ('legacy-run', 'legacy-key', 'blocked', 1, 0)
            """
        )

    database.init_db()

    with sqlite3.connect(isolated_database) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(execution_runs)")
        }
        migrations = conn.execute(
            "SELECT version, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()

    assert {"run_type", "actor_id", "payload_json", "version"} <= columns
    assert len(migrations) == len(database.MIGRATIONS)
    assert all(checksum for _, checksum in migrations)
    run, created = DurableRunRegistry().create_or_get_run(
        idempotency_key="new-run",
        run_type="audit",
        actor_id="audit-user",
        timeout_seconds=10,
    )
    assert created
    assert run["run_type"] == "audit"


def test_migrations_are_idempotent_and_checksum_protected(isolated_database):
    database.init_db()
    database.init_db()

    with sqlite3.connect(isolated_database) as conn:
        count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        conn.execute(
            "UPDATE schema_migrations SET checksum = 'tampered' "
            "WHERE version = (SELECT MIN(version) FROM schema_migrations)"
        )

    assert count == len(database.MIGRATIONS)
    with pytest.raises(RuntimeError, match="checksum"):
        database.init_db()


def test_frozen_baseline_allows_n_to_n_plus_one_upgrade(
    isolated_database, monkeypatch
):
    database.init_db()
    baseline = database.MIGRATIONS[0]
    next_version = f"{int(database.MIGRATIONS[-1].version) + 1:04d}"
    next_migration = database.Migration(
        version=next_version,
        name="add_upgrade_probe",
        sqlite_sql="CREATE TABLE upgrade_probe (id TEXT PRIMARY KEY);",
        postgres_sql=(
            "CREATE TABLE upgrade_probe "
            "(id VARCHAR(64) PRIMARY KEY);"
        ),
    )
    monkeypatch.setattr(
        database,
        "MIGRATIONS",
        database.MIGRATIONS + (next_migration,),
    )

    database.init_db()

    with sqlite3.connect(isolated_database) as conn:
        rows = conn.execute(
            "SELECT version, checksum FROM schema_migrations "
            "ORDER BY version"
        ).fetchall()
        table = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='upgrade_probe'"
        ).fetchone()

    assert rows == [
        (migration.version, migration.checksum)
        for migration in database.MIGRATIONS
    ]
    assert rows[0] == ("0001", baseline.checksum)
    assert rows[-1] == (next_version, next_migration.checksum)
    assert table is not None


def test_frozen_baseline_detects_accidental_sql_mutation():
    baseline = database.MIGRATIONS[0]

    with pytest.raises(RuntimeError, match="frozen migration changed"):
        database.Migration(
            version=baseline.version,
            name=baseline.name,
            sqlite_sql=baseline.sqlite_sql + "\n-- accidental mutation",
            postgres_sql=baseline.postgres_sql,
            expected_checksum=baseline.checksum,
        )


def test_failed_migration_rolls_back_schema_and_history(
    isolated_database, monkeypatch
):
    bad = database.Migration(
        version="9999",
        name="intentional_failure",
        sqlite_sql=(
            "CREATE TABLE must_rollback (id TEXT PRIMARY KEY);"
            "INSERT INTO table_that_does_not_exist VALUES (1);"
        ),
        postgres_sql=(
            "CREATE TABLE must_rollback (id TEXT PRIMARY KEY);"
            "INSERT INTO table_that_does_not_exist VALUES (1);"
        ),
    )
    monkeypatch.setattr(database, "MIGRATIONS", database.MIGRATIONS + (bad,))

    with pytest.raises(Exception):
        database.init_db()

    with sqlite3.connect(isolated_database) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='must_rollback'"
        ).fetchone()
        record = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version='9999'"
        ).fetchone()
    assert table is None
    assert record is None


def test_sqlite_concurrent_startup_applies_each_migration_once(
    isolated_database,
):
    barrier = threading.Barrier(2)
    failures = []

    def initialize():
        try:
            barrier.wait(timeout=2)
            database.init_db()
        except Exception as exc:  # pragma: no cover - assertion reports details
            failures.append(exc)

    threads = [threading.Thread(target=initialize) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not failures
    with sqlite3.connect(isolated_database) as conn:
        rows = conn.execute(
            "SELECT version, COUNT(*) FROM schema_migrations "
            "GROUP BY version HAVING COUNT(*) != 1"
        ).fetchall()
    assert rows == []


def test_postgres_connection_policy_is_bounded_and_tls_enabled(
    monkeypatch,
):
    monkeypatch.setattr(
        database,
        "DATABASE_URL",
        "postgresql://user:password@db.example.invalid/metis",
    )
    monkeypatch.setattr(database, "REQUIRE_DATABASE_URL", True)
    monkeypatch.setattr(database, "DB_CONNECT_TIMEOUT_SECONDS", 7)
    monkeypatch.setattr(database, "DB_STATEMENT_TIMEOUT_MS", 12000)
    monkeypatch.setattr(database, "DB_LOCK_TIMEOUT_MS", 3000)
    monkeypatch.delenv("DB_SSLMODE", raising=False)

    policy = database._postgres_connection_kwargs()

    assert policy["connect_timeout"] == 7
    assert policy["sslmode"] == "require"
    assert "statement_timeout=12000" in policy["options"]
    assert "lock_timeout=3000" in policy["options"]


def test_postgres_migrations_do_not_chain_execute_and_fetchall(monkeypatch):
    class PsycopgCursor:
        def __init__(self):
            self.statements = []

        def execute(self, statement, params=None):
            self.statements.append((statement, params))
            return None

        def fetchall(self):
            return []

    class PsycopgConnection:
        def __init__(self):
            self.cursor_instance = PsycopgCursor()

        def cursor(self):
            return self.cursor_instance

    connection = PsycopgConnection()
    monkeypatch.setattr(database, "MIGRATIONS", ())

    database._run_migrations(connection, postgres=True)

    assert any(
        "SELECT version, name, checksum FROM schema_migrations" in statement
        for statement, _params in connection.cursor_instance.statements
    )


def test_postgres_business_exception_keeps_healthy_connection(monkeypatch):
    class FakeConnection:
        closed = 0

        def __init__(self):
            self.rollbacks = 0

        def rollback(self):
            self.rollbacks += 1

        def commit(self):
            raise AssertionError("business failure must not commit")

    class FakePool:
        def __init__(self):
            self.connection = FakeConnection()
            self.close_flags = []

        def getconn(self):
            return self.connection

        def putconn(self, _connection, close=False):
            self.close_flags.append(close)

    pool = FakePool()
    monkeypatch.setattr(database, "DATABASE_URL", "postgresql://mock/metis")
    monkeypatch.setattr(database, "_pg_pool", pool)

    with pytest.raises(ValueError, match="expected rejection"):
        with database.get_conn():
            raise ValueError("expected rejection")

    assert pool.connection.rollbacks == 1
    assert pool.close_flags == [False]


def test_postgres_failed_rollback_discards_connection(monkeypatch):
    class BrokenConnection:
        closed = 0

        def rollback(self):
            raise RuntimeError("connection lost")

    class FakePool:
        def __init__(self):
            self.close_flags = []

        def getconn(self):
            return BrokenConnection()

        def putconn(self, _connection, close=False):
            self.close_flags.append(close)

    pool = FakePool()
    monkeypatch.setattr(database, "DATABASE_URL", "postgresql://mock/metis")
    monkeypatch.setattr(database, "_pg_pool", pool)

    with pytest.raises(ValueError, match="expected rejection"):
        with database.get_conn():
            raise ValueError("expected rejection")

    assert pool.close_flags == [True]


def test_postgres_failed_initial_probe_closes_candidate_pool(monkeypatch):
    from psycopg2 import pool as pg_pool

    class ProbeCursor:
        def execute(self, _sql):
            raise RuntimeError("probe failed")

    class ProbeConnection:
        closed = 0

        def cursor(self):
            return ProbeCursor()

    class CandidatePool:
        def __init__(self):
            self.closed = False

        def getconn(self):
            return ProbeConnection()

        def putconn(self, _connection):
            return None

        def closeall(self):
            self.closed = True

    candidate = CandidatePool()
    monkeypatch.setattr(
        pg_pool,
        "ThreadedConnectionPool",
        lambda *args, **kwargs: candidate,
    )
    monkeypatch.setattr(database, "DATABASE_URL", "postgresql://mock/metis")
    monkeypatch.setattr(database, "_pg_pool", None)
    monkeypatch.setattr(database, "DB_STARTUP_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(database, "DB_STARTUP_MAX_SECONDS", 1)

    with pytest.raises(RuntimeError, match="PostgreSQL is configured"):
        database._init_pg_pool()

    assert candidate.closed
