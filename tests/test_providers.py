"""模型服务 / 多轮上下文 / 渠道日志的回归测试。

重点守住三件事：
1. Key 只加密落库、接口只回脱敏串，前端回传脱敏串不会把真实 Key 写坏；
2. 对话真的走「角色绑定 > 全局默认」的模型服务（用假的 httpx 验证请求与回复）；
3. 多轮上下文真的进了 messages（第二轮能看到第一轮问答）。
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import create_app  # noqa: E402
from app.services import llm as llm_module  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    db_file = tmp_path / f"test_{uuid.uuid4().hex}.db"
    os.environ["FISHCONSOLE_KEY_FILE"] = str(tmp_path / ".key")
    app = create_app(db_path=str(db_file), do_seed=True, autostart=False)
    with TestClient(app) as c:
        yield c


def first_character(client) -> dict:
    return client.get("/api/characters").json()[0]


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)  # type: ignore[arg-type]

    def json(self) -> dict:
        return self._payload


def fake_chat_completion(calls: list[dict], reply: str = "模型回复"):
    """造一个 httpx.post 替身，把请求体记下来供断言。"""

    def _post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers or {}, "json": json or {}})
        return FakeResponse({
            "model": (json or {}).get("model", "fake-model"),
            "choices": [{"message": {"content": reply}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7},
        })

    return _post


# ---------- 模型服务 CRUD ----------

def test_provider_crud_and_masking(client):
    secret = "sk-REALSECRET-1234567890"
    created = client.post("/api/providers", json={
        "name": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": secret,
        "model": "deepseek-chat",
        "temperature": 0.6,
        "max_tokens": 800,
    })
    assert created.status_code == 201
    body = created.json()
    assert secret not in str(body), "接口绝不能回明文 Key"
    assert "****" in body["api_key_mask"]
    assert body["is_default"] is True, "第一个模型服务自动成为默认"

    # 更新时不传 api_key：沿用原 Key
    updated = client.put(f"/api/providers/{body['id']}", json={"temperature": 1.1}).json()
    assert updated["temperature"] == 1.1

    # 回传脱敏串：同样不能覆盖真实 Key
    client.put(f"/api/providers/{body['id']}", json={"api_key": body["api_key_mask"]})
    # 通过内部服务读取明文，确认 Key 没被掩码覆盖
    assert client.app.state.ctx.providers.api_key(body["id"]) == secret

    # 新建第二个并设为默认
    second = client.post("/api/providers", json={
        "name": "本地 Ollama", "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "ollama", "model": "qwen2.5:7b",
    }).json()
    assert second["is_default"] is False
    client.post(f"/api/providers/{second['id']}/default")
    providers = client.get("/api/providers").json()
    assert [p["id"] for p in providers if p["is_default"]] == [second["id"]]

    # 删除默认后自动退回另一个
    assert client.delete(f"/api/providers/{second['id']}").status_code == 204
    left = client.get("/api/providers").json()
    assert [p["id"] for p in left] == [body["id"]]
    assert left[0]["is_default"] is True


def test_provider_validation(client):
    bad_key = client.post("/api/providers", json={
        "name": "空的", "base_url": "https://x/v1", "api_key": "", "model": "m",
    })
    assert bad_key.status_code == 400 and "API Key" in bad_key.json()["detail"]

    bad_url = client.post("/api/providers", json={
        "name": "地址错", "base_url": "api.x.com", "api_key": "k", "model": "m",
    })
    assert bad_url.status_code == 400

    bad_temp = client.post("/api/providers", json={
        "name": "温度错", "base_url": "https://x/v1", "api_key": "k", "model": "m",
        "temperature": 9,
    })
    assert bad_temp.status_code == 400

    assert client.post("/api/providers", json={
        "name": "类型错", "provider_type": "nope", "base_url": "https://x/v1",
        "api_key": "k", "model": "m",
    }).status_code == 400


def test_provider_presets(client):
    body = client.get("/api/providers/presets").json()
    keys = [p["key"] for p in body["presets"]]
    assert "deepseek" in keys and "ollama" in keys
    assert body["types"][0]["value"] == "openai_chat_completion"


def test_provider_test_endpoint(client, monkeypatch):
    created = client.post("/api/providers", json={
        "name": "自检用", "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-x", "model": "deepseek-chat",
    }).json()

    calls: list[dict] = []
    monkeypatch.setattr(llm_module.httpx, "post", fake_chat_completion(calls, reply="OK"))
    ok = client.post(f"/api/providers/{created['id']}/test").json()
    assert ok["ok"] is True and ok["reply"] == "OK"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-x"

    # 失败要回诊断信息而不是 500
    def boom(*_a, **_k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(llm_module.httpx, "post", boom)
    bad = client.post(f"/api/providers/{created['id']}/test").json()
    assert bad["ok"] is False and "connection reset" in bad["error"]


# ---------- 机器人绑定模型 ----------

def test_character_model_binding(client):
    cid = first_character(client)["id"]
    assert client.get(f"/api/characters/{cid}").json()["model"]["is_fallback"] is True

    provider = client.post("/api/providers", json={
        "name": "主模型", "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-y", "model": "deepseek-chat",
    }).json()

    out = client.put(f"/api/characters/{cid}/model",
                     json={"model_provider_id": provider["id"], "context_rounds": 4}).json()
    assert out["provider_id"] == provider["id"]
    assert out["model"] == "deepseek-chat"
    assert out["is_fallback"] is False
    assert out["context_rounds"] == 4

    # 不存在的模型服务要拒绝，而不是悄悄写成悬空引用
    assert client.put(f"/api/characters/{cid}/model",
                      json={"model_provider_id": "prov_nope"}).status_code == 400

    # 轮数越界由 pydantic 拦
    assert client.put(f"/api/characters/{cid}/model",
                      json={"context_rounds": 999}).status_code == 422


# ---------- 真的走模型服务 + 多轮上下文 ----------

def test_chat_uses_configured_provider_with_history(client, monkeypatch):
    cid = first_character(client)["id"]
    provider = client.post("/api/providers", json={
        "name": "假模型", "base_url": "https://fake.local/v1",
        "api_key": "sk-fake", "model": "fake-model", "temperature": 0.3,
    }).json()
    client.put(f"/api/characters/{cid}/model",
               json={"model_provider_id": provider["id"], "context_rounds": 5})

    calls: list[dict] = []
    monkeypatch.setattr(llm_module.httpx, "post", fake_chat_completion(calls, reply="第一次回复"))

    first = client.post("/api/chat", json={
        "character_id": cid, "message": "我叫阿泽", "session_key": "qq:private:u1",
    }).json()
    assert first["reply"] == "第一次回复"
    assert first["model"]["name"] == "fake-model"
    assert first["model"]["provider"] == "假模型"
    payload = calls[0]["json"]
    assert payload["model"] == "fake-model"
    assert payload["temperature"] == 0.3
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][-1] == {"role": "user", "content": "我叫阿泽"}

    # 第二轮必须带上第一轮问答
    monkeypatch.setattr(llm_module.httpx, "post", fake_chat_completion(calls, reply="第二次回复"))
    second = client.post("/api/chat", json={
        "character_id": cid, "message": "我叫什么？", "session_key": "qq:private:u1",
    }).json()
    assert second["reply"] == "第二次回复"
    history = calls[-1]["json"]["messages"]
    assert {"role": "user", "content": "我叫阿泽"} in history
    assert {"role": "assistant", "content": "第一次回复"} in history

    # 换一个会话不该串台
    monkeypatch.setattr(llm_module.httpx, "post", fake_chat_completion(calls, reply="新会话"))
    client.post("/api/chat", json={
        "character_id": cid, "message": "刚刚聊了什么", "session_key": "qq:private:u2",
    })
    fresh = calls[-1]["json"]["messages"]
    assert len(fresh) == 2, "新会话只应带 system + 本轮用户消息"


def test_context_rounds_zero_disables_history(client, monkeypatch):
    cid = first_character(client)["id"]
    provider = client.post("/api/providers", json={
        "name": "假模型", "base_url": "https://fake.local/v1",
        "api_key": "sk-fake", "model": "fake-model",
    }).json()
    client.put(f"/api/characters/{cid}/model",
               json={"model_provider_id": provider["id"], "context_rounds": 0})

    calls: list[dict] = []
    monkeypatch.setattr(llm_module.httpx, "post", fake_chat_completion(calls, reply="好"))
    for msg in ("第一句", "第二句"):
        client.post("/api/chat", json={"character_id": cid, "message": msg, "session_key": "s"})
        client.put(f"/api/characters/{cid}/model", json={"context_rounds": 0})
    assert len(calls[-1]["json"]["messages"]) == 2


def test_persona_free_text_overrides_template(client):
    cid = first_character(client)["id"]
    version = client.post(f"/api/characters/{cid}/versions", json={
        "system_prompt": "你是阿泽养的一条咸鱼，说话三句不离躺平。",
        "note": "改成自由文本人格",
    }).json()
    assert "躺平" in (version["system_prompt"] or "")
    active = client.get(f"/api/characters/{cid}").json()["active_version"]
    assert "躺平" in active["system_prompt"]

    from app.services.llm import build_system_prompt

    prompt = build_system_prompt(
        {"name": "咸鱼酱", "one_liner": "x"},
        {"system_prompt": "你是阿泽养的一条咸鱼，说话三句不离躺平。"},
        [{"content": "用户是夜猫子", "importance": 5, "pinned": True}],
    )
    assert prompt.startswith("你是阿泽养的一条咸鱼")
    assert "用户是夜猫子" in prompt
    assert "不得编造" in prompt, "自由文本人格也不能把防编造规则丢掉"


# ---------- 渠道日志 ----------

def test_channel_logs_endpoint(client):
    cid = first_character(client)["id"]
    binding = client.post(f"/api/characters/{cid}/channels",
                          json={"channel_type": "mock", "config": {"label": "测试"}}).json()
    empty = client.get(f"/api/channels/{binding['id']}/logs").json()
    assert empty["connected"] is False

    client.post(f"/api/channels/{binding['id']}/connect")
    live = client.get(f"/api/channels/{binding['id']}/logs").json()
    assert live["connected"] is True
    assert live["status"] in ("running", "connecting")
    assert isinstance(live["logs"], list)
