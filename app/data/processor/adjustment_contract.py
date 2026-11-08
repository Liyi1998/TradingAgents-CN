"""CN price-adjustment contracts; pure conversion/alignment, no source I/O.

``adj_factor`` is a multiplier of an explicitly *raw* price. Provider event
tables are not daily observations: keep the predecessor anchor and apply each
event from its effective date. A missing/legacy/mixed series is unknown, not 1.
The extraction horizon ``coverage_end`` is not a claim that a provider has
already published every corporate action; source freshness is a separate gate.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime
import hashlib
import json
import math
from numbers import Real

import numpy as np
import pandas as pd


ADJUSTMENT_CONTRACT = "cn_price_multiplier_v1"
ADJUSTMENT_BASIS = "raw_price_multiplier"
ADJUSTED_QUOTES_CONTRACT = "cn_adjusted_quotes_v1"
_METHODS = {"akshare": "akshare_qfq_reciprocal",
            "baostock": "baostock_fore_multiplier"}


def _day(value) -> str:
    if value is None or pd.isna(value):
        raise ValueError("An explicit valid date is required")
    if isinstance(value, (datetime, date)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    value = str(value)
    if len(value) == 8 and value.isdigit():
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    return date.fromisoformat(value).isoformat()


def _positive(value) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise ValueError("Adjustment values must be positive finite numbers")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Adjustment values must be positive finite numbers") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Adjustment values must be positive finite numbers")
    return number


def _first(row, names):
    for name in names:
        value = row.get(name)
        if value is not None and not pd.isna(value) and value != "":
            return value
    return None


def normalize_adjustment_events(
    frame: pd.DataFrame, *, source: str, symbol: str,
    start_date: str | None = None, end_date: str,
) -> pd.DataFrame:
    """Normalize one complete provider event response, retaining one anchor.

    AKShare/Sina's qfq factor is a *divisor*. BaoStock's foreAdjustFactor
    multiplies raw prices. Both are re-anchored to the last effective event at
    ``end_date`` so the output series has one explicit, shared price basis.
    ``frame`` must include events before ``start_date``; an unproven initial
    interval is deliberately not invented when no anchor exists.
    """
    if source not in _METHODS:
        raise ValueError("Unsupported CN adjustment source")
    symbol = str(symbol)
    if len(symbol) != 6 or not symbol.isdigit() or symbol == "000000":
        raise ValueError("A valid six-digit CN symbol is required")
    end = _day(end_date)
    start = _day(start_date) if start_date else None
    if start and start > end:
        raise ValueError("Adjustment start is after end")
    events = {}
    for row in frame.to_dict("records"):
        supplied_symbol = _first(row, ("symbol", "code"))
        if supplied_symbol is not None:
            supplied_symbol = str(supplied_symbol).upper()
            if "." in supplied_symbol:
                parts = supplied_symbol.split(".")
                supplied_symbol = next((part for part in parts if part.isdigit()), "")
            elif supplied_symbol.startswith(("SH", "SZ", "BJ")):
                supplied_symbol = supplied_symbol[2:]
            if supplied_symbol != symbol:
                raise ValueError("Provider adjustment symbol differs from request")
        effective = _day(_first(row, ("trade_date", "date", "dividOperateDate")))
        if effective > end:
            continue
        raw = _positive(_first(row, ("qfq_factor",)) if source == "akshare"
                        else _first(row, ("foreAdjustFactor",)))
        multiplier = 1.0 / raw if source == "akshare" else raw
        back = _first(row, ("hfq_factor",) if source == "akshare"
                      else ("backAdjustFactor",))
        back = _positive(back) if back is not None else None
        event = (multiplier, raw, back)
        if effective in events and events[effective] != event:
            raise ValueError("Conflicting adjustment events on the same date")
        events[effective] = event
    if not events:
        return pd.DataFrame()
    dates = sorted(events)
    reference = events[dates[-1]][0]
    canonical = [(day, _positive(events[day][0] / reference)) for day in dates]
    # A common normalization snapshot cannot be spliced with older rebased
    # values or a second provider. No date-only version masquerades as one.
    series_id = hashlib.sha256(json.dumps(
        [source, symbol, ADJUSTMENT_CONTRACT, _METHODS[source], canonical],
        separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    if start:
        anchors = [day for day in dates if day <= start]
        anchor = anchors[-1] if anchors else start
        canonical = [(day, value) for day, value in canonical if day >= anchor]
    rows = []
    for effective, multiplier in canonical:
        _, raw, back = events[effective]
        rows.append({
            "symbol": symbol, "market": "CN", "data_source": source,
            "trade_date": effective, "adj_factor": multiplier,
            "fore_adj_factor": multiplier, "back_adj_factor": back,
            "raw_factor": raw,
            "adjustment_basis": ADJUSTMENT_BASIS,
            "adjustment_contract": ADJUSTMENT_CONTRACT,
            "adjustment_method": _METHODS[source],
            "adjustment_series_id": series_id,
            "record_type": "event", "coverage_end": end,
        })
    return pd.DataFrame(rows)


def adjustment_schemas(frame: pd.DataFrame, *, source: str):
    """Adapt only normalized CN rows, never relabel legacy scalars as trusted."""
    from app.data.schema.domains.adj_factors import AdjFactorsSchema

    result = []
    for row in frame.to_dict("records"):
        if (row.get("adjustment_contract") != ADJUSTMENT_CONTRACT
                or row.get("adjustment_basis") != ADJUSTMENT_BASIS
                or row.get("adjustment_method") != _METHODS.get(source)
                or row.get("record_type") != "event"
                or row.get("data_source") != source
                or not row.get("adjustment_series_id")):
            raise ValueError("CN adjustment rows require a normalized event contract")
        values = {field: row[field] for field in AdjFactorsSchema.__dataclass_fields__
                  if field in row and row[field] is not None and not pd.isna(row[field])}
        values["adj_factor"] = _positive(row.get("adj_factor"))
        values["trade_date"] = _day(row.get("trade_date"))
        values["coverage_end"] = _day(row.get("coverage_end"))
        if values["coverage_end"] < values["trade_date"]:
            raise ValueError("Adjustment event is beyond its coverage horizon")
        result.append(AdjFactorsSchema(**values))
    return result


def align_price_adjustments(
    quotes: pd.DataFrame, factors: pd.DataFrame, *, as_of: str,
) -> pd.DataFrame:
    """Return copied quotes plus adjusted close and per-row evidence/status.

    Callers must load the latest event at/before each quote-window start AND
    every event through ``as_of`` (not just exact daily matches). Relevant rows
    must come from one complete normalized source snapshot. An unknown quote
    basis, missing anchor, insufficient horizon, legacy row or mixed snapshot
    invalidates this stock's adjustment window. No default-to-one or backfill
    from a future event is allowed.
    """
    end = _day(as_of)
    out = quotes.copy().reset_index(drop=True)
    out["close_adj"] = np.nan
    out["_adjustment_status"] = "unknown"
    out["_adjustment_source"] = None
    out["_adjustment_contract"] = None
    if out.empty:
        out.index = quotes.index.copy()
        return out
    if not {"symbol", "trade_date", "close"}.issubset(out.columns):
        raise ValueError("Quotes require symbol, trade_date and close")

    for symbol, group in out.groupby("symbol", sort=False, dropna=False):
        positions = group.index

        def reject(status):
            out.loc[positions, "_adjustment_status"] = status

        try:
            quote_dates = [_day(value) for value in group["trade_date"]]
            if max(quote_dates) > end:
                reject("future_quote")
                continue
            if len(set(quote_dates)) != len(quote_dates):
                reject("duplicate_quote_dates")
                continue
        except (ValueError, TypeError):
            reject("invalid_quote_date")
            continue
        basis = set(group["price_basis"]) if "price_basis" in group else set()
        # Already-adjusted prices are never multiplied again. They are usable
        # only with an explicit common normalization horizon/version, otherwise
        # a mixture of different qfq snapshots could create another fake jump.
        if basis in ({"qfq"}, {"hfq"}):
            needed = {"adjustment_contract", "adjustment_as_of", "data_source"}
            if (not needed.issubset(group.columns)
                    or set(group["adjustment_contract"]) != {ADJUSTED_QUOTES_CONTRACT}
                    or group[list(needed)].isna().any().any()
                    or group["adjustment_as_of"].nunique() != 1
                    or group["data_source"].nunique() != 1):
                reject("unverified_adjusted_quote_basis")
                continue
            try:
                anchor = _day(group["adjustment_as_of"].iloc[0])
            except (ValueError, TypeError):
                reject("unverified_adjusted_quote_basis")
                continue
            if anchor != end:
                reject("adjusted_quote_horizon_mismatch")
                continue
            values = pd.to_numeric(group["close"], errors="coerce")
            numeric = group["close"].map(
                lambda value: isinstance(value, Real)
                and not isinstance(value, (bool, np.bool_)))
            usable = values.astype(float).where(numeric & np.isfinite(values) & values.gt(0))
            out.loc[positions, "close_adj"] = usable
            out.loc[positions, "_adjustment_status"] = "already_adjusted"
            out.loc[positions, "_adjustment_source"] = group["data_source"].iloc[0]
            out.loc[positions, "_adjustment_contract"] = ADJUSTED_QUOTES_CONTRACT
            out.loc[positions[usable.isna()], "_adjustment_status"] = "invalid_close"
            continue
        if basis != {"raw"}:
            reject("unknown_quote_basis")
            continue
        if factors.empty or "symbol" not in factors:
            reject("missing_adjustment_series")
            continue
        events = factors[factors["symbol"].eq(symbol)].copy()
        if events.empty or "trade_date" not in events:
            reject("missing_adjustment_series")
            continue
        try:
            events["_effective"] = events["trade_date"].map(_day)
            events = events[events["_effective"].le(end)]
            anchors = events[events["_effective"].le(min(quote_dates))]
            if anchors.empty:
                reject("missing_start_anchor")
                continue
            events = events[events["_effective"].ge(anchors["_effective"].max())]
            required = {"data_source", "adjustment_contract", "adjustment_basis",
                        "adjustment_method", "adjustment_series_id", "coverage_end",
                        "record_type", "adj_factor"}
            if not required.issubset(events.columns) or events[list(required)].isna().any().any():
                reject("legacy_or_incomplete_contract")
                continue
            source = set(events["data_source"])
            if (len(source) != 1 or next(iter(source)) not in _METHODS
                    or events["adjustment_series_id"].nunique() != 1
                    or not events["adjustment_series_id"].map(bool).all()):
                reject("mixed_adjustment_series")
                continue
            source = next(iter(source))
            if (set(events["adjustment_contract"]) != {ADJUSTMENT_CONTRACT}
                    or set(events["adjustment_basis"]) != {ADJUSTMENT_BASIS}
                    or set(events["adjustment_method"]) != {_METHODS[source]}
                    or set(events["record_type"]) != {"event"}):
                reject("unsupported_adjustment_contract")
                continue
            if events["coverage_end"].map(_day).lt(end).any():
                reject("insufficient_adjustment_horizon")
                continue
            events["adj_factor"] = events["adj_factor"].map(_positive)
            if events.groupby("_effective")["adj_factor"].nunique().gt(1).any():
                reject("conflicting_adjustment_events")
                continue
            events = events.drop_duplicates("_effective").sort_values("_effective")
            effective = events["_effective"].tolist()
            # A later provider snapshot can include events after historical
            # as_of. Filter them first, then remove its later common baseline.
            reference = events["adj_factor"].iloc[-1]
            multipliers = [_positive(value / reference)
                           for value in events["adj_factor"]]
            for position, day, close in zip(positions, quote_dates, group["close"]):
                if isinstance(close, (bool, np.bool_)) or not isinstance(close, Real):
                    out.at[position, "_adjustment_status"] = "invalid_close"
                    continue
                value = float(close) * multipliers[bisect_right(effective, day) - 1]
                if not math.isfinite(value) or value <= 0:
                    out.at[position, "_adjustment_status"] = "invalid_close"
                    continue
                out.at[position, "close_adj"] = value
                out.at[position, "_adjustment_status"] = "verified_event_series"
                out.at[position, "_adjustment_source"] = source
                out.at[position, "_adjustment_contract"] = ADJUSTMENT_CONTRACT
        except (ValueError, TypeError, OverflowError):
            reject("invalid_adjustment_events")
    out.index = quotes.index.copy()
    return out
