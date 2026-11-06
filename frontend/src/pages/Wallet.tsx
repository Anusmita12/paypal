import { useEffect, useMemo, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import type { ColDef } from "ag-grid-community";
import { api } from "../api";
import type { Ctx } from "../App";
import { gridTheme } from "../grid";
import { cents, humanize, money, when } from "../format";
import type { Wallet } from "../types";
import { Badge, ErrorNotice, useAction } from "../ui";

type Usage = Wallet["usage"][number];

export default function WalletPage({ ctx }: { ctx: Ctx }) {
  const [w, setW] = useState<Wallet | null>(null);
  const load = useAction();
  useEffect(() => {
    load.run(async () => setW(await api.get<Wallet>("/api/wallet")));
    ctx.refreshSession();
  }, [load.run]);

  const columns = useMemo<ColDef<Usage>[]>(() => [
    { field: "at", headerName: "When", width: 170, valueFormatter: (p) => when(p.value), sort: "desc" },
    { field: "tool_name", headerName: "Tool", flex: 1, minWidth: 180 },
    { field: "provider_id", headerName: "Provider", width: 140 },
    { field: "price_cents", headerName: "Price", width: 90, type: "numericColumn", valueFormatter: (p) => cents(p.value) },
    { field: "settled", headerName: "Provider payout", width: 150, valueFormatter: (p) => (p.value ? "settled" : "accrued") },
    { field: "run_id", headerName: "Agent run", width: 140, valueFormatter: (p) => (p.value ? String(p.value).slice(0, 8) : "—") },
  ], []);

  const spentByProvider = useMemo(() => {
    const out = new Map<string, number>();
    for (const u of w?.usage ?? []) out.set(u.provider_id, (out.get(u.provider_id) ?? 0) + u.price_cents);
    return [...out.entries()];
  }, [w]);

  return (
    <div className="grid-2">
      <section className="panel stack" aria-labelledby="w-h">
        <div className="panel-head" style={{ marginBottom: 0 }}>
          <h1 id="w-h">Tool wallet</h1>
          <Badge tone="info">pay-per-call</Badge>
          <span className="spacer" />
          <strong style={{ fontSize: 20 }}>{money(w?.balance_cents ?? ctx.session.wallet_balance_cents)}</strong>
        </div>
        <p className="small muted">
          Every quote, negotiation and market check the agent makes costs a few cents, debited exactly once per tool call. Top-ups go through
          PayPal checkout and providers are settled with PayPal Payouts (both arrive in Phase 4). The starting 50¢ is a synthetic demo grant, not PayPal money.
        </p>
        <ErrorNotice error={load.error} />
        <div className="grid-box short">
          <AgGridReact<Usage> theme={gridTheme} rowData={w?.usage ?? []} columnDefs={columns} defaultColDef={{ sortable: true, resizable: true, filter: true }} getRowId={(p) => String(p.data.id)} />
        </div>
      </section>
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
            <tbody>{spentByProvider.map(([p, c]) => <tr key={p}><td>{p}</td><td className="num">{cents(c)}</td></tr>)}</tbody>
          </table>
          {spentByProvider.length === 0 && <p className="small muted">No tool calls yet.</p>}
        </section>
        <section className="panel stack" aria-labelledby="lg-h">
          <h2 id="lg-h">Ledger</h2>
          <ul className="list-plain small">
            {(w?.ledger ?? []).filter((l) => l.kind !== "purchase_paid").slice(0, 12).map((l, i) => (
              <li key={i} className="row" style={{ justifyContent: "space-between" }}>
                <span>{humanize(l.kind)}</span><span className="num">{l.amount_cents > 0 ? "+" : ""}{cents(l.amount_cents)}</span>
              </li>
            ))}
          </ul>
        </section>
      </aside>
    </div>
  );
}
