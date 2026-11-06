# Claude Code build prompt — TradePilot AI

Place this file and the supplied `README.md` in the root of the project folder. Open that folder in Claude Code and paste the prompt below. This is an implementation assignment; it does not require the coding model to also be the runtime model. Runtime API access is configured separately.

---

You are the implementation engineer for **TradePilot AI**, a PayPal + AI hackathon project and a serious Forward Deployed Engineering learning project. Build a working application from the root `README.md`, which is the product and technical build contract. Read that file completely before changing code. Preserve it as `docs/build-spec.md` before updating the root README into accurate running-project documentation. Keep this prompt available in the repository.

The owner wants implementation, not another architecture proposal. Inspect the repository and applicable AGENTS.md instructions, reuse useful existing code, make a concise implementation checklist, and start building. If this is an empty folder, scaffold the project. If code exists, preserve unrelated owner work and build incrementally. Do not delete or reset the repository to start over.

## Product outcome

A small electronics retailer describes restocking needs. A buyer agent reads authoritative inventory/sales data, obtains quotes from three simulated supplier services, negotiates within supplier rules, evaluates compatible surplus alternatives, and proposes one full-basket purchase. The owner approves an exact version of the cart. The browser obtains separate PayPal buyer approval; the backend captures and reconciles the Sandbox payment. The application remains correct under changed quotes, repeated clicks, duplicate events, timeouts, and restarts.

Build for one retailer per isolated demo session, 20 seeded products, synthetic 28-day sales, three simulated suppliers, USD only, and one chosen supplier per purchase. Supplier simulations feed one demo storefront; one configured Sandbox business account receives payment from a separate Sandbox personal buyer. Do not imply multi-merchant settlement or real suppliers.

The narrow scope is deliberate. Complete this loop before adding anything else.

## Fixed implementation direction

- React + strict TypeScript + Vite; AG Grid Community for inventory and offer comparison; plain CSS with clear design tokens.
- Python + FastAPI + Pydantic + SQLAlchemy/Alembic + HTTPX, PostgreSQL, and the official Anthropic SDK.
- One bounded buyer tool loop. Supplier pricing/discounts/stock and all business/payment checks are deterministic backend rules. No agent per screen.
- One deployable application service: build React assets in a Docker builder stage and serve them from FastAPI. PostgreSQL is the durable store.
- A small persisted jobs table and in-process poller resume procurement/reconciliation after restart. One app instance/worker is the documented MVP ceiling.
- Docker Compose for local setup; `render.yaml` for Render; a versioned Postman collection plus blank-secret example environment.
- Locked dependencies, meaningful Pytest business/payment tests, Playwright critical paths, and build/type checks.

Use the simplest complete implementation. Do not add LangGraph, Redis, Celery, vector databases, Kubernetes, generic plugin frameworks, a paid-agent marketplace, real-money payments, voice, or seller dispute automation. Do not provision cloud resources or publish anything without existing authorization. Deliver ready-to-deploy configuration even if no hosting account is available.

## Sponsor integration requirements

Implement these three tools meaningfully; do not integrate every sponsor:

1. **AG Grid Community:** actual inventory and supplier-comparison data, sorting/filtering, row selection, clear status renderers, updates from the API, safe CSV export, and accessible detail/summary panels. Verify installed-version module and theme APIs. No `ag-grid-enterprise`, trial license dependency, Enterprise grouping/pivot/Excel export, or decorative grids.
2. **Render:** valid Docker web-service + PostgreSQL blueprint with secret placeholders, database connection wiring, health checks, migrations, and binding to `$PORT`. Data survives container replacement. Verify current plan/config syntax; do not promise free resources or credits. If not deployed, say “deployment configuration provided,” not “hosted on Render.”
3. **Postman:** importable request collection with chained workflow IDs, assertions, useful negative cases, safe variables, and verified local runner instructions. Actual PayPal buyer approval stays interactive. No fake approval request or credential-bearing environment file. The normal test suite must work without a Postman account.

PayPal and AI remain the central functionality. Sponsor tools support the working product. Do not claim prize eligibility merely because a tool is present.

## Non-negotiable correctness rules

Read the full README details; these reminders do not replace it:

- Store USD money as integer cents. Validate all arithmetic, currency, quantities, SKU/spec compatibility, delivery terms, and delivered total server-side. Explicitly label synthetic tax assumptions.
- Require confirmation of binding structured purchase requirements before supplier actions. Ambiguous natural-language text must not silently authorize spending or set a deadline.
- The LLM can propose and call allowed procurement tools. It cannot approve, create/capture payments, choose arbitrary URLs, execute code, or mutate authoritative supplier rules.
- Tool scope comes from the authenticated demo session. Treat user text, CSV cells, and supplier descriptions as untrusted data. Validate tool arguments and enforce call/token/time/negotiation limits.
- Owner approval binds to an immutable exact-cart snapshot/version/digest. Any material change requires a fresh proposal and approval. Revalidate at PayPal order creation and capture; stale browser callbacks must fail safely.
- Reserve stock atomically before checkout. Freeze supplier terms for the explicit reservation window. Do not hold DB locks over HTTP requests.
- Persist create/capture operation IDs and identical request payloads before provider calls. Use a distinct stable PayPal request ID per logical operation; retries reuse it. Check endpoint-specific retention rather than assuming permanent idempotency.
- Application approval and PayPal buyer approval are separate. Call real Sandbox create/capture/retrieve from the server; the browser only uses official checkout controls and public configuration.
- Validate trusted provider status, local mapping, amount, currency, and recipient. No browser callback, query parameter, model message, or unsigned webhook can mark a purchase paid.
- One shared idempotent finalizer handles capture responses, verified webhooks, and reconciliation. Unique DB constraints prevent duplicate ledger/stock effects. Paid purchases add incoming stock, not physically received on-hand stock.
- A timeout is an unknown outcome, not an automatic failure. Reconcile first; never retry under a new purchase/key. Preserve capture-in-progress reservations until the provider outcome is resolved. Resume recovery after a crash.
- Verify actual Sandbox webhooks, store verified events durably before acknowledging, deduplicate, and prevent state regression. Simulator payload limitations never justify skipping signature verification in the real path.
- Cookie session ownership, CSRF/origin protection, bounded public usage, and cross-session isolation are required for the public demo. No global reset endpoint or hard-coded admin password.
- Log factual tool/event summaries and redacted IDs, not private chain-of-thought, secrets, or full payer information.

## Runtime modes and external access

Implement explicit `AI_MODE=fixture|anthropic` and `PAYMENT_MODE=fixture|paypal_sandbox`. Fixture mode must run the actual UI, API, persistence, and business logic with deterministic external adapters and visible simulation badges. It is useful for development and tests, not evidence of actual integration.

Actual AI mode needs `ANTHROPIC_API_KEY` and a verified, account-supported `ANTHROPIC_MODEL`. Actual payments need the Sandbox client ID/secret, expected recipient merchant ID, and the same app's webhook ID. Missing credentials must produce clear setup guidance; never silently fall back to fixture mode. Do not invent a key/model ID or assume Claude Code subscription access supplies API credits.

If real credentials or a public webhook URL are unavailable, continue all independent implementation and offline tests. Supply exact setup instructions and clearly list the external checks still pending. Do not stop coding merely to ask for credentials. Do not claim actual PayPal/AI tests passed when only fixtures ran.

## Build sequence

Work in these vertical slices; each should leave the repository runnable:

1. Inspect the repo. Preserve the build contract. Scaffold/configure frontend, backend, PostgreSQL, migrations, fixtures, scoped sessions, Compose, and health checks. Prove a clean offline startup and persisted restart.
2. Implement restock math, requirements confirmation, independent supplier rules, quote revisions/expiry, compatibility, deterministic comparison, immutable approvals, and reservations. Add domain checks before LLM/payment integration.
3. Build the core inventory, procurement, offer-comparison, approval, and purchase-history screens against real API/storage. Complete a visibly labeled offline path with fixture adapters.
4. Add real Anthropic client-side tool use with bounded loops, schema validation, grounded summaries, usage reporting, and errors. Add English/Hindi handling without changing authoritative amounts/specs.
5. Implement PayPal Sandbox order creation, official browser approval, capture, verified webhook inbox, durable operation IDs, shared finalization, and persisted reconciliation jobs. Exercise duplicate/race/timeout/restart cases with controlled adapters; run real Sandbox checks if configured.
6. Finish Render config, Postman collection, CI, setup docs, accessible UI states, actual screenshots if capture is available, demo script, and submission draft. Re-run checks changed by these steps and report the real result.

Use the main demo fixture from the README: budget $500; Supplier A changes $470 to $525; Supplier B improves a comparable $510 basket to $488 through permitted negotiation/surplus; Supplier C fails a specific constraint. Build fixture line items so the totals genuinely add up. Scenario controls may change supplier data, not force AI answers or payment success. Show the actual model path, including failures, rather than faking a trace.

The interface should feel like a polished operations product: readable data grids, restrained glass styling, visible constraints, clear review actions, meaningful progress, and useful error recovery. Include reduced motion, keyboard access, focus states, semantic status text, and a compact mobile/assistive offer summary. Do not spend the implementation budget on a marketing landing page.

## Required validation

Run meaningful checks, not tests that merely repeat implementation details:

- Fresh Compose boot, migrations/seeding, persisted app restart, configuration validation.
- Deterministic restock/total calculations and supplier rule enforcement, full-basket feasibility, incompatible surplus rejection, invalid imports.
- Stale approval, forged totals, wrong provider amount/currency/recipient, cross-session IDs, and invalid signatures.
- Concurrent stock reservations, duplicate approval/order/capture clicks, duplicate and out-of-order events.
- Capture committed remotely followed by lost response; crash before local finalization; pinned unknown reservation; one ledger posting and one incoming-stock update.
- AI malformed tools, injection attempts, run limits, provider failures, and grounded output.
- Typecheck, frontend production build, a critical Playwright offline flow, and keyboard/mobile behavior.
- Collection import/schema validation and runnable Postman examples; Render config validation without provisioning resources.
- A real AI + PayPal Sandbox transaction if credentials are present; otherwise explicitly pending.

Use an isolated PostgreSQL test database; never wipe normal app data to run tests. Keep fixture fault-injection behind nonproduction/test boundaries. Default CI uses no paid-provider credentials. Do not write “all tests pass” until the commands finish successfully. If tools/network prevent a check, identify that limitation precisely and continue other work.

## Completion and handoff

Deliver all source/assets, dependency locks, migrations, fixture data, `.env.example`, Docker/Compose, Render blueprint, Postman collection, CI, root MIT license, and documentation listed in the README. Do not leave core buttons unimplemented, return placeholder success from integrations, or replace the app with a static prototype.

Rewrite the root README into accurate setup and usage instructions after implementation, while retaining the original contract in `docs/build-spec.md`. Document exact tested commands and environment variables, Windows/PowerShell notes, real-vs-simulated boundaries, one-merchant payment routing, one-worker limits, and unverified external steps. Include `docs/verification.md`, `docs/demo-script.md`, and `docs/submission.md`. Keep absent repo/demo/video URLs visibly owner-supplied; never invent them.

Preserve progress in a concise implementation checklist or handoff note so work survives context compaction. Continue to the next authorized slice without repeatedly asking whether to proceed. Ask only when a decision materially blocks correct implementation and has no reasonable safe default.

When finished, report: what works end-to-end, commands actually run and results, actual external integration evidence, and concrete remaining setup/limitations. Explain key architectural choices briefly so the owner can understand and defend the project in FDE interviews. Do not stop at planning, scaffolding, or a frontend demo.

Start by inspecting the repository and reading the full README. Then implement.

---

## Short continuation prompt

Use this if Claude's session ends before the build is complete:

> Continue TradePilot AI from the current repository. Read AGENTS.md, README.md, docs/build-spec.md, CLAUDE_BUILD_PROMPT.md, and the latest implementation/verification notes. Inspect the actual code and git diff; preserve existing work. Identify the next incomplete acceptance gate, implement it, run the relevant checks, and continue through the remaining build. Keep fixture and actual provider evidence separate. Do not restart, replace working code, stop at a plan, or claim unexecuted tests passed.
