"""Runtime configuration. Live modes never fall back to fixtures: missing credentials are startup errors."""

from datetime import date
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

PAYPAL_SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"
DEV_SESSION_SECRET = "replace-with-a-random-local-secret"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    public_base_url: str = "http://localhost:8000"
    # Extra browser origins allowed for cookie-authenticated mutations (e.g. the Vite dev server).
    allowed_origins: str = "http://localhost:5173"
    database_url: str = "postgresql+psycopg://tradepilot:local-dev-only@localhost:5432/tradepilot"
    session_secret: str = DEV_SESSION_SECRET

    ai_mode: Literal["fixture", "anthropic"] = "fixture"
    anthropic_api_key: str = ""
    anthropic_model: str = ""
    ai_max_tool_calls: int = 20
    ai_max_run_seconds: int = 90
    ai_max_output_tokens: int = 2048

    payment_mode: Literal["fixture", "paypal_sandbox"] = "fixture"
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_merchant_id: str = ""
    paypal_webhook_id: str = ""
    paypal_api_base_url: str = PAYPAL_SANDBOX_BASE_URL

    channel3_mode: Literal["fixture", "live"] = "fixture"
    channel3_api_key: str = ""

    enable_scenarios: bool = True
    demo_business_date: date | None = None
    business_timezone: str = "America/Los_Angeles"

    reservation_minutes: int = 15
    session_ttl_hours: int = 24
    max_runs_per_session_per_day: int = 20
    max_sessions_per_ip_per_hour: int = 30
    tool_spend_cap_per_run_cents: int = 25
    tool_spend_cap_per_day_cents: int = 200
    jobs_poll_seconds: float = 1.0
    run_jobs_in_process: bool = True

    port: int = 8000

    @property
    def origins(self) -> set[str]:
        extra = {o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()}
        return {self.public_base_url.rstrip("/")} | extra

    @property
    def secure_cookies(self) -> bool:
        return self.public_base_url.startswith("https://")

    def problems(self) -> list[str]:
        """Configuration errors that must stop startup, with setup guidance."""
        out: list[str] = []
        if self.app_env == "production" and self.session_secret == DEV_SESSION_SECRET:
            out.append("SESSION_SECRET must be set to a random value outside development.")
        if self.ai_mode == "anthropic":
            if not self.anthropic_api_key:
                out.append("AI_MODE=anthropic requires ANTHROPIC_API_KEY (Claude API key, not a Claude Code login).")
            if not self.anthropic_model:
                out.append("AI_MODE=anthropic requires ANTHROPIC_MODEL set to a model ID your API account supports.")
        if self.payment_mode == "paypal_sandbox":
            for name in ("paypal_client_id", "paypal_client_secret", "paypal_merchant_id", "paypal_webhook_id"):
                if not getattr(self, name):
                    out.append(f"PAYMENT_MODE=paypal_sandbox requires {name.upper()}.")
            if self.paypal_api_base_url.rstrip("/") != PAYPAL_SANDBOX_BASE_URL:
                out.append(f"PAYPAL_API_BASE_URL must be {PAYPAL_SANDBOX_BASE_URL}; production PayPal is not supported.")
        if self.channel3_mode == "live" and not self.channel3_api_key:
            out.append("CHANNEL3_MODE=live requires CHANNEL3_API_KEY.")
        if self.app_env == "production" and self.payment_mode == "fixture":
            out.append("PAYMENT_MODE=fixture is not allowed when APP_ENV=production.")
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
