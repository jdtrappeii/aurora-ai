import type { Weekly } from "@/lib/api";
import { money, n, pct, pts, sign, spct } from "@/lib/format";

type Kind = "money" | "count" | "ratio";

const KPIS: { key: keyof Weekly["current_week"]; label: string; kind: Kind; higherIsBetter: boolean }[] = [
  { key: "revenue", label: "Revenue", kind: "money", higherIsBetter: true },
  { key: "gross_profit", label: "Gross profit", kind: "money", higherIsBetter: true },
  { key: "gross_margin", label: "Gross margin", kind: "ratio", higherIsBetter: true },
  { key: "transactions", label: "Transactions", kind: "count", higherIsBetter: true },
  { key: "avg_transaction_value", label: "Avg ticket", kind: "money", higherIsBetter: true },
  { key: "discount_rate", label: "Discount rate", kind: "ratio", higherIsBetter: false },
  { key: "operating_expenses", label: "Operating expenses", kind: "money", higherIsBetter: false },
  { key: "operating_profit", label: "Operating profit", kind: "money", higherIsBetter: true },
];

function fmtValue(v: number, kind: Kind) {
  if (kind === "money") return money(v, true);
  if (kind === "ratio") return pct(v);
  return n(v);
}

function Delta({ label, kind, abs, pctChange, higherIsBetter }: { label: string; kind: Kind; abs: number; pctChange: number | null; higherIsBetter: boolean }) {
  const direction = sign(abs);
  const cls = direction === "muted" ? "muted" : (abs > 0) === higherIsBetter ? "pos" : "neg";
  const text = kind === "ratio" ? pts(abs) : spct(pctChange);
  return (
    <span>
      {label} <b className={cls}>{text}</b>
    </span>
  );
}

export default function KpiGrid({ weekly }: { weekly: Weekly }) {
  return (
    <div className="grid kpis">
      {KPIS.map((k) => {
        const value = weekly.current_week[k.key] as number;
        const vp = weekly.vs_previous_week[k.key];
        const v4 = weekly.vs_four_week_average[k.key];
        return (
          <div className="card kpi" key={k.key}>
            <h3>{k.label}</h3>
            <div className="value">{fmtValue(value, k.kind)}</div>
            <div className="deltas">
              <Delta label="vs last wk" kind={k.kind} abs={vp.abs} pctChange={vp.pct} higherIsBetter={k.higherIsBetter} />
              <Delta label="vs 4-wk avg" kind={k.kind} abs={v4.abs} pctChange={v4.pct} higherIsBetter={k.higherIsBetter} />
            </div>
          </div>
        );
      })}
    </div>
  );
}
