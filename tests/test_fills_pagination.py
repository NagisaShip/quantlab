"""T01 · 交易所真实成交的增量同步与分页。

验收要求（本文件必须自证的那条）：
    用 httpx.MockTransport 模拟 2500 条成交分 3 页返回，同步两次后数据库恰好 2500 条、
    无重复；第二次同步只请求水位之后的数据。

测试分三层，故意的：
  1. **客户端层**（不碰数据库）：用 MockTransport 断言请求里带的游标参数、
     翻页终止条件、页数上限、以及"游标不前进"时显式报错而不是空转。
     这一层盯的是"我们到底发了什么请求"——只看结果条数是发现不了"每轮都全量重拉"的。
  2. **worker 层**（真库 + inline worker）：同步两次，数数据库里的行数、看水位表。
  3. **隔离层**：水位表与新端点都必须按 workspace_id 过滤。
"""

from __future__ import annotations

import json
import logging
import time

import httpx
import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.live import accounts as acc
from quant_agent.platform.errors import AppError

KEY = "k" * 16
SECRET = "s" * 16
SYMBOL = "BTC/USDT"
INST = "BTCUSDT"

# 2500 条成交：分页边界 1000 / 1000 / 500
TOTAL = 2500


# ---------------------------------------------------------------- 假交易所
def binance_rows(count: int = TOTAL, *, newest_first: bool = False) -> list[dict]:
    """币安 /api/v3/myTrades 形状的成交行（id 从 1 开始递增）。"""
    base = 1_767_000_000_000
    rows = [
        {
            "id": i,
            "orderId": 900_000 + i,
            "symbol": INST,
            "time": base + i,
            "isBuyer": i % 2 == 0,
            "qty": "0.01",
            "price": "100.5",
            "commission": "0.001",
            "commissionAsset": "USDT",
        }
        for i in range(1, count + 1)
    ]
    return list(reversed(rows)) if newest_first else rows


def binance_transport(rows: list[dict], seen: list[dict]) -> httpx.MockTransport:
    """币安 MockTransport。

    分页模型（与真实接口一致的部分）：`fromId=k` 返回 **id >= k 的、按 id 升序的**前 1000 条。
    简化的一处：不带 `fromId` 时这里返回"从头开始的第一页"，而不是真实的"最近 1000 笔"。
    真实语义单独由 `test_binance_first_page_without_fromid_is_newest_and_sorted` 覆盖
    ——那里只验证客户端**处理**这一页的方式（排序 + 水位取最大值），不需要凑够 2500 条。
    """

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": int(time.time() * 1000)})
        if path == "/api/v3/account":
            return httpx.Response(
                200,
                json={
                    "uid": 987654321,
                    "canTrade": False,
                    "canWithdraw": False,
                    "balances": [{"asset": "USDT", "free": "1000.5", "locked": "0"}],
                },
            )
        if path == "/sapi/v1/account/apiRestrictions":
            return httpx.Response(
                200,
                json={
                    "ipRestrict": True,
                    "enableReading": True,
                    "enableWithdrawals": False,
                    "enableSpotAndMarginTrading": False,
                },
            )
        if path == "/api/v3/myTrades":
            params = dict(req.url.params)
            seen.append(params)
            start = int(params["fromId"]) if params.get("fromId") else 1
            return httpx.Response(200, json=[r for r in rows if r["id"] >= start][:1000])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def okx_rows(count: int) -> list[dict]:
    """OKX /api/v5/trade/fills-history 形状的成交行（tradeId 递增，返回时倒序）。"""
    base = 1_767_000_000_000
    rows = [
        {
            "tradeId": str(i),
            "ordId": str(800_000 + i),
            "instId": "BTC-USDT",
            "ts": str(base + i),
            "side": "buy" if i % 2 else "sell",
            "fillSz": "0.01",
            "fillPx": "100.5",
            "fee": "-0.001",
            "feeCcy": "USDT",
        }
        for i in range(1, count + 1)
    ]
    return list(reversed(rows))


def okx_transport(rows: list[dict], seen: list[dict]) -> httpx.MockTransport:
    """OKX MockTransport。

    游标模型与 OKX 分页契约一致：返回的是**从游标出发最近的 100 条**。
    - `after=X` → tradeId **严格小于** X 的那批里，最大的 100 条（往旧翻，倒序返回）
    - `before=X` → tradeId **严格大于** X 的那批里，最小的 100 条（往新翻）
    两个都开区间，游标那一笔自己不会被返回。
    """

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/api/v5/account/config":
            return httpx.Response(200, json={"code": "0", "data": [{"uid": "123456789", "perm": "read_only"}]})
        if path == "/api/v5/account/balance":
            return httpx.Response(
                200, json={"code": "0", "data": [{"details": [{"ccy": "USDT", "availBal": "1000.5"}]}]}
            )
        if path == "/api/v5/trade/fills-history":
            params = dict(req.url.params)
            seen.append(params)
            if params.get("after"):
                older = [r for r in rows if int(r["tradeId"]) < int(params["after"])]
                batch = sorted(older, key=lambda r: int(r["tradeId"]), reverse=True)[:100]
            elif params.get("before"):
                newer = [r for r in rows if int(r["tradeId"]) > int(params["before"])]
                batch = sorted(newer, key=lambda r: int(r["tradeId"]))[:100]
            else:
                batch = sorted(rows, key=lambda r: int(r["tradeId"]), reverse=True)[:100]
            return httpx.Response(200, json={"code": "0", "data": batch})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture()
def app(tmp_path):
    set_services(AppServices.build(store_root=str(tmp_path)))
    yield create_app()
    reset_services()
    acc.client_factory = acc.default_client_factory


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        login(c)
        yield c


def _connect(client, transport: httpx.MockTransport, exchange: str = "binance") -> dict:
    """建一个只读连接（权限检测也走 MockTransport，不碰网络）。"""
    if exchange == "binance":
        acc.client_factory = lambda ex, creds, env: acc.BinanceReadOnly(
            KEY, SECRET, environment=env, transport=transport
        )
        payload = {"exchange": "binance", "label": "验收账户", "api_key": KEY, "api_secret": SECRET}
    else:
        acc.client_factory = lambda ex, creds, env: acc.OKXReadOnly(
            KEY, SECRET, "pass", environment=env, transport=transport
        )
        payload = {
            "exchange": "okx",
            "label": "验收账户",
            "api_key": KEY,
            "api_secret": SECRET,
            "passphrase": "pass",
        }
    r = client.post("/api/accounts/connections", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["connection"]


def _sync(client, cid: str, **body) -> dict:
    r = client.post(f"/api/accounts/connections/{cid}/sync", json={"symbols": [SYMBOL], **body})
    assert r.status_code == 200, r.text
    return r.json()["run"]


def _ws_id(client) -> str:
    """当前工作区 id。仓库没有暴露给测试的取法，就从 /api/auth/me 拿。"""
    return client.get("/api/auth/me").json()["workspace"]["id"]


def _fill_count(db) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM fills") or 0)


# ===========================================================================
# 1) 验收主用例：2500 条 / 3 页 / 同步两次
# ===========================================================================
def test_acceptance_2500_fills_three_pages_two_syncs(client):
    """验收：2500 条成交分 3 页；同步两次后库里恰好 2500 条、无重复；
    第二次同步只请求水位（2500）之后的数据。"""
    rows = binance_rows(TOTAL)
    seen: list[dict] = []
    conn = _connect(client, binance_transport(rows, seen))
    cid = conn["id"]
    seen.clear()

    # ---- 第一次同步：应该翻 3 页把 2500 条全部拉回来 ----
    run1 = _sync(client, cid)
    assert run1["state"] == "COMPLETED", run1
    assert run1["summary"]["new_fills"] == TOTAL
    assert run1["summary"]["pages"] == 3, run1["summary"]
    assert run1["summary"]["watermarks"] == {SYMBOL: str(TOTAL)}

    # 请求序列证明"是翻页而不是一次拉 1000 条"
    assert len(seen) == 3, seen
    assert seen[0].get("fromId") is None  # 首次没有水位：不带 fromId
    assert seen[0]["limit"] == "1000"
    assert seen[1]["fromId"] == "1001"  # 最后一笔 id=1000 → 下一笔从 1001 开始
    assert seen[2]["fromId"] == "2001"
    assert all(p["symbol"] == INST for p in seen)

    db = get_services().db
    assert _fill_count(db) == TOTAL
    # 无重复：fill_key（= x:<交易所成交号>）在库里唯一
    assert int(db.scalar("SELECT COUNT(DISTINCT fill_key) FROM fills") or 0) == TOTAL
    assert int(db.scalar("SELECT COUNT(*) FROM fills WHERE fill_key='x:1'") or 0) == 1
    assert int(db.scalar("SELECT COUNT(*) FROM fills WHERE fill_key='x:2500'") or 0) == 1

    # 水位落库
    wm = db.fetchone(
        "SELECT * FROM fills_sync_watermarks WHERE workspace_id=? AND connection_id=?", (_ws_id(client), cid)
    )
    assert wm["last_fill_id"] == str(TOTAL) and wm["symbol"] == SYMBOL
    assert wm["last_fill_ts"] == rows[-1]["time"]
    assert wm["total_fills"] == TOTAL and wm["total_pages"] == 3 and wm["last_error"] is None

    # ---- 第二次同步：只请求水位之后的数据 ----
    seen.clear()
    run2 = _sync(client, cid)
    assert run2["state"] == "COMPLETED", run2
    assert run2["summary"]["new_fills"] == 0
    assert len(seen) == 1, seen
    assert seen[0]["fromId"] == str(TOTAL + 1), seen[0]  # 2501，绝不回退
    assert "startTime" not in seen[0]  # 有 id 游标时不用时间游标（两者不能同时传）

    # 库里仍然恰好 2500 条，且没有新行
    assert _fill_count(db) == TOTAL
    assert int(db.scalar("SELECT COUNT(DISTINCT fill_key) FROM fills") or 0) == TOTAL


def test_acceptance_repeat_sync_is_idempotent_even_if_upstream_replays(client):
    """即使上游把整段又回放了一遍，也不能出现重复行。

    场景是真的会发生的：用户在交易所侧有两个只读连接指向同一个账户，
    或者水位被清掉后用 `since` 手工补拉。去重靠 `runs.save_fills` 的
    `UNIQUE (run_id, fill_key)` 加上 `save_sync` 的跨 run 去重，
    fill_key 用交易所成交号（`x:<成交号>`）。
    """
    rows = binance_rows(TOTAL)
    seen: list[dict] = []
    transport = binance_transport(rows, seen)
    conn1 = _connect(client, transport)
    _sync(client, conn1["id"])
    db = get_services().db
    assert _fill_count(db) == TOTAL

    # 第二个连接指向同一个交易所账户（同一批成交号）：全量拉一遍也不该写重复行
    conn2 = _connect(client, transport)
    seen.clear()
    run2 = _sync(client, conn2["id"])
    assert run2["state"] == "COMPLETED"
    assert len(seen) == 3, "第二个连接没有水位，应该老老实实翻完 3 页"
    assert run2["summary"]["new_fills"] == 0, "重复拉到的成交不该再写进库"
    assert _fill_count(db) == TOTAL
    assert int(db.scalar("SELECT COUNT(DISTINCT fill_key) FROM fills") or 0) == TOTAL


def test_since_replay_after_watermark_loss_does_not_duplicate(client):
    """水位被清掉、用户又传了 since 做全量补拉：同样不能出现重复行。"""
    rows = binance_rows(TOTAL)
    seen: list[dict] = []
    conn = _connect(client, binance_transport(rows, seen))
    cid = conn["id"]
    _sync(client, cid)
    s = get_services()
    ws = _ws_id(client)
    db = s.db
    assert _fill_count(db) == TOTAL

    # 模拟"水位丢失"：清空游标，此时 since 才会被当成增量判据
    db.execute(
        "UPDATE fills_sync_watermarks SET last_fill_id=NULL, last_fill_ts=NULL "
        "WHERE workspace_id=? AND connection_id=?",
        (ws, cid),
    )
    seen.clear()
    run2 = _sync(client, cid, since=1)
    assert run2["state"] == "COMPLETED"
    assert run2["summary"]["new_fills"] == 0, "重复拉到的成交不该再写进库"
    assert len(seen) == 3 and "startTime" in seen[0]
    assert _fill_count(db) == TOTAL
    assert int(db.scalar("SELECT COUNT(DISTINCT fill_key) FROM fills") or 0) == TOTAL


def test_sync_retry_with_same_run_does_not_duplicate(client):
    """worker 崩溃后同一个 run 被重试：save_sync 再跑一遍也只有一份。"""
    rows = binance_rows(TOTAL)
    seen: list[dict] = []
    conn = _connect(client, binance_transport(rows, seen))
    s = get_services()
    ws = _ws_id(client)
    run = s.runs.create(
        workspace_id=ws,
        user_id="u",
        kind="account_sync",
        mode="readonly_live",
        title="重试",
        manifest={"connection_id": conn["id"]},
    )
    db = s.db
    fills = [
        {
            "ts": r["time"],
            "instrument": SYMBOL,
            "side": "buy",
            "qty": r["qty"],
            "price": r["price"],
            "fee": r["commission"],
            "fee_currency": r["commissionAsset"],
            "order_id": str(r["orderId"]),
            "fill_id": str(r["id"]),
            "client_order_id": None,
        }
        for r in rows
    ]
    first = s.live.save_sync(
        run_id=run["id"], workspace_id=ws, mode="readonly_live", balances=[], fills=fills, account_id=conn["id"]
    )
    assert first["new_fills"] == TOTAL
    second = s.live.save_sync(
        run_id=run["id"], workspace_id=ws, mode="readonly_live", balances=[], fills=fills, account_id=conn["id"]
    )
    assert second["new_fills"] == 0
    assert _fill_count(db) == TOTAL


def test_cross_run_dedupe_does_not_touch_simulated_fills(client):
    """跨 run 去重只作用于真实来源，模拟成交的 fill_key 是另一个命名空间。"""
    s = get_services()
    ws = _ws_id(client)
    run = s.runs.create(workspace_id=ws, user_id="u", kind="backtest", mode="backtest", title="回测", manifest={})
    sim = [
        {
            "fill_id": "1",  # 与交易所成交号同名，但 source=simulated
            "signal_id": None,
            "order_id": None,
            "client_order_id": None,
            "security": SYMBOL,
            "instrument": SYMBOL,
            "side": "buy",
            "amount": "0.01",
            "qty": "0.01",
            "price": "100",
            "fee": "0",
            "fee_currency": "USDT",
            "ts": 1,
        }
    ]
    s.runs.save_fills(run["id"], ws, sim, source="simulated")
    assert s.live._drop_known_real_fills(ws, sim) == sim  # 模拟成交不受影响
    assert _fill_count(s.db) == 1


# ===========================================================================
# 2) 客户端层：请求参数、排序、终止条件、上限
# ===========================================================================
def test_binance_first_page_without_fromid_is_newest_and_sorted():
    """不带 fromId 时币安返回的是**最近** 1000 笔（接近倒序）。

    客户端必须按 id 升序排好再算水位：直接取"最后一笔"的话，在倒序返回下
    拿到的会是最**小**的 id，水位立刻回退，下一轮把整段历史再拉一遍。
    """
    seen: list[dict] = []
    rows = binance_rows(5, newest_first=True)  # id 5,4,3,2,1
    c = acc.BinanceReadOnly(KEY, SECRET, transport=binance_transport(rows, seen))
    fills, pages = c.fills([SYMBOL], None)
    assert pages == 1 and len(fills) == 5
    assert [f["fill_id"] for f in fills] == ["1", "2", "3", "4", "5"]  # 已升序
    assert seen[0].get("fromId") is None


def test_binance_fromid_and_starttime_never_both_sent():
    """币安不接受 fromId 与 startTime 同时出现，客户端必须只挑一个。"""
    seen: list[dict] = []
    c = acc.BinanceReadOnly(KEY, SECRET, transport=binance_transport(binance_rows(5), seen))
    c.fills([SYMBOL], 1_767_000_000_000, after_id="3")
    assert seen[0]["fromId"] == "4" and "startTime" not in seen[0]
    seen.clear()
    c.fills([SYMBOL], 1_767_000_000_000)
    assert seen[0]["startTime"] == "1767000000000" and "fromId" not in seen[0]


def test_binance_stops_when_page_is_not_full():
    """不足一页就停：2500 条正好 3 页，第 3 页只有 500 条。"""
    seen: list[dict] = []
    c = acc.BinanceReadOnly(KEY, SECRET, transport=binance_transport(binance_rows(TOTAL), seen))
    fills, pages = c.fills([SYMBOL], None)
    assert len(fills) == TOTAL and pages == 3
    assert [p["fromId"] for p in seen[1:]] == ["1001", "2001"]


def test_binance_max_pages_cap_stops_and_warns(caplog):
    """上游一直返回满页时必须在上限处停下（否则 worker 会被拖到租约过期）。"""
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path != "/api/v3/myTrades":
            return httpx.Response(404)
        n = len(calls)
        calls.append(n)
        base = n * 1000
        return httpx.Response(
            200,
            json=[
                {"id": base + i, "time": 1, "isBuyer": True, "qty": "1", "price": "1", "commission": "0"}
                for i in range(1, 1001)
            ],
        )

    c = acc.BinanceReadOnly(KEY, SECRET, transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING, logger="quantlab.accounts"):
        fills, pages = c.fills([SYMBOL], None, max_pages=3)
    assert pages == 3 and len(fills) == 3000  # 已拉到的照常返回，不整批丢弃
    assert any("页数上限" in r.message for r in caplog.records)


def test_binance_cursor_not_advancing_raises_app_error():
    """上游忽略 fromId、反复返回同一页时必须**报错**，不能安静地空转到上限。"""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path != "/api/v3/myTrades":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json=[
                {"id": i, "time": 1, "isBuyer": True, "qty": "1", "price": "1", "commission": "0"}
                for i in range(1, 1001)
            ],
        )

    c = acc.BinanceReadOnly(KEY, SECRET, transport=httpx.MockTransport(handler))
    with pytest.raises(AppError) as ei:
        c.fills([SYMBOL], None, max_pages=5)
    assert "游标没有前进" in ei.value.message


def test_okx_after_cursor_pages_backwards_over_250_fills():
    """OKX：`after` 取**更早**的数据（与直觉相反），limit=100 → 250 条要 3 页。"""
    seen: list[dict] = []
    rows = okx_rows(250)
    c = acc.OKXReadOnly(KEY, SECRET, "pass", transport=okx_transport(rows, seen))
    fills, pages = c.fills([SYMBOL], None)
    assert len(fills) == 250 and pages == 3
    assert seen[0].get("after") is None and seen[0]["limit"] == "100"
    # 第一页返回最新 100 条（tradeId 250..151）→ 下一页 after=151，继续取更旧的
    assert seen[1]["after"] == "151"
    assert seen[2]["after"] == "51"
    ids = sorted(int(f["fill_id"]) for f in fills)
    assert ids == list(range(1, 251))


def test_okx_after_id_is_exclusive_and_not_offset_by_one():
    """首次同步（无水位）时用 `after` **往旧翻**，且 after 是开区间、不做 ±1。

    语义与直觉相反，所以这条测试刻意把方向写死：`after=151` 返回的是
    tradeId 严格小于 151 的那批里最近的 100 条（150..51），151 自己不会再出现。
    """
    seen: list[dict] = []
    c = acc.OKXReadOnly(KEY, SECRET, "pass", transport=okx_transport(okx_rows(250), seen))
    fills, pages = c.fills([SYMBOL], None)
    assert pages == 3 and len(fills) == 250
    assert seen[0].get("after") is None and seen[0].get("before") is None
    assert seen[1]["after"] == "151", seen[1]  # 本页最小 tradeId=151 → 下一批比 151 更旧
    assert "before" not in seen[1]
    assert seen[2]["after"] == "51"
    # 开区间：整批下来没有一笔重复
    assert len({f["fill_id"] for f in fills}) == 250


def test_okx_incremental_uses_before_and_pages_forward():
    """水位存在时用 `before` 取更新的成交；满页时把游标推到本页最大成交号继续。"""
    seen: list[dict] = []
    rows = okx_rows(250)
    c = acc.OKXReadOnly(KEY, SECRET, "pass", transport=okx_transport(rows, seen))
    # 水位 50 → 只取 51..250 这 200 条
    fills, pages = c.fills([SYMBOL], None, after_id="50")
    assert len(fills) == 200 and pages == 3  # 100 + 100 + 空页收尾
    assert sorted(int(f["fill_id"]) for f in fills) == list(range(51, 251))
    assert seen[0]["before"] == "50" and "after" not in seen[0]
    assert seen[1]["before"] == "150"  # 第一页 51..150，游标推到 150
    assert seen[2]["before"] == "250"


def test_okx_uses_begin_only_without_cursor():
    seen: list[dict] = []
    c = acc.OKXReadOnly(KEY, SECRET, "pass", transport=okx_transport(okx_rows(10), seen))
    c.fills([SYMBOL], 1_767_000_000_000)
    assert seen[0]["begin"] == "1767000000000" and "after" not in seen[0] and "before" not in seen[0]
    seen.clear()
    c.fills([SYMBOL], 1_767_000_000_000, after_id="5")
    # 有水位时走增量分支，时间参数不再参与（时间边界是闭区间，不能做增量判据）
    assert seen[0]["before"] == "5" and "begin" not in seen[0]


def test_okx_cursor_not_advancing_raises_app_error():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path != "/api/v5/trade/fills-history":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": [
                    {"tradeId": str(1000 + i), "ts": "1", "side": "buy", "fillSz": "1", "fillPx": "1", "fee": "0"}
                    for i in range(100)
                ],
            },
        )

    c = acc.OKXReadOnly(KEY, SECRET, "pass", transport=httpx.MockTransport(handler))
    with pytest.raises(AppError) as ei:
        c.fills([SYMBOL], None, max_pages=5)
    assert "游标没有前进" in ei.value.message


def test_readonly_clients_still_have_no_order_path():
    """T01 加了分页，但绝不允许顺手加进任何下单/提现路径。"""
    for cls in (acc.BinanceReadOnly, acc.OKXReadOnly):
        names = {n.lower() for n in dir(cls)}
        assert not any(w in n for n in names for w in ("order", "withdraw", "transfer", "cancel"))


# ===========================================================================
# 3) 水位：只前进不后退
# ===========================================================================
def test_watermark_never_moves_backwards(client):
    """水位倒退会让下一轮把已入库的历史再拉一遍，所以合并时必须取大。"""
    seen: list[dict] = []
    conn = _connect(client, binance_transport(binance_rows(TOTAL), seen))
    cid = conn["id"]
    ws = _ws_id(client)
    s = get_services()
    s.accounts.save_watermark(
        workspace_id=ws,
        connection_id=cid,
        symbol=SYMBOL,
        exchange="binance",
        environment="live",
        fill_id="2500",
        ts=1_767_000_002_500,
        new_fills=TOTAL,
        pages=3,
    )
    # 一次"更旧"的补拉（用户把窗口往回拖）：时间与 id 都更小
    s.accounts.save_watermark(
        workspace_id=ws,
        connection_id=cid,
        symbol=SYMBOL,
        exchange="binance",
        environment="live",
        fill_id="1000",
        ts=1_767_000_001_000,
        new_fills=0,
        pages=1,
    )
    row = s.accounts.watermark(ws, cid, SYMBOL)
    assert row["last_fill_id"] == "2500" and row["last_fill_ts"] == 1_767_000_002_500
    assert row["total_fills"] == TOTAL and row["total_pages"] == 4  # 累计计数照常累加


def test_sync_failure_records_error_without_moving_watermark(client):
    """同步失败：错误写进水位表，但**游标不动**，下次从同一位置重来。"""
    rows = binance_rows(TOTAL)
    seen: list[dict] = []
    transport = binance_transport(rows, seen)
    conn = _connect(client, transport)
    cid = conn["id"]
    _sync(client, cid)
    ws = _ws_id(client)
    s = get_services()
    before = s.accounts.watermark(ws, cid, SYMBOL)
    assert before["last_fill_id"] == str(TOTAL)

    # 让上游在下一次同步时报错
    def failing(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v3/myTrades":
            return httpx.Response(500, text="boom")
        return transport.handle_request(req)

    acc.client_factory = lambda ex, creds, env: acc.BinanceReadOnly(
        KEY, SECRET, environment=env, transport=httpx.MockTransport(failing)
    )
    r = client.post(f"/api/accounts/connections/{cid}/sync", json={"symbols": [SYMBOL]})
    # 上游 500 → AppError(UPSTREAM_UNAVAILABLE) → 端点返回 502（errors.py 的 CODE_TO_STATUS）
    assert r.status_code == 502, r.text
    assert r.json()["code"] == "UPSTREAM_UNAVAILABLE"
    assert "交易所返回错误" in r.json()["message"]  # 中文说明，不是裸异常
    after = s.accounts.watermark(ws, cid, SYMBOL)
    assert after["last_fill_id"] == str(TOTAL), "失败不能推进水位"
    assert after["last_error"] and "交易所返回错误" in after["last_error"]
    assert SECRET not in json.dumps(after, default=str)  # 水位表里绝不能出现密钥
    assert _fill_count(s.db) == TOTAL
    # 失败之后重试：仍然从水位 2500 之后拉，不会把历史重来一遍
    seen.clear()
    acc.client_factory = lambda ex, creds, env: acc.BinanceReadOnly(KEY, SECRET, environment=env, transport=transport)
    run = _sync(client, cid)
    assert run["state"] == "COMPLETED"
    assert len(seen) == 1 and seen[0]["fromId"] == str(TOTAL + 1)
    assert _fill_count(s.db) == TOTAL
    assert s.accounts.watermark(ws, cid, SYMBOL)["last_error"] is None  # 成功后清空错误


# ===========================================================================
# 4) 跨工作区不可见
# ===========================================================================
def test_watermarks_are_workspace_isolated(app, monkeypatch):
    monkeypatch.setenv("QAGENT_OPEN_REGISTRATION", "1")
    rows = binance_rows(TOTAL)
    seen: list[dict] = []
    transport = binance_transport(rows, seen)
    with TestClient(app) as a, TestClient(app) as b:
        login(a, email="a@example.com", password="Alice123!")
        login(b, email="b@example.com", password="Bobby123!")
        acc.client_factory = lambda ex, creds, env: acc.BinanceReadOnly(
            KEY, SECRET, environment=env, transport=transport
        )
        conn = a.post(
            "/api/accounts/connections",
            json={"exchange": "binance", "label": "A 的账户", "api_key": KEY, "api_secret": SECRET},
        ).json()["connection"]
        a.post(f"/api/accounts/connections/{conn['id']}/sync", json={"symbols": [SYMBOL]})
        # A 看得到自己的水位
        got = a.get(f"/api/accounts/connections/{conn['id']}/watermarks")
        assert got.status_code == 200
        assert got.json()["items"][0]["last_fill_id"] == str(TOTAL)
        # B 连这个连接都看不见（404），更看不到水位
        assert b.get(f"/api/accounts/connections/{conn['id']}/watermarks").status_code == 404
        assert b.get("/api/live/status").json()["connections"] == []
        # 直接查表：B 的 workspace 里没有这个连接的任何水位行
        db = get_services().db
        ws_b = b.get("/api/auth/me").json()["workspace"]["id"]
        assert (
            db.fetchall(
                "SELECT * FROM fills_sync_watermarks WHERE workspace_id=? AND connection_id=?", (ws_b, conn["id"])
            )
            == []
        )


def test_watermark_query_filters_by_workspace(client):
    """仓储层也要按 workspace_id 过滤，不能只靠路由层把关。"""
    seen: list[dict] = []
    conn = _connect(client, binance_transport(binance_rows(5), seen))
    s = get_services()
    ws = _ws_id(client)
    s.accounts.save_watermark(
        workspace_id=ws,
        connection_id=conn["id"],
        symbol=SYMBOL,
        exchange="binance",
        environment="live",
        fill_id="5",
        ts=1,
        new_fills=5,
        pages=1,
    )
    assert s.accounts.watermarks("ws-someone-else", conn["id"]) == {}
    assert s.accounts.watermark("ws-someone-else", conn["id"], SYMBOL) is None
    assert list(s.accounts.watermarks(ws, conn["id"])) == [SYMBOL]


def test_watermark_endpoint_has_chinese_summary_and_no_secrets(client):
    """新端点必须有中文摘要，且响应里不能出现任何密钥。"""
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/api/accounts/connections/{cid}/watermarks"]["get"]
    assert any("一" <= ch <= "鿿" for ch in op.get("summary", "")), op.get("summary")

    seen: list[dict] = []
    conn = _connect(client, binance_transport(binance_rows(TOTAL), seen))
    _sync(client, conn["id"])
    body = client.get(f"/api/accounts/connections/{conn['id']}/watermarks")
    assert body.status_code == 200
    text = body.text
    assert SECRET not in text and KEY not in text and "secret_ref" not in text
    items = body.json()["items"]
    assert len(items) == 1 and items[0]["symbol"] == SYMBOL
    assert items[0]["total_pages"] == 3 and items[0]["last_error"] is None


# ===========================================================================
# 5) OKX 端到端：水位也要落库、也要增量
# ===========================================================================
def test_okx_sync_paginates_and_resumes_from_watermark(client):
    seen: list[dict] = []
    rows = okx_rows(250)
    conn = _connect(client, okx_transport(rows, seen), exchange="okx")
    cid = conn["id"]
    seen.clear()
    run1 = _sync(client, cid)
    assert run1["state"] == "COMPLETED", run1
    assert run1["summary"]["new_fills"] == 250
    assert run1["summary"]["pages"] == 3
    assert run1["summary"]["watermarks"] == {SYMBOL: "250"}

    seen.clear()
    run2 = _sync(client, cid)
    assert run2["state"] == "COMPLETED"
    assert run2["summary"]["new_fills"] == 0
    # 增量走 before（取水位之后更新的数据），而不是 after（那会往回翻历史）
    assert seen[0]["before"] == "250", seen[0]
    assert "after" not in seen[0]
    assert _fill_count(get_services().db) == 250
