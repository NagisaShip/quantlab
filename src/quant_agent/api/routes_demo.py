"""交易所模拟盘连接 + 实时行情网关状态。

模拟盘 Key 只用于交易所的模拟账户（Binance Demo Mode / Spot Testnet、OKX 模拟盘、Bybit Demo Trading / Testnet）。
平台没有实盘下单路径：这里能连的地址是常量，见 live/demo_broker.py。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from fastapi import APIRouter, Query

from ..platform import context
from .deps import get_services
from .schemas import DemoConnectIn

router = APIRouter(prefix="/api/demo", tags=["交易所模拟盘"])


@router.get("/environments")
def environments() -> dict:
    """可接的模拟环境，以及每个环境的价格是否跟实盘。"""
    return {
        "environments": get_services().demo.environments(),
        "note": "平台只接交易所的模拟账户，没有实盘下单路径。",
    }


@router.get("/connections")
def list_connections() -> dict:
    return {"connections": get_services().demo.list()}


@router.post("/connections")
def connect(payload: DemoConnectIn) -> dict:
    """接一个模拟盘账户：先向模拟环境查一次余额，验证通过才加密保存。"""
    services = get_services()
    conn = services.demo.connect(
        exchange=payload.exchange,
        env=payload.env,
        label=payload.label,
        creds={"api_key": payload.api_key, "api_secret": payload.api_secret, "passphrase": payload.passphrase},
        user=context.current().user_id or "",
    )
    services.audit.record(
        "demo.connect",
        object_type="demo_connection",
        object_id=conn["id"],
        details={"exchange": conn["exchange"], "env": conn["env"]},
    )
    return {"connection": conn}


@router.post("/connections/{cid}/test")
def test_connection(cid: str) -> dict:
    return {"connection": get_services().demo.test(cid)}


@router.delete("/connections/{cid}")
def revoke_connection(cid: str) -> dict:
    services = get_services()
    conn = services.demo.revoke(cid)
    services.audit.record("demo.revoke", object_type="demo_connection", object_id=cid)
    return {"connection": conn, "note": "平台保存的密钥已擦除。交易所那边的 Key 请到交易所后台删除。"}


# ---------------------------------------------------------------------------
# 盘中行情（交易终端画图用）：API 进程里按需订阅，60 秒没人看就断开
# ---------------------------------------------------------------------------
_LAST_SEEN: dict[tuple[str, str, str], float] = {}
_LOCK = threading.Lock()
IDLE_SECONDS = 60.0


def _sweep(gw: Any) -> None:
    now = time.monotonic()
    for key, seen in list(_LAST_SEEN.items()):
        if now - seen > IDLE_SECONDS:
            _LAST_SEEN.pop(key, None)
            gw.release(*key)


@router.get("/live")
def live_quote(
    symbol: str = "BTC/USDT", timeframe: str = "1h", exchange: str = Query("binance", pattern="^(binance|okx|bybit)$")
) -> dict:
    """正在形成的 K 线与网关状态。网关关闭（QAGENT_STREAM=0）或连不上时 enabled/healthy 为 false，
    界面退回只显示已收盘 K 线。"""
    from ..data import stream
    from ..data.quality import TIMEFRAME_MS

    if not stream.enabled():
        return {"enabled": False, "forming": None}
    if timeframe not in TIMEFRAME_MS:
        return {"enabled": False, "forming": None, "error": f"不支持的周期：{timeframe}"}
    from .tasks import _backfill_for

    gw = stream.gateway(_backfill_for)
    key = (exchange, symbol, timeframe)
    with _LOCK:
        _sweep(gw)
        if key not in _LAST_SEEN:
            gw.subscribe(*key)
        _LAST_SEEN[key] = time.monotonic()
    st = gw.get(*key)
    snap = st.snapshot() if st else {}
    return {"enabled": True, **snap}
