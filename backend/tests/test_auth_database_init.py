import sqlite3

from core import auth, database


def test_resolve_jwt_secret_initializes_a_clean_database(monkeypatch, tmp_path):
    database_path = tmp_path / "fresh-auth.db"
    monkeypatch.setattr(database, "_sqlite_path", str(database_path))
    monkeypatch.setattr(auth, "JWT_SECRET", "")
    monkeypatch.delenv("JWT_SECRET", raising=False)

    secret = auth._resolve_jwt_secret()

    assert len(secret) == 64
    with sqlite3.connect(database_path) as conn:
        saved = conn.execute(
            "SELECT value FROM kv_store WHERE key = ?",
            (auth._JWT_SECRET_DB_KEY,),
        ).fetchone()
    assert saved is not None
