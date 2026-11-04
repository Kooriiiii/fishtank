"""QQ 官方机器人的被动回复协议验证。

QQ 开放平台的硬性要求：回复必须带上「刚收到那条消息」的 msg_id，并给一个
递增的 msg_seq，否则会被判成主动消息（群聊默认没有该权限）而直接拒绝。
这里用一个假的 client 截获实际发出的参数，把这条协议固定下来。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.adapters.qq_official import QQOfficialAdapter, clean_content  # noqa: E402


class FakeAPI:
    def __init__(self) -> None:
        self.group_calls: list[dict] = []
        self.c2c_calls: list[dict] = []
        self.fail_first = False

    async def post_group_message(self, **kwargs):
        self.group_calls.append(kwargs)
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("msg_id 不可用")
        return {"id": "sent-1"}

    async def post_c2c_message(self, **kwargs):
        self.c2c_calls.append(kwargs)
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("msg_id 不可用")
        return {"id": "sent-2"}


class FakeClient:
    def __init__(self) -> None:
        self.api = FakeAPI()


def make_adapter() -> tuple[QQOfficialAdapter, FakeClient]:
    adapter = QQOfficialAdapter(
        binding_id="cb_qq",
        character_id="ch_1",
        config={"app_id": "102000001", "app_secret": "x", "enable_group": True},
        event_queue=asyncio.Queue(),
    )
    client = FakeClient()
    adapter.client = client
    return adapter, client


def test_group_reply_carries_msg_id_and_increasing_seq():
    adapter, client = make_adapter()
    session_key = "cb_qq:group:G1"

    async def scenario():
        adapter._last_msg_id[session_key] = "msg-abc"
        await adapter.send_final(session_key, session_key, "第一句")
        await adapter.send_final(session_key, session_key, "第二句")

    asyncio.run(scenario())
    calls = client.api.group_calls
    assert len(calls) == 2
    assert calls[0]["group_openid"] == "G1"
    assert calls[0]["msg_id"] == "msg-abc", "被动回复必须带 msg_id"
    assert calls[0]["content"] == "第一句"
    assert calls[0]["msg_type"] == 0
    assert calls[1]["msg_seq"] > calls[0]["msg_seq"], "msg_seq 必须递增"
    assert adapter.stats["sent"] == 2


def test_c2c_reply_uses_openid():
    adapter, client = make_adapter()
    session_key = "cb_qq:private:U1"

    asyncio.run(adapter.send_final(session_key, session_key, "私聊回复"))
    call = client.api.c2c_calls[0]
    assert call["openid"] == "U1"
    assert call["content"] == "私聊回复"
    assert "msg_seq" in call
    assert adapter.stats["sent"] == 1


def test_reply_retries_without_msg_id_when_rejected():
    """私聊里 msg_id 有时不可用：被拒后应退回主动发送，而不是把消息丢掉。"""
    adapter, client = make_adapter()
    client.api.fail_first = True
    session_key = "cb_qq:private:U2"
    adapter._last_msg_id[session_key] = "msg-z"

    asyncio.run(adapter.send_final(session_key, session_key, "兜底回复"))
    assert len(client.api.c2c_calls) == 2
    assert client.api.c2c_calls[0]["msg_id"] == "msg-z"
    assert "msg_id" not in client.api.c2c_calls[1]
    assert adapter.stats["sent"] == 1
    assert any("主动发送" in item["message"] for item in adapter.logs(10))


def test_send_without_connection_is_recorded():
    adapter, _ = make_adapter()
    adapter.client = None
    try:
        asyncio.run(adapter.send_final("cb_qq:private:U3", "x", "hi"))
    except RuntimeError:
        pass
    assert adapter.status.value != "running"


def test_clean_content_strips_mentions_and_faces():
    raw = "<@!123456> 你好 <qqbot-at-user id=\"999\" /> 今天 <faceType=4,faceId=\"\",ext=\"\">"
    cleaned = clean_content(raw)
    assert "123456" not in cleaned and "qqbot-at-user" not in cleaned
    assert "你好" in cleaned and "今天" in cleaned
    assert "[表情]" in cleaned
    assert "  " not in cleaned
