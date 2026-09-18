"use client";

import { useState } from "react";
import { askAnalyst, type AnalystAnswer, type AnalystTurn } from "@/lib/api";

const SUGGESTIONS = [
  "Which stores lost the most gross profit this week, and why?",
  "Did we move with the market or against it?",
  "Which promotion gave away the most this week and did it pay for itself?",
  "What should I watch next week?",
];

/** Ask Aurora's analyst. Every number in an answer comes from the same tools the dashboard uses. */
export default function AnalystPanel({ scope, asOf }: { scope: string; asOf: string }) {
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [turns, setTurns] = useState<{ q: string; a: AnalystAnswer }[]>([]);

  const submit = async (q: string) => {
    if (!q.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const history: AnalystTurn[] = turns.flatMap((t) => [
        { role: "user" as const, content: t.q },
        { role: "assistant" as const, content: t.a.answer },
      ]);
      const a = await askAnalyst(q, scope, asOf, history.slice(-10));
      setTurns((prev) => [...prev, { q, a }]);
      setQuestion("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit(question);
        }}
        style={{ display: "flex", gap: 8 }}
      >
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder={`Ask about ${scope || "all stores"}… e.g. why was Tuesday down`}
          style={{ flex: 1 }}
          aria-label="Question for the analyst"
        />
        <button type="submit" disabled={busy || !question.trim()}>
          {busy ? "Thinking…" : "Ask"}
        </button>
      </form>
      {turns.length === 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 10 }}>
          {SUGGESTIONS.map((s) => (
            <button key={s} type="button" className="button-link" onClick={() => submit(s)} disabled={busy}>
              {s}
            </button>
          ))}
        </div>
      )}
      {error && (
        <div className="error" style={{ marginTop: 10 }}>
          {error}
        </div>
      )}
      {turns.map((t, i) => (
        <div key={i} style={{ marginTop: 16, borderTop: "1px solid var(--border, #e5e7eb)", paddingTop: 12 }}>
          <div style={{ fontWeight: 600, marginBottom: 6 }}>{t.q}</div>
          <div style={{ whiteSpace: "pre-wrap", lineHeight: 1.5 }}>{t.a.answer}</div>
          <details style={{ marginTop: 8 }}>
            <summary className="muted" style={{ cursor: "pointer", fontSize: 12 }}>
              Looked at {t.a.trace.filter((x) => x.tool).length} figures · {t.a.turns} turns · {t.a.model ?? ""}
            </summary>
            <ul className="muted" style={{ fontSize: 12, marginTop: 6 }}>
              {t.a.trace
                .filter((x) => x.tool)
                .map((x, j) => (
                  <li key={j}>
                    {x.tool}
                    {x.input && Object.keys(x.input).length ? ` (${Object.entries(x.input).map(([k, v]) => `${k}=${String(v)}`).join(", ")})` : ""}
                  </li>
                ))}
            </ul>
          </details>
        </div>
      ))}
    </div>
  );
}
