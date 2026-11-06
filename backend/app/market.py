"""Retail market reference prices for one SKU: Channel3 product search (live) or labeled fixture listings.

Results are reference data for the owner and the agent. They never change supplier prices or the ranking.
"""

import json
import statistics
from decimal import ROUND_HALF_UP, Decimal

import httpx

from app.config import Settings
from app.errors import AppError
from app.seed import FIXTURES

CHANNEL3_SEARCH_URL = "https://api.trychannel3.com/v1/search"
MAX_LISTINGS = 8


def _cents(value: object) -> int | None:
    try:
        return int((Decimal(str(value)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (ArithmeticError, ValueError, TypeError):
        return None


def _summary(sku: str, query: str, source: str, listings: list[dict]) -> dict:
    prices = sorted(x["price_cents"] for x in listings)
    return {
        "sku": sku,
        "query": query,
        "source": source,
        "listing_count": len(listings),
        "listings": listings[:MAX_LISTINGS],
        "low_cents": prices[0] if prices else None,
        "median_cents": int(statistics.median(prices)) if prices else None,
        "high_cents": prices[-1] if prices else None,
        "note": "Retail listing prices for reference only; supplier quotes and ranking are unaffected.",
    }


def _fixture(sku: str, query: str) -> dict:
    data = json.loads((FIXTURES / "market_prices.json").read_text())
    rows = data["listings"].get(sku, [])
    listings = [{"title": r["title"], "domain": r["domain"], "price_cents": _cents(r["price"]), "availability": "InStock"}
                for r in rows]
    return _summary(sku, query, "fixture", listings)


def _channel3(sku: str, query: str, settings: Settings, transport: httpx.BaseTransport | None) -> dict:
    try:
        with httpx.Client(timeout=httpx.Timeout(8.0, connect=3.0), transport=transport) as http:
            r = http.post(CHANNEL3_SEARCH_URL, headers={"x-api-key": settings.channel3_api_key},
                          json={"query": query, "limit": 10, "filters": {"availability": ["InStock"]}})
    except httpx.HTTPError as exc:
        raise AppError("market_unavailable", f"Channel3 search unreachable: {type(exc).__name__}", 502) from exc
    if r.status_code == 429:
        raise AppError("market_rate_limited", "Channel3 rate limit reached.", 429)
    if r.status_code >= 400:
        raise AppError("market_unavailable", f"Channel3 search failed with HTTP {r.status_code}.", 502)
    listings = []
    for product in (r.json().get("products") or []):
        for offer in product.get("offers") or []:
            price = offer.get("price") or {}
            cents = _cents(price.get("price"))
            if price.get("currency") != "USD" or not cents or offer.get("availability") != "InStock":
                continue
            listings.append({"title": str(product.get("title", ""))[:140], "domain": str(offer.get("domain", ""))[:80],
                             "price_cents": cents, "availability": "InStock"})
            break  # one offer per product keeps one retailer from dominating the median
    return _summary(sku, query, "channel3", listings)


def price_check(sku: str, name: str, settings: Settings, transport: httpx.BaseTransport | None = None) -> dict:
    query = name[:120]
    if settings.channel3_mode == "live":
        return _channel3(sku, query, settings, transport)
    return _fixture(sku, query)
