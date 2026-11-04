"""凭证保护：加密存储 + 脱敏展示。

合规要求（可行性文档 3.3 第 4 条）：
- AppSecret / BotSecret 不得明文出现在界面与日志；
- 展示只保留前后各 4 位。

本项目定位单机单用户，不引入 KMS。采用 Fernet 对称加密，
密钥来自环境变量 FISHCONSOLE_SECRET_KEY；未设置时按实例密钥文件生成，
文件权限收敛到当前用户。密钥缺失不会导致明文落库。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

_KEY_ENV = "FISHCONSOLE_SECRET_KEY"
_KEY_FILE = Path(os.environ.get("FISHCONSOLE_KEY_FILE", ".fishconsole_key"))


def _load_key() -> bytes:
    raw = os.environ.get(_KEY_ENV)
    if not raw:
        if _KEY_FILE.exists():
            raw = _KEY_FILE.read_text(encoding="utf-8").strip()
        else:
            raw = secrets.token_urlsafe(32)
            _KEY_FILE.write_text(raw, encoding="utf-8")
            try:
                os.chmod(_KEY_FILE, 0o600)
            except OSError:
                # Windows 下 chmod 语义有限，忽略即可
                pass
    return hashlib.sha256(raw.encode("utf-8")).digest()


_KEY = _load_key()


def _keystream(nonce: bytes, length: int) -> bytes:
    """基于 HMAC-SHA256 的计数器模式密钥流（标准库实现，无第三方依赖）。"""
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(_KEY, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt(plaintext: str) -> str:
    """加密并附加 HMAC 完整性标签，返回 base64 字符串。"""
    data = plaintext.encode("utf-8")
    nonce = secrets.token_bytes(12)
    cipher = bytes(a ^ b for a, b in zip(data, _keystream(nonce, len(data))))
    tag = hmac.new(_KEY, nonce + cipher, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(nonce + tag + cipher).decode("ascii")


def decrypt(token: str) -> str:
    """解密；密文被篡改或密钥不匹配时抛 ValueError。"""
    try:
        blob = base64.urlsafe_b64decode(token.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("凭证密文格式非法") from exc
    if len(blob) < 28:
        raise ValueError("凭证密文长度非法")
    nonce, tag, cipher = blob[:12], blob[12:28], blob[28:]
    expect = hmac.new(_KEY, nonce + cipher, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(tag, expect):
        raise ValueError("凭证完整性校验失败（密钥变更或数据被篡改）")
    return bytes(a ^ b for a, b in zip(cipher, _keystream(nonce, len(cipher)))).decode("utf-8")


def mask_secret(value: str) -> str:
    """脱敏：仅保留前 4 位与后 4 位，中间固定替换。

    过短的值整体打码，避免反推。
    """
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]} **** **** {value[-4:]}"


_FIELD_LABELS = {
    "app_id": "AppID",
    "app_secret": "Secret",
    "bot_id": "BotID",
    "bot_secret": "Secret",
    "app_key": "Key",
    "secret": "Secret",
    "token": "Token",
    "password": "Password",
}
_SECRET_HINTS = ("secret", "key", "token", "password")


def mask_credentials(creds: dict[str, str]) -> str:
    """把凭证字典渲染成一行可安全展示的字符串。

    按字段名判断敏感度（含 secret / key / token / password 的走脱敏），
    这样 QQ 的 AppID+Secret、企微的 BotID+Secret 都能自动正确展示，
    新增平台不需要改这里。
    """
    parts: list[str] = []
    for key, raw in (creds or {}).items():
        if not isinstance(raw, str):
            continue
        value = raw.strip()
        if not value:
            continue
        label = _FIELD_LABELS.get(key, key)
        if any(hint in key.lower() for hint in _SECRET_HINTS) or len(value) > 12:
            value = mask_secret(value)
        parts.append(f"{label} {value}")
    return " · ".join(parts) or "—"


def encrypt_credentials(creds: dict[str, str]) -> tuple[str, str]:
    """返回 (密文, 脱敏展示串)。调用方只应把脱敏串交给 API 层。"""
    return encrypt(json.dumps(creds, ensure_ascii=False)), mask_credentials(creds)
