"""Request bodies. Sensitive actions reject unknown fields; no client-supplied money total is ever trusted."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProcurementIn(Strict):
    request_text: str = Field(default="", max_length=2000)
    language: Literal["en", "hi"] = "en"


class LineIn(Strict):
    sku: str = Field(min_length=1, max_length=64)
    qty: int


class ConfirmIn(Strict):
    lines: list[LineIn] = Field(max_length=50)
    budget_cents: int
    deadline: date
    coverage_days: int = 14
    currency: str = "USD"


class ApproveIn(Strict):
    version: int
    digest: str = Field(min_length=64, max_length=64)
