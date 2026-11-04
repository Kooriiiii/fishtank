"""适配器包：导入即完成注册。

导入顺序决定前端渠道卡片的展示顺序：
Mock（默认可用）→ 三个官方平台 → 个人微信（置灰）。
"""

from .base import (  # noqa: F401
    ChannelAdapter,
    InboundMessage,
    PlatformError,
    PlatformStatus,
    build_session_key,
    parse_session_key,
)
from .registry import (  # noqa: F401
    ADAPTER_META,
    ADAPTER_REGISTRY,
    AdapterMeta,
    ConfigField,
    ConfigValidationError,
    catalog,
    get_meta,
    register_adapter,
    validate_config,
)

# 以下导入触发装饰器注册，顺序即展示顺序
from .mock import MockAdapter  # noqa: F401,E402
from .qq_official import QQOfficialAdapter  # noqa: F401,E402
from .wecom import WecomAdapter  # noqa: F401,E402
from .feishu import FeishuAdapter  # noqa: F401,E402
from .personal_wechat import PersonalWechatAdapter  # noqa: F401,E402
from .manager import AdapterManager  # noqa: F401,E402

ENABLED_CHANNELS = {n for n, m in ADAPTER_META.items() if m.enabled}
DISABLED_CHANNELS = {n: m.disabled_reason for n, m in ADAPTER_META.items() if not m.enabled}

__all__ = [
    "ADAPTER_META",
    "ADAPTER_REGISTRY",
    "AdapterManager",
    "AdapterMeta",
    "ChannelAdapter",
    "ConfigField",
    "ConfigValidationError",
    "DISABLED_CHANNELS",
    "ENABLED_CHANNELS",
    "InboundMessage",
    "PlatformStatus",
    "build_session_key",
    "catalog",
    "get_meta",
    "parse_session_key",
    "register_adapter",
    "validate_config",
]
