import { useCallback, useEffect, useMemo, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import type { ColDef, RowClassParams } from "ag-grid-community";
import { api } from "../api";
import { go, type Ctx } from "../App";
import { gridTheme } from "../grid";
import { day, humanize, minutesLeft, money, pct, when } from "../format";
import type { Comparison, Offer, Procurement, ProcurementSummary, Proposal, Purchase } from "../types";
import { Badge, ErrorNotice, Live, ProcurementBadge, Spinner, useAction, usePolling } from "../ui";

const EDITABLE = new Set(["needs_input", "draft", "no_feasible_offer", "failed", "awaiting_approval"]);
const RUNNABLE = new Set(["draft", "no_feasible_offer", "failed", "awaiting_approval"]);
const SCENARIOS = [
  { name: "supplier_a_price_increase", label: "Supplier A raises prices", detail: "Apex revises three unit prices (+$55 on this basket)." },
  { name: "expire_quotes", label: "Expire all quotes", detail: "Every open quote reaches its expiry time." },
  { name: "supplier_b_stock_out", label: "Supplier B stock-out", detail: "Brightline runs out of the 65W charger." },
];

export default function ProcurementPage({ ctx, id }: { ctx: Ctx; id?: string }) {
  const [list, setList] = useState<ProcurementSummary[]>([]);
  const [p, setP] = useState<Procurement | null>(null);
  const load = useAction();

  const refreshList = useCallback(() => {
    api.get<{ procurements: ProcurementSummary[] }>("/api/procurements").then((r) => setList(r.procurements)).catch(() => undefined);
  }, []);

  const refresh = useCallback(async () => {
    if (!id) return;
    const next = await api.get<Procurement>(`/api/procurements/${id}`);
    setP(next);
  }, [id]);

  useEffect(() => {
    setP(null);
    refreshList();
    if (id) load.run(refresh);
    else if (!id) api.get<{ procurements: ProcurementSummary[] }>("/api/procurements").then((r) => {
      const first = r.procurements[0];
      if (first) go({ page: "procurement", id: first.procurement_id });
    }).catch(() => undefined);
  }, [id, refresh, refreshList, load.run]);

  const working = !!p && (p.status === "planning" || p.status === "negotiating" || p.run?.status === "queued" || p.run?.status === "running");
  usePolling(working, () => {
    refresh().then(() => ctx.refreshSession()).catch(() => undefined);
  });
  useEffect(() => {
    if (!working) refreshList();
  }, [working, refreshList]);

  return (
    <div className="grid-2">
      <div className="stack">
        <ErrorNotice error={load.error} />
        {!id && <section className="panel"><h1>Procurement</h1><p className="muted">Draft a request from the Inventory page to begin.</p>
          <button className="btn primary" style={{ marginTop: 12 }} onClick={() => go({ page: "inventory" })}>Go to inventory</button></section>}
        {id && !p && !load.error && <section className="panel"><Spinner label="Loading procurement…" /></section>}
        {p && <Detail key={p.procurement_id} ctx={ctx} p={p} refresh={refresh} working={working} />}
      </div>
      <aside className="stack">
        <section className="panel stack" aria-labelledby="list-h">
          <h2 id="list-h">Requests</h2>
          <ul className="list-plain proc-list">
            {list.map((x) => (
              <li key={x.procurement_id}>
                <button aria-current={x.procurement_id === id ? "true" : undefined} onClick={() => go({ page: "procurement", id: x.procurement_id })}>
                  <div className="row"><ProcurementBadge status={x.status} /><span className="small muted">{when(x.created_at)}</span></div>
                  <div className="small" style={{ marginTop: 4 }}>{x.request_text || "(no text)"}</div>
                </button>
              </li>
            ))}
            {list.length === 0 && <li className="muted small">No requests yet.</li>}
          </ul>
        </section>
        {ctx.config.scenarios_enabled && <ScenarioPanel onDone={() => refresh().catch(() => undefined)} />}
      </aside>
    </div>
  );
}

function ScenarioPanel({ onDone }: { onDone: () => void }) {
  const act = useAction();
  const [last, setLast] = useState<string | null>(null);
  return (
    <section className="panel stack" aria-labelledby="sc-h">
      <div className="row"><h2 id="sc-h">Disruption scenarios</h2><Badge tone="warn">synthetic</Badge></div>
      <p className="small muted">Labeled test events that change supplier terms in this session only. They never touch payments.</p>
      {SCENARIOS.map((s) => (
        <div key={s.name} className="row" style={{ justifyContent: "space-between" }}>
          <div><div style={{ fontWeight: 600 }}>{s.label}</div><div className="small muted">{s.detail}</div></div>
          <button className="btn small" disabled={act.busy} onClick={() => act.run(async () => {
            await api.post(`/api/demo/scenarios/${s.name}`);
            setLast(s.label);
            onDone();
          })}>Trigger</button>
        </div>
      ))}
      {last && <div className="notice warn small" role="status">Scenario applied: {last}.</div>}
      <ErrorNotice error={act.error} />
    </section>
  );
}

function Detail({ ctx, p, refresh, working }: { ctx: Ctx; p: Procurement; refresh: () => Promise<void>; working: boolean }) {
  const runAct = useAction();
  const canRun = !!p.requirements && RUNNABLE.has(p.status) && !working;

  async function startRun() {
    await runAct.run(async () => {
      await api.post(`/api/procurements/${p.procurement_id}/run`);
      await refresh();
    });
  }

  return (
    <>
      <section className="panel stack">
        <div className="panel-head" style={{ marginBottom: 0 }}>
          <h1>Restock request</h1>
          <ProcurementBadge status={p.status} />
          {p.status_reason && <span className="small muted">({humanize(p.status_reason)})</span>}
        </div>
        {p.request_text && <blockquote className="muted" style={{ margin: 0, borderLeft: "3px solid var(--border)", paddingLeft: 10 }}>{p.request_text}</blockquote>}
      </section>

      <RequirementsPanel p={p} refresh={refresh} editable={EDITABLE.has(p.status) && !working} />

      {p.requirements && (
        <section className="panel stack" aria-labelledby="run-h">
          <div className="panel-head" style={{ marginBottom: 0 }}>
            <h2 id="run-h">Buyer agent</h2>
            {p.run && <Badge tone={p.run.status === "failed" ? "bad" : p.run.status === "succeeded" ? "ok" : "info"}>run {p.run.status}</Badge>}
            {p.run && <span className="small muted">{p.run.mode === "fixture" ? "fixture script (no model call)" : `model ${p.run.model}`}</span>}
            <span className="spacer" />
            <button className="btn primary" onClick={startRun} disabled={!canRun || runAct.busy}>
              {working ? <Spinner label="Agent working…" /> : p.run ? "Run buyer again" : "Run buyer agent"}
            </button>
          </div>
          <p className="small muted">
            The agent can read inventory, request quotes, counteroffer within supplier policy, and propose the best feasible offer. It has
            no tool that approves or pays. Each paid tool call is debited from the tool wallet (cap {ctx.config.limits.tool_spend_cap_per_run_cents}¢ per run).
          </p>
          <ErrorNotice error={runAct.error} />
          {p.run && <RunTimeline p={p} />}
        </section>
      )}

      {p.negotiation_rounds.length > 0 && <NegotiationPanel p={p} />}
      {p.comparison && <OffersPanel comparison={p.comparison} />}
      {p.proposal && <ApprovalPanel ctx={ctx} p={p} proposal={p.proposal} refresh={refresh} working={working} />}
    </>
  );
}

function parseDollars(input: string): number | null {
  const m = input.trim().replace(/[$,]/g, "").match(/^(\d{1,6})(?:\.(\d{1,2}))?$/);
  if (!m) return null;
  return Number(m[1]) * 100 + Number((m[2] ?? "").padEnd(2, "0") || 0);
}

function RequirementsPanel({ p, refresh, editable }: { p: Procurement; refresh: () => Promise<void>; editable: boolean }) {
  const base = p.requirements ?? p.proposed;
  const [lines, setLines] = useState(base.lines.map((l) => ({ sku: l.sku, qty: l.qty })));
  const names = Object.fromEntries(base.lines.map((l) => [l.sku, "name" in l ? l.name : undefined]));
  const [budget, setBudget] = useState(base.budget_cents != null ? (base.budget_cents / 100).toFixed(2) : "");
  const [deadline, setDeadline] = useState(base.deadline ?? "");
  const [coverage, setCoverage] = useState(base.coverage_days);
  const [editing, setEditing] = useState(p.status === "needs_input");
  const act = useAction();
  const budgetCents = parseDollars(budget);

  async function confirm() {
    await act.run(async () => {
      await api.post(`/api/procurements/${p.procurement_id}/confirm`, {
        lines: lines.filter((l) => l.qty > 0), budget_cents: budgetCents, deadline, coverage_days: coverage, currency: "USD",
      });
      setEditing(false);
      await refresh();
    });
  }

  if (!editing && p.requirements) {
    const r = p.requirements;
    return (
      <section className="panel stack" aria-labelledby="reqs-h">
        <div className="panel-head" style={{ marginBottom: 0 }}>
          <h2 id="reqs-h">Confirmed requirements</h2>
          <Badge tone="ok">confirmed {when(p.confirmed_at)}</Badge>
          <span className="spacer" />
          {editable && <button className="btn small" onClick={() => setEditing(true)}>Edit</button>}
        </div>
        <dl className="kv">
          <dt>Budget</dt><dd><strong>{money(r.budget_cents)}</strong> USD, delivered (shipping included, tax excluded in this synthetic scenario)</dd>
          <dt>Deliver by</dt><dd><strong>{day(r.deadline)}</strong> ({r.deadline})</dd>
          <dt>Lines</dt><dd>{r.lines.length} SKUs, {r.lines.reduce((a, l) => a + l.qty, 0)} units, {r.coverage_days}-day coverage</dd>
        </dl>
        <details>
          <summary className="small">Show lines</summary>
          <table className="simple small"><thead><tr><th>SKU</th><th>Product</th><th className="num">Qty</th></tr></thead>
            <tbody>{r.lines.map((l) => <tr key={l.sku}><td className="mono">{l.sku}</td><td>{l.name}</td><td className="num">{l.qty}</td></tr>)}</tbody></table>
        </details>
      </section>
    );
  }

  return (
    <section className="panel stack" aria-labelledby="reqs-h">
      <div className="panel-head" style={{ marginBottom: 0 }}>
        <h2 id="reqs-h">Confirm requirements</h2>
        <Badge tone="warn">not yet sent to suppliers</Badge>
      </div>
      {p.proposed.notes?.map((n) => <div key={n} className="notice small">{n}</div>)}
      <div className="row" style={{ alignItems: "end" }}>
        <label className="field" htmlFor="budget">Budget (USD, delivered)
          <input id="budget" inputMode="decimal" value={budget} onChange={(e) => setBudget(e.target.value)} aria-invalid={budget !== "" && budgetCents === null} style={{ width: 140 }} />
        </label>
        <label className="field" htmlFor="deadline">Deliver by
          <input id="deadline" type="date" value={deadline} onChange={(e) => setDeadline(e.target.value)} />
        </label>
        <label className="field" htmlFor="cov">Coverage days
          <input id="cov" type="number" min={1} max={60} value={coverage} onChange={(e) => setCoverage(Number(e.target.value))} style={{ width: 90 }} />
        </label>
      </div>
      <table className="simple">
        <caption className="sr-only">Quantities to buy</caption>
        <thead><tr><th>SKU</th><th>Product</th><th className="num">Quantity</th><th><span className="sr-only">Remove</span></th></tr></thead>
        <tbody>
          {lines.map((l, i) => (
            <tr key={l.sku}>
              <td className="mono">{l.sku}</td>
              <td>{names[l.sku] ?? ""}</td>
              <td className="num">
                <input aria-label={`Quantity for ${l.sku}`} type="number" min={0} max={500} value={l.qty} style={{ width: 90, textAlign: "right" }}
                  onChange={(e) => setLines(lines.map((x, j) => (j === i ? { ...x, qty: Math.max(0, Math.floor(Number(e.target.value) || 0)) } : x)))} />
              </td>
              <td><button className="btn small" aria-label={`Remove ${l.sku}`} onClick={() => setLines(lines.filter((_, j) => j !== i))}>Remove</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row">
        <button className="btn primary" onClick={confirm} disabled={act.busy || budgetCents === null || !deadline || lines.every((l) => l.qty === 0)}>
          Confirm requirements
        </button>
        {p.requirements && <button className="btn" onClick={() => setEditing(false)}>Cancel</button>}
        <span className="small muted">Confirming replaces any earlier quotes and proposals for this request.</span>
      </div>
      <ErrorNotice error={act.error} />
    </section>
  );
}

const TOOL_LABEL: Record<string, string> = {
  get_inventory_snapshot: "inventory",
  calculate_restock_needs: "restock",
  list_suppliers: "suppliers",
  request_quote: "quote",
  counteroffer: "negotiate",
  get_compatible_surplus_offers: "surplus",
  compare_offers: "compare",
  propose_purchase: "propose",
  check_market_price: "market",
};

const PAID = /\(paid (\d+)¢\)$/;

/** Micro-payment ticker: each paid tool call as it is debited, built from the run's persisted events. */
function SpendTicker({ p }: { p: Procurement }) {
  const charges = p.events
    .map((e) => ({ e, m: PAID.exec(e.summary) }))
    .filter((x): x is { e: (typeof p.events)[number]; m: RegExpExecArray } => !!x.m);
  if (charges.length === 0) return null;
  return (
    <div className="ticker" aria-label="Tool charges this run">
      <span className="small" style={{ fontWeight: 600 }}>Micro-payments</span>
      <ol className="ticker-list">
        {charges.map(({ e, m }) => (
          <li key={e.seq} className="fade-in" title={e.summary}>
            <span className="mono">{e.tool === "check_market_price" ? "market" : String(e.arguments?.supplier_id ?? e.tool)}</span>
            <strong>{m[1]}¢</strong>
          </li>
        ))}
      </ol>
      <strong className="ticker-total">{money(p.run?.tool_spend_cents ?? 0)}</strong>
    </div>
  );
}

function RunTimeline({ p }: { p: Procurement }) {
  const run = p.run!;
  const latest = p.events[p.events.length - 1];
  return (
    <div className="stack">
      <SpendTicker p={p} />
      <div className="row small muted">
        <span>{run.tool_calls} tool calls</span>·<span>{money(run.tool_spend_cents)} tool spend</span>
        {run.input_tokens > 0 && <>·<span>{run.input_tokens + run.output_tokens} tokens</span></>}
        {run.finished_at && run.started_at && <>·<span>{((new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000).toFixed(1)}s</span></>}
      </div>
      <ol className="timeline" aria-label="Agent activity">
        {p.events.map((e) => (
          <li key={e.seq} className={`fade-in ${e.ok ? "" : "err"} ${e.kind === "status" ? "status" : ""}`}>
            <span className="dot" aria-hidden="true">{e.ok ? e.seq : "!"}</span>
            <div>
              {e.tool && <div className="tool">{TOOL_LABEL[e.tool] ?? e.tool} · {e.tool}</div>}
              <div>{e.summary}</div>
            </div>
            <span className="small muted">{e.elapsed_ms ? `${e.elapsed_ms}ms` : ""}</span>
          </li>
        ))}
      </ol>
      {run.error_message && <div className="notice bad">{run.error_message}</div>}
      {run.summary && run.status === "succeeded" && !p.proposal && <div className="notice">{run.summary}</div>}
      <Live text={latest ? latest.summary : ""} />
    </div>
  );
}

function NegotiationPanel({ p }: { p: Procurement }) {
  return (
    <section className="panel" aria-labelledby="neg-h">
      <h2 id="neg-h" style={{ marginBottom: 8 }}>Negotiation rounds</h2>
      <table className="simple">
        <thead><tr><th>Supplier</th><th className="num">Round</th><th className="num">Asked</th><th>Surplus offered</th><th>Outcome</th><th className="num">Granted</th><th>Policy reason</th></tr></thead>
        <tbody>
          {p.negotiation_rounds.map((r, i) => (
            <tr key={i}>
              <td>{r.supplier_id}</td><td className="num">{r.round}</td><td className="num">{pct(r.requested_bps)}</td>
              <td className="mono small">{r.requested_surplus.join(", ") || "—"}</td>
              <td><Badge tone={r.outcome === "accepted" ? "ok" : r.outcome === "countered" ? "warn" : "bad"}>{r.outcome}</Badge></td>
              <td className="num">{pct(r.granted_bps)}</td><td className="small">{humanize(r.rationale_code)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function OffersPanel({ comparison }: { comparison: Comparison }) {
  const columns = useMemo<ColDef<Offer>[]>(() => [
    { headerName: "#", width: 56, valueGetter: (p) => p.data?.rank ?? "—" },
    { field: "supplier_name", headerName: "Supplier", flex: 1.4, minWidth: 170, valueFormatter: (p) => `${p.value}${p.data?.negotiated ? " · negotiated" : ""}` },
    { field: "feasible", headerName: "Feasible", width: 96, cellDataType: false, cellRenderer: (p: { data?: Offer }) => p.data ? <Badge tone={p.data.feasible ? "ok" : "bad"}>{p.data.feasible ? "Yes" : "No"}</Badge> : null },
    { field: "spec_compliant", headerName: "Specs", width: 84, cellDataType: false, valueFormatter: (p) => (p.value ? "Meets" : "Fails") },
    { field: "delivery_date", headerName: "Delivery", width: 108, cellDataType: false },
    { field: "subtotal_cents", headerName: "Subtotal", width: 100, type: "numericColumn", valueFormatter: (p) => money(p.value) },
    { field: "discount_cents", headerName: "Discount", width: 96, type: "numericColumn", valueFormatter: (p) => (p.value ? `−${money(p.value)}` : "—") },
    { field: "shipping_cents", headerName: "Shipping", width: 92, type: "numericColumn", valueFormatter: (p) => money(p.value) },
    { field: "total_cents", headerName: "Total", width: 100, type: "numericColumn", sort: "asc", cellStyle: { fontWeight: 700 }, valueFormatter: (p) => money(p.value) },
    { field: "expires_at", headerName: "Expires", width: 140, cellDataType: false, valueFormatter: (p) => when(p.value) },
    { headerName: "Why not", flex: 2, minWidth: 220, valueGetter: (p) => p.data?.reasons.map((r) => r.message).join(" · ") || "", tooltipValueGetter: (p) => p.value },
  ], []);

  return (
    <section className="panel stack" aria-labelledby="off-h">
      <div className="panel-head" style={{ marginBottom: 0 }}>
        <h2 id="off-h">Offer comparison</h2>
        <span className="small muted">Ranked by the server: lowest delivered total among feasible offers. The model cannot change this.</span>
      </div>
      <div className="row small">
        <span>Budget <strong>{money(comparison.budget_cents)}</strong></span>·<span>Deadline <strong>{comparison.deadline}</strong></span>
        {comparison.baseline && <>·<span>List-price baseline {comparison.baseline.supplier_id} {money(comparison.baseline.total_cents)}</span></>}
        {comparison.savings_cents != null && comparison.savings_cents > 0 && <Badge tone="ok">saves {money(comparison.savings_cents)}</Badge>}
      </div>
      <div className="offer-grid">
        <AgGridReact<Offer>
          theme={gridTheme}
          domLayout="autoHeight"
          rowData={comparison.offers}
          columnDefs={columns}
          defaultColDef={{ sortable: true, resizable: true }}
          getRowId={(p) => p.data.quote_id}
          getRowClass={(p: RowClassParams<Offer>) => p.data?.quote_id === comparison.best_quote_id ? "row-best" : p.data?.feasible ? undefined : "row-infeasible"}
          tooltipShowDelay={200}
        />
      </div>
      <ul className="list-plain offer-cards" aria-label="Offer summary">
        {comparison.offers.map((o) => (
          <li key={o.quote_id} className="panel" style={{ padding: 10 }}>
            <div className="row"><strong>{o.supplier_name}</strong>{o.negotiated && <span className="small muted">negotiated</span>}<Badge tone={o.feasible ? "ok" : "bad"}>{o.feasible ? `Rank ${o.rank}` : "Rejected"}</Badge></div>
            <div className="small">{money(o.total_cents)} · delivers {o.delivery_date}</div>
            {o.reasons.map((r) => <div key={r.code} className="small muted">{r.message}</div>)}
          </li>
        ))}
      </ul>
      {comparison.suggestions.length > 0 && (
        <div className="notice warn"><strong>No feasible offer.</strong> Changes you could approve:<ul>{comparison.suggestions.map((s) => <li key={s}>{s}</li>)}</ul></div>
      )}
    </section>
  );
}

function ApprovalPanel({ ctx, p, proposal, refresh, working }: { ctx: Ctx; p: Procurement; proposal: Proposal; refresh: () => Promise<void>; working: boolean }) {
  const act = useAction();
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 15000);
    return () => window.clearInterval(t);
  }, []);
  const s = proposal.snapshot;
  const left = minutesLeft(s.quote_expires_at, now);
  const current = proposal.status === "current" && p.status === "awaiting_approval" && !working;

  async function approve() {
    const purchase = await act.run(async () => {
      try {
        return await api.post<Purchase>(`/api/proposals/${proposal.proposal_id}/approve`, { version: proposal.version, digest: proposal.digest });
      } finally {
        await refresh();
      }
    });
    if (purchase) go({ page: "purchases", id: purchase.purchase_id });
  }

  return (
    <section className={`panel stack ${current ? "cart" : ""}`} aria-labelledby="cart-h">
      <div className="panel-head" style={{ marginBottom: 0 }}>
        <h2 id="cart-h">Proposed purchase</h2>
        <Badge tone={proposal.status === "current" ? "warn" : proposal.status === "approved" ? "ok" : "plain"}>
          version {proposal.version} · {proposal.status}
        </Badge>
        {proposal.status_reason && <span className="small muted">({humanize(proposal.status_reason)})</span>}
      </div>

      {proposal.status !== "current" && proposal.status !== "approved" && (
        <div className="notice warn" role="status">
          This proposal is no longer valid ({humanize(proposal.status_reason ?? proposal.status)}). Run the buyer again for fresh quotes; nothing was reserved or paid.
        </div>
      )}

      <p>
        Buy from <strong>{s.supplier.name}</strong> (terms revision {s.supplier.revision}), delivered by <strong>{day(s.delivery_date)}</strong>,
        for <strong>{money(s.total_cents)}</strong> — {s.total_cents <= s.budget_cents ? `${money(s.budget_cents - s.total_cents)} under` : "over"} the {money(s.budget_cents)} budget.
      </p>
      <table className="simple">
        <caption className="sr-only">Exact cart</caption>
        <thead><tr><th>Item</th><th>Supplier SKU</th><th className="num">Qty</th><th className="num">Unit</th><th className="num">Line</th></tr></thead>
        <tbody>
          {s.lines.map((l) => (
            <tr key={l.offered_sku}>
              <td>{l.name}{l.is_surplus && <> <Badge tone="info">surplus substitute for {l.sku}</Badge></>}</td>
              <td className="mono small">{l.offered_sku}</td>
              <td className="num">{l.qty}</td><td className="num">{money(l.unit_cents)}</td><td className="num">{money(l.line_cents)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr><td colSpan={4}>Subtotal</td><td className="num">{money(s.subtotal_cents)}</td></tr>
          {s.discount_cents > 0 && <tr><td colSpan={4}>Negotiated discount ({pct(s.discount_bps)})</td><td className="num">−{money(s.discount_cents)}</td></tr>}
          <tr><td colSpan={4}>Shipping</td><td className="num">{money(s.shipping_cents)}</td></tr>
          <tr><td colSpan={4}>Tax <span className="small muted">({s.tax_note})</span></td><td className="num">{money(s.tax_cents)}</td></tr>
          <tr className="total"><td colSpan={4}>Total (USD)</td><td className="num">{money(s.total_cents)}</td></tr>
        </tfoot>
      </table>

      <div className="stack">
        <div className="row"><h3>Why this offer</h3><Badge tone={ctx.config.ai_mode === "fixture" ? "warn" : "info"}>{ctx.config.ai_mode === "fixture" ? "deterministic template (fixture mode)" : `written by ${ctx.config.ai_mode === "gemini" ? "Gemini" : "Claude"}; amounts checked against server figures`}</Badge></div>
        <p>{proposal.explanation}</p>
      </div>

      <p className="small muted">{s.payee_note} Snapshot digest <span className="mono">{proposal.digest.slice(0, 12)}…</span></p>

      {current && (
        <>
          {left <= 5 && <div className="notice warn" role="status">Quote expires {left <= 0 ? "now" : `in ${left} min`}. After that the server rejects approval.</div>}
          <div className="notice small">
            <strong>Step 1 of 2 — approve the procurement.</strong> This locks these exact items and prices and reserves supplier stock for{" "}
            {ctx.config.limits.reservation_minutes} minutes. No money moves yet; you pay in step 2 on PayPal.
          </div>
          <button className="btn primary big" onClick={approve} disabled={act.busy || left < 0}>
            {act.busy ? <Spinner label="Approving…" /> : `Approve this purchase — ${money(s.total_cents)}`}
          </button>
        </>
      )}
      {p.purchase_id && (
        <button className="btn" onClick={() => go({ page: "purchases", id: p.purchase_id! })}>Open checkout and payment status</button>
      )}
      <ErrorNotice error={act.error} />
    </section>
  );
}
