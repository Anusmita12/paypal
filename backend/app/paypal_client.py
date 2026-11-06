"""PayPal Sandbox REST adapter: OAuth, Orders v2 create/retrieve/capture, and webhook signature verification.

Error mapping keeps the payment rules honest: timeouts, connection failures, 429 and 5xx are `ProviderTimeout`
(outcome unknown, reconcile later); 4xx answers are `ProviderRejected` with PayPal's issue code.
"""

import threading
import time
from typing import Any

import httpx

from app.config import Settings
from app.payments import ProviderRejected, ProviderTimeout

TIMEOUT = httpx.Timeout(20.0, connect=5.0)
_token_lock = threading.Lock()
_token: dict[str, Any] = {"value": None, "expires": 0.0, "client_id": None}


class SandboxPayPal:
    mode = "paypal_sandbox"

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.base = settings.paypal_api_base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=TIMEOUT, transport=transport)

    # --- plumbing ---

    def _access_token(self, force: bool = False) -> str:
        with _token_lock:
            fresh = _token["client_id"] == self.settings.paypal_client_id and time.monotonic() < _token["expires"]
            if fresh and not force:
                return _token["value"]
            try:
                r = self.http.post(
                    "/v1/oauth2/token",
                    data={"grant_type": "client_credentials"},
                    auth=(self.settings.paypal_client_id, self.settings.paypal_client_secret),
                )
            except httpx.HTTPError as exc:
                raise ProviderTimeout(f"oauth: {type(exc).__name__}") from exc
            if r.status_code >= 500 or r.status_code == 429:
                raise ProviderTimeout(f"oauth: HTTP {r.status_code}")
            if r.status_code != 200:
                raise ProviderRejected("AUTHENTICATION_FAILURE", "PayPal rejected the client credentials.")
            body = r.json()
            _token.update(value=body["access_token"], expires=time.monotonic() + int(body["expires_in"]) - 60,
                          client_id=self.settings.paypal_client_id)
            return _token["value"]

    def _call(self, method: str, path: str, *, json: Any = None, headers: dict | None = None) -> dict:
        for attempt in (1, 2):
            h = {"Authorization": f"Bearer {self._access_token(force=attempt == 2)}", "Content-Type": "application/json",
                 **(headers or {})}
            try:
                r = self.http.request(method, path, json=json, headers=h)
            except httpx.HTTPError as exc:
                # A request that may have reached PayPal has an unknown outcome.
                raise ProviderTimeout(f"{method} {path}: {type(exc).__name__}") from exc
            if r.status_code == 401 and attempt == 1:
                continue  # expired token; retry once with a fresh one (same request ID keeps it idempotent)
            if r.status_code >= 500 or r.status_code == 429:
                raise ProviderTimeout(f"{method} {path}: HTTP {r.status_code} debug_id={r.headers.get('paypal-debug-id')}")
            if r.status_code >= 400:
                raise ProviderRejected(*_issue(r))
            return r.json() if r.content else {}
        raise ProviderRejected("AUTHENTICATION_FAILURE", "PayPal rejected a fresh access token.")

    # --- Orders v2 ---

    def create_order(self, request_id: str, payload: dict) -> dict:
        return self._call("POST", "/v2/checkout/orders", json=payload,
                          headers={"PayPal-Request-Id": request_id, "Prefer": "return=representation"})

    def get_order(self, order_id: str) -> dict:
        return self._call("GET", f"/v2/checkout/orders/{_safe_id(order_id)}")

    def capture_order(self, order_id: str, request_id: str) -> dict:
        order = self._call("POST", f"/v2/checkout/orders/{_safe_id(order_id)}/capture", json={},
                           headers={"PayPal-Request-Id": request_id, "Prefer": "return=representation"})
        unit = (order.get("purchase_units") or [{}])[0]
        if not unit.get("payee") or not unit.get("custom_id"):
            # The finalizer validates payee and correlation; take them from the authoritative order record.
            return self.get_order(order_id)
        return order

    # --- Payouts ---

    def create_payout(self, request_id: str, payload: dict) -> dict:
        return self._call("POST", "/v1/payments/payouts", json=payload, headers={"PayPal-Request-Id": request_id})

    def get_payout(self, payout_batch_id: str) -> dict:
        return self._call("GET", f"/v1/payments/payouts/{_safe_id(payout_batch_id)}")

    # --- webhooks ---

    def verify_webhook(self, headers: dict[str, str], event: dict) -> bool:
        body = {
            "auth_algo": headers.get("paypal-auth-algo", ""),
            "cert_url": headers.get("paypal-cert-url", ""),
            "transmission_id": headers.get("paypal-transmission-id", ""),
            "transmission_sig": headers.get("paypal-transmission-sig", ""),
            "transmission_time": headers.get("paypal-transmission-time", ""),
            "webhook_id": self.settings.paypal_webhook_id,
            "webhook_event": event,
        }
        if not all(body[k] for k in ("auth_algo", "cert_url", "transmission_id", "transmission_sig", "transmission_time")):
            return False
        try:
            result = self._call("POST", "/v1/notifications/verify-webhook-signature", json=body)
        except ProviderRejected:
            return False
        return result.get("verification_status") == "SUCCESS"


def _safe_id(value: str) -> str:
    if not value.isalnum() or len(value) > 64:
        raise ProviderRejected("INVALID_RESOURCE_ID", "Malformed PayPal ID.")
    return value


def _issue(r: httpx.Response) -> tuple[str, str]:
    try:
        body = r.json()
    except ValueError:
        return f"HTTP_{r.status_code}", ""
    details = body.get("details") or []
    code = (details[0].get("issue") if details else None) or body.get("name") or f"HTTP_{r.status_code}"
    return code, (body.get("message") or "")[:200]
