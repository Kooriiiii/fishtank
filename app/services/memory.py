"""记忆服务（F3）：条目化 CRUD + 检索注入 + 命中留痕。

产品主张：记忆必须可见、可编辑、可删除，且「这句话用了哪几条记忆」可追溯。
因此检索逻辑刻意保持简单可解释（关键词重合 + 置顶 + 重要度），
不引入向量库——黑箱相似度与「可解释」的主张相悖，本项目量级也不需要。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from ..db import Database, utcnow

MAX_CONTENT_LEN = 500
VALID_SOURCES = ("manual", "extracted")
STOPWORDS = {
    "的", "了", "吗", "呢", "啊", "吧", "是", "我", "你", "他", "她", "它",
    "在", "和", "就", "都", "也", "还", "有", "没", "不", "very", "the", "a", "an",
}


class MemoryError_(Exception):
    """记忆服务业务异常。"""


def row_to_memory(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "character_id": row["character_id"],
        "content": row["content"],
        "source": row["source"],
        "importance": int(row["importance"]),
        "pinned": bool(row["pinned"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _tokenize(text: str) -> set[str]:
    """中英混排的轻量分词：英文按词，中文按二元字组。"""
    text = (text or "").lower()
    tokens: set[str] = set(re.findall(r"[a-z0-9]{2,}", text))
    han = re.findall(r"[\u4e00-\u9fff]+", text)
    for seg in han:
        tokens.update(seg[i : i + 2] for i in range(len(seg) - 1))
        if len(seg) == 1:
            tokens.add(seg)
    return {t for t in tokens if t not in STOPWORDS}


class MemoryService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------- CRUD ----------

    def list_memories(self, character_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            """SELECT * FROM memory WHERE character_id = ?
               ORDER BY pinned DESC, importance DESC, created_at DESC""",
            (character_id,),
        )
        return [row_to_memory(r) for r in rows]

    def get(self, memory_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM memory WHERE id = ?", (memory_id,))
        if row is None:
            raise MemoryError_(f"记忆条目不存在: {memory_id}")
        return row_to_memory(row)

    @staticmethod
    def _validate(content: str, source: str, importance: int) -> tuple[str, str, int]:
        content = (content or "").strip()
        if not content:
            raise MemoryError_("记忆内容不能为空")
        if len(content) > MAX_CONTENT_LEN:
            raise MemoryError_(f"记忆内容过长，上限 {MAX_CONTENT_LEN} 字")
        source = source if source in VALID_SOURCES else "manual"
        try:
            importance = int(importance)
        except (TypeError, ValueError):
            importance = 3
        importance = max(1, min(5, importance))
        return content, source, importance

    def create(
        self,
        character_id: str,
        content: str,
        source: str = "manual",
        importance: int = 3,
        pinned: bool = False,
    ) -> dict[str, Any]:
        content, source, importance = self._validate(content, source, importance)
        memory_id = f"mem_{uuid.uuid4().hex[:12]}"
        now = utcnow()
        with self.db.write() as conn:
            if conn.execute("SELECT 1 FROM character WHERE id = ?", (character_id,)).fetchone() is None:
                raise MemoryError_(f"角色不存在: {character_id}")
            conn.execute(
                """INSERT INTO memory
                   (id, character_id, content, source, importance, pinned, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (memory_id, character_id, content, source, importance, int(bool(pinned)), now, now),
            )
        return self.get(memory_id)

    def update(self, memory_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.get(memory_id)
        content, source, importance = self._validate(
            payload.get("content", current["content"]),
            payload.get("source", current["source"]),
            payload.get("importance", current["importance"]),
        )
        pinned = payload.get("pinned", current["pinned"])
        with self.db.write() as conn:
            conn.execute(
                """UPDATE memory SET content = ?, source = ?, importance = ?, pinned = ?, updated_at = ?
                   WHERE id = ?""",
                (content, source, importance, int(bool(pinned)), utcnow(), memory_id),
            )
        return self.get(memory_id)

    def delete(self, memory_id: str) -> None:
        with self.db.write() as conn:
            cur = conn.execute("DELETE FROM memory WHERE id = ?", (memory_id,))
            if cur.rowcount == 0:
                raise MemoryError_(f"记忆条目不存在: {memory_id}")

    # ---------- 检索注入 ----------

    def retrieve(self, character_id: str, query: str, limit: int = 6) -> list[dict[str, Any]]:
        """为一次对话挑选要注入的记忆，返回带 score/reason 的条目。

        打分规则（刻意可解释，便于在 UI 上向用户说明）：
          置顶 +100（置顶即「永远记得」）
          关键词每命中一个 +12（上限 48）
          重要度 × 6
        仅返回「置顶」或「有关键词命中」的条目：无关记忆不注入，
        避免模型拿到一堆不相干事实后自由发挥。
        """
        items = self.list_memories(character_id)
        if not items:
            return []
        q_tokens = _tokenize(query)
        scored: list[dict[str, Any]] = []
        for item in items:
            hits = q_tokens & _tokenize(item["content"])
            if not item["pinned"] and not hits:
                continue
            score = item["importance"] * 6 + min(len(hits), 4) * 12
            reasons = []
            if item["pinned"]:
                score += 100
                reasons.append("置顶")
            if hits:
                reasons.append("关键词命中：" + "、".join(sorted(hits)[:3]))
            enriched = dict(item)
            enriched["score"] = score
            enriched["reason"] = " / ".join(reasons)
            scored.append(enriched)

        scored.sort(key=lambda x: (-x["score"], x["created_at"]))
        return scored[: max(0, limit)]
