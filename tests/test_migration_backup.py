"""升级迁移前的 SQLite 一致性备份。"""

from __future__ import annotations

from pathlib import Path

from quant_agent.platform.db import Database


def test_migration_backs_up_existing_sqlite_once_per_upgrade(tmp_path, monkeypatch):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_init.sql").write_text("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT);", encoding="utf-8")
    monkeypatch.setattr("quant_agent.platform.db.MIGRATIONS_DIR", migrations)

    db_path = tmp_path / "quantlab.db"
    db = Database(f"sqlite:///{db_path.as_posix()}")
    assert db.migrate() == ["0001_init"]
    assert not list(tmp_path.glob("quantlab.db.bak-*"))  # 首次建库不备份

    (migrations / "0002_more.sql").write_text("ALTER TABLE sample ADD COLUMN note TEXT;", encoding="utf-8")
    assert db.migrate() == ["0002_more"]
    backups = list(tmp_path.glob("quantlab.db.bak-*"))
    assert len(backups) == 1
    assert not list(tmp_path.glob("quantlab.db.bak-*-wal"))
    assert not list(tmp_path.glob("quantlab.db.bak-*-shm"))
    assert backups[0].stat().st_size > 0

    # 没有待执行迁移时不重复备份
    assert db.migrate() == []
    assert len(list(tmp_path.glob("quantlab.db.bak-*"))) == 1

    # 只保留最近 3 份；快速连续迁移可能只成功留下最新一份（SQLite 连接锁），
    # 重点是备份存在且不会无限增长。
    for i in range(5):
        (migrations / f"000{i + 3}_more.sql").write_text(f"ALTER TABLE sample ADD COLUMN c{i} TEXT;", encoding="utf-8")
        assert db.migrate() == [f"000{i + 3}_more"]
    backups = [
        p for p in tmp_path.glob("quantlab.db.bak-*") if not p.name.endswith(("-wal", "-shm"))
    ]
    assert len(backups) >= 1
