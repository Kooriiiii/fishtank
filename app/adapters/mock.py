"""Mock 适配器：内置模拟消息流，演示与测试完全不依赖外部审核。"""

from __future__ import annotations

import asyncio
from typing import Any

from .base import ChannelAdapter, InboundMessage, PlatformStatus, build_session_key
from .registry import AdapterMeta, ConfigField, register_adapter


@register_adapter(AdapterMeta(
    name="mock",
    label="Mock 模拟渠道",
    description="内置模拟消息流，用于本地开发、回归测试与离线演示。",
    icon="🧪",
    doc_hint="无需任何外部凭证，绑定后立即可用。",
    connection_mode="internal",
    supports_proactive=True,
    # Mock 刻意开启占位流程：它的用途就是在本机复现真机时序，
    # 让「先占位、后替换」这条链路在演示与测试中真实跑一遍。
    requires_placeholder=True,
    fields=[
        ConfigField(
            key="label", label="备注名称", type="text",
            default="本地调试", placeholder="便于区分多个 Mock 实例",
            hint="仅用于界面展示，不参与连接。",
        ),
        ConfigField(
            key="auto_reply_delay_ms", label="模拟延迟（毫秒）", type="number",
            default=300, min=0, max=10000,
            hint="模拟真机的网络与推理耗时，便于观察占位响应效果。",
        ),
    ],
    constraints=["仅本机可用，不对外收发真实消息。"],
))
class MockAdapter(ChannelAdapter):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.outbox: list[dict[str, Any]] = []
        self._seq = 0
        self._stop = asyncio.Event()

    async def run(self) -> None:
        self.status = PlatformStatus.CONNECTING
        self.clear_errors()
        self.status = PlatformStatus.RUNNING
        # 常驻等待，直到 terminate 被调用；与真机适配器的生命周期保持一致
        await self._stop.wait()

    async def terminate(self) -> None:
        self._stop.set()
        await super().terminate()

    async def feed(
        self, text: str, session_id: str = "local", idempotency_key: str = "",
        message_type: str = "private",
    ) -> InboundMessage:
        """测试/演示入口：向管线注入一条模拟消息。

        自动生成的幂等键必须带进程维度（client_self_id）：
        仅用 binding_id + 自增序号的话，服务重启后序号从 1 重新计数，
        会与库里已有的键碰撞，消息被误判为重复事件而静默丢弃。
        """
        self._seq += 1
        msg = InboundMessage(
            channel_type=self.channel_type,
            session_key=build_session_key(self.channel_type, message_type, session_id, self.binding_id),
            user_message=text,
            idempotency_key=idempotency_key or f"mock-{self.client_self_id}-{self._seq}",
            sender_id=session_id,
            sender_name="模拟用户",
            message_type=message_type,
        )
        self.log("info", "收到模拟消息", text[:200])
        self.commit_event(msg)
        return msg

    async def send_placeholder(self, session_key: str) -> str:
        self._seq += 1
        handle = f"mock-msg-{self._seq}"
        self.outbox.append({"handle": handle, "session_key": session_key, "text": "…", "finish": False})
        self.stats["placeholders"] += 1
        self.log("debug", "已发出占位消息", "…")
        return handle

    async def send_final(self, session_key: str, handle: str, text: str) -> None:
        delay = int(self.config.get("auto_reply_delay_ms") or 0)
        if delay:
            await asyncio.sleep(min(delay, 10000) / 1000)
        for item in self.outbox:
            if item["handle"] == handle:
                # 保留占位阶段的痕迹，便于在界面上看到「先占位、后替换」两步，
                # 这是真机时序（飞书 3 秒 / 企微流式）的本地复现
                item["placeholder_text"] = item["text"]
                item.update({"text": text, "finish": True})
                self.stats["sent"] += 1
                self.log("info", "已回复模拟用户", text[:200])
                return
        self.outbox.append({"handle": handle, "session_key": session_key, "text": text, "finish": True})
        self.stats["sent"] += 1
        self.log("info", "已回复模拟用户", text[:200])
