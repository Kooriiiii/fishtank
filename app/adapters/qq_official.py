"""QQ 官方机器人适配器（qq-botpy WebSocket 长连接）。

连接方式：QQ 开放平台 → 机器人 → AppID / AppSecret，
官方 SDK `qq-botpy` 维持 WebSocket 长连接，**不需要公网 IP**。

真机联调最容易踩的两个坑，这里都处理了：

1. **被动回复必须带 msg_id + msg_seq。** 官方只允许「回复刚收到的那条消息」，
   且同一 msg_id 下 msg_seq 必须递增。AstrBot 的做法是在内存里按会话记住
   最近一条入站消息 id，回包时带上，本实现同样如此（`_last_msg_id` / `_msg_seq`）：
   不带 msg_id 会被判为「主动消息」，而群聊主动消息默认没有权限，直接 400。
2. **群聊内容是带 @ 标记的原文。** `<@!123456>` / `<qqbot-at-user id="..."/>`
   必须先剥掉，否则模型会把这串 ID 当成用户说的话。

平台硬限制（已写进 meta.constraints，界面可见）：
- 不支持主动消息（用户先说话才能回）
- 不支持 At、语音、视频与引用回复
- 个人认证主体最多 5 个机器人
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import traceback
from typing import Any

from .base import ChannelAdapter, InboundMessage, PlatformStatus, build_session_key
from .registry import AdapterMeta, ConfigField, register_adapter

# 入站内容里会混入的 @ 标记，全部剥掉
MENTION_PATTERNS = (
    re.compile(r"<qqbot-at-user\s+id=\"[^\"]*\"\s*/>"),
    re.compile(r"<@!?\d+>"),
)
# 表情标记 <faceType=4,faceId="",ext="...">，统一替换成可读占位
FACE_PATTERN = re.compile(r"<faceType=\d+[^>]*>")


def clean_content(raw: str) -> str:
    """去掉 @ 标记与表情标签，返回干净的正文。"""
    text = raw or ""
    for pattern in MENTION_PATTERNS:
        text = pattern.sub(" ", text)
    text = FACE_PATTERN.sub("[表情]", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


class _BotpyLogBridge(logging.Handler):
    """把 botpy 自己的日志转发进适配器的环形缓冲。

    botpy 遇到「AppID/Secret 不对」时不会抛异常，而是打一行日志然后无限重试。
    不接管这行日志，界面上就只会看到一个永远「连接中」的渠道。
    """

    def __init__(self, adapter: "QQOfficialAdapter") -> None:
        super().__init__()
        self.adapter = adapter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — 日志格式化失败不能影响主流程
            return
        level = "error" if record.levelno >= logging.ERROR else (
            "warn" if record.levelno >= logging.WARNING else "info"
        )
        self.adapter.log(level, f"[botpy] {message}")


@register_adapter(AdapterMeta(
    name="qq_official",
    label="QQ 官方机器人",
    description="QQ 开放平台官方机器人，WebSocket 长连接，无需公网 IP。",
    icon="🐧",
    doc_hint="手机 QQ 扫码登录 q.qq.com 注册机器人，拿到 AppID 与 AppSecret；群聊需在「功能配置」里开通。",
    doc_url="https://bot.q.qq.com/wiki/",
    connection_mode="websocket",
    supports_proactive=False,
    requires_placeholder=False,
    fields=[
        ConfigField("app_id", "AppID", "text", required=True,
                    placeholder="如 102000001",
                    hint="QQ 开放平台 → 应用详情 → 机器人 AppID。"),
        ConfigField("app_secret", "AppSecret", "secret", required=True,
                    placeholder="仅服务端使用，保存后不可再读取",
                    hint="加密存储，界面只展示前后各 4 位。"),
        ConfigField("enable_group", "接收群聊消息", "bool", default=True,
                    hint="需在开放平台开通群聊权限并通过审核，否则只能私聊。"),
        ConfigField("use_markdown", "使用 Markdown 回复", "bool", default=False,
                    hint="需在平台申请 Markdown 能力；未申请时保持关闭，用纯文本。"),
        ConfigField("sandbox", "沙箱环境", "bool", default=False,
                    hint="开启后连接沙箱网关，可用未上线的机器人先行联调。"),
    ],
    constraints=[
        "官方通道不支持主动消息，必须用户先发消息。",
        "不支持 At、语音、视频与引用回复。",
        "群聊需先在开放平台开通群聊权限并通过审核。",
    ],
))
class QQOfficialAdapter(ChannelAdapter):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.client: Any = None
        self._stop = asyncio.Event()
        self._bridge = _BotpyLogBridge(self)
        # botpy 的 ConnectionSession 用 ensure_future 起连接任务、再 asyncio.wait 等它，
        # 取消等待方并不会取消这些任务，socket 会继续收消息（僵尸连接）。
        # 所以自己记住连接任务，停机时显式取消。
        self._conn_tasks: set[asyncio.Task] = set()
        # 被动回复所需：按会话记住最近一条入站消息 id，以及回复序号
        self._last_msg_id: dict[str, str] = {}
        self._msg_seq: dict[str, int] = {}

    async def run(self) -> None:
        self.status = PlatformStatus.CONNECTING
        self.clear_errors()
        self.log("info", "正在连接 QQ 开放平台…")
        try:
            import botpy
        except ImportError:
            self.record_error("缺少依赖 botpy。请执行 pip install qq-botpy 后重新连接。")
            return

        botpy_logger = logging.getLogger("botpy")
        botpy_logger.addHandler(self._bridge)
        botpy_logger.setLevel(logging.INFO)

        adapter = self

        class _Client(botpy.Client):
            async def on_ready(self) -> None:
                adapter.status = PlatformStatus.RUNNING
                adapter.log("info", "QQ 机器人已上线，等待消息（私聊直接发，群聊需 @ 机器人）")

            async def on_group_at_message_create(self, message: Any) -> None:
                adapter._on_message(message, "group")

            async def on_c2c_message_create(self, message: Any) -> None:
                adapter._on_message(message, "private")

            async def on_direct_message_create(self, message: Any) -> None:
                adapter._on_message(message, "private")

            async def bot_connect(self, session: Any) -> None:
                """每一条 websocket 连接都跑在这个任务里，记下来才能在停机时掐掉。"""
                task = asyncio.current_task()
                if task is not None:
                    adapter._conn_tasks.add(task)
                try:
                    await super().bot_connect(session)
                finally:
                    adapter._conn_tasks.discard(task)

        intents = botpy.Intents(
            public_messages=bool(self.config.get("enable_group", True)),
            public_guild_messages=bool(self.config.get("enable_group", True)),
            direct_message=True,
        )
        watchdog = asyncio.create_task(self._watchdog())
        try:
            self.client = _Client(intents=intents, is_sandbox=bool(self.config.get("sandbox")), bot_log=False)
            await self.client.start(
                appid=str(self.config.get("app_id", "")),
                secret=str(self.config.get("app_secret", "")),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 连接失败必须如实记录
            self.record_error(f"QQ 连接失败：{exc}", traceback.format_exc())
        finally:
            watchdog.cancel()
            try:
                await watchdog
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            botpy_logger.removeHandler(self._bridge)

    async def _watchdog(self) -> None:
        """20 秒还没上线就把排查方向写进日志。

        botpy 对凭证错误不抛异常、只是无限重试，没有这行提示，用户看到的
        永远是一个转圈的「连接中」。
        """
        await asyncio.sleep(20)
        if self._stop.is_set() or self.status == PlatformStatus.RUNNING:
            return
        self.log(
            "warn",
            "20 秒内未上线：请核对 AppID / AppSecret、机器人是否已在开放平台发布、群聊权限是否开通。",
        )

    def _on_message(self, message: Any, message_type: str) -> None:
        """把 botpy 消息对象归一化为 InboundMessage。"""
        if message_type == "group" and not self.config.get("enable_group", True):
            return
        author = getattr(message, "author", None)
        sender_id = str(
            getattr(author, "member_openid", None)
            or getattr(author, "user_openid", None)
            or getattr(author, "id", "")
        )
        group_id = str(getattr(message, "group_openid", "") or "")
        session_id = group_id or sender_id
        if not session_id:
            self.log("warn", "收到无法确定会话的消息，已忽略", str(getattr(message, "id", "")))
            return
        session_key = build_session_key(self.channel_type, message_type, session_id, self.binding_id)
        self._last_msg_id[session_key] = str(getattr(message, "id", "") or "")
        content = clean_content(getattr(message, "content", "") or "")
        self.log("info", f"收到{'群' if message_type == 'group' else '私聊'}消息", content[:200])
        self.commit_event(InboundMessage(
            channel_type=self.channel_type,
            session_key=session_key,
            user_message=content,
            # QQ 消息 id 作幂等键
            idempotency_key=str(getattr(message, "id", "") or ""),
            sender_id=sender_id,
            sender_name=str(getattr(author, "username", "") or ""),
            message_type=message_type,
            group_id=group_id,
        ))

    async def send_placeholder(self, session_key: str) -> str:
        """QQ 官方通道按「被动回复」语义工作，用 msg_id 关联即可，
        无需先发占位消息。这里只返回句柄占位。"""
        self.stats["placeholders"] += 1
        return f"qq:{session_key}"

    def _next_seq(self, session_key: str) -> int:
        """同一会话内的回复序号，递增但没有必要精确连续。

        官方要求 msg_seq 不能重复，AstrBot 用随机数；这里用递增计数，
        既避免重复，也让同一条入站消息的多次分片回复保持有序。
        """
        seq = self._msg_seq.get(session_key, random.randint(1, 100))
        seq = seq + 1 if seq < 100000 else 1
        self._msg_seq[session_key] = seq
        return seq

    async def send_final(self, session_key: str, handle: str, text: str) -> None:
        if self.client is None:
            raise RuntimeError("QQ 适配器尚未连接")
        _, message_type, session_id = session_key.split(":", 2)
        api = self.client.api
        msg_id = self._last_msg_id.get(session_key, "")
        payload: dict[str, Any] = {"msg_seq": self._next_seq(session_key)}
        if msg_id:
            payload["msg_id"] = msg_id
        if self.config.get("use_markdown"):
            payload["msg_type"] = 2
            payload["markdown"] = {"content": text}
        else:
            payload["msg_type"] = 0
            payload["content"] = text

        try:
            if message_type == "group":
                await api.post_group_message(group_openid=session_id, **payload)
            else:
                await api.post_c2c_message(openid=session_id, **payload)
            self.stats["sent"] += 1
            self.log("info", f"已回复{'群' if message_type == 'group' else '私聊'}", text[:200])
        except Exception as exc:  # noqa: BLE001
            # C2C 与群聊对 msg_id 的要求不同：带 msg_id 被拒时退回主动发送再试一次
            if "msg_id" in str(exc) and "msg_id" in payload:
                payload.pop("msg_id", None)
                try:
                    if message_type == "group":
                        await api.post_group_message(group_openid=session_id, **payload)
                    else:
                        await api.post_c2c_message(openid=session_id, **payload)
                    self.stats["sent"] += 1
                    self.log("warn", "带 msg_id 回复被拒，已改用主动发送", text[:120])
                    return
                except Exception as retry_exc:  # noqa: BLE001
                    self.stats["send_failed"] += 1
                    self.record_error(f"QQ 发送失败：{retry_exc}")
                    return
            self.stats["send_failed"] += 1
            self.record_error(f"QQ 发送失败：{exc}")

    async def terminate(self) -> None:
        self._stop.set()
        # 先掐掉 websocket 连接任务，否则 botpy 会一直重连：
        # socket 活着 = 这条已停止的渠道还继续收消息，然后把幂等键吃掉，
        # 真正在线的连接就再也收不到这条消息了。
        pending = [t for t in self._conn_tasks if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._conn_tasks.clear()

        if self.client is not None:
            try:
                await self.client.close()
            except Exception:  # noqa: BLE001 — 关闭失败不应阻塞停机
                pass
        self.log("info", "QQ 适配器已停止")
        await super().terminate()
