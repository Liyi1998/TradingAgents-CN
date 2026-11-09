"""Shared, immutable analysis-period contracts for requests and evidence windows."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal, cast

AnalysisHorizon = Literal["short", "medium", "long"]


@dataclass(frozen=True)
class HorizonProfile:
    key: AnalysisHorizon
    label: str
    holding_period: str
    daily_lookback_days: int
    technical_lookback_days: int
    financial_lookback_days: int
    valuation_sessions: int
    weekly_bars: int
    monthly_bars: int


_PROFILES = {
    "short": HorizonProfile("short", "短线", "近期交易机会（沿用现有短线计划）", 45, 730, 1460, 120, 26, 18),
    "medium": HorizonProfile("medium", "中线", "1–3个月", 180, 1095, 1460, 250, 52, 36),
    "long": HorizonProfile("long", "长线", "6–12个月", 365, 1825, 1825, 500, 104, 60),
}


def normalize_analysis_horizon(value: object = None) -> AnalysisHorizon:
    """Legacy records without a period retain their original short-term meaning."""
    if value is None:
        return "short"
    if not isinstance(value, str) or value not in _PROFILES:
        raise ValueError("analysis_horizon must be short, medium or long")
    return cast(AnalysisHorizon, value)


def get_horizon_profile(value: object = None) -> HorizonProfile:
    return _PROFILES[normalize_analysis_horizon(value)]


def horizon_start_date(context: dict, domain: str) -> str:
    """Anchor lookbacks to the requested observation date, not the wall clock."""
    profile = get_horizon_profile(context.get("analysis_horizon"))
    days = {
        "daily_quotes": profile.daily_lookback_days,
        "financial_data": profile.financial_lookback_days,
        "technical_analysis": profile.technical_lookback_days,
    }[domain]
    return (date.fromisoformat(str(context["trade_date"])[:10]) - timedelta(days=days)).isoformat()
