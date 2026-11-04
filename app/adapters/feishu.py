"""飞书自建应用适配器（lark-oapi WebSocket 长连接）。

连接方式与 AstrBot 一致（`sources/lark/lark_adapter.py` 的模式）：
用 `lark_oapi.ws.Client` + EventDispatcherHandler 注册
`im.message.receive_v1`，内部维持长连接，无需公网 IP。

两个平台硬约束直接决定实现方式：
1. **回调必须 3 秒内返回。** 因此事件回调里只做「归一化 + 投队列」，
   立刻返回；模型推理在管线侧异步进行。AstrBot 的做法同样是
   `asyncio.create_task(...)` 后立即 return。
2. **事件会重复推送**（失败按 15 秒/5 分钟/1 小时/6 小时 重试最多 4 次，
   成功也可能因链路重发而重复）。因此用 `event_id` 作幂等键，
   由 DedupService 统一判重。
"""

from __future__ import annotations

import asyncio
import json
import traceback
from typing import Any

from .base import ChannelAdapter, InboundMessage, PlatformStatus, build_session_key
from .registry import AdapterMeta, ConfigField, register_adapter


@register_adapter(AdapterMeta(
    name="feishu",
    label="飞书自建应用",
    description="飞书企业自建应用，WebSocket 长连接，订阅 im.message.receive_v1。",
    icon="🦅",
    doc_hint="必须创建「企业自建应用」，长连接不支持商店应用；改完配置需创建版本并发布才生效。",
    doc_url="https://open.feishu.cn/document/",
    connection_mode="websocket",
    supports_proactive=True,
    requires_placeholder=True,
    fields=[
        ConfigField("app_id", "App ID", "text", required=True,
                    placeholder="cli_xxxxxxxxxxxx",
                    hint="开发者后台 → 凭证与基础信息。"),
        ConfigField("app_secret", "App Secret", "secret", required=True,
                    placeholder="仅服务端使用，保存后不可再读取",
                    hint="加密存储，界面只展示前后各 4 位。"),
        ConfigField("domain", "服务域", "select", default="feishu",
                    options=[{"value": "feishu", "label": "飞书（国内）"},
                             {"value": "lark", "label": "Lark（国际）"}],
                    hint="国内租户选飞书，海外租户选 Lark。"),
        ConfigField("placeholder_text", "占位文案", "text", default="让我想想…",
                    hint="3 秒内先回这句，模型算完再替换为正文。"),
    ],
    constraints=[
        "长连接收到消息须 3 秒内处理完成，故必须先回占位再异步更新。",
        "事件会重复推送，必须按 event_id 幂等去重。",
        "每个应用最多 50 个长连接，且推送为集群模式不广播，不要多实例部署。",
        "修改应用配置后需创建版本并发布，否则不生效。",
    ],
))
class FeishuAdapter(ChannelAdapter):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.client: Any = None
        self.api: Any = None
        self._placeholders: dict[str, str] = {}

    async def run(self) -> None:
        self.status = PlatformStatus.CONNECTING
        self.clear_errors()
        try:
            import lark_oapi as lark
        except ImportError:
            self.record_error("缺少依赖 lark-oapi。请执行 pip install lark-oapi 后重新连接。")
            return

        domain = lark.LARK_DOMAIN if self.config.get("domain") == "lark" else lark.FEISHU_DOMAIN
        app_id = str(self.config.get("app_id", ""))
        app_secret = str(self.config.get("app_secret", ""))

        def on_message(event: Any) -> None:
            # 必须立即返回：飞书要求 3 秒内完成回调处理
            self._on_message(event)

        try:
            handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(on_message)
                .build()
            )
            self.api = (
                lark.Client.builder()
                .app_id(app_id).app_secret(app_secret)
                .domain(domain).log_level(lark.LogLevel.ERROR).build()
            )
            self.client = lark.ws.Client(
                app_id=app_id, app_secret=app_secret,
                domain=domain, log_level=lark.LogLevel.ERROR,
                event_handler=handler,
            )
            self.status = PlatformStatus.RUNNING
            await self.client._connect()
        except Exception as exc:  # noqa: BLE001
            self.record_error(f"飞书连接失败: {exc}", traceback.format_exc())

    def _on_message(self, event: Any) -> None:
        """归一化飞书事件。只做转换与投队列，保证 3 秒内返回。"""
        try:
            msg = event.event.message
            sender = event.event.sender
            text = ""
            if getattr(msg, "message_type", "") == "text":
                try:
                    text = json.loads(msg.content or "{}").get("text", "")
                except ValueError:
                    text = ""
            chat_type = getattr(msg, "chat_type", "p2p")
            message_type = "group" if chat_type == "group" else "private"
            session_id = str(getattr(msg, "chat_id", "") or "")
            sender_id = str(getattr(getattr(sender, "sender_id", None), "open_id", "") or "")
            self.commit_event(InboundMessage(
                channel_type=self.channel_type,
                session_key=build_session_key(self.channel_type, message_type, session_id, self.binding_id),
                user_message=(text or "").strip(),
                # v2.0 事件用 event_id 作幂等键（v1.0 用 uuid）
                idempotency_key=str(getattr(event.header, "event_id", "") or ""),
                sender_id=sender_id,
                message_type=message_type,
                group_id=session_id if message_type == "group" else "",
                raw={"message_id": getattr(msg, "message_id", "")},
            ))
        except Exception as exc:  # noqa: BLE001 — 单条消息解析失败不应断开长连接
            self.record_error(f"飞书消息解析失败: {exc}")

    async def send_placeholder(self, session_key: str) -> str:
        """先发占位消息，拿到 message_id 供后续更新。"""
        if self.api is None:
            raise RuntimeError("飞书适配器尚未连接")
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        _, _, chat_id = session_key.split(":", 2)
        text = str(self.config.get("placeholder_text") or "让我想想…")
        req = (
            CreateMessageRequest.builder().receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id).msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False)).build()
            ).build()
        )
        resp = await asyncio.to_thread(self.api.im.v1.message.create, req)
        if not resp.success():
            raise RuntimeError(f"飞书占位消息发送失败: {resp.msg}")
        handle = resp.data.message_id
        self._placeholders[handle] = chat_id
        self.stats["placeholders"] += 1
        return handle

    async def send_final(self, session_key: str, handle: str, text: str) -> None:
        """用最终结果替换占位消息（PATCH 同一 message_id）。"""
        if self.api is None:
            raise RuntimeError("飞书适配器尚未连接")
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        req = (
            PatchMessageRequest.builder().message_id(handle)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps({"text": text}, ensure_ascii=False)).build()
            ).build()
        )
        resp = await asyncio.to_thread(self.api.im.v1.message.patch, req)
        if not resp.success():
            raise RuntimeError(f"飞书消息更新失败: {resp.msg}")
        self._placeholders.pop(handle, None)
        self.stats["sent"] += 1

    async def terminate(self) -> None:
        if self.client is not None:
            try:
                await self.client._disconnect()
            except Exception:  # noqa: BLE001
                pass
        await super().terminate()
