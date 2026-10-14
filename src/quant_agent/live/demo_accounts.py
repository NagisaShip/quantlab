"""交易所模拟盘连接：保存模拟盘 API Key（加密），给实时模拟提供下单客户端。

和“只读账户接入”（live/accounts.py）分开存放、分开管理：
- 只读接入只收**没有交易权限**的实盘 Key；这里只收**模拟盘** Key，必须能交易。
- 连接时只向模拟环境的地址验证（查一次余额），验证不过不保存。
- 密钥用平台主密钥加密，单独一个文件；撤销即擦除文件。接口只回显掩码。
- 密钥只在 worker 进程里解密，交给 OMS；策略代码所在的隔离进程永远拿不到。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from ..platform.errors import AppError, ErrorCode
from . import demo_broker as DB

COLLECTION = "demo_connections"

#: 测试可替换：broker_factory(exchange, env, creds) -> DemoBroker
broker_factory = DB.make_broker


def _now() -> int:
    return int(time.time() * 1000)


class DemoConnectionService:
    def __init__(self, store: Any, secrets: Any, home: Path) -> None:
        self.store = store
        self.secrets = secrets
        self.dir = Path(home) / "demo-secrets"
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- 密文 ----
    def _write_secret(self, creds: dict[str, str]) -> str:
        ref = uuid.uuid4().hex
        p = self.dir / f"{ref}.enc"
        p.write_text(self.secrets.encrypt(json.dumps(creds)), encoding="utf-8")
        try:
            p.chmod(0o600)
        except OSError:
            pass
        return ref

    def _read_secret(self, ref: str) -> dict[str, str]:
        p = self.dir / f"{ref}.enc"
        if not ref or not p.exists():
            raise AppError(ErrorCode.CREDENTIAL_REVOKED, "该模拟盘连接的密钥已删除，请重新连接")
        return json.loads(self.secrets.decrypt(p.read_text(encoding="utf-8")))

    def _wipe(self, ref: str | None) -> None:
        if ref:
            p = self.dir / f"{ref}.enc"
            if p.exists():
                p.write_bytes(b"\0" * p.stat().st_size)
                p.unlink()

    @staticmethod
    def _out(doc: dict[str, Any]) -> dict[str, Any]:
        env = DB.ENVIRONMENTS.get((doc.get("exchange", ""), doc.get("env", "")), {})
        return {
            **{k: v for k, v in doc.items() if k != "secret_ref"},
            "env_label": env.get("label", ""),
            "realistic": env.get("realistic", False),
            "env_note": env.get("note", ""),
        }

    # ---- 增删改查 ----
    @staticmethod
    def environments() -> list[dict[str, Any]]:
        return [
            {"exchange": ex, "env": env, **{k: v for k, v in meta.items() if k != "base"}, "host": meta["base"]}
            for (ex, env), meta in DB.ENVIRONMENTS.items()
        ]

    def list(self) -> list[dict[str, Any]]:
        docs = sorted(self.store.list(COLLECTION), key=lambda d: -(d.get("created_at") or 0))
        return [self._out(d) for d in docs]

    def get(self, cid: str) -> dict[str, Any]:
        doc = self.store.get(COLLECTION, cid)
        if doc is None:
            raise AppError(ErrorCode.NOT_FOUND, "模拟盘连接不存在")
        return doc

    def _probe(self, exchange: str, env: str, creds: dict[str, str]) -> dict[str, Any]:
        broker = broker_factory(exchange, env, creds)
        try:
            bal = broker.balances()
        except DB.BrokerError as exc:
            raise AppError(ErrorCode.PERMISSION_DENIED, f"模拟盘验证失败：{exc}。请确认填的是模拟盘 Key") from exc
        except DB.BrokerUnknown as exc:
            raise AppError(ErrorCode.UPSTREAM_UNAVAILABLE, f"连不上 {broker.label}：{exc}") from exc
        finally:
            broker.close()
        top = sorted(((k, v) for k, v in bal.items() if v > 0), key=lambda kv: -float(kv[1]))[:8]
        return {"at": _now(), "ok": True, "balances": {k: str(v) for k, v in top}}

    def connect(self, *, exchange: str, env: str, label: str, creds: dict[str, str], user: str = "") -> dict[str, Any]:
        if (exchange, env) not in DB.ENVIRONMENTS:
            raise AppError(ErrorCode.INVALID_PARAMETER, f"不支持的模拟环境：{exchange}/{env}")
        # 只有 OKX 需要 Passphrase；Binance / Bybit 只要 Key + Secret
        need = ["api_key", "api_secret"] + (["passphrase"] if exchange == "okx" else [])
        missing = [k for k in need if not (creds.get(k) or "").strip()]
        if missing:
            raise AppError(ErrorCode.INVALID_PARAMETER, "缺少：" + "、".join(missing))
        creds = {k: creds[k].strip() for k in need}
        test = self._probe(exchange, env, creds)
        key = creds["api_key"]
        doc = {
            "id": f"demo-{uuid.uuid4().hex[:10]}",
            "exchange": exchange,
            "env": env,
            "label": (label or DB.ENVIRONMENTS[(exchange, env)]["label"])[:60],
            "key_mask": f"{key[:4]}…{key[-4:]}" if len(key) > 10 else "****",
            "status": "active",
            "last_test": test,
            "created_at": _now(),
            "created_by": user,
            "secret_ref": None,
        }
        self.store.upsert(COLLECTION, doc)
        try:
            doc["secret_ref"] = self._write_secret(creds)
            self.store.upsert(COLLECTION, doc)
        except Exception:
            self.store.delete(COLLECTION, doc["id"])
            raise
        return self._out(doc)

    def test(self, cid: str) -> dict[str, Any]:
        doc = self.get(cid)
        try:
            doc["last_test"] = self._probe(doc["exchange"], doc["env"], self._read_secret(doc.get("secret_ref") or ""))
            doc["status"] = "active"
        except AppError as exc:
            doc["last_test"] = {"at": _now(), "ok": False, "error": exc.message}
            doc["status"] = "error" if doc.get("secret_ref") else "revoked"
        self.store.upsert(COLLECTION, doc)
        return self._out(doc)

    def revoke(self, cid: str) -> dict[str, Any]:
        doc = self.get(cid)
        self._wipe(doc.get("secret_ref"))
        doc.update(secret_ref=None, status="revoked", revoked_at=_now())
        self.store.upsert(COLLECTION, doc)
        return self._out(doc)

    def broker(self, cid: str) -> tuple[dict[str, Any], DB.DemoBroker]:
        """只在 worker 里调用：解密密钥，返回下单客户端。"""
        doc = self.get(cid)
        if doc.get("status") == "revoked" or not doc.get("secret_ref"):
            raise AppError(ErrorCode.CREDENTIAL_REVOKED, "模拟盘连接已撤销")
        return self._out(doc), broker_factory(doc["exchange"], doc["env"], self._read_secret(doc["secret_ref"]))
