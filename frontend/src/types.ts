// Shapes returned by the FastAPI backend. Money is always integer USD cents.

export interface AppConfig {
  ai_mode: "fixture" | "anthropic" | "gemini";
  payment_mode: "fixture" | "paypal_sandbox";
  channel3_mode: "fixture" | "live";
  scenarios_enabled: boolean;
  paypal_client_id: string | null;
  ai_model: string | null;
  currency: "USD";
  limits: {
    max_tool_calls: number;
    max_run_seconds: number;
    reservation_minutes: number;
    tool_spend_cap_per_run_cents: number;
  };
}

export interface DemoSession {
  session_id: string;
  business_date: string;
  timezone: string;
  expires_at: string;
  wallet_balance_cents: number;
  csrf_token?: string;
}

export interface InventoryLine {
  sku: string;
  name: string;
  category: string;
  on_hand: number;
  incoming: number;
  safety_stock: number;
  units_28d: number;
  days_with_history: number;
  daily_sales: string;
  target_units: number;
  need: number;
  status: "shortage" | "ok";
  warning: string | null;
}

export interface Inventory {
  business_date: string;
  window: { start: string; end: string; days: number };
  coverage_days: number;
  formula: string;
  products: InventoryLine[];
  shortage_count: number;
}

export interface Line {
  sku: string;
  qty: number;
}

export interface Requirements {
  lines: (Line & { name?: string; category?: string; attributes?: Record<string, unknown> })[];
  budget_cents: number;
  deadline: string;
  coverage_days: number;
  currency: "USD";
  business_date?: string;
}

export interface Proposed {
  lines: (Line & { name?: string })[];
  coverage_days: number;
  budget_cents: number | null;
  deadline: string | null;
  currency: "USD";
  notes: string[];
}

export interface AgentEvent {
  seq: number;
  kind: "tool_call" | "message" | "error" | "status";
  tool: string | null;
  summary: string;
  ok: boolean;
  arguments: Record<string, unknown> | null;
  elapsed_ms: number;
  at: string;
}

export interface Run {
  run_id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  mode: string;
  model: string | null;
  tool_calls: number;
  input_tokens: number;
  output_tokens: number;
  tool_spend_cents: number;
  error_code: string | null;
  error_message: string | null;
  summary: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface Reason {
  code: string;
  message: string;
}

export interface Offer {
  quote_id: string;
  supplier_id: string;
  supplier_name: string;
  negotiated: boolean;
  supplier_revision: number;
  subtotal_cents: number;
  discount_cents: number;
  discount_bps: number;
  shipping_cents: number;
  tax_cents: number;
  total_cents: number;
  currency: string;
  delivery_date: string;
  expires_at: string;
  spec_compliant: boolean;
  uses_surplus: boolean;
  feasible: boolean;
  reasons: Reason[];
  rank?: number;
}

export interface Comparison {
  offers: Offer[];
  best_quote_id: string | null;
  baseline: { quote_id: string; supplier_id: string; total_cents: number } | null;
  savings_cents: number | null;
  suggestions: string[];
  budget_cents: number;
  deadline: string;
  evaluated_at: string;
}

export interface NegotiationRound {
  supplier_id: string;
  round: number;
  requested_bps: number;
  requested_surplus: string[];
  outcome: "accepted" | "countered" | "rejected";
  granted_bps: number;
  rationale_code: string;
  result_quote_id: string | null;
}

export interface SnapshotLine {
  sku: string;
  offered_sku: string;
  name: string;
  attributes: Record<string, unknown>;
  qty: number;
  unit_cents: number;
  line_cents: number;
  is_surplus: boolean;
}

export interface Snapshot {
  procurement_id: string;
  proposal_version: number;
  quote_id: string;
  supplier: { id: string; name: string; revision: number };
  lines: SnapshotLine[];
  subtotal_cents: number;
  discount_bps: number;
  discount_cents: number;
  shipping_cents: number;
  tax_cents: number;
  tax_note: string;
  total_cents: number;
  currency: string;
  delivery_date: string;
  deadline: string;
  budget_cents: number;
  quote_expires_at: string;
  payee_note: string;
}

export interface Proposal {
  proposal_id: string;
  version: number;
  status: "current" | "approved" | "superseded" | "expired";
  status_reason: string | null;
  snapshot: Snapshot;
  digest: string;
  explanation: string;
  comparison: Comparison;
}

export type ProcurementStatus =
  | "needs_input"
  | "draft"
  | "planning"
  | "negotiating"
  | "awaiting_approval"
  | "approved"
  | "no_feasible_offer"
  | "failed";

export interface Procurement {
  procurement_id: string;
  status: ProcurementStatus;
  status_reason: string | null;
  request_text: string;
  language: "en" | "hi";
  proposed: Proposed;
  requirements: Requirements | null;
  confirmed_at: string | null;
  run: Run | null;
  events: AgentEvent[];
  negotiation_rounds: NegotiationRound[];
  quotes: { quote_id: string; supplier_id: string; status: string; negotiated: boolean; total_cents: number }[];
  comparison: Comparison | null;
  proposal: Proposal | null;
  purchase_id: string | null;
}

export interface ProcurementSummary {
  procurement_id: string;
  status: ProcurementStatus;
  request_text: string;
  created_at: string;
}

export type PurchaseStatus =
  | "creating"
  | "awaiting_paypal_approval"
  | "capture_pending"
  | "paid"
  | "unknown"
  | "cancelled"
  | "failed"
  | "manual_review";

export interface Purchase {
  purchase_id: string;
  kind: "restock" | "wallet_topup";
  status: PurchaseStatus;
  status_reason: string | null;
  amount_cents: number;
  currency: string;
  snapshot: Snapshot;
  digest: string;
  paypal_order_id: string | null;
  capture_id: string | null;
  payment_mode: "fixture" | "paypal_sandbox";
  reservation: { status: string; expires_at: string; lines: { supplier_item_id: number; sku: string; qty: number }[] } | null;
  created_at: string;
  updated_at: string;
  events?: { kind: string; source: string; summary: string; at: string }[];
  operations?: { kind: string; request_id: string; state: string; attempts: number; payload_digest: string; last_error: string | null }[];
  capture?: { id: string; status: string; amount_cents: number; currency: string; source: string; at: string } | null;
  ledger?: { kind: string; business_key: string; amount_cents: number; at: string }[];
}

export interface Wallet {
  balance_cents: number;
  tools: { code: string; name: string; provider_id: string; provider_name: string; price_cents: number }[];
  usage: { id: number; run_id: string | null; tool_code: string; tool_name: string; provider_id: string; price_cents: number; settled: boolean; at: string }[];
  ledger: { kind: string; amount_cents: number; details: Record<string, unknown>; at: string }[];
}

export interface ImportPreview {
  import_id: string;
  kind: "inventory" | "sales";
  status: "previewed" | "applied";
  valid_rows: number;
  error_count: number;
  errors: { row: number; message: string }[];
  rows_preview: Record<string, unknown>[];
}
