"""种子数据的回归测试：默认角色、幂等、以及「换角色」时的升级清理。

默认角色不再写死在测试里，而是**从 seed_data.json 读**——毕竟这份种子现在
是「用户在控制台里调好的那几套」，随时会被 scripts/export_seed.py 覆盖。

守三件事：
1. 全新库建出种子文件里声明的角色（数量、版本数、记忆数都对得上）；
2. 重复启动不能重复插入；
3. 改示例角色（种子版本号变化）时，旧示例角色被替换，**用户自己建的角色不能受影响**。
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import Database  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.catalog import CharacterService  # noqa: E402
from app.services.persona import PersonaService  # noqa: E402

SEED = json.loads((ROOT / "app" / "seed_data.json").read_text(encoding="utf-8"))
SPECS = SEED["characters"]
SEED_NAMES = [c["name"] for c in SPECS]
VERSIONS = {c["name"]: len(c.get("versions") or []) for c in SPECS}
MEMORIES = {c["name"]: len(c.get("memories") or []) for c in SPECS}
HAS_MOCK = {c["name"]: bool(c.get("mock_replies")) for c in SPECS}


@pytest.fixture()
def db_file(tmp_path):
    os.environ["FISHCONSOLE_KEY_FILE"] = str(tmp_path / ".key")
    return tmp_path / f"seed_{uuid.uuid4().hex}.db"


def test_seed_creates_declared_roles(db_file):
    app = create_app(db_path=str(db_file), do_seed=True)
    with TestClient(app) as client:
        chars = client.get("/api/characters").json()
        assert [c["name"] for c in chars] == SEED_NAMES

        assert {c["name"]: c["memory_count"] for c in chars} == MEMORIES

        for c in chars:
            detail = client.get(f"/api/characters/{c['id']}").json()
            assert len(detail["versions"]) == VERSIONS[c["name"]]
            assert detail["active_version"]["version_no"] == VERSIONS[c["name"]]
            reply = client.post("/api/chat", json={
                "character_id": c["id"], "message": "今天好累，不想动",
            }).json()["reply"]
            assert reply, "对话必须有回复"
            if HAS_MOCK[c["name"]]:
                assert "还没有配置离线回复" not in reply


def test_seed_is_idempotent_across_restart(db_file):
    with TestClient(create_app(db_path=str(db_file), do_seed=True)):
        pass
    with TestClient(create_app(db_path=str(db_file), do_seed=True)) as client:
        assert [c["name"] for c in client.get("/api/characters").json()] == SEED_NAMES


def test_seed_upgrade_replaces_legacy_roles_but_keeps_user_bots(tmp_path, db_file):
    """模拟 v1 → v2 的种子升级：老示例角色被清掉，用户自己的角色保留。"""
    db = Database(str(db_file))
    persona = PersonaService(db)
    characters = CharacterService(db, persona)
    for name in ("鲸落", "咸鱼酱", "砚"):
        characters.create({"name": name, "one_liner": "旧示例角色"})
    mine = characters.create({"name": "我的鱼", "one_liner": "用户自己建的"})
    db.close()

    with TestClient(create_app(db_path=str(db_file), do_seed=True)) as client:
        names = [c["name"] for c in client.get("/api/characters").json()]
        assert [n for n in names if n in SEED_NAMES] == SEED_NAMES
        assert "我的鱼" in names, "用户自建角色不能被种子升级删掉"
        assert not ({"鲸落", "咸鱼酱", "砚"} & set(names)), "旧示例角色应被替换掉"
        assert client.get(f"/api/characters/{mine['id']}").status_code == 200


def test_seed_version_change_drops_previous_seed_roles(db_file):
    """种子版本号变化时，按登记的 id 清掉上一版示例角色。"""
    with TestClient(create_app(db_path=str(db_file), do_seed=True)):
        pass

    db = Database(str(db_file))
    old_state = db.query_one("SELECT value FROM app_setting WHERE key='seed_state'")
    state = json.loads(old_state["value"])
    stale_id = next(iter(state["characters"].values()))
    with db.write() as conn:
        conn.execute(
            "UPDATE app_setting SET value = ? WHERE key='seed_state'",
            (json.dumps({"version": state["version"] - 1, "characters": state["characters"]}),),
        )
    db.close()

    with TestClient(create_app(db_path=str(db_file), do_seed=True)) as client:
        names = [c["name"] for c in client.get("/api/characters").json()]
        assert names == SEED_NAMES, "被替换的角色应重建，而不是留下两份"
        assert client.get(f"/api/characters/{stale_id}").status_code in (200, 400)


def test_renamed_seed_role_is_not_duplicated(db_file):
    """用户给示例角色改名后重启，不能再冒出一个同名的第二份。

    幂等判定必须按 id：按名字找会找不到（名字被改了），于是重复创建。
    """
    with TestClient(create_app(db_path=str(db_file), do_seed=True)) as client:
        cid = client.get("/api/characters").json()[0]["id"]
        client.put(f"/api/characters/{cid}", json={"name": "改名测试鱼"})

    with TestClient(create_app(db_path=str(db_file), do_seed=True)) as client:
        names = [c["name"] for c in client.get("/api/characters").json()]
        assert names == ["改名测试鱼"] + SEED_NAMES[1:]
        assert len(names) == len(SEED_NAMES), "改名后的示例角色不能被重复创建"


def test_deleted_seed_role_is_not_resurrected(db_file):
    """用户主动删掉的示例角色，重启后不该自己长回来。"""
    with TestClient(create_app(db_path=str(db_file), do_seed=True)) as client:
        chars = client.get("/api/characters").json()
        victim = chars[0]["id"]
        assert client.delete(f"/api/characters/{victim}").status_code == 204

    with TestClient(create_app(db_path=str(db_file), do_seed=True)) as client:
        names = [c["name"] for c in client.get("/api/characters").json()]
        assert names == SEED_NAMES[1:], names
