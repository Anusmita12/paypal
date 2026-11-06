import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import type { AppConfig, ProcurementStatus, PurchaseStatus } from "./types";
import { humanize } from "./format";

type Tone = "ok" | "warn" | "bad" | "info" | "plain";

const PROCUREMENT_TONE: Record<ProcurementStatus, Tone> = {
  needs_input: "warn",
  draft: "info",
  planning: "info",
  negotiating: "info",
  awaiting_approval: "warn",
  approved: "ok",
  no_feasible_offer: "bad",
  failed: "bad",
};

const PURCHASE_TONE: Record<PurchaseStatus, Tone> = {
  creating: "info",
  awaiting_paypal_approval: "warn",
  capture_pending: "warn",
  paid: "ok",
  unknown: "bad",
  cancelled: "plain",
  failed: "bad",
  manual_review: "bad",
};

export function Badge({ tone = "plain", children, title }: { tone?: Tone; children: React.ReactNode; title?: string }) {
  return (
    <span className={`badge ${tone}`} title={title}>
      {children}
    </span>
  );
}

export function ProcurementBadge({ status }: { status: ProcurementStatus }) {
  return <Badge tone={PROCUREMENT_TONE[status]}>{humanize(status)}</Badge>;
}

export function PurchaseBadge({ status }: { status: PurchaseStatus }) {
  const label = status === "unknown" ? "outcome unknown" : humanize(status);
  return <Badge tone={PURCHASE_TONE[status]}>{label}</Badge>;
}

export function ModeBadges({ config }: { config: AppConfig }) {
  const ai = config.ai_mode === "fixture" ? "AI: fixture script" : `AI: ${config.ai_mode === "gemini" ? "Gemini" : "Claude"} · ${config.ai_model ?? ""}`;
  const pay = config.payment_mode === "paypal_sandbox" ? "PayPal: Sandbox" : "PayPal: offline fixture";
  return (
    <>
      <Badge tone={config.ai_mode === "fixture" ? "warn" : "ok"} title="Which buyer-agent driver is running">
        {ai}
      </Badge>
      <Badge tone={config.payment_mode === "paypal_sandbox" ? "ok" : "warn"} title="Which payment provider is configured">
        {pay}
      </Badge>
    </>
  );
}

export function ErrorNotice({ error, onDismiss }: { error: unknown; onDismiss?: () => void }) {
  if (!error) return null;
  const e = error instanceof ApiError ? error : null;
  const reason = e?.details && typeof e.details.reason === "string" ? e.details.reason : null;
  return (
    <div className="notice bad fade-in" role="alert">
      <div className="row">
        <strong>{e ? humanize(e.code) : "Something went wrong"}</strong>
        {onDismiss && (
          <button className="btn small" style={{ marginLeft: "auto" }} onClick={onDismiss}>
            Dismiss
          </button>
        )}
      </div>
      <p>{e ? e.message : String(error)}</p>
      {reason && <p className="small">Reason: {humanize(reason)}</p>}
      {e?.requestId && <p className="small mono">request {e.requestId}</p>}
    </div>
  );
}

/** Run an async action with a busy flag and captured error. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const run = useCallback(async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (e) {
      setError(e);
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, error, setError, run };
}

/** Poll while `active`; stops itself when the predicate goes false. No permanently spinning widgets. */
export function usePolling(active: boolean, fn: () => void, ms = 900) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => ref.current(), ms);
    return () => window.clearInterval(id);
  }, [active, ms]);
}

export function Spinner({ label }: { label: string }) {
  return (
    <span className="row muted" role="status">
      <span className="spinner" aria-hidden="true" /> {label}
    </span>
  );
}

export function Live({ text }: { text: string }) {
  return (
    <div className="sr-only" aria-live="polite">
      {text}
    </div>
  );
}
