"""渠道连接的「僵尸连接」防线。

真实事故：QQ 不回消息。复盘发现 botpy 断线重连的残留 socket 还在收包，
它把消息的幂等键吃掉了，但没有适配器可以回包；真正在线的连接反而被判重复。
这里把三道防线固定下来：

1. 适配器已经不在 → 消息直接丢弃，不交给管线（否则幂等键被吃掉）；
2. 同一个 AppID 只保留一条连接；
3. 删角色时先停掉它的连接。
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.adapters.base import InboundMessage  # noqa: E402
from app.adapters.manager import AdapterManager  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.catalog import ChannelService  # noqa: E402


def test_dispatch_drops_messages_from_stale_binding():
    """已停止渠道的消息不能进管线：会吃掉幂等键，还会白烧一次模型调用。"""
    manager = AdapterManager()
    called: list[InboundMessage] = []
    manager.handler = lambda msg: called.append(msg) or {"reply": "不该被调用"}

    msg = InboundMessage(
        channel_type="qq_official",
        session_key="cb_gone:private:u1",
        user_message="在吗",
        idempotency_key="evt-1",
        binding_id="cb_gone",
    )
    asyncio.run(manager._dispatch(msg))

    assert called == [], "适配器不在时不能调管线"
    assert manager.dropped == 1
    assert manager.processed == 0
    assert manager.stats()["dropped"] == 1


def test_dispatch_processes_when_adapter_exists():
    """对照：适配器在的时候照常处理并回包。"""
    manager = AdapterManager()
    sent: list[tuple] = []

    class FakeAdapter:
        channel_type = "mock"
        character_id = "ch_1"
        meta = SimpleNamespace(requires_placeholder=False)
        config: dict = {}
        stats: dict = {}

        async def send_final(self, session_key, handle, text):
            sent.append((session_key, text))

        def log(self, *args, **kwargs):
            pass

        def record_error(self, *args, **kwargs):
            pass

    manager.adapters["cb_1"] = FakeAdapter()  # type: ignore[assignment]
    manager.handler = lambda msg: {"reply": "收到"}

    msg = InboundMessage(
        channel_type="mock", session_key="cb_1:private:u1",
        user_message="你好", idempotency_key="evt-2", binding_id="cb_1",
    )
    asyncio.run(manager._dispatch(msg))

    assert sent == [("cb_1:private:u1", "收到")]
    assert manager.processed == 1
    assert manager.dropped == 0


def test_same_appid_only_one_connection(tmp_path):
    """同一个 QQ AppID 起第二条连接时，先把第一条停掉。"""
    db_file = tmp_path / f"guard_{uuid.uuid4().hex}.db"
    os.environ["FISHCONSOLE_KEY_FILE"] = str(tmp_path / ".key")
    app = create_app(db_path=str(db_file), do_seed=True, autostart=False)

    manager = app.state.ctx.manager
    service = ChannelService(app.state.ctx.db, manager)

    old = SimpleNamespace(
        channel_type="qq_official", character_id="ch_old",
        config={"app_id": "102000001"}, stats={},
    )
    other_bot = SimpleNamespace(
        channel_type="qq_official", character_id="ch_x",
        config={"app_id": "102000999"}, stats={},
    )
    manager.adapters["cb_old"] = old  # type: ignore[assignment]
    manager.adapters["cb_other"] = other_bot  # type: ignore[assignment]

    stopped = asyncio.run(
        service._stop_same_identity("qq_official", "cb_new", {"app_id": "102000001"})
    )
    assert stopped == ["cb_old"], "只应停掉同一个 AppID 的旧连接"
    assert "cb_other" in manager.adapters, "别的机器人不能被误伤"
    assert "cb_old" not in manager.adapters


def test_qq_terminate_cancels_websocket_tasks():
    """停机时必须显式取消 botpy 的连接任务。

    botpy 的 ConnectionSession 用 ensure_future 起连接、再 asyncio.wait 等它，
    取消等待方不会取消连接任务，socket 会活着继续收消息（僵尸连接）。
    """
    from app.adapters.qq_official import QQOfficialAdapter

    adapter = QQOfficialAdapter(
        binding_id="cb_qq", character_id="ch_1", config={"app_id": "1"}, event_queue=asyncio.Queue()
    )

    async def scenario():
        async def never_ends():
            await asyncio.sleep(3600)

        task = asyncio.create_task(never_ends())
        adapter._conn_tasks.add(task)
        await adapter.terminate()
        await asyncio.sleep(0.05)
        return task

    task = asyncio.run(scenario())
    assert task.cancelled() or task.done(), "连接任务必须被取消，否则就是僵尸连接"


def test_delete_character_stops_its_adapters(tmp_path):
    """删角色必须先停连接，否则适配器会变成收得到发不出的僵尸。"""
    db_file = tmp_path / f"guard_{uuid.uuid4().hex}.db"
    os.environ["FISHCONSOLE_KEY_FILE"] = str(tmp_path / ".key")
    from fastapi.testclient import TestClient

    app = create_app(db_path=str(db_file), do_seed=True, autostart=False)
    with TestClient(app) as client:
        cid = client.get("/api/characters").json()[0]["id"]
        binding = client.post(f"/api/characters/{cid}/channels", json={
            "channel_type": "mock", "config": {"label": "删除测试"},
        }).json()
        assert client.post(f"/api/channels/{binding['id']}/connect").json()["status"] == "connected"
        manager = client.app.state.ctx.manager
        assert manager.get(binding["id"]) is not None

        assert client.delete(f"/api/characters/{cid}").status_code == 204
        assert manager.get(binding["id"]) is None, "删角色时必须停掉适配器"
