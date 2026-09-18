"use client";

import { useCallback, useEffect, useState } from "react";
import { API_URL, fetchDashboard, fetchStores, type Dashboard } from "@/lib/api";
import { shortDate } from "@/lib/format";
import KpiGrid from "@/components/KpiGrid";
import TrendChart from "@/components/TrendChart";
import CategoryTable from "@/components/CategoryTable";
import ProductsTable from "@/components/ProductsTable";
import InventoryPanel from "@/components/InventoryPanel";
import PromotionsPanel from "@/components/PromotionsPanel";
import DiscountsPanel from "@/components/DiscountsPanel";
import MarketPanel from "@/components/MarketPanel";
import ExternalPanel from "@/components/ExternalPanel";

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section className="section">
      <div className="section-head">
        <h2>{title}</h2>
        {hint && <span className="hint">{hint}</span>}
      </div>
      {children}
    </section>
  );
}

export default function Page() {
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [asOf, setAsOf] = useState("");
  const [store, setStore] = useState("");
  const [stores, setStores] = useState<{ code: string; name: string }[]>([]);

  const load = useCallback(async (asOfValue: string, storeValue: string) => {
    setLoading(true);
    setError(null);
    try {
      const d = await fetchDashboard(asOfValue || undefined, storeValue || undefined);
      setData(d);
      if (!asOfValue) setAsOf(d.as_of);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchStores().then(setStores).catch(() => setStores([]));
    load("", "");
  }, [load]);

  const w = data?.weekly;
  const cur = w?.current_week.period;

  return (
    <main className="shell">
      <header className="masthead">
        <div>
          <h1>
            <span>Aurora</span> AI
          </h1>
          <div className="sub">
            {cur ? (
              <>
                Week of {shortDate(cur.start)} – {shortDate(cur.end)} · {w?.comparison_basis}
                {data?.store ? ` · ${data.store}` : stores.length > 1 ? " · all stores" : ""}
              </>
            ) : (
              "Profitability intelligence"
            )}
          </div>
        </div>
        <form
          className="controls"
          onSubmit={(e) => {
            e.preventDefault();
            load(asOf, store);
          }}
        >
          <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} aria-label="As of date" />
          {stores.length > 1 && (
            <select value={store} onChange={(e) => setStore(e.target.value)} aria-label="Store">
              <option value="">All stores</option>
              {stores.map((s) => (
                <option key={s.code} value={s.code}>
                  {s.name}
                </option>
              ))}
            </select>
          )}
          <button type="submit" disabled={loading}>
            {loading ? "Loading…" : "Refresh"}
          </button>
          <a
            className="button-link"
            href={`${API_URL}/api/report/weekly?format=html${asOf ? `&as_of=${asOf}` : ""}${store ? `&store=${store}` : ""}`}
            target="_blank"
            rel="noreferrer"
          >
            Weekly report
          </a>
        </form>
      </header>

      {error && (
        <div className="error">
          Could not reach the Aurora API: {error}
          <div className="note">Start the backend with <code>uvicorn app.main:app --reload</code> and import data with <code>python -m app.cli import-dir ../sample_data</code>.</div>
        </div>
      )}

      {data && w && (
        <>
          <Section title="This week" hint={w.is_partial ? `${w.days_elapsed} of 7 days · compared with the same weekdays` : "full week · Mon–Sun"}>
            <KpiGrid weekly={w} />
          </Section>

          <Section title="Twelve weeks" hint="revenue, gross profit and margin by week">
            <TrendChart trend={data.trend} />
          </Section>

          <Section title="Where the profit comes from">
            <div className="grid two">
              <CategoryTable rows={data.categories} title="Categories · this week" />
              <CategoryTable rows={data.four_week_categories} title="Categories · previous 4 weeks" hint="for comparison" />
            </div>
            <div style={{ marginTop: 12 }}>
              <ProductsTable rows={data.top_products} title="Top products by gross profit · this week" />
            </div>
          </Section>

          <Section title="Inventory" hint={`as of ${shortDate(data.inventory.as_of)} · latest snapshot`}>
            <InventoryPanel inv={data.inventory} />
          </Section>

          <Section title="Promotions · deal autopsy" hint="each promotion vs the 28 days before it">
            <PromotionsPanel promos={data.promotions} />
          </Section>

          {data.discounts && (
            <Section title="Discount codes" hint="this week · what each code cost and how deep it cut">
              <DiscountsPanel d={data.discounts} />
            </Section>
          )}

          {data.pressure && (
            <Section title="The market" hint="state weekly report · competitor deals observed">
              <MarketPanel m={data.market} p={data.pressure} />
            </Section>
          )}

          {data.external && (
            <Section title="Outside the four walls" hint={`external intelligence · ${data.external.store}`}>
              <ExternalPanel ext={data.external} />
            </Section>
          )}
        </>
      )}

      {!data && !error && <div className="empty">Loading…</div>}
    </main>
  );
}
