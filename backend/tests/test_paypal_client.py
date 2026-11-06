"""Sandbox adapter against mocked PayPal HTTP (respx). Shapes follow Orders v2; real calls are verified separately."""

import httpx
import pytest
import respx

import app.paypal_client as pc
from app.config import get_settings
from app.payments import ProviderRejected, ProviderTimeout

BASE = "https://api-m.sandbox.paypal.com"


@pytest.fixture
def client(monkeypatch):
    s = get_settings().model_copy(update={"paypal_client_id": "cid", "paypal_client_secret": "secret",
                                          "paypal_merchant_id": "MERCHANT1", "paypal_webhook_id": "WH-1"})
    pc._token.update(value=None, expires=0.0, client_id=None)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/v1/oauth2/token").respond(200, json={"access_token": "tok-1", "expires_in": 3600})
        yield pc.SandboxPayPal(s), mock


def order(status="COMPLETED", payee=True):
    unit = {"reference_id": "p1", "custom_id": "p1", "amount": {"currency_code": "USD", "value": "488.00"},
            "payments": {"captures": [{"id": "CAP1", "status": "COMPLETED",
                                       "amount": {"currency_code": "USD", "value": "488.00"}}]}}
    if payee:
        unit["payee"] = {"merchant_id": "MERCHANT1"}
    return {"id": "ORDER1", "status": status, "purchase_units": [unit]}


def test_create_sends_request_id_and_bearer(client):
    c, mock = client
    route = mock.post("/v2/checkout/orders").respond(201, json={"id": "ORDER1", "status": "CREATED"})
    assert c.create_order("tp-create_order-p1", {"intent": "CAPTURE"})["id"] == "ORDER1"
    req = route.calls.last.request
    assert req.headers["PayPal-Request-Id"] == "tp-create_order-p1"
    assert req.headers["Authorization"] == "Bearer tok-1"
    assert req.headers["Prefer"] == "return=representation"


def test_error_mapping(client):
    c, mock = client
    mock.post("/v2/checkout/orders/ORDER1/capture").respond(
        422, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_ALREADY_CAPTURED"}]})
    with pytest.raises(ProviderRejected) as e:
        c.capture_order("ORDER1", "tp-capture_order-p1")
    assert e.value.code == "ORDER_ALREADY_CAPTURED"

    mock.get("/v2/checkout/orders/ORDER2").respond(503)
    with pytest.raises(ProviderTimeout):
        c.get_order("ORDER2")
    mock.get("/v2/checkout/orders/ORDER3").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderTimeout):
        c.get_order("ORDER3")
    with pytest.raises(ProviderRejected):
        c.get_order("../../v1/evil")


def test_expired_token_is_refreshed_once(client):
    c, mock = client
    mock.get("/v2/checkout/orders/ORDER1").mock(side_effect=[httpx.Response(401), httpx.Response(200, json=order())])
    assert c.get_order("ORDER1")["status"] == "COMPLETED"


def test_capture_without_payee_reads_the_authoritative_order(client):
    c, mock = client
    mock.post("/v2/checkout/orders/ORDER1/capture").respond(201, json=order(payee=False))
    mock.get("/v2/checkout/orders/ORDER1").respond(200, json=order())
    assert c.capture_order("ORDER1", "req")["purchase_units"][0]["payee"]["merchant_id"] == "MERCHANT1"


def test_webhook_verification(client):
    c, mock = client
    headers = {k: "x" for k in ("paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id",
                                 "paypal-transmission-sig", "paypal-transmission-time")}
    route = mock.post("/v1/notifications/verify-webhook-signature")
    route.respond(200, json={"verification_status": "SUCCESS"})
    assert c.verify_webhook(headers, {"id": "WH-EVT"}) is True
    assert route.calls.last.request.content and b'"webhook_id":"WH-1"' in route.calls.last.request.content.replace(b" ", b"")
    route.respond(200, json={"verification_status": "FAILURE"})
    assert c.verify_webhook(headers, {"id": "WH-EVT"}) is False
    assert c.verify_webhook({}, {"id": "WH-EVT"}) is False  # missing headers never reach PayPal
