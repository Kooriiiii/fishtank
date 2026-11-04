"""模型服务（AstrBot 的「服务提供商」）—— CRUD、凭证加密、连通性自检。

设计取舍：
- 只做 **OpenAI 兼容** 一种协议。DeepSeek / Moonshot / 硅基流动 / Ollama /
  one-api 全部兼容 `/chat/completions`，一个实现覆盖绝大多数情况，
  新增协议等真有需求再说（AstrBot 也是靠 provider 插件做扩展）。
- 每次调用都按 id 现读配置，不缓存实例：改完 Key 立刻生效，
  不需要重启服务，也不会出现「改了配置但还在用旧 Key」的错觉。
- Key 只加密落库，接口只回脱敏串；更新时回传脱敏串不会覆盖真实 Key。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from ..db import Database, utcnow
from ..security import decrypt, encrypt, mask_secret

PROVIDER_TYPES = {
    "openai_chat_completion": "OpenAI 兼容（/chat/completions）",
}

# 常见服务商的默认地址，前端「预设」下拉用。
PRESETS = [
    {
        "key": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    {
        "key": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    {
        "key": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
    },
    {
        "key": "moonshot",
        "label": "Moonshot / Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
    },
    {
        "key": "dashscope",
        "label": "阿里云百炼",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    {
        "key": "ollama",
        "label": "本地 Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
    },
]

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"


class ProviderError(Exception):
    """模型服务业务异常，由 API 层翻译成 400。"""


def row_to_provider(row: Any) -> dict[str, Any]:
    """数据库行 -> 对外视图。绝不返回 api_key_enc / 明文 Key。"""
    return {
        "id": row["id"],
        "name": row["name"],
        "provider_type": row["provider_type"],
        "base_url": row["base_url"],
        "model": row["model"],
        "temperature": float(row["temperature"]),
        "max_tokens": int(row["max_tokens"]),
        "api_key_mask": row["api_key_mask"],
        "is_default": bool(row["is_default"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class ProviderService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------- 查询 ----------

    def list_providers(self) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM llm_provider ORDER BY is_default DESC, created_at ASC")
        return [row_to_provider(r) for r in rows]

    def get_row(self, provider_id: str) -> Any:
        row = self.db.query_one("SELECT * FROM llm_provider WHERE id = ?", (provider_id,))
        if row is None:
            raise ProviderError(f"模型服务不存在: {provider_id}")
        return row

    def get(self, provider_id: str) -> dict[str, Any]:
        return row_to_provider(self.get_row(provider_id))

    def default_row(self) -> Any:
        return self.db.query_one(
            "SELECT * FROM llm_provider ORDER BY is_default DESC, created_at ASC LIMIT 1"
        )

    def api_key(self, provider_id: str) -> str:
        """取明文 Key。只在内置的模型调用与自检里用，绝不进 API 响应。"""
        row = self.get_row(provider_id)
        if not row["api_key_enc"]:
            return ""
        try:
            return decrypt(row["api_key_enc"])
        except ValueError as exc:
            raise ProviderError(f"{row['name']} 的 Key 解密失败：{exc}") from None

    # ---------- 写入 ----------

    @staticmethod
    def _validate(payload: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
        base = current or {
            "name": "",
            "provider_type": "openai_chat_completion",
            "base_url": DEFAULT_BASE_URL,
            "model": DEFAULT_MODEL,
            "temperature": 0.7,
            "max_tokens": 1000,
        }
        name = str(payload.get("name") or base["name"]).strip()
        if not name:
            raise ProviderError("服务名称不能为空")
        if len(name) > 40:
            raise ProviderError("服务名称过长，上限 40 字")

        provider_type = str(payload.get("provider_type") or base["provider_type"])
        if provider_type not in PROVIDER_TYPES:
            raise ProviderError(f"不支持的协议类型: {provider_type}")

        base_url = str(payload.get("base_url") or base["base_url"]).strip().rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            raise ProviderError("接口地址必须以 http:// 或 https:// 开头")

        model = str(payload.get("model") or base["model"]).strip()
        if not model:
            raise ProviderError("模型名称不能为空")

        try:
            temperature = float(payload.get("temperature", base["temperature"]))
        except (TypeError, ValueError):
            raise ProviderError("温度必须是数字") from None
        if not 0.0 <= temperature <= 2.0:
            raise ProviderError("温度取值范围 0 ~ 2")

        try:
            max_tokens = int(payload.get("max_tokens", base["max_tokens"]))
        except (TypeError, ValueError):
            raise ProviderError("最大输出长度必须是整数") from None
        if not 1 <= max_tokens <= 32000:
            raise ProviderError("最大输出长度取值范围 1 ~ 32000")

        return {
            "name": name,
            "provider_type": provider_type,
            "base_url": base_url,
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self._validate(payload)
        api_key = str(payload.get("api_key") or "").strip()
        if not api_key:
            raise ProviderError("API Key 不能为空")
        provider_id = f"prov_{uuid.uuid4().hex[:12]}"
        now = utcnow()
        is_default = bool(payload.get("is_default"))
        with self.db.write() as conn:
            # 第一个服务自动设为默认，省掉「建完还得点一下默认」的多余操作
            total = conn.execute("SELECT COUNT(*) AS c FROM llm_provider").fetchone()["c"]
            if total == 0:
                is_default = True
            if is_default:
                conn.execute("UPDATE llm_provider SET is_default = 0")
            conn.execute(
                """INSERT INTO llm_provider
                   (id, name, provider_type, base_url, api_key_enc, api_key_mask, model,
                    temperature, max_tokens, is_default, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    provider_id,
                    data["name"],
                    data["provider_type"],
                    data["base_url"],
                    encrypt(api_key),
                    mask_secret(api_key),
                    data["model"],
                    data["temperature"],
                    data["max_tokens"],
                    int(is_default),
                    now,
                    now,
                ),
            )
        return self.get(provider_id)

    def update(self, provider_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        row = self.get_row(provider_id)
        current = row_to_provider(row)
        data = self._validate(payload, current)

        api_key = str(payload.get("api_key") or "").strip()
        # 前端回传脱敏串或留空：沿用原 Key，不把掩码写进库
        keep_key = not api_key or "****" in api_key
        enc, mask = (row["api_key_enc"], row["api_key_mask"])
        if not keep_key:
            enc, mask = encrypt(api_key), mask_secret(api_key)

        is_default = bool(payload["is_default"]) if "is_default" in payload else bool(row["is_default"])
        with self.db.write() as conn:
            if is_default:
                conn.execute("UPDATE llm_provider SET is_default = 0 WHERE id != ?", (provider_id,))
            conn.execute(
                """UPDATE llm_provider SET name = ?, provider_type = ?, base_url = ?,
                       api_key_enc = ?, api_key_mask = ?, model = ?, temperature = ?,
                       max_tokens = ?, is_default = ?, updated_at = ?
                     WHERE id = ?""",
                (
                    data["name"],
                    data["provider_type"],
                    data["base_url"],
                    enc,
                    mask,
                    data["model"],
                    data["temperature"],
                    data["max_tokens"],
                    int(is_default),
                    utcnow(),
                    provider_id,
                ),
            )
        return self.get(provider_id)

    def delete(self, provider_id: str) -> None:
        row = self.get_row(provider_id)
        with self.db.write() as conn:
            conn.execute("DELETE FROM llm_provider WHERE id = ?", (provider_id,))
            # 角色指向被删的模型服务时回落到默认，避免留下悬空引用
            conn.execute(
                "UPDATE character SET model_provider_id = '' WHERE model_provider_id = ?",
                (provider_id,),
            )
            if row["is_default"]:
                nxt = conn.execute(
                    "SELECT id FROM llm_provider ORDER BY created_at ASC LIMIT 1"
                ).fetchone()
                if nxt is not None:
                    conn.execute("UPDATE llm_provider SET is_default = 1 WHERE id = ?", (nxt["id"],))

    def set_default(self, provider_id: str) -> dict[str, Any]:
        self.get_row(provider_id)
        with self.db.write() as conn:
            conn.execute("UPDATE llm_provider SET is_default = 0")
            conn.execute(
                "UPDATE llm_provider SET is_default = 1, updated_at = ? WHERE id = ?",
                (utcnow(), provider_id),
            )
        return self.get(provider_id)

    # ---------- 连通性自检 ----------

    def fetch_models(self, provider_id: str) -> list[str]:
        """拉取服务端模型列表（OpenAI 兼容的 GET /models）。"""
        row = self.get_row(provider_id)
        key = self.api_key(provider_id)
        url = f"{row['base_url'].rstrip('/')}/models"
        try:
            resp = httpx.get(
                url,
                headers={"Authorization": f"Bearer {key}"} if key else {},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"拉取模型列表失败：HTTP {exc.response.status_code} {exc.response.text[:200]}"
            ) from None
        except Exception as exc:  # noqa: BLE001 — 网络类异常统一转成可读提示
            raise ProviderError(f"拉取模型列表失败：{exc}") from None

        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ProviderError("该服务未返回标准模型列表（data 字段）")
        return sorted(str(i.get("id")) for i in items if isinstance(i, dict) and i.get("id"))

    def test(self, provider_id: str, model: str = "") -> dict[str, Any]:
        """发一句最小对话验证「地址 + Key + 模型名」三者可用。

        失败不抛异常，而是把原因放进结果里返回：自检页要显示的是诊断信息，
        不是 400 错误页。
        """
        row = self.get_row(provider_id)
        key = self.api_key(provider_id)
        use_model = model or row["model"]
        started = time.perf_counter()
        try:
            resp = httpx.post(
                f"{row['base_url'].rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {key}"} if key else {},
                json={
                    "model": use_model,
                    "messages": [{"role": "user", "content": "回复 OK 两个字母即可"}],
                    "max_tokens": 16,
                    "temperature": 0,
                },
                timeout=30.0,
            )
            latency = int((time.perf_counter() - started) * 1000)
            if resp.status_code >= 400:
                return {
                    "ok": False,
                    "latency_ms": latency,
                    "model": use_model,
                    "error": f"HTTP {resp.status_code}: {resp.text[:300]}",
                }
            data = resp.json()
            content = (data["choices"][0]["message"]["content"] or "").strip()
            usage = data.get("usage") or {}
            return {
                "ok": True,
                "latency_ms": latency,
                "model": use_model,
                "reply": content[:200],
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "model": use_model,
                "error": f"{type(exc).__name__}: {exc}",
            }
