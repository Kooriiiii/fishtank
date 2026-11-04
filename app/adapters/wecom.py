"""企业微信智能机器人适配器（WebSocket 长连接 + 流式占位）。

连接方式：企业微信管理后台 → 应用管理 → 智能机器人 → API 模式 → 使用长连接，
拿到 **BotID + Secret**，连接地址 `wss://openws.work.weixin.qq.com`，**无需域名/IP**。

协议按官方长连接规范实现（与 AstrBot 的 wecom_ai_bot 一致）：

    发 → {"cmd":"aibot_subscribe","headers":{"req_id":...},"body":{"bot_id","secret"}}
    收 → {"cmd":"aibot_msg_callback","headers":{"req_id":...},"body":{...用户消息...}}
    回 → {"cmd":"aibot_respond_msg","headers":{"req_id":<回调里的 req_id>},
          "body":{"msgtype":"stream","stream":{"id":<stream_id>,"finish":false,"content":"…"}}}
    再 → 同一 stream_id 发 finish=true，全量替换占位内容

三个必须照做的细节：
1. **回复必须带回调帧里的 req_id**。不复用这个 req_id，平台认不出这是对哪条消息的应答。
2. **先占位再补全**。大模型推理普遍超过平台的等待时间，先发 finish=false 的
   流式分片，算完再发 finish=true 全量替换，用户不会看到「已读不回」。
3. **errcode 6000 是命令冲突**，按指数退避重试；其它 errcode 如实记录不重试。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from typing import Any

from .base import ChannelAdapter, InboundMessage, PlatformStatus, build_session_key
from .registry import AdapterMeta, ConfigField, register_adapter

WS_ENDPOINT = "wss://openws.work.weixin.qq.com"
CMD_SUBSCRIBE = "aibot_subscribe"
CMD_RESPOND = "aibot_respond_msg"
CMD_RESPOND_WELCOME = "aibot_respond_welcome_msg"
CMD_PING = "ping"


@register_adapter(AdapterMeta(
    name="wecom",
    label="企业微信智能机器人",
    description="企业微信智能机器人 API 模式，WebSocket 长连接，无需域名或公网 IP。",
    icon="💼",
    doc_hint="管理后台 → 智能机器人 → API 模式 → 使用长连接，获取 BotID 与 Secret。",
    doc_url="https://developer.work.weixin.qq.com/document/",
    connection_mode="websocket",
    supports_proactive=False,
    requires_placeholder=True,
    fields=[
        ConfigField("bot_id", "BotID", "text", required=True,
                    placeholder="智能机器人详情页获取",
                    hint="API 模式下的机器人唯一标识。"),
        ConfigField("bot_secret", "Secret", "secret", required=True,
                    placeholder="仅服务端使用，保存后不可再读取",
                    hint="加密存储，界面只展示前后各 4 位。"),
        ConfigField("endpoint", "连接地址", "text", default=WS_ENDPOINT,
                    hint="默认官方长连接地址，一般无需修改。"),
        ConfigField("send_rate_per_minute", "发送节流（条/分钟）", "number",
                    default=30, min=1, max=30,
                    hint="平台硬限 30 条/分钟，超出会被限流，不建议调高。"),
        ConfigField("placeholder_text", "占位文案", "text", default="让我想想…",
                    hint="模型思考期间先回这个，算完自动替换成正式回复。"),
        ConfigField("welcome_text", "进会话欢迎语", "text", default="",
                    hint="留空则不发送。仅对单聊「进入会话」事件生效。"),
    ],
    constraints=[
        "需用户先给机器人发过消息，之后才能主动推送，无法冷启动打招呼。",
        "发送频率限制 30 条/分钟、1000 条/小时（含回复与主动推送）。",
        "长连接提供流式消息：先发 finish=false 占位，算完发 finish=true 全量替换。",
    ],
))
class WecomAdapter(ChannelAdapter):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ws: Any = None
        self._stop = asyncio.Event()
        # 每个会话最近一次回调的 req_id 与 stream_id：回包必须复用它们
        self._pending: dict[str, dict[str, str]] = {}
        self._waiters: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self._send_log: deque[float] = deque()

    # ---------- 连接 ----------

    async def run(self) -> None:
        self.status = PlatformStatus.CONNECTING
        self.clear_errors()
        try:
            import websockets
        except ImportError:
            self.record_error("缺少依赖 websockets。请执行 pip install websockets 后重新连接。")
            return

        url = str(self.config.get("endpoint") or WS_ENDPOINT)
        backoff = 1
        while not self._stop.is_set():
            try:
                retry = await self._run_once(websockets, url)
                backoff = 1
                if not retry:
                    # 凭证错这类问题是可判定的，重连一万次也不会好，直接停下等用户改
                    break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 一次断线不应终止整个适配器
                self.log("warn", f"长连接中断：{exc}")
            if self._stop.is_set():
                break
            self.status = PlatformStatus.CONNECTING
            self.log("info", f"{backoff} 秒后重连…")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _run_once(self, websockets: Any, url: str) -> bool:
        """跑一次完整的长连接生命周期。返回值表示「值得重连」。"""
        async with websockets.connect(url, ping_interval=20, open_timeout=15) as ws:
            self.ws = ws
            # 先起收帧循环，再发订阅：订阅的应答也是从一个帧里回来的，
            # 没人读帧就会一直等到超时（这一步是联调时最容易写错的地方）。
            receiver = asyncio.create_task(self._receive_loop(ws))
            self.log("info", f"已连接 {url}，正在订阅…")
            try:
                if not await self._subscribe():
                    return False
                self.status = PlatformStatus.RUNNING
                self.log("info", "订阅成功，等待企业微信消息")
                heartbeat = asyncio.create_task(self._heartbeat_loop())
                try:
                    await receiver
                finally:
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except asyncio.CancelledError:
                        pass
                return True
            finally:
                if not receiver.done():
                    receiver.cancel()
                    try:
                        await receiver
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                self.ws = None

    async def _receive_loop(self, ws: Any) -> None:
        async for raw in ws:
            await self._on_frame(raw)
            if self._stop.is_set():
                return

    async def _subscribe(self) -> bool:
        """订阅（鉴权）。失败即凭证问题，直接进入 error 状态。"""
        req_id = uuid.uuid4().hex
        reply = await self._send_and_wait(
            CMD_SUBSCRIBE,
            req_id,
            {"bot_id": str(self.config.get("bot_id", "")),
             "secret": str(self.config.get("bot_secret", ""))},
            timeout=15.0,
        )
        if reply is None:
            self.record_error("订阅超时：没有收到企业微信的应答，请检查网络或连接地址。")
            return False
        if reply.get("errcode") not in (0, None):
            self.record_error(
                f"订阅失败 errcode={reply.get('errcode')} errmsg={reply.get('errmsg')}，"
                "请核对 BotID 与 Secret 是否属于同一个智能机器人。"
            )
            return False
        return True

    async def _heartbeat_loop(self) -> None:
        interval = 30
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            if self._stop.is_set() or self.ws is None:
                return
            try:
                await self._send_and_wait(CMD_PING, uuid.uuid4().hex, None, timeout=10.0)
                self.log("debug", "心跳正常")
            except Exception as exc:  # noqa: BLE001
                self.log("warn", f"心跳失败：{exc}")
                return

    # ---------- 收发帧 ----------

    async def _send_json(self, payload: dict[str, Any]) -> None:
        ws = self.ws
        if ws is None:
            raise RuntimeError("企业微信适配器尚未连接")
        async with self._send_lock:
            await ws.send(json.dumps(payload, ensure_ascii=False))

    async def _send_and_wait(
        self,
        cmd: str,
        req_id: str,
        body: dict[str, Any] | None,
        timeout: float = 10.0,
    ) -> dict[str, Any] | None:
        """发命令并等待同 req_id 的应答；errcode 6000（命令冲突）自动退避重试。"""
        loop = asyncio.get_running_loop()
        payload: dict[str, Any] = {"cmd": cmd, "headers": {"req_id": req_id}}
        if body is not None:
            payload["body"] = body

        for attempt in range(4):
            waiter: asyncio.Future = loop.create_future()
            self._waiters[req_id] = waiter
            try:
                await self._send_json(payload)
                reply = await asyncio.wait_for(waiter, timeout=timeout)
            except TimeoutError:
                self.log("warn", f"{cmd} 等待应答超时（req_id={req_id[:8]}…）")
                return None
            finally:
                self._waiters.pop(req_id, None)

            errcode = reply.get("errcode")
            if errcode in (0, None):
                return reply
            if errcode == 6000 and attempt < 3:
                delay = min(0.2 * (2 ** attempt), 2.0)
                self.log("warn", f"{cmd} 命令冲突（errcode=6000），{delay:.1f}s 后重试")
                await asyncio.sleep(delay)
                continue
            self.log("warn", f"{cmd} 失败 errcode={errcode} errmsg={reply.get('errmsg')}")
            return reply
        return None

    async def _on_frame(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.log("warn", "收到非 JSON 帧，已忽略", str(raw)[:200])
            return

        req_id = (payload.get("headers") or {}).get("req_id")
        if isinstance(req_id, str):
            waiter = self._waiters.get(req_id)
            if waiter is not None and not waiter.done():
                waiter.set_result(payload)
                return

        cmd = payload.get("cmd")
        if cmd == "aibot_msg_callback":
            await self._process_callback(payload)
        elif cmd == "aibot_event_callback":
            await self._process_event(payload)
        elif payload.get("errcode") not in (None, 0):
            self.log("warn", f"服务端返回错误 errcode={payload.get('errcode')} errmsg={payload.get('errmsg')}")

    @staticmethod
    def _extract_text(body: dict[str, Any]) -> str:
        """从回调 body 里取正文。text / mixed 两种消息类型都要支持。"""
        msgtype = body.get("msgtype")
        if msgtype == "text":
            return str((body.get("text") or {}).get("content") or "").strip()
        if msgtype == "mixed":
            parts = []
            for item in (body.get("mixed") or {}).get("msg_item") or []:
                if item.get("msgtype") == "text":
                    parts.append(str((item.get("text") or {}).get("content") or ""))
            return " ".join(parts).strip()
        if msgtype == "voice":
            # 语音消息平台会附带识别文本
            return str((body.get("voice") or {}).get("content") or "").strip()
        return ""

    async def _process_callback(self, payload: dict[str, Any]) -> None:
        body = payload.get("body") or {}
        req_id = str((payload.get("headers") or {}).get("req_id") or "")
        chattype = body.get("chattype", "single")
        message_type = "group" if chattype == "group" else "private"
        chat_id = str(body.get("chatid") or "")
        sender_id = str((body.get("from") or {}).get("userid") or "")
        session_id = chat_id or sender_id
        if not session_id:
            self.log("warn", "回调缺少会话标识，已忽略")
            return

        text = self._extract_text(body)
        session_key = build_session_key(self.channel_type, message_type, session_id, self.binding_id)
        stream_id = f"{session_id}_{uuid.uuid4().hex[:10]}"
        self._pending[session_key] = {"req_id": req_id, "stream_id": stream_id}

        if not text:
            self.log("info", f"收到不支持的消息类型 {body.get('msgtype')}，已忽略")
            await self._respond(session_key, stream_id, "我暂时只看得懂文字消息～", finish=True)
            return

        self.log("info", f"收到{'群' if message_type == 'group' else '单聊'}消息", text[:200])
        self.commit_event(InboundMessage(
            channel_type=self.channel_type,
            session_key=session_key,
            user_message=text,
            # 企微 msgid 作幂等键
            idempotency_key=str(body.get("msgid") or ""),
            sender_id=sender_id,
            message_type=message_type,
            group_id=chat_id if message_type == "group" else "",
            raw={"req_id": req_id, "stream_id": stream_id},
        ))

    async def _process_event(self, payload: dict[str, Any]) -> None:
        body = payload.get("body") or {}
        req_id = str((payload.get("headers") or {}).get("req_id") or "")
        event = body.get("event") or {}
        event_type = event.get("eventtype")
        if event_type == "enter_chat":
            welcome = str(self.config.get("welcome_text") or "").strip()
            if welcome and req_id:
                await self._send_and_wait(
                    CMD_RESPOND_WELCOME, req_id,
                    {"msgtype": "text", "text": {"content": welcome}},
                )
            return
        if event_type == "disconnected_event":
            # 同一个机器人有新的长连接进来时，旧连接会被要求下线
            self.log("warn", "收到 disconnected_event：该机器人在别处重新连接，本连接将关闭")
            self._stop.set()
            return
        self.log("debug", f"忽略事件 {event_type}")

    # ---------- 发送 ----------

    async def _throttle(self) -> None:
        """发送节流：滑动窗口，贴近平台 30 条/分钟的硬限。"""
        limit = int(self.config.get("send_rate_per_minute") or 30)
        while True:
            now = time.monotonic()
            while self._send_log and now - self._send_log[0] > 60:
                self._send_log.popleft()
            if len(self._send_log) < limit:
                self._send_log.append(now)
                return
            await asyncio.sleep(max(0.05, 60 - (now - self._send_log[0]) + 0.05))

    async def _respond(self, session_key: str, stream_id: str, text: str, finish: bool) -> dict[str, Any] | None:
        pending = self._pending.get(session_key) or {}
        req_id = pending.get("req_id", "")
        if not req_id:
            raise RuntimeError("找不到这条消息的 req_id，无法回复")
        await self._throttle()
        return await self._send_and_wait(
            CMD_RESPOND,
            req_id,
            {
                "msgtype": "stream",
                "stream": {"id": stream_id, "finish": finish, "content": text},
            },
        )

    async def send_placeholder(self, session_key: str) -> str:
        """先发 finish=false 的占位分片，返回 stream.id 作句柄。"""
        pending = self._pending.get(session_key) or {}
        stream_id = pending.get("stream_id") or f"{uuid.uuid4().hex[:10]}"
        try:
            await self._respond(
                session_key, stream_id, str(self.config.get("placeholder_text") or "让我想想…"), finish=False
            )
            self.stats["placeholders"] += 1
        except Exception as exc:  # noqa: BLE001 — 占位失败不影响后续正式回复
            self.log("warn", f"占位消息发送失败：{exc}")
        return stream_id

    async def send_final(self, session_key: str, handle: str, text: str) -> None:
        """用 finish=true 全量替换占位内容。"""
        stream_id = handle if handle and not handle.startswith("wecom:") else (
            (self._pending.get(session_key) or {}).get("stream_id") or uuid.uuid4().hex[:10]
        )
        try:
            reply = await self._respond(session_key, stream_id, text, finish=True)
            if reply is not None and reply.get("errcode") not in (0, None):
                self.stats["send_failed"] += 1
                self.record_error(
                    f"企业微信回复失败 errcode={reply.get('errcode')} errmsg={reply.get('errmsg')}"
                )
                return
            self.stats["sent"] += 1
            self.log("info", "已回复", text[:200])
        finally:
            self._pending.pop(session_key, None)

    async def terminate(self) -> None:
        self._stop.set()
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass
        self.log("info", "企业微信适配器已停止")
        await super().terminate()
