import type { Promotion } from "@/lib/api";
import { money, pct, pts, shortDate, spct } from "@/lib/format";

const VERDICT: Record<Promotion["verdict"], { label: string; cls: string }> = {
  profitable: { label: "Profitable · repeat", cls: "good" },
  revenue_up_profit_down: { label: "Revenue up, profit down · narrow", cls: "warn" },
  unprofitable: { label: "Unprofitable · discontinue", cls: "bad" },
  no_baseline: { label: "No baseline", cls: "neutral" },
};

export default function PromotionsPanel({ promos }: { promos: Promotion[] }) {
  if (promos.length === 0) return <div className="card empty">No promotions imported.</div>;
  return (
    <div className="grid">
      {promos.map((p) => {
        const v = VERDICT[p.verdict];
        const scope = p.eligible_category ?? (p.eligible_skus.length ? `${p.eligible_skus.length} SKUs` : "all");
        return (
          <div className="card promo" key={p.promotion}>
            <div>
              <div style={{ fontWeight: 600, fontSize: 15 }}>{p.promotion}</div>
              <div className="muted" style={{ fontSize: 12, marginTop: 3 }}>
                {shortDate(p.start_date)} – {shortDate(p.end_date)} · {p.days}d · {p.discount_type} {p.discount_value}
                {p.discount_type === "percent" ? "%" : ""} · {scope}
              </div>
              <div style={{ marginTop: 8 }}>
                <span className={`badge ${v.cls}`}>{v.label}</span>
              </div>
            </div>
            <div className="stat">
              <small>Revenue / day vs baseline</small>
              {money(p.revenue_per_day)} <span className="muted">vs {money(p.baseline?.revenue_per_day)}</span>
              <div className={p.vs_baseline && (p.vs_baseline.revenue_per_day_pct ?? 0) >= 0 ? "pos" : "neg"}>{spct(p.vs_baseline?.revenue_per_day_pct)}</div>
            </div>
            <div className="stat">
              <small>Gross profit / day vs baseline</small>
              {money(p.gross_profit_per_day)} <span className="muted">vs {money(p.baseline?.gross_profit_per_day)}</span>
              <div className={p.vs_baseline && (p.vs_baseline.gross_profit_per_day_pct ?? 0) > 0 ? "pos" : "neg"}>{spct(p.vs_baseline?.gross_profit_per_day_pct)}</div>
            </div>
            <div className="stat">
              <small>Margin · discount given · attachment</small>
              {pct(p.gross_margin)} <span className={p.vs_baseline && p.vs_baseline.gross_margin_delta < 0 ? "neg" : "pos"}>({pts(p.vs_baseline?.gross_margin_delta)})</span>
              <div className="muted">
                {money(p.discount_total)} · {pct(p.attachment_rate, 0)} of tickets
              </div>
            </div>
            <p>{p.explanation}</p>
          </div>
        );
      })}
    </div>
  );
}
