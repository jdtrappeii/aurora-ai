const usd0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const usd2 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const num = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

export const money = (v: number | null | undefined, cents = false): string =>
  v == null ? "–" : (cents ? usd2 : usd0).format(v);

export const n = (v: number | null | undefined): string => (v == null ? "–" : num.format(v));

/** 0.1234 -> "12.3%" */
export const pct = (v: number | null | undefined, digits = 1): string =>
  v == null ? "–" : `${(v * 100).toFixed(digits)}%`;

/** Signed percentage change: 0.1234 -> "+12.3%" */
export const spct = (v: number | null | undefined, digits = 1): string =>
  v == null ? "–" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(digits)}%`;

/** Signed percentage points for ratio deltas: 0.0051 -> "+0.5 pts" */
export const pts = (v: number | null | undefined): string =>
  v == null ? "–" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)} pts`;

export const smoney = (v: number | null | undefined): string =>
  v == null ? "–" : `${v >= 0 ? "+" : "−"}${usd0.format(Math.abs(v))}`;

export const sign = (v: number | null | undefined): "pos" | "neg" | "muted" =>
  v == null || v === 0 ? "muted" : v > 0 ? "pos" : "neg";

export const shortDate = (iso: string): string => {
  const d = new Date(iso.length === 10 ? `${iso}T00:00:00` : iso);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
};

export const shortDateTime = (iso: string): string => {
  const d = new Date(iso);
  return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
};

export const titleCase = (s: string): string => s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
