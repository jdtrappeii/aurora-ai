import type { Market, Pressure } from "@/lib/api";
import { n, pct, shortDate, sign, spct } from "@/lib/format";

const bps = (v: number | null | undefined): string => (v == null ? "–" : `${v >= 0 ? "+" : ""}${v.toFixed(0)} bps`);
const mg = (v: number | null | undefined): string => (v == null ? "–" : `${(v / 1_000_000).toFixed(2)}M mg`);

/** Statewide market from the regulator's weekly report, and competitor promo pressure. */
export default function MarketPanel({ m, p }: { m: Market | null; p: Pressure }) {
  return (
    <div className="grid two">
      <div className="card">
        {!m ? (
          <div className="empty">No market data yet. Run a sheets sync with the market dashboard configured.</div>
        ) : (
          <>
            <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
              Week ending {shortDate(m.current.week_ending)} · {m.current.operators} operators · {n(m.current.patients)} patients
            </div>
            {m.read && <div style={{ fontWeight: 600, marginBottom: 10 }}>{m.read}</div>}
            <div className="kpis">
              <div className="stat">
                <small>Market volume (THC)</small>
                {mg(m.current.market_mg_thc)}
                <div className={sign(m.vs_previous_week.market_mg_thc_pct)}>{spct(m.vs_previous_week.market_mg_thc_pct)} WoW</div>
              </div>
              <div className="stat">
                <small>Our volume (THC)</small>
                {mg(m.current.self_mg_thc)}
                <div className={sign(m.vs_previous_week.self_mg_thc_pct)}>{spct(m.vs_previous_week.self_mg_thc_pct)} WoW</div>
              </div>
              <div className="stat">
                <small>THC share</small>
                {m.current.share_thc_pct == null ? "–" : `${m.current.share_thc_pct.toFixed(2)}%`}
                <div className={sign(m.vs_previous_week.share_thc_bps)}>{bps(m.vs_previous_week.share_thc_bps)} WoW · {bps(m.vs_four_week_average.share_thc_bps)} vs 4wk</div>
              </div>
              <div className="stat">
                <small>Flower share</small>
                {m.current.share_flower_pct == null ? "–" : `${m.current.share_flower_pct.toFixed(2)}%`}
                <div className={sign(m.vs_previous_week.share_flower_bps)}>{bps(m.vs_previous_week.share_flower_bps)} WoW</div>
              </div>
              <div className="stat">
                <small>Dispensaries · ours / state</small>
                {n(m.current.self_dispensaries)} / {n(m.current.market_dispensaries)}
                <div className="muted">
                  state {m.vs_previous_week.market_dispensaries_delta == null ? "–" : `${m.vs_previous_week.market_dispensaries_delta >= 0 ? "+" : ""}${m.vs_previous_week.market_dispensaries_delta}`} WoW
                </div>
              </div>
            </div>
          </>
        )}
      </div>
      <div className="card">
        <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
          Competitor deals observed · {p.total_deals} this period vs {p.previous_total_deals} prior ({spct(p.vs_previous_pct)}) · {p.operators_active} operators active
        </div>
        {p.operators.length === 0 ? (
          <div className="empty">No competitor deals in this period.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Operator</th>
                <th className="num">Deals</th>
                <th className="num">Deep</th>
                <th className="num">vs prior</th>
                <th>Latest</th>
              </tr>
            </thead>
            <tbody>
              {p.operators.map((o) => (
                <tr key={o.operator}>
                  <td>{o.operator}</td>
                  <td className="num">{o.deals}</td>
                  <td className="num" title="40%+ off or BOGO">{o.major}</td>
                  <td className={`num ${sign(o.vs_previous_pct == null ? null : -o.vs_previous_pct)}`}>{spct(o.vs_previous_pct)}</td>
                  <td className="muted" title={o.sample ?? undefined}>
                    {o.latest ? shortDate(o.latest) : "–"}{o.sample ? ` · ${o.sample.length > 48 ? `${o.sample.slice(0, 47)}…` : o.sample}` : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
