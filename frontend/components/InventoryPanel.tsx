import type { Inventory, InventoryItem } from "@/lib/api";
import { money, n, pct } from "@/lib/format";

const BUCKETS: { key: string; color: string }[] = [
  { key: "0-30", color: "#4fd18b" },
  { key: "31-60", color: "#7aa2f7" },
  { key: "61-90", color: "#f2c14e" },
  { key: "90+", color: "#f0716f" },
  { key: "unknown", color: "#8b98a8" },
];

const STATUS_CLASS: Record<InventoryItem["status"], string> = {
  hot: "good",
  normal: "neutral",
  slow: "warn",
  dead: "bad",
  out_of_stock: "bad",
};

function ItemsTable({ items, empty, showSupply }: { items: InventoryItem[]; empty: string; showSupply: boolean }) {
  if (items.length === 0) return <div className="empty">{empty}</div>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Product</th>
            <th>Status</th>
            <th className="num">On hand</th>
            <th className="num">Value</th>
            <th className="num">Age</th>
            <th className="num">Sold 30d</th>
            <th className="num">Sell-through</th>
            {showSupply && <th className="num">Days supply</th>}
          </tr>
        </thead>
        <tbody>
          {items.map((i) => (
            <tr key={i.sku}>
              <td>
                {i.product} <span className="muted" style={{ fontSize: 11 }}>{i.category}</span>
              </td>
              <td>
                <span className={`badge ${STATUS_CLASS[i.status]}`}>{i.status.replace("_", " ")}</span>
              </td>
              <td className="num">{i.quantity_on_hand}</td>
              <td className="num">{money(i.inventory_value)}</td>
              <td className={`num ${i.age_days != null && i.age_days > 90 ? "neg" : "muted"}`}>{i.age_days == null ? "–" : `${i.age_days}d`}</td>
              <td className="num">{i.units_sold_30d}</td>
              <td className="num">{pct(i.sell_through_30d, 0)}</td>
              {showSupply && <td className={`num ${i.days_of_supply != null && i.days_of_supply < 7 ? "neg" : ""}`}>{i.days_of_supply == null ? "–" : n(i.days_of_supply)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function InventoryPanel({ inv }: { inv: Inventory }) {
  const total = inv.inventory_value || 1;
  const counts = inv.status_counts;
  return (
    <div className="grid two">
      <div className="card">
        <h3>Inventory position · {inv.sku_count} SKUs</h3>
        <div className="kpi">
          <div className="value">{money(inv.inventory_value)}</div>
        </div>
        <div className="aging" title="Inventory value by age">
          {BUCKETS.map((b) => (
            <span key={b.key} style={{ width: `${((inv.aging_value[b.key] ?? 0) / total) * 100}%`, background: b.color }} />
          ))}
        </div>
        <div className="legend">
          {BUCKETS.filter((b) => (inv.aging_value[b.key] ?? 0) > 0).map((b) => (
            <span key={b.key}>
              <i style={{ background: b.color }} />
              {b.key}d {money(inv.aging_value[b.key])}
            </span>
          ))}
        </div>
        <p style={{ margin: "14px 0 6px" }}>
          Cash tied up over 90 days: <b className={`mono ${inv.cash_tied_over_90_days > 0 ? "neg" : ""}`}>{money(inv.cash_tied_over_90_days)}</b>
        </p>
        <div className="chips">
          {(["hot", "normal", "slow", "dead", "out_of_stock"] as const)
            .filter((s) => counts[s])
            .map((s) => (
              <span className="chip" key={s}>
                <span className={`badge ${STATUS_CLASS[s]}`}>{s.replace("_", " ")}</span> <b>{counts[s]}</b> · {money(inv.status_value[s])}
              </span>
            ))}
        </div>
      </div>
      <div className="card">
        <h3>Likely to stock out (under 7 days of supply)</h3>
        <ItemsTable items={inv.stockout_risk} empty="Nothing is at risk of stocking out in the next week." showSupply />
      </div>
      <div className="card" style={{ gridColumn: "1 / -1" }}>
        <h3>Slow and dead stock · stop reordering, bundle, or mark down</h3>
        <ItemsTable items={inv.watch} empty="No slow or dead SKUs." showSupply={false} />
      </div>
    </div>
  );
}
