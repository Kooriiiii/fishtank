"""个人微信：显式注册为禁用渠道。

刻意注册而非省略：UI 上必须出现这张卡片并标注禁用原因，
向用户与评审主动传达合规立场，而不是让人误以为「还没做」。

腾讯官方不允许机器人操控个人微信账号收发消息；市面方案只有
客户端注入与协议模拟两条路，均违反微信《可接受使用政策》
且封号风险极高。本平台不实现、不演示任何相关能力。
"""

from __future__ import annotations

from typing import Any

from .base import ChannelAdapter
from .registry import AdapterMeta, register_adapter

DISABLED_REASON = (
    "腾讯官方不允许机器人操控个人微信账号收发消息。"
    "市面方案仅有客户端注入与协议模拟两条路径，均违反微信《可接受使用政策》"
    "且封号风险极高。本平台不实现、不演示任何相关能力。"
)


@register_adapter(AdapterMeta(
    name="personal_wechat",
    label="个人微信",
    description="本平台永久不支持。",
    icon="🚫",
    enabled=False,
    disabled_reason=DISABLED_REASON,
    doc_hint="本平台永久不支持，不提供任何绕过方案。",
    connection_mode="",
    supports_proactive=False,
    fields=[],
    constraints=["违反微信《可接受使用政策》，封号风险极高。"],
))
class PersonalWechatAdapter(ChannelAdapter):
    """仅用于在目录中占位展示，任何操作都明确拒绝。"""

    async def run(self) -> None:
        raise NotImplementedError(DISABLED_REASON)

    async def send_placeholder(self, session_key: str) -> str:
        raise NotImplementedError(DISABLED_REASON)

    async def send_final(self, session_key: str, handle: str, text: str) -> None:
        raise NotImplementedError(DISABLED_REASON)
