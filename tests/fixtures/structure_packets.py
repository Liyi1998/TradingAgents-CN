"""Synthetic single-stock packets for isolated page checks. Not exchange data."""

from __future__ import annotations

import json
from pathlib import Path

ENDS = (600, 630, 660, 690, 810, 840, 870, 900)
COMPLETE = [
    (10.0, 10.2, 9.8, 10.0),
    (10.0, 10.6, 9.9, 10.4),
    (10.4, 11.5, 10.3, 11.2),
    (11.0, 11.2, 10.6, 10.8),
    (10.8, 11.0, 10.5, 10.7),
    (10.8, 11.8, 10.7, 11.2),
    (11.1, 11.3, 10.9, 11.0),
    (11.0, 11.2, 10.8, 10.9),
]


def bar(day: str, minute: int, prices: tuple, source: str = "synthetic-test") -> dict:
    opened, high, low, close = prices
    return {
        "symbol": "900001",
        "datetime": f"{day} {minute // 60:02d}:{minute % 60:02d}:00",
        "freq": "30min",
        "open": opened,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1000,
        "ohlc_kind": "native_bar",
        "price_basis": "raw",
        "volume_unit": "share",
        "bar_time_kind": "end",
        "data_source": source,
    }


def session(symbol: str, day: str, prices) -> list[dict]:
    rows = []
    for minute, price in zip(ENDS, prices):
        row = bar(day, minute, price)
        row["symbol"] = symbol
        rows.append(row)
    return rows


def calendar(days: dict[str, bool]) -> list[dict]:
    return [{"cal_date": day, "is_open": opened} for day, opened in days.items()]


def closed_span(start: str, end: str) -> dict[str, bool]:
    from datetime import datetime, timedelta

    cursor = datetime.strptime(start, "%Y-%m-%d")
    last = datetime.strptime(end, "%Y-%m-%d")
    out = {}
    while cursor <= last:
        out[cursor.date().isoformat()] = False
        cursor += timedelta(days=1)
    return out


def base_calendar() -> dict[str, bool]:
    days = {"2026-11-05": True}
    days.update(closed_span("2026-11-06", "2027-06-30"))
    return days


def factors(symbol: str, day: str = "2026-11-05") -> list[dict]:
    return [{"symbol": symbol, "trade_date": day, "adj_factor": 1}]


def packet(symbol: str, rows, *, with_calendar=True, with_factors=True) -> dict:
    return {
        "rows": rows,
        "calendar": calendar(base_calendar()) if with_calendar else [],
        "adjustments": factors(symbol) if with_factors else [],
        "daily": [],
        "index_rows": [],
        "index_symbol": "000300.SH",
        "issues": [],
    }


def scenario_packets() -> dict[str, dict]:
    day = "2026-11-05"
    complete = session("900001", day, COMPLETE)
    missing_interior = [row for row in session("900003", day, COMPLETE) if not row["datetime"].endswith("11:30:00")]
    stale = [row for row in session("900004", day, COMPLETE) if row["datetime"] <= f"{day} 11:30:00"]
    conflicted = session("900005", day, COMPLETE)
    conflicted.append(
        {**conflicted[3], "open": 11.05, "high": 11.4, "low": 10.7, "close": 11.1, "data_source": "synthetic-other"}
    )
    return {
        "900001": packet("900001", complete),
        "900002": packet("900002", session("900002", day, COMPLETE), with_calendar=False),
        "900003": packet("900003", missing_interior),
        "900004": packet("900004", stale),
        "900005": packet("900005", conflicted),
        "900006": packet("900006", session("900006", day, COMPLETE), with_factors=False),
    }


def write_packets(directory: str) -> None:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    for symbol, body in scenario_packets().items():
        (root / f"{symbol}.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    import sys

    write_packets(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).with_name("structure_auxiliary")))
