import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { go, type Ctx } from "../App";
import { humanize, money, when } from "../format";
import type { Dispute } from "../types";
import { Badge, ErrorNotice, Live, Spinner, useAction, usePolling } from "../ui";

const TONE: Record<Dispute["status"], "ok" | "warn" | "bad" | "info" | "plain"> = {
  open: "warn", drafting: "info", draft_ready: "warn", submitting: "info", submitted: "ok", unknown: "bad", failed: "bad", resolved: "plain",
};

export default function DisputesPage({ ctx, id }: { ctx: Ctx; id?: string }) {
  const [list, setList] = useState<Dispute[]>([]);
  const load = useAction();
  const refresh = useCallback(async () => {
    setList((await api.get<{ disputes: Dispute[] }>("/api/disputes")).disputes);
  }, []);
  useEffect(() => {
    load.run(refresh);
  }, [load.run, refresh]);
  const current = list.find((d) => d.dispute_id === id) ?? (id ? undefined : list[0]);
  usePolling(list.some((d) => d.status === "drafting" || d.status === "submitting"), () => refresh().catch(() => undefined), 1200);

  return (
    <div className="grid-2">
      <div className="stack">
        <ErrorNotice error={load.error} />
        {list.length === 0 && !load.busy && (
          <section className="panel stack">
            <h1>Dispute desk</h1>
            <p className="muted">
              This is the storefront's side. When a buyer disputes a captured payment, PayPal notifies TradePilot. The agent drafts a response
              from the store's own records, citing each one, and nothing is sent to PayPal until you approve the exact text.
            </p>
            <p className="small muted">
              {ctx.config.payment_mode === "fixture"
                ? "To try it offline: open a paid restock purchase, ship it, then use “Simulate buyer dispute”."
                : "To try it in Sandbox: log in to sandbox PayPal as the buyer and open a dispute on a paid order."}
            </p>
            <button className="btn" onClick={() => go({ page: "purchases" })}>Go to purchases</button>
          </section>
        )}
        {current && <DisputeDetail key={current.dispute_id} d={current} refresh={refresh} />}
      </div>
      <aside className="panel stack" aria-labelledby="dl-h">
        <h2 id="dl-h">Disputes</h2>
        <ul className="list-plain proc-list">
          {list.map((d) => (
            <li key={d.dispute_id}>
              <button aria-current={d.dispute_id === current?.dispute_id ? "true" : undefined} onClick={() => go({ page: "disputes", id: d.dispute_id })}>
                <div className="row"><Badge tone={TONE[d.status]}>{humanize(d.status)}</Badge><strong>{money(d.amount_cents)}</strong></div>
                <div className="small muted">{humanize(d.reason.toLowerCase())} · {d.paypal_dispute_id}</div>
              </button>
            </li>
          ))}
          {list.length === 0 && <li className="small muted">No disputes.</li>}
        </ul>
      </aside>
    </div>
  );
}

function DisputeDetail({ d, refresh }: { d: Dispute; refresh: () => Promise<void> }) {
  const act = useAction();
  const [focus, setFocus] = useState<string | null>(null);
  const [announce, setAnnounce] = useState("");
  const draft = d.draft;
  const due = d.seller_response_due ? new Date(d.seller_response_due) : null;

  async function post(path: string, body?: unknown, msg = "") {
    await act.run(async () => {
      try {
        await api.post(`/api/disputes/${d.dispute_id}/${path}`, body);
      } finally {
        await refresh();
      }
      setAnnounce(msg);
    });
  }

  return (
    <>
      <section className="panel stack">
        <div className="panel-head" style={{ marginBottom: 0 }}>
          <h1>Dispute {d.paypal_dispute_id}</h1>
          <Badge tone={TONE[d.status]}>{humanize(d.status)}</Badge>
          <span className="spacer" />
          <strong style={{ fontSize: 18 }}>{money(d.amount_cents)}</strong>
        </div>
        <dl className="kv small">
          <dt>Buyer's reason</dt><dd>{humanize(d.reason.toLowerCase())}</dd>
          <dt>PayPal status</dt><dd>{humanize(d.paypal_status.toLowerCase())}{d.lifecycle_stage && ` · ${d.lifecycle_stage.toLowerCase()}`}</dd>
          <dt>Respond by</dt><dd>{due ? when(d.seller_response_due) : "—"}</dd>
          <dt>Mode</dt><dd>{d.payment_mode === "fixture" ? <Badge tone="warn">simulated dispute — offline fixture</Badge> : <Badge tone="ok">PayPal Sandbox</Badge>}</dd>
          <dt>Purchase</dt><dd>{d.purchase_id ? <button className="btn small" onClick={() => go({ page: "purchases", id: d.purchase_id! })}>Open purchase</button> : "not matched"}</dd>
          <dt>Shipment</dt><dd>{d.shipment ? `${d.shipment.carrier} · ${d.shipment.tracking_number}${d.shipment.delivered_at ? " · delivered" : ""} · tracking ${d.shipment.tracker_state} on PayPal order` : "none recorded"}</dd>
        </dl>
        <div className="row">
          {["open", "draft_ready", "failed"].includes(d.status) && (
            <button className="btn primary" disabled={act.busy} onClick={() => post("draft", undefined, "Drafting evidence.")}>
              {draft ? "Redraft evidence" : "Draft evidence with AI"}
            </button>
          )}
          {d.status === "drafting" && <Spinner label="Gathering records and drafting…" />}
          <button className="btn" disabled={act.busy} onClick={() => post("refresh", undefined, "Status checked.")}>Check PayPal status</button>
        </div>
        {d.draft_error && <div className="notice bad">Draft rejected: {d.draft_error}</div>}
        {d.status === "unknown" && <div className="notice bad">PayPal did not confirm the submission. Check status: the server never resends with a new request ID.</div>}
        {d.submission_error && d.status === "failed" && <div className="notice bad">PayPal rejected the submission: {d.submission_error}</div>}
        <ErrorNotice error={act.error} onDismiss={() => act.setError(null)} />
      </section>

      {draft && (
        <section className={`panel stack ${d.status === "draft_ready" ? "cart" : ""}`} aria-labelledby="dr-h">
          <div className="panel-head" style={{ marginBottom: 0 }}>
            <h2 id="dr-h">Proposed response</h2>
            <Badge tone={draft.draft.recommendation === "contest" ? "ok" : "warn"}>recommend: {draft.draft.recommendation}</Badge>
            <Badge tone={draft.model === "fixture-template" ? "warn" : "info"}>
              {draft.model === "fixture-template" ? "deterministic template (fixture mode)" : `drafted by ${draft.model}; citations checked by the server`}
            </Badge>
          </div>
          <p>{draft.draft.summary}</p>
          <ol className="stack" style={{ paddingLeft: 18, margin: 0 }}>
            {draft.draft.claims.map((c, i) => (
              <li key={i}>
                {c.text}{" "}
                {c.citations.map((cid) => (
                  <button key={cid} className="badge info plain" style={{ cursor: "pointer", border: 0 }} aria-pressed={focus === cid}
                    onClick={() => setFocus(focus === cid ? null : cid)}>{cid.split(":")[0]}</button>
                ))}
              </li>
            ))}
          </ol>
          <details open={!!focus}>
            <summary className="small">Records the agent was given ({draft.records.length}); click a citation to highlight it</summary>
            <ul className="list-plain small">
              {draft.records.map((r) => (
                <li key={r.id} className="panel" style={{ padding: 8, borderColor: focus === r.id ? "var(--accent)" : undefined }}>
                  <div className="mono">{r.id}</div>
                  <pre className="mono" style={{ margin: 0, whiteSpace: "pre-wrap" }}>{JSON.stringify(Object.fromEntries(Object.entries(r).filter(([k]) => k !== "id")), null, 1)}</pre>
                </li>
              ))}
            </ul>
          </details>
          <h3>Exactly what will be sent to PayPal</h3>
          {draft.submission.map((e, i) => (
            <div key={i} className="panel small" style={{ padding: 10 }}>
              <div><strong>{humanize(e.evidence_type.toLowerCase())}</strong>{e.evidence_info && ` · tracking ${e.evidence_info.tracking_info.map((t) => t.tracking_number).join(", ")}`}</div>
              <pre className="mono" style={{ whiteSpace: "pre-wrap", margin: "6px 0 0" }}>{e.notes}</pre>
            </div>
          ))}
          {d.status === "draft_ready" && (
            <>
              {d.can_provide_evidence === false && <div className="notice warn">PayPal is not currently accepting seller evidence for this dispute.</div>}
              <button className="btn primary big" disabled={act.busy || d.can_provide_evidence === false}
                onClick={() => post("approve", { digest: d.draft_digest }, "Evidence sent to PayPal.")}>
                Approve and send this evidence to PayPal
              </button>
              <p className="small muted">Approval is tied to this exact text (digest {d.draft_digest?.slice(0, 12)}…). A redraft needs a fresh approval.</p>
            </>
          )}
          {d.status === "submitted" && <div className="notice ok" role="status">Evidence submitted {when(d.approved_at)}. PayPal status: {humanize(d.paypal_status.toLowerCase())}.</div>}
        </section>
      )}
      <Live text={announce} />
    </>
  );
}
