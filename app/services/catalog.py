"""角色服务（F1）、渠道绑定（F4）、用量统计（F6）。"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ..adapters import catalog as adapter_catalog
from ..adapters import get_meta, validate_config
from ..adapters.registry import ConfigValidationError
from ..db import Database, utcnow
from ..security import decrypt, encrypt_credentials, mask_secret
from .persona import PersonaService

MAX_NAME_LEN = 40
MAX_ONE_LINER_LEN = 120


class ServiceError(Exception):
    """角色/渠道/用量服务的业务异常。"""


class CharacterService:
    def __init__(self, db: Database, persona: PersonaService) -> None:
        self.db = db
        self.persona = persona

    def list_characters(self) -> list[dict[str, Any]]:
        """列表页数据。用聚合子查询一次取齐计数，避免 N+1。"""
        rows = self.db.query(
            """
            SELECT c.*,
                   (SELECT version_no FROM persona_version p
                     WHERE p.character_id = c.id AND p.is_active = 1) AS active_version_no,
                   (SELECT COUNT(*) FROM memory m WHERE m.character_id = c.id) AS memory_count,
                   (SELECT COUNT(*) FROM conversation v WHERE v.character_id = c.id) AS conversation_count,
                   (SELECT name FROM llm_provider lp
                     WHERE lp.id = c.model_provider_id) AS bound_provider_name,
                   (SELECT name FROM llm_provider lp2
                     WHERE lp2.is_default = 1) AS default_provider_name,
                   (SELECT COUNT(*) FROM channel_binding b
                     WHERE b.character_id = c.id) AS binding_count,
                   (SELECT COUNT(*) FROM channel_binding b2
                     WHERE b2.character_id = c.id AND b2.status = 'connected') AS connected_count
              FROM character c
             ORDER BY c.created_at ASC
            """
        )
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "one_liner": r["one_liner"],
                "active_version_no": r["active_version_no"] or 1,
                "memory_count": r["memory_count"],
                "conversation_count": r["conversation_count"],
                "model_provider_id": r["model_provider_id"],
                "model_provider_name": r["bound_provider_name"] or r["default_provider_name"] or "离线 Mock",
                "context_rounds": r["context_rounds"],
                "binding_count": r["binding_count"],
                "connected_count": r["connected_count"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def raw(self, character_id: str) -> dict[str, Any]:
        """原始行（含模型路由字段），供管内做模型解析。"""
        return self._row(character_id)

    def _row(self, character_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM character WHERE id = ?", (character_id,))
        if row is None:
            raise ServiceError(f"角色不存在: {character_id}")
        return dict(row)

    def get(self, character_id: str) -> dict[str, Any]:
        row = self._row(character_id)
        return {
            "id": row["id"],
            "name": row["name"],
            "one_liner": row["one_liner"],
            "model_provider_id": row["model_provider_id"],
            "context_rounds": row["context_rounds"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _validate(name: str, one_liner: str) -> tuple[str, str]:
        name = (name or "").strip()
        if not name:
            raise ServiceError("角色名称不能为空")
        if len(name) > MAX_NAME_LEN:
            raise ServiceError(f"角色名称过长，上限 {MAX_NAME_LEN} 字")
        one_liner = (one_liner or "").strip()[:MAX_ONE_LINER_LEN]
        return name, one_liner

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        """创建角色并自动生成 v1 人设版本（角色永远不会处于「无版本」状态）。

        不再提供头像字段：界面用名字首字做色块标识，少一个要填的项。
        """
        name, one_liner = self._validate(payload.get("name", ""), payload.get("one_liner", ""))
        character_id = payload.get("id") or f"ch_{uuid.uuid4().hex[:12]}"
        now = utcnow()
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO character (id, name, one_liner, created_at, updated_at) VALUES (?,?,?,?,?)",
                (character_id, name, one_liner, now, now),
            )
        self.persona.create_version(
            character_id, payload, note=payload.get("note") or "初始人设", inherit=False
        )
        return self.get(character_id)

    def update(self, character_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """只更新角色基础信息；人设字段必须走 create_version，保证历史完整。"""
        current = self.get(character_id)
        name, one_liner = self._validate(
            payload.get("name", current["name"]), payload.get("one_liner", current["one_liner"])
        )
        with self.db.write() as conn:
            conn.execute(
                "UPDATE character SET name = ?, one_liner = ?, updated_at = ? WHERE id = ?",
                (name, one_liner, utcnow(), character_id),
            )
        return self.get(character_id)

    def delete(self, character_id: str) -> None:
        with self.db.write() as conn:
            cur = conn.execute("DELETE FROM character WHERE id = ?", (character_id,))
            if cur.rowcount == 0:
                raise ServiceError(f"角色不存在: {character_id}")

    def update_model(self, character_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """更新机器人的模型路由：用哪个模型服务、带几轮上下文。"""
        current = self._row(character_id)
        provider_id = payload.get("model_provider_id", current["model_provider_id"])
        provider_id = str(provider_id or "").strip()
        if provider_id:
            row = self.db.query_one("SELECT 1 FROM llm_provider WHERE id = ?", (provider_id,))
            if row is None:
                raise ServiceError(f"模型服务不存在: {provider_id}")
        rounds = payload.get("context_rounds", current["context_rounds"])
        rounds = max(0, min(50, int(rounds)))
        with self.db.write() as conn:
            conn.execute(
                "UPDATE character SET model_provider_id = ?, context_rounds = ?, updated_at = ? WHERE id = ?",
                (provider_id, rounds, utcnow(), character_id),
            )
        return self.get(character_id)


class ChannelService:
    """渠道绑定。配置字段与校验规则全部来自适配器自己声明的 schema，
    新增平台无需改动本类、API 层或前端。"""

    def __init__(self, db: Database, manager: Any = None) -> None:
        self.db = db
        self.manager = manager

    @staticmethod
    def catalog() -> list[dict[str, Any]]:
        """渠道目录 + 配置 schema。前端据此自动渲染表单。"""
        return adapter_catalog()

    def list_bindings(self, character_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM channel_binding WHERE character_id = ? ORDER BY created_at ASC",
            (character_id,),
        )
        return [self._to_public(dict(r)) for r in rows]

    def _get_binding(self, binding_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM channel_binding WHERE id = ?", (binding_id,))
        if row is None:
            raise ServiceError(f"渠道绑定不存在: {binding_id}")
        return dict(row)

    # 适配器状态机用 running，而渠道绑定对外语义是 connected。
    # 统一在视图层映射，避免两套词汇泄漏到 API 与前端。
    _STATUS_MAP = {"running": "connected", "pending": "disconnected", "stopped": "disconnected"}

    def _to_public(self, b: dict[str, Any]) -> dict[str, Any]:
        """对外视图。credentials_enc 绝不出现，敏感字段按 schema 自动脱敏。"""
        live = self.manager.get(b["id"]) if self.manager else None
        if live is not None:
            raw_status = live.status.value
            status = self._STATUS_MAP.get(raw_status, raw_status)
        else:
            status = b["status"]
        return {
            "id": b["id"],
            "character_id": b["character_id"],
            "channel_type": b["channel_type"],
            "credentials_masked": b["credentials_mask"],
            "config": self._safe_config(b),
            "status": status,
            "last_error": (live.last_error.message if live and live.last_error else b["last_error"]),
            "stats": dict(live.stats) if live else None,
            "created_at": b["created_at"],
        }

    def _safe_config(self, b: dict[str, Any]) -> dict[str, Any]:
        """回填表单用的配置：非敏感字段回明文，secret 字段只回脱敏串。"""
        try:
            meta = get_meta(b["channel_type"])
        except ValueError:
            return {}
        raw = self._decrypt_config(b)
        out: dict[str, Any] = {}
        for f in meta.fields:
            value = raw.get(f.key, f.default)
            out[f.key] = mask_secret(str(value)) if f.type == "secret" and value else value
        return out

    def _decrypt_config(self, b: dict[str, Any]) -> dict[str, Any]:
        if not b.get("credentials_enc"):
            return {}
        try:
            data = json.loads(decrypt(b["credentials_enc"]))
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    def bind(self, character_id: str, channel_type: str, credentials: dict[str, Any]) -> dict[str, Any]:
        """按 schema 校验并落库。校验规则来自字段声明，不在此硬编码。"""
        try:
            config = validate_config(channel_type, credentials)
        except ConfigValidationError as exc:
            raise ServiceError(str(exc)) from None

        enc, mask = encrypt_credentials(config)
        binding_id = f"cb_{uuid.uuid4().hex[:12]}"
        now = utcnow()
        with self.db.write() as conn:
            if conn.execute("SELECT 1 FROM character WHERE id = ?", (character_id,)).fetchone() is None:
                raise ServiceError(f"角色不存在: {character_id}")
            if conn.execute(
                "SELECT 1 FROM channel_binding WHERE character_id = ? AND channel_type = ?",
                (character_id, channel_type),
            ).fetchone():
                raise ServiceError("该角色已绑定此渠道，请先解绑")
            conn.execute(
                """INSERT INTO channel_binding
                   (id, character_id, channel_type, credentials_enc, credentials_mask, status, last_error, created_at)
                   VALUES (?,?,?,?,?,?,'',?)""",
                (binding_id, character_id, channel_type, enc, mask, "disconnected", now),
            )
        return self._to_public(self._get_binding(binding_id))

    def update_config(self, binding_id: str, credentials: dict[str, Any]) -> dict[str, Any]:
        """更新配置。未提交或原样回传的 secret 字段沿用原值，
        避免把界面上的脱敏串当成新密钥写回。"""
        b = self._get_binding(binding_id)
        meta = get_meta(b["channel_type"])
        merged = dict(self._decrypt_config(b))
        for f in meta.fields:
            if f.key not in (credentials or {}):
                continue
            value = credentials[f.key]
            if f.type == "secret" and (value is None or value == "" or "****" in str(value)):
                continue
            merged[f.key] = value
        try:
            config = validate_config(b["channel_type"], merged)
        except ConfigValidationError as exc:
            raise ServiceError(str(exc)) from None

        enc, mask = encrypt_credentials(config)
        with self.db.write() as conn:
            conn.execute(
                "UPDATE channel_binding SET credentials_enc = ?, credentials_mask = ? WHERE id = ?",
                (enc, mask, binding_id),
            )
        return self._to_public(self._get_binding(binding_id))

    def set_status(self, binding_id: str, status: str, error: str = "") -> dict[str, Any]:
        with self.db.write() as conn:
            conn.execute(
                "UPDATE channel_binding SET status = ?, last_error = ? WHERE id = ?",
                (status, str(error)[:500], binding_id),
            )
        return self._to_public(self._get_binding(binding_id))

    async def connect(self, binding_id: str) -> dict[str, Any]:
        """启动适配器，并**等它自己跑到终态**再把状态写进库。

        直接 `await manager.start()` 只能拿到「任务起来了」；像 QQ 这种要真连上网关
        才会 `on_ready` 的适配器，此刻状态还是 connecting。立刻落库就会把
        「连接中」当成最终状态存下来，界面于是永远卡在连接中。
        """
        b = self._get_binding(binding_id)
        if self.manager is None:
            return self.set_status(binding_id, "error", "适配器管理器未就绪")
        config = self._decrypt_config(b)
        stopped = await self._stop_same_identity(b["channel_type"], b["id"], config)
        try:
            adapter = self.manager.build(b["channel_type"], b["id"], b["character_id"], config)
            await self.manager.start(adapter)
        except Exception as exc:  # noqa: BLE001
            return self.set_status(binding_id, "error", str(exc))

        if stopped:
            adapter.log(
                "warn",
                f"同一个机器人还有 {len(stopped)} 条旧连接，已先断开（同 AppID 双连会互抢消息）",
            )

        await self._wait_settled(adapter)

        err = adapter.last_error
        if err:
            return self.set_status(binding_id, "error", err.message)
        status = adapter.status.value
        # 超时仍是 connecting 时如实保留，前端会继续轮询真实状态
        return self.set_status(binding_id, self._STATUS_MAP.get(status, status))

    # 每个平台里标识「同一个机器人实例」的字段。
    # 同一个 AppID 只应有一条长连接：QQ 会把事件广播给同账号的所有连接，
    # 两条连接就会互相把对方的消息判成重复，最后谁都不回。
    IDENTITY_FIELDS = {"qq_official": "app_id", "wecom": "bot_id", "feishu": "app_id"}

    async def _stop_same_identity(
        self, channel_type: str, binding_id: str, config: dict[str, Any]
    ) -> list[str]:
        field = self.IDENTITY_FIELDS.get(channel_type)
        if self.manager is None or not field:
            return []
        value = str(config.get(field) or "").strip()
        if not value:
            return []
        stopped: list[str] = []
        for other_id, other in list(self.manager.adapters.items()):
            if other_id == binding_id or other.channel_type != channel_type:
                continue
            if str(other.config.get(field) or "").strip() == value:
                await self.manager.stop(other_id)
                stopped.append(other_id)
        return stopped

    async def stop_for_character(self, character_id: str) -> int:
        """删角色前先停掉它的渠道适配器，否则连接会变成收不到也发不出的僵尸。"""
        if self.manager is None:
            return 0
        stopped = 0
        for binding_id, adapter in list(self.manager.adapters.items()):
            if adapter.character_id == character_id:
                await self.manager.stop(binding_id)
                stopped += 1
        return stopped

    @staticmethod
    async def _wait_settled(adapter: Any, timeout: float = 12.0) -> None:
        """等适配器进入 running / error / stopped，最多 timeout 秒。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if adapter.status.value in ("running", "error", "stopped"):
                return
            await asyncio.sleep(0.1)

    def runtime(self) -> list[dict[str, Any]]:
        """所有渠道的真实运行态（内存里的适配器优先，其次是库里的状态）。

        前端在平台页会轮询它，用来把「连接中」刷成真实的「在线 / 失败」，
        也用来在适配器掉线时及时变灰。
        """
        live = {a["binding_id"]: a for a in self.manager.snapshot()} if self.manager else {}
        rows = self.db.query(
            """SELECT id, character_id, channel_type, status, last_error
                 FROM channel_binding ORDER BY created_at ASC"""
        )
        out = []
        for b in rows:
            a = live.get(b["id"])
            if a is not None:
                status = self._STATUS_MAP.get(a["status"], a["status"])
                error = a["last_error"]
                stats = a["stats"]
            else:
                status, error, stats = b["status"], b["last_error"], None
            out.append(
                {
                    "id": b["id"],
                    "character_id": b["character_id"],
                    "channel_type": b["channel_type"],
                    "status": status,
                    "last_error": error,
                    "stats": stats,
                    "live": a is not None,
                }
            )
        return out

    async def disconnect(self, binding_id: str) -> dict[str, Any]:
        self._get_binding(binding_id)
        if self.manager is not None:
            await self.manager.stop(binding_id)
        return self.set_status(binding_id, "disconnected")

    async def unbind(self, binding_id: str) -> None:
        if self.manager is not None:
            await self.manager.stop(binding_id)
        with self.db.write() as conn:
            cur = conn.execute("DELETE FROM channel_binding WHERE id = ?", (binding_id,))
            if cur.rowcount == 0:
                raise ServiceError(f"渠道绑定不存在: {binding_id}")

    async def autostart(self) -> list[dict[str, Any]]:
        """启动时自动重连上次处于 connected 的绑定。

        `connecting` 也一并重连：那是上一次连接还没落定就被重启的情况，
        不接的话用户会莫名其妙发现「明明连过，重启后却是断的」。
        """
        rows = self.db.query(
            """SELECT id FROM channel_binding
                WHERE status IN ('connected', 'connecting')
                ORDER BY created_at ASC"""
        )
        out = []
        for r in rows:
            try:
                out.append(await self.connect(r["id"]))
            except Exception:  # noqa: BLE001 — 单个绑定重连失败不应阻塞启动
                pass
        return out


class UsageService:
    RANGES = {"today": 1, "7d": 7, "30d": 30}

    def __init__(self, db: Database) -> None:
        self.db = db

    def summary(self, range_key: str = "today") -> dict[str, Any]:
        from .pipeline import estimate_cost

        days = self.RANGES.get(range_key, 1)
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        # 必须与 utcnow() 的毫秒精度对齐：'...T00:00:00Z' 在字典序上大于
        # '...T00:00:00.001Z'（'.' < 'Z'），会把当天数据整体漏掉。
        start_iso = start.isoformat(timespec="milliseconds").replace("+00:00", "Z")

        rows = self.db.query(
            """SELECT c.id, c.name,
                      COUNT(v.id) AS conversations,
                      COALESCE(SUM(v.prompt_tokens), 0) AS prompt_tokens,
                      COALESCE(SUM(v.completion_tokens), 0) AS completion_tokens
                 FROM character c
                 LEFT JOIN conversation v
                        ON v.character_id = c.id AND v.created_at >= ? AND v.status = 'done'
                GROUP BY c.id
                ORDER BY conversations DESC, c.created_at ASC""",
            (start_iso,),
        )
        by_character = []
        totals = {"conversations": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_estimate": 0.0}
        for r in rows:
            pt, ct = int(r["prompt_tokens"]), int(r["completion_tokens"])
            cost = estimate_cost(pt, ct)
            by_character.append(
                {
                    "character_id": r["id"],
                    "name": r["name"],
                    "conversations": int(r["conversations"]),
                    "prompt_tokens": pt,
                    "completion_tokens": ct,
                    "cost_estimate": cost,
                }
            )
            totals["conversations"] += int(r["conversations"])
            totals["prompt_tokens"] += pt
            totals["completion_tokens"] += ct
            totals["cost_estimate"] = round(totals["cost_estimate"] + cost, 6)

        series_rows = self.db.query(
            """SELECT substr(created_at, 1, 10) AS d,
                      COUNT(*) AS conversations,
                      COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens
                 FROM conversation WHERE created_at >= ? AND status = 'done'
                GROUP BY d ORDER BY d ASC""",
            (start_iso,),
        )
        found = {r["d"]: r for r in series_rows}
        series = []
        for i in range(days):
            day = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            hit = found.get(day)
            series.append(
                {
                    "date": day,
                    "conversations": int(hit["conversations"]) if hit else 0,
                    "tokens": int(hit["tokens"]) if hit else 0,
                }
            )

        return {"range": range_key, "since": start_iso, "totals": totals, "by_character": by_character, "series": series}

    def conversations(self, character_id: str, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        rows = self.db.query(
            """SELECT v.*, p.version_no
                 FROM conversation v
                 LEFT JOIN persona_version p ON p.id = v.persona_version_id
                WHERE v.character_id = ?
                ORDER BY v.created_at DESC LIMIT ?""",
            (character_id, limit),
        )
        out = []
        for r in rows:
            try:
                hit_ids = json.loads(r["hit_memory_ids"] or "[]")
            except ValueError:
                hit_ids = []
            out.append(
                {
                    "id": r["id"],
                    "user_message": r["user_message"],
                    "assistant_message": r["assistant_message"],
                    "persona_version_no": r["version_no"],
                    "hit_memory_ids": hit_ids,
                    "prompt_tokens": r["prompt_tokens"],
                    "completion_tokens": r["completion_tokens"],
                    "latency_ms": r["latency_ms"],
                    "status": r["status"],
                    "model": r["model"],
                    "created_at": r["created_at"],
                }
            )
        return out
