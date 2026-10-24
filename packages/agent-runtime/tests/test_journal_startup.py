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
