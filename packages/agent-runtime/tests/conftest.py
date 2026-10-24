"""Run the same kernel contracts against SQLite and an optional real PostgreSQL."""

import os
import uuid

import pytest

PG = os.environ.get("QAGENT_TEST_PG", "").strip()


def pytest_generate_tests(metafunc):
    if metafunc.module.__name__ in {
        "test_engine",
        "test_process_crash",
        "test_fault_matrix",
        "test_telemetry",
        "test_children",
        "test_dependencies",
    }:
        metafunc.parametrize("persistence_backend", ["sqlite", *(["postgres"] if PG else [])], indirect=True)


@pytest.fixture(autouse=True)
def persistence_backend(request, monkeypatch):
    if getattr(request, "param", "sqlite") == "sqlite":
        yield
        return
    import psycopg
    from psycopg import sql

    schema = "fault_" + uuid.uuid4().hex
    original = request.module.Journal

    class PostgresJournal(original):
        def __init__(self, path, **kwargs):
            super().__init__(PG, schema=schema)

    monkeypatch.setattr(request.module, "Journal", PostgresJournal)
    monkeypatch.setenv("QAGENT_TEST_RUNTIME_DB", PG)
    monkeypatch.setenv("QAGENT_TEST_RUNTIME_SCHEMA", schema)
    try:
        yield
    finally:
        with psycopg.connect(PG, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
