"""会话级限流器（滑动窗口日志）。

算法思路借鉴 AstrBot 的 RateLimitStage（独立实现，未复制其代码）：
每个会话维护一个请求时间戳队列，取请求前先淘汰窗口外的旧时间戳，
再按队列长度判断是否超限。两点刻意的差异：

1. **默认 discard 而非 stall。** AstrBot 的管线是 asyncio 异步消费，
   stall 只挂起一个协程；FishConsole 的对话接口是同步 HTTP 端点，
   stall 会占住工作线程，高并发下反而放大故障。因此默认拒绝并返回
   429 + Retry-After，由调用方决定重试，stall 作为可选策略保留。

2. **锁与队列会被回收。** AstrBot 用 defaultdict 持有 per-session 锁，
   只增不删，长期运行下会话数越多内存占用越大。这里在每次取请求时
   顺带清理空闲会话，避免同样的泄漏。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

from .settings import MAX_STALL_SECONDS, SettingsService

# 空闲会话回收：超过「窗口长度 × 该倍数」未访问即丢弃其队列与锁
IDLE_FACTOR = 3
# 每次取请求最多顺带清理的会话数，避免单次调用做太多工作
SWEEP_BATCH = 64


@dataclass
class RateLimitDecision:
    allowed: bool
    remaining: int          # 本窗口剩余可用次数
    retry_after: float      # 需等待多久才会有名额（秒）
    limit: int
    window_seconds: int
    strategy: str
    stalled: float = 0.0    # 实际 stall 了多久


class RateLimitExceeded(Exception):
    """超出限流阈值。由 API 层翻译成 429。"""

    def __init__(self, decision: RateLimitDecision) -> None:
        super().__init__(
            f"请求过于频繁：{decision.window_seconds} 秒内最多 {decision.limit} 次，"
            f"请 {decision.retry_after:.1f} 秒后重试"
        )
        self.decision = decision


class RateLimiter:
    """线程安全的滑动窗口限流器。

    用单把全局锁保护所有会话队列。队列操作是 O(过期条目数) 的纯内存操作，
    临界区极短；相比 AstrBot 的 per-session 锁，少了「锁对象本身无限增长」
    的问题，在本项目的单机量级下代价可忽略。
    """

    def __init__(self, settings: SettingsService) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        # OrderedDict 维持访问顺序，回收时从最久未用的一端开始扫
        self._windows: OrderedDict[str, deque[float]] = OrderedDict()

    def _sweep(self, now: float, idle_ttl: float) -> None:
        """回收空闲会话。调用方必须已持锁。"""
        checked = 0
        while self._windows and checked < SWEEP_BATCH:
            key, stamps = next(iter(self._windows.items()))
            last_seen = stamps[-1] if stamps else 0.0
            if now - last_seen <= idle_ttl:
                break  # 最久未用的都还新鲜，后面的更新，无需继续
            self._windows.popitem(last=False)
            checked += 1

    def check(self, session_key: str, cost: int = 1) -> RateLimitDecision:
        """取一个请求名额。

        未超限时记录时间戳并返回 allowed=True；
        超限时按策略 discard（抛 RateLimitExceeded）或 stall（阻塞到有名额）。
        """
        cfg = self.settings.rate_limit()
        if not cfg.enabled or cfg.count <= 0:
            return RateLimitDecision(True, cfg.count, 0.0, cfg.count,
                                     cfg.window_seconds, cfg.strategy)

        window = float(cfg.window_seconds)
        idle_ttl = window * IDLE_FACTOR
        key = session_key or "anonymous"
        total_stalled = 0.0
        deadline = time.monotonic() + MAX_STALL_SECONDS

        while True:
            with self._lock:
                now = time.monotonic()
                self._sweep(now, idle_ttl)

                stamps = self._windows.get(key)
                if stamps is None:
                    stamps = deque()
                    self._windows[key] = stamps
                self._windows.move_to_end(key)

                # 淘汰窗口外的时间戳
                cutoff = now - window
                while stamps and stamps[0] <= cutoff:
                    stamps.popleft()

                if len(stamps) + cost <= cfg.count:
                    for _ in range(cost):
                        stamps.append(now)
                    return RateLimitDecision(
                        allowed=True,
                        remaining=max(0, cfg.count - len(stamps)),
                        retry_after=0.0,
                        limit=cfg.count,
                        window_seconds=cfg.window_seconds,
                        strategy=cfg.strategy,
                        stalled=total_stalled,
                    )

                # 最早的那条时间戳滑出窗口时，就会空出名额
                retry_after = max(0.0, stamps[0] + window - now) + 0.05
                decision = RateLimitDecision(
                    allowed=False,
                    remaining=0,
                    retry_after=retry_after,
                    limit=cfg.count,
                    window_seconds=cfg.window_seconds,
                    strategy=cfg.strategy,
                    stalled=total_stalled,
                )

            # 释放锁后再等待，避免阻塞其他会话
            if cfg.strategy != "stall":
                raise RateLimitExceeded(decision)
            if time.monotonic() + retry_after > deadline:
                # stall 也有上限，避免请求被无限期挂住
                raise RateLimitExceeded(decision)
            time.sleep(retry_after)
            total_stalled += retry_after

    def peek(self, session_key: str) -> RateLimitDecision:
        """只读探测当前配额，不消耗名额。供前端展示用。"""
        cfg = self.settings.rate_limit()
        if not cfg.enabled or cfg.count <= 0:
            return RateLimitDecision(True, cfg.count, 0.0, cfg.count,
                                     cfg.window_seconds, cfg.strategy)
        window = float(cfg.window_seconds)
        key = session_key or "anonymous"
        with self._lock:
            now = time.monotonic()
            stamps = self._windows.get(key, deque())
            cutoff = now - window
            used = sum(1 for t in stamps if t > cutoff)
            retry_after = 0.0
            if used >= cfg.count and stamps:
                oldest = next((t for t in stamps if t > cutoff), now)
                retry_after = max(0.0, oldest + window - now)
        return RateLimitDecision(
            allowed=used < cfg.count,
            remaining=max(0, cfg.count - used),
            retry_after=retry_after,
            limit=cfg.count,
            window_seconds=cfg.window_seconds,
            strategy=cfg.strategy,
        )

    def reset(self, session_key: str | None = None) -> int:
        """清空限流计数。返回被清理的会话数。"""
        with self._lock:
            if session_key:
                return 1 if self._windows.pop(session_key, None) is not None else 0
            n = len(self._windows)
            self._windows.clear()
            return n

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "tracked_sessions": len(self._windows),
                "total_timestamps": sum(len(v) for v in self._windows.values()),
            }
