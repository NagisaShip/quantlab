"""真实记录服务：导入、只读同步、对照报告。

真实数据与模拟数据**分开存放**：真实成交的 runs.mode 是 imported_live / readonly_live / testnet，
fills.source 同步标注；任何把模拟结果冒充真实结果的路径都不存在。
没有真实成交时，`status()` 返回 has_live_data=false，前端显示“未接入真实成交”，不得用模拟曲线补位。
"""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from typing import Any

from ..engine.metrics import fee_summary
from ..platform.db import Database, IntegrityError, dec, jdump, jload, now_ms
from ..platform.errors import AppError, ErrorCode
from ..platform.runs import COMPLETED, RunService
from ..sdk.ledger import dstr
from .attribution import attribute
from .imports import parse_csv

LIVE_MODES = ("imported_live", "readonly_live")
TEST_MODES = ("testnet",)


def _fill_key(r: dict[str, Any]) -> str:
    """真实成交的去重键。

    有交易所成交号时用它（最可靠）。没有时用**成交内容**的哈希，且**不含行号**——
    否则同一笔成交分两个文件导入会被当成两笔，收益被重复计入。

    代价说清楚：两笔字段完全相同的成交（同毫秒、同标的、同方向、同数量、同价格）
    在无成交号时无法区分，会被当成同一笔去重。对账场景下宁可少算也不重复计入，
    并且导入报告里会提示"缺少成交号，已按内容去重"。
    """
    if r.get("fill_id"):
        return f"x:{r['fill_id']}"
    raw = f"{r['ts']}|{r['instrument']}|{r['side']}|{r['qty']}|{r['price']}"
    return "h:" + hashlib.sha256(raw.encode()).hexdigest()[:20]


def wire_fill(r: dict[str, Any], key: str, *, account_id: str = "") -> dict[str, Any]:
    """真实成交 → fills 表统一形状（金额一律十进制字符串）。

    `account_id` 记录这笔成交来自哪个账户（只读连接 ID）。归因的启发式匹配把它当作
    硬性维度：同一策略挂在多个账户上时，少了这一维就会把 A 账户的成交算到 B 账户头上。
    """
    return {
        "fill_id": key,
        "signal_id": None,
        "order_id": r.get("order_id"),
        "client_order_id": r.get("client_order_id"),
        "security": r["instrument"],
        "instrument": r["instrument"],
        "side": r["side"],
        "amount": dstr(dec(r["qty"])),
        "qty": dstr(dec(r["qty"])),
        "price": dstr(dec(r["price"])),
        "fee": dstr(dec(r.get("fee") or 0)),
        "fee_currency": (r.get("fee_currency") or r["instrument"].split("/")[-1]).upper(),
        "ts": int(r["ts"]),
        "exchange_fill_id": r.get("fill_id"),
        "account_id": account_id or r.get("account_id") or "",
    }


class LiveService:
    def __init__(self, db: Database, runs: RunService) -> None:
        self.db = db
        self.runs = runs

    # ------------------------------------------------------------------ 导入
    def create_import(
        self,
        *,
        workspace_id: str,
        user_id: str,
        kind: str,
        filename: str,
        content: str | bytes,
        mapping: dict[str, str] | None,
        environment: str = "live",
        exchange: str = "",
    ) -> dict[str, Any]:
        res = parse_csv(kind, content, mapping=mapping)
        dup = self.db.fetchone(
            "SELECT id FROM imports WHERE workspace_id=? AND kind=? AND digest=? AND status='done'",
            (workspace_id, kind, res.digest),
        )
        if dup:
            raise AppError(ErrorCode.CONFLICT, "同一份文件已经导入过", details={"import_id": dup["id"]})
        mode = "testnet" if environment == "testnet" else "imported_live"
        run = self.runs.create(
            workspace_id=workspace_id,
            user_id=user_id,
            kind="import",
            mode=mode,
            title=f"导入 · {filename}",
            manifest={"kind": kind, "filename": filename, "exchange": exchange, "digest": res.digest},
        )
        self.db.execute(
            "UPDATE runs SET state=?, started_at=?, ended_at=?, updated_at=? WHERE id=?",
            (COMPLETED, now_ms(), now_ms(), now_ms(), run["id"]),
        )
        n_saved = 0
        if kind == "fills":
            wires = [wire_fill(r, _fill_key(r)) for r in res.rows]
            n_saved = self.runs.save_fills(run["id"], workspace_id, wires, source=mode)
        elif kind == "flows":
            for i, r in enumerate(res.rows):
                try:
                    self.db.execute(
                        "INSERT INTO live_flows (run_id, workspace_id, ts, currency, amount, kind, ref) VALUES (?,?,?,"
                        "?,?,?,?)",
                        (
                            run["id"],
                            workspace_id,
                            r["ts"],
                            r["currency"],
                            r["amount"],
                            r["kind"],
                            r.get("ref") or f"row{i}",
                        ),
                    )
                    n_saved += 1
                except IntegrityError:
                    continue
        else:
            for r in res.rows:
                self.db.execute(
                    "INSERT INTO balance_snapshots (run_id, workspace_id, ts, currency, free, locked, source) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (run["id"], workspace_id, r["ts"], r["currency"], r["free"], r["locked"], mode),
                )
                n_saved += 1
        iid = f"imp-{uuid.uuid4().hex[:12]}"
        report = {**res.report(), "saved": n_saved, "environment": environment, "exchange": exchange}
        self.db.execute(
            "INSERT INTO imports (id, workspace_id, created_by, kind, filename, rows, status, mapping, report, run_id, "
            "created_at, digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                iid,
                workspace_id,
                user_id,
                kind,
                filename[:200],
                len(res.rows),
                "done",
                jdump(res.mapping),
                jdump(report),
                run["id"],
                now_ms(),
                res.digest,
            ),
        )
        self.runs.db.execute("UPDATE runs SET summary=? WHERE id=?", (jdump(report), run["id"]))
        return self.get_import(workspace_id, iid)

    def _imp(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        out = dict(row)
        out["mapping"] = jload(row.get("mapping"), {})
        out["report"] = jload(row.get("report"), {})
        return out

    def list_imports(self, workspace_id: str) -> list[dict[str, Any]]:
        return [
            self._imp(r)
            for r in self.db.fetchall(
                "SELECT * FROM imports WHERE workspace_id=? ORDER BY created_at DESC", (workspace_id,)
            )
        ]

    def get_import(self, workspace_id: str, iid: str) -> dict[str, Any]:
        row = self._imp(self.db.fetchone("SELECT * FROM imports WHERE id=? AND workspace_id=?", (iid, workspace_id)))
        if row is None:
            raise AppError(ErrorCode.NOT_FOUND, "导入记录不存在")
        return row

    def delete_import(self, workspace_id: str, iid: str) -> None:
        imp = self.get_import(workspace_id, iid)
        rid = imp["run_id"]
        with self.db.tx() as t:
            t.execute("DELETE FROM fills WHERE run_id=? AND workspace_id=?", (rid, workspace_id))
            t.execute("DELETE FROM live_flows WHERE run_id=? AND workspace_id=?", (rid, workspace_id))
            t.execute("DELETE FROM balance_snapshots WHERE run_id=? AND workspace_id=?", (rid, workspace_id))
            t.execute("UPDATE imports SET status='deleted' WHERE id=?", (iid,))

    # ------------------------------------------------------------------ 只读同步（worker 调用）
    def save_sync(
        self,
        *,
        run_id: str,
        workspace_id: str,
        mode: str,
        balances: list[dict[str, str]],
        fills: list[dict[str, Any]],
        account_id: str = "",
    ) -> dict[str, Any]:
        """只读同步落库：余额快照 + 成交。

        成交去重是**两层**，缺一层都会留下重复：

        1. `runs.save_fills` 的 `UNIQUE (run_id, fill_key)`——保证同一个 run 被重试
           （worker 崩溃后重新认领、`attempts` 增加）时不会写两份；
        2. 这里的**跨 run 去重**——每次"同步"都是一个新 run，而交易所的成交号
           （`fill_key` = `x:<成交号>`）在交易所侧是永久的。只靠第 1 层的话，
           任何一次重叠拉取（用户手工把 `since` 往回拖、水位被清掉、上游分页边界
           多给一笔）都会在库里留下第二行，`/api/live/status` 的成交数就会虚高，
           对照报告也会把同一笔真实成交算两遍。

        只在真实来源（imported_live / readonly_live / testnet）之间去重：模拟成交的
        fill_key 由策略运行生成，与交易所成交号不是同一个命名空间，混在一起判会误删。
        """
        ts = now_ms()
        for b in balances:
            self.db.execute(
                "INSERT INTO balance_snapshots (run_id, workspace_id, ts, currency, free, locked, source) VALUES (?,?,"
                "?,?,?,?,?)",
                (run_id, workspace_id, ts, b["currency"], dec(b["free"]), dec(b.get("locked") or 0), mode),
            )
        wires = [wire_fill(f, _fill_key(f), account_id=account_id) for f in fills]
        wires = self._drop_known_real_fills(workspace_id, wires)
        saved = self.runs.save_fills(run_id, workspace_id, wires, source=mode)
        return {"balances": len(balances), "fills": len(fills), "new_fills": saved, "account_id": account_id}

    #: 真实成交的来源标记（与 LIVE_MODES / TEST_MODES 一致，显式列出便于 SQL 里用）
    REAL_SOURCES = ("imported_live", "readonly_live", "testnet")

    def _drop_known_real_fills(self, workspace_id: str, wires: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """去掉工作区里**已经存在**的真实成交（按 fill_key），保留首次出现的顺序。

        分批查而不是一条一条查：一次同步可能是上万条成交，逐条 SELECT 会把
        worker 的数据库往返次数放大到不可接受。SQLite 与 PostgreSQL 都有
        参数个数上限（SQLite 默认 32766 / PG 65535），所以按 500 一批。
        """
        if not wires:
            return []
        keys = [w["fill_id"] for w in wires]
        marks = ",".join("?" * len(self.REAL_SOURCES))
        known: set[str] = set()
        for i in range(0, len(keys), 500):
            chunk = keys[i : i + 500]
            rows = self.db.fetchall(
                f"SELECT DISTINCT fill_key FROM fills WHERE workspace_id=? AND source IN ({marks}) "
                f"AND fill_key IN ({','.join('?' * len(chunk))})",
                [workspace_id, *self.REAL_SOURCES, *chunk],
            )
            known.update(r["fill_key"] for r in rows)
        if not known:
            return wires
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for w in wires:
            k = w["fill_id"]
            if k in known or k in seen:
                continue  # 库里已有，或同一批里重复出现
            seen.add(k)
            out.append(w)
        return out

    # ------------------------------------------------------------------ 状态与数据
    def live_runs(self, workspace_id: str, *, include_test: bool = False) -> list[dict[str, Any]]:
        modes = LIVE_MODES + (TEST_MODES if include_test else ())
        rows = self.db.fetchall(
            f"SELECT id, mode, kind, title, created_at, summary FROM runs WHERE workspace_id=? AND mode IN "
            f"({','.join('?' * len(modes))}) ORDER BY created_at DESC",
            [workspace_id, *modes],
        )
        for r in rows:
            r["summary"] = jload(r["summary"], {})
            r["fills"] = int(self.db.scalar("SELECT COUNT(*) FROM fills WHERE run_id=?", (r["id"],)) or 0)
        return rows

    def status(self, workspace_id: str) -> dict[str, Any]:
        runs = self.live_runs(workspace_id)
        n = sum(r["fills"] for r in runs)
        last = self.db.fetchone(
            f"SELECT MAX(ts) AS ts FROM fills WHERE workspace_id=? AND source IN ({','.join('?' * len(LIVE_MODES))})",
            [workspace_id, *LIVE_MODES],
        )
        balances = self.db.fetchall(
            f"SELECT currency, free, locked, ts, source FROM balance_snapshots WHERE workspace_id=? AND source IN "
            f"({','.join('?' * len(LIVE_MODES))}) AND ts = (SELECT MAX(ts) FROM balance_snapshots b2 WHERE "
            f"b2.workspace_id=balance_snapshots.workspace_id AND b2.source IN ({','.join('?' * len(LIVE_MODES))}))",
            [workspace_id, *LIVE_MODES, *LIVE_MODES],
        )
        return {
            "live_enabled": False,
            "has_live_data": n > 0,
            "live_fills": n,
            "last_live_fill_ts": (last or {}).get("ts"),
            "sources": runs,
            "test_sources": [r for r in self.live_runs(workspace_id, include_test=True) if r["mode"] in TEST_MODES],
            "balances": [{**b, "free": str(b["free"]), "locked": str(b["locked"])} for b in balances],
            "message": "已接入真实成交"
            if n
            else "未接入真实成交：请导入 CSV 或连接只读账户。没有真实数据时不展示任何实盘收益",
        }

    def live_fills(self, workspace_id: str, run_ids: list[str]) -> list[dict[str, Any]]:
        if not run_ids:
            return []
        rows = self.db.fetchall(
            "SELECT run_id, source, meta FROM fills WHERE workspace_id=? AND run_id IN "
            f"({','.join('?' * len(run_ids))}) "
            "ORDER BY ts",
            [workspace_id, *run_ids],
        )
        out = []
        for r in rows:
            f = jload(r["meta"], {})
            f["source"] = r["source"]
            f["live_run_id"] = r["run_id"]
            f.setdefault("instrument", f.get("security"))
            f.setdefault("qty", f.get("amount"))
            out.append(f)
        return out

    def flows(self, workspace_id: str, run_ids: list[str]) -> list[dict[str, Any]]:
        if not run_ids:
            return []
        rows = self.db.fetchall(
            f"SELECT ts, currency, amount, kind FROM live_flows WHERE workspace_id=? AND run_id IN "
            f"({','.join('?' * len(run_ids))}) ORDER BY ts",
            [workspace_id, *run_ids],
        )
        return [{**r, "amount": str(r["amount"])} for r in rows]

    def equity_series(self, workspace_id: str, run_ids: list[str]) -> list[dict[str, Any]]:
        """由余额快照拼出实盘权益曲线（供对照的权益层使用）。

        只在快照里**只有一种计价币**时才给曲线：多币种账户要先把各币种按价格折算成同一计价币，
        平台本期不接实时价格做这件事，硬加会把 BTC 的数量当成 USDT 的金额，所以宁可返回空。
        """
        if not run_ids:
            return []
        rows = self.db.fetchall(
            f"SELECT ts, currency, free, locked FROM balance_snapshots WHERE workspace_id=? AND run_id IN "
            f"({','.join('?' * len(run_ids))}) ORDER BY ts",
            [workspace_id, *run_ids],
        )
        if not rows:
            return []
        if len({r["currency"] for r in rows}) > 1:
            return []
        by_ts: dict[int, Decimal] = {}
        for r in rows:
            by_ts[int(r["ts"])] = by_ts.get(int(r["ts"]), Decimal(0)) + dec(r["free"]) + dec(r["locked"])
        return [{"ts": ts, "value": dstr(v)} for ts, v in sorted(by_ts.items())]

    # ------------------------------------------------------------------ 对照
    def compare(
        self,
        *,
        workspace_id: str,
        user_id: str,
        sim_run_id: str,
        live_run_ids: list[str],
        mapping: dict[str, str] | None = None,
        window_ms: int = 2 * 3_600_000,
        fee_rates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """模拟与真实成交的逐笔对照。

        `fee_rates`：`{币种: 1 单位该币种 = 多少计价币}`，用于把**第三币种**手续费
        （币安常见的 BNB 抵扣）折算到计价币。**必须由调用方显式给出**——平台不联网取价、
        不按 1:1 估算。没给的币种其手续费记为"未折算"，在 `fees.unconverted` 里单列，
        绝不并进 `converted_value`。
        """
        sim_run = self.runs.get(sim_run_id, workspace_id=workspace_id)
        if sim_run is None or sim_run["mode"] not in ("backtest", "paper"):
            raise AppError(ErrorCode.NOT_FOUND, "模拟运行不存在（需要回测或实时模拟的 run）")
        live_runs = []
        for rid in live_run_ids:
            r = self.runs.get(rid, workspace_id=workspace_id)
            if r is None or r["mode"] not in LIVE_MODES + TEST_MODES:
                raise AppError(ErrorCode.NOT_FOUND, f"真实记录来源不存在：{rid}")
            live_runs.append(r)
        if not live_runs:
            raise AppError(ErrorCode.DATA_INCOMPLETE, "未接入真实成交：请先导入 CSV 或同步只读账户")
        sim = self.runs.fills(sim_run_id, limit=50_000)
        live = self.live_fills(workspace_id, live_run_ids)
        if not live:
            raise AppError(ErrorCode.DATA_INCOMPLETE, "所选来源没有真实成交记录")
        rep = attribute(sim, live, mapping=mapping, window_ms=window_ms, fee_rates=fee_rates)
        body = rep.to_dict()
        # 手续费口径与回测报告统一（engine.metrics.fee_summary）：能折算的进总额，
        # 不能折算的按币种单列，两者不混。模拟侧的手续费在生成时已按计价币折算，
        # 所以这里只把**真实侧**的未折算部分暴露出来。
        live_fees = body["summary"]["attributed_result"]
        body["fees"] = fee_summary(
            by_currency=live_fees.get("fees_by_currency"),
            converted=live_fees.get("fees", "0"),
            unconverted=live_fees.get("unconverted_fees"),
            unconverted_details=live_fees.get("unconverted_fee_details"),
        )
        is_test = any(r["mode"] in TEST_MODES for r in live_runs)
        if is_test:
            body["notes"].insert(0, "包含测试网成交：本报告标为“测试”，不代表实盘结果")
        sim_ex = ((sim_run.get("manifest") or {}).get("spec") or {}).get("exchange") or (
            sim_run.get("manifest") or {}
        ).get("exchange")
        live_ex = {(r.get("manifest") or {}).get("exchange") for r in live_runs} - {None, ""}
        if sim_ex and live_ex and sim_ex not in live_ex:
            body["notes"].append(
                f"市场不一致：模拟使用 {sim_ex} 行情，真实成交来自 {'、'.join(sorted(live_ex))}；"
                "价差中包含两个市场的价格差异"
            )
        flows = self.flows(workspace_id, live_run_ids)
        body["flows"] = {"count": len(flows), "net": dstr(sum((Decimal(f["amount"]) for f in flows), Decimal(0)))}
        body["labels"] = {
            "test": is_test,
            "sim_mode": sim_run["mode"],
            "live_modes": sorted({r["mode"] for r in live_runs}),
        }
        cid = f"cmp-{uuid.uuid4().hex[:12]}"
        self.db.execute(
            "INSERT INTO comparisons (id, workspace_id, created_by, sim_run_id, live_run_id, status, result, created_a"
            "t) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (cid, workspace_id, user_id, sim_run_id, ",".join(live_run_ids), "done", jdump(body), now_ms()),
        )
        return {"id": cid, **body}

    def list_comparisons(self, workspace_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT id, sim_run_id, live_run_id, status, created_at, result FROM comparisons WHERE workspace_id=? "
            "ORDER BY created_at DESC LIMIT 100",
            (workspace_id,),
        )
        for r in rows:
            res = jload(r.pop("result"), {})
            r["summary"] = res.get("summary")
            r["labels"] = res.get("labels")
        return rows

    def get_comparison(self, workspace_id: str, cid: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM comparisons WHERE id=? AND workspace_id=?", (cid, workspace_id))
        if row is None:
            raise AppError(ErrorCode.NOT_FOUND, "对照报告不存在")
        return {
            "id": row["id"],
            "sim_run_id": row["sim_run_id"],
            "live_run_ids": row["live_run_id"].split(","),
            "created_at": row["created_at"],
            **jload(row["result"], {}),
        }
