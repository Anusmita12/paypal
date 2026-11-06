# Verification

What has actually been run, how, and with what result. Anything not listed as **passed** is unverified.

Last updated: 2026-11-07 (authoring machine: macOS, Python 3.13.5, Node 24.15, PostgreSQL 17).

## Automated (offline fixture modes)
| Check | Command | Result |
|---|---|---|
| Backend lint | `cd backend && uv run ruff check .` | passed |
| Backend tests | `cd backend && uv run pytest -q` | **82 passed** |
| Frontend typecheck + production build | `cd frontend && npm run build` | passed |
| Browser end-to-end (desktop + mobile) | `cd frontend && npx playwright test` | **5 passed** |
| Fresh clone follows README (no Docker) | clone → `cp .env.example .env` → `uv sync` → `migrate` → `npm ci && npm run build` → `uvicorn` | passed: `/api/ready` at migration head, demo session created (50¢ grant), 8 shortages, SPA at `/`, unknown `/api/*` returns JSON 404 |
| Deploy YAML parses | `render.yaml`, `compose.yaml`, `.github/workflows/ci.yml` loaded with PyYAML | passed (syntax only; not executed) |

The backend tests cover:
- **Payment flow:**
  - **Main story:** the arithmetic and the stale-quote refusal.
  - **Double approval and concurrency:** double approve, concurrent captures, and concurrent stock reservation.
  - **Lost responses (fault injection):** create, capture, Payouts and dispute evidence.
  - **Webhooks:** a webhook arriving before the browser's response, duplicate webhooks, and invalid signatures.
  - **Finalizer and reservations:** duplicate finalization, amount mismatch going to manual review, and an expired reservation.
- **Isolation:** cross-session access.
- **Tool wallet:** charges once per call, spend caps, top-up credited once, and Payouts settled once per provider.
- **AI loops (Claude and Gemini, with scripted fake clients):**
  - Invented amounts are rejected.
  - A non-best offer is refused.
  - Unknown tools such as `capture_payment` are rejected.
  - Rate limits and crashes fail the run visibly.
- **Disputes:** citations are validated, a lost evidence response is reconciled, and webhook intake works.
- **Adapters (mocked HTTP):** the Sandbox adapter's error mapping, the Channel3 parser, and the CSV imports.

Fault-injection tests use the offline PayPal stand-in and are **not** evidence of PayPal's behaviour.

## External integrations — status
| Integration | Status | Evidence to add when run |
|---|---|---|
| Gemini tool loop (`AI_MODE=gemini`) | **not yet run** (no key) | model ID, run ID, token counts, screenshot of timeline |
| Claude tool loop (`AI_MODE=anthropic`) | **not run** (no paid API access) | — |
| PayPal Sandbox Orders create / buyer approval / capture | **not yet run** | order ID, capture ID, amounts (redacted) |
| PayPal webhook signature verification | **not yet run** | event IDs and `verification_status` |
| Wallet top-up via Sandbox checkout | **not yet run** | order/capture IDs |
| Payouts batch in Sandbox | **not yet run** | `payout_batch_id`, batch status |
| Order tracking push | **not yet run** | tracker response |
| Disputes: intake + provide-evidence in Sandbox | **not yet run** | dispute ID, status after submission (or documented Sandbox limitation) |
| Channel3 live search | **not yet run** | query, listing count |
| AG Studio with trial licence | **not yet run** (renders with watermark without a licence) | screenshot without watermark |
| Docker image build / `docker compose up` | **not run** (no Docker on authoring machine) | build log |
| Render deploy | **not done** (needs owner go-ahead) | hosted URL |
