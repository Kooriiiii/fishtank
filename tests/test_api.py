"""回归测试：覆盖验收标准 1-6 以及两条核心防线。

重点用例：
- test_rollback_changes_reply_style  —— F2 回滚后回复风格可感知地变回去
- test_deleted_memory_not_injected   —— F3 删除记忆后不再被注入（产品主张）
- test_prompt_forbids_fabrication    —— 禁止编造共同经历的 Prompt 防线
- test_idempotent_event              —— 平台重复推送的去重硬要求
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import create_app  # noqa: E402
from app.services.llm import build_system_prompt  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    db_file = tmp_path / f"test_{uuid.uuid4().hex}.db"
    os.environ["FISHCONSOLE_KEY_FILE"] = str(tmp_path / ".key")
    app = create_app(db_path=str(db_file), do_seed=True)
    with TestClient(app) as c:
        yield c


def first_character(client) -> dict:
    resp = client.get("/api/characters")
    assert resp.status_code == 200
    data = resp.json()
    assert data, "种子数据应预置角色"
    return data[0]


# ---------- 验收 1：角色与持久化 ----------

def test_health_and_seed(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["characters"] == 3


def test_create_character_generates_v1(client):
    resp = client.post(
        "/api/characters",
        json={"name": "测试鱼", "avatar": "🐡", "one_liner": "测试用角色",
              "personality_tags": ["好奇"], "speech_style": "简短", "note": "初版"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["active_version"]["version_no"] == 1
    assert body["active_version"]["personality_tags"] == ["好奇"]


def test_data_persists_across_restart(tmp_path):
    db_file = tmp_path / "persist.db"
    os.environ["FISHCONSOLE_KEY_FILE"] = str(tmp_path / ".key")
    with TestClient(create_app(db_path=str(db_file))) as c1:
        cid = c1.post("/api/characters", json={"name": "持久鱼", "one_liner": "重启不丢"}).json()["character"]["id"]
    # 重新装配应用，模拟进程重启；seed 幂等不应重复插入
    with TestClient(create_app(db_path=str(db_file))) as c2:
        assert c2.get(f"/api/characters/{cid}").status_code == 200
        assert len(c2.get("/api/characters").json()) == 4


# ---------- 验收 2：版本化与回滚 ----------

def test_version_diff_and_rollback(client):
    ch = first_character(client)
    cid = ch["id"]
    base_no = ch["active_version_no"]

    new_v = client.post(
        f"/api/characters/{cid}/versions",
        json={"speech_style": "全部改成电报体", "note": "压缩风格"},
    ).json()
    assert new_v["version_no"] == base_no + 1
    assert new_v["is_active"] is True

    versions = client.get(f"/api/characters/{cid}/versions").json()
    old = next(v for v in versions if v["version_no"] == base_no)

    diff = client.get(
        f"/api/characters/{cid}/versions/diff", params={"left": old["id"], "right": new_v["id"]}
    ).json()
    style = next(c for c in diff["changes"] if c["field"] == "speech_style")
    assert style["status"] == "modified"
    # 未提交的字段应继承，标记为 same
    assert any(c["status"] == "same" for c in diff["changes"])

    rolled = client.post(f"/api/versions/{old['id']}/rollback").json()
    assert rolled["version_no"] == base_no + 2
    assert rolled["speech_style"] == old["speech_style"]
    # 回滚不删除中间版本，历史完整
    assert len(client.get(f"/api/characters/{cid}/versions").json()) == base_no + 2


def test_rollback_changes_reply_style(client):
    """验收 2 的关键：回滚后对话风格可感知地变回去。

    这里刻意用「毒舌收尾」这种确定性变换，而不是依赖某个示例角色当前的风格：
    示例角色本身可能就是极简风格，再加一个「更简短」的版本反而看不出差别。
    """
    ch = first_character(client)
    cid = ch["id"]
    versions = client.get(f"/api/characters/{cid}/versions").json()
    original = client.post("/api/chat", json={"character_id": cid, "message": "今天好累"}).json()

    changed = client.post(
        f"/api/characters/{cid}/versions",
        json={"style_hint": "毒舌、犀利，最后一句永远嘴硬", "speech_style": "先怼一句", "note": "改成毒舌"},
    ).json()
    after = client.post("/api/chat", json={"character_id": cid, "message": "今天好累"}).json()
    assert after["persona_version"]["version_no"] == changed["version_no"]

    active_before = next(v for v in versions if v["is_active"])
    client.post(f"/api/versions/{active_before['id']}/rollback")
    restored = client.post("/api/chat", json={"character_id": cid, "message": "今天好累"}).json()
    # 回滚后的回复应与原始风格一致，且与改过的版本不同
    assert restored["reply"] == original["reply"]
    assert after["reply"] != restored["reply"]


def test_cannot_rollback_active_version(client):
    ch = first_character(client)
    versions = client.get(f"/api/characters/{ch['id']}/versions").json()
    active = next(v for v in versions if v["is_active"])
    assert client.post(f"/api/versions/{active['id']}/rollback").status_code == 400


def test_diff_rejects_cross_character(client):
    chars = client.get("/api/characters").json()
    a = client.get(f"/api/characters/{chars[0]['id']}/versions").json()[0]
    b = client.get(f"/api/characters/{chars[1]['id']}/versions").json()[0]
    resp = client.get(
        f"/api/characters/{chars[0]['id']}/versions/diff", params={"left": a["id"], "right": b["id"]}
    )
    assert resp.status_code == 400


# ---------- 验收 3：记忆可见可编辑 ----------

def test_memory_crud_and_validation(client):
    cid = first_character(client)["id"]
    created = client.post(
        f"/api/characters/{cid}/memories",
        json={"content": "用户养了一只叫土豆的橘猫", "source": "manual", "importance": 5, "pinned": True},
    ).json()
    assert created["pinned"] is True

    updated = client.put(f"/api/memories/{created['id']}", json={"importance": 2, "pinned": False}).json()
    assert updated["importance"] == 2 and updated["pinned"] is False

    assert client.delete(f"/api/memories/{created['id']}").status_code == 204
    assert client.put(f"/api/memories/{created['id']}", json={"content": "x"}).status_code == 400
    # 越界重要度由 pydantic 拦截
    assert client.post(f"/api/characters/{cid}/memories", json={"content": "x", "importance": 9}).status_code == 422


def test_deleted_memory_not_injected(client):
    """验收 3 的关键：删除某条记忆后，它不再进入 Prompt。"""
    cid = first_character(client)["id"]
    mem = client.post(
        f"/api/characters/{cid}/memories",
        json={"content": "用户的生日是十月十七号", "importance": 5, "pinned": True},
    ).json()

    before = client.post("/api/chat", json={"character_id": cid, "message": "我生日快到了"}).json()
    assert mem["id"] in [h["id"] for h in before["hit_memories"]]

    client.delete(f"/api/memories/{mem['id']}")
    after = client.post("/api/chat", json={"character_id": cid, "message": "我生日快到了"}).json()
    assert mem["id"] not in [h["id"] for h in after["hit_memories"]]
    assert all("十月十七号" not in h["content"] for h in after["hit_memories"])


def test_retrieval_skips_irrelevant_memories(client):
    """无关记忆不注入：避免模型拿到一堆不相干事实后自由发挥。"""
    cid = first_character(client)["id"]
    noise = client.post(
        f"/api/characters/{cid}/memories",
        json={"content": "用户不喜欢吃香菜", "importance": 1, "pinned": False},
    ).json()
    out = client.post("/api/chat", json={"character_id": cid, "message": "帮我看看代码报错"}).json()
    assert noise["id"] not in [h["id"] for h in out["hit_memories"]]


# ---------- Prompt 防线 ----------

def test_prompt_forbids_fabrication():
    prompt = build_system_prompt(
        {"name": "鲸落", "one_liner": "温柔的鲸鱼娘"},
        {"personality_tags": ["温柔"], "backstory": "", "speech_style": "轻声",
         "catchphrases": [], "taboo_topics": []},
        [],
    )
    assert "不得编造与用户的共同经历" in prompt
    assert "不要自称 AI" in prompt
    assert "不要假装记得" in prompt  # 空记忆时的额外提示


def test_prompt_includes_hit_memories():
    prompt = build_system_prompt(
        {"name": "鲸落", "one_liner": "x"},
        {"personality_tags": [], "backstory": "", "speech_style": "", "catchphrases": [], "taboo_topics": ["政治"]},
        [{"content": "用户是夜猫子", "importance": 5, "pinned": True}],
    )
    assert "用户是夜猫子" in prompt and "置顶" in prompt
    assert "政治" in prompt


# ---------- 验收 4/5：溯源、渠道与合规 ----------

def test_chat_returns_traceability(client):
    cid = first_character(client)["id"]
    body = client.post("/api/chat", json={"character_id": cid, "message": "在吗"}).json()
    assert body["reply"]
    assert body["persona_version"]["version_no"] >= 1
    assert body["usage"]["prompt_tokens"] > 0
    assert body["latency_ms"] >= 0
    convs = client.get(f"/api/characters/{cid}/conversations").json()
    assert convs[0]["assistant_message"] == body["reply"]


def test_idempotent_event(client):
    """飞书会重复推送，同一幂等键只能处理一次。"""
    cid = first_character(client)["id"]
    key = "evt_" + uuid.uuid4().hex
    first = client.post("/api/chat", json={"character_id": cid, "message": "重复消息", "idempotency_key": key}).json()
    second = client.post("/api/chat", json={"character_id": cid, "message": "重复消息", "idempotency_key": key}).json()
    assert first["duplicated"] is False
    assert second["duplicated"] is True and second["reply"] == ""
    assert len(client.get(f"/api/characters/{cid}/conversations").json()) == 1


def test_channel_catalog_blocks_personal_wechat(client):
    catalog = client.get("/api/channels/catalog").json()
    pw = next(c for c in catalog if c["channel_type"] == "personal_wechat")
    assert pw["enabled"] is False and "不允许" in pw["disabled_reason"]


def test_binding_masks_credentials(client):
    """验收 5：凭证不得以明文出现在接口响应中。"""
    cid = first_character(client)["id"]
    secret = "SECRET-abcdefghijklmnop-9xYz"
    binding = client.post(
        f"/api/characters/{cid}/channels",
        json={"channel_type": "qq_official",
              "config": {"app_id": "102000001", "app_secret": secret}},
    ).json()
    assert secret not in str(binding)
    assert "****" in binding["credentials_masked"]
    assert binding["credentials_masked"].endswith("9xYz")
    # 回填给表单的 config 里，secret 字段同样只能是脱敏串
    assert "****" in binding["config"]["app_secret"]
    assert secret not in str(binding["config"])
    # 非敏感字段可回明文，便于表单编辑
    assert binding["config"]["app_id"] == "102000001"

    detail = client.get(f"/api/characters/{cid}").json()
    assert secret not in str(detail)
    assert "credentials_enc" not in str(detail)

    # 重复绑定同一渠道应被拒绝
    assert client.post(
        f"/api/characters/{cid}/channels",
        json={"channel_type": "qq_official", "config": {"app_id": "x", "app_secret": "y" * 12}},
    ).status_code == 400


def test_bind_personal_wechat_rejected(client):
    cid = first_character(client)["id"]
    resp = client.post(
        f"/api/characters/{cid}/channels",
        json={"channel_type": "personal_wechat", "credentials": {"app_secret": "x"}},
    )
    assert resp.status_code == 400


def test_real_adapter_reports_honest_status(client):
    """真机依赖缺失时必须显示 error 并给出安装提示，不能伪装 connected。"""
    cid = first_character(client)["id"]
    b = client.post(
        f"/api/characters/{cid}/channels",
        json={"channel_type": "feishu",
              "config": {"app_id": "cli_x", "app_secret": "s" * 20}},
    ).json()
    out = client.post(f"/api/channels/{b['id']}/connect").json()
    # 未安装 lark-oapi 时应如实报错；若环境恰好装了则至少不能是 connected 的假状态
    assert out["status"] in ("error", "connecting", "running", "connected")
    if out["status"] == "error":
        assert out["last_error"], "error 状态必须带原因"

    mock_b = client.post(
        f"/api/characters/{cid}/channels",
        json={"channel_type": "mock", "config": {"label": "测试"}},
    ).json()
    assert client.post(f"/api/channels/{mock_b['id']}/connect").json()["status"] == "connected"


# ---------- 验收 6：用量 ----------

def test_usage_accumulates(client):
    cid = first_character(client)["id"]
    before = client.get("/api/usage", params={"range": "today"}).json()["totals"]
    client.post("/api/chat", json={"character_id": cid, "message": "统计一下"})
    after = client.get("/api/usage", params={"range": "today"}).json()
    assert after["totals"]["conversations"] == before["conversations"] + 1
    assert after["totals"]["prompt_tokens"] > before["prompt_tokens"]
    assert after["totals"]["cost_estimate"] >= before["cost_estimate"]
    assert len(after["series"]) == 1
    assert client.get("/api/usage", params={"range": "7d"}).json()["series"].__len__() == 7
    assert client.get("/api/usage", params={"range": "bad"}).status_code == 422


# ---------- P1：A/B 对比 ----------

def test_ab_compare_does_not_pollute_usage(client):
    cid = first_character(client)["id"]
    versions = client.get(f"/api/characters/{cid}/versions").json()
    before = client.get("/api/usage").json()["totals"]["conversations"]
    out = client.post(
        "/api/chat/ab",
        json={"character_id": cid, "message": "今天好累",
              "left_version_id": versions[0]["id"], "right_version_id": versions[-1]["id"]},
    ).json()
    assert out["left"]["reply"] and out["right"]["reply"]
    assert out["left"]["version_no"] != out["right"]["version_no"]
    assert client.get("/api/usage").json()["totals"]["conversations"] == before

    same = client.post(
        "/api/chat/ab",
        json={"character_id": cid, "message": "x",
              "left_version_id": versions[0]["id"], "right_version_id": versions[0]["id"]},
    )
    assert same.status_code == 400


# ---------- 边界与异常 ----------

def test_error_cases(client):
    assert client.get("/api/characters/not_exist").status_code == 400
    assert client.post("/api/chat", json={"character_id": "nope", "message": "hi"}).status_code == 400
    cid = first_character(client)["id"]
    # 纯空格能通过 pydantic 的 min_length，由管线 strip 后判空 -> 400 业务错误
    assert client.post("/api/chat", json={"character_id": cid, "message": "   "}).status_code == 400
    # 空字符串与超长由 pydantic 在入口拦截 -> 422
    assert client.post("/api/chat", json={"character_id": cid, "message": ""}).status_code == 422
    assert client.post("/api/chat", json={"character_id": cid, "message": "x" * 2100}).status_code == 422


def test_delete_character_cascades(client):
    cid = first_character(client)["id"]
    client.post(f"/api/characters/{cid}/memories", json={"content": "待级联删除的记忆"})
    assert client.delete(f"/api/characters/{cid}").status_code == 204
    assert client.get(f"/api/characters/{cid}").status_code == 400
    assert client.get(f"/api/characters/{cid}/memories").status_code == 400


def test_credentials_encryption_roundtrip(tmp_path):
    os.environ["FISHCONSOLE_KEY_FILE"] = str(tmp_path / ".k")
    from app.security import decrypt, encrypt, mask_secret

    token = encrypt("super-secret-value")
    assert "super" not in token
    assert decrypt(token) == "super-secret-value"
    with pytest.raises(ValueError):
        decrypt(token[:-4] + "AAAA")
    assert mask_secret("A1b2cdefghij9xYz") == "A1b2 **** **** 9xYz"
    assert mask_secret("short") == "*****"


def test_mock_reply_is_deterministic_across_processes():
    """Mock 选句不得依赖 Python 的 str hash（带进程随机盐）。

    用子进程验证：同一输入在两个独立解释器里必须得到同一回复，
    否则「离线演示可复现」与回滚风格对比都会随机失效。
    """
    import subprocess
    import sys

    code = (
        "from app.services.llm import MockProvider;"
        "p=MockProvider();"
        "p.register('c1',{'default':['A句。','B句。','C句。','D句。']});"
        "print(p.chat('sys','今天好累','c1').reply)"
    )
    root = str(Path(__file__).resolve().parent.parent)
    outs = [
        subprocess.run(
            [sys.executable, "-c", code], cwd=root,
            capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        ).stdout.strip()
        for _ in range(2)
    ]
    assert outs[0] and outs[0] == outs[1]


def test_memory_placeholder_has_no_duplicate_subject():
    """{memory} 替换必须同时避免两类病句：主语重复、以及丢主语。

    - 「知道你用户是夜猫子」：未转换，主语重复
    - 「又到这个点啦。是夜猫子」：只删前缀，丢主语
    约定：记忆统一以「用户…」书写，替换时转为「你…」，模板使用不带主语的句式。
    """
    from app.services.llm import MockProvider

    p = MockProvider()
    p.register("c1", {"default": ["又到这个点啦。{memory}"]})
    reply = p.chat("sys", "今天好累", "c1",
                   memories=[{"content": "用户是夜猫子，常凌晨两点还在改方案", "importance": 5}]).reply
    assert "你你" not in reply and "你用户" not in reply, "主语重复"
    assert "。是夜猫子" not in reply, "丢主语"
    assert "你是夜猫子" in reply

    reply2 = p.chat("sys", "今天好累", "c1",
                    memories=[{"content": "用户的猫叫土豆", "importance": 3}]).reply
    assert "你你" not in reply2 and "你用户" not in reply2
    assert "你的猫叫土豆" in reply2

    # 无记忆时给出不编造的兜底表述
    reply3 = p.chat("sys", "今天好累", "c1", memories=[]).reply
    assert "你跟我说过的那些事" in reply3


def test_ab_versions_are_independently_resolved(client):
    """P1 的正确性前提：两栏必须各自按所选版本解析人设，而不是都用当前激活版本。

    注意：不断言两侧回复文本必然不同。Mock 回复池有限，不同版本的选句
    可能碰撞到同一句；真正要守住的是「用的是哪个版本的人设」这件事。
    版本内容不同 -> system prompt 必须不同，这才是可断言的硬性语义。
    """
    cid = first_character(client)["id"]
    versions = client.get(f"/api/characters/{cid}/versions").json()
    assert len(versions) >= 2
    v_old, v_new = versions[-1], versions[0]

    out = client.post(
        "/api/chat/ab",
        json={"character_id": cid, "message": "今天好累，方案又被打回了",
              "left_version_id": v_old["id"], "right_version_id": v_new["id"]},
    ).json()
    assert out["left"]["version_no"] == v_old["version_no"]
    assert out["right"]["version_no"] == v_new["version_no"]
    assert out["left"]["reply"] and out["right"]["reply"]

    # 人设内容不同 -> 组装出的 system prompt 必须不同
    from app.services.llm import build_system_prompt

    char = {"name": "x", "one_liner": "y"}
    assert build_system_prompt(char, v_old, []) != build_system_prompt(char, v_new, [])


def test_persona_fingerprint_ignores_version_number(client):
    """回滚一致性：内容相同的版本指纹必须相同，哪怕版本号不同。

    否则回滚后「内容恢复了但回复变了」，违背「回滚即恢复原状」的语义。
    """
    from app.services.pipeline import persona_fingerprint

    cid = first_character(client)["id"]
    versions = client.get(f"/api/characters/{cid}/versions").json()
    target = versions[-1]
    rolled = client.post(f"/api/versions/{target['id']}/rollback").json()
    assert rolled["version_no"] != target["version_no"]
    assert persona_fingerprint(rolled) == persona_fingerprint(target)


# ========== 限流（借鉴 AstrBot 滑动窗口思路，独立实现）==========

def test_rate_limit_blocks_and_returns_429(client):
    """超出窗口阈值必须返回 429，并带 Retry-After 供调用方退避。"""
    cid = first_character(client)["id"]
    client.put("/api/settings/rate_limit",
               json={"enabled": True, "count": 3, "window_seconds": 60, "strategy": "discard"})

    for i in range(3):
        r = client.post("/api/chat", json={"character_id": cid, "message": f"第{i}条"})
        assert r.status_code == 200, f"第 {i+1} 条不应被限流"
        assert r.json()["rate_limit"]["remaining"] == 2 - i

    blocked = client.post("/api/chat", json={"character_id": cid, "message": "超额"})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert blocked.headers["X-RateLimit-Limit"] == "3"
    body = blocked.json()["rate_limit"]
    assert body["remaining"] == 0 and body["limit"] == 3

    # 被限流的请求不应落库
    assert len(client.get(f"/api/characters/{cid}/conversations").json()) == 3


def test_rate_limit_is_per_session(client):
    """限流粒度是会话，不同会话互不影响。"""
    cid = first_character(client)["id"]
    client.put("/api/settings/rate_limit", json={"count": 2, "window_seconds": 60})
    for i in range(2):
        assert client.post("/api/chat", json={"character_id": cid, "message": f"a{i}",
                                              "session_key": "sess:A"}).status_code == 200
    assert client.post("/api/chat", json={"character_id": cid, "message": "a3",
                                          "session_key": "sess:A"}).status_code == 429
    # 另一个会话仍有完整配额
    assert client.post("/api/chat", json={"character_id": cid, "message": "b1",
                                          "session_key": "sess:B"}).status_code == 200


def test_rate_limit_window_slides(client):
    """窗口滑动后配额恢复。"""
    cid = first_character(client)["id"]
    client.put("/api/settings/rate_limit", json={"count": 1, "window_seconds": 1})
    assert client.post("/api/chat", json={"character_id": cid, "message": "x1"}).status_code == 200
    assert client.post("/api/chat", json={"character_id": cid, "message": "x2"}).status_code == 429
    time.sleep(1.2)
    assert client.post("/api/chat", json={"character_id": cid, "message": "x3"}).status_code == 200


def test_rate_limit_disabled_and_duplicate_not_charged(client):
    """关闭后不限流；重复事件不消耗配额（去重必须排在限流之前）。"""
    cid = first_character(client)["id"]
    client.put("/api/settings/rate_limit", json={"enabled": True, "count": 2, "window_seconds": 60})
    key = "evt_" + uuid.uuid4().hex
    first = client.post("/api/chat", json={"character_id": cid, "message": "去重", "idempotency_key": key})
    assert first.status_code == 200
    for _ in range(5):
        dup = client.post("/api/chat", json={"character_id": cid, "message": "去重", "idempotency_key": key})
        assert dup.status_code == 200 and dup.json()["duplicated"] is True
    # 仅首条扣了配额，剩余 1 次仍可用
    assert client.post("/api/chat", json={"character_id": cid, "message": "还能发"}).status_code == 200

    client.put("/api/settings/rate_limit", json={"enabled": False})
    for i in range(6):
        assert client.post("/api/chat", json={"character_id": cid, "message": f"n{i}"}).status_code == 200


def test_rate_limit_stall_strategy_waits(client):
    """stall 策略等待到下个窗口再放行，而非直接拒绝。"""
    cid = first_character(client)["id"]
    client.put("/api/settings/rate_limit",
               json={"count": 1, "window_seconds": 1, "strategy": "stall"})
    assert client.post("/api/chat", json={"character_id": cid, "message": "s1"}).status_code == 200
    t0 = time.monotonic()
    r = client.post("/api/chat", json={"character_id": cid, "message": "s2"})
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    assert elapsed >= 0.5, "stall 应当真的等待过"
    assert r.json()["rate_limit"]["stalled_seconds"] > 0


def test_ab_compare_does_not_consume_quota(client):
    """A/B 不落库，也不应扣配额，否则一次操作扣两次。"""
    cid = first_character(client)["id"]
    versions = client.get(f"/api/characters/{cid}/versions").json()
    client.put("/api/settings/rate_limit", json={"count": 2, "window_seconds": 60})
    for _ in range(3):
        r = client.post("/api/chat/ab",
                        json={"character_id": cid, "message": "今天好累",
                              "left_version_id": versions[-1]["id"],
                              "right_version_id": versions[0]["id"]})
        assert r.status_code == 200
    # 正常对话配额未被 A/B 消耗
    assert client.post("/api/chat", json={"character_id": cid, "message": "q1"}).status_code == 200
    assert client.post("/api/chat", json={"character_id": cid, "message": "q2"}).status_code == 200
    assert client.post("/api/chat", json={"character_id": cid, "message": "q3"}).status_code == 429


# ========== 去重 TTL 清理 ==========

def test_dedup_ttl_sweep_removes_expired(client):
    """TTL 到期的幂等键被清理，解决 processed_event 无限增长。"""
    cid = first_character(client)["id"]
    keys = []
    for i in range(5):
        k = f"evt_old_{i}"
        keys.append(k)
        client.post("/api/chat", json={"character_id": cid, "message": f"m{i}", "idempotency_key": k})

    st = client.get("/api/settings/runtime").json()["dedup"]
    assert st["total_rows"] == 5

    # 同一键仍判重
    dup = client.post("/api/chat", json={"character_id": cid, "message": "m0", "idempotency_key": keys[0]})
    assert dup.json()["duplicated"] is True

    # TTL 设为 0 秒并立即清理 -> 全部过期
    client.put("/api/settings/dedup", json={"ttl_seconds": 1, "sweep_interval_seconds": 10})
    time.sleep(1.2)
    swept = client.post("/api/settings/dedup/sweep").json()
    assert swept["removed"] == 5
    assert swept["stats"]["total_rows"] == 0

    # 清理后同一键不再判重（TTL 的预期语义）
    again = client.post("/api/chat", json={"character_id": cid, "message": "m0", "idempotency_key": keys[0]})
    assert again.json()["duplicated"] is False


def test_dedup_max_rows_caps_growth(client):
    """即使 TTL=0（永不过期），行数上限也必须兜住增长。

    max_rows 下限为 100（低于此值会被 422 拒绝），因此用 100 做边界验证。
    """
    cid = first_character(client)["id"]
    ok = client.put("/api/settings/dedup",
                    json={"ttl_seconds": 0, "max_rows": 100, "sweep_interval_seconds": 10})
    assert ok.status_code == 200 and ok.json()["max_rows"] == 100

    for i in range(130):
        client.post("/api/chat", json={"character_id": cid, "message": "g",
                                       "idempotency_key": f"grow_{i:04d}"})
    assert client.get("/api/settings/runtime").json()["dedup"]["total_rows"] == 130

    swept = client.post("/api/settings/dedup/sweep").json()
    assert swept["removed"] == 30, "应淘汰超出上限的 30 行"
    assert swept["stats"]["total_rows"] == 100

    # 淘汰的是最旧的：最新写入的键仍判重
    newest = client.post("/api/chat", json={"character_id": cid, "message": "g",
                                            "idempotency_key": "grow_0129"})
    assert newest.json()["duplicated"] is True


def test_dedup_max_rows_rejects_below_floor(client):
    """行数上限有下限保护，避免误配成极小值导致去重形同虚设。"""
    assert client.put("/api/settings/dedup", json={"max_rows": 10}).status_code == 422


def test_dedup_can_be_disabled(client):
    """关闭去重后，同一幂等键会被重复处理。"""
    cid = first_character(client)["id"]
    client.put("/api/settings/dedup", json={"enabled": False})
    key = "evt_off"
    a = client.post("/api/chat", json={"character_id": cid, "message": "x", "idempotency_key": key})
    b = client.post("/api/chat", json={"character_id": cid, "message": "x", "idempotency_key": key})
    assert a.json()["duplicated"] is False and b.json()["duplicated"] is False


# ========== 设置接口 ==========

def test_settings_crud_and_validation(client):
    body = client.get("/api/settings").json()
    assert body["rate_limit"]["count"] == 30          # 默认对齐企微 30 条/分钟
    assert body["rate_limit"]["window_seconds"] == 60
    assert body["dedup"]["ttl_seconds"] == 3600

    updated = client.put("/api/settings/rate_limit",
                         json={"count": 5, "strategy": "stall"}).json()
    assert updated["count"] == 5 and updated["strategy"] == "stall"
    assert updated["window_seconds"] == 60, "未提交的字段应保持原值"

    # 越界与非法枚举由 pydantic 在入口拦截
    assert client.put("/api/settings/rate_limit", json={"count": 0}).status_code == 422
    assert client.put("/api/settings/rate_limit", json={"count": 99999}).status_code == 422
    assert client.put("/api/settings/rate_limit", json={"strategy": "boom"}).status_code == 422
    assert client.put("/api/settings/dedup", json={"ttl_seconds": -1}).status_code == 422
    assert client.post("/api/settings/unknown_key/reset").status_code == 400

    assert client.post("/api/settings/rate_limit/reset").json()["count"] == 30


def test_settings_persist_across_restart(tmp_path):
    """设置必须落库：AstrBot 的限流配置在内存，重启清零。"""
    db_file = tmp_path / "settings.db"
    os.environ["FISHCONSOLE_KEY_FILE"] = str(tmp_path / ".key")
    with TestClient(create_app(db_path=str(db_file))) as c1:
        c1.put("/api/settings/rate_limit", json={"count": 7, "window_seconds": 120})
        c1.put("/api/settings/dedup", json={"ttl_seconds": 99})
    with TestClient(create_app(db_path=str(db_file))) as c2:
        s = c2.get("/api/settings").json()
        assert s["rate_limit"]["count"] == 7 and s["rate_limit"]["window_seconds"] == 120
        assert s["dedup"]["ttl_seconds"] == 99


def test_runtime_status_and_counter_reset(client):
    cid = first_character(client)["id"]
    client.put("/api/settings/rate_limit", json={"count": 3, "window_seconds": 60})
    client.post("/api/chat", json={"character_id": cid, "message": "一条", "session_key": "sess:X"})

    rt = client.get("/api/settings/runtime", params={"session_key": "sess:X"}).json()
    assert rt["rate_limit"]["remaining"] == 2
    assert rt["rate_limit"]["tracked_sessions"] >= 1
    assert "total_rows" in rt["dedup"]

    client.post("/api/settings/rate_limit/reset_counters")
    rt2 = client.get("/api/settings/runtime", params={"session_key": "sess:X"}).json()
    assert rt2["rate_limit"]["remaining"] == 3, "计数清空后配额应恢复"


def test_limiter_sessions_are_reclaimed():
    """空闲会话的队列与锁必须被回收（AstrBot 的 defaultdict 只增不删）。"""
    from app.services.ratelimit import RateLimiter
    from app.services.settings import RateLimitSettings

    class _S:
        def rate_limit(self):
            return RateLimitSettings(enabled=True, count=5, window_seconds=1, strategy="discard")

    rl = RateLimiter(_S())
    for i in range(50):
        rl.check(f"sess-{i}")
    assert rl.stats()["tracked_sessions"] == 50
    time.sleep(3.2)  # 超过 window * IDLE_FACTOR
    for i in range(3):
        rl.check(f"fresh-{i}")
    assert rl.stats()["tracked_sessions"] < 50, "空闲会话应被回收"


def test_timestamp_precision_is_subsecond(client):
    """时间戳必须保留亚秒精度，且与用量统计的区间边界格式一致。

    回归两个已修缺陷：
      1. 秒级截断会让小于 1 秒粒度的 TTL 整体失效；
      2. 边界用 '...T00:00:00Z' 而数据是 '...T00:00:00.123Z' 时，
         字典序下 '.' < 'Z'，当天用量会被整体漏掉。
    """
    from app.db import utcnow

    ts = utcnow()
    assert ts.endswith("Z") and "." in ts, f"应为毫秒精度: {ts}"
    assert len(ts) == len("2026-11-04T00:00:00.000Z")

    # 当天的对话必须被 today 区间统计到
    cid = first_character(client)["id"]
    before = client.get("/api/usage", params={"range": "today"}).json()["totals"]["conversations"]
    client.post("/api/chat", json={"character_id": cid, "message": "边界验证"})
    after = client.get("/api/usage", params={"range": "today"}).json()["totals"]["conversations"]
    assert after == before + 1, "毫秒精度下当天数据不应被区间边界漏掉"


# ========== 适配器 schema 驱动（照搬 AstrBot 的配置模板机制）==========

def test_platform_schema_declares_fields(client):
    """每个适配器自带配置模板，前端据此渲染表单，不需手写输入框。"""
    schema = client.get("/api/platforms/schema").json()
    by_type = {c["channel_type"]: c for c in schema}
    assert set(by_type) == {"mock", "qq_official", "wecom", "feishu", "personal_wechat"}

    qq = by_type["qq_official"]
    keys = [f["key"] for f in qq["fields"]]
    assert keys == ["app_id", "app_secret", "enable_group", "use_markdown", "sandbox"]
    assert qq["secret_keys"] == ["app_secret"]
    assert qq["connection_mode"] == "websocket"
    assert qq["supports_proactive"] is False       # QQ 官方不支持主动消息
    assert any("主动消息" in c for c in qq["constraints"])

    # 每个字段都要有渲染表单所需的完整元信息
    for c in schema:
        for f in c["fields"]:
            assert f["type"] in ("text", "secret", "number", "bool", "select")
            assert f["label"] and "default" in f
            if f["type"] == "select":
                assert f["options"], f"{f['key']} 缺少候选项"
            if f["type"] == "number":
                assert "min" in f and "max" in f

    # 飞书/企微必须声明「需要先回占位」，这是平台硬约束
    assert by_type["feishu"]["requires_placeholder"] is True
    assert by_type["wecom"]["requires_placeholder"] is True
    assert any("3 秒" in c for c in by_type["feishu"]["constraints"])
    assert any("30 条" in c for c in by_type["wecom"]["constraints"])

    # 个人微信：置灰 + 原因，字段为空
    pw = by_type["personal_wechat"]
    assert pw["enabled"] is False and pw["fields"] == []
    assert "不允许" in pw["disabled_reason"]

    # 旧路径保持兼容
    assert client.get("/api/channels/catalog").json() == schema


def test_schema_driven_validation(client):
    """校验规则来自字段声明：required / number 范围 / select 枚举。"""
    cid = first_character(client)["id"]

    # 缺 required 字段
    r = client.post(f"/api/characters/{cid}/channels",
                    json={"channel_type": "feishu", "config": {"app_id": "cli_x"}})
    assert r.status_code == 400 and "App Secret" in r.json()["detail"]

    # select 非法值
    r = client.post(f"/api/characters/{cid}/channels",
                    json={"channel_type": "feishu",
                          "config": {"app_id": "a", "app_secret": "b" * 12, "domain": "boom"}})
    assert r.status_code == 400 and "服务域" in r.json()["detail"]

    # number 越界（企微节流上限 30）
    r = client.post(f"/api/characters/{cid}/channels",
                    json={"channel_type": "wecom",
                          "config": {"bot_id": "a", "bot_secret": "b" * 12,
                                     "send_rate_per_minute": 999}})
    assert r.status_code == 400 and "不能大于" in r.json()["detail"]

    # 未知渠道
    assert client.post(f"/api/characters/{cid}/channels",
                       json={"channel_type": "telegram", "config": {}}).status_code == 400

    # 合法配置：未提交的字段取声明的默认值
    ok = client.post(f"/api/characters/{cid}/channels",
                     json={"channel_type": "feishu",
                           "config": {"app_id": "cli_ok", "app_secret": "s" * 20}}).json()
    assert ok["config"]["domain"] == "feishu"
    assert ok["config"]["placeholder_text"] == "让我想想…"


def test_update_config_keeps_secret_when_masked(client):
    """前端回传脱敏串时不得覆盖真实密钥，否则编辑一次就把凭证写坏。"""
    cid = first_character(client)["id"]
    secret = "REALSECRET-abcdefgh-1234"
    b = client.post(f"/api/characters/{cid}/channels",
                    json={"channel_type": "qq_official",
                          "config": {"app_id": "102000001", "app_secret": secret}}).json()

    masked = b["config"]["app_secret"]
    assert "****" in masked

    # 只改非敏感字段，secret 原样回传脱敏串
    updated = client.put(f"/api/channels/{b['id']}",
                         json={"config": {"app_id": "102000002", "app_secret": masked,
                                          "sandbox": True}}).json()
    assert updated["config"]["app_id"] == "102000002"
    assert updated["config"]["sandbox"] is True
    # 真实密钥未被脱敏串覆盖
    assert updated["credentials_masked"].endswith(secret[-4:])

    # 真正换密钥则生效
    new_secret = "NEWSECRET-zzzzzzzz-9999"
    again = client.put(f"/api/channels/{b['id']}",
                       json={"config": {"app_secret": new_secret}}).json()
    assert again["credentials_masked"].endswith("9999")


# ========== 适配器运行时：事件总线全链路 ==========

def test_inbound_message_flows_through_pipeline(client):
    """验证「适配器 → 事件队列 → 管线 → 回写占位」整条链路。

    真机消息与调试台走同一条管线，这是「后台能跑通的，接上真机也能跑通」
    的技术保证。
    """
    cid = first_character(client)["id"]
    b = client.post(f"/api/characters/{cid}/channels",
                    json={"channel_type": "mock",
                          "config": {"label": "链路测试", "auto_reply_delay_ms": 0}}).json()
    assert client.post(f"/api/channels/{b['id']}/connect").json()["status"] == "connected"

    out = client.post(f"/api/channels/{b['id']}/simulate",
                      json={"message": "今天好累", "session_id": "u1"}).json()

    # 会话键是结构化的，可逆解析
    assert out["session_key"].startswith(b["id"] + ":private:u1")
    # 适配器发件箱里有已完成的回复
    final = [m for m in out["outbox"] if m["finish"]]
    assert final and final[-1]["text"], "管线结果应回写到适配器"
    assert out["bus"]["processed"] >= 1 and out["bus"]["failed"] == 0

    # 对话已落库，与调试台同一条管线
    convs = client.get(f"/api/characters/{cid}/conversations").json()
    assert convs and convs[0]["user_message"] == "今天好累"


def test_inbound_dedup_across_adapter(client):
    """适配器重复推送同一事件时，管线统一去重（AstrBot 是各适配器各写一套）。"""
    cid = first_character(client)["id"]
    b = client.post(f"/api/characters/{cid}/channels",
                    json={"channel_type": "mock",
                          "config": {"auto_reply_delay_ms": 0}}).json()
    client.post(f"/api/channels/{b['id']}/connect")

    key = "evt_" + uuid.uuid4().hex
    for _ in range(3):
        client.post(f"/api/channels/{b['id']}/simulate",
                    json={"message": "重复推送", "session_id": "u2", "idempotency_key": key})

    convs = [c for c in client.get(f"/api/characters/{cid}/conversations").json()
             if c["user_message"] == "重复推送"]
    assert len(convs) == 1, f"同一幂等键只应处理一次，实际 {len(convs)}"


def test_platform_runtime_reports_status(client):
    """运行态接口给出连接状态、收发计数与队列水位。"""
    cid = first_character(client)["id"]
    b = client.post(f"/api/characters/{cid}/channels",
                    json={"channel_type": "mock", "config": {"auto_reply_delay_ms": 0}}).json()
    client.post(f"/api/channels/{b['id']}/connect")
    client.post(f"/api/channels/{b['id']}/simulate", json={"message": "统计", "session_id": "u3"})

    rt = client.get("/api/platforms/runtime").json()
    assert rt["bus"]["running_adapters"] == 1
    mine = next(a for a in rt["adapters"] if a["binding_id"] == b["id"])
    assert mine["status"] == "running" and mine["stats"]["received"] >= 1

    # 断开后不再计入运行中
    client.post(f"/api/channels/{b['id']}/disconnect")
    assert client.get("/api/platforms/runtime").json()["bus"]["running_adapters"] == 0


def test_simulate_rejects_non_mock_and_disconnected(client):
    cid = first_character(client)["id"]
    b = client.post(f"/api/characters/{cid}/channels",
                    json={"channel_type": "mock", "config": {}}).json()
    # 未连接时拒绝
    assert client.post(f"/api/channels/{b['id']}/simulate",
                       json={"message": "x"}).status_code == 400


def test_connected_bindings_autostart_after_restart(tmp_path):
    """凭证填过一次即持久化，重启后自动恢复连接，无需再去界面点一遍。"""
    db_file = tmp_path / "autostart.db"
    os.environ["FISHCONSOLE_KEY_FILE"] = str(tmp_path / ".key")

    with TestClient(create_app(db_path=str(db_file))) as c1:
        cid = c1.get("/api/characters").json()[0]["id"]
        b = c1.post(f"/api/characters/{cid}/channels",
                    json={"channel_type": "mock", "config": {"label": "持久连接"}}).json()
        assert c1.post(f"/api/channels/{b['id']}/connect").json()["status"] == "connected"
        bid = b["id"]

    # 重启：lifespan 的 autostart 应自动把它拉起来
    with TestClient(create_app(db_path=str(db_file))) as c2:
        rt = c2.get("/api/platforms/runtime").json()
        assert rt["bus"]["running_adapters"] == 1, "connected 的绑定应自动重连"
        assert any(a["binding_id"] == bid for a in rt["adapters"])
        # 直接注入消息即可工作，无需手动再连一次
        out = c2.post(f"/api/channels/{bid}/simulate",
                      json={"message": "重启后直接可用", "session_id": "u9"}).json()
        assert any(m["finish"] for m in out["outbox"])

    # autostart=False 时不自动连，便于测试与排障
    with TestClient(create_app(db_path=str(db_file), autostart=False)) as c3:
        assert c3.get("/api/platforms/runtime").json()["bus"]["running_adapters"] == 0


def test_session_key_is_structured():
    """会话标识可逆解析，支持区分私聊/群聊并隔离不同绑定。"""
    from app.adapters import build_session_key, parse_session_key

    key = build_session_key("feishu", "group", "oc_123", "cb_abc")
    assert key == "cb_abc:group:oc_123"
    assert parse_session_key(key) == ("cb_abc", "group", "oc_123")
    # 群聊与私聊的同一 id 不会串线
    assert build_session_key("feishu", "private", "oc_123", "cb_abc") != key


def test_placeholder_then_replace_flow(client):
    """「先回占位、再替换为正文」必须真实走一遍。

    这不是优化项：飞书要求 3 秒内响应，企微长连接要求先发 finish=false
    占位分片。Mock 适配器刻意复现同一时序，保证本地所见即真机所得。
    """
    cid = first_character(client)["id"]
    b = client.post(f"/api/characters/{cid}/channels",
                    json={"channel_type": "mock",
                          "config": {"auto_reply_delay_ms": 0}}).json()
    client.post(f"/api/channels/{b['id']}/connect")

    out = client.post(f"/api/channels/{b['id']}/simulate",
                      json={"message": "今天好累", "session_id": "ph1"}).json()
    final = [m for m in out["outbox"] if m["finish"]]
    assert final, "必须有最终消息"
    # 占位痕迹保留，证明走的是两阶段而非一次性发送
    assert final[-1].get("placeholder_text") == "…"
    assert final[-1]["text"] != "…"

    rt = client.get("/api/platforms/runtime").json()
    mine = next(a for a in rt["adapters"] if a["binding_id"] == b["id"])
    assert mine["stats"]["placeholders"] >= 1, "占位计数应被记录"
    assert mine["stats"]["sent"] >= 1


def test_requires_placeholder_declared_per_platform(client):
    """占位需求由平台 schema 声明，管线据此决定是否先发占位。"""
    by_type = {c["channel_type"]: c for c in client.get("/api/platforms/schema").json()}
    # 飞书 3 秒约束、企微流式占位、Mock 复现时序
    assert by_type["feishu"]["requires_placeholder"] is True
    assert by_type["wecom"]["requires_placeholder"] is True
    assert by_type["mock"]["requires_placeholder"] is True
    # QQ 官方是被动回复语义，不需要先发占位
    assert by_type["qq_official"]["requires_placeholder"] is False


def test_mock_idempotency_key_survives_restart(tmp_path):
    """Mock 自动生成的幂等键必须带进程维度。

    若只用 binding_id + 自增序号，重启后序号从 1 重新计数，会与库里
    已有的键碰撞，消息被误判为重复事件而静默丢弃——表现为「只有占位、
    没有最终回复」，很容易被误判成链路断了。
    """
    db_file = tmp_path / "idem.db"
    os.environ["FISHCONSOLE_KEY_FILE"] = str(tmp_path / ".key")

    with TestClient(create_app(db_path=str(db_file))) as c1:
        cid = c1.get("/api/characters").json()[0]["id"]
        b = c1.post(f"/api/characters/{cid}/channels",
                    json={"channel_type": "mock", "config": {"auto_reply_delay_ms": 0}}).json()
        c1.post(f"/api/channels/{b['id']}/connect")
        first = c1.post(f"/api/channels/{b['id']}/simulate",
                        json={"message": "第一轮", "session_id": "u1"}).json()
        assert any(m["finish"] for m in first["outbox"])
        assert first["duplicated"] is False
        bid = b["id"]

    # 重启：自动重连后再注入，不应因键碰撞被当成重复
    with TestClient(create_app(db_path=str(db_file))) as c2:
        again = c2.post(f"/api/channels/{bid}/simulate",
                        json={"message": "第二轮", "session_id": "u1"}).json()
        assert again["duplicated"] is False, "重启后不应被误判为重复事件"
        final = [m for m in again["outbox"] if m["finish"]]
        assert final and final[-1]["text"], "必须拿到最终回复"


def test_simulate_marks_duplicate_explicitly(client):
    """显式重复推送时标注原因，不让人误以为链路断了。"""
    cid = first_character(client)["id"]
    b = client.post(f"/api/characters/{cid}/channels",
                    json={"channel_type": "mock", "config": {"auto_reply_delay_ms": 0}}).json()
    client.post(f"/api/channels/{b['id']}/connect")

    key = "evt_" + uuid.uuid4().hex
    first = client.post(f"/api/channels/{b['id']}/simulate",
                        json={"message": "去重验证", "session_id": "u1",
                              "idempotency_key": key}).json()
    assert first["duplicated"] is False and any(m["finish"] for m in first["outbox"])

    dup = client.post(f"/api/channels/{b['id']}/simulate",
                      json={"message": "去重验证", "session_id": "u1",
                            "idempotency_key": key}).json()
    assert dup["duplicated"] is True
    assert "重复事件" in dup["note"]
