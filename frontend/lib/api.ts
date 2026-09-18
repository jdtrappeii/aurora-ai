/** Typed client for the Aurora backend. FastAPI serialises Decimal as JSON numbers. */

export interface PeriodInfo {
  label: string;
  start: string;
  end: string;
  days: number;
}

export interface Summary {
  period: PeriodInfo;
  gross_sales: number;
  discount_total: number;
  discount_rate: number;
  revenue: number;
  cogs: number;
  gross_profit: number;
  gross_margin: number;
  transactions: number;
  units: number;
  avg_transaction_value: number;
  units_per_transaction: number;
  refund_count: number;
  refund_amount: number;
  void_count: number;
  operating_expenses: number;
  operating_profit: number;
  week_start?: string;
  is_partial?: boolean;
}

export interface Delta {
  abs: number;
  pct: number | null;
}

export interface Weekly {
  as_of: string;
  store: string | null;
  is_partial: boolean;
  days_elapsed: number;
  comparison_basis: string;
  current_week: Summary;
  previous_week: Summary;
  four_week_average: Summary & { weeks: number };
  vs_previous_week: Record<string, Delta>;
  vs_four_week_average: Record<string, Delta>;
}

export interface CategoryRow extends Omit<Summary, "period"> {
  category: string;
  revenue_share: number;
}

export interface ProductRow extends Omit<Summary, "period"> {
  sku: string;
  product: string;
  category: string;
  brand: string | null;
  vendor: string | null;
}

export interface InventoryItem {
  store: string;
  sku: string;
  product: string;
  category: string;
  quantity_on_hand: number;
  unit_cost: number;
  inventory_value: number;
  received_date: string | null;
  last_sale_date: string | null;
  age_days: number | null;
  age_bucket: string;
  units_sold_30d: number;
  sell_through_30d: number;
  daily_velocity: number;
  days_of_supply: number | null;
  status: "hot" | "normal" | "slow" | "dead" | "out_of_stock";
}

export interface Inventory {
  as_of: string;
  sku_count: number;
  inventory_value: number;
  aging_value: Record<string, number>;
  cash_tied_over_90_days: number;
  status_counts: Record<string, number>;
  status_value: Record<string, number>;
  watch: InventoryItem[];
  stockout_risk: InventoryItem[];
}

export interface PromotionFeed {
  discount_names: string[];
  applies_to_store: boolean;
  scheduled_days?: number;
  window?: { discount_total: number; revenue: number; units: number; transaction_count: number; days_with_data: number; discount_depth: number; discount_per_day: number; revenue_per_day: number; tickets_per_day: number };
  baseline?: { discount_per_day: number; revenue_per_day: number; tickets_per_day: number; days_with_data: number } | null;
  vs_baseline?: { discount_per_day_pct: number | null; revenue_per_day_pct: number | null; tickets_per_day_pct: number | null } | null;
}

export interface Promotion extends Omit<Summary, "period"> {
  promotion: string;
  weekdays: string | null;
  store_codes: string[];
  audience: string | null;
  source: string;
  feed: PromotionFeed | null;
  start_date: string;
  end_date: string;
  days: number;
  discount_type: string;
  discount_value: number;
  eligible_skus: string[];
  eligible_category: string | null;
  revenue_per_day: number;
  gross_profit_per_day: number;
  units_per_day: number;
  attachment_rate: number;
  baseline: (Omit<Summary, "period"> & { period: PeriodInfo; revenue_per_day: number; gross_profit_per_day: number }) | null;
  vs_baseline: { revenue_per_day_pct: number | null; gross_profit_per_day_pct: number | null; gross_margin_delta: number } | null;
  verdict: "profitable" | "revenue_up_profit_down" | "unprofitable" | "no_baseline";
  explanation: string;
}

export interface ExternalFinding {
  event_id: string;
  event_type: string;
  severity: string;
  source: string;
  description: string | null;
  start_time: string;
  end_time: string;
  distance_km: number | null;
  location_weight: number;
  expected_revenue: number;
  actual_revenue: number;
  expected_transactions: number;
  actual_transactions: number;
  variance: number;
  variance_pct: number | null;
  baseline_samples: number;
  historical_effect_pct: number | null;
  competing_internal_factors: string[];
  evidence_level: "no_material_variance" | "correlation" | "historical_relationship" | "likely_contributor";
  evidence_rule: string;
  confidence: "low" | "medium" | "high";
  estimated_impact: { low: number; high: number; basis: string } | null;
}

export interface WeatherEffect {
  condition: string;
  observations: number;
  mean_variance_pct: number;
  consistent_share: number;
  evidence_level: string;
}

export interface ForecastFactor {
  kind: string;
  condition?: string;
  description?: string | null;
  effect_pct: number | null;
  evidence_level: string;
  observations: number;
}

export interface ForecastDay {
  date: string;
  weekday: string;
  expected_revenue_baseline: number;
  projected_low: number;
  projected_high: number;
  weather: { tags: string[]; alerts: string[]; max_temp_f: number | null; precipitation_in: number } | null;
  factors: ForecastFactor[];
}

export interface External {
  store: string;
  this_week_findings: ExternalFinding[];
  top_findings: ExternalFinding[];
  resilience: { outage_events: number; outage_loss: number; history_days: number; annualised_outage_loss: number };
  weather_effects: WeatherEffect[];
  forecast: ForecastDay[];
}

export interface DiscountCode {
  discount_name: string;
  discount_total: number;
  revenue: number;
  units: number;
  transaction_count: number;
  discount_depth: number;
  share: number;
  previous_discount_total: number | null;
  vs_previous_pct: number | null;
}

export interface Discounts {
  period: PeriodInfo;
  previous_period: PeriodInfo;
  store: string | null;
  total_discounts: number;
  previous_total_discounts: number;
  vs_previous_pct: number | null;
  code_count: number;
  undiscounted: { revenue: number; units: number; transaction_count: number } | null;
  codes: DiscountCode[];
}

export interface MarketWeek {
  week_ending: string;
  market_mg_thc: number;
  market_flower_oz: number;
  market_dispensaries: number;
  operators: number;
  patients: number | null;
  self_operator: string | null;
  self_mg_thc: number | null;
  self_flower_oz: number | null;
  self_dispensaries: number | null;
  share_thc_pct: number | null;
  share_flower_pct: number | null;
}

export interface Market {
  as_of: string;
  current: MarketWeek;
  previous: MarketWeek | null;
  weeks_in_average: number;
  vs_previous_week: {
    market_mg_thc_pct: number | null;
    self_mg_thc_pct: number | null;
    share_thc_bps: number | null;
    share_flower_bps: number | null;
    self_dispensaries_delta: number | null;
    market_dispensaries_delta: number | null;
    patients_delta: number | null;
  };
  vs_four_week_average: { market_mg_thc_pct: number | null; self_mg_thc_pct: number | null; share_thc_bps: number | null; share_flower_bps: number | null };
  read: string | null;
}

export interface PressureOperator {
  operator: string;
  deals: number;
  major: number;
  previous_deals: number;
  vs_previous_pct: number | null;
  latest: string | null;
  sample: string | null;
}

export interface Pressure {
  period: PeriodInfo;
  previous_period: PeriodInfo;
  total_deals: number;
  previous_total_deals: number;
  vs_previous_pct: number | null;
  operators_active: number;
  operators: PressureOperator[];
}

export interface Dashboard {
  as_of: string;
  store: string | null;
  weekly: Weekly;
  trend: Summary[];
  categories: CategoryRow[];
  top_products: ProductRow[];
  four_week_categories: CategoryRow[];
  inventory: Inventory;
  promotions: Promotion[];
  discounts: Discounts;
  market: Market | null;
  pressure: Pressure;
  external: External | null;
}

/** Empty string = same origin (behind the proxy). Unset = local dev against uvicorn. */
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function fetchDashboard(asOf?: string, store?: string): Promise<Dashboard> {
  const params = new URLSearchParams();
  if (asOf) params.set("as_of", asOf);
  if (store) params.set("store", store);
  const qs = params.toString();
  const res = await fetch(`${API_URL}/api/dashboard${qs ? `?${qs}` : ""}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${await res.text()}`);
  return res.json();
}

export interface StoreOption {
  code: string;
  name: string;
  state?: string | null;
}

export interface StoreList {
  default: string | null;
  scopes: StoreOption[];
  stores: StoreOption[];
}

export async function fetchStores(): Promise<StoreList> {
  const res = await fetch(`${API_URL}/api/stores`, { cache: "no-store" });
  if (!res.ok) return { default: null, scopes: [], stores: [] };
  return res.json();
}
