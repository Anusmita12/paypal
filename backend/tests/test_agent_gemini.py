"""The Gemini tool loop, driven by a scripted fake client (offline). Real-model evidence is recorded separately."""

from types import SimpleNamespace

import pytest
from google.genai import errors, types

from app import agent_gemini
from app.config import get_settings
from app.jobs import drain
from tests.helpers import confirmed_procurement


def call(name, args):
    return types.Part(function_call=types.FunctionCall(id=f"c-{name}", name=name, args=args))


class FakeGemini:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []
        self.models = self

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": list(contents), "config": config})
        parts = self.steps.pop(0)(contents)
        usage = SimpleNamespace(prompt_token_count=50, tool_use_prompt_token_count=0, candidates_token_count=10,
                                thoughts_token_count=5)
        return SimpleNamespace(candidates=[SimpleNamespace(content=types.Content(role="model", parts=parts))],
                               usage_metadata=usage, prompt_feedback=None)


def results(contents):
    return [p.function_response.response for p in contents[-1].parts]


@pytest.fixture
def gemini_mode(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "ai_mode", "gemini")
    monkeypatch.setattr(s, "gemini_model", "gemini-test-model")
    monkeypatch.setattr(s, "gemini_api_key", "test-key-not-real")

    def install(fake):
        monkeypatch.setattr(agent_gemini.genai, "Client", lambda **_: fake)
        return fake

    return install


def run(api):
    p = confirmed_procurement(api)
    assert api.post(f"/api/procurements/{p['procurement_id']}/run").status_code == 202
    drain()
    return api.get(f"/api/procurements/{p['procurement_id']}").json()


def test_gemini_drives_tools_to_a_grounded_proposal(api, gemini_mode):
    state = {}

    def after_compare(contents):
        state["best"] = results(contents)[0]["data"]["best_quote_id"]
        return [call("propose_purchase", {"quote_id": state["best"],
                                          "explanation": "Apex delivers for $470.00, $30.00 under the $500.00 budget."})]

    fake = gemini_mode(FakeGemini([
        lambda c: [call("calculate_restock_needs", {}), call("list_suppliers", {})],
        lambda c: [call("request_quote", {"supplier_id": s}) for s in ("supplier-a", "supplier-b", "supplier-c")],
        lambda c: [call("counteroffer", {"supplier_id": "supplier-b", "requested_discount_bps": 300, "surplus_skus": []})],
        lambda c: [call("compare_offers", {})],
        after_compare,
        lambda c: [types.Part(text="Proposal saved for the owner.")],
    ]))
    d = run(api)
    assert d["run"]["status"] == "succeeded" and d["run"]["mode"] == "gemini" and d["run"]["model"] == "gemini-test-model"
    assert d["proposal"]["snapshot"]["total_cents"] == 47_000
    assert d["run"]["input_tokens"] == 6 * 50 and d["run"]["output_tokens"] == 6 * 15
    cfg = fake.calls[0]["config"]
    assert cfg.automatic_function_calling.disable is True
    names = {f.name for f in cfg.tools[0].function_declarations}
    assert "capture_payment" not in names and "propose_purchase" in names
    # Function responses carry the call ids back.
    assert fake.calls[1]["contents"][-1].parts[0].function_response.id == "c-calculate_restock_needs"


def test_gemini_rate_limit_fails_the_run_visibly(api, gemini_mode):
    class Limited(FakeGemini):
        def generate_content(self, **_):
            raise errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})

    gemini_mode(Limited([]))
    d = run(api)
    assert d["run"]["status"] == "failed" and d["run"]["error_code"] == "model_rate_limited"
    assert d["proposal"] is None
