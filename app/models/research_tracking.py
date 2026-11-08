"""Versioned, owner-scoped short-horizon research contracts."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from app.models.wyckoff import WyckoffAssessment


class PriceLevel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["support", "resistance", "entry", "exit", "target"]
    label: str = Field(min_length=1, max_length=60)
    lower: float = Field(gt=0, allow_inf_nan=False, strict=True)
    upper: float = Field(gt=0, allow_inf_nan=False, strict=True)
    condition: str = Field(min_length=1, max_length=600)
    source_quote: str = Field(min_length=8, max_length=1200)

    @model_validator(mode="after")
    def ordered(self):
        if self.lower > self.upper:
            raise ValueError("价格区间上下界顺序错误")
        return self


class ResearchCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["entry", "exit", "veto"]
    text: str = Field(min_length=1, max_length=800)
    state: Literal["met", "not_met", "unknown", "invalidated"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=16)


class TrackingStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: date
    valid_until: date
    currency: Literal["CNY"] = "CNY"
    price_basis: Literal["raw"] = "raw"
    levels: list[PriceLevel] = Field(default_factory=list, max_length=16)
    conditions: list[ResearchCondition] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def dated(self):
        if not 0 <= (self.valid_until - self.as_of).days <= 31:
            raise ValueError("短线策略需要明确、有限的有效期")
        return self


class ModuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=40, max_length=12000)
    changes: list[str] = Field(max_length=12)
    evidence_ids: list[str] = Field(max_length=100)
    limitations: list[str] = Field(default_factory=list, max_length=20)
    requires_full_review: bool = False


class DailyReview(ModuleUpdate):
    research_view: Literal["bullish", "neutral", "bearish", "uncertain"]
    # These are research observations, never an order or account authorization.
    unheld_action: Literal["BUY", "WATCH", "RESEARCH_REQUIRED"]
    held_action: Literal["ADD", "HOLD", "REDUCE", "SELL", "RESEARCH_REQUIRED"]
    strategy: TrackingStrategy | None = None


class TechnicalUpdate(ModuleUpdate):
    wyckoff: WyckoffAssessment | None = None


class CreateTrack(BaseModel):
    analysis_id: str | None = Field(default=None, max_length=128)


class TrackSettings(BaseModel):
    intraday_enabled: StrictBool = True
    model_config = ConfigDict(extra="forbid")
    auto_enabled: bool = False
    max_calls_per_day: int = Field(default=6, ge=1, le=12)


class UpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retry_failed: bool = False
    manual_extra_calls: int = Field(default=0, ge=0, le=10, strict=True)
    expected_baseline_analysis_id: str | None = Field(default=None, max_length=128)


class SwitchBaselineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_id: str = Field(min_length=1, max_length=128)
    expected_baseline_analysis_id: str = Field(min_length=1, max_length=128)
