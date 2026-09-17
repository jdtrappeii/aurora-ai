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
        const DAYS = ["", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
        const days = p.weekdays ? p.weekdays.split(",").map((d) => DAYS[Number(d)]).join("/") : "daily";
        const stores = p.store_codes.length ? `${p.store_codes.length} store${p.store_codes.length > 1 ? "s" : ""}` : "all stores";
        const feed = p.feed && p.feed.applies_to_store && p.feed.window ? p.feed : null;
        return (
          <div className="card promo" key={p.promotion}>
            <div>
              <div style={{ fontWeight: 600, fontSize: 15 }}>{p.promotion}</div>
              <div className="muted" style={{ fontSize: 12, marginTop: 3 }}>
                {shortDate(p.start_date)} – {shortDate(p.end_date)} · {p.days}d · {p.discount_type} {p.discount_value}
                {p.discount_type === "percent" ? "%" : ""} · {scope} · {days} · {stores}
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
            {feed && (
              <div className="stat">
                <small>From the POS feed · {feed.window!.days_with_data} of {feed.scheduled_days} days</small>
                {money(feed.window!.discount_per_day)} <span className="muted">given away / day</span>
                <div className="muted">
                  {money(feed.window!.revenue_per_day)} item revenue / day · {pct(feed.window!.discount_depth, 0)} depth
                  {feed.vs_baseline ? ` · revenue ${spct(feed.vs_baseline.revenue_per_day_pct)} vs before` : ""}
                </div>
              </div>
            )}
            <p>{p.explanation}</p>
          </div>
        );
      })}
    </div>
  );
}
