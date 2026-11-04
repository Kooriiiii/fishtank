"""清理「示例角色被重复创建」的残留。

背景：v2 种子第一次运行时创建了三个示例角色，用户把它们改名成
学习鱼/妈妈鱼/网瘾鱼；之后一次重启按名字找不到原角色，又建了一份同名的。
本脚本按「记忆内容」把角色归到种子 key 上，保留有聊天记录的那一份，
删掉没有聊天记录的重复副本，并把 seed_state 指回保留下来的 id。

用法：
    python fix_dup_seed.py <db_path> [--apply]
不加 --apply 只打印计划。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import Database  # noqa: E402

SEED = json.loads((ROOT / "app" / "seed_data.json").read_text(encoding="utf-8"))


def main() -> int:
    db_path = sys.argv[1]
    apply = "--apply" in sys.argv
    db = Database(db_path)

    rows = db.query("SELECT id, name, created_at FROM character ORDER BY created_at")
    chars = []
    for row in rows:
        mems = {
            r["content"]
            for r in db.query("SELECT content FROM memory WHERE character_id = ?", (row["id"],))
        }
        convs = db.query_one(
            "SELECT COUNT(*) AS c FROM conversation WHERE character_id = ?", (row["id"],)
        )["c"]
        vers = db.query_one(
            "SELECT COUNT(*) AS c FROM persona_version WHERE character_id = ?", (row["id"],)
        )["c"]
        chars.append(
            {
                "id": row["id"], "name": row["name"], "created_at": row["created_at"],
                "mems": mems, "convs": int(convs), "vers": int(vers),
            }
        )

    state_row = db.query_one("SELECT value FROM app_setting WHERE key='seed_state'")
    state = json.loads(state_row["value"]) if state_row else {"version": 0, "characters": {}}

    mapping: dict[str, str] = {}
    to_delete: list[dict] = []
    for spec in SEED["characters"]:
        key = spec["key"]
        wanted = {m["content"] for m in spec.get("memories") or []}
        matches = [c for c in chars if c["mems"] == wanted]
        if not matches:
            print(f"[{key}] 没找到匹配的示例角色（可能已被删除），跳过")
            continue
        matches.sort(key=lambda c: (-c["convs"], c["created_at"]))
        keep, extras = matches[0], matches[1:]
        mapping[key] = keep["id"]
        print(
            f"[{key}] 保留 {keep['name']}({keep['id']}) convs={keep['convs']} vers={keep['vers']}"
        )
        for extra in extras:
            print(
                f"        重复 {extra['name']}({extra['id']}) convs={extra['convs']} vers={extra['vers']}"
            )
            if extra["convs"] > 0:
                print("        ↑ 这份有聊天记录，不动它，请人工确认")
                continue
            to_delete.append(extra)

    print("\nseed_state 将更新为:", json.dumps(mapping, ensure_ascii=False))
    print("将删除:", [f"{c['name']}({c['id']})" for c in to_delete] or "无")

    if not apply:
        print("\n干跑结束，加 --apply 才真正写入")
        db.close()
        return 0

    with db.write() as conn:
        for c in to_delete:
            conn.execute("DELETE FROM character WHERE id = ?", (c["id"],))
        conn.execute(
            """INSERT INTO app_setting (key, value, updated_at) VALUES ('seed_state', ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (
                json.dumps({"version": state.get("version") or 2, "characters": mapping}, ensure_ascii=False),
                "2026-11-04T00:00:00.000Z",
            ),
        )
    db.close()
    print("\n已写入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
