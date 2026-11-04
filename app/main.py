"""FishConsole 后端入口。

启动：
    python -m uvicorn app.main:app --reload --port 8000
或：
    python run.py

前后端同进程：FastAPI 用 StaticFiles 托管 web/ 目录，一条命令起全栈。
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .adapters import AdapterManager, catalog as adapter_catalog
from .adapters.registry import ConfigValidationError
from .db import Database
from .seed import seed_database
from .services.catalog import CharacterService, ChannelService, ServiceError, UsageService
from .services.dedup import DedupService
from .services.llm import MockProvider
from .services.memory import MemoryError_, MemoryService
from .services.persona import PersonaError, PersonaService
from .services.pipeline import MessagePipeline, PipelineError
from .services.providers import PRESETS, PROVIDER_TYPES, ProviderError, ProviderService
from .services.ratelimit import RateLimiter, RateLimitExceeded
from .services.settings import SettingsError, SettingsService
from .services.stats import RANGES as STATS_RANGES
from .services.stats import StatsService

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
APP_VERSION = "2.1.0"


# ---------------- 请求模型 ----------------

class PersonaPayload(BaseModel):
    system_prompt: str | None = None
    personality_tags: list[str] | None = None
    backstory: str | None = None
    speech_style: str | None = None
    catchphrases: list[str] | None = None
    taboo_topics: list[str] | None = None
    style_hint: str | None = None
    note: str = ""


class CharacterCreate(PersonaPayload):
    name: str = Field(min_length=1, max_length=40)
    one_liner: str = ""


class CharacterUpdate(BaseModel):
    name: str | None = None
    one_liner: str | None = None


class CharacterModelPayload(BaseModel):
    """机器人的模型路由：绑哪个模型服务、带几轮上下文。"""

    model_provider_id: str | None = None
    context_rounds: int | None = Field(default=None, ge=0, le=50)


class ProviderPayload(BaseModel):
    """模型服务。api_key 留空或传脱敏串都表示「沿用原 Key」。"""

    name: str = Field(min_length=1, max_length=40)
    provider_type: str = "openai_chat_completion"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.7
    max_tokens: int = 1000
    is_default: bool = False


class ProviderUpdate(BaseModel):
    name: str | None = None
    provider_type: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    is_default: bool | None = None


class MemoryPayload(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    source: str = "manual"
    importance: int = Field(default=3, ge=1, le=5)
    pinned: bool = False


class MemoryUpdate(BaseModel):
    content: str | None = None
    source: str | None = None
    importance: int | None = Field(default=None, ge=1, le=5)
    pinned: bool | None = None


class BindingPayload(BaseModel):
    """绑定请求。config 的字段由适配器 schema 定义，这里不做枚举。"""

    channel_type: str
    config: dict[str, Any] = Field(default_factory=dict)
    # 兼容旧字段名
    credentials: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, _ctx: Any) -> None:
        if not self.config and self.credentials:
            self.config = self.credentials


class BindingConfigPayload(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


class SimulatePayload(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str = "local"
    idempotency_key: str = ""


class ChatPayload(BaseModel):
    character_id: str
    message: str = Field(min_length=1, max_length=2000)
    session_key: str = "console:debug"
    channel_type: str = "mock"
    idempotency_key: str = ""


class ABPayload(BaseModel):
    character_id: str
    message: str = Field(min_length=1, max_length=2000)
    left_version_id: str
    right_version_id: str


class RollbackPayload(BaseModel):
    note: str = ""


class RateLimitPayload(BaseModel):
    """限流设置。字段均可选，只提交要改的部分。"""

    enabled: bool | None = None
    count: int | None = Field(default=None, ge=1, le=10000)
    window_seconds: int | None = Field(default=None, ge=1, le=86400)
    strategy: str | None = Field(default=None, pattern="^(discard|stall)$")


class DedupPayload(BaseModel):
    """去重与 TTL 清理设置。ttl_seconds=0 表示永不过期（仍受 max_rows 约束）。"""

    enabled: bool | None = None
    ttl_seconds: int | None = Field(default=None, ge=0, le=30 * 86400)
    sweep_interval_seconds: int | None = Field(default=None, ge=10, le=86400)
    max_rows: int | None = Field(default=None, ge=100, le=10_000_000)


# ---------------- 应用装配 ----------------

class AppContext:
    """集中持有服务实例，便于测试时注入独立数据库。"""

    def __init__(self, db_path: str | None = None, do_seed: bool = True) -> None:
        self.db = Database(db_path or os.environ.get("FISHCONSOLE_DB", "fishconsole.db"))
        self.persona = PersonaService(self.db)
        self.memory = MemoryService(self.db)
        self.characters = CharacterService(self.db, self.persona)
        self.manager = AdapterManager()
        self.channels = ChannelService(self.db, self.manager)
        self.usage = UsageService(self.db)
        self.settings = SettingsService(self.db)
        self.providers = ProviderService(self.db)
        self.dedup = DedupService(self.db, self.settings)
        self.limiter = RateLimiter(self.settings)
        self.stats = StatsService(self.db, manager=self.manager, version=APP_VERSION)
        self.mock = MockProvider()
        self.seed_map: dict[str, str] = {}
        if do_seed:
            self.seed_map = seed_database(self.characters, self.persona, self.memory, self.mock)
        else:
            # 不写入数据库，但仍注册 mock 回复库，保证已有角色能对话
            try:
                from .seed import load_seed_file

                for spec in load_seed_file()["characters"]:
                    for c in self.characters.list_characters():
                        if c["name"] == spec.get("name"):
                            self.mock.register(c["id"], spec.get("mock_replies") or {})
            except (OSError, ValueError):
                pass
        self.pipeline = MessagePipeline(
            self.db, self.persona, self.memory, self.mock,
            dedup=self.dedup, limiter=self.limiter, providers=self.providers,
        )
        # 真机消息与调试台走完全相同的管线：后台能跑通的，接上真机也能跑通
        self.manager.handler = self._handle_inbound

    def _handle_inbound(self, msg) -> dict:
        """适配器入站消息 -> 消息管线。由 AdapterManager 的消费者调用。"""
        outcome = self.pipeline.handle(
            character_id=msg.character_id,
            user_message=msg.user_message,
            session_key=msg.session_key,
            channel_type=msg.channel_type,
            idempotency_key=msg.idempotency_key,
        )
        return outcome.to_dict()

    @property
    def llm_mode(self) -> str:
        """当前全局生效的模型名。没有配置模型服务时为 mock。"""
        row = self.providers.default_row()
        return str(row["model"]) if row is not None else "mock"

    def effective_model(self, character: dict[str, Any]) -> dict[str, Any]:
        """机器人实际会用的模型服务，供界面显示「这个鱼用哪个模型」。"""
        provider, model, label = self.pipeline.resolve_provider(character)
        return {
            "model": model,
            "provider_name": label,
            "provider_id": str(character.get("model_provider_id") or ""),
            "is_fallback": provider is self.mock,
            "context_rounds": int(character.get("context_rounds") or 0),
        }


def create_app(
    db_path: str | None = None, do_seed: bool = True, autostart: bool = True
) -> FastAPI:
    ctx = AppContext(db_path, do_seed)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 凭证填过一次即持久化，重启后自动恢复连接，无需再去界面点一遍
        if autostart:
            await ctx.channels.autostart()
        yield
        await ctx.manager.stop_all()
        ctx.db.close()

    app = FastAPI(title="鱼缸 · FishTank Console", version="2.0.0", lifespan=lifespan)
    app.state.ctx = ctx

    # 业务异常统一翻译成 400，避免 500 把内部细节暴露给前端
    @app.exception_handler(ServiceError)
    @app.exception_handler(PersonaError)
    @app.exception_handler(MemoryError_)
    @app.exception_handler(PipelineError)
    @app.exception_handler(SettingsError)
    @app.exception_handler(ProviderError)
    @app.exception_handler(ConfigValidationError)
    async def business_error(_request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def value_error(_request, exc: ValueError) -> JSONResponse:
        """未知渠道类型等参数问题由 schema 层抛 ValueError，统一归 400。"""
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(RateLimitExceeded)
    async def rate_limited(_request, exc: RateLimitExceeded) -> JSONResponse:
        """限流返回标准 429 + Retry-After，调用方可据此退避重试。"""
        d = exc.decision
        return JSONResponse(
            status_code=429,
            headers={
                "Retry-After": str(max(1, int(d.retry_after + 0.999))),
                "X-RateLimit-Limit": str(d.limit),
                "X-RateLimit-Remaining": "0",
            },
            content={
                "detail": str(exc),
                "rate_limit": {
                    "limit": d.limit,
                    "remaining": 0,
                    "window_seconds": d.window_seconds,
                    "retry_after": round(d.retry_after, 2),
                    "strategy": d.strategy,
                },
            },
        )

    # ---------- 基础 ----------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "llm_mode": ctx.llm_mode,
            "providers": len(ctx.providers.list_providers()),
            "characters": len(ctx.characters.list_characters()),
        }

    # ---------- F1 角色 ----------

    @app.get("/api/characters")
    def list_characters() -> list[dict[str, Any]]:
        return ctx.characters.list_characters()

    @app.post("/api/characters", status_code=201)
    def create_character(payload: CharacterCreate) -> dict[str, Any]:
        character = ctx.characters.create(payload.model_dump())
        return detail(character["id"])

    @app.get("/api/characters/{character_id}")
    def detail(character_id: str) -> dict[str, Any]:
        character = ctx.characters.get(character_id)
        return {
            "character": character,
            "active_version": ctx.persona.get_active(character_id),
            "versions": ctx.persona.list_versions(character_id),
            "memories": ctx.memory.list_memories(character_id),
            "bindings": ctx.channels.list_bindings(character_id),
            "model": ctx.effective_model(ctx.characters.raw(character_id)),
        }

    @app.put("/api/characters/{character_id}")
    def update_character(character_id: str, payload: CharacterUpdate) -> dict[str, Any]:
        data = {k: v for k, v in payload.model_dump().items() if v is not None}
        return ctx.characters.update(character_id, data)

    @app.delete("/api/characters/{character_id}", status_code=204)
    async def delete_character(character_id: str) -> None:
        # 先停掉它的渠道连接：直接删库会让适配器变成收得到消息却发不出回复的僵尸
        await ctx.channels.stop_for_character(character_id)
        ctx.characters.delete(character_id)

    # ---------- F2 人设版本 ----------

    @app.get("/api/characters/{character_id}/versions")
    def list_versions(character_id: str) -> list[dict[str, Any]]:
        ctx.characters.get(character_id)
        return ctx.persona.list_versions(character_id)

    @app.post("/api/characters/{character_id}/versions", status_code=201)
    def create_version(character_id: str, payload: PersonaPayload) -> dict[str, Any]:
        data = payload.model_dump()
        note = data.pop("note", "")
        return ctx.persona.create_version(character_id, data, note=note)

    @app.get("/api/characters/{character_id}/versions/diff")
    def diff_versions(character_id: str, left: str = Query(...), right: str = Query(...)) -> dict[str, Any]:
        result = ctx.persona.diff(left, right)
        if result["left"]["character_id"] != character_id:
            raise HTTPException(400, "版本不属于该角色")
        return result

    @app.post("/api/versions/{version_id}/rollback")
    def rollback(version_id: str, payload: RollbackPayload | None = None) -> dict[str, Any]:
        return ctx.persona.rollback(version_id, note=(payload.note if payload else ""))

    # ---------- F3 记忆 ----------

    @app.get("/api/characters/{character_id}/memories")
    def list_memories(character_id: str) -> list[dict[str, Any]]:
        ctx.characters.get(character_id)
        return ctx.memory.list_memories(character_id)

    @app.post("/api/characters/{character_id}/memories", status_code=201)
    def create_memory(character_id: str, payload: MemoryPayload) -> dict[str, Any]:
        return ctx.memory.create(
            character_id, payload.content, payload.source, payload.importance, payload.pinned
        )

    @app.put("/api/memories/{memory_id}")
    def update_memory(memory_id: str, payload: MemoryUpdate) -> dict[str, Any]:
        return ctx.memory.update(memory_id, {k: v for k, v in payload.model_dump().items() if v is not None})

    @app.delete("/api/memories/{memory_id}", status_code=204)
    def delete_memory(memory_id: str) -> None:
        ctx.memory.delete(memory_id)

    # ---------- F4 渠道（schema 驱动） ----------

    @app.get("/api/platforms/schema")
    def platform_schema() -> list[dict[str, Any]]:
        """渠道目录 + 每个平台自己声明的配置字段。

        前端据此自动渲染表单：新增一个平台只需在适配器里声明字段，
        不必改动本接口与前端代码。
        """
        return adapter_catalog()

    @app.get("/api/channels/catalog")
    def channel_catalog() -> list[dict[str, Any]]:
        """兼容旧路径，等价于 /api/platforms/schema。"""
        return adapter_catalog()

    @app.get("/api/platforms/runtime")
    def platform_runtime() -> dict[str, Any]:
        """适配器运行态：连接状态、收发计数、事件队列水位。"""
        return {"adapters": ctx.manager.snapshot(), "bus": ctx.manager.stats()}

    @app.get("/api/channels/runtime")
    def channels_runtime() -> dict[str, Any]:
        """所有渠道绑定的真实状态（内存适配器优先）。前端平台页轮询它刷新状态。"""
        return {"channels": ctx.channels.runtime()}

    @app.get("/api/characters/{character_id}/channels")
    def list_bindings(character_id: str) -> list[dict[str, Any]]:
        ctx.characters.get(character_id)
        return ctx.channels.list_bindings(character_id)

    @app.post("/api/characters/{character_id}/channels", status_code=201)
    def bind_channel(character_id: str, payload: BindingPayload) -> dict[str, Any]:
        return ctx.channels.bind(character_id, payload.channel_type, payload.config)

    @app.put("/api/channels/{binding_id}")
    def update_binding(binding_id: str, payload: BindingConfigPayload) -> dict[str, Any]:
        return ctx.channels.update_config(binding_id, payload.config)

    @app.post("/api/channels/{binding_id}/connect")
    async def connect_channel(binding_id: str) -> dict[str, Any]:
        return await ctx.channels.connect(binding_id)

    @app.post("/api/channels/{binding_id}/disconnect")
    async def disconnect_channel(binding_id: str) -> dict[str, Any]:
        return await ctx.channels.disconnect(binding_id)

    @app.delete("/api/channels/{binding_id}", status_code=204)
    async def unbind_channel(binding_id: str) -> None:
        await ctx.channels.unbind(binding_id)

    @app.post("/api/channels/{binding_id}/simulate")
    async def simulate_inbound(binding_id: str, payload: SimulatePayload) -> dict[str, Any]:
        """向 Mock 适配器注入一条消息，验证「适配器 → 队列 → 管线」全链路。"""
        adapter = ctx.manager.get(binding_id)
        if adapter is None:
            raise HTTPException(400, "该绑定未处于连接状态")
        if adapter.channel_type != "mock":
            raise HTTPException(400, "仅 Mock 渠道支持模拟注入")
        before = ctx.manager.processed + ctx.manager.failed
        outbox_before = len(adapter.outbox)
        msg = await adapter.feed(payload.message, payload.session_id, payload.idempotency_key)

        # 轮询等待消费完成，而不是固定 sleep：适配器自身的模拟延迟可配置，
        # 写死等待时间会在延迟较大时只看到占位、看不到最终结果。
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if ctx.manager.processed + ctx.manager.failed > before:
                break
            await asyncio.sleep(0.05)

        # 只看本次新产生的发件记录，避免历史消息干扰判断
        fresh = adapter.outbox[outbox_before:]
        # 幂等键命中时管线返回空回复，适配器不会发出最终消息。
        # 明确标注原因，避免被误读为「链路断了」。
        duplicated = bool(fresh) and not any(m.get("finish") for m in fresh)
        return {
            "session_key": msg.session_key,
            "idempotency_key": msg.idempotency_key,
            "outbox": fresh or adapter.outbox[-2:],
            "duplicated": duplicated,
            "note": "该幂等键已处理过，本次按重复事件忽略" if duplicated else "",
            "bus": ctx.manager.stats(),
        }

    # ---------- 模型服务（AstrBot 的「服务提供商」） ----------

    @app.get("/api/providers/presets")
    def provider_presets() -> dict[str, Any]:
        """常见服务商预设 + 可选的协议类型，供新建表单一键填充。"""
        return {"presets": PRESETS, "types": [{"value": k, "label": v} for k, v in PROVIDER_TYPES.items()]}

    @app.get("/api/providers")
    def list_providers() -> list[dict[str, Any]]:
        return ctx.providers.list_providers()

    @app.post("/api/providers", status_code=201)
    def create_provider(payload: ProviderPayload) -> dict[str, Any]:
        return ctx.providers.create(payload.model_dump())

    @app.get("/api/providers/{provider_id}")
    def get_provider(provider_id: str) -> dict[str, Any]:
        return ctx.providers.get(provider_id)

    @app.put("/api/providers/{provider_id}")
    def update_provider(provider_id: str, payload: ProviderUpdate) -> dict[str, Any]:
        data = {k: v for k, v in payload.model_dump().items() if v is not None}
        return ctx.providers.update(provider_id, data)

    @app.delete("/api/providers/{provider_id}", status_code=204)
    def delete_provider(provider_id: str) -> None:
        ctx.providers.delete(provider_id)

    @app.post("/api/providers/{provider_id}/default")
    def set_default_provider(provider_id: str) -> dict[str, Any]:
        return ctx.providers.set_default(provider_id)

    @app.get("/api/providers/{provider_id}/models")
    def fetch_provider_models(provider_id: str) -> dict[str, Any]:
        """拉服务端模型列表，省得用户手打模型名打错。"""
        return {"models": ctx.providers.fetch_models(provider_id)}

    @app.post("/api/providers/{provider_id}/test")
    def test_provider(provider_id: str, model: str = "") -> dict[str, Any]:
        """连通性自检：发一句最小对话，返回 OK / 失败原因与耗时。"""
        return ctx.providers.test(provider_id, model=model)

    @app.put("/api/characters/{character_id}/model")
    def update_character_model(character_id: str, payload: CharacterModelPayload) -> dict[str, Any]:
        data = {k: v for k, v in payload.model_dump().items() if v is not None}
        ctx.characters.update_model(character_id, data)
        return detail(character_id)["model"]

    @app.get("/api/channels/{binding_id}/logs")
    def channel_logs(binding_id: str, limit: int = 80) -> dict[str, Any]:
        """渠道运行时日志。真机联调时不用盯控制台，界面里就能看。"""
        adapter = ctx.manager.get(binding_id)
        if adapter is None:
            return {"connected": False, "logs": [], "stats": None, "status": "disconnected"}
        return {
            "connected": True,
            "status": adapter.status.value,
            "stats": dict(adapter.stats),
            "logs": adapter.logs(limit),
        }

    # ---------- F5 对话 / P1 A-B ----------

    @app.post("/api/chat")
    def chat(payload: ChatPayload) -> dict[str, Any]:
        outcome = ctx.pipeline.handle(
            character_id=payload.character_id,
            user_message=payload.message,
            session_key=payload.session_key,
            channel_type=payload.channel_type,
            idempotency_key=payload.idempotency_key,
        )
        return outcome.to_dict()

    @app.post("/api/chat/ab")
    def chat_ab(payload: ABPayload) -> dict[str, Any]:
        return ctx.pipeline.compare(
            payload.character_id, payload.message, payload.left_version_id, payload.right_version_id
        )

    # ---------- 运行概览 / 统计（照 AstrBot 的 Dashboard 信息结构） ----------

    @app.get("/api/stats/overview")
    def stats_overview() -> dict[str, Any]:
        """概览卡片：运行时间、平台实例、消息总数、今日模型调用与耗时、CPU/内存。"""
        return ctx.stats.overview()

    @app.get("/api/stats")
    def stats_range(range: str = Query(default="7d", pattern="^(" + "|".join(STATS_RANGES) + ")$")) -> dict[str, Any]:
        """区间统计：消息趋势、平台排名、模型趋势与排名、Token 总量、会话 Top 10。"""
        return ctx.stats.range(range)

    # ---------- F6 用量 ----------

    @app.get("/api/characters/{character_id}/conversations")
    def conversations(character_id: str, limit: int = 50) -> list[dict[str, Any]]:
        ctx.characters.get(character_id)
        return ctx.usage.conversations(character_id, limit)

    @app.get("/api/usage")
    def usage(range: str = Query(default="today", pattern="^(today|7d|30d)$")) -> dict[str, Any]:
        return ctx.usage.summary(range)

    # ---------- 运行时设置：限流 / 去重 ----------

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        """一次取回全部可调参数，供前端设置页初始化。"""
        return ctx.settings.all()

    @app.put("/api/settings/rate_limit")
    def update_rate_limit(payload: RateLimitPayload) -> dict[str, Any]:
        data = {k: v for k, v in payload.model_dump().items() if v is not None}
        return ctx.settings.update("rate_limit", data)

    @app.put("/api/settings/dedup")
    def update_dedup(payload: DedupPayload) -> dict[str, Any]:
        data = {k: v for k, v in payload.model_dump().items() if v is not None}
        return ctx.settings.update("dedup", data)

    @app.post("/api/settings/{key}/reset")
    def reset_settings(key: str) -> dict[str, Any]:
        return ctx.settings.reset(key)

    @app.get("/api/settings/runtime")
    def runtime_status(session_key: str = "console:debug") -> dict[str, Any]:
        """运行态观测：当前会话剩余配额 + 去重表水位。供设置页实时展示。"""
        d = ctx.limiter.peek(session_key)
        return {
            "rate_limit": {
                "session_key": session_key,
                "limit": d.limit,
                "remaining": d.remaining,
                "window_seconds": d.window_seconds,
                "retry_after": round(d.retry_after, 2),
                "strategy": d.strategy,
                "allowed": d.allowed,
                **ctx.limiter.stats(),
            },
            "dedup": ctx.dedup.stats(),
        }

    @app.post("/api/settings/rate_limit/reset_counters")
    def reset_counters(session_key: str | None = None) -> dict[str, Any]:
        """清空限流计数。不传 session_key 则清空全部会话。"""
        return {"cleared_sessions": ctx.limiter.reset(session_key)}

    @app.post("/api/settings/dedup/sweep")
    def sweep_dedup() -> dict[str, Any]:
        """立即执行一次去重表清理，不等清理间隔。"""
        removed = ctx.dedup.sweep()
        return {"removed": removed, "stats": ctx.dedup.stats()}

    # ---------- 前端托管 ----------

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
        # 前端依赖（Tailwind / Alpine）放在 web/vendor 下，断网也能打开界面
        vendor_dir = WEB_DIR / "vendor"
        if vendor_dir.exists():
            app.mount("/vendor", StaticFiles(directory=str(vendor_dir)), name="vendor")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(str(WEB_DIR / "index.html"))

    return app


app = create_app()
