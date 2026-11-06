import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import type { ColDef, RowClassParams } from "ag-grid-community";
import { api } from "../api";
import { go, type Ctx } from "../App";
import { gridTheme, safeCsvCell } from "../grid";
import type { ImportPreview, Inventory, InventoryLine, Procurement } from "../types";
import { Badge, ErrorNotice, Spinner, useAction } from "../ui";

const SAMPLE_REQUEST = "Restock everything we're short on before the weekend rush. Keep it under $500 delivered, and we need it within 3 days.";

export default function InventoryPage({ ctx }: { ctx: Ctx }) {
  const [coverage, setCoverage] = useState(14);
  const [inv, setInv] = useState<Inventory | null>(null);
  const [text, setText] = useState(SAMPLE_REQUEST);
  const [language, setLanguage] = useState<"en" | "hi">("en");
  const grid = useRef<AgGridReact<InventoryLine>>(null);
  const load = useAction();
  const create = useAction();

  const refresh = useCallback(() => {
    load.run(async () => setInv(await api.get<Inventory>(`/api/inventory?coverage_days=${coverage}`)));
  }, [coverage, load.run]);

  useEffect(refresh, [refresh]);

  const columns = useMemo<ColDef<InventoryLine>[]>(
    () => [
      { field: "sku", headerName: "SKU", pinned: "left", width: 140, filter: "agTextColumnFilter" },
      { field: "name", headerName: "Product", flex: 2, minWidth: 200, filter: "agTextColumnFilter" },
      { field: "category", width: 110, filter: "agTextColumnFilter" },
      { field: "on_hand", headerName: "On hand", width: 105, type: "numericColumn", filter: "agNumberColumnFilter" },
      { field: "incoming", width: 105, type: "numericColumn", filter: "agNumberColumnFilter" },
      { field: "units_28d", headerName: "Sold 28d", width: 105, type: "numericColumn", filter: "agNumberColumnFilter" },
      { field: "safety_stock", headerName: "Safety", width: 90, type: "numericColumn" },
      { field: "target_units", headerName: "Target", width: 95, type: "numericColumn" },
      { field: "need", headerName: "Need", width: 90, type: "numericColumn", filter: "agNumberColumnFilter", sort: "desc",
        cellStyle: (p) => (p.value > 0 ? { fontWeight: 700 } : null) },
      {
        field: "status",
        width: 130,
        filter: "agTextColumnFilter",
        cellRenderer: (p: { data?: InventoryLine }) =>
          p.data ? (
            <span className="row" style={{ gap: 4 }}>
              <Badge tone={p.data.status === "shortage" ? "warn" : "ok"}>{p.data.status === "shortage" ? "Shortage" : "OK"}</Badge>
              {p.data.warning && <span title={p.data.warning} aria-label={p.data.warning}>⚠︎</span>}
            </span>
          ) : null,
      },
      { field: "warning", headerName: "Data note", width: 180, tooltipField: "warning" },
    ],
    [],
  );

  async function startRequest() {
    const p = await create.run(() => api.post<Procurement>("/api/procurements", { request_text: text, language }));
    if (p) go({ page: "procurement", id: p.procurement_id });
  }

  return (
    <div className="grid-2">
      <section className="panel" aria-labelledby="inv-h">
        <div className="panel-head">
          <h1 id="inv-h">Inventory</h1>
          {inv && <Badge tone={inv.shortage_count ? "warn" : "ok"}>{inv.shortage_count} shortages</Badge>}
          <span className="spacer" />
          <label className="row small muted" htmlFor="coverage">
            Coverage days
            <input id="coverage" type="number" min={1} max={60} value={coverage} style={{ width: 70 }}
              onChange={(e) => setCoverage(Math.max(1, Math.min(60, Number(e.target.value) || 14)))} />
          </label>
          <button className="btn small" onClick={() => grid.current?.api.exportDataAsCsv({ fileName: "inventory.csv", processCellCallback: safeCsvCell })}>
            Export CSV
          </button>
        </div>
        <ErrorNotice error={load.error} />
        <div className="grid-box">
          <AgGridReact<InventoryLine>
            ref={grid}
            theme={gridTheme}
            rowData={inv?.products ?? []}
            columnDefs={columns}
            defaultColDef={{ sortable: true, resizable: true }}
            rowSelection={{ mode: "multiRow", checkboxes: true, headerCheckbox: true }}
            getRowId={(p) => p.data.sku}
            getRowClass={(p: RowClassParams<InventoryLine>) => (p.data?.status === "shortage" ? "row-shortage" : undefined)}
            loading={load.busy && !inv}
            tooltipShowDelay={300}
          />
        </div>
        {inv && (
          <p className="small muted" style={{ marginTop: 8 }}>
            Formula: <code>{inv.formula}</code>. Sales window {inv.window.start} → {inv.window.end} ({inv.window.days} complete days before
            business date {inv.business_date}). Synthetic demo data.
          </p>
        )}
      </section>

      <aside className="stack">
        <section className="panel stack" aria-labelledby="req-h">
          <h2 id="req-h">New restock request</h2>
          <p className="small muted">
            Describe what you need. TradePilot suggests quantities from the formula and reads hints like a budget or a weekday from your
            text, but nothing is sent to suppliers until you confirm exact requirements.
          </p>
          <label className="field" htmlFor="req-text">
            Request
            <textarea id="req-text" value={text} maxLength={2000} onChange={(e) => setText(e.target.value)} />
          </label>
          <label className="field" htmlFor="req-lang">
            Explanation language
            <select id="req-lang" value={language} onChange={(e) => setLanguage(e.target.value as "en" | "hi")}>
              <option value="en">English</option>
              <option value="hi">Hindi (हिन्दी)</option>
            </select>
          </label>
          <button className="btn primary" onClick={startRequest} disabled={create.busy}>
            {create.busy ? <Spinner label="Drafting…" /> : "Draft requirements"}
          </button>
          <ErrorNotice error={create.error} />
        </section>
        <ImportPanel onApplied={refresh} />
        <section className="panel small muted">
          Session data is fictional and isolated to this browser. {ctx.config.scenarios_enabled ? "Scenarios are enabled." : ""}
        </section>
      </aside>
    </div>
  );
}

const TEMPLATES = {
  inventory: "sku,on_hand\nCHG-65W-PD,12\n",
  sales: "sale_id,sku,date,units\nPOS-1001,PB-10K,2026-11-03,2\n",
};

function ImportPanel({ onApplied }: { onApplied: () => void }) {
  const [kind, setKind] = useState<"inventory" | "sales">("inventory");
  const [csv, setCsv] = useState(TEMPLATES.inventory);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const act = useAction();

  async function onFile(file: File | undefined) {
    if (!file) return;
    if (file.size > 256 * 1024) {
      act.setError(new Error("CSV files are limited to 256 KB."));
      return;
    }
    setCsv(await file.text());
    setPreview(null);
  }

  return (
    <section className="panel stack" aria-labelledby="imp-h">
      <h2 id="imp-h">Import CSV</h2>
      <div className="row">
        <label className="field" htmlFor="imp-kind">
          Kind
          <select id="imp-kind" value={kind} onChange={(e) => { const k = e.target.value as "inventory" | "sales"; setKind(k); setCsv(TEMPLATES[k]); setPreview(null); }}>
            <option value="inventory">Counted on-hand</option>
            <option value="sales">Sales rows</option>
          </select>
        </label>
        <label className="field" htmlFor="imp-file">
          File
          <input id="imp-file" type="file" accept=".csv,text/csv" onChange={(e) => onFile(e.target.files?.[0])} />
        </label>
      </div>
      <label className="field" htmlFor="imp-text">
        CSV text
        <textarea id="imp-text" className="mono" value={csv} onChange={(e) => { setCsv(e.target.value); setPreview(null); }} />
      </label>
      <div className="row">
        <button className="btn" disabled={act.busy} onClick={() => act.run(async () => setPreview(await api.post<ImportPreview>("/api/imports/preview", { kind, csv_text: csv })))}>
          Preview
        </button>
        <button className="btn primary" disabled={act.busy || !preview || preview.error_count > 0 || preview.status === "applied"}
          onClick={() => act.run(async () => { setPreview(await api.post<ImportPreview>(`/api/imports/${preview!.import_id}/apply`)); onApplied(); })}>
          Apply {preview && preview.error_count === 0 ? `${preview.valid_rows} rows` : ""}
        </button>
      </div>
      <ErrorNotice error={act.error} />
      {preview && (
        <div className={`notice ${preview.status === "applied" ? "ok" : preview.error_count ? "bad" : ""}`} role="status">
          {preview.status === "applied"
            ? `Applied ${preview.valid_rows} rows.`
            : `${preview.valid_rows} valid rows, ${preview.error_count} with errors. ${preview.error_count ? "Nothing will be applied until every row is valid." : "Review, then apply."}`}
          {preview.errors.length > 0 && (
            <ul className="small">
              {preview.errors.map((e) => <li key={e.row}>Row {e.row}: {e.message}</li>)}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
