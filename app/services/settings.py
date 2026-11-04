"""运行时设置（限流 / 去重 TTL）：数据库持久化 + 内存缓存。

设计要点：
- 设置项落 SQLite，重启后仍生效（AstrBot 的限流配置在内存，重启清零）。
- 读多写少，内存缓存一份，写时失效；避免每条消息都打一次库。
- 只接受白名单内的键，避免前端写入任意配置污染存储。
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Any

from ..db import Database, utcnow

# 限流策略：
#   discard —— 直接拒绝并返回 429（默认，适合同步 HTTP 接口）
#   stall   —— 等待到下一个窗口再放行（适合后台异步消费场景）
STRATEGIES = ("discard", "stall")

# 单次 stall 的最长等待，防止配置失误导致请求长时间挂住
MAX_STALL_SECONDS = 30.0


@dataclass
class RateLimitSettings:
    enabled: bool = True
    count: int = 30           # 窗口内允许的请求数
    window_seconds: int = 60  # 窗口长度；默认对齐企微 30 条/分钟
    strategy: str = "discard"

    def validate(self) -> None:
        if self.count < 1 or self.count > 10000:
            raise SettingsError("限流次数需在 1–10000 之间")
        if self.window_seconds < 1 or self.window_seconds > 86400:
            raise SettingsError("限流窗口需在 1–86400 秒之间")
        if self.strategy not in STRATEGIES:
            raise SettingsError(f"限流策略只能是 {' 或 '.join(STRATEGIES)}")


@dataclass
class DedupSettings:
    enabled: bool = True
    ttl_seconds: int = 3600       # 幂等键保留时长；0 表示永不过期
    sweep_interval_seconds: int = 300  # 最小清理间隔，避免每条消息都删一次
    max_rows: int = 100000        # 行数硬上限，超出按时间淘汰最旧的

    def validate(self) -> None:
        if self.ttl_seconds < 0 or self.ttl_seconds > 30 * 86400:
            raise SettingsError("去重 TTL 需在 0–2592000 秒之间（0 表示永不过期）")
        if self.sweep_interval_seconds < 10 or self.sweep_interval_seconds > 86400:
            raise SettingsError("清理间隔需在 10–86400 秒之间")
        if self.max_rows < 100 or self.max_rows > 10_000_000:
            raise SettingsError("去重表行数上限需在 100–10000000 之间")


class SettingsError(Exception):
    """设置项校验失败，由 API 层翻译成 400。"""


DEFAULTS: dict[str, Any] = {
    "rate_limit": asdict(RateLimitSettings()),
    "dedup": asdict(DedupSettings()),
}


class SettingsService:
    """设置读写。值以 JSON 文本存 app_setting 表，单行一个命名空间。"""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = {}

    # ---------- 读 ----------

    def _load(self, key: str) -> dict[str, Any]:
        import json

        with self._lock:
            if key in self._cache:
                return dict(self._cache[key])
        row = self.db.query_one("SELECT value FROM app_setting WHERE key = ?", (key,))
        merged = dict(DEFAULTS[key])
        if row is not None:
            try:
                stored = json.loads(row["value"])
                if isinstance(stored, dict):
                    # 与默认值合并：新增字段在升级后自动补齐，不需要迁移
                    merged.update({k: v for k, v in stored.items() if k in merged})
            except ValueError:
                pass
        with self._lock:
            self._cache[key] = dict(merged)
        return merged

    def rate_limit(self) -> RateLimitSettings:
        return RateLimitSettings(**self._load("rate_limit"))

    def dedup(self) -> DedupSettings:
        return DedupSettings(**self._load("dedup"))

    def all(self) -> dict[str, Any]:
        return {"rate_limit": asdict(self.rate_limit()), "dedup": asdict(self.dedup())}

    # ---------- 写 ----------

    def update(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        import json

        if key not in DEFAULTS:
            raise SettingsError(f"未知设置项: {key}")
        current = self._load(key)
        # 只接受已知字段，忽略多余键
        merged = {k: (payload[k] if k in payload and payload[k] is not None else v)
                  for k, v in current.items()}

        obj = RateLimitSettings(**merged) if key == "rate_limit" else DedupSettings(**merged)
        obj.validate()
        merged = asdict(obj)

        with self.db.write() as conn:
            conn.execute(
                """INSERT INTO app_setting (key, value, updated_at) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                  updated_at = excluded.updated_at""",
                (key, json.dumps(merged, ensure_ascii=False), utcnow()),
            )
        with self._lock:
            self._cache[key] = dict(merged)
        return merged

    def reset(self, key: str) -> dict[str, Any]:
        if key not in DEFAULTS:
            raise SettingsError(f"未知设置项: {key}")
        with self.db.write() as conn:
            conn.execute("DELETE FROM app_setting WHERE key = ?", (key,))
        with self._lock:
            self._cache.pop(key, None)
        return dict(DEFAULTS[key])
