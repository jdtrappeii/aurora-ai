import type { Discounts } from "@/lib/api";
import { money, n, pct, sign, spct } from "@/lib/format";

/** Discount / promo codes from the aggregate feed (Headset). Each code's dollars
 *  are attributable to that code only, so the rows add up to the total; ticket
 *  counts do not add across codes (a receipt can carry several). */
export default function DiscountsPanel({ d }: { d: Discounts }) {
  if (d.code_count === 0) return <div className="card empty">No discount-code data for this period. Run a Headset sync to populate it.</div>;
  return (
    <div className="card">
      <div className="kpis" style={{ marginBottom: 12 }}>
        <div className="stat">
          <small>Total given away</small>
          {money(d.total_discounts)}
          <div className={sign(d.vs_previous_pct == null ? null : -d.vs_previous_pct)}>{spct(d.vs_previous_pct)} vs prior period</div>
        </div>
        <div className="stat">
          <small>Codes in use</small>
          {d.code_count}
        </div>
        {d.undiscounted && (
          <div className="stat">
            <small>Sold at full price</small>
            {money(d.undiscounted.revenue)}
            <div className="muted">{n(d.undiscounted.units)} units · {n(d.undiscounted.transaction_count)} tickets</div>
          </div>
        )}
      </div>
      <table>
        <thead>
          <tr>
            <th>Code</th>
            <th className="num">Discount $</th>
            <th className="num">Share</th>
            <th className="num">Depth</th>
            <th className="num">Item revenue</th>
            <th className="num">Units</th>
            <th className="num">Tickets</th>
            <th className="num">vs prior</th>
          </tr>
        </thead>
        <tbody>
          {d.codes.map((c) => (
            <tr key={c.discount_name}>
              <td title={c.discount_name}>{c.discount_name.length > 56 ? `${c.discount_name.slice(0, 55)}…` : c.discount_name}</td>
              <td className="num">{money(c.discount_total)}</td>
              <td className="num">{pct(c.share, 0)}</td>
              <td className="num" title="discount / (item revenue + discount)">{pct(c.discount_depth, 0)}</td>
              <td className="num">{money(c.revenue)}</td>
              <td className="num">{n(c.units)}</td>
              <td className="num">{n(c.transaction_count)}</td>
              <td className={`num ${sign(c.vs_previous_pct == null ? null : -c.vs_previous_pct)}`}>{spct(c.vs_previous_pct)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
