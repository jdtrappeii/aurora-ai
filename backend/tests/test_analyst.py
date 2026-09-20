"""The analyst: every tool returns valid JSON from the deterministic analytics,
the agent loop builds an answer and a trace from the runner's messages, and
the API route guards the key. The Claude API is never called: the runner is
faked."""
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.analyst import agent as agent_mod
from app.analyst.agent import ask, trace_text
from app.analyst.tools import build_tools
from app.models import Store
from tests.conftest import MONDAY, dt

SUNDAY = MONDAY + timedelta(days=6)


def seed_ledger(seed):
    seed.store.state = "FL"
    seed.product("A", "Flower", "10", "25")
    seed.product("B", "Edibles", "4", "10")
    seed.sale(dt(MONDAY - timedelta(days=7), 10), [("A", 2, "25"), ("B", 6, "10")])
    seed.sale(dt(MONDAY, 10), [("A", 4, "25")])
    seed.inventory("A", MONDAY, 100, MONDAY - timedelta(days=120))
    seed.promotion("Flower Friday", MONDAY, SUNDAY, category="Flower")
    seed.commit()


def tool_map(session, scope=None, as_of=SUNDAY):
    tools = build_tools(session, scope, as_of)
    return {t.name: t for t in tools}


def test_every_tool_returns_json_on_seeded_data(session, seed):
    seed_ledger(seed)
    tools = tool_map(session, "state:FL")
    assert list(tools)[:3] == ["list_stores", "data_coverage", "weekly_summary"]  # stable order for the prompt cache
    calls = {
        "list_stores": {}, "data_coverage": {}, "weekly_summary": {}, "weekly_trend_series": {"weeks": 4},
        "store_ranking_for_period": {}, "categories": {}, "products": {"limit": 5}, "inventory": {"status": "slow"},
        "promotions": {}, "discount_codes": {}, "market": {}, "competitor_deals": {},
        "external_findings": {"store": "MAIN"}, "weather_effects": {"store": "MAIN"}, "forecast": {"store": "MAIN", "days": 3},
        "upcoming_events": {"days": 7},
    }
    for name, kwargs in calls.items():
        out = json.loads(tools[name].call(kwargs))
        assert out is not None or name == "market", name
    ws = json.loads(tools["weekly_summary"].call({}))
    assert ws["store"] == "state:FL" and ws["current_week"]["revenue"] == "100.00"
    ranking = json.loads(tools["store_ranking_for_period"].call({}))
    assert ranking[0]["store"] == "MAIN" and ranking[0]["gross_profit_delta"] == "-6.00"
    inv = json.loads(tools["inventory"].call({"status": "slow"}))
    assert inv["items"][0]["sku"] == "A"
    stores = json.loads(tools["list_stores"].call({}))
    assert stores["default_scope"] == "state:FL" and stores["scopes"] == ["state:FL"]
    cov = json.loads(tools["data_coverage"].call({}))
    assert cov["first_sale"] == (MONDAY - timedelta(days=7)).isoformat() and cov["market_report"] is False
    # explicit scope wins over the default
    assert json.loads(tools["weekly_summary"].call({"scope": "MAIN"}))["store"] == "MAIN"
    # tool descriptions reach the schema the model sees
    schema = tools["external_findings"].to_dict()
    assert "evidence level" in schema["description"] and "store" in schema["input_schema"]["required"]


class FakeBlock(SimpleNamespace):
    pass


class FakeMessage(SimpleNamespace):
    pass


def fake_client(script, captured):
    """A client whose tool_runner yields the scripted messages and records kwargs."""
    def tool_runner(**kwargs):
        captured.update(kwargs)
        return iter(script)
    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(tool_runner=tool_runner)))


def test_ask_builds_answer_trace_and_usage(session, seed):
    seed_ledger(seed)
    script = [
        FakeMessage(stop_reason="tool_use", model="claude-opus-5",
                    usage=SimpleNamespace(input_tokens=1200, output_tokens=80, cache_read_input_tokens=1000),
                    content=[FakeBlock(type="text", text="Checking coverage first."),
                             FakeBlock(type="tool_use", name="data_coverage", input={}),
                             FakeBlock(type="tool_use", name="weekly_summary", input={"scope": "MAIN"})]),
        FakeMessage(stop_reason="end_turn", model="claude-opus-5",
                    usage=SimpleNamespace(input_tokens=2500, output_tokens=300, cache_read_input_tokens=1000),
                    content=[FakeBlock(type="thinking", thinking=""), FakeBlock(type="text", text="Revenue fell 9.1% to $100.")]),
    ]
    captured = {}
    settings = SimpleNamespace(default_scope="state:FL", anthropic_api_key="", analyst_model="claude-opus-5", analyst_effort="high", analyst_max_iterations=24)
    res = ask(session, "Why was revenue down?", history=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
              settings=settings, client=fake_client(script, captured))
    assert res.answer == "Revenue fell 9.1% to $100." and res.turns == 2 and res.stop_reason == "end_turn"
    assert res.usage == {"input_tokens": 3700, "output_tokens": 380, "cache_read_input_tokens": 2000}
    assert [t.get("tool") for t in res.trace if "tool" in t] == ["data_coverage", "weekly_summary"]
    assert "Checking coverage first." in trace_text(res.trace) and "weekly_summary(scope=MAIN)" in trace_text(res.trace)
    # request shape: current API, cached system prompt, fallbacks on, history preserved, context in the user turn
    assert captured["model"] == "claude-opus-5" and captured["output_config"] == {"effort": "high"}
    assert captured["betas"] == ["server-side-fallback-2026-07-01"] and captured["fallbacks"] == "default"
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"} and "never compute money" in captured["system"][0]["text"]
    assert [m["role"] for m in captured["messages"]] == ["user", "assistant", "user"]
    assert "Default scope: state:FL" in captured["messages"][-1]["content"] and "Question: Why was revenue down?" in captured["messages"][-1]["content"]
    assert len(captured["tools"]) == 16 and captured["max_iterations"] == 24


def test_ask_reports_refusal_and_cutoff(session, seed):
    seed_ledger(seed)
    settings = SimpleNamespace(default_scope="", anthropic_api_key="", analyst_model="claude-opus-5", analyst_effort="high", analyst_max_iterations=3)
    refused = [FakeMessage(stop_reason="refusal", model="m", usage=None, content=[], stop_details=SimpleNamespace(category="cyber"))]
    res = ask(session, "x", settings=settings, client=fake_client(refused, {}))
    assert res.answer.startswith("The model declined") and "cyber" in res.answer
    stuck = [FakeMessage(stop_reason="tool_use", model="m", usage=None, content=[FakeBlock(type="tool_use", name="market", input={})])]
    res = ask(session, "x", settings=settings, client=fake_client(stuck, {}))
    assert "without a final answer" in res.answer and res.turns == 1


def test_analyst_route_requires_key(session, engine, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from app import config
    from app.db import get_session
    from app.main import app

    monkeypatch.setattr(config.settings, "anthropic_api_key", "")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    app.dependency_overrides[get_session] = lambda: (yield factory())
    with TestClient(app) as c:
        r = c.post("/api/analyst", json={"question": "hello"})
        assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]
        assert c.post("/api/analyst", json={"question": ""}).status_code == 422
        monkeypatch.setattr(config.settings, "anthropic_api_key", "sk-test")
        monkeypatch.setattr(agent_mod, "_client", lambda api_key=None: fake_client(
            [FakeMessage(stop_reason="end_turn", model="m", usage=None, content=[FakeBlock(type="text", text="42")])], {}))
        r = c.post("/api/analyst", json={"question": "what is the answer", "scope": "state:FL", "history": [{"role": "user", "content": "x"}]})
        assert r.status_code == 200 and r.json()["answer"] == "42"
    app.dependency_overrides.clear()
