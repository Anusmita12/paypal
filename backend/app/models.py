"""Persistent records. Money is integer USD cents everywhere; JSON snapshots are immutable once written.

Allowed state transitions live next to each state set and are enforced through `transition()`.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

Json = JSON().with_variant(JSONB(), "postgresql")


def new_id() -> str:
    return uuid.uuid4().hex


class InvalidTransition(Exception):
    def __init__(self, record: str, current: str, target: str):
        super().__init__(f"{record}: {current} -> {target} not allowed")
        self.record, self.current, self.target = record, current, target


def transition(obj: Any, target: str, allowed: dict[str, set[str]], record: str) -> None:
    current = obj.status
    if current == target:
        return
    if target not in allowed.get(current, set()):
        raise InvalidTransition(record, current, target)
    obj.status = target


PROCUREMENT_TRANSITIONS = {
    "needs_input": {"draft"},
    "draft": {"planning", "needs_input"},
    "planning": {"negotiating", "awaiting_approval", "no_feasible_offer", "failed", "needs_input"},
    "negotiating": {"awaiting_approval", "no_feasible_offer", "failed", "needs_input"},
    "awaiting_approval": {"approved", "planning", "draft"},
    "no_feasible_offer": {"planning", "draft", "needs_input"},
    "failed": {"planning", "draft", "needs_input"},
    "approved": set(),
}
PROPOSAL_TRANSITIONS = {"current": {"approved", "superseded", "expired"}}
PURCHASE_TRANSITIONS = {
    "creating": {"awaiting_paypal_approval", "unknown", "cancelled", "failed", "manual_review"},
    "awaiting_paypal_approval": {"capture_pending", "cancelled", "failed", "unknown", "manual_review"},
    "capture_pending": {"paid", "unknown", "failed", "manual_review"},
    "unknown": {"awaiting_paypal_approval", "capture_pending", "paid", "failed", "cancelled", "manual_review"},
    # A confirmed capture after a local failure/cancel is never reset to unpaid; a human reviews it.
    "failed": {"manual_review"},
    "cancelled": {"manual_review"},
    "paid": {"manual_review"},
    "manual_review": set(),
}
RESERVATION_TRANSITIONS = {
    "held": {"capture_in_progress", "released", "consumed"},
    "capture_in_progress": {"consumed", "released"},
}
JOB_STATES = ("pending", "processing", "processed", "failed")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DemoSession(Base, TimestampMixin):
    __tablename__ = "demo_sessions"
    __table_args__ = (CheckConstraint("wallet_balance_cents >= 0", name="wallet_non_negative"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    ip_hash: Mapped[str] = mapped_column(String(64), index=True)
    business_date: Mapped[date] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    wallet_balance_cents: Mapped[int] = mapped_column(BigInteger, default=0)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("session_id", "sku"),
        CheckConstraint("on_hand >= 0 AND incoming >= 0 AND safety_stock >= 0", name="stock_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("demo_sessions.id", ondelete="CASCADE"), index=True)
    sku: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(Json, default=dict)
    on_hand: Mapped[int] = mapped_column(Integer)
    incoming: Mapped[int] = mapped_column(Integer, default=0)
    safety_stock: Mapped[int] = mapped_column(Integer)
    # First day sales were tracked for this SKU; shorter history triggers a visible warning.
    listed_since: Mapped[date] = mapped_column(Date)


class Sale(Base):
    __tablename__ = "sales"
    __table_args__ = (
        UniqueConstraint("session_id", "import_key"),
        CheckConstraint("units >= 0", name="units_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("demo_sessions.id", ondelete="CASCADE"), index=True)
    sku: Mapped[str] = mapped_column(String(64))
    sale_date: Mapped[date] = mapped_column(Date)
    units: Mapped[int] = mapped_column(Integer)
    import_key: Mapped[str] = mapped_column(String(128))


class ImportBatch(Base, TimestampMixin):
    __tablename__ = "import_batches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("demo_sessions.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # inventory | sales
    status: Mapped[str] = mapped_column(String(16), default="previewed")  # previewed | applied
    rows: Mapped[list] = mapped_column(Json, default=list)
    errors: Mapped[list] = mapped_column(Json, default=list)


class Supplier(Base):
    """Session-scoped copy of a simulated supplier's authoritative commercial rules."""

    __tablename__ = "suppliers"
    __table_args__ = (
        UniqueConstraint("session_id", "code"),
        CheckConstraint("shipping_cents >= 0 AND max_discount_bps BETWEEN 0 AND 5000", name="supplier_terms_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("demo_sessions.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")  # untrusted text, never instructions
    categories: Mapped[list] = mapped_column(Json, default=list)
    lead_days: Mapped[int] = mapped_column(Integer)
    shipping_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    max_discount_bps: Mapped[int] = mapped_column(Integer, default=0)
    discount_ladder_bps: Mapped[list] = mapped_column(Json, default=list)
    min_subtotal_for_discount_cents: Mapped[int] = mapped_column(Integer, default=0)
    quote_ttl_minutes: Mapped[int] = mapped_column(Integer, default=30)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class SupplierItem(Base):
    __tablename__ = "supplier_items"
    __table_args__ = (
        UniqueConstraint("supplier_id", "offered_sku"),
        CheckConstraint("stock >= 0 AND reserved >= 0 AND reserved <= stock", name="stock_reserved_valid"),
        CheckConstraint("unit_cents > 0 AND moq >= 1", name="price_moq_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), index=True)
    sku: Mapped[str] = mapped_column(String(64))  # the retailer SKU this item fulfils
    offered_sku: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(Json, default=dict)
    unit_cents: Mapped[int] = mapped_column(Integer)
    stock: Mapped[int] = mapped_column(Integer)
    reserved: Mapped[int] = mapped_column(Integer, default=0)
    moq: Mapped[int] = mapped_column(Integer, default=1)
    is_surplus: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")


class Procurement(Base, TimestampMixin):
    __tablename__ = "procurements"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("demo_sessions.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="needs_input")
    status_reason: Mapped[str | None] = mapped_column(String(120))
    request_text: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(8), default="en")
    # Proposed (unconfirmed) and confirmed requirements: {lines:[{sku,qty}], budget_cents, deadline, coverage_days}
    proposed: Mapped[dict] = mapped_column(Json, default=dict)
    requirements: Mapped[dict | None] = mapped_column(Json)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, default=1)

    __mapper_args__ = {"version_id_col": row_version}  # noqa: RUF012 - SQLAlchemy declarative idiom


class AgentRun(Base, TimestampMixin):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    procurement_id: Mapped[str] = mapped_column(ForeignKey("procurements.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("demo_sessions.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|succeeded|failed
    mode: Mapped[str] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(80))
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    tool_spend_cents: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentEvent(Base):
    __tablename__ = "agent_events"
    __table_args__ = (UniqueConstraint("run_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(24))  # tool_call | message | error | status
    tool_name: Mapped[str | None] = mapped_column(String(64))
    arguments: Mapped[dict | None] = mapped_column(Json)
    result: Mapped[dict | None] = mapped_column(Json)
    summary: Mapped[str] = mapped_column(Text, default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Quote(Base):
    """Immutable supplier quote for a full confirmed basket. Negotiation creates a new child quote."""

    __tablename__ = "quotes"
    __table_args__ = (CheckConstraint("total_cents >= 0 AND discount_cents >= 0", name="quote_money_valid"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    procurement_id: Mapped[str] = mapped_column(ForeignKey("procurements.id", ondelete="CASCADE"), index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), index=True)
    supplier_revision: Mapped[int] = mapped_column(Integer)
    parent_quote_id: Mapped[str | None] = mapped_column(ForeignKey("quotes.id"))
    negotiated: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | superseded
    lines: Mapped[list] = mapped_column(Json)
    issues: Mapped[list] = mapped_column(Json, default=list)  # line-level problems: stock, moq
    subtotal_cents: Mapped[int] = mapped_column(Integer)
    discount_bps: Mapped[int] = mapped_column(Integer, default=0)
    discount_cents: Mapped[int] = mapped_column(Integer, default=0)
    shipping_cents: Mapped[int] = mapped_column(Integer)
    tax_cents: Mapped[int] = mapped_column(Integer, default=0)
    total_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    delivery_date: Mapped[date] = mapped_column(Date)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rationale_code: Mapped[str] = mapped_column(String(48), default="list_price")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NegotiationRound(Base):
    __tablename__ = "negotiation_rounds"
    # Round limits apply per supplier within one buyer run; a re-run after changed terms may negotiate again.
    __table_args__ = (UniqueConstraint("procurement_id", "run_id", "supplier_id", "round_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    procurement_id: Mapped[str] = mapped_column(ForeignKey("procurements.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"))
    round_no: Mapped[int] = mapped_column(Integer)
    base_quote_id: Mapped[str] = mapped_column(ForeignKey("quotes.id"))
    requested_discount_bps: Mapped[int] = mapped_column(Integer)
    requested_surplus: Mapped[list] = mapped_column(Json, default=list)
    outcome: Mapped[str] = mapped_column(String(16))  # accepted | countered | rejected
    granted_discount_bps: Mapped[int] = mapped_column(Integer, default=0)
    result_quote_id: Mapped[str | None] = mapped_column(ForeignKey("quotes.id"))
    rationale_code: Mapped[str] = mapped_column(String(48))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Proposal(Base):
    __tablename__ = "proposals"
    __table_args__ = (UniqueConstraint("procurement_id", "version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    procurement_id: Mapped[str] = mapped_column(ForeignKey("procurements.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("demo_sessions.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"))
    version: Mapped[int] = mapped_column(Integer)
    quote_id: Mapped[str] = mapped_column(ForeignKey("quotes.id"))
    status: Mapped[str] = mapped_column(String(16), default="current")
    status_reason: Mapped[str | None] = mapped_column(String(120))
    snapshot: Mapped[dict] = mapped_column(Json)
    digest: Mapped[str] = mapped_column(String(64))
    comparison: Mapped[dict] = mapped_column(Json, default=dict)
    explanation: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), unique=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("demo_sessions.id", ondelete="CASCADE"), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Purchase(Base, TimestampMixin):
    __tablename__ = "purchases"
    __table_args__ = (CheckConstraint("amount_cents > 0", name="purchase_amount_positive"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("demo_sessions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # restock | wallet_topup
    proposal_id: Mapped[str | None] = mapped_column(ForeignKey("proposals.id"), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="creating")
    status_reason: Mapped[str | None] = mapped_column(String(160))
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    snapshot: Mapped[dict] = mapped_column(Json)
    digest: Mapped[str] = mapped_column(String(64))
    paypal_order_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    payment_mode: Mapped[str] = mapped_column(String(16))
    row_version: Mapped[int] = mapped_column(Integer, default=1)

    __mapper_args__ = {"version_id_col": row_version}  # noqa: RUF012 - SQLAlchemy declarative idiom


class Reservation(Base, TimestampMixin):
    __tablename__ = "reservations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    purchase_id: Mapped[str] = mapped_column(ForeignKey("purchases.id"), unique=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("demo_sessions.id"), index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    status: Mapped[str] = mapped_column(String(24), default="held")
    lines: Mapped[list] = mapped_column(Json)  # [{supplier_item_id, sku, qty}]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProviderOperation(Base, TimestampMixin):
    """Intent persisted before every provider call; retries reuse request_id and the identical payload."""

    __tablename__ = "provider_operations"
    __table_args__ = (UniqueConstraint("purchase_id", "kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purchase_id: Mapped[str] = mapped_column(ForeignKey("purchases.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # create_order | capture_order
    request_id: Mapped[str] = mapped_column(String(64), unique=True)
    payload: Mapped[dict] = mapped_column(Json)
    payload_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="pending")  # pending|succeeded|unknown|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    response: Mapped[dict | None] = mapped_column(Json)
    last_error: Mapped[str | None] = mapped_column(Text)


class PurchaseEvent(Base):
    """Append-only payment timeline: order creation, buyer approval, capture, webhooks, reconciliation."""

    __tablename__ = "purchase_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purchase_id: Mapped[str] = mapped_column(ForeignKey("purchases.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(24))  # owner | capture_response | webhook | reconciliation | system
    summary: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(Json, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Capture(Base):
    __tablename__ = "captures"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # PayPal capture ID
    purchase_id: Mapped[str] = mapped_column(ForeignKey("purchases.id"), unique=True)
    order_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24))
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    payee_merchant_id: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(24))  # capture_response | webhook | reconciliation
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LedgerEntry(Base):
    """One row per business effect. `business_key` uniqueness is what prevents double postings."""

    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("demo_sessions.id"), index=True)
    business_key: Mapped[str] = mapped_column(String(160), unique=True)
    kind: Mapped[str] = mapped_column(String(32))  # purchase_paid | wallet_topup | wallet_grant | tool_usage
    amount_cents: Mapped[int] = mapped_column(Integer)
    purchase_id: Mapped[str | None] = mapped_column(ForeignKey("purchases.id"))
    details: Mapped[dict] = mapped_column(Json, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_event_id: Mapped[str] = mapped_column(String(80), unique=True)
    event_type: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    payload: Mapped[dict] = mapped_column(Json)
    delivery_count: Mapped[int] = mapped_column(Integer, default=1)
    last_error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict] = mapped_column(Json, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=8)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class PaidTool(Base):
    """Registry of pay-per-call tools. Prices are authoritative; the model cannot change them."""

    __tablename__ = "paid_tools"
    __table_args__ = (CheckConstraint("price_cents > 0 AND price_cents <= 100", name="tool_price_bounded"),)

    code: Mapped[str] = mapped_column(String(48), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    provider_id: Mapped[str] = mapped_column(String(48))
    provider_name: Mapped[str] = mapped_column(String(120))
    price_cents: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text, default="")


class ToolUsage(Base):
    __tablename__ = "tool_usage"
    __table_args__ = (CheckConstraint("price_cents > 0", name="usage_price_positive"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("demo_sessions.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    call_key: Mapped[str] = mapped_column(String(120), unique=True)  # idempotency for one logical tool call
    tool_code: Mapped[str] = mapped_column(ForeignKey("paid_tools.code"))
    provider_id: Mapped[str] = mapped_column(String(48), index=True)
    price_cents: Mapped[int] = mapped_column(Integer)
    payout_batch_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PayoutBatch(Base, TimestampMixin):
    """One PayPal Payouts batch settling a provider's accrued tool earnings. Usage rows are claimed (payout_batch_id)
    in the same transaction that creates the batch, so no usage can be paid twice."""

    __tablename__ = "payout_batches"
    __table_args__ = (CheckConstraint("amount_cents > 0", name="payout_amount_positive"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("demo_sessions.id"), index=True)
    provider_id: Mapped[str] = mapped_column(String(48))
    amount_cents: Mapped[int] = mapped_column(Integer)
    item_count: Mapped[int] = mapped_column(Integer)
    receiver: Mapped[str] = mapped_column(String(254))
    sender_batch_id: Mapped[str] = mapped_column(String(64), unique=True)  # stable across retries
    status: Mapped[str] = mapped_column(String(24), default="pending")  # pending|submitted|unknown|success|failed|manual_review
    paypal_batch_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    paypal_status: Mapped[str | None] = mapped_column(String(32))
    payment_mode: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class FixtureProviderRecord(Base):
    """Durable state for the offline PayPal fixture adapter (fixture mode and tests only)."""

    __tablename__ = "fixture_provider_records"
    __table_args__ = (UniqueConstraint("kind", "key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(80))
    data: Mapped[dict] = mapped_column(Json)
