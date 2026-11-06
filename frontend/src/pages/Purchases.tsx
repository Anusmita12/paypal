import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { go, type Ctx } from "../App";
import { humanize, minutesLeft, money, when } from "../format";
import type { Purchase } from "../types";
import { Badge, ErrorNotice, Live, PurchaseBadge, Spinner, useAction, usePolling } from "../ui";

export default function PurchasesPage({ ctx, id }: { ctx: Ctx; id?: string }) {
  const [list, setList] = useState<Purchase[]>([]);
  const [p, setP] = useState<Purchase | null>(null);
  const load = useAction();

  const refreshList = useCallback(() => {
    api.get<{ purchases: Purchase[] }>("/api/purchases").then((r) => setList(r.purchases)).catch(() => undefined);
  }, []);
  const refresh = useCallback(async () => {
    if (!id) return;
    setP(await api.get<Purchase>(`/api/purchases/${id}`));
  }, [id]);

  useEffect(() => {
    setP(null);
    refreshList();
    if (id) load.run(refresh);
  }, [id, refresh, refreshList, load.run]);

  // Unknown and pending outcomes resolve in the background; watch them without a permanent spinner.
  usePolling(!!p && (p.status === "unknown" || p.status === "capture_pending"), () => refresh().catch(() => undefined), 2500);

  return (
    <div className="grid-2">
      <div className="stack">
        <ErrorNotice error={load.error} />
        {!id && <section className="panel"><h1>Purchases</h1><p className="muted">Select a purchase to see checkout, payment status and evidence.</p></section>}
        {id && !p && !load.error && <section className="panel"><Spinner label="Loading purchase…" /></section>}
        {p && <PurchaseDetail ctx={ctx} p={p} setP={(x) => { setP(x); refreshList(); }} refresh={refresh} />}
      </div>
      <aside className="panel stack" aria-labelledby="pl-h">
        <h2 id="pl-h">All purchases</h2>
        <table className="simple small">
          <thead><tr><th>Status</th><th className="num">Total</th><th>Supplier</th></tr></thead>
          <tbody>
            {list.map((x) => (
              <tr key={x.purchase_id} style={{ cursor: "pointer", background: x.purchase_id === id ? "var(--accent-soft)" : undefined }}>
                <td><button className="btn small" style={{ border: 0, background: "transparent", padding: 0 }} onClick={() => go({ page: "purchases", id: x.purchase_id })}><PurchaseBadge status={x.status} /></button></td>
                <td className="num">{money(x.amount_cents)}</td>
                <td>{x.snapshot.supplier?.name ?? x.kind}</td>
              </tr>
            ))}
            {list.length === 0 && <tr><td colSpan={3} className="muted">No purchases yet.</td></tr>}
          </tbody>
        </table>
      </aside>
    </div>
  );
}

function PurchaseDetail({ ctx, p, setP, refresh }: { ctx: Ctx; p: Purchase; setP: (p: Purchase) => void; refresh: () => Promise<void> }) {
  const act = useAction();
  const [announce, setAnnounce] = useState("");
  const s = p.snapshot;
  const ordered = !!p.paypal_order_id;
  const paid = p.status === "paid";
  const resLeft = p.reservation ? minutesLeft(p.reservation.expires_at) : null;

  async function step<T extends Purchase>(path: string, msg: string): Promise<T | undefined> {
    const next = await act.run(async () => {
      try {
        return await api.post<T>(`/api/purchases/${p.purchase_id}/${path}`);
      } catch (e) {
        await refresh(); // show what the server now knows (e.g. outcome unknown)
        throw e;
      }
    });
    if (next) {
      setP(next);
      setAnnounce(msg);
      if (next.status === "paid") ctx.refreshSession();
    }
    return next;
  }

  async function simulatedBuyerApproves() {
    const approved = await step("fixture-approve", "Simulated buyer approved.");
    if (approved) await step("capture", "Payment captured.");
  }

  return (
    <>
      <section className="panel stack">
        <div className="panel-head" style={{ marginBottom: 0 }}>
          <h1>{p.kind === "restock" ? `Restock from ${s.supplier.name}` : "Tool wallet top-up"}</h1>
          <PurchaseBadge status={p.status} />
          <span className="spacer" />
          <strong style={{ fontSize: 18 }}>{money(p.amount_cents)}</strong>
        </div>
        {p.status_reason && <p className="small muted">Reason: {humanize(p.status_reason)}</p>}
        <dl className="kv small">
          <dt>Payment mode</dt><dd>{p.payment_mode === "fixture" ? <Badge tone="warn">offline fixture — no PayPal, no money</Badge> : <Badge tone="ok">PayPal Sandbox</Badge>}</dd>
          <dt>PayPal order</dt><dd className="mono">{p.paypal_order_id ?? "not created yet"}</dd>
          <dt>Capture</dt><dd className="mono">{p.capture_id ?? "—"}</dd>
          {p.reservation && <><dt>Stock reservation</dt><dd>{p.reservation.status}{p.reservation.status === "held" && resLeft !== null && ` · ${resLeft >= 0 ? `${resLeft} min left` : "expired"}`}{p.reservation.status === "capture_in_progress" && " · pinned until the payment outcome is known"}</dd></>}
        </dl>
        {p.status === "unknown" && (
          <div className="notice bad" role="status">
            PayPal did not confirm the outcome. We never retry blindly: the server is reconciling by order ID with the original request ID. Stock stays reserved meanwhile.
          </div>
        )}
        {p.status === "manual_review" && <div className="notice bad">Stopped for manual review. No automatic retries will move money for this purchase.</div>}
        {paid && p.kind === "restock" && (
          <div className="notice ok" role="status">
            Paid. {s.lines?.reduce((a, l) => a + l.qty, 0)} units added to <strong>incoming</strong> stock (they count as on-hand only when received).
          </div>
        )}
        {paid && p.kind === "wallet_topup" && (
          <div className="notice ok" role="status">
            Paid. {money(p.amount_cents)} added to the tool wallet. <button className="btn small" onClick={() => go({ page: "wallet" })}>Open wallet</button>
          </div>
        )}
      </section>

      {!paid && ["creating", "awaiting_paypal_approval", "capture_pending", "unknown"].includes(p.status) && (
        <section className="panel stack" aria-labelledby="co-h">
          <h2 id="co-h">Step 2 of 2 — pay with PayPal</h2>
          <div className={`step ${ordered ? "done" : "active"}`}>
            <span className="step-n">1</span>
            <div className="stack">
              <div><strong>Create the PayPal order</strong> <span className="small muted">from the approved snapshot only ({money(p.amount_cents)} USD)</span></div>
              {!ordered && ctx.config.payment_mode === "fixture" && (
                <button className="btn primary" onClick={() => step("paypal-order", "PayPal order created.")} disabled={act.busy}>
                  Create checkout order
                </button>
              )}
            </div>
          </div>
          <div className={`step ${p.status === "capture_pending" || paid ? "done" : ordered ? "active" : ""}`}>
            <span className="step-n">2</span>
            <div className="stack">
              <strong>Buyer approves on PayPal, then the server captures</strong>
              {ctx.config.payment_mode === "fixture" ? (
                ordered && p.status === "awaiting_paypal_approval" && (
                  <div className="fixture-checkout stack">
                    <div className="row"><Badge tone="warn">SIMULATED</Badge><strong>Offline PayPal stand-in</strong></div>
                    <p className="small">Fixture mode has no PayPal window. This button plays the buyer's approval so the real server capture, validation and finalizer run. No credentials are ever entered here.</p>
                    <button className="btn primary" onClick={simulatedBuyerApproves} disabled={act.busy}>
                      {act.busy ? <Spinner label="Capturing…" /> : "Simulate buyer approval and capture"}
                    </button>
                  </div>
                )
              ) : (
                <PayPalButtons ctx={ctx} p={p} onPaid={(x) => { setP(x); setAnnounce("Payment captured."); }} onError={(e) => act.setError(e)} />
              )}
            </div>
          </div>
          {(p.status === "unknown" || p.status === "capture_pending") && (
            <button className="btn" onClick={() => step("reconcile", "Status checked.")} disabled={act.busy}>Check payment status</button>
          )}
          <ErrorNotice error={act.error} onDismiss={() => act.setError(null)} />
        </section>
      )}

      {paid && p.kind === "restock" && <Fulfilment ctx={ctx} p={p} refresh={refresh} />}

      <section className="grid-2-even">
        <div className="panel stack" aria-labelledby="tl-h">
          <div className="row"><h2 id="tl-h">Payment timeline</h2><span className="spacer" />
            {paid && <button className="btn small" onClick={() => step("reconcile", "Status checked.")} disabled={act.busy}>Check payment status</button>}
          </div>
          <ol className="timeline" aria-label="Payment events">
            {(p.events ?? []).map((e, i) => (
              <li key={i} className={e.kind === "failed" || e.kind === "manual_review" || e.kind === "outcome_unknown" ? "err" : ""}>
                <span className="dot" aria-hidden="true">{i + 1}</span>
                <div><div className="tool">{e.kind} · {e.source}</div><div>{e.summary}</div></div>
                <span className="small muted">{when(e.at)}</span>
              </li>
            ))}
            {(p.events ?? []).length === 0 && <li className="muted small" style={{ display: "block" }}>No payment activity yet.</li>}
          </ol>
        </div>
        <div className="panel stack" aria-labelledby="ev-h">
          <h2 id="ev-h">Evidence</h2>
          <table className="simple small">
            <thead><tr><th>Operation</th><th>Request ID</th><th>State</th><th className="num">Tries</th></tr></thead>
            <tbody>
              {(p.operations ?? []).map((o) => <tr key={o.kind}><td>{o.kind}</td><td className="mono">{o.request_id}</td><td>{o.state}</td><td className="num">{o.attempts}</td></tr>)}
              {(p.operations ?? []).length === 0 && <tr><td colSpan={4} className="muted">No provider calls yet.</td></tr>}
            </tbody>
          </table>
          {(p.ledger ?? []).map((l) => <div key={l.business_key} className="small">Ledger <span className="mono">{l.business_key}</span> · {money(l.amount_cents)}</div>)}
          <div className="row">
            <a className="btn small" href={`/api/purchases/${p.purchase_id}/evidence?format=json`} download>Download JSON</a>
            <a className="btn small" href={`/api/purchases/${p.purchase_id}/evidence?format=csv`} download>Download CSV</a>
          </div>
        </div>
      </section>

      {s.lines && (
        <section className="panel">
          <h2 style={{ marginBottom: 8 }}>Approved items</h2>
          <table className="simple">
            <thead><tr><th>Item</th><th>For SKU</th><th className="num">Qty</th><th className="num">Unit</th><th className="num">Line</th></tr></thead>
            <tbody>{s.lines.map((l) => <tr key={l.offered_sku}><td>{l.name}</td><td className="mono small">{l.sku}</td><td className="num">{l.qty}</td><td className="num">{money(l.unit_cents)}</td><td className="num">{money(l.line_cents)}</td></tr>)}</tbody>
          </table>
        </section>
      )}
      <Live text={announce} />
    </>
  );
}

declare global {
  interface Window {
    paypal?: { Buttons: (opts: Record<string, unknown>) => { render: (el: HTMLElement) => Promise<void>; close: () => void } };
  }
}

let sdkPromise: Promise<void> | null = null;
function loadPayPalSdk(clientId: string): Promise<void> {
  sdkPromise ??= new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = `https://www.paypal.com/sdk/js?client-id=${encodeURIComponent(clientId)}&currency=USD&intent=capture&components=buttons`;
    s.onload = () => resolve();
    s.onerror = () => { sdkPromise = null; reject(new Error("Could not load the PayPal checkout script.")); };
    document.head.appendChild(s);
  });
  return sdkPromise;
}

/** Official PayPal Smart Buttons (Sandbox). The server creates and captures; the browser only relays IDs. */
function PayPalButtons({ ctx, p, onPaid, onError }: { ctx: Ctx; p: Purchase; onPaid: (p: Purchase) => void; onError: (e: unknown) => void }) {
  const box = useRef<HTMLDivElement>(null);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const clientId = ctx.config.paypal_client_id;
    if (!clientId || !box.current) return;
    let buttons: { close: () => void } | null = null;
    loadPayPalSdk(clientId).then(() => {
      if (!window.paypal || !box.current) return;
      const b = window.paypal.Buttons({
        style: { layout: "vertical", label: "pay" },
        createOrder: async () => (await api.post<Purchase>(`/api/purchases/${p.purchase_id}/paypal-order`)).paypal_order_id,
        onApprove: async () => onPaid(await api.post<Purchase>(`/api/purchases/${p.purchase_id}/capture`)),
        onError: (err: unknown) => onError(err instanceof ApiError ? err : new Error("PayPal checkout reported an error.")),
      });
      buttons = b;
      box.current.innerHTML = "";
      return b.render(box.current).then(() => setReady(true));
    }).catch(onError);
    return () => buttons?.close();
  }, [ctx.config.paypal_client_id, p.purchase_id]);

  return (
    <div className="stack">
      <p className="small muted">PayPal opens its own secure window. TradePilot never asks for PayPal credentials.</p>
      {!ready && <Spinner label="Loading PayPal checkout…" />}
      <div ref={box} style={{ maxWidth: 360 }} />
    </div>
  );
}

/** Storefront side after payment: simulated shipment, tracking pushed to the PayPal order, and dispute entry point. */
function Fulfilment({ ctx, p, refresh }: { ctx: Ctx; p: Purchase; refresh: () => Promise<void> }) {
  const act = useAction();
  const sh = p.shipment;
  const run = (path: string, body?: unknown) =>
    act.run(async () => {
      try {
        const r = await api.post<{ dispute_id?: string }>(`/api/purchases/${p.purchase_id}/${path}`, body);
        if (r.dispute_id) go({ page: "disputes", id: r.dispute_id });
      } finally {
        await refresh();
      }
    });
  return (
    <section className="panel stack" aria-labelledby="ff-h">
      <div className="panel-head" style={{ marginBottom: 0 }}>
        <h2 id="ff-h">Fulfilment</h2>
        <Badge tone="warn">simulated carrier</Badge>
      </div>
      {sh ? (
        <dl className="kv small">
          <dt>Tracking</dt><dd className="mono">{sh.carrier} · {sh.tracking_number}</dd>
          <dt>Shipped</dt><dd>{when(sh.shipped_at)}</dd>
          <dt>Delivered</dt><dd>{sh.delivered_at ? when(sh.delivered_at) : "in transit"}</dd>
          <dt>On PayPal order</dt><dd><Badge tone={sh.tracker_state === "pushed" ? "ok" : "bad"}>{sh.tracker_state}</Badge>{sh.tracker_error && ` ${sh.tracker_error}`}</dd>
        </dl>
      ) : (
        <p className="small muted">Not shipped yet. Shipping records the supplier's tracking number and adds it to the PayPal order.</p>
      )}
      <div className="row">
        {(!sh || sh.tracker_state !== "pushed") && <button className="btn" disabled={act.busy} onClick={() => run("ship")}>{sh ? "Retry tracking push" : "Ship order"}</button>}
        {sh && !sh.delivered_at && <button className="btn" disabled={act.busy} onClick={() => run("deliver")}>Mark delivered</button>}
        {ctx.config.payment_mode === "fixture" && ctx.config.scenarios_enabled && !(p.disputes ?? []).length && (
          <button className="btn" disabled={act.busy} onClick={() => run("simulate-dispute", { reason: "MERCHANDISE_OR_SERVICE_NOT_RECEIVED" })}>
            Simulate buyer dispute
          </button>
        )}
        {(p.disputes ?? []).map((d) => (
          <button key={d.dispute_id} className="btn" onClick={() => go({ page: "disputes", id: d.dispute_id })}>Open dispute {d.paypal_dispute_id}</button>
        ))}
      </div>
      <ErrorNotice error={act.error} onDismiss={() => act.setError(null)} />
    </section>
  );
}
