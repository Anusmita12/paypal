import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { money } from "./format";
import type { AppConfig, DemoSession } from "./types";
import { ErrorNotice, ModeBadges, Spinner } from "./ui";
import InventoryPage from "./pages/Inventory";
import ProcurementPage from "./pages/Procurement";
import PurchasesPage from "./pages/Purchases";
import WalletPage from "./pages/Wallet";

export type Route =
  | { page: "inventory" }
  | { page: "procurement"; id?: string }
  | { page: "purchases"; id?: string }
  | { page: "wallet" };

function parseHash(): Route {
  const [, page, id] = window.location.hash.replace(/^#/, "").split("/");
  if (page === "procurement") return { page, id };
  if (page === "purchases") return { page, id };
  if (page === "wallet") return { page };
  return { page: "inventory" };
}

export function go(route: Route) {
  const id = "id" in route && route.id ? `/${route.id}` : "";
  window.location.hash = `/${route.page}${id}`;
}

const NAV: { page: Route["page"]; label: string }[] = [
  { page: "inventory", label: "Inventory" },
  { page: "procurement", label: "Procurement" },
  { page: "purchases", label: "Purchases" },
  { page: "wallet", label: "Tool wallet" },
];

export interface Ctx {
  config: AppConfig;
  session: DemoSession;
  refreshSession: () => void;
}

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [session, setSession] = useState<DemoSession | null>(null);
  const [route, setRoute] = useState<Route>(parseHash);
  const [error, setError] = useState<unknown>(null);

  const refreshSession = useCallback(() => {
    api.get<DemoSession>("/api/demo/session").then(setSession).catch(() => undefined);
  }, []);

  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    (async () => {
      try {
        setConfig(await api.get<AppConfig>("/api/config"));
        try {
          setSession(await api.get<DemoSession>("/api/demo/session"));
        } catch {
          setSession(await api.post<DemoSession>("/api/demo/session"));
        }
      } catch (e) {
        setError(e);
      }
    })();
  }, []);

  async function resetDemo() {
    if (!window.confirm("Start a fresh demo retailer? Your current session stays on the server but you will leave it.")) return;
    try {
      setSession(await api.post<DemoSession>("/api/demo/session"));
      go({ page: "inventory" });
      window.location.reload();
    } catch (e) {
      setError(e);
    }
  }

  if (error)
    return (
      <main className="main">
        <ErrorNotice error={error} />
        <button className="btn" onClick={() => window.location.reload()}>Retry</button>
      </main>
    );
  if (!config || !session) return <main className="main"><Spinner label="Starting your demo retailer…" /></main>;

  const ctx: Ctx = { config, session, refreshSession };
  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <svg width="18" height="18" viewBox="0 0 32 32"><path d="M5 23l7-12 5 7 3-4 7 9" stroke="white" strokeWidth="3" fill="none" strokeLinecap="round" strokeLinejoin="round" /></svg>
          </span>
          <span>TradePilot AI</span>
        </div>
        <nav className="nav" aria-label="Main">
          {NAV.map((n) => (
            <button key={n.page} aria-current={route.page === n.page ? "page" : undefined} onClick={() => go({ page: n.page } as Route)}>
              {n.label}
            </button>
          ))}
        </nav>
        <div className="topbar-right">
          <ModeBadges config={config} />
          <span className="badge plain" title="Prepaid balance for pay-per-call agent tools">
            Tools {money(session.wallet_balance_cents)}
          </span>
          <span className="badge plain" title={`Business date in ${session.timezone}`}>
            {session.business_date}
          </span>
          <button className="btn small" onClick={resetDemo}>New demo</button>
        </div>
      </header>
      <main className="main" id="main">
        {route.page === "inventory" && <InventoryPage ctx={ctx} />}
        {route.page === "procurement" && <ProcurementPage ctx={ctx} id={route.id} />}
        {route.page === "purchases" && <PurchasesPage ctx={ctx} id={route.id} />}
        {route.page === "wallet" && <WalletPage ctx={ctx} />}
      </main>
    </div>
  );
}
