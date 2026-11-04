"""把库里现有的角色导出成 seed_data.json（首次建库的默认角色）。

场景：默认角色不再由开发者写死，而是「你在控制台里调好的那几套」。
调完人设 / 记忆后跑一次，种子数据就同步成你的版本。

用法：
    python scripts/export_seed.py --dry-run          # 只看会导出什么，不写文件
    python scripts/export_seed.py                    # 导出全部角色
    python scripts/export_seed.py --names 菲比,学习鱼 # 只导出指定角色（按创建顺序）
    python scripts/export_seed.py --version 3        # 同时把种子版本号改成 3

注意两点：
1. 版本号不变（默认保持原样）时，已经跑过的库不会被改动，只影响**全新库**；
   把 `--version` 调高会让老库按「替换示例角色」的规则重建这几个人（会删掉它们的会话）。
2. `mock_replies`（离线回复库）按角色 key 从旧种子沿用；新角色没有对应 key 就不写，
   没有配模型服务时会显示「未配置离线回复」的提示。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import Database  # noqa: E402

SEED_PATH = ROOT / "app" / "seed_data.json"


def load_old_seed(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build_character(db: Database, row: Any, key: str, mock: dict[str, Any] | None) -> dict[str, Any]:
    versions = []
    for v in db.query(
        "SELECT * FROM persona_version WHERE character_id = ? ORDER BY version_no ASC", (row["id"],)
    ):
        item: dict[str, Any] = {"note": v["note"]}
        if v["system_prompt"]:
            item["system_prompt"] = v["system_prompt"]
        for field in ("personality_tags", "catchphrases", "taboo_topics"):
            values = json.loads(v[field] or "[]")
            if values:
                item[field] = values
        for field in ("backstory", "speech_style", "style_hint"):
            if v[field]:
                item[field] = v[field]
        if v["style_hint"]:
            item["mock_style_hint"] = v["style_hint"]
        versions.append(item)

    memories = []
    for m in db.query(
        "SELECT * FROM memory WHERE character_id = ? ORDER BY created_at ASC", (row["id"],)
    ):
        memories.append(
            {
                "content": m["content"],
                "source": m["source"],
                "importance": int(m["importance"]),
                "pinned": bool(m["pinned"]),
            }
        )

    spec: dict[str, Any] = {
        "key": key,
        "name": row["name"],
        "one_liner": row["one_liner"],
        "versions": versions,
        "memories": memories,
    }
    if mock:
        spec["mock_replies"] = mock
    return spec


def main() -> int:
    parser = argparse.ArgumentParser(description="把库里的角色导出成 seed_data.json")
    parser.add_argument("--db", default=os.environ.get("FISHCONSOLE_DB", "fishconsole.db"))
    parser.add_argument("--out", default=str(SEED_PATH))
    parser.add_argument("--names", default="", help="逗号分隔，只导出这些名字（按名字精确匹配）")
    parser.add_argument("--version", type=int, default=None, help="覆盖种子版本号")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"找不到数据库: {db_path}")
        return 1

    old = load_old_seed(Path(args.out))
    old_by_name = {c.get("name"): c for c in old.get("characters", [])}
    old_mock_by_key = {
        c.get("key"): c.get("mock_replies")
        for c in old.get("characters", [])
        if c.get("mock_replies")
    }

    db = Database(str(db_path))

    # 优先用库里的 seed_state 认回原 key：用户给示例角色改过名时，按名字就认不出来了
    state_row = db.query_one("SELECT value FROM app_setting WHERE key='seed_state'")
    key_by_id: dict[str, str] = {}
    if state_row is not None:
        try:
            state = json.loads(state_row["value"] or "{}")
            for key, cid in (state.get("characters") or {}).items():
                if cid:
                    key_by_id[cid] = key
        except ValueError:
            pass

    rows = db.query("SELECT * FROM character ORDER BY created_at ASC")
    if args.names:
        wanted = [n.strip() for n in args.names.split(",") if n.strip()]
        rows = [r for r in rows if r["name"] in wanted]
        missing = set(wanted) - {r["name"] for r in rows}
        if missing:
            print("库里没有这些角色:", "、".join(sorted(missing)))
    if not rows:
        print("没有可导出的角色")
        return 1

    characters = []
    for index, row in enumerate(rows):
        # key 的优先级：seed_state 里登记的（改过名也认得出）> 旧种子同名 > role_N。
        # 离线回复库按 key 对应，key 保住了，没配 API Key 时的演示才不会退化。
        old_spec = old_by_name.get(row["name"])
        key = key_by_id.get(row["id"]) or (old_spec or {}).get("key") or f"role_{index + 1}"
        mock = old_mock_by_key.get(key)
        spec = build_character(db, row, key, mock)
        characters.append(spec)
        flag = "沿用" if mock else "无"
        print(
            f"  {spec['name']:8} key={key:14} 版本 {len(spec['versions'])}"
            f"  记忆 {len(spec['memories']):2}  离线回复库 {flag}"
        )
    db.close()

    version = args.version if args.version is not None else int(old.get("version") or 2)
    payload = {
        "version": version,
        "replace_legacy_names": old.get("replace_legacy_names", []),
        "characters": characters,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    print(f"\n角色 {len(characters)} 个 · 种子版本 {version} · 输出 {args.out}")
    if args.dry_run:
        print("（dry-run，没有写文件）")
        return 0

    Path(args.out).write_text(text, encoding="utf-8")
    print("已写入。全新库启动时会按这份数据建默认角色。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
