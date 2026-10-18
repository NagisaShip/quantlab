"""数据库访问层：PostgreSQL（部署）与 SQLite（单机开发）共用一套 SQL。

为什么自己写一层而不是上 ORM：
- 账本、租约与幂等键都需要精确控制事务边界与 `UPDATE ... WHERE` 条件，手写 SQL 最直接、最好审计；
- 依赖越少，私有部署越容易复现（只需要一个 PostgreSQL 驱动）。

约定
- SQL 里统一用 `?` 占位，PostgreSQL 下自动换成 `%s`；
- 金额 / 数量 / 价格列在迁移脚本里写成 `DECIMAL_T`：PostgreSQL 为 NUMERIC(38,18)，
  SQLite 为 TEXT（SQLite 的 NUMERIC 亲和性会把 "899.9" 变成二进制浮点，必须避免）；
- 时间一律是 UTC 毫秒整数；JSON 存 TEXT；
- 读出的十进制值用 `dec()` 统一转成 Decimal。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).with_name("migrations")


def now_ms() -> int:
    return int(time.time() * 1000)


def dec(value: Any) -> Decimal:
    """数据库 / JSON 里的数值 → Decimal。浮点数先转成最短十进制字符串，避免二进制误差扩散。"""
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def dstr(value: Any) -> str:
    """Decimal → 十进制字符串（去掉多余的尾随零，保留至少一位整数）。"""
    d = dec(value)
    if d == 0:
        return "0"
    s = format(d.normalize(), "f")
    return s


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def jload(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return dstr(o)
    raise TypeError(f"无法序列化 {type(o).__name__}")


class DatabaseError(RuntimeError):
    pass


class IntegrityError(DatabaseError):
    """唯一键冲突等完整性错误（两种方言统一成这一个类型）。"""


class Tx:
    """一个事务内的游标封装。"""

    def __init__(self, db: Database, conn: Any) -> None:
        self.db = db
        self.conn = conn

    def _sql(self, sql: str) -> str:
        return self.db.translate(sql)

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        """执行一条语句，返回受影响行数。"""
        try:
            cur = self.conn.cursor()
            cur.execute(self._sql(sql), self.db.params(params))
            return cur.rowcount
        except Exception as exc:  # 统一完整性错误
            raise self.db.wrap_error(exc) from exc

    def insert_id(self, sql: str, params: tuple | list = ()) -> int:
        """执行带自增主键的 INSERT，返回新 id。SQL 末尾不要写 RETURNING。"""
        try:
            cur = self.conn.cursor()
            if self.db.dialect == "postgres":
                cur.execute(self._sql(sql) + " RETURNING id", self.db.params(params))
                return int(cur.fetchone()[0])
            cur.execute(self._sql(sql), self.db.params(params))
            return int(cur.lastrowid)
        except Exception as exc:
            raise self.db.wrap_error(exc) from exc

    def fetchall(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        try:
            cur = self.conn.cursor()
            cur.execute(self._sql(sql), self.db.params(params))
            cols = [c[0] for c in cur.description] if cur.description else []
            return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]
        except Exception as exc:
            raise self.db.wrap_error(exc) from exc

    def fetchone(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: tuple | list = ()) -> Any:
        row = self.fetchone(sql, params)
        return next(iter(row.values())) if row else None


def _adapt(p: Any, dialect: str = "sqlite") -> Any:
    if isinstance(p, Decimal):
        # SQLite 存 TEXT；PostgreSQL 直接交给驱动按 NUMERIC 传输
        return p if dialect == "postgres" else dstr(p)
    if isinstance(p, bool):
        return 1 if p else 0
    if isinstance(p, (dict, list)):
        return jdump(p)
    return p


class Database:
    """连接工厂 + 事务 + 迁移。

    URL：
    - ``sqlite:///绝对或相对路径.db``（默认，单机）
    - ``postgresql://user:pass@host:port/dbname``（部署，需要 psycopg 3）
    """

    def __init__(self, url: str) -> None:
        self.url = url
        if url.startswith("sqlite:///"):
            self.dialect = "sqlite"
            self.path = url[len("sqlite:///") :]
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        elif url.startswith(("postgresql://", "postgres://")):
            self.dialect = "postgres"
            try:
                import psycopg  # noqa: F401
            except ImportError as exc:  # pragma: no cover - 部署时才会走到
                raise DatabaseError("使用 PostgreSQL 需要安装 psycopg：pip install 'psycopg[binary]'") from exc
        else:
            raise DatabaseError(f"不支持的数据库地址：{url.split('://')[0]}://…")
        self._local = threading.local()
        self._sqlite_lock = threading.RLock()

    # ---------------- 方言 ----------------
    def params(self, params: tuple | list) -> tuple:
        return tuple(_adapt(p, self.dialect) for p in params)

    def translate(self, sql: str) -> str:
        if self.dialect == "postgres":
            return sql.replace("?", "%s")
        return sql

    def wrap_error(self, exc: Exception) -> Exception:
        if isinstance(exc, DatabaseError):
            return exc
        name = type(exc).__name__
        if isinstance(exc, sqlite3.IntegrityError) or name in (
            "UniqueViolation",
            "IntegrityError",
            "ForeignKeyViolation",
        ):
            return IntegrityError(str(exc))
        return DatabaseError(f"{name}: {exc}")

    def _connect(self) -> Any:
        if self.dialect == "sqlite":
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            return conn
        import psycopg

        return psycopg.connect(self.url, autocommit=False)

    def _conn(self) -> Any:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self) -> Iterator[Tx]:
        """一个事务。嵌套调用复用外层事务（内层不单独提交）。"""
        depth = getattr(self._local, "depth", 0)
        conn = self._conn()
        if depth:
            self._local.depth = depth + 1
            try:
                yield Tx(self, conn)
            finally:
                self._local.depth -= 1
            return
        self._local.depth = 1
        if self.dialect == "sqlite":
            self._sqlite_lock.acquire()
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield Tx(self, conn)
            if self.dialect == "sqlite":
                conn.execute("COMMIT")
            else:
                conn.commit()
        except BaseException:
            try:
                if self.dialect == "sqlite":
                    conn.execute("ROLLBACK")
                else:
                    conn.rollback()
            except Exception:
                self.reset()
            raise
        finally:
            self._local.depth = 0
            if self.dialect == "sqlite":
                self._sqlite_lock.release()

    def reset(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        self._local.conn = None
        self._local.depth = 0

    # 便捷：单语句自成事务
    def execute(self, sql: str, params: tuple | list = ()) -> int:
        with self.tx() as t:
            return t.execute(sql, params)

    def fetchall(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self.tx() as t:
            return t.fetchall(sql, params)

    def fetchone(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        with self.tx() as t:
            return t.fetchone(sql, params)

    def scalar(self, sql: str, params: tuple | list = ()) -> Any:
        with self.tx() as t:
            return t.scalar(sql, params)

    # ---------------- 迁移 ----------------
    def migrate(self) -> list[str]:
        """按文件名顺序执行 migrations/*.sql 中尚未执行的脚本。返回本次执行的版本号。"""
        applied: list[str] = []
        with self.tx() as t:
            t.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at BIGINT NOT NULL)"
            )
            done = {r["version"] for r in t.fetchall("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem
            if version in done:
                continue
            script = self._dialect_sql(path.read_text(encoding="utf-8"))
            with self.tx() as t:
                # 多个进程（API、worker）同时启动时只允许一个执行迁移
                if self.dialect == "postgres":
                    t.execute("SELECT pg_advisory_xact_lock(7243001)")
                if t.fetchone("SELECT version FROM schema_migrations WHERE version=?", (version,)):
                    continue
                for stmt in _split_sql(script):
                    t.execute(stmt)
                t.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", (version, now_ms()))
            applied.append(version)
        return applied

    def _dialect_sql(self, script: str) -> str:
        if self.dialect == "postgres":
            script = script.replace("DECIMAL_T", "NUMERIC(38,18)")
            script = script.replace("AUTOINC_PK", "BIGSERIAL PRIMARY KEY")
            script = script.replace("RANDOM_HEX_8", "upper(substr(md5(random()::text), 1, 8))")
        else:
            script = script.replace("DECIMAL_T", "TEXT")
            script = script.replace("AUTOINC_PK", "INTEGER PRIMARY KEY AUTOINCREMENT")
            script = script.replace("RANDOM_HEX_8", "upper(substr(hex(randomblob(4)), 1, 8))")
        return script


def _split_sql(script: str) -> list[str]:
    script = re.sub(r"--[^\n]*", "", script)
    return [s.strip() for s in script.split(";") if s.strip()]


def default_url(home: Path) -> str:
    url = os.environ.get("QAGENT_DATABASE_URL", "").strip()
    if url:
        return url
    return f"sqlite:///{(home / 'quantlab.db').as_posix()}"
