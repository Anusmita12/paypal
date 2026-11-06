# TradePilot AI — Build Plan and Handoff Checklist

Status: **plan approved pending** · Started 2026-11-06 · Deadline: 6 days (submission ~2026-11-12)
Rule: build and verify everything **locally first**; hosting (Render, public GitHub) only after the local build works.

This file is the source of truth for progress. Each phase ends with a commit and a status update in the checklist at the bottom.

---

## 1. What we are building (one paragraph)

**TradePilot AI — agentic commerce with PayPal guardrails across the whole transaction.**
A small electronics retailer's AI buyer agent (Claude) **pays per call** for supplier and market-data tools from a prepaid PayPal-funded balance, **negotiates and buys** a restock basket that the owner approves to the exact cart, pays through **PayPal Sandbox checkout**, and — on the seller side — an AI **dispute defense** agent assembles evidence from the immutable records if a buyer disputes the payment. The AI proposes; deterministic backend rules and humans control money.

### Two personas, one demo
| Persona | Screen | What they do |
|---|---|---|
| Retailer owner (buyer) | Inventory, Procurement, Approval, Wallet | Tops up the agent's tool balance, confirms requirements, approves exact cart, pays with PayPal |
| Storefront operator (seller) | Disputes, Payouts, Dashboard | Receives payment, pays tool providers via Payouts, defends disputes with AI-drafted evidence |

Both personas live in the same app (separate tabs) so the demo shows the full lifecycle. All suppliers are simulated offer sources behind **one** PayPal Sandbox business account — no real multi-merchant settlement.

---

## 2. The three features and how they connect

### A. Pay-per-call tool marketplace (before buying)
- A **tool registry** (DB table) lists paid tools with fixed prices, e.g.:
  - `supplier_quote` — 1¢ per quote request (each simulated supplier charges)
  - `supplier_counteroffer` — 1¢ per negotiation round
  - `market_price_check` — 2¢, calls the **real Channel3 API** to compare a quote with real retail prices
- Real PayPal per 2¢ transaction is not viable (fixed fees, each checkout needs buyer approval). So:
  1. Owner **tops up a prepaid balance once** (e.g. $5) via PayPal Orders checkout — one human approval for the whole budget.
  2. Every paid tool call **debits automatically** — no human. Server enforces per-call price, per-run cap, daily cap, and balance. Unique key per tool call ⇒ a retried call never charges twice.
  3. Provider earnings accrue in a ledger and are **settled with the PayPal Payouts API** (Sandbox) in batches to separate sandbox accounts. `sender_batch_id` is stable per settlement ⇒ retries cannot double-pay.
- Demo: live "micro-payments" ticker adding up during the agent run, then a real Payouts batch.

### B. Procurement + checkout (buying) — the original spec
Requirements → confirmation → quotes → bounded negotiation → surplus alternatives → deterministic comparison → immutable approval snapshot (digest) → stock reservation → PayPal order create → buyer approves in official PayPal popup → server capture → validate amount/currency/recipient → shared idempotent finalizer → ledger + **incoming** stock.
Main fixture story: A $470 → revised $525 (stale); B $510 → negotiated $488 (with compatible surplus); C rejected (incompatible/late). Totals derived from line items.

### C. Dispute defense (after selling)
- PayPal webhook `CUSTOMER.DISPUTE.CREATED/UPDATED/RESOLVED` → signature-verified inbox → job.
- Server fetches the dispute (Disputes API), maps it to our purchase by capture/transaction ID.
- Claude gets **read-only** tools: purchase record, approved snapshot + digest, capture evidence, shipment tracking, message log. It drafts an evidence response that **must cite record IDs**; backend rejects drafts citing records that don't exist.
- Operator reviews and clicks **Approve & submit** → server calls `provide-evidence`.
- We also push shipment tracking to PayPal (`/v2/checkout/orders/{id}/track`) at fulfillment, which strengthens evidence. Carrier/shipment data is **simulated** and labeled.
- Risk: Sandbox dispute creation + evidence submission must be verified on day 3. Fallback: evidence package generated and shown, labeled "submission not verified in Sandbox".

---

## 3. Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Backend | Python 3.13, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, psycopg 3, HTTPX | `uv` for env + `uv.lock` |
| Database | PostgreSQL 17 (Homebrew locally) | Separate `tradepilot` and `tradepilot_test` DBs |
| AI | Official `anthropic` Python SDK, client-side tool use, bounded loop | Model ID set in `.env`, verified against your account |
| Payments | PayPal Sandbox REST: Orders v2, Payouts v1, Disputes v1, Webhooks verify, Order tracking | Server-side only; browser uses official JS SDK buttons |
| Frontend | React + TypeScript (strict) + Vite, plain CSS tokens | |
| Grids | AG Grid Community — inventory, offers, tool usage ledger, disputes | MIT, no Enterprise imports |
| Dashboard | **AG Studio** (trial license via env var) — spend/usage/dispute dashboard + Studio Agent Framework widget builder | Day 5; app runs without it if no license |
| Market data | **Channel3** API (`market_price_check` paid tool) | `CHANNEL3_MODE=fixture|live` |
| Background jobs | Postgres `jobs` table + in-process async poller (lease, retry, backoff) | One worker — documented MVP ceiling |
| Tests | Pytest (domain, payment, races), Playwright (one offline critical flow) | |
| Packaging (later) | Dockerfile, compose.yaml, render.yaml, GitHub Actions CI | Docker verified once Docker/OrbStack is installed |
| Build helper | APIMatic PayPal Context Plugin in Claude Code (if available) | For PayPal API accuracy; credited in submission |

### Runtime modes (visible badge in UI)
`AI_MODE=fixture|anthropic`, `PAYMENT_MODE=fixture|paypal_sandbox`, `CHANNEL3_MODE=fixture|live`, `ENABLE_SCENARIOS=true|false`.
Fixture modes run the real UI/API/DB/business code with deterministic fake adapters. Selecting a live mode without credentials = clear startup error, **never silent fallback**.

---

## 4. Repository layout

```
README.md  CLAUDE_BUILD_PROMPT.md  LICENSE(placeholder)  .env.example  .gitignore
Dockerfile  compose.yaml  render.yaml  .github/workflows/ci.yml
backend/
  pyproject.toml  uv.lock  alembic.ini  migrations/
  app/
    main.py config.py db.py models.py schemas.py api.py security.py
    restock.py procurement.py suppliers.py compare.py approvals.py
    agent.py agent_tools.py
    wallet.py tool_market.py payouts.py channel3.py
    payments.py paypal_client.py finalizer.py webhooks.py
    disputes.py dispute_agent.py
    jobs.py cli.py seed.py
  tests/
frontend/  src/ (pages, grids, api client, styles)  e2e/
fixtures/  products.csv sales.csv suppliers.json tools.json scenarios/
postman/   collection + example environment (optional, low priority)
docs/      PLAN.md build-spec.md build-spec-amendment.md architecture.md verification.md demo-script.md submission.md
```

---

## 5. Core data model (abridged)

- `demo_sessions` (cookie-scoped retailer), `products`, `inventory`, `sales`, `imports`
- `suppliers`, `supplier_stock`, `supplier_price_rules`, `surplus_offers`
- `procurements` (requirements + state), `agent_runs`, `agent_events`, `tool_calls`
- `quotes`, `quote_items`, `negotiation_rounds`
- `proposals` (immutable snapshot JSON + digest + version), `approvals`, `reservations`
- `purchases` (kind: `restock` | `wallet_topup`), `provider_operations` (request ID, payload digest, state), `paypal_orders`, `captures`
- `ledger_entries` (unique business key), `webhook_events` (unique provider event ID), `jobs`
- `paid_tools`, `wallets`, `tool_usage` (unique per tool call), `payout_batches`, `payout_items`
- `shipments`, `disputes`, `dispute_evidence_drafts`

State machines (in code, with allowed-transition tables): procurement run, proposal, purchase/payment, reservation, job, dispute.

---

## 6. Phases and schedule

Each phase leaves the app runnable, ends with tests run + a local git commit + a status report.

| Phase | Day | Scope | Gate (must be true to finish) | Needs from owner |
|---|---|---|---|---|
| **0. Setup** | 1 (≈1h) | Postgres + uv install, git init, scaffold backend/frontend, `.env.example`, config validation, health/ready endpoints | `GET /api/ready` OK against local Postgres | — |
| **1. Domain core** | 1 | Models + migrations, seed (20 products, 28-day sales, 3 suppliers, paid tools), demo sessions (cookie + CSRF), restock math, supplier rules, quotes/expiry/revisions, negotiation policy, compatibility, comparison, snapshot+digest approval, atomic reservations, wallet debit, jobs poller | Pytest: fixture story totals add up ($470/$525/$510/$488/C rejected), discount floor, infeasible basket, stale approval, concurrent reservations, wallet double-debit | — |
| **2. Offline product** | 2 | React UI: Inventory grid, requirement confirm, procurement run timeline, offer comparison grid, approval, purchases, wallet + tool-usage ledger; fixture AI + fixture PayPal end-to-end | Playwright offline flow passes; keyboard path works | — |
| **3. Real AI + PayPal** | 3 | Claude tool loop (limits, schema validation, injection defense), PayPal create/capture/retrieve, official JS buttons, webhook verify inbox, shared finalizer, reconciliation jobs, timeout/duplicate/restart tests. **Verify Sandbox disputes are possible.** | One real Sandbox restock purchase reconciled once; fault-injection tests pass | Anthropic key + model, PayPal Sandbox app, public HTTPS tunnel for webhooks |
| **4. Pay-per-call** | 4 | Wallet top-up via PayPal, metered paid tools, Channel3 market check, caps, Payouts settlement, live ticker UI | Real top-up + real Payouts batch in Sandbox; no double charge on retries | Channel3 key, Payouts enabled on Sandbox app |
| **5. Disputes + dashboard** | 5 | Dispute webhook/inbox/fetch, AI evidence drafting with citations, approve & submit, order tracking push, AG Studio dashboard + agent-built widget | Real Sandbox dispute handled (or fallback documented) | AG Studio trial license |
| **6. Delivery** | 6 | Polish, accessibility, README rewrite, docs (verification, demo script, submission), Dockerfile/compose/render.yaml/CI, demo recording support | Fresh-clone local setup works from README | Docker/OrbStack (to verify image), license name |
| **Hosting** | after 6 or in parallel on 6 | Push to GitHub, Render deploy, re-point webhooks | Hosted URL works | GitHub + Render accounts, your go-ahead |

**Cut order if behind:** Postman → Hindi → AG Studio agent widget (keep dashboard) → dispute submission (keep draft) → Bryntum (not planned). Never cut payment correctness.

---

## 7. How I (Claude Code) work

- **Tools:** shell (installs, migrations, tests, servers), file editing, the built-in browser pane to click through the app and screenshot the UI, local git commits.
- **You do:** paste secrets into `.env` yourself (never needed in chat); log in as the PayPal Sandbox **buyer** in the checkout popup when we test real payments (I will not type PayPal passwords); create accounts/keys; approve anything public (pushes, deploys).
- **Never without asking:** pushing to GitHub, deploying, creating cloud resources, spending money.
- **Honesty rules:** fixture runs are labeled fixture; "verified" only after a command actually passed; external checks not yet run are listed as pending in `docs/verification.md`.

### Updates
- End of every phase: report (what works, commands run + real results, what's next, what I need) + push notification.
- During long phases: progress lines roughly hourly, plus the live task list.
- Blockers: immediate question.

---

## 8. Progress checklist

- [x] Phase 0 — Setup (Postgres 17 via Homebrew, uv, FastAPI, Alembic, health/ready, config validation)
- [x] Phase 1 — Domain core (35 pytest tests passing; fixture buyer run reproduces $470 → $525 / $510 → $488 / C rejected)
  - Decision: negotiation limit is 3 rounds per supplier **per run** (a re-run after changed terms may negotiate again).
  - Decision: each new demo session gets a 50¢ synthetic starter tool credit (ledger kind `wallet_grant`, not PayPal money).
  - CSV imports moved to Phase 2 (done there).
- [x] Phase 2 — Offline product (verified 2026-11-06)
  - Backend: fixture PayPal adapter (Orders v2 shapes, request-ID idempotency, one-shot fault injection), create order / capture
    with persisted intent, one shared `finalize_from_order` used by capture, reconciliation (and Phase 3 webhooks), reconcile job
    + `reconcile-pending` CLI, purchase event timeline, evidence JSON/CSV, CSV imports preview/apply, TTL maintenance loop.
  - UI: React + strict TS + Vite, AG Grid Community (inventory grid, offer grid, tool-usage grid), requirement confirmation, agent
    timeline, negotiation table, exact-cart approval, two-step checkout, fixture buyer stand-in (clearly labeled), PayPal JS SDK
    buttons wired for Sandbox (unverified until Phase 3), purchases + evidence, tool wallet, scenarios, mobile offer summary.
  - Tests: 50 pytest (incl. lost create/capture responses, concurrent captures, duplicate finalization, amount mismatch → manual
    review, expired reservation, cross-session), 3 Playwright (main story by keyboard, wallet metering, mobile layout).
  - Decision: added "within N days" deadline hint; the sample request uses it so the demo works on any business date.
  - Decision: supplier terms are frozen during the reservation window; create/capture revalidate snapshot digest + reservation.
- [ ] Phase 3 — Real AI + PayPal
  - Built and tested offline (2026-11-06): `agent_llm.run_claude` manual tool loop (strict tool schemas, adaptive
    thinking with `AI_EFFORT`, server-side refusal fallback on supported models, token accounting, no fixture fallback),
    explanation grounding check (dollar amounts must exist in server facts), `paypal_client.SandboxPayPal` (OAuth cache,
    PayPal-Request-Id, timeout/5xx/429 → unknown, 4xx → rejected issue code, 401 refresh), verified webhook inbox
    (`/api/webhooks/paypal` → store → `process_webhook` job → re-read order → shared finalizer). 62 pytest passing.
  - NOT yet verified against real Claude or real PayPal Sandbox: waiting for credentials + tunnel.
- [ ] Phase 4 — Pay-per-call
- [ ] Phase 5 — Disputes + dashboard
- [ ] Phase 6 — Delivery
- [ ] Hosting

### Pending external verification
- Real Claude run (model ID TBD)
- Real PayPal Sandbox purchase, top-up, Payouts batch, dispute
- Channel3 live call
- Docker image build, Render deploy
