"""人设版本化服务（F2）：快照、差异对比、回滚。

核心不变量：
1. 版本不可变 —— 任何修改都产生新版本，历史永不改写。
2. 同一角色任意时刻有且仅有一个 is_active=1 的版本。
3. 回滚 = 以目标版本内容创建一个新版本并激活，而非删除中间版本。
   这样「回滚」本身也在历史里留痕，可以再回滚回去。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from ..db import Database, utcnow

# 参与版本快照与差异对比的字段，顺序即 UI 展示顺序
PERSONA_FIELDS = (
    "system_prompt",
    "personality_tags",
    "backstory",
    "speech_style",
    "catchphrases",
    "taboo_topics",
)
LIST_FIELDS = {"personality_tags", "catchphrases", "taboo_topics"}

FIELD_LABELS = {
    "system_prompt": "人格设定（自由文本）",
    "personality_tags": "性格标签",
    "backstory": "背景故事",
    "speech_style": "说话风格",
    "catchphrases": "口头禅",
    "taboo_topics": "禁忌话题",
}


class PersonaError(Exception):
    """人设服务的业务异常，由 API 层翻译成 4xx。"""


def _loads(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in value] if isinstance(value, list) else []


def row_to_version(row: Any) -> dict[str, Any]:
    """数据库行 -> API 字典，JSON 字段自动反序列化。"""
    return {
        "id": row["id"],
        "character_id": row["character_id"],
        "version_no": row["version_no"],
        "system_prompt": row["system_prompt"],
        "personality_tags": _loads(row["personality_tags"]),
        "backstory": row["backstory"],
        "speech_style": row["speech_style"],
        "catchphrases": _loads(row["catchphrases"]),
        "taboo_topics": _loads(row["taboo_topics"]),
        "style_hint": row["style_hint"],
        "note": row["note"],
        "created_at": row["created_at"],
        "is_active": bool(row["is_active"]),
    }


class PersonaService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------- 查询 ----------

    def list_versions(self, character_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM persona_version WHERE character_id = ? ORDER BY version_no DESC",
            (character_id,),
        )
        return [row_to_version(r) for r in rows]

    def get_version(self, version_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM persona_version WHERE id = ?", (version_id,))
        if row is None:
            raise PersonaError(f"人设版本不存在: {version_id}")
        return row_to_version(row)

    def get_active(self, character_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM persona_version WHERE character_id = ? AND is_active = 1",
            (character_id,),
        )
        return row_to_version(row) if row else None

    # ---------- 写入 ----------

    def create_version(
        self,
        character_id: str,
        payload: dict[str, Any],
        note: str = "",
        inherit: bool = True,
    ) -> dict[str, Any]:
        """创建新版本并激活。

        inherit=True 时，payload 中缺失的字段从当前激活版本继承，
        避免前端只提交部分字段就把其余人设清空。
        """
        base = self.get_active(character_id) if inherit else None
        values: dict[str, Any] = {}
        for field in PERSONA_FIELDS:
            if field in payload and payload[field] is not None:
                raw = payload[field]
                if field in LIST_FIELDS:
                    items = [str(x).strip() for x in (raw if isinstance(raw, list) else [raw])]
                    values[field] = [x for x in items if x]
                else:
                    values[field] = str(raw).strip()
            elif base is not None:
                values[field] = base[field]
            else:
                values[field] = [] if field in LIST_FIELDS else ""

        style_hint = str(payload.get("style_hint") or (base or {}).get("style_hint") or "").strip()
        version_id = f"pv_{uuid.uuid4().hex[:12]}"
        now = utcnow()

        with self.db.write() as conn:
            # 角色必须存在，否则外键会报错，这里给出更友好的提示
            if conn.execute("SELECT 1 FROM character WHERE id = ?", (character_id,)).fetchone() is None:
                raise PersonaError(f"角色不存在: {character_id}")
            row = conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) AS m FROM persona_version WHERE character_id = ?",
                (character_id,),
            ).fetchone()
            next_no = int(row["m"]) + 1
            conn.execute(
                "UPDATE persona_version SET is_active = 0 WHERE character_id = ?", (character_id,)
            )
            conn.execute(
                """INSERT INTO persona_version
                   (id, character_id, version_no, system_prompt, personality_tags, backstory,
                    speech_style, catchphrases, taboo_topics, style_hint, note, created_at, is_active)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (
                    version_id,
                    character_id,
                    next_no,
                    values["system_prompt"],
                    json.dumps(values["personality_tags"], ensure_ascii=False),
                    values["backstory"],
                    values["speech_style"],
                    json.dumps(values["catchphrases"], ensure_ascii=False),
                    json.dumps(values["taboo_topics"], ensure_ascii=False),
                    style_hint,
                    str(note or "").strip(),
                    now,
                ),
            )
            conn.execute(
                "UPDATE character SET updated_at = ? WHERE id = ?", (now, character_id)
            )
        return self.get_version(version_id)

    def rollback(self, version_id: str, note: str = "") -> dict[str, Any]:
        """回滚：复制目标版本内容生成新版本并激活。"""
        target = self.get_version(version_id)
        if target["is_active"]:
            raise PersonaError("该版本已是当前版本，无需回滚")
        payload = {f: target[f] for f in PERSONA_FIELDS}
        payload["style_hint"] = target["style_hint"]
        auto_note = note or f"回滚到 v{target['version_no']}"
        return self.create_version(target["character_id"], payload, note=auto_note, inherit=False)

    # ---------- 差异对比 ----------

    def diff(self, left_id: str, right_id: str) -> dict[str, Any]:
        """字段级差异。状态语义以「从 left 变到 right」为准：
        - added    : left 为空，right 有值
        - removed  : left 有值，right 为空
        - modified : 两侧都有值但不同
        - same     : 完全一致
        """
        left = self.get_version(left_id)
        right = self.get_version(right_id)
        if left["character_id"] != right["character_id"]:
            raise PersonaError("只能对比同一角色的人设版本")

        changes = []
        for field in PERSONA_FIELDS:
            lv, rv = left[field], right[field]
            l_empty = not lv
            r_empty = not rv
            if lv == rv:
                status = "same"
            elif l_empty and not r_empty:
                status = "added"
            elif r_empty and not l_empty:
                status = "removed"
            else:
                status = "modified"
            changes.append(
                {"field": field, "label": FIELD_LABELS[field], "left": lv, "right": rv, "status": status}
            )

        return {
            "left": left,
            "right": right,
            "changes": changes,
            "changed_count": sum(1 for c in changes if c["status"] != "same"),
        }
