"use client";

import { Bar, CartesianGrid, Cell, ComposedChart, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Summary } from "@/lib/api";
import { money, pct, shortDate } from "@/lib/format";

export default function TrendChart({ trend }: { trend: Summary[] }) {
  const data = trend.map((w) => ({
    week: shortDate(w.week_start ?? w.period.start),
    revenue: w.revenue,
    gross_profit: w.gross_profit,
    gross_margin: w.gross_margin,
    partial: w.is_partial ?? false,
  }));
  return (
    <div className="card">
      <ResponsiveContainer width="100%" height={280}>
        <ComposedChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
          <CartesianGrid stroke="#24303f" vertical={false} />
          <XAxis dataKey="week" stroke="#8b98a8" tickLine={false} axisLine={false} fontSize={12} />
          <YAxis yAxisId="money" stroke="#8b98a8" tickLine={false} axisLine={false} fontSize={12} tickFormatter={(v) => money(v)} width={70} />
          <YAxis yAxisId="ratio" orientation="right" domain={[0, 1]} stroke="#8b98a8" tickLine={false} axisLine={false} fontSize={12} tickFormatter={(v) => pct(v, 0)} width={48} />
          <Tooltip
            contentStyle={{ background: "#131a23", border: "1px solid #24303f", borderRadius: 8, fontFamily: "ui-monospace, monospace", fontSize: 12 }}
            formatter={(value: number, name: string) => [name === "Gross margin" ? pct(value) : money(value, true), name]}
            labelFormatter={(label, payload) => `Week of ${label}${payload?.[0]?.payload?.partial ? " (week to date)" : ""}`}
          />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <Bar yAxisId="money" dataKey="revenue" name="Revenue" fill="#7aa2f7" radius={[3, 3, 0, 0]}>
            {data.map((d, i) => (
              <Cell key={i} fillOpacity={d.partial ? 0.45 : 1} />
            ))}
          </Bar>
          <Bar yAxisId="money" dataKey="gross_profit" name="Gross profit" fill="#5ad0a7" radius={[3, 3, 0, 0]}>
            {data.map((d, i) => (
              <Cell key={i} fillOpacity={d.partial ? 0.45 : 1} />
            ))}
          </Bar>
          <Line yAxisId="ratio" type="monotone" dataKey="gross_margin" name="Gross margin" stroke="#f2c14e" strokeWidth={2} dot={false} />
        </ComposedChart>
      </ResponsiveContainer>
      <div className="note">Lighter bars are the week in progress.</div>
    </div>
  );
}
