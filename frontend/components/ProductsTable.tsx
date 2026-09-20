import type { ProductRow } from "@/lib/api";
import { money, pct } from "@/lib/format";

export default function ProductsTable({ rows, title, hint }: { rows: ProductRow[]; title: string; hint?: string }) {
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
              <th>Product</th>
              <th>Category</th>
              <th className="num">Units</th>
              <th className="num">Revenue</th>
              <th className="num">Gross profit</th>
              <th className="num">Margin</th>
              <th className="num">Discount</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="empty">No sales in this period.</td>
              </tr>
            )}
            {rows.map((r) => (
              <tr key={r.sku}>
                <td>
                  {r.product} <span className="muted mono" style={{ fontSize: 11 }}>{r.sku}</span>
                </td>
                <td className="muted">{r.category}</td>
                <td className="num">{r.units}</td>
                <td className="num">{money(r.revenue)}</td>
                <td className="num">{money(r.gross_profit)}</td>
                <td className={`num ${r.gross_margin < 0.45 ? "warn" : ""}`}>{pct(r.gross_margin)}</td>
                <td className={`num ${r.discount_rate > 0.12 ? "warn" : "muted"}`}>{pct(r.discount_rate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
