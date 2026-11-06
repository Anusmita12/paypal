# Demo script (under 3 minutes)

Record with `AI_MODE=gemini` and `PAYMENT_MODE=paypal_sandbox`. Keep the header mode badges visible.
If a step falls back to fixture mode for any reason, say so on screen.

| Time | Show | Say |
|---|---|---|
| 0:00–0:15 | Inventory grid with 8 highlighted shortages | "A small electronics shop needs a weekend restock. TradePilot buys it, and PayPal is how the money moves at every step." |
| 0:15–0:35 | Type the request → requirement form shows the $500 budget and a 3-day deadline → Confirm | "The AI suggests the cart, but nothing reaches suppliers until the owner confirms exact requirements." |
| 0:35–1:05 | Run the buyer: timeline, quotes, negotiation rounds, micro-payment ticker | "The agent quotes three suppliers and negotiates within each one's policy. Every quote and round costs a cent from a prepaid wallet." |
| 1:05–1:20 | Offer grid: Cobalt rejected for missing certification | "Ranking and feasibility are server rules. The model explains them; it can't change them." |
| 1:20–1:35 | Trigger the Supplier A price rise, click the old Approve → refused; Run again → Brightline at $488 | "A stale approval is refused by the server. The re-run finds a negotiated $488 offer." |
| 1:35–2:05 | Approve exact cart → PayPal button → Sandbox buyer approves in PayPal's window → paid, capture ID, incoming stock | "One approval and one capture. Webhooks and reconciliation share one finalizer, so the payment is recorded exactly once." |
| 2:05–2:20 | Tool wallet: top-up, then Settle → Payouts batch | "Tool providers get paid with PayPal Payouts. The batch ID is stable, so a retry can't pay anyone twice." |
| 2:20–2:45 | Dispute desk: open a Sandbox dispute → Draft with AI → citations → Approve and send | "When a buyer disputes, the agent drafts evidence that cites real records. Nothing goes to PayPal until the owner approves." |
| 2:45–2:55 | Insights (AG Studio) | "AG Studio dashboards over live data: savings, PayPal volume, pay-per-call spend." |

Recovery replay (optional, clearly labeled): show an "outcome unknown" purchase becoming paid after reconciliation, with
one ledger entry. If it comes from the offline fault injection, call it a simulation.
