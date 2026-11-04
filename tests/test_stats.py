"""运行统计接口的回归测试。

覆盖 AstrBot 统计页那套信息结构：概览 KPI（含运行时间）、消息趋势、
平台排名、模型用量排名、Token 总量卡（含成功率）、会话 Top 10。
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import create_app  # noqa: E402
from app.services.llm import LLMResult  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    db_file = tmp_path / f"stats_{uuid.uuid4().hex}.db"
    os.environ["FISHCONSOLE_KEY_FILE"] = str(tmp_path / ".key")
    app = create_app(db_path=str(db_file), do_seed=True, autostart=False)
    with TestClient(app) as c:
        yield c


def first_character(client) -> dict:
    return client.get("/api/characters").json()[0]


def test_overview_shape(client):
    body = client.get("/api/stats/overview").json()
    # 概览卡片：AstrBot 的 platformCount / messageCount / todayModelCalls / uptime
    assert body["uptime_seconds"] >= 0
    assert body["uptime_text"]
    assert body["started_at"]
    assert set(body["platforms"]) == {"total", "running"}
    assert body["characters"] == 3
    assert body["message_count"] == 0
    assert body["today"]["model_calls"] == 0
    assert isinstance(body["resources"], dict)
    assert body["recent"] == []


def test_range_stats_aggregate_after_chat(client):
    cid = first_character(client)["id"]
    for msg in ("第一句", "第二句"):
        client.post("/api/chat", json={
            "character_id": cid, "message": msg, "session_key": "stats:private:u1",
        })

    body = client.get("/api/stats?range=1d").json()
    assert body["message_count"] == 2
    assert body["model"]["calls"] == 2
    assert body["model"]["success_rate"] == 100
    assert body["model"]["tokens"] > 0
    assert body["model"]["prompt_tokens"] > 0

    # 平台排名（Mock 渠道）
    assert [c["key"] for c in body["by_channel"]] == ["mock"]
    assert body["by_channel"][0]["count"] == 2

    # 模型排名
    assert len(body["by_model"]) == 1
    assert body["by_model"][0]["calls"] == 2

    # 会话 Top 10（同一会话两轮）
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["calls"] == 2

    # 趋势按天分桶，最后一格是今天
    assert body["series"][-1]["messages"] == 2
    assert body["series"][-1]["calls"] == 2

    # 概览也跟着动
    ov = client.get("/api/stats/overview").json()
    assert ov["message_count"] == 2
    assert ov["today"]["model_calls"] == 2
    assert len(ov["recent"]) == 2


def test_range_validation(client):
    assert client.get("/api/stats?range=1d").status_code == 200
    assert client.get("/api/stats?range=7d").status_code == 200
    assert client.get("/api/stats?range=all").status_code == 200
    assert client.get("/api/stats?range=999d").status_code == 422
    assert client.get("/api/stats?range=forever").status_code == 422


def test_failed_call_is_recorded_and_drops_success_rate(client):
    """模型调用失败要留痕：成功率不能永远是 100%。"""

    class BoomProvider:
        label = "boom"
        model = "boom"

        def chat(self, **kwargs):
            return LLMResult("", 0, 0, "boom", error="模型炸了")

    ctx = client.app.state.ctx
    cid = first_character(client)["id"]
    # 先成功一次，作为对照
    client.post("/api/chat", json={"character_id": cid, "message": "正常一句", "session_key": "s1"})

    ctx.pipeline.provider = BoomProvider()
    ctx.pipeline.providers = None
    resp = client.post("/api/chat", json={"character_id": cid, "message": "会失败", "session_key": "s2"})
    assert resp.status_code == 400

    body = client.get("/api/stats?range=1d").json()
    assert body["message_count"] == 2
    assert body["model"]["calls"] == 1
    assert body["model"]["errors"] == 1
    assert body["model"]["success_rate"] == 50

    # 失败记录不能污染用量统计
    usage = client.get("/api/usage?range=today").json()
    assert usage["totals"]["conversations"] == 1

    # 失败记录也不该进多轮上下文（assistant_message 为空）
    ctx.pipeline.provider, _ = ctx.mock, None
    from app.services.pipeline import MessagePipeline

    history = ctx.pipeline.history_for(cid, "s2", 5)
    assert all(item["content"] != "" for item in history)
