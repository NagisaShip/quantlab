"""个人账户只读接入（readonly_live）。

原则（plan v2.0 第 161–185 行）：
- 只读：连接前检测 API Key 权限；**开启了交易或提现权限的 Key 一律拒绝**，不保存；
- 只读客户端里**没有任何下单 / 撤单 / 提现方法**——不是“没调用”，而是结构上不存在；
- 凭证用独立主密钥加密后单独存放（secret_ref 指向密文），数据库与接口只出现掩码与 connection_id；
- 撤销连接只删除平台保存的密文与引用（加密擦除），不会也无法替用户在交易所侧删除 Key，
  界面明确提示用户到交易所后台删除或禁用；
- 测试网（testnet）连接单独标注，其成交永远标为测试，不进入实盘结果。
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
import time
import uuid
from base64 import b64encode
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from ..platform.db import Database, IntegrityError, jdump, jload, now_ms
from ..platform.errors import AppError, ErrorCode
from ..platform.secretbox import SecretBox

log = logging.getLogger("quantlab.accounts")

EXCHANGES = ("binance", "okx")

#: T01：成交分页的最大页数上限。防御"游标没往前走"这类上游异常把 worker 卡死——
#: 没有上限的话，只要交易所持续返回满页，循环就永远不会结束，而这次请求持有的是 run 的租约，
#: 卡住的后果不只是慢：租约过期会被判成 worker 崩溃，任务被别的 worker 反复重试。
#: 触顶时**已拉到的成交照常返回**（少一段时间的数据好过整批丢掉），并在日志与同步记录里留痕。
BINANCE_FILLS_MAX_PAGES = 20  # 20 × 1000 = 单次同步最多 2 万条成交
OKX_FILLS_MAX_PAGES = 50  # 50 × 100 = 单次同步最多 5000 条成交
ENDPOINTS = {
    ("binance", "live"): "https://api.binance.com",
    ("binance", "testnet"): "https://testnet.binance.vision",
    ("okx", "live"): "https://www.okx.com",
    ("okx", "testnet"): "https://www.okx.com",
}


@dataclass
class PermissionReport:
    ok: bool
    can_read: bool
    can_trade: bool
    can_withdraw: bool
    ip_restricted: bool | None
    account_id: str = ""
    server_time_skew_ms: int | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReadOnlyClient(Protocol):
    def permissions(self) -> PermissionReport: ...
    def balances(self) -> list[dict[str, str]]: ...

    def fills(
        self,
        symbols: list[str],
        since: int | None,
        *,
        after_id: str | None = None,
        max_pages: int = ...,
    ) -> tuple[list[dict[str, Any]], int]:
        """返回 (成交列表, 请求页数)。

        `after_id` 是**每个交易对各自的水位**，单值签名装不下，所以实际调用走
        `fetch_fills()`（见下）；这个 Protocol 只是让替身实现有据可依。
        """
        ...


def _mask_id(v: Any) -> str:
    s = str(v or "")
    return (s[:3] + "***" + s[-2:]) if len(s) > 6 else ("***" if s else "")


def _as_int(v: Any) -> int | None:
    """交易所返回的成交号 / 时间戳 → int。非数字返回 None，由调用方决定怎么降级。"""
    if v is None or v == "":
        return None
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def _row_id(row: dict[str, Any], key: str) -> int | None:
    """从成交行里取成交号（币安 `id` / OKX `tradeId`）。

    两家交易所的成交号都是十进制整数且单调递增，但 JSON 里是字符串。非数字
    （上游换了字段类型）时返回 None，由调用方降级成"不能用 id 作游标"，
    而不是抛异常——同步到一半因为一个字段格式崩掉是最差的结果。
    """
    return _as_int(row.get(key))


def _fill_dict(
    row: dict[str, Any],
    symbol: str,
    *,
    exchange: str,
    ts_key: str,
    qty_key: str,
    price_key: str,
    side: str | None = None,
) -> dict[str, Any]:
    """交易所成交行 → 平台统一形状（live/service.py::wire_fill 的输入）。"""
    fee_currency = row.get("commissionAsset") if exchange == "binance" else row.get("feeCcy")
    return {
        "ts": int(_as_int(row.get(ts_key)) or 0),
        "instrument": symbol,
        "side": row.get("side") if side is None else side,
        "qty": row[qty_key],
        "price": row[price_key],
        "fee": str(abs(float(row.get("fee") or 0))) if exchange == "okx" else row.get("commission", "0"),
        "fee_currency": fee_currency,
        "order_id": str(row.get("orderId") or row.get("ordId") or ""),
        "fill_id": str(row.get("id") or row.get("tradeId") or ""),
        "client_order_id": row.get("clOrdId") or None,
    }


class BinanceReadOnly:
    """币安只读客户端：只实现 GET 账户接口。"""

    def __init__(
        self, key: str, secret: str, *, environment: str = "live", transport: httpx.BaseTransport | None = None
    ):
        self.key, self.secret, self.env = key, secret, environment
        self.c = httpx.Client(base_url=ENDPOINTS[("binance", environment)], timeout=15.0, transport=transport)

    def _get(self, path: str, params: dict[str, Any] | None = None, *, signed: bool = True) -> Any:
        p = dict(params or {})
        headers = {"X-MBX-APIKEY": self.key}
        if signed:
            p["timestamp"] = int(time.time() * 1000)
            p["recvWindow"] = 10_000
            q = urlencode(p)
            p["signature"] = hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        r = self.c.get(path, params=p, headers=headers)
        if r.status_code in (401, 403):
            raise AppError(
                ErrorCode.CREDENTIAL_REVOKED,
                f"交易所拒绝了该 Key（HTTP {r.status_code}）：可能已删除、过期或 IP 不在白名单",
            )
        if r.status_code == 429 or r.status_code == 418:
            raise AppError(ErrorCode.UPSTREAM_RATE_LIMITED, "交易所限流，请稍后再试")
        if r.status_code >= 400:
            raise AppError(ErrorCode.UPSTREAM_UNAVAILABLE, f"交易所返回错误 HTTP {r.status_code}：{r.text[:200]}")
        return r.json()

    def permissions(self) -> PermissionReport:
        checks: list[dict[str, Any]] = []
        st = self._get("/api/v3/time", signed=False)
        skew = int(st.get("serverTime", 0)) - int(time.time() * 1000)
        checks.append({"name": "时间同步", "ok": abs(skew) < 5000, "detail": f"本机与交易所时间差 {skew} ms"})
        acct = self._get("/api/v3/account", {"omitZeroBalances": "true"})
        can_trade = bool(acct.get("canTrade"))
        can_withdraw = bool(acct.get("canWithdraw"))
        ip_restricted: bool | None = None
        if self.env == "live":
            r = self._get("/sapi/v1/account/apiRestrictions")
            can_trade = bool(r.get("enableSpotAndMarginTrading") or r.get("enableFutures") or r.get("enableMargin"))
            can_withdraw = bool(
                r.get("enableWithdrawals") or r.get("enableInternalTransfer") or r.get("permitsUniversalTransfer")
            )
            ip_restricted = bool(r.get("ipRestrict"))
        checks.append({"name": "账户身份", "ok": True, "detail": f"账户 {_mask_id(acct.get('uid'))}"})
        return _decide(self.env, can_trade, can_withdraw, ip_restricted, _mask_id(acct.get("uid")), skew, checks)

    def balances(self) -> list[dict[str, str]]:
        acct = self._get("/api/v3/account", {"omitZeroBalances": "true"})
        return [
            {"currency": b["asset"], "free": b["free"], "locked": b["locked"]}
            for b in acct.get("balances", [])
            if float(b.get("free") or 0) or float(b.get("locked") or 0)
        ]

    def fills(
        self,
        symbols: list[str],
        since: int | None,
        *,
        after_id: str | None = None,
        max_pages: int = BINANCE_FILLS_MAX_PAGES,
    ) -> tuple[list[dict[str, Any]], int]:
        """拉取成交，**按 fromId 游标翻页**直到取完（T01）。

        `since` 与 `after_id` 的分工（两个都要，缺一个就会出错）：

        - `after_id`（水位，来自 fills_sync_watermarks.last_fill_id）：增量续拉的**主判据**。
          有它时用 `fromId = after_id + 1`，直接跳过已经入库的成交，请求量只跟"新增了多少笔"有关，
          跟账户历史总量无关。
        - `since`（时间水位）：只在没有 `after_id` 时用（首次同步、或上游没给成交号）。
          这时退回 `startTime` 语义，只兜住水位之后的成交。

        为什么不一直用 `startTime`：币安的 `startTime` 是**闭区间**，上次同步到的最后一笔
        在下一轮会被原样再拉一次；只差一笔还算小事，真正的坑是当同一毫秒有多笔成交时，
        无法从时间上把它们切开。`fromId` 是成交序号，可以精确切分。

        币安约束（`/api/v3/myTrades`）：
        - `fromId` 与 `startTime`+`endTime` **不能同时传**，一起传会被拒；所以有 `after_id` 时
          只传 `fromId`，没有时才传 `startTime`；
        - 返回按 id **升序**，本页 **按 id 再排一次**：不能假设交易所永远保持有序，
          游标取的是"本页最大的 id"，顺序错了游标就会往回退，造成下一轮重复拉整段数据；
        - 不传 `fromId` 时返回的是**最近** `limit` 笔（接近倒序分页语义），
          拿到后按 id 升序排好就变成"最老 → 最新"，与 `fromId` 模式一致。

        返回 `(成交列表, 请求页数)`。页数返回给调用方记进水位表（`total_pages`），
        这样"长期顶在页数上限"这件事在数据库里看得见，而不是只出现在某一行日志里。
        """
        out: list[dict[str, Any]] = []
        pages = 0
        for sym in symbols:
            inst = sym.replace("/", "")
            cursor = _as_int(after_id)
            rows: list[dict[str, Any]] = []
            for _page in range(max_pages):
                params: dict[str, Any] = {"symbol": inst, "limit": 1000}
                if cursor is not None:
                    # cursor 是"已经拉到过的最后一笔 id"，下一页从它的下一条开始
                    params["fromId"] = cursor + 1
                elif since:
                    params["startTime"] = int(since)
                batch = self._get("/api/v3/myTrades", params)
                pages += 1
                if not isinstance(batch, list):
                    raise AppError(
                        ErrorCode.UPSTREAM_UNAVAILABLE,
                        f"{sym} 的成交分页返回了非列表数据，已停止本次同步以免混入脏数据",
                    )
                if not batch:
                    break
                batch = sorted(batch, key=lambda r: _row_id(r, "id") or 0)
                rows.extend(batch)
                if len(batch) < 1000:
                    break  # 不足一页 = 已经拉完
                last = _row_id(batch[-1], "id")
                if last is None:
                    raise AppError(
                        ErrorCode.UPSTREAM_UNAVAILABLE,
                        f"{sym} 的成交分页缺少成交号（id），无法用游标续拉；已停止本次同步以免漏拉",
                    )
                if cursor is not None and last <= cursor:
                    # 游标没往前走的唯一原因是上游忽略/异常处理了 fromId。继续循环只会
                    # 把同一页拉 max_pages 遍，所以立刻停下来报错，而不是安静地空转。
                    raise AppError(
                        ErrorCode.UPSTREAM_UNAVAILABLE,
                        f"{sym} 的成交分页游标没有前进（fromId={cursor + 1} 仍返回 id<={cursor}），已停止本次同步",
                    )
                cursor = last
            else:
                log.warning(
                    "币安成交同步触及页数上限 %d 页（%s）：本次最多拉取 %d 条，剩余部分等下次同步继续",
                    max_pages,
                    sym,
                    max_pages * 1000,
                )
            for r in rows:
                out.append(
                    _fill_dict(
                        r,
                        sym,
                        exchange="binance",
                        ts_key="time",
                        qty_key="qty",
                        price_key="price",
                        side="buy" if r.get("isBuyer") else "sell",
                    )
                )
        return out, pages


class OKXReadOnly:
    """OKX 只读客户端：只实现 GET 账户接口。"""

    def __init__(
        self,
        key: str,
        secret: str,
        passphrase: str,
        *,
        environment: str = "live",
        transport: httpx.BaseTransport | None = None,
    ):
        self.key, self.secret, self.passphrase, self.env = key, secret, passphrase, environment
        self.c = httpx.Client(base_url=ENDPOINTS[("okx", environment)], timeout=15.0, transport=transport)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        q = ("?" + urlencode(params)) if params else ""
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"
        sign = b64encode(hmac.new(self.secret.encode(), f"{ts}GET{path}{q}".encode(), hashlib.sha256).digest()).decode()
        headers = {
            "OK-ACCESS-KEY": self.key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
        }
        if self.env == "testnet":
            headers["x-simulated-trading"] = "1"
        r = self.c.get(path + q, headers=headers)
        if r.status_code in (401, 403):
            raise AppError(ErrorCode.CREDENTIAL_REVOKED, f"交易所拒绝了该 Key（HTTP {r.status_code}）")
        if r.status_code == 429:
            raise AppError(ErrorCode.UPSTREAM_RATE_LIMITED, "交易所限流，请稍后再试")
        body = r.json() if r.content else {}
        if r.status_code >= 400 or str(body.get("code", "0")) != "0":
            msg = body.get("msg") or r.text[:200]
            code = (
                ErrorCode.CREDENTIAL_REVOKED
                if str(body.get("code")) in ("50111", "50113", "50119")
                else ErrorCode.UPSTREAM_UNAVAILABLE
            )
            raise AppError(code, f"交易所返回错误：{msg}")
        return body.get("data", [])

    def permissions(self) -> PermissionReport:
        cfg = (self._get("/api/v5/account/config") or [{}])[0]
        perm = str(cfg.get("perm", ""))
        can_trade = "trade" in perm
        can_withdraw = "withdraw" in perm
        ip_restricted = bool(cfg.get("ip")) if "ip" in cfg else None
        checks = [{"name": "账户身份", "ok": True, "detail": f"账户 {_mask_id(cfg.get('uid'))}，权限 {perm or '未知'}"}]
        return _decide(self.env, can_trade, can_withdraw, ip_restricted, _mask_id(cfg.get("uid")), None, checks)

    def balances(self) -> list[dict[str, str]]:
        out = []
        for d in self._get("/api/v5/account/balance"):
            for x in d.get("details", []):
                out.append({"currency": x["ccy"], "free": x.get("availBal", "0"), "locked": x.get("frozenBal", "0")})
        return out

    def fills(
        self,
        symbols: list[str],
        since: int | None,
        *,
        after_id: str | None = None,
        max_pages: int = OKX_FILLS_MAX_PAGES,
    ) -> tuple[list[dict[str, Any]], int]:
        """拉取成交，**按 after / before 游标翻页**直到取完（T01）。

        ⚠️ OKX 的 `after` / `before` 语义与直觉**相反**，务必看清再改：

        - `after=<成交号>` 取的是**比这个 id 更早**（更旧）的数据。名字来自"从这一条之后
          继续往前翻"，不是"时间上比它晚"。开区间：该 id 自己不会被返回。
        - `before=<成交号>` 取的是**比这个 id 更新**的数据。同样是开区间。
        - 两个都传时只有 `after` 生效（`before` 被忽略），所以这里永远只传一个。
        - 两个方向都是"从游标出发**最近的** limit 条"（这是分页接口的通用契约），
          所以沿着同一方向换游标就能连续翻页，不会漏中间的记录。

        因此两个方向各有用途，代码里分两条分支：

        - **增量**（`after_id` = 水位，即已知的最大成交号）：用 `before = 水位`，
          只取水位之后的**新**成交；还有更多时把游标推到"本页最大成交号"继续往前，
          直到某页不足 limit。请求量只跟"新增了多少笔"有关，与账户历史总量无关。
        - **首次 / 手工补拉**（没有水位）：不带游标拿最新一页，然后用
          `after = 本页最小成交号` 一直往回翻到不足一页，或到 `since` 圈定的起点为止。
          `since` 用 `begin`（时间过滤），只在没有游标时使用——时间边界是闭区间，
          拿它做增量判定会把上一笔重复拉一遍。

        返回 `(成交列表, 请求页数)`，页数语义与币安实现一致。
        """
        out: list[dict[str, Any]] = []
        pages = 0
        for sym in symbols:
            inst = sym.replace("/", "-")
            cursor = _as_int(after_id)
            # `incremental` 在循环外定一次：它表示"这次同步的起点是不是水位"，
            # 而不是"当前游标是不是空"。首次同步时 cursor 也会在第二页起变成非空，
            # 但方向仍然是往旧翻（after），不能因为 cursor 有值就切到 before。
            incremental = cursor is not None
            rows: list[dict[str, Any]] = []
            for _page in range(max_pages):
                params: dict[str, Any] = {"instType": "SPOT", "instId": inst, "limit": "100"}
                if incremental:
                    # before = 取比该成交号更新的数据（开区间，不含自己）
                    params["before"] = str(cursor)
                elif cursor is not None:
                    # 首次同步翻到第二页起：after = 取比该成交号更早的数据（开区间）
                    params["after"] = str(cursor)
                elif since:
                    params["begin"] = str(int(since))
                batch = self._get("/api/v5/trade/fills-history", params)
                pages += 1
                if not isinstance(batch, list):
                    raise AppError(
                        ErrorCode.UPSTREAM_UNAVAILABLE,
                        f"{sym} 的成交分页返回了非列表数据，已停止本次同步以免混入脏数据",
                    )
                if not batch:
                    break
                rows.extend(batch)
                if len(batch) < 100:
                    break  # 不足一页 = 这个方向已经翻到头
                ids = [_row_id(r, "tradeId") for r in batch]
                if any(i is None for i in ids):
                    raise AppError(
                        ErrorCode.UPSTREAM_UNAVAILABLE,
                        f"{sym} 的成交分页缺少成交号（tradeId），无法用游标续拉；已停止本次同步以免漏拉",
                    )
                nxt = max(ids) if incremental else min(ids)
                if nxt == cursor:
                    # 游标没往前走。继续翻只会把同一页拉 max_pages 遍，所以立刻停下报错，
                    # 把"上游不认这个游标参数"这件事暴露出来，而不是安静地空转到页数上限。
                    raise AppError(
                        ErrorCode.UPSTREAM_UNAVAILABLE,
                        f"{sym} 的成交分页游标没有前进（{'before' if incremental else 'after'}={cursor}），"
                        "已停止本次同步",
                    )
                cursor = nxt
            else:
                log.warning(
                    "OKX 成交同步触及页数上限 %d 页（%s）：本次最多拉取 %d 条，剩余部分等下次同步继续",
                    max_pages,
                    sym,
                    max_pages * 100,
                )
            for r in rows:
                out.append(_fill_dict(r, sym, exchange="okx", ts_key="ts", qty_key="fillSz", price_key="fillPx"))
        return out, pages


def _decide(
    env: str,
    can_trade: bool,
    can_withdraw: bool,
    ip_restricted: bool | None,
    account_id: str,
    skew: int | None,
    checks: list[dict[str, Any]],
) -> PermissionReport:
    reasons = []
    if can_withdraw:
        reasons.append(
            "该 Key 开启了提现 / 划转权限：平台只接受只读 Key，已拒绝且未保存"
            + ("" if env == "live" else "（测试环境同样不接受）")
        )
    if can_trade:
        reasons.append(
            "该 Key 开启了交易权限：平台只接受只读 Key，已拒绝且未保存"
            + ("" if env == "live" else "（测试环境同样不接受）")
        )
    checks.append({"name": "只读权限", "ok": not reasons, "detail": "仅读取" if not reasons else "；".join(reasons)})
    checks.append(
        {
            "name": "IP 限制",
            "ok": bool(ip_restricted),
            "detail": (
                "已绑定 IP 白名单"
                if ip_restricted
                else "未绑定 IP 白名单：强烈建议在交易所后台限制为本服务的出口 IP"
                if ip_restricted is False
                else "交易所未返回 IP 限制信息"
            ),
        }
    )
    return PermissionReport(
        ok=not reasons,
        can_read=True,
        can_trade=can_trade,
        can_withdraw=can_withdraw,
        ip_restricted=ip_restricted,
        account_id=account_id,
        server_time_skew_ms=skew,
        checks=checks,
        reasons=reasons,
    )


# 测试可替换：client_factory(exchange, creds, environment) -> ReadOnlyClient
def default_client_factory(exchange: str, creds: dict[str, str], environment: str) -> ReadOnlyClient:
    if exchange == "binance":
        return BinanceReadOnly(creds["api_key"], creds["api_secret"], environment=environment)
    if exchange == "okx":
        return OKXReadOnly(
            creds["api_key"], creds["api_secret"], creds.get("passphrase") or "", environment=environment
        )
    raise AppError(ErrorCode.UNSUPPORTED_CAPABILITY, f"不支持的交易所：{exchange}")


client_factory = default_client_factory

#: `fills()` 是否支持 after_id 游标的缓存（键是客户端类）
_CURSOR_SUPPORT: dict[type, bool] = {}


def _accepts_cursor(client: ReadOnlyClient) -> bool:
    """该客户端的 `fills()` 是否支持 `after_id` 游标参数。

    用**签名自省**而不是 `try/except TypeError`：后者会把 `fills()` 内部的
    真实 TypeError（例如交易所字段缺失）当成"不支持游标"吞掉，然后静默退回全量拉取
    ——那正是 T01 要修的 bug 的一种伪装形态。签名查一次就够，结果缓存下来，
    免得每次同步都对同一个类做一遍反射。
    """
    fn = getattr(client, "fills", None)
    if fn is None:
        return False
    cls = type(client)
    cached = _CURSOR_SUPPORT.get(cls)
    if cached is not None:
        return cached
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True  # 拿不到签名（C 扩展 / 动态包装）时按新签名试，错了会显式报错
    ok = "after_id" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    _CURSOR_SUPPORT[cls] = ok
    return ok


def fetch_fills(
    client: ReadOnlyClient,
    symbols: list[str],
    *,
    after_ids: dict[str, str | None] | None = None,
    since: int | None = None,
) -> tuple[list[dict[str, Any]], int, dict[str, str | None]]:
    """按交易对**逐个**拉取成交，每个交易对带自己的游标水位。

    为什么要逐个调用而不是把 `after_ids` 塞进 `client.fills()` 的签名里：
    水位是"每个交易对一条"，单值参数装不下；而把所有水位压成"全局最小 id"是错的
    ——不同交易对的成交号属于**不同的序列**（币安 BTCUSDT 与 ETHUSDT 的 id 各自递增），
    取最小值会让 id 较大的那个交易对每轮都从头再拉一遍。

    返回值第三项是**本次各交易对拉到的最大成交号**（不是旧水位），由调用方合并进水位表；
    某个交易对一条都没拉到时为 None，调用方就不会拿 None 去覆盖已有水位。

    兼容性：不支持 `after_id` 的老实现（例如只实现 `fills(symbols, since)` 的替身）
    退回老调用方式，此时只能靠 `since` 做增量——能力变弱但不会报错。
    返回类型同样两种都认：tuple 是新签名 `(成交, 页数)`，list 是老签名。
    """
    out: list[dict[str, Any]] = []
    pages = 0
    seen: dict[str, str | None] = {}
    cursor_ok = _accepts_cursor(client)
    for sym in symbols:
        cursor = (after_ids or {}).get(sym)
        if cursor_ok:
            res = client.fills([sym], since, after_id=cursor)
        else:
            res = client.fills([sym], since)
        if isinstance(res, tuple):
            rows, n_pages = res[0], int(res[1])
        else:  # 老式实现：只返回列表
            rows, n_pages = res, 1
        pages += n_pages
        out.extend(rows)
        ids = [int(r["fill_id"]) for r in rows if str(r.get("fill_id") or "").isdigit()]
        seen[sym] = str(max(ids)) if ids else None
    return out, pages, seen


class AccountService:
    def __init__(self, db: Database, secrets: SecretBox, home: Any) -> None:
        self.db = db
        self.secrets = secrets
        self.dir = home / "account-secrets"
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- 密文：每个连接一个文件，文件名 = secret_ref；撤销即删除（加密擦除）----
    def _write_secret(self, creds: dict[str, str]) -> str:
        ref = uuid.uuid4().hex
        path = self.dir / f"{ref}.enc"
        path.write_text(self.secrets.encrypt(json.dumps(creds)), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return ref

    def _read_secret(self, ref: str) -> dict[str, str]:
        path = self.dir / f"{ref}.enc"
        if not ref or not path.exists():
            raise AppError(ErrorCode.CREDENTIAL_REVOKED, "该连接的凭证已删除，请重新连接")
        return json.loads(self.secrets.decrypt(path.read_text(encoding="utf-8")))

    def _delete_secret(self, ref: str | None) -> None:
        if ref:
            p = self.dir / f"{ref}.enc"
            if p.exists():
                p.write_bytes(b"\0" * p.stat().st_size)
                p.unlink()

    @staticmethod
    def _out(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        out = {k: v for k, v in row.items() if k != "secret_ref"}
        out["scope"] = jload(row.get("scope"), {})
        out["last_test"] = jload(row.get("last_test"), {})
        out["ip_restricted"] = bool(row.get("ip_restricted"))
        out["connection_id"] = row["id"]
        return out

    def list(self, workspace_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM account_connections WHERE workspace_id=? ORDER BY created_at DESC", (workspace_id,)
        )
        return [self._out(r) for r in rows]

    def get(self, workspace_id: str, cid: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM account_connections WHERE id=? AND workspace_id=?", (cid, workspace_id))
        if row is None:
            raise AppError(ErrorCode.NOT_FOUND, "连接不存在")
        return row

    def connect(
        self,
        *,
        workspace_id: str,
        user_id: str,
        exchange: str,
        environment: str,
        label: str,
        creds: dict[str, str],
        entity: str = "spot",
    ) -> dict[str, Any]:
        """测试权限 → 只读才保存。返回连接（含掩码，不含凭证）。"""
        if exchange not in EXCHANGES:
            raise AppError(ErrorCode.UNSUPPORTED_CAPABILITY, f"暂不支持 {exchange}；可选：{', '.join(EXCHANGES)}")
        if environment not in ("live", "testnet"):
            raise AppError(ErrorCode.INVALID_PARAMETER, "environment 只能是 live（只读实盘）或 testnet（官方测试环境）")
        client = client_factory(exchange, creds, environment)
        report = client.permissions()
        if not report.ok:
            raise AppError(ErrorCode.PERMISSION_DENIED, "；".join(report.reasons), details={"report": report.to_dict()})
        cid = f"conn-{uuid.uuid4().hex[:12]}"
        key = creds.get("api_key", "")
        mask = f"{key[:4]}…{key[-4:]}" if len(key) > 10 else "****"
        scope = {
            "read": True,
            "trade": report.can_trade,
            "withdraw": report.can_withdraw,
            "environment": environment,
            "account": report.account_id,
        }
        # 先落库、后写密文：反过来的话，插入失败（例如唯一约束冲突）会留下一个
        # 没有任何连接引用的孤儿密文文件，既占空间又没人清理。
        self.db.execute(
            "INSERT INTO account_connections (id, workspace_id, created_by, exchange, entity, label, key_mask, secret_"
            "ref, "
            "scope, status, last_test, created_at, environment, ip_restricted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                cid,
                workspace_id,
                user_id,
                exchange,
                entity,
                label[:60] or exchange,
                mask,
                None,
                jdump(scope),
                "active",
                jdump({"at": now_ms(), **report.to_dict()}),
                now_ms(),
                environment,
                int(bool(report.ip_restricted)),
            ),
        )
        try:
            ref = self._write_secret(creds)
            self.db.execute("UPDATE account_connections SET secret_ref=? WHERE id=?", (ref, cid))
        except Exception:
            # 密文没写成，连接就不能留着（否则它是个读不到凭证的坏连接）
            self.db.execute("DELETE FROM account_connections WHERE id=?", (cid,))
            raise
        return self._out(self.get(workspace_id, cid))

    def test(self, workspace_id: str, cid: str) -> dict[str, Any]:
        row = self.get(workspace_id, cid)
        if row["status"] == "revoked":
            raise AppError(ErrorCode.CREDENTIAL_REVOKED, "连接已撤销")
        try:
            report = client_factory(
                row["exchange"], self._read_secret(row["secret_ref"]), row["environment"]
            ).permissions()
            status = "active" if report.ok else "rejected"
            err = None if report.ok else "；".join(report.reasons)
            last = {"at": now_ms(), **report.to_dict()}
        except AppError as exc:
            status = "expired" if exc.code == ErrorCode.CREDENTIAL_REVOKED else row["status"]
            err, last = exc.message, {"at": now_ms(), "ok": False, "error": exc.message, "code": exc.code}
        self.db.execute(
            "UPDATE account_connections SET status=?, last_test=?, last_error=? WHERE id=?",
            (status, jdump(last), err, cid),
        )
        if status == "rejected":
            # 权限被放大（例如用户事后开启了交易）：立即删除密文，停止同步
            self._delete_secret(row["secret_ref"])
            self.db.execute("UPDATE account_connections SET secret_ref=NULL WHERE id=?", (cid,))
        return self._out(self.get(workspace_id, cid))

    def revoke(self, workspace_id: str, cid: str) -> dict[str, Any]:
        row = self.get(workspace_id, cid)
        self._delete_secret(row["secret_ref"])
        self.db.execute(
            "UPDATE account_connections SET status='revoked', secret_ref=NULL, revoked_at=? WHERE id=?", (now_ms(), cid)
        )
        return self._out(self.get(workspace_id, cid))

    def client(self, workspace_id: str, cid: str) -> tuple[dict[str, Any], ReadOnlyClient]:
        row = self.get(workspace_id, cid)
        if row["status"] != "active":
            raise AppError(ErrorCode.CREDENTIAL_REVOKED, f"连接状态为 {row['status']}，不能同步")
        return row, client_factory(row["exchange"], self._read_secret(row["secret_ref"]), row["environment"])

    def mark_synced(self, cid: str, run_id: str, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE account_connections SET last_sync_at=?, sync_run_id=?, last_error=? WHERE id=?",
            (now_ms(), run_id, error, cid),
        )

    # ---- 同步水位（T01）：每个交易对一条，下次只从水位之后拉 ----
    def watermarks(self, workspace_id: str, connection_id: str) -> dict[str, dict[str, Any]]:
        """该连接的水位，键是交易对。**必须按 workspace_id 过滤**：
        只按 connection_id 查的话，恶意/写错的 workspace 头就能读到别的租户的同步进度
        （connection_id 是随机串猜不到，但"猜不到"不是隔离，隔离要靠条件本身）。"""
        rows = self.db.fetchall(
            "SELECT * FROM fills_sync_watermarks WHERE workspace_id=? AND connection_id=?",
            (workspace_id, connection_id),
        )
        return {r["symbol"]: r for r in rows}

    def watermark(self, workspace_id: str, connection_id: str, symbol: str) -> dict[str, Any] | None:
        return self.db.fetchone(
            "SELECT * FROM fills_sync_watermarks WHERE workspace_id=? AND connection_id=? AND symbol=?",
            (workspace_id, connection_id, symbol),
        )

    def save_watermark(
        self,
        *,
        workspace_id: str,
        connection_id: str,
        symbol: str,
        exchange: str,
        environment: str,
        fill_id: str | None,
        ts: int | None,
        new_fills: int = 0,
        pages: int = 0,
        error: str | None = None,
    ) -> None:
        """写回水位（UPSERT）。

        水位**只前进不后退**：`fill_id` 与 `ts` 都用 MAX(旧值, 新值) 合并。
        为什么这件事值得写一段代码而不是直接覆盖：
        时间水位取的是"本次返回里最大的成交时间"，而 `after` / `fromId` 翻页模式下，
        越往后翻拿到的成交越旧，所以一次"增量"同步返回的最大时间戳完全可能小于
        上次记录的水位（例如用户把同步窗口往回拖了一截来做对账）。直接覆盖会让水位倒退，
        下一轮就要把已经入库的历史再拉一遍。成交号单调，所以取大是安全的。
        """
        now = now_ms()
        old = self.watermark(workspace_id, connection_id, symbol)
        prev_ts = int(old["last_fill_ts"]) if old and old.get("last_fill_ts") is not None else None
        prev_id = str(old["last_fill_id"]) if old and old.get("last_fill_id") else None
        merged_ts = max([t for t in (prev_ts, ts) if t is not None], default=None)
        # 成交号是数字时按数值比较，不是数字（上游换了格式）就退回字符串比较
        if prev_id and fill_id:
            try:
                merged_id = str(max(int(prev_id), int(fill_id)))
            except (TypeError, ValueError):
                merged_id = fill_id
        else:
            merged_id = fill_id or prev_id
        if old is None:
            try:
                self.db.execute(
                    "INSERT INTO fills_sync_watermarks (workspace_id, connection_id, symbol, exchange, environment, "
                    "last_fill_id, last_fill_ts, last_batch_size, total_fills, total_pages, last_sync_at, last_error, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        workspace_id,
                        connection_id,
                        symbol,
                        exchange,
                        environment,
                        merged_id,
                        merged_ts,
                        int(new_fills),
                        int(new_fills),
                        int(pages),
                        now,
                        error,
                        now,
                        now,
                    ),
                )
            except IntegrityError:
                # 并发（两个 worker / 两次同步）同时插入同一行：退化成更新，不让同步失败
                self.db.execute(
                    "UPDATE fills_sync_watermarks SET last_fill_id=?, last_fill_ts=?, last_batch_size=?, "
                    "total_fills=total_fills+?, total_pages=total_pages+?, last_sync_at=?, last_error=?, updated_at=? "
                    "WHERE workspace_id=? AND connection_id=? AND symbol=?",
                    (
                        merged_id,
                        merged_ts,
                        int(new_fills),
                        int(new_fills),
                        int(pages),
                        now,
                        error,
                        now,
                        workspace_id,
                        connection_id,
                        symbol,
                    ),
                )
        else:
            self.db.execute(
                "UPDATE fills_sync_watermarks SET last_fill_id=?, last_fill_ts=?, last_batch_size=?, "
                "total_fills=total_fills+?, total_pages=total_pages+?, last_sync_at=?, last_error=?, updated_at=? "
                "WHERE workspace_id=? AND connection_id=? AND symbol=?",
                (
                    merged_id,
                    merged_ts,
                    int(new_fills),
                    int(new_fills),
                    int(pages),
                    now,
                    error,
                    now,
                    workspace_id,
                    connection_id,
                    symbol,
                ),
            )


GUIDES: dict[str, dict[str, Any]] = {
    "binance": {
        "name": "币安（Binance）",
        "doc": "https://www.binance.com/zh-CN/support/faq/360002502072",
        "steps": [
            "登录币安网页版 → 头像 → API 管理 → 创建 API（系统生成）；建议名称写明“QuantLab 只读”",
            "权限只勾选“允许读取”（Enable Reading）；不要勾选现货交易、杠杆、合约、提现、内部划转",
            "在“IP 访问限制”中选择“仅限受信任的 IP”，填入本服务的出口 IP",
            "复制 API Key 与 Secret Key（Secret 只显示一次），粘贴到本页面；平台加密保存，只回显掩码",
            "点击“测试连接”：平台检查时间同步、账户身份与权限；开启了交易或提现的 Key 会被拒绝且不保存",
            "**先小额验证**：建议先用测试网（Testnet）Key 或只读权限的小额账户跑通一遍，"
            "确认同步到的成交与你自己在交易所看到的对得上，再接入正式账户",
            "测试通过后点击“同步”，读取余额与成交；在“实盘对照”里选择要对照的策略",
        ],
    },
    "okx": {
        "name": "OKX",
        "doc": "https://www.okx.com/zh-hans/help/how-can-i-create-an-api-key",
        "steps": [
            "登录 OKX → 个人中心 → API → 创建 V5 API Key；用途选择“第三方应用”或“API 交易”",
            "权限只勾选“读取”；不要勾选“交易”“提现”",
            "绑定 IP 地址（填本服务出口 IP）；设置 Passphrase 并妥善保存",
            "把 API Key、Secret Key、Passphrase 粘贴到本页面；平台加密保存，只回显掩码",
            "**先小额验证**：建议先用测试环境 Key 或只读权限的小额账户跑通一遍，"
            "确认同步到的成交与交易所页面一致，再接入正式账户",
            "测试连接通过后同步余额与成交",
        ],
    },
}
COMMON_NOTES = [
    "平台不收集登录密码、短信验证码、谷歌验证码、助记词或网页登录凭证",
    "粘贴 Key 不会启动任何交易：这是只读连接；微实盘使用单独页面和单独 Key",
    "删除连接只删除平台保存的加密凭证与引用；交易所侧的 API Key 仍然有效，请到交易所后台删除或禁用",
    "**解绑不代表仓位已平仓**：解绑只停止平台的读取，交易所账户里的持仓、挂单与委托原样保留，"
    "平台不会、也无法替你平仓；如需平仓请自行到交易所操作",
    "解绑后平台不再能读到该账户的新成交，已导入的历史成交与对照报告仍会保留",
    "测试网（testnet）连接的成交一律标为“测试”，不会出现在实盘结果中",
]

#: 解绑/删除连接时必须展示给用户的说明（plan v2.0 第 175 行）
UNBIND_NOTICE = {
    "title": "解绑不会平仓",
    "points": [
        "解绑（删除连接）只删除平台保存的加密凭证与引用，**不涉及任何仓位操作**",
        "交易所账户里的持仓、未成交委托、资金原样保留，平台不会替你卖出或撤单",
        "该只读连接没有下单能力；微实盘使用独立连接，不会因解绑只读账户而平仓",
        "如果跟单执行器正在跟随，解绑前请先在执行器侧选择源策略关闭后的处置方式（保留仓位 / 停止新信号 / 平掉仓位）",
        "要真正停止账户的读取权限，请到交易所后台删除或禁用该 API Key",
    ],
}
