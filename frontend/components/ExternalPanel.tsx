import type { External, ExternalFinding, ForecastDay } from "@/lib/api";
import { money, pct, shortDate, shortDateTime, smoney, spct, titleCase } from "@/lib/format";

const EVIDENCE: Record<ExternalFinding["evidence_level"], { label: string; cls: string }> = {
  likely_contributor: { label: "Likely contributor", cls: "good" },
  historical_relationship: { label: "Historical relationship", cls: "info" },
  correlation: { label: "Correlation only", cls: "warn" },
  no_material_variance: { label: "No material variance", cls: "neutral" },
};

function Finding({ f }: { f: ExternalFinding }) {
  const e = EVIDENCE[f.evidence_level];
  const varCls = f.variance >= 0 ? "pos" : "neg";
  const sameDay = f.start_time.slice(0, 10) === f.end_time.slice(0, 10);
  return (
    <div className="finding">
      <div>
        <div className="what">
          {f.description ?? titleCase(f.event_type)} <span className={`badge ${e.cls}`} style={{ marginLeft: 6 }}>{e.label}</span>{" "}
          <span className="badge neutral">{f.confidence} confidence</span>
        </div>
        <div className="when">
          {titleCase(f.event_type)} · {f.severity} · {shortDateTime(f.start_time)} → {sameDay ? shortDateTime(f.end_time).split(", ")[1] ?? shortDateTime(f.end_time) : shortDateTime(f.end_time)}
          {f.distance_km != null && ` · ${f.distance_km} km away`}
          {f.competing_internal_factors.length > 0 && ` · promotion in window: ${f.competing_internal_factors.join(", ")}`}
        </div>
      </div>
      <div className="nums">
        <div>
          expected {money(f.expected_revenue)} · actual {money(f.actual_revenue)}
        </div>
        <div className={varCls}>
          {smoney(f.variance)} ({spct(f.variance_pct)})
        </div>
        {f.estimated_impact && f.estimated_impact.low !== f.estimated_impact.high && (
          <div className="muted" style={{ fontSize: 12 }}>
            est. {smoney(f.estimated_impact.low)} to {smoney(f.estimated_impact.high)}
          </div>
        )}
        {f.historical_effect_pct != null && (
          <div className="muted" style={{ fontSize: 12 }}>
            history: {spct(f.historical_effect_pct)}
          </div>
        )}
      </div>
      <div className="rule">{f.evidence_rule}</div>
    </div>
  );
}

function ForecastRow({ d }: { d: ForecastDay }) {
  const range = d.projected_low === d.projected_high ? money(d.projected_low) : `${money(d.projected_low)} – ${money(d.projected_high)}`;
  const shift = d.projected_low !== d.expected_revenue_baseline || d.projected_high !== d.expected_revenue_baseline;
  return (
    <tr>
      <td>
        {d.weekday.slice(0, 3)} <span className="muted">{shortDate(d.date)}</span>
      </td>
      <td className="muted">
        {d.weather ? d.weather.tags.map(titleCase).join(", ") : "–"}
        {d.weather?.alerts.length ? ` · ${d.weather.alerts.join(", ")}` : ""}
      </td>
      <td className="num">{money(d.expected_revenue_baseline)}</td>
      <td className={`num ${shift ? (d.projected_high < d.expected_revenue_baseline ? "neg" : "pos") : "muted"}`}>{range}</td>
      <td>
        {d.factors.length === 0 && <span className="muted">–</span>}
        {d.factors.map((f, i) => (
          <span key={i} className={`badge ${f.effect_pct == null ? "neutral" : f.effect_pct < 0 ? "bad" : "good"}`} style={{ marginRight: 4 }}>
            {f.condition ? titleCase(f.condition) : f.description ?? titleCase(f.kind)}
            {f.effect_pct != null ? ` ${spct(f.effect_pct, 0)}` : ` · ${f.observations} obs, no effect claimed`}
          </span>
        ))}
      </td>
    </tr>
  );
}

export default function ExternalPanel({ ext }: { ext: External }) {
  return (
    <div className="grid">
      {ext.this_week_findings.length > 0 && (
        <div className="card">
          <h3>Outside conditions that moved this week</h3>
          {ext.this_week_findings.map((f) => (
            <Finding key={f.event_id} f={f} />
          ))}
        </div>
      )}
      <div className="grid two">
        <div className="card">
          <h3>Largest external findings on record</h3>
          {ext.top_findings.length === 0 && <div className="empty">No external events matched this store.</div>}
          {ext.top_findings.map((f) => (
            <Finding key={f.event_id} f={f} />
          ))}
        </div>
        <div className="grid" style={{ alignContent: "start" }}>
          <div className="card">
            <h3>What this store has learned about weather</h3>
            {ext.weather_effects.length === 0 && <div className="empty">Not enough weather history yet.</div>}
            <div className="chips">
              {ext.weather_effects.map((w) => (
                <span className="chip" key={w.condition} title={`${w.observations} days, ${pct(w.consistent_share, 0)} moved the same way`}>
                  {titleCase(w.condition)} <b className={w.mean_variance_pct < 0 ? "neg" : "pos"}>{spct(w.mean_variance_pct, 0)}</b>{" "}
                  <span className="muted">
                    n={w.observations} · {w.evidence_level === "historical_relationship" ? "relationship" : w.evidence_level === "correlation" ? "correlation" : "noise"}
                  </span>
                </span>
              ))}
            </div>
          </div>
          <div className="card">
            <h3>Resilience</h3>
            {ext.resilience.outage_events === 0 ? (
              <div className="muted">No power or connectivity outages on record.</div>
            ) : (
              <div>
                {ext.resilience.outage_events} outage{ext.resilience.outage_events === 1 ? "" : "s"} cost{" "}
                <b className="mono neg">{money(ext.resilience.outage_loss)}</b> in revenue over {ext.resilience.history_days} days —{" "}
                about <b className="mono neg">{money(ext.resilience.annualised_outage_loss)}</b> a year at that rate. Compare that with the price of backup power or cellular failover.
              </div>
            )}
          </div>
        </div>
      </div>
      <div className="card">
        <h3>Next 7 days · forecasts and scheduled events, not facts</h3>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Day</th>
                <th>Forecast weather</th>
                <th className="num">Normal revenue</th>
                <th className="num">Projected</th>
                <th>Factors</th>
              </tr>
            </thead>
            <tbody>
              {ext.forecast.map((d) => (
                <ForecastRow key={d.date} d={d} />
              ))}
            </tbody>
          </table>
        </div>
        <div className="note">Projections apply only effects this store has shown at least three times. Anything else is listed but not priced in.</div>
      </div>
    </div>
  );
}
