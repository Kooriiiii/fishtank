"""幂等去重 + TTL 清理。

保留 SQLite 持久化（重启后仍有效、可多进程共享），
补上 AstrBot 内存方案自带、而我们此前缺失的过期回收：

- **判重仍靠主键冲突**，不是「先查后写」，没有 TOCTOU 窗口。
- **TTL 清理按间隔触发**，不是每条消息都删一次；清理动作本身加时间闸，
  避免高频对话把写锁耗在 DELETE 上。
- **行数硬上限兜底**：即使 TTL 设为永不过期，也不会无限涨——
  超限时按时间淘汰最旧的，保证磁盘占用有界。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import Database, utcnow
from .settings import SettingsService


class DedupService:
    def __init__(self, db: Database, settings: SettingsService) -> None:
        self.db = db
        self.settings = settings
        self._lock = threading.Lock()
        self._last_sweep = 0.0
        self._swept_total = 0

    # ---------- 判重 ----------

    def check_and_mark(self, idempotency_key: str, channel_type: str) -> bool:
        """返回 True 表示这是重复事件，应当忽略。

        靠 INSERT 的主键冲突判定，避免「先查后写」的并发窗口。
        """
        if not idempotency_key:
            return False
        if not self.settings.dedup().enabled:
            return False

        self.maybe_sweep()
        try:
            with self.db.write() as conn:
                conn.execute(
                    """INSERT INTO processed_event (idempotency_key, channel_type, processed_at)
                       VALUES (?,?,?)""",
                    (idempotency_key, channel_type, utcnow()),
                )
            return False
        except Exception as exc:  # sqlite3.IntegrityError 及其子类
            if "UNIQUE" in str(exc) or "PRIMARY KEY" in str(exc):
                return True
            raise

    # ---------- TTL 清理 ----------

    def maybe_sweep(self) -> int:
        """按配置间隔触发清理。未到间隔直接返回 0，开销仅为一次时间比较。"""
        cfg = self.settings.dedup()
        now = time.monotonic()
        with self._lock:
            if now - self._last_sweep < cfg.sweep_interval_seconds:
                return 0
            self._last_sweep = now
        return self.sweep()

    def sweep(self, force_ttl: int | None = None) -> int:
        """立即清理：先按 TTL 删过期行，再按行数上限淘汰最旧的。返回删除行数。"""
        cfg = self.settings.dedup()
        ttl = cfg.ttl_seconds if force_ttl is None else force_ttl
        removed = 0

        with self.db.write() as conn:
            if ttl > 0:
                # 与 utcnow() 保持同样的毫秒精度，否则秒级截断会让
                # 小于 1 秒粒度的 TTL 因落在同一秒内而失效
                cutoff = (
                    datetime.now(timezone.utc) - timedelta(seconds=ttl)
                ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                cur = conn.execute(
                    "DELETE FROM processed_event WHERE processed_at < ?", (cutoff,)
                )
                removed += cur.rowcount or 0

            # 行数兜底：TTL=0（永不过期）时这是唯一的增长约束
            row = conn.execute("SELECT COUNT(*) AS c FROM processed_event").fetchone()
            overflow = int(row["c"]) - cfg.max_rows
            if overflow > 0:
                cur = conn.execute(
                    """DELETE FROM processed_event
                        WHERE rowid IN (
                            SELECT rowid FROM processed_event
                             ORDER BY processed_at ASC LIMIT ?
                        )""",
                    (overflow,),
                )
                removed += cur.rowcount or 0

        with self._lock:
            self._swept_total += removed
        return removed

    # ---------- 观测 ----------

    def stats(self) -> dict[str, Any]:
        row = self.db.query_one(
            """SELECT COUNT(*) AS total,
                      MIN(processed_at) AS oldest,
                      MAX(processed_at) AS newest
                 FROM processed_event"""
        )
        cfg = self.settings.dedup()
        with self._lock:
            swept = self._swept_total
            last = self._last_sweep
        return {
            "total_rows": int(row["total"]) if row else 0,
            "oldest_at": row["oldest"] if row else None,
            "newest_at": row["newest"] if row else None,
            "max_rows": cfg.max_rows,
            "ttl_seconds": cfg.ttl_seconds,
            "swept_total": swept,
            "seconds_since_last_sweep": round(time.monotonic() - last, 1) if last else None,
        }
