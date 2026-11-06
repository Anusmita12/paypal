import { useCallback, useEffect, useMemo, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import type { ColDef } from "ag-grid-community";
import { api } from "../api";
import { go, type Ctx } from "../App";
import { gridTheme } from "../grid";
import { cents, humanize, money, when } from "../format";
import type { Payout, Purchase, Wallet } from "../types";
import { Badge, ErrorNotice, Spinner, useAction } from "../ui";

type Usage = Wallet["usage"][number];

const PAYOUT_TONE: Record<Payout["status"], "ok" | "warn" | "bad" | "info"> = {
  pending: "info", submitted: "warn", unknown: "bad", success: "ok", failed: "bad", manual_review: "bad",
};

export default function WalletPage({ ctx }: { ctx: Ctx }) {
  const [w, setW] = useState<Wallet | null>(null);
  const load = useAction();
  const act = useAction();

  const refresh = useCallback(async () => {
    setW(await api.get<Wallet>("/api/wallet"));
    ctx.refreshSession();
  }, [ctx.refreshSession]);

  useEffect(() => {
    load.run(refresh);
  }, [load.run, refresh]);

  const columns = useMemo<ColDef<Usage>[]>(() => [
    { field: "at", headerName: "When", width: 170, valueFormatter: (p) => when(p.value), sort: "desc" },
    { field: "tool_name", headerName: "Tool", flex: 1, minWidth: 170 },
    { field: "provider_id", headerName: "Provider", width: 130 },
    { field: "price_cents", headerName: "Price", width: 84, type: "numericColumn", valueFormatter: (p) => cents(p.value) },
    { field: "payout_status", headerName: "Provider payout", width: 140, valueFormatter: (p) => humanize(String(p.value)) },
    { field: "run_id", headerName: "Agent run", width: 110, valueFormatter: (p) => (p.value ? String(p.value).slice(0, 8) : "—") },
  ], []);

  async function topUp(amount: number) {
    const p = await act.run(() => api.post<Purchase>("/api/wallet/topups", { amount_cents: amount }));
    if (p) go({ page: "purchases", id: p.purchase_id });
  }

  const owed = useMemo(() => {
    const out = new Map<string, number>();
    for (const u of w?.usage ?? []) if (u.payout_status === "accrued") out.set(u.provider_id, (out.get(u.provider_id) ?? 0) + u.price_cents);
    return [...out.entries()];
  }, [w]);

  return (
    <div className="grid-2">
      <div className="stack">
        <section className="panel stack" aria-labelledby="w-h">
          <div className="panel-head" style={{ marginBottom: 0 }}>
            <h1 id="w-h">Tool wallet</h1>
            <Badge tone="info">pay-per-call</Badge>
            <span className="spacer" />
            <strong style={{ fontSize: 20 }} aria-label="Balance">{money(w?.balance_cents ?? ctx.session.wallet_balance_cents)}</strong>
          </div>
          <p className="small muted">
            A separate PayPal checkout for every 1–2¢ tool call isn't practical, so the wallet works like a prepaid card. You top up once
            through PayPal checkout. Every quote, negotiation round and market check is then debited automatically, exactly once per tool
            call, within per-run and daily caps. Providers are paid out in batches with PayPal Payouts. The starting 50¢ is a synthetic
            demo grant, not PayPal money.
          </p>
          <div className="row">
            <span className="small" style={{ fontWeight: 600 }}>Top up with PayPal:</span>
            {(w?.topup_amounts_cents ?? []).map((a) => (
              <button key={a} className="btn small" disabled={act.busy} onClick={() => topUp(a)}>{money(a)}</button>
            ))}
          </div>
          <ErrorNotice error={load.error ?? act.error} />
          <div className="grid-box short">
            <AgGridReact<Usage> theme={gridTheme} rowData={w?.usage ?? []} columnDefs={columns}
              defaultColDef={{ sortable: true, resizable: true, filter: true }} getRowId={(p) => String(p.data.id)} />
          </div>
        </section>

        <section className="panel stack" aria-labelledby="po-h">
          <div className="panel-head" style={{ marginBottom: 0 }}>
            <h2 id="po-h">Provider payouts</h2>
            <Badge tone={ctx.config.payment_mode === "paypal_sandbox" ? "ok" : "warn"}>
              {ctx.config.payment_mode === "paypal_sandbox" ? "PayPal Payouts (Sandbox)" : "offline fixture"}
            </Badge>
            <span className="spacer" />
            <button className="btn primary" disabled={act.busy || !w?.unsettled_cents}
              onClick={() => act.run(async () => { await api.post("/api/wallet/settle"); await refresh(); })}>
              {act.busy ? <Spinner label="Settling…" /> : `Settle ${cents(w?.unsettled_cents ?? 0)} owed`}
            </button>
          </div>
          <p className="small muted">
            Each settlement claims the unpaid calls and sends one Payouts batch per provider with a stable batch ID, so repeating it can never
            pay a provider twice. In production this runs on a schedule.
          </p>
          <table className="simple small">
            <thead><tr><th>Provider</th><th className="num">Amount</th><th className="num">Calls</th><th>Status</th><th>PayPal batch</th><th /></tr></thead>
            <tbody>
              {(w?.payouts ?? []).map((b) => (
                <tr key={b.payout_id}>
                  <td>{b.provider_id}</td>
                  <td className="num">{cents(b.amount_cents)}</td>
                  <td className="num">{b.item_count}</td>
                  <td><Badge tone={PAYOUT_TONE[b.status]}>{humanize(b.status)}</Badge>{b.last_error && <div className="muted">{b.last_error}</div>}</td>
                  <td className="mono">{b.paypal_batch_id ?? "—"}</td>
                  <td>
                    {["submitted", "unknown", "pending"].includes(b.status) && (
                      <button className="btn small" disabled={act.busy}
                        onClick={() => act.run(async () => { await api.post(`/api/wallet/payouts/${b.payout_id}/refresh`); await refresh(); })}>
                        Check status
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {(w?.payouts ?? []).length === 0 && <tr><td colSpan={6} className="muted">No payouts yet.</td></tr>}
            </tbody>
          </table>
        </section>
      </div>

      <aside className="stack">
        <section className="panel stack" aria-labelledby="pr-h">
          <h2 id="pr-h">Price list</h2>
          <table className="simple small">
            <thead><tr><th>Tool</th><th>Provider</th><th className="num">Price</th></tr></thead>
            <tbody>{(w?.tools ?? []).map((t) => <tr key={t.code}><td>{t.name}</td><td>{t.provider_name}</td><td className="num">{cents(t.price_cents)}</td></tr>)}</tbody>
          </table>
        </section>
        <section className="panel stack" aria-labelledby="sp-h">
          <h2 id="sp-h">Owed to providers</h2>
          <table className="simple small">
            <tbody>{owed.map(([p, c]) => <tr key={p}><td>{p}</td><td className="num">{cents(c)}</td></tr>)}</tbody>
          </table>
          {owed.length === 0 && <p className="small muted">Nothing owed.</p>}
        </section>
        <section className="panel stack" aria-labelledby="lg-h">
          <h2 id="lg-h">Wallet ledger</h2>
          <ul className="list-plain small">
            {(w?.ledger ?? []).filter((l) => l.kind !== "purchase_paid").slice(0, 14).map((l, i) => (
              <li key={i} className="row" style={{ justifyContent: "space-between" }}>
                <span>{humanize(l.kind)}</span><span className="num">{l.amount_cents > 0 ? "+" : ""}{money(l.amount_cents)}</span>
              </li>
            ))}
          </ul>
        </section>
      </aside>
    </div>
  );
}
