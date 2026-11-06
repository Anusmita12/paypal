"""The Claude tool loop, driven by a scripted fake client (offline). Real-model evidence is recorded separately."""

import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app import agent_llm
from app.agent_tools import ungrounded_amounts
from app.config import get_settings
from app.jobs import drain
from tests.helpers import confirmed_procurement


def tool_use(name, args, i=[0]):  # noqa: B006 - deliberate counter for unique ids
    i[0] += 1
    return SimpleNamespace(type="tool_use", id=f"toolu_{i[0]}", name=name, input=args)


def text(t):
    return SimpleNamespace(type="text", text=t)


def last_results(messages) -> list[dict]:
    return [json.loads(r["content"]) for r in messages[-1]["content"]]


class FakeClaude:
    """Plays a scripted conversation; each step sees the messages so far and returns content blocks."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls: list[dict] = []
        self.messages = self
        self.beta = SimpleNamespace(messages=self)

    def with_options(self, **_):
        return self

    def create(self, **params):
        self.calls.append(params)
        blocks = self.steps.pop(0)(params["messages"])
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
        usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        return SimpleNamespace(content=blocks, stop_reason=stop, usage=usage)


@pytest.fixture
def claude_mode(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "ai_mode", "anthropic")
    monkeypatch.setattr(s, "anthropic_model", "claude-opus-5-5")
    monkeypatch.setattr(s, "anthropic_api_key", "test-key-not-real")

    def install(fake):
        monkeypatch.setattr(agent_llm.anthropic, "Anthropic", lambda **_: fake)
        return fake

    return install


def run(api):
    p = confirmed_procurement(api)
    assert api.post(f"/api/procurements/{p['procurement_id']}/run").status_code == 202
    drain()
    return api.get(f"/api/procurements/{p['procurement_id']}").json()


def negotiating_script(explanations):
    state = {}

    def propose(messages):
        best = state["best"]
        return [tool_use("propose_purchase", {"quote_id": best, "explanation": explanations.pop(0)})]

    def compare(messages):
        return [tool_use("compare_offers", {})]

    def after_compare(messages):
        state["best"] = last_results(messages)[0]["data"]["best_quote_id"]
        return propose(messages)

    return [
        lambda m: [tool_use("calculate_restock_needs", {}), tool_use("list_suppliers", {})],
        lambda m: [tool_use("request_quote", {"supplier_id": s}) for s in ("supplier-a", "supplier-b", "supplier-c")],
        lambda m: [tool_use("get_compatible_surplus_offers", {"supplier_id": "supplier-b"})],
        lambda m: [tool_use("counteroffer", {"supplier_id": "supplier-b", "requested_discount_bps": 300,
                                             "surplus_skus": ["SUR-CHG-65W-GAN"]})],
        compare,
        after_compare,
        propose,  # retried after the server rejected an ungrounded amount
        lambda m: [text("Proposal saved for the owner.")],
    ]


def test_claude_drives_the_same_tools_and_cannot_invent_amounts(api, claude_mode):
    fake = claude_mode(FakeClaude(negotiating_script([
        "Apex is best at $470.00 and saves $99.99.",  # $99.99 appears in no tool result
        ("Apex Wholesale delivers the full basket for $470.00 by 2026-11-06, $30.00 under the $500.00 budget. "
        "Cobalt was rejected: its Lightning cable lacks mfi certification."),
    ])))
    d = run(api)
    assert d["run"]["status"] == "succeeded" and d["run"]["mode"] == "anthropic"
    assert d["proposal"]["snapshot"]["total_cents"] == 47_000
    assert d["proposal"]["explanation"].startswith("Apex Wholesale delivers")
    rejected = [e for e in d["events"] if e["summary"] == "propose_purchase: ungrounded_explanation"]
    assert len(rejected) == 1
    assert d["run"]["input_tokens"] == 100 * 8 and d["run"]["output_tokens"] == 20 * 8
    assert any(e["kind"] == "message" for e in d["events"])

    first = fake.calls[0]
    assert first["model"] == "claude-opus-5-5" and first["fallbacks"] == "default"
    assert first["betas"] == ["server-side-fallback-2026-07-01"]
    names = {t["name"] for t in first["tools"]}
    assert "capture_payment" not in names and "approve_purchase" not in names
    for t in first["tools"]:
        assert t["strict"] is True and t["input_schema"]["additionalProperties"] is False


def test_model_cannot_propose_a_worse_offer_or_call_payment_tools(api, claude_mode):
    state = {}

    def grab(m):
        quotes = [r["data"] for r in last_results(m)]
        state["c"] = next(q["quote_id"] for q in quotes if q["supplier_id"] == "supplier-b")
        return [tool_use("capture_payment", {"amount": 1}), tool_use("propose_purchase", {
            "quote_id": state["c"], "explanation": "Brightline at $510.00."})]

    claude_mode(FakeClaude([
        lambda m: [tool_use("request_quote", {"supplier_id": s}) for s in ("supplier-a", "supplier-b", "supplier-c")],
        grab,
        lambda m: [text("I could not propose that offer.")],
    ]))
    d = run(api)
    summaries = [e["summary"] for e in d["events"]]
    assert "Rejected unknown tool 'capture_payment'" in summaries
    assert "propose_purchase: not_best_feasible_offer" in summaries
    assert d["proposal"] is None and d["status"] == "no_feasible_offer"


def test_api_failure_fails_the_run_without_fixture_fallback(api, claude_mode):
    class Down(FakeClaude):
        def create(self, **params):
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))

    claude_mode(Down([]))
    d = run(api)
    assert d["run"]["status"] == "failed" and d["run"]["error_code"] == "model_unavailable"
    assert d["run"]["tool_calls"] == 0 and d["proposal"] is None


def test_grounding_allows_server_figures_only():
    comparison = {"budget_cents": 50_000, "offers": [{"total_cents": 47_000, "reasons": []}],
                  "savings_cents": 2_200, "note": "exceeds budget by $25.00"}
    assert ungrounded_amounts("$470.00, $30 under $500, saves $22.00, over by $25.00", comparison) == []
    assert ungrounded_amounts("only $1,234.56", comparison) == ["$1,234.56"]
