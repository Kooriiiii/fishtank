"""企业微信智能机器人长连接协议的端到端验证。

不连真机，而是起一个本地 WebSocket「假企业微信」，按官方长连接的帧格式
收发，检验适配器是否真的做对了三件事：

1. 先 `aibot_subscribe` 鉴权，并等应答；
2. 收到 `aibot_msg_callback` 后，回复必须复用回调帧里的 `req_id`；
3. 先发 `finish=false` 占位，再发 `finish=true` 全量替换，且两次的
   `stream.id` 必须一致（不一致平台会当成两条消息，占位就不会被替换）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.adapters.base import build_session_key  # noqa: E402
from app.adapters.wecom import WecomAdapter  # noqa: E402


def test_wecom_long_connection_flow():
    websockets = pytest.importorskip("websockets")

    received: list[dict] = []

    async def scenario() -> None:
        done = asyncio.Event()

        async def handler(ws) -> None:
            async for raw in ws:
                frame = json.loads(raw)
                received.append(frame)
                req_id = (frame.get("headers") or {}).get("req_id", "")
                cmd = frame.get("cmd")
                if cmd == "aibot_subscribe":
                    # 鉴权成功
                    assert frame["body"] == {"bot_id": "bot-1", "secret": "sec-1"}
                    await ws.send(json.dumps({"errcode": 0, "headers": {"req_id": req_id}}))
                    # 订阅成功后推一条用户消息
                    await ws.send(json.dumps({
                        "cmd": "aibot_msg_callback",
                        "headers": {"req_id": "req-1"},
                        "body": {
                            "msgtype": "text",
                            "text": {"content": "今天好累"},
                            "chattype": "single",
                            "from": {"userid": "u1"},
                            "msgid": "m1",
                            "aibotid": "bot-1",
                        },
                    }))
                elif cmd in ("ping", "aibot_respond_msg"):
                    await ws.send(json.dumps({"errcode": 0, "headers": {"req_id": req_id}}))
                    if cmd == "aibot_respond_msg" and len(
                        [f for f in received if f.get("cmd") == "aibot_respond_msg"]
                    ) >= 2:
                        done.set()

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        queue: asyncio.Queue = asyncio.Queue()
        adapter = WecomAdapter(
            binding_id="cb_wecom_test",
            character_id="ch_1",
            config={
                "bot_id": "bot-1",
                "bot_secret": "sec-1",
                "endpoint": f"ws://127.0.0.1:{port}",
                "placeholder_text": "让我想想…",
            },
            event_queue=queue,
        )
        task = asyncio.create_task(adapter.run())
        try:
            inbound = await asyncio.wait_for(queue.get(), timeout=10)
            assert inbound.user_message == "今天好累"
            assert inbound.message_type == "private"
            assert inbound.sender_id == "u1"
            assert inbound.idempotency_key == "m1"

            session_key = build_session_key("wecom", "private", "u1", adapter.binding_id)
            assert inbound.session_key == session_key

            handle = await adapter.send_placeholder(session_key)
            await adapter.send_final(session_key, handle, "累就躺会儿，我陪你。")

            await asyncio.wait_for(done.wait(), timeout=10)
        finally:
            await adapter.terminate()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())

    responds = [f for f in received if f.get("cmd") == "aibot_respond_msg"]
    assert len(responds) == 2, f"应发出占位 + 最终两条回复，实际 {len(responds)}"
    assert all(f["headers"]["req_id"] == "req-1" for f in responds), "回复必须复用回调的 req_id"

    placeholder, final = responds
    assert placeholder["body"]["stream"]["finish"] is False
    assert placeholder["body"]["stream"]["content"] == "让我想想…"
    assert final["body"]["stream"]["finish"] is True
    assert final["body"]["stream"]["content"] == "累就躺会儿，我陪你。"
    assert placeholder["body"]["stream"]["id"] == final["body"]["stream"]["id"], (
        "两次回复必须用同一个 stream.id，否则占位不会被替换"
    )


def test_wecom_wrong_secret_sets_error():
    """订阅被拒（凭证错）必须进 error 并带上 errcode，不能假装连上。"""
    websockets = pytest.importorskip("websockets")

    async def scenario() -> tuple[str, str, list[dict]]:
        async def handler(ws) -> None:
            async for raw in ws:
                frame = json.loads(raw)
                if frame.get("cmd") == "aibot_subscribe":
                    await ws.send(json.dumps({
                        "errcode": 40001,
                        "errmsg": "invalid secret",
                        "headers": {"req_id": frame["headers"]["req_id"]},
                    }))
                    return

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        adapter = WecomAdapter(
            binding_id="cb_wecom_bad",
            character_id="ch_1",
            config={
                "bot_id": "bot-x",
                "bot_secret": "wrong",
                "endpoint": f"ws://127.0.0.1:{port}",
                "send_rate_per_minute": 30,
            },
            event_queue=asyncio.Queue(),
        )
        task = asyncio.create_task(adapter.run())
        try:
            for _ in range(100):
                if adapter.status.value == "error":
                    break
                await asyncio.sleep(0.05)
            # 停机会把状态改成 stopped，所以在停机前把状态与日志取出来
            error = adapter.last_error.message if adapter.last_error else ""
            return adapter.status.value, error, adapter.logs(20)
        finally:
            await adapter.terminate()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            server.close()
            await server.wait_closed()

    status, error, logs = asyncio.run(scenario())
    assert status == "error"
    assert "40001" in error
    # 错误也要落到日志里，界面才看得到
    assert any("40001" in item["message"] for item in logs)


def test_wecom_message_flows_through_pipeline(tmp_path):
    """真机消息 → 适配器 → 队列 → 管线 → 回包，整条链路一次跑通。

    这条用例守的是「后台能跑通的，接上真机也能跑通」：与真实渠道共用
    AdapterManager + MessagePipeline，而不是各写一套。
    """
    websockets = pytest.importorskip("websockets")

    async def scenario() -> list[dict]:
        finished = asyncio.Event()
        streams: list[dict] = []

        async def handler(ws) -> None:
            async for raw in ws:
                frame = json.loads(raw)
                cmd = frame.get("cmd")
                req_id = (frame.get("headers") or {}).get("req_id", "")
                if cmd == "aibot_subscribe":
                    await ws.send(json.dumps({"errcode": 0, "headers": {"req_id": req_id}}))
                    await ws.send(json.dumps({
                        "cmd": "aibot_msg_callback",
                        "headers": {"req_id": "req-e2e"},
                        "body": {
                            "msgtype": "text",
                            "text": {"content": "今天好累，方案又被打回了"},
                            "chattype": "single",
                            "from": {"userid": "u9"},
                            "msgid": "m-e2e",
                            "aibotid": "bot-e2e",
                        },
                    }))
                elif cmd in ("ping", "aibot_respond_msg"):
                    await ws.send(json.dumps({"errcode": 0, "headers": {"req_id": req_id}}))
                    if cmd == "aibot_respond_msg":
                        streams.append(frame["body"]["stream"])
                        if frame["body"]["stream"]["finish"]:
                            finished.set()

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        from app.main import AppContext

        ctx = AppContext(str(tmp_path / "e2e.db"), do_seed=True)
        character_id = ctx.characters.list_characters()[0]["id"]
        adapter = ctx.manager.build("wecom", "cb_e2e", character_id, {
            "bot_id": "bot-e2e",
            "bot_secret": "sec-e2e",
            "endpoint": f"ws://127.0.0.1:{port}",
            "placeholder_text": "让我想想…",
        })
        await ctx.manager.start(adapter)
        try:
            await asyncio.wait_for(finished.wait(), timeout=20)
            # 给 send_final 的收尾一点时间
            await asyncio.sleep(0.1)
        finally:
            await ctx.manager.stop_all()
            ctx.db.close()
            server.close()
            await server.wait_closed()
        return streams

    streams = asyncio.run(scenario())
    assert streams, "管线没有回包"
    assert streams[0]["finish"] is False, "必须先发占位"
    final = [s for s in streams if s["finish"]]
    assert final, "必须有最终回复"
    assert final[-1]["content"].strip(), "最终回复不能是空的"
    assert final[-1]["id"] == streams[0]["id"], "占位与最终回复必须是同一个 stream"
