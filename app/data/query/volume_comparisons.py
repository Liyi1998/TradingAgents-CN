"""Dated volume arithmetic on an already selected, single-source daily series."""
import math


def volume_comparisons(bars: list[dict], sessions: list[str]) -> dict:
    """Caller must supply verified consecutive completed exchange sessions.

    Missing sessions, conflicting duplicates or units never become zero volume.
    No phase interpretation is inferred from these numbers.
    """
    days = sorted(set(sessions))[-6:]
    result = {"unit": "shares", "sessions": days, "previous_session": None,
              "previous_5_mean": None, "window_endpoints": None}
    if len(days) < 2:
        return result
    selected = [b for b in bars if b.get("date", b.get("trade_date")) in days]
    by_day = {b.get("date", b.get("trade_date")): b for b in selected}
    if len(selected) != len(by_day) or set(by_day) != set(days):
        return result
    if len({b.get("data_source") for b in selected}) > 1 or len({b.get("price_basis") for b in selected}) > 1:
        return result
    if any(b.get("status") or b.get("volume_unit", "shares") not in {"share", "shares"} or
           type(b.get("volume")) not in (int, float) or not math.isfinite(b["volume"]) or b["volume"] < 0
           for b in selected):
        return result
    volumes = [by_day[d]["volume"] for d in days]
    current = volumes[-1]

    def comparison(base, start, end, kind):
        return {"current_date": days[-1], "current_volume": current,
                "reference_start": start, "reference_end": end, "reference_kind": kind,
                "reference_volume": round(base, 6),
                "change_pct": round((current / base - 1) * 100, 6) if base > 0 else None}

    result["previous_session"] = comparison(volumes[-2], days[-2], days[-2], "previous_session")
    result["window_endpoints"] = {**comparison(volumes[0], days[0], days[0], "first_session"),
        "strictly_decreasing_every_session": all(a > b for a, b in zip(volumes, volumes[1:])),
        "increased_on": [days[i] for i in range(1, len(days)) if volumes[i] > volumes[i-1]]}
    if len(days) == 6:
        result["previous_5_mean"] = comparison(sum(volumes[:-1])/5, days[0], days[-2], "mean_previous_5_sessions")
    return result
