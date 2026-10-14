"""签名与精度工具。两个交易所共用，避免各写一份走样。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode


def hmac_sha256_hex(secret: str, payload: str) -> str:
    """Binance 风格：HMAC-SHA256 十六进制小写。"""
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def hmac_sha256_b64(secret: str, payload: str) -> str:
    """OKX 风格：HMAC-SHA256 后 base64。"""
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def iso_timestamp_ms(ms: int | None = None) -> str:
    """OKX 要求的 ISO8601 毫秒时间戳，形如 2026-10-12T03:04:05.678Z。"""
    ms = ms if ms is not None else int(time.time() * 1000)
    seconds, millis = divmod(ms, 1000)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds))
    return f"{stamp}.{millis:03d}Z"


def signed_query(params: dict[str, object], secret: str) -> str:
    """Binance 风格：参数按字典序拼接后签名，签名附在末尾。"""
    payload = urlencode(sorted((k, v) for k, v in params.items() if v is not None))
    signature = hmac_sha256_hex(secret, payload)
    return f"{payload}&signature={signature}"


def decimal_str(value: Any, digits: int = 8) -> str:
    """把数量/价格格式化成不带科学计数法的字符串（Decimal 精确，不经过浮点）。

    交易所对精度很敏感，`1e-05` 这种写法会被直接拒单。
    """
    from decimal import ROUND_DOWN, Decimal

    d = value if isinstance(value, Decimal) else Decimal(repr(value) if isinstance(value, float) else str(value))
    d = d.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_DOWN)
    text = format(d, "f").rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"
