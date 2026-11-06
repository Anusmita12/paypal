# Architecture

## Components
- **FastAPI app** (`backend/app`), one process and one worker:
  - The REST API under `/api`.
  - The built SPA under `/`; the fallback never captures `/api` paths.
  - An in-process job poller and a maintenance loop that expires reservations.
- **PostgreSQL** holds all state. The container filesystem holds none.
- **React SPA** (`frontend/src`):
  - AG Grid Community for the operational grids.
  - AG Studio for the Insights dashboard, lazy-loaded.

## Request path and trust boundaries
| Boundary | Rule |
|---|---|
| Browser → API | Session cookie (HttpOnly, SameSite), CSRF header on mutations, origin check, per-session ownership on every record (another session gets `404`) |
| Model → tools | Strict Pydantic arguments, server-scoped context, call budget and deadline, paid calls charged once per `call_key`, no approve/pay/capture tool |
| Supplier / buyer text | Treated as data; never instructions |
| PayPal → webhook | Signature verified by PayPal's verification endpoint (`/v1/notifications/verify-webhook-signature`), stored before acknowledgement, deduplicated by event ID, then the order or dispute is **re-read from PayPal** |

## Money flows
1. **Restock:**
   - Proposal snapshot (digest) → owner approval → atomic stock reservation → `POST /v2/checkout/orders` (intent persisted, `PayPal-Request-Id`).
   - Buyer approves in PayPal's window → `POST …/capture` (separate request ID) → `finalize_from_order`.
   - Finalizing checks the amount, currency, payee and correlation, then writes the capture row, one ledger entry, the consumed reservation and incoming stock, all once.
2. **Tool wallet:** a `wallet_topup` purchase uses the same create/capture/finalize path. Its finalizer credits the balance once. Each paid tool call is an atomic conditional debit keyed by `call_key`, and calls are capped per run and per day.
3. **Provider settlement:**
   - Unsettled usage rows are claimed per provider in the same transaction that creates a `payout_batches` row.
   - Then `POST /v1/payments/payouts` is called with a stable `sender_batch_id` and request ID.
   - A lost response stays `unknown` until a refresh replays the same IDs.
4. **Disputes:**
   - A webhook triggers `GET /v1/customer/disputes/{id}`, and the dispute is matched to a purchase by `seller_transaction_id` (the capture ID).
   - The server gathers the records, the model drafts claims citing them, and the server validates the citations and amounts.
   - The owner approves the digest of the exact payload, which is then sent as multipart `provide-evidence` (`input` JSON).

## State machines
These are enforced by `transition()` in `models.py`.
- **Procurement:** `needs_input → draft → planning → negotiating → awaiting_approval → approved`. It can also end as `no_feasible_offer` or `failed`.
- **Purchase:** `creating → awaiting_paypal_approval → capture_pending → paid`. It can also be `unknown`, `cancelled`, `failed` or `manual_review`.
- **Reservation:** `held → capture_in_progress → consumed`, or `released`. TTL cleanup never releases `capture_in_progress`.

## Jobs
`jobs` table with `FOR UPDATE SKIP LOCKED` claims and lease expiry:
- **Retries:** backoff with jitter for retryable errors.
- **Deduplication:** `dedupe_key` prevents duplicate open jobs.
- **Kinds:** `agent_run`, `process_webhook`, `reconcile_purchase`, `ingest_dispute`, `draft_evidence`.

## AI drivers
| Mode | Loop | Notes |
|---|---|---|
| `fixture` | deterministic script over the real tools | no model call; explanations come from a template |
| `gemini` | `agent_gemini.run_gemini`, manual function-calling loop | automatic function calling disabled; tokens recorded |
| `anthropic` | `agent_llm.run_claude`, manual tool-use loop | strict tool schemas, server-side refusal fallback on supported models |

A crash or an unavailable model fails the run visibly, and it never falls back to another driver. Dispute drafts use
`llm.generate_json` for structured output, with one corrective retry when validation fails.
