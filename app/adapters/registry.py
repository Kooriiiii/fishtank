"""适配器注册表与配置 Schema。

照搬 AstrBot 的做法（`core/platform/register.py` + `platform_metadata.py` 的思路，
独立实现）：每个适配器用装饰器声明自己的 **配置模板 + 字段元数据**，
前端拉取 `/api/platforms/schema` 后自动渲染表单，不再手写死输入框。

这样新增一个平台只需两件事：
1. 实现 ChannelAdapter 的几个方法；
2. 在装饰器里声明字段。
WebUI、校验、脱敏规则全部自动生效，无需改动前端或 API 层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------- 字段类型 ----------------
# text     普通文本
# secret   敏感凭证：前端用 password 框，展示脱敏，不回传明文
# number   数字，带 min/max
# bool     开关
# select   下拉，options 给候选
FIELD_TYPES = ("text", "secret", "number", "bool", "select")


@dataclass
class ConfigField:
    """一个配置项的声明。前端据此渲染控件，后端据此校验。"""

    key: str
    label: str
    type: str = "text"
    required: bool = False
    default: Any = ""
    placeholder: str = ""
    hint: str = ""
    options: list[dict[str, str]] = field(default_factory=list)
    min: int | None = None
    max: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "default": self.default,
            "placeholder": self.placeholder,
            "hint": self.hint,
        }
        if self.options:
            d["options"] = self.options
        if self.min is not None:
            d["min"] = self.min
        if self.max is not None:
            d["max"] = self.max
        return d


@dataclass
class AdapterMeta:
    """适配器元数据。对应 AstrBot 的 PlatformMetadata。"""

    name: str                      # 唯一标识，如 qq_official
    label: str                     # 界面显示名
    description: str
    icon: str = "🔌"
    enabled: bool = True           # False 表示平台级禁用（如个人微信）
    disabled_reason: str = ""
    doc_hint: str = ""
    doc_url: str = ""
    connection_mode: str = ""      # websocket / webhook / internal
    supports_proactive: bool = True   # 是否支持主动消息
    requires_placeholder: bool = False  # 是否必须先回占位响应
    fields: list[ConfigField] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)  # 平台硬限制，展示给用户

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_type": self.name,
            "label": self.label,
            "description": self.description,
            "icon": self.icon,
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason,
            "doc_hint": self.doc_hint,
            "doc_url": self.doc_url,
            "connection_mode": self.connection_mode,
            "supports_proactive": self.supports_proactive,
            "requires_placeholder": self.requires_placeholder,
            "constraints": list(self.constraints),
            "fields": [f.to_dict() for f in self.fields],
            "secret_keys": [f.key for f in self.fields if f.type == "secret"],
        }

    def defaults(self) -> dict[str, Any]:
        return {f.key: f.default for f in self.fields}


# ---------------- 注册表 ----------------

ADAPTER_REGISTRY: dict[str, type] = {}
ADAPTER_META: dict[str, AdapterMeta] = {}
_ORDER: list[str] = []


def register_adapter(meta: AdapterMeta):
    """注册适配器。重名直接报错，避免静默覆盖。"""

    def decorator(cls):
        if meta.name in ADAPTER_REGISTRY:
            raise ValueError(f"渠道适配器 {meta.name} 已注册，发生命名冲突")
        cls.channel_type = meta.name
        cls.meta = meta
        ADAPTER_REGISTRY[meta.name] = cls
        ADAPTER_META[meta.name] = meta
        _ORDER.append(meta.name)
        return cls

    return decorator


def catalog() -> list[dict[str, Any]]:
    """渠道目录，按注册顺序返回。供前端渲染卡片与表单。"""
    return [ADAPTER_META[n].to_dict() for n in _ORDER]


def get_meta(channel_type: str) -> AdapterMeta:
    meta = ADAPTER_META.get(channel_type)
    if meta is None:
        raise ValueError(f"未知渠道类型: {channel_type}")
    return meta


class ConfigValidationError(Exception):
    """配置校验失败，由 API 层翻译成 400。"""


def validate_config(channel_type: str, raw: dict[str, Any]) -> dict[str, Any]:
    """按 schema 校验并归一化配置。

    校验规则来自字段声明本身，新增平台不需要改这里。
    """
    meta = get_meta(channel_type)
    if not meta.enabled:
        raise ConfigValidationError(meta.disabled_reason)

    raw = raw or {}
    out: dict[str, Any] = {}
    for f in meta.fields:
        value = raw.get(f.key, None)
        if value is None or value == "":
            if f.required:
                raise ConfigValidationError(f"{meta.label}：{f.label} 为必填项")
            out[f.key] = f.default
            continue

        if f.type == "number":
            try:
                num = int(value)
            except (TypeError, ValueError):
                raise ConfigValidationError(f"{f.label} 必须是整数") from None
            if f.min is not None and num < f.min:
                raise ConfigValidationError(f"{f.label} 不能小于 {f.min}")
            if f.max is not None and num > f.max:
                raise ConfigValidationError(f"{f.label} 不能大于 {f.max}")
            out[f.key] = num
        elif f.type == "bool":
            out[f.key] = bool(value) if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
        elif f.type == "select":
            allowed = [o["value"] for o in f.options]
            if str(value) not in allowed:
                raise ConfigValidationError(f"{f.label} 只能是 {'、'.join(allowed)} 之一")
            out[f.key] = str(value)
        else:
            text = str(value).strip()
            if f.required and not text:
                raise ConfigValidationError(f"{meta.label}：{f.label} 为必填项")
            out[f.key] = text

    return out
