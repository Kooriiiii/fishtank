"""消息管线：一次对话的完整七步流程。

① 去重（幂等键）→ ② 会话路由 → ③ 立即占位响应 → ④ 组装上下文
→ ⑤ 调模型 → ⑥ 更新响应 → ⑦ 落库

第 ① 和 ③ 步是平台硬要求而非优化项：飞书会重复推送事件、要求 3 秒内响应，
企微长连接同样需要先发 finish=false 的占位。调试台走同一条管线，
保证「后台能跑通的，接上真机也能跑通」。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..db import Database, utcnow
from .llm import build_system_prompt, provider_from_row
from .memory import MemoryService
from .persona import PERSONA_FIELDS, PersonaService


def persona_fingerprint(version: dict[str, Any]) -> str:
    """人设内容指纹：只取参与版本快照的字段，不含版本号与时间。

    用于让 Mock 的选句在「内容相同」时稳定复现：
    回滚会生成新的版本号，但内容与目标版本一致，回复也必须一致。
    """
    parts = []
    for field in PERSONA_FIELDS:
        value = version.get(field)
        parts.append("|".join(value) if isinstance(value, list) else str(value or ""))
    parts.append(str(version.get("style_hint") or ""))
    return "¦".join(parts)

# 估算单价（元 / 1K token），仅用于成本观察，不作账单依据
PRICE_PROMPT = 0.001
PRICE_COMPLETION = 0.002


def estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    return round(prompt_tokens / 1000 * PRICE_PROMPT + completion_tokens / 1000 * PRICE_COMPLETION, 6)


class PipelineError(Exception):
    """管线业务异常。"""


@dataclass
class ChatOutcome:
    conversation_id: str
    reply: str
    persona_version: dict[str, Any]
    hit_memories: list[dict[str, Any]]
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    duplicated: bool = False
    warning: str = ""
    rate_limit: dict[str, Any] | None = None
    model: str = ""
    provider_name: str = ""
    context_rounds: int = 0
    system_prompt: str = field(default="", repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "reply": self.reply,
            "persona_version": {
                "id": self.persona_version.get("id"),
                "version_no": self.persona_version.get("version_no"),
                "speech_style": self.persona_version.get("speech_style"),
                "note": self.persona_version.get("note"),
            },
            "hit_memories": [
                {
                    "id": m["id"],
                    "content": m["content"],
                    "importance": m["importance"],
                    "pinned": m.get("pinned", False),
                    "reason": m.get("reason", ""),
                }
                for m in self.hit_memories
            ],
            "model": {"name": self.model, "provider": self.provider_name},
            "context_rounds": self.context_rounds,
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "cost_estimate": estimate_cost(self.prompt_tokens, self.completion_tokens),
            },
            "latency_ms": self.latency_ms,
            "duplicated": self.duplicated,
            "warning": self.warning,
            "rate_limit": self.rate_limit,
        }


class MessagePipeline:
    def __init__(
        self,
        db: Database,
        persona: PersonaService,
        memory: MemoryService,
        provider: Any,
        dedup: Any = None,
        limiter: Any = None,
        providers: Any = None,
    ) -> None:
        self.db = db
        self.persona = persona
        self.memory = memory
        self.provider = provider      # 兜底 Provider（离线 Mock）
        self.providers = providers    # ProviderService：按角色取真实模型服务
        self.dedup = dedup
        self.limiter = limiter

    # ---------- 模型路由 ----------

    def resolve_provider(self, character: dict[str, Any]) -> tuple[Any, str, str]:
        """按「角色绑定 > 全局默认 > 离线 Mock」的顺序取模型服务。

        返回 (provider, 模型名, 服务名)。任何读取失败都退回 Mock，
        机器人不能因为配置文件坏了一个字段就彻底不说话。
        """
        fallback_label = getattr(self.provider, "label", "离线 Mock")
        fallback_model = getattr(self.provider, "model", "mock")
        if self.providers is None:
            return self.provider, fallback_model, fallback_label
        pid = str(character.get("model_provider_id") or "")
        try:
            row = self.providers.get_row(pid) if pid else self.providers.default_row()
            if row is None:
                return self.provider, fallback_model, fallback_label
            key = self.providers.api_key(row["id"])
            return provider_from_row(row, key, fallback=self.provider), str(row["model"]), str(row["name"])
        except Exception:  # noqa: BLE001 — 配置损坏时降级而不是报错
            return self.provider, fallback_model, fallback_label

    def history_for(self, character_id: str, session_key: str, rounds: int) -> list[dict[str, str]]:
        """按会话键取最近 N 轮问答，作为多轮上下文。

        上下文按 session_key 隔离：同一个机器人被不同人私聊、或在群里被 @，
        各自有各自的记忆线索，不会串台。
        """
        if rounds <= 0:
            return []
        rows = self.db.query(
            """SELECT user_message, assistant_message FROM conversation
                WHERE character_id = ? AND session_key = ? AND assistant_message != ''
                ORDER BY created_at DESC LIMIT ?""",
            (character_id, session_key, int(rounds)),
        )
        history: list[dict[str, str]] = []
        for row in reversed(rows):
            history.append({"role": "user", "content": row["user_message"]})
            history.append({"role": "assistant", "content": row["assistant_message"]})
        return history

    # ---------- ① 去重 ----------

    def is_duplicate(self, idempotency_key: str, channel_type: str) -> bool:
        """幂等检查。靠主键冲突判定，避免「先查后写」的竞态窗口。

        去重与 TTL 清理统一由 DedupService 承担；未注入时降级为直接写入，
        保证管线在被单独实例化（如测试）时仍可工作。
        """
        if not idempotency_key:
            return False
        if self.dedup is not None:
            return self.dedup.check_and_mark(idempotency_key, channel_type)
        try:
            with self.db.write() as conn:
                conn.execute(
                    "INSERT INTO processed_event (idempotency_key, channel_type, processed_at) VALUES (?,?,?)",
                    (idempotency_key, channel_type, utcnow()),
                )
            return False
        except Exception as exc:  # sqlite3.IntegrityError 及其子类
            if "UNIQUE" in str(exc) or "PRIMARY KEY" in str(exc):
                return True
            raise

    def _record_failure(
        self,
        character_id: str,
        channel_type: str,
        session_key: str,
        user_message: str,
        version_id: str | None,
        latency_ms: int,
    ) -> None:
        """把一次失败的调用写进 conversation（status=error），供统计页算成功率。

        失败记录不写 assistant_message，所以多轮上下文（按 assistant_message 过滤）
        不会被这些空回复污染。
        """
        try:
            with self.db.write() as conn:
                conn.execute(
                    """INSERT INTO conversation
                       (id, character_id, channel_type, session_key, user_message, assistant_message,
                        persona_version_id, hit_memory_ids, model, prompt_tokens, completion_tokens,
                        latency_ms, status, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"conv_{uuid.uuid4().hex[:12]}",
                        character_id,
                        channel_type,
                        session_key,
                        user_message,
                        "",
                        version_id,
                        "[]",
                        "",
                        0,
                        0,
                        latency_ms,
                        "error",
                        utcnow(),
                    ),
                )
        except Exception:  # noqa: BLE001 — 记失败本身不能掩盖原始错误
            pass

    # ---------- 主流程 ----------
    def handle(
        self,
        character_id: str,
        user_message: str,
        session_key: str = "console:debug",
        channel_type: str = "mock",
        idempotency_key: str = "",
        version_id: str | None = None,
        persist: bool = True,
    ) -> ChatOutcome:
        started = time.perf_counter()
        user_message = (user_message or "").strip()
        if not user_message:
            raise PipelineError("消息内容不能为空")
        if len(user_message) > 2000:
            raise PipelineError("消息过长，上限 2000 字")

        # ① 去重
        if self.is_duplicate(idempotency_key, channel_type):
            return ChatOutcome(
                conversation_id="",
                reply="",
                persona_version={},
                hit_memories=[],
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=0,
                duplicated=True,
                warning="重复事件已忽略（幂等键命中）",
            )

        # ①.5 限流（必须排在去重之后：重复事件不应消耗配额）
        rate_limit = None
        if self.limiter is not None and persist:
            # A/B 对比等不落库的内部调用不计入配额，避免一次操作扣两次
            decision = self.limiter.check(session_key)
            rate_limit = {
                "limit": decision.limit,
                "remaining": decision.remaining,
                "window_seconds": decision.window_seconds,
                "stalled_seconds": round(decision.stalled, 3),
            }

        # ② 会话路由
        row = self.db.query_one("SELECT * FROM character WHERE id = ?", (character_id,))
        if row is None:
            raise PipelineError(f"角色不存在: {character_id}")
        character = dict(row)

        # ③ 占位响应由适配器负责（见 ChannelAdapter.send_placeholder）；
        #    调试台走 HTTP 同步返回，由前端先渲染打字动画。

        # ④ 组装上下文
        version = (
            self.persona.get_version(version_id) if version_id else self.persona.get_active(character_id)
        )
        if version is None:
            raise PipelineError("该角色尚无人设版本，请先保存一次人设")
        if version["character_id"] != character_id:
            raise PipelineError("指定的人设版本不属于该角色")

        hits = self.memory.retrieve(character_id, user_message)
        system_prompt = build_system_prompt(character, version, hits)

        rounds = int(character.get("context_rounds") or 0)
        history = self.history_for(character_id, session_key, rounds) if persist else []
        provider, model_name, provider_name = self.resolve_provider(character)

        # ⑤ 调模型
        result = provider.chat(
            system_prompt=system_prompt,
            user_message=user_message,
            character_id=character_id,
            memories=hits,
            style_hint=version.get("style_hint", ""),
            version_key=persona_fingerprint(version),
            history=history,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        if result.error and not result.reply:
            # 失败也要留痕：统计页的「成功率」需要真实分母，
            # 否则出错的对话根本不存在，成功率永远是 100%。
            if persist:
                self._record_failure(
                    character_id=character_id,
                    channel_type=channel_type,
                    session_key=session_key,
                    user_message=user_message,
                    version_id=version["id"],
                    latency_ms=latency_ms,
                )
            raise PipelineError(result.error)

        # ⑥⑦ 更新响应并落库
        conversation_id = f"conv_{uuid.uuid4().hex[:12]}"
        if persist:
            with self.db.write() as conn:
                conn.execute(
                    """INSERT INTO conversation
                       (id, character_id, channel_type, session_key, user_message, assistant_message,
                        persona_version_id, hit_memory_ids, model, prompt_tokens, completion_tokens,
                        latency_ms, status, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        conversation_id,
                        character_id,
                        channel_type,
                        session_key,
                        user_message,
                        result.reply,
                        version["id"],
                        json.dumps([m["id"] for m in hits], ensure_ascii=False),
                        result.model,
                        result.prompt_tokens,
                        result.completion_tokens,
                        latency_ms,
                        "done",
                        utcnow(),
                    ),
                )

        return ChatOutcome(
            conversation_id=conversation_id if persist else "",
            reply=result.reply,
            persona_version=version,
            hit_memories=hits,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=latency_ms,
            warning=result.error,
            rate_limit=rate_limit,
            model=result.model,
            provider_name=result.provider_name or provider_name,
            context_rounds=rounds if persist else 0,
            system_prompt=system_prompt,
        )

    # ---------- P1 加分项：人设 A/B 对比 ----------

    def compare(
        self, character_id: str, user_message: str, left_version_id: str, right_version_id: str
    ) -> dict[str, Any]:
        """同一句输入跑两个人设版本。A/B 结果不落库，避免污染用量统计。"""
        if left_version_id == right_version_id:
            raise PipelineError("请选择两个不同的人设版本")
        out = {}
        for side, vid in (("left", left_version_id), ("right", right_version_id)):
            outcome = self.handle(
                character_id=character_id,
                user_message=user_message,
                session_key=f"console:ab:{side}",
                channel_type="mock",
                version_id=vid,
                persist=False,
            )
            payload = outcome.to_dict()
            payload["version_no"] = outcome.persona_version.get("version_no")
            payload["note"] = outcome.persona_version.get("note", "")
            out[side] = payload
        return out
