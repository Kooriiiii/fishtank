"""SQLite 持久化层：连接管理、建表、轻量查询助手。

设计要点：
- 单文件 SQLite，零部署；WAL 模式保证读写并发下不互相阻塞。
- 外键强制开启，角色删除级联清理版本/记忆/绑定/对话。
- 所有时间统一存 ISO-8601 UTC 字符串，避免跨时区歧义。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

DEFAULT_DB_PATH = os.environ.get("FISHCONSOLE_DB", "fishconsole.db")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS character (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    avatar      TEXT NOT NULL DEFAULT '🐟',
    one_liner   TEXT NOT NULL DEFAULT '',
    -- 绑定的模型服务；空串表示用全局默认，都没有则走离线 Mock
    model_provider_id TEXT NOT NULL DEFAULT '',
    -- 带几轮历史进上下文（0 = 单轮，不带历史）
    context_rounds INTEGER NOT NULL DEFAULT 8,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- 模型服务（AstrBot 的「服务提供商」）。Key 加密存储，只回脱敏串。
CREATE TABLE IF NOT EXISTS llm_provider (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    provider_type TEXT NOT NULL DEFAULT 'openai_chat_completion',
    base_url      TEXT NOT NULL DEFAULT '',
    api_key_enc   TEXT NOT NULL DEFAULT '',
    api_key_mask  TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    temperature   REAL NOT NULL DEFAULT 0.7,
    max_tokens    INTEGER NOT NULL DEFAULT 1000,
    is_default    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persona_version (
    id                TEXT PRIMARY KEY,
    character_id      TEXT NOT NULL REFERENCES character(id) ON DELETE CASCADE,
    version_no        INTEGER NOT NULL,
    -- 人格自由文本（AstrBot 的「人格」）：非空时整体覆盖结构化模板
    system_prompt     TEXT NOT NULL DEFAULT '',
    personality_tags  TEXT NOT NULL DEFAULT '[]',
    backstory         TEXT NOT NULL DEFAULT '',
    speech_style      TEXT NOT NULL DEFAULT '',
    catchphrases      TEXT NOT NULL DEFAULT '[]',
    taboo_topics      TEXT NOT NULL DEFAULT '[]',
    style_hint        TEXT NOT NULL DEFAULT '',
    note              TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    is_active         INTEGER NOT NULL DEFAULT 0,
    UNIQUE (character_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_version_char ON persona_version(character_id, version_no DESC);

CREATE TABLE IF NOT EXISTS memory (
    id            TEXT PRIMARY KEY,
    character_id  TEXT NOT NULL REFERENCES character(id) ON DELETE CASCADE,
    content       TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'manual',
    importance    INTEGER NOT NULL DEFAULT 3,
    pinned        INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_char ON memory(character_id, pinned DESC, importance DESC);

CREATE TABLE IF NOT EXISTS channel_binding (
    id               TEXT PRIMARY KEY,
    character_id     TEXT NOT NULL REFERENCES character(id) ON DELETE CASCADE,
    channel_type     TEXT NOT NULL,
    credentials_enc  TEXT NOT NULL DEFAULT '',
    credentials_mask TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'disconnected',
    last_error       TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    UNIQUE (character_id, channel_type)
);

CREATE TABLE IF NOT EXISTS processed_event (
    idempotency_key TEXT NOT NULL,
    channel_type    TEXT NOT NULL,
    processed_at    TEXT NOT NULL,
    PRIMARY KEY (idempotency_key, channel_type)
);
-- TTL 清理按 processed_at 范围删除，没有索引会退化为全表扫描
CREATE INDEX IF NOT EXISTS idx_event_time ON processed_event(processed_at);

-- 运行时设置（限流 / 去重 TTL），单行一个命名空间，值为 JSON 文本
CREATE TABLE IF NOT EXISTS app_setting (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation (
    id                 TEXT PRIMARY KEY,
    character_id       TEXT NOT NULL REFERENCES character(id) ON DELETE CASCADE,
    channel_type       TEXT NOT NULL DEFAULT 'mock',
    session_key        TEXT NOT NULL,
    user_message       TEXT NOT NULL,
    assistant_message  TEXT NOT NULL DEFAULT '',
    persona_version_id TEXT,
    hit_memory_ids     TEXT NOT NULL DEFAULT '[]',
    model              TEXT NOT NULL DEFAULT '',
    prompt_tokens      INTEGER NOT NULL DEFAULT 0,
    completion_tokens  INTEGER NOT NULL DEFAULT 0,
    latency_ms         INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'done',
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_char_time ON conversation(character_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conv_session ON conversation(session_key, created_at DESC);
"""

# 老库补列。SQLite 的 ALTER TABLE 不支持 IF NOT EXISTS，靠 PRAGMA 先探测。
# 每一项：(表名, 列名, 建列语句)。只增不改，历史数据不受影响。
COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("persona_version", "system_prompt",
     "ALTER TABLE persona_version ADD COLUMN system_prompt TEXT NOT NULL DEFAULT ''"),
    ("character", "model_provider_id",
     "ALTER TABLE character ADD COLUMN model_provider_id TEXT NOT NULL DEFAULT ''"),
    ("character", "context_rounds",
     "ALTER TABLE character ADD COLUMN context_rounds INTEGER NOT NULL DEFAULT 8"),
    ("conversation", "model",
     "ALTER TABLE conversation ADD COLUMN model TEXT NOT NULL DEFAULT ''"),
)


def utcnow() -> str:
    """返回 ISO-8601 UTC 时间字符串（毫秒精度，带 Z 后缀）。

    必须保留亚秒精度：TTL 清理按字符串比较 processed_at < cutoff，
    若截断到整秒，小于 1 秒粒度的 TTL 会因落在同一秒内而整体失效。
    毫秒精度下 ISO-8601 的字典序仍与时间序一致。
    """
    return (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


class Database:
    """线程安全的 SQLite 封装。

    FastAPI 的同步端点跑在线程池里，连接不能跨线程共享，
    因此用 threading.local 为每个线程维持一条独立连接。
    """

    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        self.path = path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def init_schema(self) -> None:
        with self._write_lock:
            self.conn.executescript(SCHEMA)
            for table, column, ddl in COLUMN_MIGRATIONS:
                existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    self.conn.execute(ddl)
            self.conn.commit()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """写事务：串行化写入，异常自动回滚。"""
        with self._write_lock:
            conn = self.conn
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
