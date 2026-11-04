"""种子数据加载：从 seed_data.json 预置示例角色、人设版本链与记忆。

幂等：已存在同名角色则跳过，重复启动不会产生重复数据。
每个角色按 versions 顺序逐个创建版本，形成真实的 v1→v2→v3 历史，
这样「版本对比 / 回滚」在首次启动后就有内容可演示。

种子升级：seed_data.json 里带 `version`。版本号变了就先把**上一次由种子创建
的角色**删掉再重新铺一遍，避免「改了示例角色之后，老角色还占着列表」。
删除范围只限种子里登记过的角色（先按记录的 id，其次按 `replace_legacy_names`
里的老名字），用户自己建的角色不受影响。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .db import utcnow
from .services.catalog import CharacterService
from .services.llm import MockProvider
from .services.memory import MemoryService
from .services.persona import PersonaService

SEED_PATH = Path(__file__).with_name("seed_data.json")
SEED_STATE_KEY = "seed_state"


def load_seed_file(path: Path = SEED_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"characters": []}
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(data.get("characters"), list):
        raise ValueError(f"种子文件结构非法: {path}")
    return data


def _load_state(characters: CharacterService) -> dict[str, Any] | None:
    row = characters.db.query_one("SELECT value FROM app_setting WHERE key = ?", (SEED_STATE_KEY,))
    if row is None:
        return None
    try:
        state = json.loads(row["value"])
    except (TypeError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _save_state(characters: CharacterService, version: int, mapping: dict[str, str]) -> None:
    with characters.db.write() as conn:
        conn.execute(
            """INSERT INTO app_setting (key, value, updated_at) VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (SEED_STATE_KEY, json.dumps({"version": version, "characters": mapping}, ensure_ascii=False), utcnow()),
        )


def _drop_stale_seed(characters: CharacterService, stale_ids: set[str], legacy_names: list[str]) -> None:
    """删掉上一版种子角色（级联清掉其人设/记忆/绑定/对话）。"""
    if not stale_ids and not legacy_names:
        return
    with characters.db.write() as conn:
        for character_id in stale_ids:
            conn.execute("DELETE FROM character WHERE id = ?", (character_id,))
        if legacy_names:
            placeholders = ",".join("?" * len(legacy_names))
            conn.execute(f"DELETE FROM character WHERE name IN ({placeholders})", tuple(legacy_names))


def seed_database(
    characters: CharacterService,
    persona: PersonaService,
    memory: MemoryService,
    mock_provider: MockProvider,
    path: Path = SEED_PATH,
) -> dict[str, str]:
    """返回 seed key -> character_id 的映射。

    幂等的判定标准是 **id 而不是名字**：示例角色被用户改了名之后，
    再按名字去找就会找不到、重新建一份，列表里于是出现重复的两套角色。
    所以这里按 `seed_state` 里登记的 id 复用它；只有从没登记过的新 key
    才会按名字收养或新建。用户主动删掉的示例角色也不再复活。
    """
    data = load_seed_file(path)
    version = int(data.get("version") or 1)
    state = _load_state(characters)

    if state is not None and int(state.get("version") or 0) == version:
        known: dict[str, str] = state.get("characters") or {}
    else:
        # 种子版本变了（或第一次带版本号启动）：清掉上一版由种子创建的角色
        stale_ids: set[str] = set()
        if state is not None:
            stale_ids = set((state.get("characters") or {}).values())
        legacy_names = list(data.get("replace_legacy_names") or []) if state is None else []
        _drop_stale_seed(characters, stale_ids, legacy_names)
        known = {}

    existing = characters.list_characters()
    by_id = {c["id"]: c for c in existing}
    by_name = {c["name"]: c["id"] for c in existing}
    mapping: dict[str, str] = {}

    for spec in data["characters"]:
        name = spec.get("name", "").strip()
        if not name:
            continue
        key = spec.get("key") or name

        if key in known:
            character_id = known[key]
            if character_id not in by_id:
                # 用户自己删掉了这个示例角色，别给他复活
                mapping[key] = ""
                continue
            mock_provider.register(character_id, spec.get("mock_replies") or {})
            mapping[key] = character_id
            continue

        if name in by_name:
            # 版本化之前的种子、或用户手工建过同名角色：收养，不重复创建
            character_id = by_name[name]
            mock_provider.register(character_id, spec.get("mock_replies") or {})
            mapping[key] = character_id
            continue

        versions: list[dict[str, Any]] = spec.get("versions") or []
        first = dict(versions[0]) if versions else {}
        character_id = characters.create(
            {
                "name": name,
                "one_liner": spec.get("one_liner", ""),
                "system_prompt": first.get("system_prompt", ""),
                "personality_tags": first.get("personality_tags", []),
                "backstory": first.get("backstory", ""),
                "speech_style": first.get("speech_style", ""),
                "catchphrases": first.get("catchphrases", []),
                "taboo_topics": first.get("taboo_topics", []),
                "style_hint": first.get("mock_style_hint", ""),
                "note": first.get("note", "初始人设"),
            }
        )["id"]
        # 其余版本依次叠加，构造真实的版本演进历史
        for ver in versions[1:]:
            persona.create_version(
                character_id,
                {
                    "system_prompt": ver.get("system_prompt"),
                    "personality_tags": ver.get("personality_tags"),
                    "backstory": ver.get("backstory"),
                    "speech_style": ver.get("speech_style"),
                    "catchphrases": ver.get("catchphrases"),
                    "taboo_topics": ver.get("taboo_topics"),
                    "style_hint": ver.get("mock_style_hint", ""),
                },
                note=ver.get("note", ""),
            )
        for mem in spec.get("memories") or []:
            memory.create(
                character_id,
                content=mem.get("content", ""),
                source=mem.get("source", "manual"),
                importance=int(mem.get("importance", 3)),
                pinned=bool(mem.get("pinned", False)),
            )

        mock_provider.register(character_id, spec.get("mock_replies") or {})
        mapping[key] = character_id

    _save_state(characters, version, mapping)
    return mapping
