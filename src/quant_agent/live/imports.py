"""真实记录 CSV 导入（imported_live）：字段映射、逐行校验、公式注入防护、Decimal 解析。

支持的记录类型：
    fills     成交：时间、标的、方向、数量、价格（必需）；手续费、手续费币种、订单号、client_order_id、成交号（可选）
    flows     资金流水：时间、币种、金额（正入负出）、类型（可选）
    balances  余额快照：时间、币种、可用、冻结

字段映射：`mapping = {标准字段: CSV 列名}`；未给出的标准字段按常见列名自动识别
（含交易所导出的英文列名与中文列名）。

导入是**全有或全无**：任何一行不合法，整份文件不入库，并逐行报告原因（最多 100 条）。
导入结果只代表“账户里实际发生了什么”；哪些成交属于目标策略要靠归因（见 attribution.py）。
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from ..platform.csvsafe import is_formula

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 50_000
MAX_ERRORS = 100

FIELDS: dict[str, dict[str, Any]] = {
    "fills": {
        "required": ["ts", "instrument", "side", "qty", "price"],
        "optional": ["fee", "fee_currency", "order_id", "client_order_id", "fill_id"],
    },
    "flows": {"required": ["ts", "currency", "amount"], "optional": ["kind", "ref"]},
    "balances": {"required": ["ts", "currency", "free"], "optional": ["locked"]},
}

ALIASES: dict[str, list[str]] = {
    "ts": [
        "ts",
        "time",
        "timestamp",
        "date(utc)",
        "date",
        "datetime",
        "created_at",
        "fill_time",
        "时间",
        "成交时间",
        "日期",
    ],
    "instrument": ["instrument", "symbol", "pair", "market", "instid", "交易对", "标的", "币对"],
    "side": ["side", "direction", "type", "方向", "买卖"],
    "qty": ["qty", "quantity", "amount", "executed", "size", "fillsz", "filled", "数量", "成交数量"],
    "price": ["price", "avg_price", "fillpx", "成交价", "价格", "成交价格"],
    "fee": ["fee", "commission", "手续费"],
    "fee_currency": ["fee_currency", "fee_asset", "commission_asset", "feeccy", "fee coin", "手续费币种"],
    "order_id": ["order_id", "orderid", "ordid", "订单号"],
    "client_order_id": ["client_order_id", "clientorderid", "clordid", "客户订单号"],
    "fill_id": ["fill_id", "trade_id", "tradeid", "id", "成交号", "成交编号"],
    "currency": ["currency", "coin", "asset", "ccy", "币种"],
    "amount": ["amount", "change", "qty", "数量", "金额", "变动"],
    "kind": ["kind", "type", "operation", "类型"],
    "ref": ["ref", "txid", "id", "流水号"],
    "free": ["free", "available", "availbal", "可用"],
    "locked": ["locked", "frozen", "frozenbal", "冻结"],
}


class ImportError_(ValueError):
    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


@dataclass
class ImportResult:
    kind: str
    rows: list[dict[str, Any]]
    mapping: dict[str, str]
    digest: str
    instruments: list[str] = field(default_factory=list)
    period: tuple[int, int] | None = None
    warnings: list[str] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "rows": len(self.rows),
            "mapping": self.mapping,
            "digest": self.digest,
            "instruments": self.instruments,
            "period": list(self.period) if self.period else None,
            "warnings": self.warnings,
        }


def _norm(s: str) -> str:
    return s.strip().lower().replace(" ", "").replace("_", "")


def parse_ts(v: str) -> int:
    s = v.strip()
    if s.isdigit():
        n = int(s)
        if n < 10**11:  # 秒
            n *= 1000
        if n > 10**14:  # 微秒
            n //= 1000
        return n
    s2 = s.replace("/", "-").replace("T", " ").rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s2, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"无法识别的时间：{v}（支持毫秒时间戳或 2026-01-31 08:00:00，按 UTC 解释）")


def parse_dec(v: str, *, allow_negative: bool = False) -> Decimal:
    s = v.strip().replace(",", "")
    try:
        d = Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"不是合法数字：{v}") from exc
    if not d.is_finite():
        raise ValueError(f"不是有限数字：{v}")
    if d < 0 and not allow_negative:
        raise ValueError(f"不能为负：{v}")
    return d


def parse_side(v: str) -> str:
    s = v.strip().lower()
    if s in ("buy", "b", "bid", "买", "买入", "long"):
        return "buy"
    if s in ("sell", "s", "ask", "卖", "卖出", "short"):
        return "sell"
    raise ValueError(f"无法识别的方向：{v}（应为 buy / sell / 买 / 卖）")


def parse_instrument(v: str) -> str:
    s = v.strip().upper().replace("-", "/").replace("_", "/")
    if "/" in s:
        return s
    for q in ("USDT", "USDC", "FDUSD", "BUSD", "BTC", "ETH", "USD"):
        if s.endswith(q) and len(s) > len(q):
            return f"{s[: -len(q)]}/{q}"
    raise ValueError(f"无法识别的标的：{v}（应为 BTC/USDT、BTC-USDT 或 BTCUSDT）")


def resolve_mapping(kind: str, header: list[str], mapping: dict[str, str] | None) -> dict[str, str]:
    spec = FIELDS[kind]
    want = spec["required"] + spec["optional"]
    by_norm = {_norm(h): h for h in header}
    out: dict[str, str] = {}
    for f, col in (mapping or {}).items():
        if f not in want:
            raise ImportError_(f"未知的标准字段：{f}；{kind} 可映射字段：{', '.join(want)}")
        if col not in header:
            raise ImportError_(f"映射的列 {col!r} 不在文件表头中")
        out[f] = col
    used = set(out.values())
    for f in want:
        if f in out:
            continue
        for alias in ALIASES.get(f, [f]):
            col = by_norm.get(_norm(alias))
            if col and col not in used:
                out[f] = col
                used.add(col)
                break
    missing = [f for f in spec["required"] if f not in out]
    if missing:
        raise ImportError_(
            f"缺少必需字段：{', '.join(missing)}；请在字段映射中指定对应的列",
            [{"row": 0, "field": f, "message": "未找到对应列"} for f in missing],
        )
    return out


def parse_csv(kind: str, content: str | bytes, *, mapping: dict[str, str] | None = None) -> ImportResult:
    if kind not in FIELDS:
        raise ImportError_(f"不支持的记录类型：{kind}；可选：{', '.join(FIELDS)}")
    raw = content.encode("utf-8") if isinstance(content, str) else content
    if len(raw) > MAX_BYTES:
        raise ImportError_(f"文件过大：{len(raw) // 1024} KB，上限 {MAX_BYTES // 1024 // 1024} MB")
    text = raw.decode("utf-8-sig", errors="strict") if isinstance(content, bytes) else content.lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ImportError_("文件为空") from exc
    header = [h.strip() for h in header]
    errors: list[dict[str, Any]] = []
    for i, h in enumerate(header):
        if is_formula(h):
            errors.append({"row": 1, "field": f"列 {i + 1}", "message": "表头疑似公式注入，已拒绝"})
    if errors:
        raise ImportError_("文件包含疑似公式的单元格，已整体拒绝", errors)
    m = resolve_mapping(kind, header, mapping)
    idx = {f: header.index(col) for f, col in m.items()}
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for n, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells):
            continue
        if len(rows) >= MAX_ROWS:
            raise ImportError_(f"行数超过上限 {MAX_ROWS}，请拆分文件")
        bad = [c for c in cells if is_formula(c)]
        if bad:
            errors.append({"row": n, "field": "*", "message": f"疑似公式注入：{bad[0][:20]!r}"})
            continue

        def cell(f: str, cells: list[str] = cells) -> str:
            j = idx.get(f)
            return cells[j].strip() if j is not None and j < len(cells) else ""

        try:
            row: dict[str, Any] = {"ts": parse_ts(cell("ts"))}
            if kind == "fills":
                row.update(
                    {
                        "instrument": parse_instrument(cell("instrument")),
                        "side": parse_side(cell("side")),
                        "qty": parse_dec(cell("qty")),
                        "price": parse_dec(cell("price")),
                        "fee": parse_dec(cell("fee")) if cell("fee") else Decimal(0),
                        "fee_currency": (cell("fee_currency") or "").upper() or None,
                        "order_id": cell("order_id") or None,
                        "client_order_id": cell("client_order_id") or None,
                        "fill_id": cell("fill_id") or None,
                    }
                )
                if row["qty"] == 0 or row["price"] == 0:
                    raise ValueError("数量与价格必须大于 0")
                if row["fill_id"]:
                    if row["fill_id"] in seen_ids:
                        raise ValueError(f"成交号重复：{row['fill_id']}")
                    seen_ids.add(row["fill_id"])
            elif kind == "flows":
                amt = parse_dec(cell("amount"), allow_negative=True)
                kind_v = (cell("kind") or ("deposit" if amt > 0 else "withdraw")).lower()
                row.update(
                    {"currency": cell("currency").upper(), "amount": amt, "kind": kind_v, "ref": cell("ref") or None}
                )
                if not row["currency"]:
                    raise ValueError("币种不能为空")
            else:
                row.update(
                    {
                        "currency": cell("currency").upper(),
                        "free": parse_dec(cell("free")),
                        "locked": parse_dec(cell("locked")) if cell("locked") else Decimal(0),
                    }
                )
                if not row["currency"]:
                    raise ValueError("币种不能为空")
            rows.append(row)
        except ValueError as exc:
            errors.append({"row": n, "field": "", "message": str(exc)})
        if len(errors) >= MAX_ERRORS:
            break
    if errors:
        raise ImportError_(f"有 {len(errors)} 行不合法，整份文件未导入（全有或全无）", errors[:MAX_ERRORS])
    if not rows:
        raise ImportError_("没有数据行")
    rows.sort(key=lambda r: r["ts"])
    digest = hashlib.sha256(raw).hexdigest()
    warnings = []
    if kind == "fills":
        missing_fee = sum(1 for r in rows if not r["fee"])
        if missing_fee:
            warnings.append(f"{missing_fee} 笔成交没有手续费，对照时手续费差异会偏大")
        if not any(r["client_order_id"] for r in rows):
            warnings.append("没有 client_order_id：只能按品种、方向、时间与数量启发式归因，结果带置信度")
        if not any(r.get("fill_id") for r in rows):
            warnings.append(
                "没有交易所成交号：跨文件导入时按成交内容去重，"
                "字段完全相同的两笔成交会被当成一笔（宁可少算也不重复计入）"
            )
    instruments = sorted({r["instrument"] for r in rows}) if kind == "fills" else []
    return ImportResult(
        kind=kind,
        rows=rows,
        mapping=m,
        digest=digest,
        instruments=instruments,
        period=(rows[0]["ts"], rows[-1]["ts"]),
        warnings=warnings,
    )
