"""服务端凭证加密。

- 交易所 API Key / Secret / Passphrase、大模型密钥都只以密文形式落盘；
- 加密主密钥来自 `QAGENT_SECRET_KEY` 环境变量，或独立的密钥文件
  （默认在数据目录之外的 `<数据目录>-keys/secret.key`），**不与数据库或数据目录放在一起**，
  因此单独泄露一份数据库备份不足以解出凭证；
- 接口只返回掩码与引用 id，明文绝不回到浏览器、日志或导出文件。
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretError(RuntimeError):
    pass


def mask(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * 6}{value[-keep:]}"


class SecretBox:
    def __init__(self, data_home: Path) -> None:
        self.data_home = Path(data_home)
        self._fernet = Fernet(self._load_key())

    def key_path(self) -> Path:
        explicit = os.environ.get("QAGENT_SECRET_KEY_FILE")
        if explicit:
            return Path(explicit)
        return self.data_home.parent / f"{self.data_home.name}-keys" / "secret.key"

    def _load_key(self) -> bytes:
        if (
            os.environ.get("QUANTLAB_MODE") == "local"
            and os.name == "nt"
            and not os.environ.get("QAGENT_SECRET_KEY")
            and not os.environ.get("QAGENT_SECRET_KEY_FILE")
        ):
            from ..local.credentials import master_key

            return master_key(self.data_home)
        env = os.environ.get("QAGENT_SECRET_KEY", "").strip()
        if env:
            try:
                raw = base64.urlsafe_b64decode(env.encode())
                if len(raw) != 32:
                    raise ValueError
            except ValueError as exc:
                raise SecretError(
                    "QAGENT_SECRET_KEY 必须是 32 字节的 urlsafe base64（可用 qagent gen-key 生成）"
                ) from exc
            return env.encode()
        path = self.key_path()
        if path.exists():
            return path.read_bytes().strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(key)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(path)
        return key

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise SecretError("凭证无法解密：加密主密钥已更换或密文损坏") from exc


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")
