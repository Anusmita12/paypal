import httpx
import pytest
import respx

from app.config import get_settings
from app.errors import AppError
from app.market import CHANNEL3_SEARCH_URL, price_check


def live_settings():
    return get_settings().model_copy(update={"channel3_mode": "live", "channel3_api_key": "k-test"})


@respx.mock
def test_channel3_response_is_parsed_into_cents():
    route = respx.post(CHANNEL3_SEARCH_URL).respond(200, json={"products": [
        {"id": "p1", "title": "65W charger", "offers": [
            {"url": "u", "domain": "a.com", "price": {"price": 29.99, "currency": "USD"}, "availability": "InStock"},
            {"url": "u", "domain": "b.com", "price": {"price": 10.0, "currency": "USD"}, "availability": "InStock"}]},
        {"id": "p2", "title": "65W GaN", "offers": [
            {"url": "u", "domain": "c.com", "price": {"price": 35.5, "currency": "USD"}, "availability": "InStock"}]},
        {"id": "p3", "title": "EU", "offers": [
            {"url": "u", "domain": "d.eu", "price": {"price": 20, "currency": "EUR"}, "availability": "InStock"}]},
        {"id": "p4", "title": "gone", "offers": [
            {"url": "u", "domain": "e.com", "price": {"price": 1, "currency": "USD"}, "availability": "OutOfStock"}]},
    ], "next_page_token": None})
    r = price_check("CHG-65W-PD", "USB-C 65W PD wall charger", live_settings())
    assert r["source"] == "channel3" and r["listing_count"] == 2
    assert (r["low_cents"], r["median_cents"], r["high_cents"]) == (2999, 3274, 3550)
    req = route.calls.last.request
    assert req.headers["x-api-key"] == "k-test" and b'"query"' in req.content


@respx.mock
def test_channel3_failures_are_explicit():
    respx.post(CHANNEL3_SEARCH_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(AppError) as e:
        price_check("PB-10K", "Power bank", live_settings())
    assert e.value.code == "market_unavailable"


def test_fixture_mode_is_labeled():
    r = price_check("CHG-65W-PD", "USB-C 65W PD wall charger", get_settings())
    assert r["source"] == "fixture" and r["median_cents"] == 3250
