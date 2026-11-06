import { useEffect, useMemo, useState } from "react";
import { AgStudio, AgStudioProvider } from "ag-studio-react";
import type { AgReportState, AgStudioMode } from "ag-studio";
import { api } from "../api";
import type { Ctx } from "../App";
import { Badge, ErrorNotice, Spinner, useAction } from "../ui";

type Rows = Record<string, Record<string, string | number | null>[]>;
type Fmt = "textFormat" | "integerFormat" | "currencyFormat";

// Declared fields: sources may be empty for a fresh session, and names read better in edit mode.
const SCHEMA: Record<string, { name: string; fields: [string, string, Fmt][] }> = {
  offers: { name: "Supplier offers", fields: [["supplier", "Supplier", "textFormat"], ["negotiated", "Pricing", "textFormat"],
    ["total_usd", "Delivered total (USD)", "currencyFormat"], ["discount_usd", "Discount (USD)", "currencyFormat"],
    ["shipping_usd", "Shipping (USD)", "currencyFormat"], ["quote_status", "Quote status", "textFormat"], ["delivery_date", "Delivery date", "textFormat"]] },
  savings: { name: "Proposals", fields: [["proposal_version", "Version", "integerFormat"], ["supplier", "Supplier", "textFormat"],
    ["total_usd", "Total (USD)", "currencyFormat"], ["savings_usd", "Savings vs list (USD)", "currencyFormat"],
    ["budget_headroom_usd", "Under budget by (USD)", "currencyFormat"], ["status", "Status", "textFormat"]] },
  purchases: { name: "Purchases", fields: [["day", "Day", "textFormat"], ["kind", "Kind", "textFormat"], ["counterparty", "Paid for", "textFormat"],
    ["status", "Status", "textFormat"], ["amount_usd", "Amount (USD)", "currencyFormat"]] },
  tool_usage: { name: "Paid tool calls", fields: [["tool", "Tool", "textFormat"], ["provider", "Provider", "textFormat"],
    ["cents", "Price (¢)", "integerFormat"], ["calls", "Calls", "integerFormat"], ["settled", "Payout", "textFormat"]] },
  disputes: { name: "Disputes", fields: [["reason", "Reason", "textFormat"], ["status", "Status", "textFormat"], ["amount_usd", "Amount (USD)", "currencyFormat"]] },
  inventory: { name: "Inventory", fields: [["sku", "SKU", "textFormat"], ["category", "Category", "textFormat"], ["on_hand", "On hand", "integerFormat"],
    ["incoming", "Incoming", "integerFormat"], ["need", "Need", "integerFormat"], ["status", "Stock status", "textFormat"]] },
};

// Licence from the environment (45-day AG Studio trial). Without it Studio shows a watermark and console warnings.
const LICENSE_KEY = import.meta.env.VITE_AG_STUDIO_LICENSE_KEY as string | undefined;

const title = (text: string) => ({ title: { enabled: true, text } });
const kpi = (id: string, field: string, aggregation: "sum" | "count" | "avg", text: string) => ({
  [id]: { type: "value" as const, dataMapping: { value: [{ id: field, aggregation }] }, format: title(text) },
});

// Prebuilt report so the dashboard is useful on first load; analysts can switch to edit mode and change it.
const REPORT: AgReportState = {
  selectedPageId: "overview",
  pages: [
    {
      id: "overview",
      widgets: {
        ...kpi("k-savings", "savings.savings_usd", "sum", "Negotiated savings (USD)"),
        ...kpi("k-volume", "purchases.amount_usd", "sum", "PayPal volume (USD)"),
        ...kpi("k-tools", "tool_usage.cents", "sum", "Agent tool spend (¢)"),
        ...kpi("k-calls", "tool_usage.calls", "sum", "Paid tool calls"),
        offers: {
          type: "column-chart-grouped",
          dataMapping: {
            categoryKey: [{ id: "offers.supplier" }],
            valueKey: [{ id: "offers.total_usd", aggregation: "min" }],
            legendKey: [{ id: "offers.negotiated" }],
          },
          format: title("Best delivered quote by supplier (USD)"),
        },
        spend: {
          type: "donut-chart",
          dataMapping: { categoryKey: [{ id: "tool_usage.provider" }], valueKey: [{ id: "tool_usage.cents", aggregation: "sum" }] },
          format: title("Pay-per-call spend by provider (¢)"),
        },
        needs: {
          type: "column-chart-grouped",
          dataMapping: {
            categoryKey: [{ id: "inventory.sku" }],
            valueKey: [{ id: "inventory.need", aggregation: "sum" }, { id: "inventory.incoming", aggregation: "sum" }],
          },
          format: title("Restock need vs paid incoming units"),
        },
        purchases: {
          type: "column-chart-stacked",
          dataMapping: {
            categoryKey: [{ id: "purchases.kind" }],
            valueKey: [{ id: "purchases.amount_usd", aggregation: "sum" }],
            legendKey: [{ id: "purchases.status" }],
          },
          format: title("PayPal volume by purchase type and status (USD)"),
        },
      },
      widgetLayout: {
        "k-savings": { xTrack: 0, yTrack: 0, xSpan: 6, ySpan: 6 },
        "k-volume": { xTrack: 6, yTrack: 0, xSpan: 6, ySpan: 6 },
        "k-tools": { xTrack: 12, yTrack: 0, xSpan: 6, ySpan: 6 },
        "k-calls": { xTrack: 18, yTrack: 0, xSpan: 6, ySpan: 6 },
        offers: { xTrack: 0, yTrack: 6, xSpan: 12, ySpan: 18 },
        spend: { xTrack: 12, yTrack: 6, xSpan: 12, ySpan: 18 },
        needs: { xTrack: 0, yTrack: 24, xSpan: 12, ySpan: 18 },
        purchases: { xTrack: 12, yTrack: 24, xSpan: 12, ySpan: 18 },
      },
    },
  ],
};

export default function InsightsPage({ ctx }: { ctx: Ctx }) {
  const [rows, setRows] = useState<Rows | null>(null);
  const [mode, setMode] = useState<AgStudioMode>("view");
  const load = useAction();

  useEffect(() => {
    load.run(async () => setRows((await api.get<{ sources: Rows }>("/api/analytics")).sources));
  }, [load.run]);

  const data = useMemo(
    () => rows && {
      sources: Object.entries(SCHEMA).map(([id, def]) => ({
        id,
        name: def.name,
        data: rows[id] ?? [],
        fields: def.fields.map(([fid, name, format]) => ({ id: fid, name, format })),
      })),
    },
    [rows],
  );

  return (
    <section className="panel stack" aria-labelledby="ins-h">
      <div className="panel-head" style={{ marginBottom: 0 }}>
        <h1 id="ins-h">Insights</h1>
        <Badge tone="info">AG Studio</Badge>
        {!LICENSE_KEY && <Badge tone="warn" title="Set VITE_AG_STUDIO_LICENSE_KEY">trial licence not set (watermark)</Badge>}
        <span className="spacer" />
        <span className="small muted">Live data from this session · {ctx.config.payment_mode === "fixture" ? "offline fixture payments" : "PayPal Sandbox"}</span>
        <button className="btn small" aria-pressed={mode === "edit"} onClick={() => setMode(mode === "edit" ? "view" : "edit")}>
          {mode === "edit" ? "Done editing" : "Edit dashboard"}
        </button>
      </div>
      <ErrorNotice error={load.error} />
      {!data ? (
        <Spinner label="Loading analytics…" />
      ) : (
        <div style={{ height: "calc(100vh - 170px)", minHeight: 560 }}>
          <AgStudioProvider licenseKey={LICENSE_KEY}>
            <AgStudio data={data} mode={mode} initialState={REPORT} />
          </AgStudioProvider>
        </div>
      )}
    </section>
  );
}
