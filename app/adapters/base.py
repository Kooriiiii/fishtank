"""渠道适配器层。

架构照搬 AstrBot 的 Platform 抽象（`core/platform/platform.py` 的设计思路，
独立实现，未复制其源码）：

- **状态机** PlatformStatus：pending/connecting/running/error/stopped
- **错误簿记** record_error，真机连不上时 UI 能看到原因而非假装已连
- **事件队列** commit_event：适配器只负责把归一化消息投进队列，
  不直接调业务逻辑，与消息管线彻底解耦
- **生命周期** run/terminate 由 AdapterManager 统一托管为 asyncio Task
- **配置 schema** 每个适配器声明自己的字段，WebUI 自动渲染表单

三平台的共性决定了抽象成本极低：凭证都是 AppID/Secret 类、
都支持 WebSocket 长连接、都无需公网 IP。
"""

from __future__ import annotations

import abc
import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .registry import AdapterMeta, ConfigField, register_adapter


class PlatformStatus(str, Enum):
    PENDING = "pending"
    CONNECTING = "connecting"
    RUNNING = "running"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass
class PlatformLog:
    """一条运行时日志。真机联调时这是唯一的真相来源。"""

    level: str
    message: str
    at: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.at:
            self.at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "message": self.message, "at": self.at, "detail": self.detail}


@dataclass
class PlatformError:
    message: str
    at: str = ""
    traceback: str | None = None

    def __post_init__(self) -> None:
        if not self.at:
            self.at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class InboundMessage:
    """归一化入站消息。所有适配器都必须产出这个结构。

    比起只有一个文本字段，这里保留了发送者、消息类型与原始载荷，
    便于后续扩展群聊隔离与富媒体，而不必改动管线。
    """

    channel_type: str
    session_key: str            # 跨平台统一会话标识
    user_message: str
    idempotency_key: str        # 飞书 event_id / 企微 msgid / QQ 消息 id
    sender_id: str = ""
    sender_name: str = ""
    message_type: str = "private"   # private | group
    group_id: str = ""
    binding_id: str = ""
    character_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def build_session_key(
    channel_type: str, message_type: str, session_id: str, binding_id: str = ""
) -> str:
    """结构化会话标识。

    借鉴 AstrBot 的 unified_msg_origin（`platform_id:message_type:session_id`），
    可逆解析，便于区分私聊/群聊并隔离不同绑定实例的上下文。
    """
    scope = binding_id or channel_type
    return f"{scope}:{message_type}:{session_id}"


def parse_session_key(key: str) -> tuple[str, str, str]:
    parts = (key or "").split(":", 2)
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


class ChannelAdapter(abc.ABC):
    """适配器基类。

    子类只需实现 run / send_placeholder / send_final；
    状态机、错误记录、事件投递由基类统一提供。
    """

    channel_type: str = "base"
    meta: AdapterMeta

    def __init__(
        self,
        binding_id: str,
        character_id: str,
        config: dict[str, Any] | None = None,
        event_queue: asyncio.Queue | None = None,
    ) -> None:
        self.binding_id = binding_id
        self.character_id = character_id
        self.config = config or {}
        self._queue = event_queue
        self._status = PlatformStatus.PENDING
        self._errors: list[PlatformError] = []
        self._started_at: str | None = None
        self.client_self_id = uuid.uuid4().hex[:12]
        self.stats = {"received": 0, "sent": 0, "placeholders": 0, "send_failed": 0}
        # 环形缓冲：真机联调时用户不需要去看控制台，界面里就能看到收发与异常
        self._logs: deque[PlatformLog] = deque(maxlen=200)

    # ---------- 状态 ----------

    @property
    def status(self) -> PlatformStatus:
        return self._status

    @status.setter
    def status(self, value: PlatformStatus) -> None:
        self._status = value
        if value == PlatformStatus.RUNNING and self._started_at is None:
            self._started_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    @property
    def last_error(self) -> PlatformError | None:
        return self._errors[-1] if self._errors else None

    def record_error(self, message: str, tb: str | None = None) -> None:
        """记录错误并转入 error 状态。绝不伪装成已连接。"""
        self._errors.append(PlatformError(message=str(message)[:500], traceback=tb))
        self._status = PlatformStatus.ERROR
        self.log("error", str(message)[:300], tb or "")

    def log(self, level: str, message: str, detail: str = "") -> None:
        self._logs.append(PlatformLog(level=level, message=str(message)[:500], detail=str(detail)[:4000]))

    def logs(self, limit: int = 80) -> list[dict[str, Any]]:
        """最近 limit 条日志，倒序（最新在前）。"""
        items = list(self._logs)[-max(1, limit):]
        return [i.to_dict() for i in reversed(items)]

    def clear_errors(self) -> None:
        self._errors.clear()
        if self._status == PlatformStatus.ERROR:
            self._status = PlatformStatus.PENDING

    def snapshot(self) -> dict[str, Any]:
        err = self.last_error
        return {
            "binding_id": self.binding_id,
            "character_id": self.character_id,
            "channel_type": self.channel_type,
            "status": self._status.value,
            "started_at": self._started_at,
            "last_error": err.message if err else "",
            "last_error_at": err.at if err else None,
            "error_count": len(self._errors),
            "stats": dict(self.stats),
            "logs": self.logs(30),
        }

    # ---------- 事件投递 ----------

    def commit_event(self, msg: InboundMessage) -> None:
        """把归一化消息投进事件队列。适配器不直接调业务逻辑。"""
        msg.binding_id = self.binding_id
        msg.character_id = self.character_id
        self.stats["received"] += 1
        if self._queue is not None:
            self._queue.put_nowait(msg)

    # ---------- 子类实现 ----------

    @abc.abstractmethod
    async def run(self) -> None:
        """建立连接并持续接收消息。由 AdapterManager 托管为常驻 Task。"""

    @abc.abstractmethod
    async def send_placeholder(self, session_key: str) -> str:
        """立即回占位消息，返回可用于后续替换的句柄。

        不是优化项：飞书要求 3 秒内响应，企微长连接要求先发
        finish=false 的流式占位，而模型推理普遍超过 3 秒。
        """

    @abc.abstractmethod
    async def send_final(self, session_key: str, handle: str, text: str) -> None:
        """用最终结果替换占位消息。"""

    async def terminate(self) -> None:
        self.status = PlatformStatus.STOPPED
