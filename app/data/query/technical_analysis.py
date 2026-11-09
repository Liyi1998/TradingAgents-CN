"""Deterministic technical context from standard daily bars, not MCP estimates."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from app.utils.indicators import kdj, ma, macd
from app.data.processor.quote_contract import select_quote_series


def _number(value: Any) -> float | None:
    try:
        value = float(value)
        return round(value, 6) if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def verified_price_group(frame: pd.DataFrame, end_date: str) -> bool:
    if frame.empty or "price_basis" not in frame:
        return False
    if frame["price_basis"].eq("raw").all():
        return True
    if not frame["price_basis"].isin(["qfq", "hfq"]).all() or frame["price_basis"].nunique() != 1:
        return False
    contracts = frame.get("adjustment_contract", pd.Series(None, index=frame.index))
    horizons = pd.to_datetime(frame.get("adjustment_as_of", pd.Series(None, index=frame.index)), format="%Y-%m-%d", errors="coerce")
    return bool(contracts.eq("cn_adjusted_quotes_v1").all() and horizons.notna().all()
                and horizons.nunique() == 1 and horizons.iloc[0] <= pd.Timestamp(end_date)
                and horizons.iloc[0] >= frame["trade_date"].max())


def select_daily_bars(records: list[dict], end_date: str, priorities: list[str]) -> pd.DataFrame:
    """Use one source/price basis; never double-count cross-source volume."""
    frame = pd.DataFrame(records)
    if frame.empty or not {"trade_date", "close"}.issubset(frame.columns):
        return pd.DataFrame()
    frame = frame.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame = frame[frame["trade_date"] <= pd.Timestamp(end_date)]
    if "period" in frame:
        frame = frame[frame["period"].fillna("daily").isin(["daily", "d"])]
    for key in ("open", "high", "low", "close", "volume", "amount"):
        if key not in frame:
            frame[key] = float("nan")
        frame[key] = pd.to_numeric(frame[key], errors="coerce")
    frame = frame.dropna(subset=["trade_date", "close"])
    frame = frame[frame["close"] > 0]
    if frame.empty:
        return frame
    chosen = select_quote_series(frame, end_date, priorities)
    return chosen.sort_values("trade_date").reset_index(drop=True)


def _bars(frame: pd.DataFrame, limit: int) -> list[dict]:
    output = []
    for _, row in frame.tail(limit).iterrows():
        output.append({"trade_date": row["trade_date"].strftime("%Y-%m-%d"), **{
            key: _number(row[key]) for key in ("open", "high", "low", "close", "volume", "amount")
        }})
    return output


def _aggregate(frame: pd.DataFrame, period: str, limit: int) -> list[dict]:
    work = frame.copy()
    work["_period"] = work["trade_date"].dt.to_period(period)
    grouped = work.groupby("_period", sort=True).agg(
        trade_date=("trade_date", "last"), open=("open", "first"),
        high=("high", "max"), low=("low", "min"), close=("close", "last"),
        volume=("volume", lambda values: values.sum(min_count=1)),
        amount=("amount", lambda values: values.sum(min_count=1)),
        first_trade_date=("trade_date", "first"),
    )
    result = _bars(grouped.reset_index(), limit)
    for item, (_, row) in zip(result, grouped.tail(limit).iterrows()):
        item["first_trade_date"] = row["first_trade_date"].strftime("%Y-%m-%d")
        item["derived_from_daily"] = True
    return result


def build_technical_snapshot(
    records: list[dict], calendar: list[dict], end_date: str, priorities: list[str], trajectory_days: int = 20,
    *, weekly_bars: int = 26, monthly_bars: int = 18,
) -> dict:
    """Calendar-validated OHLCV snapshot with explicit warm-up and gap rules."""
    frame = select_daily_bars(records, end_date, priorities)
    if frame.empty:
        return {"status": "no_data", "analysis_date": end_date, "daily_bars": 0}
    first = frame["trade_date"].min().strftime("%Y-%m-%d")
    last = frame["trade_date"].max().strftime("%Y-%m-%d")
    from app.data.core.analysis_context import _calendar_days
    days = _calendar_days(calendar)
    recorded = set(frame["trade_date"].dt.strftime("%Y-%m-%d"))
    expected = {day for day, is_open in days.items() if is_open and first <= day <= last}
    missing = sorted(expected - recorded)
    closed = sorted(day for day, is_open in days.items() if is_open is False and first <= day <= last)
    calendar_complete = all(days.get(day.strftime("%Y-%m-%d")) is not None for day in pd.date_range(first, last))
    invalid_closed_bars = sorted(recorded.intersection(closed))
    if invalid_closed_bars:
        frame = frame[~frame["trade_date"].dt.strftime("%Y-%m-%d").isin(invalid_closed_bars)].copy()
    if frame.empty:
        return {"status": "no_data", "analysis_date": end_date, "warnings": ["行情全部落在已确认休市日"]}
    first = frame["trade_date"].min().strftime("%Y-%m-%d")
    last = frame["trade_date"].max().strftime("%Y-%m-%d")
    # Do not bridge a genuinely missing open session when calculating recent indicators.
    tail = frame[frame["trade_date"] > pd.Timestamp(missing[-1])] if missing else frame
    tail = tail.reset_index(drop=True)
    count = len(tail)
    values: dict[str, float | None] = {}
    for period in (5, 10, 20, 60, 120, 250):
        values[f"ma{period}"] = _number(ma(tail["close"], period, min_periods=period).iloc[-1]) if count >= period else None
    close = tail["close"]
    if count >= 60:
        calculated = macd(close)
        values.update({"macd_dif": _number(calculated["dif"].iloc[-1]),
                       "macd_dea": _number(calculated["dea"].iloc[-1]),
                       "macd_hist": _number(2 * calculated["macd_hist"].iloc[-1])})
    else:
        values.update(dict.fromkeys(("macd_dif", "macd_dea", "macd_hist")))
    if count >= 20:
        mid, std = close.rolling(20, min_periods=20).mean(), close.rolling(20, min_periods=20).std(ddof=0)
        values.update({"boll_mid": _number(mid.iloc[-1]), "boll_upper": _number((mid + 2 * std).iloc[-1]),
                       "boll_lower": _number((mid - 2 * std).iloc[-1])})
    else:
        values.update(dict.fromkeys(("boll_mid", "boll_upper", "boll_lower")))
    if count >= 15:
        delta = close.diff().dropna()
        gains, losses = delta.clip(lower=0), -delta.clip(upper=0)
        gain, loss = gains.iloc[:14].mean(), losses.iloc[:14].mean()
        for g, l in zip(gains.iloc[14:], losses.iloc[14:]):
            gain, loss = (13 * gain + g) / 14, (13 * loss + l) / 14
        values["rsi14"] = _number(50 if gain == loss == 0 else 100 if loss == 0 else 100 - 100 / (1 + gain / loss))
    else:
        values["rsi14"] = None
    complete_ohlc = tail[["high", "low"]].notna().all(axis=1)
    if count >= 9 and complete_ohlc.all():
        calculated = kdj(tail["high"], tail["low"], close)
        values.update({key: _number(calculated[key].iloc[-1]) for key in ("kdj_k", "kdj_d", "kdj_j")})
    else:
        values.update(dict.fromkeys(("kdj_k", "kdj_d", "kdj_j")))
    if count >= 14 and complete_ohlc.tail(14).all():
        high, low = tail["high"].tail(14).max(), tail["low"].tail(14).min()
        values["wr14"] = _number((high - close.iloc[-1]) / (high - low) * 100) if high != low else None
    else:
        values["wr14"] = None
    warnings = []
    if not calendar_complete:
        warnings.append("交易日历覆盖不全，未将未知日期当作已确认漏数或休市日")
    if missing:
        warnings.append("存在已确认开市但无行情的日期；可能漏数或停牌，指标不跨越该缺口")
    if count < 260:
        warnings.append("有效连续历史不足260条，长周期指标及指数平滑预热可能不足")
    source = str(frame.iloc[-1]["data_source"])
    price_verified = verified_price_group(frame, end_date)
    price_basis = str(frame.iloc[-1]["price_basis"]) if price_verified else "stored_price_adjustment_unverified"
    if price_basis == "raw":
        warnings.append("指标使用已标明的不复权日线；跨除权事件的价格跳变不等同于投资收益，不宣称是前复权指标")
    elif not price_verified:
        warnings.append("标准日线未保存可核验复权标识；指标基于原始存储价格，不宣称是前复权指标")
    latest_open = max((day for day, is_open in days.items() if is_open and day <= end_date), default=None)
    if latest_open and last < latest_open:
        warnings.append(f"行情截止{last}，晚于该日期的已开市行情尚未覆盖")
    from app.data.query.volume_comparisons import volume_comparisons
    recent_daily = _bars(frame, 30)
    for bar in recent_daily:
        bar["evidence_id"] = f"technical:{source}:{price_basis}:{bar['trade_date']}"
    # A remote hole in multi-year history must not erase today's valid volume
    # comparison or the separately verified recent structure window.
    recent_start = recent_daily[0]["trade_date"]
    recent_calendar_complete = all(days.get(day.strftime("%Y-%m-%d")) is not None
        for day in pd.date_range(recent_start, last))
    comparison_start = recent_daily[-min(6, len(recent_daily))]["trade_date"]
    comparison_calendar_complete = all(days.get(day.strftime("%Y-%m-%d")) is not None
        for day in pd.date_range(comparison_start, last))
    verified_sessions = sorted(d for d in expected if d >= comparison_start) if comparison_calendar_complete else []
    return {
        "status": "success", "analysis_date": end_date, "data_cutoff": last,
        "data_source": source, "calculation_source": "local_ohlcv", "price_basis": price_basis,
        "daily_bars": len(frame), "continuous_tail_bars": count, "history_start": first,
        "calendar_complete": calendar_complete, "latest_expected_trade_date": latest_open,
        "recent_calendar_complete": recent_calendar_complete,
        "missing_open_dates": missing, "closed_dates": closed[-60:], "invalid_closed_bars": invalid_closed_bars,
        "indicator_conventions": {"ma": "完整N条交易记录算术均值，禁止不足周期冒充", "macd": "12/26/9，EMA adjust=False，柱=2*(DIF-DEA)，至少60条预热",
                                  "boll": "20/2，总体标准差ddof=0", "rsi": "Wilder14，首14次变化均值初始化", "kdj": "9/3/3，K/D初始50", "wr": "14，0至100，中国常用正值口径"},
        "indicator_trajectory": [
            {"trade_date": day, "indicators": build_technical_snapshot(records, calendar, day, priorities, trajectory_days=0).get("indicators", {})}
            for day in frame["trade_date"].tail(trajectory_days).dt.strftime("%Y-%m-%d")
        ] if trajectory_days else [],
        "indicators": values, "previous_close": _number(close.iloc[-2]) if count > 1 else None,
        "recent_daily": recent_daily, "weekly": _aggregate(frame, "W-FRI", weekly_bars), "monthly": _aggregate(frame, "M", monthly_bars),
        "volume_comparisons": volume_comparisons(recent_daily, verified_sessions),
        "period_note": "周/月线为同源日线聚合，边界首期和最后一期可能仅覆盖部分周期；成交量单位股，成交额元",
        "warnings": warnings,
    }
