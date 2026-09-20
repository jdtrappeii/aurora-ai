"""Run one analyst question through Claude with Aurora's read-only tools.

    result = ask(session, "Why was Pace down on Tuesday?", scope="state:FL")
    result["answer"], result["trace"], result["usage"]

The model is claude-opus-5 with adaptive thinking (the model's default) at
`ANALYST_EFFORT`, and server-side refusal fallbacks enabled. The system
prompt is stable so it caches; the volatile part (today's date, default
scope) goes in the user turn. The SDK's tool runner drives the loop; we
mirror each turn to build a trace of what was looked at, and stop after
`ANALYST_MAX_ITERATIONS` model turns.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.analyst.tools import build_tools

SYSTEM_PROMPT = """You are Aurora's analyst for a multi-store cannabis retailer. You answer the owner's questions about profitability using only the tools provided. The tools run Aurora's deterministic analytics over stored data: every figure they return is a sum, mean or ratio of stored rows, and you never compute money yourself or estimate a figure the tools did not return.

How to work:
- Start with data_coverage when a question is about a drop, a change, or a comparison, so you know whether a day lacks product detail before you explain it.
- Use list_stores to resolve a store named by city or nickname to its code. A scope is a store code, "state:FL" for a state, or omitted for the default.
- Prefer the smallest tool that answers the question. Do not call the same tool twice with the same arguments.
- External findings carry an evidence level. Report it as given: "correlation" means the two things happened together and nothing more; only "likely_contributor" may be described as a probable cause. Never promote a correlation to a cause.
- When the tools cannot answer, say what is missing and which source would supply it.

How to answer:
- Lead with the answer in one or two sentences, with the numbers that support it.
- Then the evidence: the figures you relied on, each with its period and scope.
- Then caveats: partial weeks, missing detail days, weak evidence.
- Plain language for an operator. No headers. Dollar figures rounded to the dollar unless cents matter; percentages to one decimal."""


@dataclass
class AnalystResult:
    answer: str
    trace: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    stop_reason: str | None = None
    model: str | None = None
    turns: int = 0

    def to_dict(self) -> dict:
        return {"answer": self.answer, "trace": self.trace, "usage": self.usage, "stop_reason": self.stop_reason, "model": self.model, "turns": self.turns}


def _client(api_key: str | None = None):
    import anthropic

    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


def ask(session: Session, question: str, scope: str | None = None, as_of: date | None = None, history: list[dict] | None = None,
        settings=None, client: Any = None) -> AnalystResult:
    """One question. `history` is prior turns as [{"role": "user"|"assistant", "content": text}]."""
    from app.api.routes import resolve_as_of
    from app.config import settings as default_settings

    settings = settings or default_settings
    as_of = resolve_as_of(session, as_of)
    scope = scope or settings.default_scope or None
    tools = build_tools(session, scope, as_of)
    client = client or _client(settings.anthropic_api_key or None)

    context = (f"Today is {date.today().isoformat()}. The latest sales data is dated {as_of.isoformat()}; "
               f"treat that as 'this week' unless the question names a date. Default scope: {scope or 'all stores'}.")
    messages = [{"role": m["role"], "content": m["content"]} for m in (history or []) if m.get("role") in ("user", "assistant") and m.get("content")]
    messages.append({"role": "user", "content": f"{context}\n\nQuestion: {question}"})

    runner = client.beta.messages.tool_runner(
        model=settings.analyst_model,
        max_tokens=16000,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=tools,
        messages=messages,
        max_iterations=settings.analyst_max_iterations,
        output_config={"effort": settings.analyst_effort},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )

    result = AnalystResult(answer="")
    usage_in = usage_out = cache_read = 0
    last = None
    for message in runner:
        last = message
        result.turns += 1
        if getattr(message, "usage", None):
            usage_in += message.usage.input_tokens or 0
            usage_out += message.usage.output_tokens or 0
            cache_read += getattr(message.usage, "cache_read_input_tokens", 0) or 0
        for block in message.content:
            if block.type == "tool_use":
                result.trace.append({"tool": block.name, "input": block.input})
            elif block.type == "text" and block.text.strip():
                result.trace.append({"note": block.text.strip()[:400]})
    if last is not None:
        result.stop_reason = last.stop_reason
        result.model = getattr(last, "model", None)
        if last.stop_reason == "refusal":
            details = getattr(last, "stop_details", None)
            result.answer = "The model declined to answer this question" + (f" ({details.category})." if details and getattr(details, "category", None) else ".")
        elif last.stop_reason == "max_tokens":
            result.answer = "".join(b.text for b in last.content if b.type == "text") + "\n\n[answer cut off: max_tokens]"
        else:
            result.answer = "\n".join(b.text for b in last.content if b.type == "text").strip()
        if last.stop_reason == "tool_use":
            result.answer = (result.answer + "\n\n" if result.answer else "") + f"[stopped after {result.turns} tool turns without a final answer]"
    result.usage = {"input_tokens": usage_in, "output_tokens": usage_out, "cache_read_input_tokens": cache_read}
    return result


def trace_text(trace: list[dict]) -> str:
    lines = []
    for t in trace:
        if "tool" in t:
            args = ", ".join(f"{k}={v}" for k, v in (t["input"] or {}).items())
            lines.append(f"  → {t['tool']}({args})")
        elif "note" in t:
            lines.append(f"  · {t['note']}")
    return "\n".join(lines)
