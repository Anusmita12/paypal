# Devpost submission draft

> Owner-supplied items are marked **[OWNER]**. Do not publish until they are filled in and
> [verification.md](verification.md) shows the external checks as passed.

**Name:** TradePilot AI

**Tagline:** An AI purchasing agent that negotiates within your rules, pays with PayPal, meters its own tools, and defends your disputes.

## Problem
Small retailers restock by hand: they collect quotes, compare them in spreadsheets, chase discounts and pay. They also handle
chargebacks with little evidence at hand. Letting an AI loose on money is risky, because it can misread prices, follow
instructions hidden in a supplier's text, or pay twice on a retry.

## What we built
- **Procurement agent:** Gemini requests quotes, negotiates within supplier policy, checks retail prices with Channel3, and
  explains its pick. The server decides feasibility and ranking, the owner approves an exact cart, and **PayPal Checkout**
  collects the payment with a server-side capture.
- **Pay-per-call agent tools:** every quote, negotiation round and market check is a 1–2¢ debit from a prepaid wallet.
  The owner tops up the wallet through **PayPal Checkout**, and tool providers are paid with **PayPal Payouts**.
- **Dispute defense:** when a buyer files a dispute, the agent drafts evidence that cites stored records, and the server
  rejects any claim or amount it can't trace. The owner approves the exact text, and the evidence goes to PayPal's
  **Disputes API** along with the shipment tracking pushed to the order.

## How PayPal is used
- **Orders v2:** create, capture, retrieve, and tracking.
- **Webhooks:** signature verification.
- **Payouts.**
- **Disputes:** retrieve and provide-evidence.
- **Sandbox:** all of it runs in the Sandbox.
- **Idempotency:** request IDs are persisted before every call, unknown outcomes are reconciled, and one finalizer handles every payment confirmation.

## How AI is used
- **Model:** Gemini **[OWNER: model ID used in the recording]** drives a bounded tool loop, with a cap on tool calls and a run deadline.
- **Tools:** the model has no payment authority, its tool arguments are strictly validated, and supplier text is treated as untrusted.
- **Grounding:** explanations and dispute claims are checked against server records.

## Sponsor tech
- **AG Grid Community:** the operational grids.
- **AG Studio:** the Insights dashboard.
- **Channel3:** the market price tool.
- **Render:** the deployment blueprint.

## Real vs simulated
The suppliers, the retailer data, the carrier and the scenario events are simulated and labeled in the UI. PayPal, Gemini
and Channel3 are real external services in live mode. Offline fixture modes exist for tests and are labeled in the UI.

## Links
- **Repository:** [OWNER — public GitHub URL]
- **Video (under 3 minutes):** [OWNER — YouTube URL]
- **Hosted demo:** [OWNER — Render URL, if deployed]
- **License:** MIT ([OWNER — copyright holder name in LICENSE])

## Known limitations
See the README's "Limitations" section and [verification.md](verification.md).
