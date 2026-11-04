"""运行统计：概览 KPI、消息趋势、平台/模型用量排名、会话用量。

数据源只有一个 —— `conversation` 表。每次对话都会写入人设版本、渠道、
模型名、token 用量、耗时与状态，统计页只是把这些行按不同维度聚合，
不再单独维护一套计数器，避免「统计和实际对话对不上」。

时间分桶按**本地时区**做：库里存的是 UTC，统计页看到的「今天」应该是
用户手机上的今天，而不是 UTC 的今天，否则东八区会整体偏 8 小时。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..adapters.registry import ADAPTER_META
from ..db import Database

# AstrBot 的统计页给了 1 天 / 3 天 / 1 周，这里再补 30 天和全部
RANGES: dict[str, int] = {"1d": 1, "3d": 3, "7d": 7, "30d": 30, "all": 0}
RANGE_LABELS = {
    "1d": "最近 1 天",
    "3d": "最近 3 天",
    "7d": "最近 7 天",
    "30d": "最近 30 天",
    "all": "全部",
}


def _parse(iso: str) -> datetime | None:
    """把库里的 ISO-8601 UTC 字符串解析成带时区的 datetime。"""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _fmt_uptime(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


@dataclass
class StatsService:
    db: Database
    manager: Any = None
    started_at: float = field(default_factory=time.time)
    version: str = "2.0.0"

    # ---------- 采集 ----------

    def _rows(self) -> list[dict[str, Any]]:
        """一次取全量对话行（单机个人机器人，量级很小），带上角色名。"""
        rows = self.db.query(
            """SELECT v.id, v.character_id, c.name AS character_name, v.channel_type,
                      v.session_key, v.model, v.prompt_tokens, v.completion_tokens,
                      v.latency_ms, v.status, v.created_at
                 FROM conversation v
                 LEFT JOIN character c ON c.id = v.character_id"""
        )
        out = []
        for r in rows:
            item = dict(r)
            item["created"] = _parse(item["created_at"])
            if item["created"] is None:
                continue
            item["tokens"] = int(item["prompt_tokens"] or 0) + int(item["completion_tokens"] or 0)
            out.append(item)
        return out

    @staticmethod
    def _platform_meta(channel_type: str) -> tuple[str, str]:
        meta = ADAPTER_META.get(channel_type)
        if meta is None:
            return channel_type, "🔌"
        return meta.label, meta.icon

    def _platform_counts(self) -> tuple[int, int]:
        """(已配置绑定数, 正在运行的适配器数)。"""
        total = self.db.query_one("SELECT COUNT(*) AS c FROM channel_binding")
        running = 0
        if self.manager is not None:
            running = sum(1 for a in self.manager.snapshot() if a["status"] == "running")
        return int(total["c"]) if total else 0, running

    @staticmethod
    def _resources() -> dict[str, Any]:
        """CPU / 内存。psutil 是可选依赖，没装就不显示这几张卡。"""
        try:
            import psutil
        except ImportError:
            return {"available": False}
        try:
            proc = psutil.Process()
            return {
                "available": True,
                "cpu_percent": round(proc.cpu_percent(interval=None), 1),
                "system_cpu_percent": round(psutil.cpu_percent(interval=None), 1),
                "memory_mb": round(proc.memory_info().rss / 1024 / 1024, 1),
                "system_memory_mb": round(psutil.virtual_memory().total / 1024 / 1024),
            }
        except Exception:  # noqa: BLE001 — 取不到就当没有，不能让统计页挂掉
            return {"available": False}

    # ---------- 概览 ----------

    def overview(self) -> dict[str, Any]:
        now = time.time()
        uptime = now - self.started_at
        rows = self._rows()

        local_today = datetime.now().astimezone().date()
        today_rows = [r for r in rows if r["created"].astimezone().date() == local_today]
        today_done = [r for r in today_rows if r["status"] == "done"]
        today_tokens = sum(r["tokens"] for r in today_done)
        today_latency = (
            round(sum(r["latency_ms"] for r in today_done) / len(today_done)) if today_done else 0
        )

        total, running = self._platform_counts()
        characters = self.db.query_one("SELECT COUNT(*) AS c FROM character")
        providers = self.db.query_one("SELECT COUNT(*) AS c FROM llm_provider")

        recent = sorted(rows, key=lambda r: r["created"], reverse=True)[:8]

        return {
            "started_at": datetime.fromtimestamp(self.started_at, tz=timezone.utc).isoformat(timespec="seconds"),
            "uptime_seconds": round(uptime),
            "uptime_text": _fmt_uptime(uptime),
            "version": self.version,
            "platforms": {"total": total, "running": running},
            "characters": int(characters["c"]) if characters else 0,
            "providers": int(providers["c"]) if providers else 0,
            "message_count": len(rows),
            "today": {
                "messages": len(today_rows),
                "model_calls": len(today_done),
                "tokens": today_tokens,
                "avg_latency_ms": today_latency,
            },
            "resources": self._resources(),
            "recent": [
                {
                    "character": r["character_name"] or "（已删除）",
                    "channel": r["channel_type"],
                    "channel_label": self._platform_meta(r["channel_type"])[0],
                    "model": r["model"],
                    "latency_ms": int(r["latency_ms"] or 0),
                    "tokens": r["tokens"],
                    "status": r["status"],
                    "created_at": r["created_at"],
                }
                for r in recent
            ],
        }

    # ---------- 区间统计 ----------

    def range(self, key: str = "7d") -> dict[str, Any]:
        if key not in RANGES:
            raise ValueError(f"统计范围只能是 {'、'.join(RANGES)} 之一")
        days = RANGES[key]
        rows = self._rows()
        now = datetime.now().astimezone()

        if days:
            since = now - timedelta(days=days)
            rows = [r for r in rows if r["created"].astimezone() >= since]
            start_day = since.date()
        else:
            start_day = min((r["created"].astimezone().date() for r in rows), default=now.date())

        done = [r for r in rows if r["status"] == "done"]
        errors = [r for r in rows if r["status"] != "done"]
        prompt_tokens = sum(int(r["prompt_tokens"] or 0) for r in done)
        completion_tokens = sum(int(r["completion_tokens"] or 0) for r in done)
        total_tokens = prompt_tokens + completion_tokens
        latency = round(sum(r["latency_ms"] for r in done) / len(done)) if done else 0
        span_minutes = max(1.0, days * 24 * 60 if days else (
            (now - min(r["created"].astimezone() for r in rows)).total_seconds() / 60 + 1 if rows else 1
        ))

        # 按天（本地时区）分桶，补齐没有消息的日期，折线/柱状图才不会跳
        buckets: dict[str, dict[str, int]] = {}
        for r in rows:
            day = r["created"].astimezone().date().isoformat()
            b = buckets.setdefault(day, {"messages": 0, "calls": 0, "tokens": 0})
            b["messages"] += 1
            if r["status"] == "done":
                b["calls"] += 1
                b["tokens"] += r["tokens"]

        series = []
        cursor = start_day
        end_day = now.date()
        while cursor <= end_day:
            key_day = cursor.isoformat()
            b = buckets.get(key_day, {"messages": 0, "calls": 0, "tokens": 0})
            series.append({
                "date": key_day,
                "label": f"{cursor.month}/{cursor.day}",
                **b,
            })
            cursor += timedelta(days=1)

        # 平台排名
        channels: dict[str, int] = {}
        for r in rows:
            channels[r["channel_type"]] = channels.get(r["channel_type"], 0) + 1
        by_channel = []
        for channel_type, count in sorted(channels.items(), key=lambda kv: -kv[1]):
            label, icon = self._platform_meta(channel_type)
            by_channel.append({"key": channel_type, "name": label, "icon": icon, "count": count})

        # 模型用量排名
        models: dict[str, dict[str, int]] = {}
        for r in done:
            m = models.setdefault(r["model"] or "未记录", {"calls": 0, "tokens": 0})
            m["calls"] += 1
            m["tokens"] += r["tokens"]
        by_model = [
            {"name": name, "calls": v["calls"], "tokens": v["tokens"]}
            for name, v in sorted(models.items(), key=lambda kv: -kv[1]["tokens"])
        ]

        # 角色用量
        chars: dict[str, dict[str, Any]] = {}
        for r in rows:
            c = chars.setdefault(
                r["character_id"],
                {"id": r["character_id"], "name": r["character_name"] or "（已删除）", "count": 0, "tokens": 0},
            )
            c["count"] += 1
            c["tokens"] += r["tokens"]
        by_character = sorted(chars.values(), key=lambda c: -c["count"])

        # 会话 Top 10（按 token）
        sessions: dict[str, dict[str, Any]] = {}
        for r in done:
            s = sessions.setdefault(
                r["session_key"],
                {
                    "session_key": r["session_key"],
                    "channel": r["channel_type"],
                    "channel_label": self._platform_meta(r["channel_type"])[0],
                    "character": r["character_name"] or "（已删除）",
                    "calls": 0,
                    "tokens": 0,
                },
            )
            s["calls"] += 1
            s["tokens"] += r["tokens"]
        top_sessions = sorted(sessions.values(), key=lambda s: -s["tokens"])[:10]

        return {
            "range": key,
            "range_label": RANGE_LABELS[key],
            "since": (now - timedelta(days=days)).isoformat(timespec="seconds") if days else None,
            "message_count": len(rows),
            "model": {
                "calls": len(done),
                "errors": len(errors),
                "success_rate": round(len(done) / len(rows) * 100) if rows else 100,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "tokens": total_tokens,
                "avg_latency_ms": latency,
                "tpm": round(total_tokens / span_minutes, 1),
            },
            "by_channel": by_channel,
            "by_model": by_model,
            "by_character": by_character,
            "sessions": top_sessions,
            "series": series,
        }
