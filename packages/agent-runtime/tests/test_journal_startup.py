"""SQLite cold-start contention retries only actual busy/locked errors."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_runtime import Journal
from agent_runtime.journal import _DB


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_CORRUPT])
def test_startup_recovers_from_contention_but_does_not_mask_corruption(tmp_path, monkeypatch, code):
    original = _DB.execute
    attempts = []

    def execute(self, sql, params=()):
        if sql == "PRAGMA journal_mode=WAL":
            attempts.append(sql)
            if len(attempts) == 1:
                error = sqlite3.OperationalError("injected startup fault")
                error.sqlite_errorcode = code
                raise error
        return original(self, sql, params)

    monkeypatch.setattr(_DB, "execute", execute)
    if code == sqlite3.SQLITE_CORRUPT:
        with pytest.raises(sqlite3.OperationalError):
            Journal(tmp_path / "journal.sqlite")
        assert len(attempts) == 1
    else:
        journal = Journal(tmp_path / "journal.sqlite")
        assert len(attempts) == 2
        with journal.connection() as db:
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_reused_connections_keep_readonly_and_rollback_boundaries(tmp_path):
    journal = Journal(tmp_path / "pooled.sqlite")
    with journal.read_tx() as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO resource_policy_meta(id,version) VALUES(99,1)")
    with journal.tx() as db:
        db.execute("INSERT INTO resource_policy_meta(id,version) VALUES(99,1)")
    with journal.connection() as db:
        db.execute("BEGIN")
        db.execute("UPDATE resource_policy_meta SET version=2 WHERE id=99")
    with journal.read_tx() as db:
        assert db.execute("SELECT version FROM resource_policy_meta WHERE id=99").fetchone()["version"] == 1
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
    journal.close()
    with pytest.raises(RuntimeError, match="closed"):
        with journal.connection():
            pass
    with Journal(journal.path).read_tx() as db:
        assert db.execute("SELECT version FROM resource_policy_meta WHERE id=99").fetchone()["version"] == 1


def test_pool_bounds_idle_handles_and_close_drains_active_borrowers(tmp_path):
    from contextlib import ExitStack

    journal = Journal(tmp_path / "bounded.sqlite")
    with ExitStack() as stack:
        borrowers = [stack.enter_context(journal.connection()) for _ in range(8)]
        assert len({id(db.raw) for db in borrowers}) == 8
    assert len(journal._sqlite_pool) == 4
    with journal.connection() as active:
        raw = active.raw
        journal.close()
        assert active.execute("SELECT 1").fetchone()[0] == 1
    assert journal._sqlite_pool == []
    with pytest.raises(sqlite3.ProgrammingError):
        raw.execute("SELECT 1")
