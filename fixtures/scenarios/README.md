# Demo scenarios

Session-scoped supplier disruptions, triggered with `POST /api/demo/scenarios/{name}` when `ENABLE_SCENARIOS=true`.
They change supplier data only. They never force an agent answer or a payment outcome.

| Name | Effect |
|---|---|
| `supplier_a_price_increase` | Apex raises three unit prices (+$55.00 on the default basket: $470.00 → $525.00) and bumps its revision, so earlier Apex quotes become stale. |
| `expire_quotes` | Marks every current quote in the session as expired, to show re-quoting. |
| `supplier_b_stock_out` | Brightline's stock of the 65W charger drops to 0, so its full basket is no longer feasible. |
