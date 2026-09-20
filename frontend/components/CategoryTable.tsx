import type { CategoryRow } from "@/lib/api";
import { money, pct } from "@/lib/format";

export default function CategoryTable({ rows, title, hint }: { rows: CategoryRow[]; title: string; hint?: string }) {
  return (
    <div className="card">
      <div className="section-head">
        <h2 style={{ fontSize: 13 }}>{title}</h2>
        {hint && <span className="hint">{hint}</span>}
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Category</th>
              <th className="num">Revenue</th>
              <th className="num">Share</th>
              <th className="num">Gross profit</th>
              <th className="num">Margin</th>
              <th className="num">Discount</th>
              <th className="num">Units</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="empty">No sales in this period.</td>
              </tr>
            )}
            {rows.map((r) => (
              <tr key={r.category}>
                <td>{r.category}</td>
                <td className="num">{money(r.revenue)}</td>
                <td className="num muted">{pct(r.revenue_share)}</td>
                <td className="num">{money(r.gross_profit)}</td>
                <td className={`num ${r.gross_margin < 0.45 ? "warn" : ""}`}>{pct(r.gross_margin)}</td>
                <td className={`num ${r.discount_rate > 0.12 ? "warn" : "muted"}`}>{pct(r.discount_rate)}</td>
                <td className="num muted">{r.units}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
