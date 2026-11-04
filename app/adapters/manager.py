"""适配器管理器 + 事件总线。

照搬 AstrBot 的运行模型（`platform/manager.py` + `event_bus.py` 的思路）：

    适配器 run() 收包 → commit_event() 投入 asyncio.Queue
        → EventBus 单消费者取出 → 每条消息派一个 Task 交给管线
        → 管线：去重 → 限流 → 占位 → 组装 → 调模型 → 更新 → 落库

与 AstrBot 一致的要点：
- 每个适配器两个 Task（run 本体 + 异常包装），异常转 record_error 而非静默死掉
- `_pending_tasks` 持强引用防止 Task 被 GC 提前回收
- 停机时排空队列并取消在途 Task

相比 AstrBot 的改进：去重在管线统一归口（它是各适配器各写一套内存 TTL）。
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from typing import Any, Callable

from .base import ChannelAdapter, InboundMessage, PlatformStatus
from .registry import ADAPTER_REGISTRY, get_meta

logger = logging.getLogger(__name__)


class AdapterManager:
    """托管所有已连接的适配器实例与消息分发。"""

    # 同一 binding 只提示一次「丢弃过期消息」，避免日志刷屏
    _drop_noted: set[str] = set()

    def __init__(self, handler: Callable[[InboundMessage], Any] | None = None) -> None:
        self.queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.adapters: dict[str, ChannelAdapter] = {}   # binding_id -> adapter
        self._tasks: dict[str, asyncio.Task] = {}
        self._pending: set[asyncio.Task] = set()
        self._consumer: asyncio.Task | None = None
        self.handler = handler
        self.processed = 0
        self.failed = 0
        self.dropped = 0

    # ---------- 构建 ----------

    def build(
        self, channel_type: str, binding_id: str, character_id: str, config: dict[str, Any]
    ) -> ChannelAdapter:
        meta = get_meta(channel_type)
        if not meta.enabled:
            raise ValueError(meta.disabled_reason)
        cls = ADAPTER_REGISTRY[channel_type]
        return cls(binding_id, character_id, config, self.queue)

    # ---------- 生命周期 ----------

    async def start(self, adapter: ChannelAdapter) -> ChannelAdapter:
        """启动适配器为常驻 Task。失败不抛异常，转 error 状态由 UI 展示。"""
        await self.stop(adapter.binding_id)
        self.adapters[adapter.binding_id] = adapter

        async def _wrapper() -> None:
            try:
                await adapter.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                adapter.record_error(str(exc), traceback.format_exc())

        self._tasks[adapter.binding_id] = asyncio.create_task(_wrapper())
        self.ensure_consumer()
        # 让 run() 有机会跑到状态切换点，便于调用方立刻读到真实状态
        await asyncio.sleep(0.05)
        return adapter

    async def stop(self, binding_id: str) -> bool:
        task = self._tasks.pop(binding_id, None)
        adapter = self.adapters.pop(binding_id, None)
        if adapter is not None:
            try:
                await adapter.terminate()
            except Exception:  # noqa: BLE001
                pass
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        return adapter is not None

    async def stop_all(self) -> None:
        for binding_id in list(self._tasks):
            await self.stop(binding_id)
        if self._consumer is not None and not self._consumer.done():
            self._consumer.cancel()
            try:
                await self._consumer
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._consumer = None
        for t in list(self._pending):
            if not t.done():
                t.cancel()
        self._pending.clear()

    def get(self, binding_id: str) -> ChannelAdapter | None:
        return self.adapters.get(binding_id)

    def snapshot(self) -> list[dict[str, Any]]:
        return [a.snapshot() for a in self.adapters.values()]

    # ---------- 事件总线 ----------

    def ensure_consumer(self) -> None:
        if self._consumer is None or self._consumer.done():
            self._consumer = asyncio.create_task(self._consume())

    async def _consume(self) -> None:
        """单消费者循环：取出消息后派独立 Task，避免慢消息阻塞队列。"""
        while True:
            msg = await self.queue.get()
            if self.handler is None:
                continue
            task = asyncio.create_task(self._dispatch(msg))
            # 持强引用，防止 Task 被 GC 提前回收
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def _dispatch(self, msg: InboundMessage) -> None:
        """一条消息的完整处理：占位 → 交管线 → 回写结果。

        管线里是同步的 HTTP 调用（模型推理可能十几秒），必须丢进线程池。
        直接在事件循环里跑会把这个进程里所有机器人一起冻住——
        一个慢模型会让另一个平台的回包超时，这是最容易忽略的坑。

        **适配器已经不在了就直接丢掉这条消息**，而且不能交给管线：
        过期的长连接（botpy 断线重连的残留）还会继续收包，如果让它进管线，
        它会把这条消息的幂等键标成「已处理」，然后因为没有适配器而发不出回复，
        真正在线的那个连接反而被判重复 —— 用户看到的就是「发消息不回」。
        """
        adapter = self.adapters.get(msg.binding_id)
        if adapter is None:
            self.dropped += 1
            self._log_drop(msg)
            return

        handle = ""
        try:
            # 平台硬要求：先回占位（飞书 3 秒、企微流式）
            if adapter.meta.requires_placeholder:
                handle = await adapter.send_placeholder(msg.session_key)

            result = await asyncio.to_thread(self.handler, msg)
            if asyncio.iscoroutine(result):
                result = await result
            reply = (result or {}).get("reply", "") if isinstance(result, dict) else str(result or "")
            warning = (result or {}).get("warning", "") if isinstance(result, dict) else ""
            if warning:
                adapter.log("warn", str(warning))

            if reply:
                await adapter.send_final(msg.session_key, handle or msg.session_key, reply)
            self.processed += 1
        except Exception as exc:  # noqa: BLE001 — 单条消息失败不应终止消费循环
            self.failed += 1
            adapter.record_error(f"消息处理失败: {exc}")
            adapter.log("error", "消息处理失败", traceback.format_exc())

    @staticmethod
    def _log_drop(msg: InboundMessage) -> None:
        """丢弃过期连接的消息只记一次摘要，别把日志刷爆。"""
        if msg.binding_id not in AdapterManager._drop_noted:
            AdapterManager._drop_noted.add(msg.binding_id)
            logger.info(
                "[bus] 收到已停止渠道的消息，已丢弃 binding=%s channel=%s",
                msg.binding_id,
                msg.channel_type,
            )

    def stats(self) -> dict[str, Any]:
        return {
            "queue_size": self.queue.qsize(),
            "running_adapters": sum(
                1 for a in self.adapters.values() if a.status == PlatformStatus.RUNNING
            ),
            "total_adapters": len(self.adapters),
            "processed": self.processed,
            "failed": self.failed,
            "dropped": self.dropped,
            "in_flight": len(self._pending),
        }
