"""LLM 抽象层：Prompt 组装 + 两种 Provider。

- **MockProvider**：零依赖、确定性、断网可用。没有配置模型服务时兜底，
  保证「刚装好还没填 Key」也能把整套流程走通。
- **OpenAICompatProvider**：任意 OpenAI 兼容接口（DeepSeek / OpenAI / Ollama /
  硅基流动 / one-api …），由「模型服务」页配置，按 id 现读，改完即时生效。

Prompt 组装有两条路径：
1. 人格自由文本（`persona_version.system_prompt` 非空）——AstrBot 的「人格」写法，
   整段由用户自己写，系统只追加记忆与三条硬约束；
2. 结构化模板（性格/背景/风格/口头禅/禁忌）——默认路径，适合不想写提示词的人。
两条路径共享同一条防线：**记忆必须显式列出，禁止编造共同经历**。
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

PROMPT_TEMPLATE = """你是 {name}。

【核心设定】{one_liner}
【性格】{personality}
【背景】{backstory}
【说话风格】{speech_style}
【口头禅】适度使用，不要每句都带：{catchphrases}

【你记得的事】
{memories}

【禁忌】以下话题自然回避，不要生硬拒绝：{taboos}

{rules}"""

RULES = """规则：
1. 始终保持人设，不要自称 AI、模型或助手。
2. 只使用【你记得的事】中列出的信息，不得编造与用户的共同经历。
3. 回复控制在 3 句话以内，口语化。"""

# 人格自由文本路径：用户自己写人设，系统只补记忆与硬约束。
FREE_TEXT_TEMPLATE = """{persona}

【你记得的事】
{memories}

{rules}"""

EMPTY_MEMORY_HINT = "- （暂无长期记忆。不要假装记得任何与用户的过往，也不要编造共同经历。）"


def build_system_prompt(
    character: dict[str, Any],
    version: dict[str, Any],
    memories: list[dict[str, Any]],
) -> str:
    """把角色、人设版本、命中记忆拼装成 system prompt。

    `version["system_prompt"]` 非空时走人格自由文本路径：用户写的整段人格
    原样保留，只在后面追加记忆与三条硬约束（这两块是产品防线，不允许被覆盖）。
    """
    if memories:
        mem_text = "\n".join(
            f"- {m['content']}（重要度 {m['importance']}{'，置顶' if m.get('pinned') else ''}）"
            for m in memories
        )
    else:
        mem_text = EMPTY_MEMORY_HINT

    free_text = str(version.get("system_prompt") or "").strip()
    if free_text:
        return FREE_TEXT_TEMPLATE.format(persona=free_text, memories=mem_text, rules=RULES)

    def join(values: Any, empty: str) -> str:
        items = [str(v).strip() for v in (values or []) if str(v).strip()]
        return "、".join(items) if items else empty

    return PROMPT_TEMPLATE.format(
        name=character.get("name", "未命名角色"),
        one_liner=character.get("one_liner") or "（未填写）",
        personality=join(version.get("personality_tags"), "（未设定）"),
        backstory=version.get("backstory") or "（未填写）",
        speech_style=version.get("speech_style") or "（未设定）",
        catchphrases=join(version.get("catchphrases"), "（无）"),
        memories=mem_text,
        taboos=join(version.get("taboo_topics"), "（无）"),
        rules=RULES,
    )


def build_messages(
    system_prompt: str,
    user_message: str,
    history: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """组装 OpenAI 格式的 messages：system + 历史 + 本轮用户消息。

    history 由调用方从 conversation 表按 session_key 取出，已按时间正序。
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for item in history or []:
        role = "assistant" if item.get("role") == "assistant" else "user"
        content = str(item.get("content") or "").strip()
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})
    return messages


def estimate_tokens(text: str) -> int:
    """粗估 token 数：中文约 1 字 1 token，英文约 4 字符 1 token。

    仅用于成本观察，不作账单依据。真实调用时会被 provider 返回的用量覆盖。
    """
    text = text or ""
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - han
    return max(1, han + other // 4)


@dataclass
class LLMResult:
    reply: str
    prompt_tokens: int
    completion_tokens: int
    model: str = "mock"
    error: str = ""
    provider_name: str = "离线 Mock"


def _as_second_person(memories: list[dict[str, Any]]) -> str:
    """把记忆内容转成可嵌入回复的第二人称短句。

    记忆统一以「用户…」书写（便于管理与审阅），回复里则是直接对用户说话。
    转换需同时避免两种病句：
      - 不转换  -> 「知道你用户是夜猫子」（主语重复）
      - 只删前缀 -> 「又到这个点啦。是夜猫子」（丢主语）
    因此统一替换为「你」，再截取首个分句，避免长串事实塞进口语回复。
    """
    if not memories:
        return "你跟我说过的那些事"
    text = memories[0]["content"]
    if text.startswith("用户的"):
        text = "你的" + text[3:]
    elif text.startswith("用户"):
        text = "你" + text[2:]
    # 只取第一个分句，避免把一长串事实塞进一句口语化回复
    return re.split(r"[，。；、]", text)[0]


@dataclass
class MockProvider:
    """确定性 Mock 模型。

    按 character key 从 seed 的 mock_replies 取回复，支持关键词触发与
    {memory} 占位符替换；同时按人设版本的 style_hint 做轻量语气变换，
    让「改人设 / 回滚版本」在离线演示下也能产生可感知的差异。
    """

    replies: dict[str, Any] = field(default_factory=dict)
    name: str = "mock"
    label: str = "离线 Mock"
    model: str = "mock"

    def register(self, character_id: str, mock_replies: dict[str, Any]) -> None:
        self.replies[character_id] = mock_replies or {}

    @staticmethod
    def _apply_style(text: str, hint: str) -> str:
        """按版本风格提示做确定性的轻量变换，不依赖外部模型。

        「简短」做两级压缩：先按句切，若首句即原文（本来就只有一句），
        再按分句标点切。不做两级的话，单句回复的风格切换会是恒等变换，
        演示时「改人设 / 回滚」看不出任何差别。
        """
        if not hint:
            return text
        if "简短" in hint or "少说" in hint:
            parts = [p for p in re.split(r"(?<=[。！？~…])", text) if p.strip()]
            short = parts[0] if parts else text
            if short == text:
                clauses = [c for c in re.split(r"(?<=[，、；])", text) if c.strip()]
                if len(clauses) > 1:
                    short = clauses[0].rstrip("，、；") + text[-1] if text[-1] in "。！？~…" else clauses[0].rstrip("，、；") + "。"
            return short
        if "叠词" in hint:
            return text.replace("好", "好好").replace("慢", "慢慢").replace("。", "呀。", 1)
        if "追问" in hint and not text.rstrip().endswith("？"):
            return text.rstrip("。 ") + "。具体是卡在哪一步？"
        if "毒舌" in hint or "犀利" in hint:
            return text.rstrip("。 ") + "。行吧，看在你可怜的份上。"
        if "留白" in hint or "平静" in hint:
            # 平静留白：去掉软化语气词，保持克制
            return text.replace("呀", "").replace("啦", "").replace("呢", "")
        return text

    def chat(
        self,
        system_prompt: str,
        user_message: str,
        character_id: str = "",
        memories: list[dict[str, Any]] | None = None,
        style_hint: str = "",
        seed: int | None = None,
        version_key: str = "",
        history: list[dict[str, str]] | None = None,
    ) -> LLMResult:
        bank = self.replies.get(character_id) or {}
        pool: list[str] = []
        lowered = (user_message or "").lower()
        for group in bank.get("keywords", []) or []:
            if any(str(k).lower() in lowered for k in group.get("match", [])):
                pool = list(group.get("replies") or [])
                break
        if not pool:
            pool = list(bank.get("default") or [])
        if not pool:
            pool = ["（这条角色还没有配置离线回复，请到「模型服务」页接入真实 API。）"]

        # 固定随机源，保证同样输入得到同样输出，便于回归测试与演示复现。
        # 不能用内置 hash()：Python 对 str 的哈希带进程级随机盐，
        # 重启后同一句话会选到不同回复，「确定性回放」会失效。
        #
        # version_key 必须是「人设内容指纹」而不是版本号：
        #   - 用版本号：回滚会生成新版本号，内容相同却选到不同回复，
        #     违背「回滚即恢复原状」的语义；
        #   - 不带版本：不同人设版本选到同一句，A/B 对比左右两栏一样。
        if seed is None:
            digest = hashlib.sha256(
                f"{character_id}|{version_key}|{user_message}".encode("utf-8")
            ).digest()
            seed = int.from_bytes(digest[:8], "big")
        rng = random.Random(seed)
        reply = rng.choice(pool)

        mems = memories or []
        if "{memory}" in reply:
            reply = reply.replace("{memory}", _as_second_person(mems))
        reply = self._apply_style(reply, style_hint)

        return LLMResult(
            reply=reply,
            prompt_tokens=estimate_tokens(system_prompt) + estimate_tokens(user_message),
            completion_tokens=estimate_tokens(reply),
            model="mock",
            provider_name=self.label,
        )


@dataclass
class OpenAICompatProvider:
    """OpenAI 兼容接口。

    所有异常都转成 `LLMResult.error` 而不抛出：QQ / 企微那边必须回一句人话，
    不能让用户看到「连接已断开」。调用方据 error 决定是否降级到 Mock。
    """

    api_key: str
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    temperature: float = 0.7
    max_tokens: int = 1000
    timeout: float = 60.0
    fallback: MockProvider | None = None
    name: str = "openai_chat_completion"
    label: str = "OpenAI 兼容"

    def chat(
        self,
        system_prompt: str,
        user_message: str,
        character_id: str = "",
        memories: list[dict[str, Any]] | None = None,
        style_hint: str = "",
        seed: int | None = None,
        version_key: str = "",
        history: list[dict[str, str]] | None = None,
    ) -> LLMResult:
        messages = build_messages(system_prompt, user_message, history)
        try:
            resp = httpx.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                json={
                    "model": self.model,
                    "temperature": self.temperature,
                    "max_tokens": self.max_tokens,
                    "messages": messages,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            reply = (data["choices"][0]["message"]["content"] or "").strip()
            usage = data.get("usage") or {}
            return LLMResult(
                reply=reply,
                prompt_tokens=int(usage.get("prompt_tokens") or estimate_tokens(system_prompt)),
                completion_tokens=int(usage.get("completion_tokens") or estimate_tokens(reply)),
                model=str(data.get("model") or self.model),
                provider_name=self.label,
            )
        except Exception as exc:  # noqa: BLE001 — 任何异常都必须降级，机器人不能哑掉
            if self.fallback is None:
                return LLMResult("", 0, 0, self.model, error=f"模型调用失败: {exc}")
            result = self.fallback.chat(
                system_prompt, user_message, character_id, memories, style_hint, seed, version_key
            )
            # 不把异常细节（可能含 URL/密钥片段）回传前端，只标注降级事实
            result.error = "真实模型调用失败，已降级到离线 Mock 回复"
            return result


def provider_from_row(
    row: Any, api_key: str, fallback: MockProvider | None = None
) -> OpenAICompatProvider:
    """数据库行 + 已解密的 Key -> Provider 实例。"""
    return OpenAICompatProvider(
        api_key=api_key,
        base_url=str(row["base_url"]),
        model=str(row["model"]),
        temperature=float(row["temperature"]),
        max_tokens=int(row["max_tokens"]),
        fallback=fallback,
        label=str(row["name"]),
    )
