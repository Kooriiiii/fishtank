"""渠道连接状态的回归测试。

用户报的 bug：点「连接」后界面一直停在「连接中」，其实适配器已经连上了。
根因是后端把「任务起来了」当成「连接完成」落库，而 QQ 这类适配器要等
网关握手才会 on_ready。这里把三条相关行为固定下来：

1. `/connect` 返回的是**终态**（connected / error），不是 connecting；
2. `/api/channels/runtime` 能查到真实运行态（含掉线后的状态）；
3. 上次停在 connecting 的绑定，重启后也会自动重连。
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import create_app  # noqa: E402


def make_client(tmp_path, autostart: bool = False, db_path: Path | None = None) -> TestClient:
    os.environ["FISHCONSOLE_KEY_FILE"] = str(tmp_path / ".key")
    target = Path(db_path) if db_path else tmp_path / f"chan_{uuid.uuid4().hex}.db"
    app = create_app(db_path=str(target), do_seed=True, autostart=autostart)
    return TestClient(app)


def first_character(client) -> dict:
    return client.get("/api/characters").json()[0]


def test_connect_returns_settled_status(tmp_path):
    """connect 返回时必须已经是 connected，而不是 connecting。"""
    with make_client(tmp_path) as client:
        cid = first_character(client)["id"]
        binding = client.post(f"/api/characters/{cid}/channels", json={
            "channel_type": "mock", "config": {"label": "测试"},
        }).json()
        assert binding["status"] == "disconnected"

        out = client.post(f"/api/channels/{binding['id']}/connect").json()
        assert out["status"] == "connected", out
        assert not out["last_error"]

        # 库里的状态也应是 connected（重启后据此自动重连）
        detail = client.get(f"/api/characters/{cid}").json()
        assert detail["bindings"][0]["status"] == "connected"


def test_runtime_endpoint_reflects_live_state(tmp_path):
    with make_client(tmp_path) as client:
        cid = first_character(client)["id"]
        binding = client.post(f"/api/characters/{cid}/channels", json={
            "channel_type": "mock", "config": {"label": "测试"},
        }).json()

        before = client.get("/api/channels/runtime").json()["channels"]
        assert [c["id"] for c in before] == [binding["id"]]
        assert before[0]["status"] == "disconnected"
        assert before[0]["live"] is False

        client.post(f"/api/channels/{binding['id']}/connect")
        after = client.get("/api/channels/runtime").json()["channels"][0]
        assert after["status"] == "connected"
        assert after["live"] is True
        assert after["stats"] is not None

        client.post(f"/api/channels/{binding['id']}/disconnect")
        off = client.get("/api/channels/runtime").json()["channels"][0]
        assert off["status"] == "disconnected"
        assert off["live"] is False


def test_autostart_reconnects_connecting_bindings(tmp_path):
    """上次连接还没落定就被重启（库里是 connecting）也要自动接回来。"""
    db_path = tmp_path / "restart.db"
    with make_client(tmp_path, db_path=db_path) as client:
        cid = first_character(client)["id"]
        binding = client.post(f"/api/characters/{cid}/channels", json={
            "channel_type": "mock", "config": {"label": "重启测试"},
        }).json()

    # 模拟「连到一半进程被杀」：把状态写成 connecting
    from app.db import Database

    db = Database(str(db_path))
    with db.write() as conn:
        conn.execute(
            "UPDATE channel_binding SET status = 'connecting' WHERE id = ?", (binding["id"],)
        )
    db.close()

    with make_client(tmp_path, autostart=True, db_path=db_path) as client2:
        runtime = client2.get("/api/channels/runtime").json()["channels"]
        target = next(c for c in runtime if c["id"] == binding["id"])
        assert target["status"] == "connected", target
        assert target["live"] is True
