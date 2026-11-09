"""Single-stock TPO/SMC auxiliary. Deterministic, prefix-only, not a trade signal.

TPO counts each price bin covered by a complete 30-minute high-low at most once.
It is not time-at-price, trade count, or a volume profile. SMC structure is
knowable only after the confirming bar has closed. Nothing here writes a
report, a Wyckoff phase, or an order.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
import math
from zoneinfo import ZoneInfo

CN = ZoneInfo("Asia/Shanghai")
# Keep aligned with app.engine.intraday_research.ENDS (interval end, minutes).
SESSION_ENDS = (600, 630, 660, 690, 810, 840, 870, 900)
ALGORITHM_VERSION = "structure-auxiliary-v1"
TPO_VERSION = "bar-tpo-m30-v1"
SMC_VERSION = "smc-m30-v1"
TPO_LABEL = "30分钟OHLC区间覆盖的bar-based TPO近似"
MAX_BARS = 160
MAX_BINS = 4000


def _finite(value):
    return type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(value)


def _id(payload) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _day(value) -> str | None:
    text = str(value or "")[:10]
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return text


def _price(ticks: int, tick: Decimal) -> float:
    return float(Decimal(ticks) * tick)


def _ticks(price, tick: Decimal) -> int | None:
    if not _finite(price):
        return None
    try:
        scaled = Decimal(str(price)) / tick
    except Exception:
        return None
    nearest = round(scaled)
    if abs(scaled - nearest) > Decimal("0.000001"):
        return None
    return int(nearest)


def _parameters(overrides: dict | None) -> dict:
    params = {
        "tick": "0.01",
        "bin_multiple": 1,
        "value_area": "0.70",
        "swing_left": 2,
        "swing_right": 2,
        "publication_delay_seconds": 60,
        "max_bars": MAX_BARS,
        "max_bins": MAX_BINS,
    }
    for key, value in (overrides or {}).items():
        if key in params:
            params[key] = value
    tick = Decimal(str(params["tick"]))
    multiple = params["bin_multiple"]
    left, right = params["swing_left"], params["swing_right"]
    delay = params["publication_delay_seconds"]
    ratio = Fraction(str(params["value_area"]))
    if (
        tick <= 0
        or type(multiple) is not int
        or not 1 <= multiple <= 100
        or type(left) is not int
        or type(right) is not int
        or not 1 <= left <= 5
        or not 1 <= right <= 5
        or type(delay) is not int
        or not 0 <= delay <= 300
        or type(params["max_bars"]) is not int
        or not 8 <= params["max_bars"] <= MAX_BARS
        or type(params["max_bins"]) is not int
        or not 8 <= params["max_bins"] <= MAX_BINS
        or not Fraction(1, 2) <= ratio <= Fraction(9, 10)
    ):
        raise ValueError("结构辅助参数超出允许范围")
    params.update(tick_decimal=tick, ratio=ratio)
    return params


def _calendar(rows) -> tuple[str, dict[str, bool]]:
    if not rows:
        return "unverified", {}
    days: dict[str, bool | None] = {}
    for row in rows:
        day = _day(row.get("cal_date") or row.get("trade_date") or row.get("date"))
        if not day:
            continue
        raw = row.get("is_open")
        if isinstance(raw, bool):
            opened = raw
        else:
            text = str(raw).lower()
            opened = text in {"1", "true"} if text in {"1", "true", "0", "false"} else None
        if day not in days:
            days[day] = opened
        elif days[day] != opened:
            days[day] = None
    if any(value is None for value in days.values()):
        return "conflict", {day: bool(value) for day, value in days.items() if value is not None}
    return "verified", {day: bool(value) for day, value in days.items()}


def _adjustments(rows, symbol: str) -> tuple[str, dict[str, Decimal], list[str]]:
    by_day: dict[str, set[str]] = defaultdict(set)
    for row in rows or []:
        row_symbol = str(row.get("symbol") or "")
        if row_symbol and row_symbol.split(".")[0].zfill(6) != symbol:
            continue
        day = _day(row.get("trade_date") or row.get("date"))
        if not day or not _finite(row.get("adj_factor")) or row.get("adj_factor") <= 0:
            continue
        by_day[day].add(str(Decimal(str(row["adj_factor"])).normalize()))
    factors = {}
    conflict = False
    for day, values in by_day.items():
        if len(values) != 1:
            conflict = True
            continue
        factors[day] = Decimal(next(iter(values)))
    if conflict:
        return "conflict", factors, sorted(day for day, values in by_day.items() if len(values) != 1)
    return ("unverified" if not factors else "consistent"), factors, []


def _normalize(symbol, rows, now, tick: Decimal, delay: int, max_bars: int):
    local = now.astimezone(CN)
    grouped = defaultdict(list)
    rejected = future = 0
    for row in rows or []:
        raw_symbol = str(row.get("symbol") or "")
        if raw_symbol and raw_symbol.split(".")[0].zfill(6) != symbol:
            rejected += 1
            continue
        stamp = str(row.get("datetime") or "")
        try:
            end = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN)
        except ValueError:
            rejected += 1
            continue
        minute = end.hour * 60 + end.minute
        if (
            local < end + timedelta(seconds=delay)
            or end.second != 0
            or minute not in SESSION_ENDS
            or row.get("freq") != "30min"
            or row.get("ohlc_kind") != "native_bar"
            or row.get("price_basis") != "raw"
            or row.get("bar_time_kind") != "end"
            or row.get("volume_unit") != "share"
        ):
            if local < end + timedelta(seconds=delay) and end.second == 0 and minute in SESSION_ENDS:
                future += 1
            else:
                rejected += 1
            continue
        if any(not _finite(row.get(key)) or row[key] <= 0 for key in ("open", "high", "low", "close")):
            rejected += 1
            continue
        if not row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"]:
            rejected += 1
            continue
        if _ticks(row["low"], tick) is None or _ticks(row["high"], tick) is None:
            rejected += 1
            continue
        if row.get("volume") is not None and (not _finite(row.get("volume")) or row["volume"] < 0):
            rejected += 1
            continue
        source = str(row.get("data_source") or "")
        if not source:
            rejected += 1
            continue
        bar = {
            "datetime": stamp,
            "day": stamp[:10],
            "end_minute": minute,
            "source": source,
            "price_basis": "raw",
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        bar["id"] = _id({key: bar[key] for key in ("datetime", "source", "open", "high", "low", "close")})
        grouped[stamp].append(bar)
    conflicts = []
    chosen = []
    for stamp, items in grouped.items():
        prices = {(item["open"], item["high"], item["low"], item["close"]) for item in items}
        if len(prices) != 1:
            conflicts.append(stamp)
            continue
        bar = dict(items[0])
        bar["sources"] = sorted({item["source"] for item in items})
        if len(bar["sources"]) == 1:
            bar["source"] = bar["sources"][0]
        chosen.append(bar)
    chosen.sort(key=lambda item: item["datetime"])
    truncated = len(chosen) > max_bars
    if truncated:
        chosen = chosen[-max_bars:]
    return chosen, rejected, future, conflicts, truncated


def _expected_minutes(day: str, now) -> list[int]:
    local = now.astimezone(CN)
    today = local.date().isoformat()
    if day < today:
        return list(SESSION_ENDS)
    if day > today:
        return []
    ready = []
    for minute in SESSION_ENDS:
        end = local.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)
        if local >= end + timedelta(seconds=0):
            # Caller already removed bars still inside the publication delay.
            # A minute is expected only when its end plus delay is <= now.
            pass
        if local >= end:
            ready.append(minute)
    return ready


def _due_end(day: str, now, delay: int):
    local = now.astimezone(CN)
    due = None
    for minute in SESSION_ENDS:
        end = datetime.strptime(f"{day} {minute // 60:02d}:{minute % 60:02d}:00", "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=CN
        )
        if local >= end + timedelta(seconds=delay):
            due = end
    return due


def _freshness(bars, now, calendar_status: str, open_days: dict[str, bool], delay: int) -> dict:
    """Clock alignment only. A stale cutoff does not delete an older complete profile."""
    if not bars:
        return {"name": "新鲜度", "status": "cannot_judge", "as_of": None, "reason": "没有可用的完整K线"}
    last = datetime.strptime(bars[-1]["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN)
    local = now.astimezone(CN)
    age = int((local - last).total_seconds())
    as_of = bars[-1]["datetime"]
    gate = {"name": "新鲜度", "as_of": as_of, "age_seconds": age}
    if age < 0:
        return {**gate, "status": "cannot_judge", "reason": "K线时间晚于观察时钟，不能使用"}
    today = local.date().isoformat()
    if calendar_status == "verified" and today not in open_days:
        return {**gate, "status": "cannot_judge", "reason": "观察日不在已读取的交易日历覆盖范围内，不能判断是否陈旧"}
    if calendar_status != "verified":
        if last.date().isoformat() == today and age <= 30 * 60 + delay:
            return {
                **gate,
                "status": "cannot_judge",
                "reason": "最后一根K线离观察时钟不远，但交易日历未证明，新鲜度不能标成已核验",
            }
        return {
            **gate,
            "status": "stale",
            "reason": "交易日历未证明，且最后一根K线不是当前附近的完整K线；按陈旧处理，不补缺失会话",
        }
    if open_days.get(today) is True:
        due = _due_end(today, now, delay)
        if due is None:
            prior = [day for day, opened in open_days.items() if opened and day < today]
            if not prior:
                return {**gate, "status": "cannot_judge", "reason": "尚未到首根K线，也没有更早的已证明交易日"}
            previous = max(prior)
            close = datetime.strptime(f"{previous} 15:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN)
            if last == close:
                return {**gate, "status": "ready", "reason": f"开盘前已对齐最近交易日 {previous} 的收盘K线"}
            return {**gate, "status": "stale", "reason": f"开盘前缺少 {previous} 15:00 的K线，实际只到 {as_of}"}
        if last == due:
            return {**gate, "status": "ready", "reason": "已对齐当前应完成的最后一根K线，不是逐笔成交价"}
        return {**gate, "status": "stale", "reason": f"当前应有截至 {due.strftime('%H:%M')} 的K线，实际只到 {as_of}"}
    prior = [day for day, opened in open_days.items() if opened and day <= today]
    if not prior:
        return {**gate, "status": "cannot_judge", "reason": "日历没有已证明的交易日，不能判断分钟线是否陈旧"}
    previous = max(prior)
    close = datetime.strptime(f"{previous} 15:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN)
    if last == close:
        return {**gate, "status": "ready", "reason": f"最近已证明交易日 {previous} 的收盘K线已到"}
    return {**gate, "status": "stale", "reason": f"最近已证明交易日是 {previous}，分钟线只到 {as_of}"}


def _sessions(bars, conflicts, now, calendar_status, open_days, delay: int):
    local = now.astimezone(CN)
    by_day = defaultdict(list)
    for bar in bars:
        by_day[bar["day"]].append(bar)
    conflict_days = {stamp[:10] for stamp in conflicts}
    days = set(by_day) | conflict_days
    if calendar_status == "verified":
        first = min(days) if days else local.date().isoformat()
        last = local.date().isoformat()
        cursor = datetime.strptime(first, "%Y-%m-%d").date()
        end = datetime.strptime(last, "%Y-%m-%d").date()
        while cursor <= end:
            key = cursor.isoformat()
            if open_days.get(key) is True:
                days.add(key)
            cursor += timedelta(days=1)
    sessions = []
    for day in sorted(days):
        present = {bar["end_minute"]: bar for bar in by_day.get(day, [])}
        if calendar_status == "verified" and open_days.get(day) is False:
            status = "conflict" if present or day in conflict_days else "closed"
            sessions.append(
                {
                    "date": day,
                    "status": status,
                    "bars": [],
                    "missing": [],
                    "reason": "日历标为休市但出现分钟线" if status == "conflict" else "休市，不补造K线",
                }
            )
            continue
        if calendar_status == "verified" and open_days.get(day) is not True and not present:
            continue
        expected = []
        for minute in _expected_minutes(day, now) if day == local.date().isoformat() else list(SESSION_ENDS):
            end = datetime.strptime(f"{day} {minute // 60:02d}:{minute % 60:02d}:00", "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=CN
            )
            if local >= end + timedelta(seconds=delay):
                expected.append(minute)
        missing = [minute for minute in expected if minute not in present]
        ordered = [present[minute] for minute in SESSION_ENDS if minute in present and minute in expected]
        if day in conflict_days:
            status, reason = "conflict", "同一时点存在不一致的OHLC，整天隔离，不挑选其中一份"
            ordered = []
        elif not expected and not ordered:
            status, reason = "waiting", "尚无已过发布等待期的完整30分钟K线"
        elif missing:
            labels = "、".join(f"{minute // 60:02d}:{minute % 60:02d}" for minute in missing)
            status, reason = "incomplete", f"缺少已完成K线 {labels}，不填平、不计算该日接受区"
            ordered = []
        elif day < local.date().isoformat() or expected == list(SESSION_ENDS):
            status, reason = "complete", "当日8根30分钟K线齐全"
        else:
            status, reason = "in_progress", "交易日尚未结束；已完成的前缀可用于结构，不可当作全天接受区"
        sessions.append({"date": day, "status": status, "bars": ordered, "missing": missing, "reason": reason})
    return sessions


def select_poc(counts: dict[int, int], low_tick: int, high_tick: int, width: int) -> int:
    """Highest count; ties go to the bin nearest the interval midpoint, then the lower bin."""
    best = max(counts.values())
    midpoint = (low_tick + high_tick) / 2

    def key(bin_id: int):
        start = bin_id * width
        end = start + width - 1
        return (abs(((start + end) / 2) - midpoint), bin_id)

    return min((bin_id for bin_id, count in counts.items() if count == best), key=key)


def expand_value_area(counts: dict[int, int], poc: int, ratio: Fraction) -> tuple[int, int, int]:
    """Grow to the heavier side until the ratio. Equal next counts are both taken."""
    total = sum(counts.values())
    lo = hi = poc
    covered = counts[poc]
    bins = sorted(counts)

    def next_bin(origin: int, step: int):
        choices = (
            [bin_id for bin_id in bins if bin_id < origin]
            if step < 0
            else [bin_id for bin_id in bins if bin_id > origin]
        )
        if not choices:
            return None, 0
        chosen = choices[-1] if step < 0 else choices[0]
        return chosen, counts[chosen]

    while total and Fraction(covered, total) < ratio:
        left_id, left_count = next_bin(lo, -1)
        right_id, right_count = next_bin(hi, 1)
        if left_count == 0 and right_count == 0:
            break
        if left_count > right_count:
            lo, covered = left_id, covered + left_count
        elif right_count > left_count:
            hi, covered = right_id, covered + right_count
        else:
            lo, hi = left_id, right_id
            covered += left_count + right_count
    return lo, hi, covered


def _tpo_day(session, tick: Decimal, width: int, ratio: Fraction, max_bins: int) -> dict:
    base = {
        "date": session["date"],
        "status": "cannot_judge",
        "method_label": TPO_LABEL,
        "method_version": TPO_VERSION,
        "not": ["time_at_price", "trade_count", "volume_profile"],
        "reason": session["reason"],
        "poc": None,
        "val": None,
        "vah": None,
    }
    if session["status"] != "complete":
        return base
    counts: dict[int, int] = defaultdict(int)
    low_tick = high_tick = None
    for bar in session["bars"]:
        low = _ticks(bar["low"], tick)
        high = _ticks(bar["high"], tick)
        first, last = low // width, high // width
        if last - first + 1 > max_bins or len(counts) + (last - first + 1) > max_bins:
            base["reason"] = "价格档数超过上限，拒绝计算以避免资源膨胀"
            return base
        for bin_id in range(first, last + 1):
            counts[bin_id] += 1
        low_tick = low if low_tick is None else min(low_tick, low)
        high_tick = high if high_tick is None else max(high_tick, high)
    if not counts or low_tick is None:
        base["reason"] = "没有可覆盖的价格档"
        return base
    poc = select_poc(counts, low_tick, high_tick, width)
    lo, hi, covered = expand_value_area(counts, poc, ratio)
    total = sum(counts.values())
    base.update(
        status="ready",
        reason="仅统计完整交易日；每根K线每个被高低价覆盖的档计一次",
        poc={"lower": _price(poc * width, tick), "upper": _price((poc + 1) * width, tick), "count": counts[poc]},
        val=_price(lo * width, tick),
        vah=_price((hi + 1) * width, tick),
        total_count=total,
        covered_count=covered,
        covered_ratio=float(Fraction(covered, total)),
        bin_width=_price(width, tick),
        interval={"lower": _price(low_tick, tick), "upper": _price(high_tick, tick)},
        first_knowable_time=session["bars"][-1]["datetime"],
        evidence_ids=[bar["id"] for bar in session["bars"]],
        bin_count=len(counts),
    )
    return base


def _window_ok(window) -> bool:
    for left, right in zip(window, window[1:]):
        if left["day"] != right["day"]:
            return False
        if SESSION_ENDS.index(right["end_minute"]) != SESSION_ENDS.index(left["end_minute"]) + 1:
            return False
    return True


def _event(kind, **fields):
    fields.update(kind=kind, algorithm_version=SMC_VERSION)
    fields["id"] = _id(
        {
            key: fields.get(key)
            for key in (
                "kind",
                "event_time",
                "first_knowable_time",
                "price",
                "lower",
                "upper",
                "evidence_ids",
                "direction",
            )
        }
    )
    return fields


def _smc(bars, left: int, right: int) -> dict:
    swings = []
    count = len(bars)
    for index in range(left, count - right):
        window = bars[index - left : index + right + 1]
        if not _window_ok(window):
            continue
        evidence = [bar["id"] for bar in window]
        if all(
            bars[index]["high"] > bars[other]["high"]
            for other in range(index - left, index + right + 1)
            if other != index
        ):
            swings.append(
                _event(
                    "swing_high",
                    side="high",
                    status="confirmed",
                    direction=None,
                    event_time=bars[index]["datetime"],
                    first_knowable_time=bars[index + right]["datetime"],
                    confirmed_time=bars[index + right]["datetime"],
                    invalidated_time=None,
                    price=bars[index]["high"],
                    lower=bars[index]["high"],
                    upper=bars[index]["high"],
                    evidence_ids=evidence,
                    confirm_index=index + right,
                    pivot_index=index,
                    invalidation="极值来自已完成K线，后市只记录是否被收盘突破，不回写该价格",
                )
            )
        if all(
            bars[index]["low"] < bars[other]["low"]
            for other in range(index - left, index + right + 1)
            if other != index
        ):
            swings.append(
                _event(
                    "swing_low",
                    side="low",
                    status="confirmed",
                    direction=None,
                    event_time=bars[index]["datetime"],
                    first_knowable_time=bars[index + right]["datetime"],
                    confirmed_time=bars[index + right]["datetime"],
                    invalidated_time=None,
                    price=bars[index]["low"],
                    lower=bars[index]["low"],
                    upper=bars[index]["low"],
                    evidence_ids=evidence,
                    confirm_index=index + right,
                    pivot_index=index,
                    invalidation="极值来自已完成K线，后市只记录是否被收盘突破，不回写该价格",
                )
            )
    by_confirm = defaultdict(list)
    for swing in swings:
        by_confirm[swing["confirm_index"]].append(swing)
    highs, lows = [], []
    events = list(swings)
    state, owner, previous_owner = "undetermined", None, None
    open_sweep = {}

    def latest(pool):
        usable = [item for item in pool if not item.get("broken")]
        return usable[-1] if usable else None

    def invalidate_older(bar):
        nonlocal state, owner, previous_owner
        for event in events:
            if event["status"] == "invalidated" or event["first_knowable_time"] >= bar["datetime"]:
                continue
            if event["kind"] in {"bos", "choch"} and (
                (event["direction"] == "bullish" and bar["close"] <= event["price"])
                or (event["direction"] == "bearish" and bar["close"] >= event["price"])
            ):
                event["status"] = "invalidated"
                event["invalidated_time"] = bar["datetime"]
                for pool in (highs, lows):
                    for swing in pool:
                        if swing["id"] == event.get("swing_id"):
                            swing["broken"] = False
                if event["id"] == owner:
                    state, owner, previous_owner = event["previous_state"], event["previous_owner"], None
            if event["kind"] == "fvg" and event["status"] in {"confirmed", "partially_filled"}:
                lower, upper = event["lower"], event["upper"]
                if event["direction"] == "bullish" and bar["low"] <= lower:
                    event["status"], event["invalidated_time"] = "invalidated", bar["datetime"]
                elif event["direction"] == "bullish" and bar["low"] < upper:
                    event["status"] = "partially_filled"
                elif event["direction"] == "bearish" and bar["high"] >= upper:
                    event["status"], event["invalidated_time"] = "invalidated", bar["datetime"]
                elif event["direction"] == "bearish" and bar["high"] > lower:
                    event["status"] = "partially_filled"
            if event["kind"] == "sweep" and event["status"] in {"candidate", "confirmed"}:
                level = event["price"]
                held = bar["close"] <= level if event["side"] == "high" else bar["close"] >= level
                if event["status"] == "candidate" and event["first_knowable_time"] < bar["datetime"] and held:
                    event["status"] = "confirmed"
                    event["confirmed_time"] = bar["datetime"]
                elif not held:
                    event["status"] = "invalidated"
                    event["invalidated_time"] = bar["datetime"]
                    open_sweep.pop((event.get("swing_id"), event["side"]), None)

    for index, bar in enumerate(bars):
        invalidate_older(bar)
        high, low = latest(highs), latest(lows)
        broke_high = bool(high and bar["close"] > high["price"])
        broke_low = bool(low and bar["close"] < low["price"])
        if broke_high and broke_low:
            events.append(
                _event(
                    "conflict",
                    status="cannot_judge",
                    direction=None,
                    event_time=bar["datetime"],
                    first_knowable_time=bar["datetime"],
                    confirmed_time=None,
                    invalidated_time=None,
                    price=bar["close"],
                    lower=bar["low"],
                    upper=bar["high"],
                    evidence_ids=[bar["id"]],
                    invalidation="同一根收盘不能同时突破已确认高点和低点",
                    side=None,
                )
            )
        elif broke_high or broke_low:
            swing = high if broke_high else low
            direction = "bullish" if broke_high else "bearish"
            kind = "choch" if state in {"down", "up"} and state != ("up" if broke_high else "down") else "bos"
            if state == "undetermined":
                kind = "bos"
            previous_state, previous = state, owner
            state = "up" if broke_high else "down"
            swing["broken"] = True
            event = _event(
                kind,
                status="confirmed",
                direction=direction,
                side=swing["side"],
                event_time=bar["datetime"],
                first_knowable_time=bar["datetime"],
                confirmed_time=bar["datetime"],
                invalidated_time=None,
                price=swing["price"],
                lower=swing["price"],
                upper=swing["price"],
                evidence_ids=[swing["evidence_ids"][left], bar["id"]],
                swing_id=swing["id"],
                invalidation="后一根完整K线收盘重新回到被突破结构价的另一侧则失效",
                previous_state=previous_state,
                previous_owner=previous,
            )
            owner, previous_owner = event["id"], previous
            events.append(event)
        else:
            for side, swing, pierced, reclaimed in (
                ("high", high, high and bar["high"] > high["price"], high and bar["close"] <= high["price"]),
                ("low", low, low and bar["low"] < low["price"], low and bar["close"] >= low["price"]),
            ):
                if not (swing and pierced and reclaimed):
                    continue
                key = (swing["id"], side)
                if key in open_sweep and open_sweep[key]["status"] != "invalidated":
                    continue
                event = _event(
                    "sweep",
                    status="candidate",
                    direction="bearish" if side == "high" else "bullish",
                    side=side,
                    event_time=bar["datetime"],
                    first_knowable_time=bar["datetime"],
                    confirmed_time=None,
                    invalidated_time=None,
                    price=swing["price"],
                    lower=min(bar["low"], swing["price"]),
                    upper=max(bar["high"], swing["price"]),
                    evidence_ids=[swing["evidence_ids"][left], bar["id"]],
                    swing_id=swing["id"],
                    invalidation="下一根完整K线仍收回结构价才确认；收盘重新刺穿则候选失效",
                )
                open_sweep[key] = event
                events.append(event)
        if index >= 2 and _window_ok(bars[index - 2 : index + 1]):
            first, _middle, third = bars[index - 2 : index + 1]
            if third["low"] > first["high"] or third["high"] < first["low"]:
                bullish = third["low"] > first["high"]
                lower = first["high"] if bullish else third["high"]
                upper = third["low"] if bullish else first["low"]
                events.append(
                    _event(
                        "fvg",
                        status="confirmed",
                        direction="bullish" if bullish else "bearish",
                        event_time=bars[index - 1]["datetime"],
                        first_knowable_time=third["datetime"],
                        confirmed_time=third["datetime"],
                        invalidated_time=None,
                        price=None,
                        lower=lower,
                        upper=upper,
                        evidence_ids=[bar["id"] for bar in bars[index - 2 : index + 1]],
                        invalidation="后市完整K线完全回补缺口则失效；只进入缺口为部分回补",
                    )
                )
        highs.extend(item for item in by_confirm.get(index, []) if item["side"] == "high")
        lows.extend(item for item in by_confirm.get(index, []) if item["side"] == "low")
    public = []
    for event in events:
        item = {
            key: value
            for key, value in event.items()
            if key not in {"confirm_index", "pivot_index", "broken", "previous_state", "previous_owner"}
        }
        public.append(item)
    active = [
        item
        for item in public
        if item["kind"] in {"bos", "choch", "fvg", "sweep"}
        and item["status"] in {"candidate", "confirmed", "partially_filled"}
    ]
    latest = active[-1] if active else None
    return {
        "version": SMC_VERSION,
        "state": state,
        "events": public,
        "latest": latest,
        "order_blocks": {"status": "deferred", "reason": "首版只记录可复核的客观结构，Order Block 后置"},
    }


def _adjacent(previous: str, current: str, calendar_status: str, open_days: dict[str, bool]) -> str:
    if calendar_status != "verified":
        return "unverified"
    if open_days.get(previous) is not True or open_days.get(current) is not True:
        return "unverified"
    cursor = datetime.strptime(previous, "%Y-%m-%d").date() + timedelta(days=1)
    last = datetime.strptime(current, "%Y-%m-%d").date()
    while cursor < last:
        key = cursor.isoformat()
        if key not in open_days:
            return "unverified"
        if open_days[key]:
            return "gap"
        cursor += timedelta(days=1)
    return "adjacent"


def _overnight(previous, current, adjacency: str, adjustment: str) -> dict | None:
    if not previous["bars"] or not current["bars"]:
        return None
    if previous["bars"][-1]["end_minute"] != SESSION_ENDS[-1] or current["bars"][0]["end_minute"] != SESSION_ENDS[0]:
        return None
    old, new = previous["bars"][-1], current["bars"][0]
    gap = new["open"] - old["close"]
    status = "confirmed" if adjacency == "adjacent" and adjustment == "same" and gap != 0 else "cannot_judge"
    if gap == 0 and status == "confirmed":
        return None
    reason = "相邻交易日的隔夜价格差，不是三根K线FVG"
    if adjacency != "adjacent":
        reason = "交易日历不能证明两个会话相邻，或中间仍有未覆盖交易日，不能称为隔夜跳空"
    elif adjustment == "unverified":
        reason = "复权因子未核验，隔夜原始价差不能确认"
    elif adjustment != "same":
        reason = "除权断点两侧的原始价格不能直接比较"
    elif gap == 0:
        return None
    return {
        "kind": "overnight_gap",
        "status": status,
        "direction": "bullish" if gap > 0 else "bearish",
        "event_time": f"{current['date']} 09:30:00",
        "first_knowable_time": new["datetime"],
        "price_change": gap,
        "previous_close": old["close"],
        "next_open": new["open"],
        "evidence_ids": [old["id"], new["id"]],
        "reason": reason,
        "id": _id(["overnight", old["id"], new["id"], status]),
    }


def _factor_relation(left: str, right: str, factors, status: str) -> str:
    if status == "conflict":
        return "different"
    first, second = factors.get(left), factors.get(right)
    if first is None or second is None:
        return "unverified"
    return "same" if first == second else "different"


def _segment_factor(day, factors, status):
    if status == "conflict":
        return None
    if day not in factors:
        return "missing"
    return factors[day]


def _relative_leg(rows, symbol: str, label: str) -> dict:
    usable = []
    seen = {}
    for row in rows or []:
        row_symbol = str(row.get("symbol") or "")
        if symbol and row_symbol and row_symbol.split(".")[0] != symbol.split(".")[0]:
            continue
        day = _day(row.get("trade_date") or row.get("date"))
        close = row.get("close")
        if not day or not _finite(close) or close <= 0 or row.get("price_basis") not in (None, "raw"):
            continue
        if day in seen and seen[day] != float(close):
            return {
                "label": label,
                "status": "cannot_judge",
                "as_of": None,
                "reason": f"{label}同一交易日有冲突收盘，不拼接",
            }
        if day in seen:
            continue
        seen[day] = float(close)
        usable.append({"date": day, "close": float(close), "source": row.get("data_source")})
    usable.sort(key=lambda item: item["date"])
    if len(usable) < 2:
        return {
            "label": label,
            "status": "cannot_judge",
            "as_of": usable[-1]["date"] if usable else None,
            "reason": f"{label}缺少至少两根已核验的不复权日线，不能计算相对变化",
        }
    previous, last = usable[-2], usable[-1]
    return {
        "label": label,
        "status": "ready",
        "as_of": last["date"],
        "previous_date": previous["date"],
        "close": last["close"],
        "previous_close": previous["close"],
        "source": last["source"],
        "price_basis": "raw",
        "close_to_close_change": last["close"] / previous["close"] - 1,
        "reason": "只描述这两根日线收盘的变化，不是收益或交易结果",
    }


def _relative(daily, index_rows, index_symbol: str, symbol: str) -> dict:
    stock = _relative_leg(daily, symbol, "个股")
    index = _relative_leg(index_rows, index_symbol, "指数")
    sector = {
        "label": "板块",
        "status": "cannot_judge",
        "as_of": None,
        "reason": "首版没有已核验的板块指数序列，不把行业名称当成涨跌",
    }
    aligned = (
        stock["status"] == "ready"
        and index["status"] == "ready"
        and stock["as_of"] == index["as_of"]
        and stock["previous_date"] == index["previous_date"]
    )
    return {
        "stock": stock,
        "index": {**index, "symbol": index_symbol},
        "sector": sector,
        "excess_close_to_close": (stock["close_to_close_change"] - index["close_to_close_change"]) if aligned else None,
        "aligned": aligned,
        "attribution": "指数、板块和个股分开记录时点。缺少任一侧，或市场下跌时，都不能把指数涨跌归因成该股异常。",
    }


def _wyckoff(value, as_of: str | None) -> dict:
    if not isinstance(value, dict) or not value.get("phase"):
        return {
            "status": "unavailable",
            "direction": "unknown",
            "as_of": as_of,
            "phase": None,
            "trend": None,
            "reason": "当前研究版本没有已保存的日线威科夫结论，辅助层不另造阶段",
            "mutated": False,
        }
    trend = value.get("trend")
    direction = {"up": "bullish", "down": "bearish"}.get(trend, "unknown")
    return {
        "status": "stored",
        "direction": direction,
        "as_of": as_of,
        "phase": value.get("phase"),
        "trend": trend,
        "assessment_status": value.get("status"),
        "method_version": value.get("method_version"),
        "reason": "沿用已保存的日线威科夫结论作背景，不改写阶段、状态或原文",
        "mutated": False,
    }


def _direction(event) -> str | None:
    if not event or event.get("status") not in {"candidate", "confirmed", "partially_filled"}:
        return None
    if event.get("kind") == "fvg":
        return event.get("direction")
    if event.get("kind") == "sweep":
        return event.get("direction")
    if event.get("kind") in {"bos", "choch"}:
        return event.get("direction")
    return None


def _conflicts(wyckoff, tpo_days, smc) -> list[dict]:
    readings = []
    if wyckoff.get("direction") in {"bullish", "bearish"}:
        readings.append(("日线威科夫背景", wyckoff["direction"]))
    ready = [day for day in tpo_days if day["status"] == "ready"]
    if ready and smc.get("bars_close") is not None:
        last = ready[-1]
        close = smc["bars_close"]
        location = "bullish" if close > last["vah"] else "bearish" if close < last["val"] else None
        if location:
            readings.append(("最近完整交易日TPO位置", location))
    if smc.get("state") == "up":
        readings.append(("30分钟SMC状态", "bullish"))
    elif smc.get("state") == "down":
        readings.append(("30分钟SMC状态", "bearish"))
    elif _direction(smc.get("latest")) in {"bullish", "bearish"}:
        readings.append(("30分钟SMC状态", _direction(smc.get("latest"))))
    found = []
    for index, (left_name, left) in enumerate(readings):
        for right_name, right in readings[index + 1 :]:
            if left != right:
                found.append(
                    {
                        "left": left_name,
                        "right": right_name,
                        "left_reading": left,
                        "right_reading": right,
                        "resolution": "保留冲突，不投票、不合成买卖或仓位",
                    }
                )
    return found


def analyze(
    symbol: str,
    rows,
    now,
    *,
    calendar=None,
    adjustments=None,
    daily=None,
    index_rows=None,
    index_symbol: str = "000300.SH",
    wyckoff=None,
    held_action=None,
    unheld_action=None,
    wyckoff_as_of: str | None = None,
    parameters: dict | None = None,
) -> dict:
    """Replayable auxiliary view. ``now`` must be timezone-aware. Rows after it are ignored."""
    if now.tzinfo is None:
        raise ValueError("aware clock required")
    if len(symbol) != 6 or not symbol.isdigit():
        raise ValueError("仅支持六位A股代码")
    params = _parameters(parameters)
    tick, width, ratio = params["tick_decimal"], params["bin_multiple"], params["ratio"]
    bars, rejected, future, conflicts, truncated = _normalize(
        symbol, rows, now, tick, params["publication_delay_seconds"], params["max_bars"]
    )
    calendar_status, open_days = _calendar(calendar)
    adjustment_status, factors, adjustment_conflicts = _adjustments(adjustments, symbol)
    sessions = _sessions(bars, conflicts, now, calendar_status, open_days, params["publication_delay_seconds"])
    usable = [session for session in sessions if session["status"] in {"complete", "in_progress"} and session["bars"]]
    segments, current, current_factor = [], [], "start"
    breakpoints = []
    for session in usable:
        factor = _segment_factor(session["date"], factors, adjustment_status)
        previous = current[-1]["date"] if current else None
        adjacency = _adjacent(previous, session["date"], calendar_status, open_days) if previous else "same"
        same_factor = previous is None or (factor == current_factor and factor not in {None, "missing"})
        if previous and (adjacency != "adjacent" or not same_factor or adjustment_status == "conflict"):
            segments.append(current)
            current = []
            if (
                previous
                and factor != current_factor
                and factor not in {None, "missing"}
                and current_factor not in {None, "missing", "start"}
            ):
                breakpoints.append(session["date"])
        current.append(session)
        current_factor = factor
    if current:
        segments.append(current)
    if breakpoints:
        adjustment_status = "breakpoint"
    elif adjustment_status == "consistent":
        ordered = [session["date"] for session in usable]
        values = [factors.get(day) for day in ordered]
        if any(value is None for value in values):
            adjustment_status = "unverified"
        elif len(set(values)) > 1:
            adjustment_status = "breakpoint"
            breakpoints = [day for day, value in zip(ordered, values) if value != values[0]]
    tpo_days = [
        _tpo_day(session, tick, width, ratio, params["max_bins"])
        for session in sessions
        if session["status"] != "closed"
    ]
    smc_events, overnight, state = [], [], "undetermined"
    last_close = None
    latest = None
    for segment in segments:
        segment_bars = [bar for session in segment for bar in session["bars"]]
        produced = _smc(segment_bars, params["swing_left"], params["swing_right"]) if segment_bars else None
        if produced:
            smc_events.extend(produced["events"])
            state = produced["state"]
            latest = produced["latest"]
            last_close = segment_bars[-1]["close"]
        else:
            latest = None
        for previous, nxt in zip(segment, segment[1:]):
            gap = _overnight(
                previous,
                nxt,
                _adjacent(previous["date"], nxt["date"], calendar_status, open_days),
                _factor_relation(previous["date"], nxt["date"], factors, adjustment_status),
            )
            if gap:
                overnight.append(gap)
    # Gaps across isolated segments are recorded as cannot-judge, not as FVG.
    usable_days = [session["date"] for session in usable]
    for previous, nxt in zip(usable_days, usable_days[1:]):
        if any(
            previous in {item["date"] for item in segment} and nxt in {item["date"] for item in segment}
            for segment in segments
        ):
            continue
        left = next(session for session in usable if session["date"] == previous)
        right = next(session for session in usable if session["date"] == nxt)
        gap = _overnight(left, right, _adjacent(previous, nxt, calendar_status, open_days), "different")
        if gap:
            overnight.append(gap)
    background = _wyckoff(wyckoff, wyckoff_as_of)
    smc = {
        "version": SMC_VERSION,
        "state": state,
        "events": smc_events,
        "latest": latest if segments else None,
        "bars_close": last_close,
        "order_blocks": {"status": "deferred", "reason": "首版只记录可复核的客观结构，Order Block 后置"},
    }
    issues = []
    if rejected:
        issues.append(f"{rejected}条分钟记录口径、时间或价格档不符合要求，已剔除")
    if future:
        issues.append(f"{future}根尚未过发布等待期的K线未使用")
    if truncated:
        issues.append(f"只保留最近{params['max_bars']}根完整K线，更早的记录未参与")
    if conflicts:
        issues.append("存在同刻价格冲突的交易日已整日隔离")
    sources = sorted({bar["source"] for bar in bars})
    as_of = bars[-1]["datetime"] if bars else None
    gates = [
        {
            "name": "分钟历史",
            "status": "ready" if any(session["status"] == "complete" for session in sessions) else "cannot_judge",
            "reason": "至少有一个完整交易日"
            if any(session["status"] == "complete" for session in sessions)
            else "没有完整的30分钟交易日，不能判断接受区",
        },
        _freshness(bars, now, calendar_status, open_days, params["publication_delay_seconds"]),
        {
            "name": "交易日历",
            "status": "ready" if calendar_status == "verified" else "cannot_judge",
            "reason": "已使用交易日历区分休市、相邻会话和中间空档"
            if calendar_status == "verified"
            else "交易日历缺失或冲突，不能把缺日判断为停牌或假期",
        },
        {"name": "午休", "status": "ready", "reason": "11:30到13:30之间没有30分钟K线是午休，不当作缺失"},
        {
            "name": "停牌或缺失",
            "status": "cannot_judge"
            if any(session["status"] in {"incomplete", "conflict"} for session in sessions)
            or calendar_status != "verified"
            else "ready",
            "reason": "不完整、冲突或日历未证明的交易日已隔离，没有补造K线",
        },
        {
            "name": "来源",
            "status": "cannot_judge" if conflicts or len(sources) != 1 else "ready",
            "reason": "同一时点存在不一致的OHLC，冲突日已整日隔离"
            if conflicts
            else sources[0]
            if len(sources) == 1
            else "没有唯一分钟来源"
            if not sources
            else "多个分钟来源同时存在，冲突日已隔离",
        },
        {
            "name": "复权",
            "status": "ready" if adjustment_status == "consistent" and not breakpoints else "cannot_judge",
            "reason": "窗口内复权因子一致，跨日比较限于同一因子段"
            if adjustment_status == "consistent" and not breakpoints
            else "因子缺失、冲突或除权断点已把两侧隔离，不把原始价格连成同一结构",
        },
    ]
    limitations = [
        TPO_LABEL + "：不是价格停留时长、成交笔数或 Volume Profile。",
        "每根完整K线只把高低价覆盖到的每个价格档计入一次，档宽是最小报价单位的整数倍。",
        "局部高低点要等右侧确认K线收完才首次可知；事件发生时间不等于确认时间，也不回画。",
        "BOS 是收盘突破已确认结构价；CHoCH 是已有趋势后的第一笔反向收盘突破。",
        "FVG 只来自同一交易日连续三根完整K线；隔夜价格差单独列出，午休不是缺失。",
        "Sweep 先是候选：刺穿已确认高低点后当根收回。下一根仍收回才确认。",
        "Order Block 未计算。不生成仓位、目标价或交易指令，不调用付费模型。",
        "TPO、SMC 与已保存日线威科夫共享该股研究上下文和这里列出的价格身份，不能当作三张独立选票。",
        "未验证全市场数据覆盖；证据不足时结果是不可判断。",
    ]
    shared = {
        "symbol": symbol,
        "frequency": "30min",
        "price_basis": "raw",
        "sources": sources,
        "as_of": as_of,
        "bar_ids": [bar["id"] for bar in bars],
        "identity": _id([symbol, as_of, [bar["id"] for bar in bars]]),
        "note": "30分钟结构使用这一组不复权K线。日线威科夫仍是原研究版本的日线结论，只作背景对照。",
    }
    preserved = {
        "held_action": held_action,
        "unheld_action": unheld_action,
        "wyckoff_phase": background.get("phase"),
        "wyckoff_status": background.get("assessment_status"),
        "report_mutated": False,
        "decision_effect": "none",
    }
    ready_parts = any(day["status"] == "ready" for day in tpo_days) or any(
        event["kind"] in {"bos", "choch", "fvg", "swing_high", "swing_low"} for event in smc_events
    )
    freshness = next(gate for gate in gates if gate["name"] == "新鲜度")
    blocking = (
        calendar_status != "verified"
        or adjustment_status != "consistent"
        or bool(breakpoints)
        or freshness["status"] != "ready"
        or any(session["status"] in {"incomplete", "conflict"} for session in sessions)
    )
    status = (
        "cannot_judge"
        if not ready_parts
        else "partial"
        if blocking or any(day["status"] == "cannot_judge" for day in tpo_days)
        else "ready"
    )
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "symbol": symbol,
        "status": status,
        "as_of": as_of,
        "checked_at": now.isoformat(),
        "frequency": "30min",
        "price_basis": "raw",
        "currency": "CNY",
        "source": sources[0] if len(sources) == 1 else None,
        "trade_authorized": False,
        "decision_effect": "none",
        "vote": False,
        "performance_claim": False,
        "parameters": {key: value for key, value in params.items() if key not in {"tick_decimal", "ratio"}},
        "gates": gates,
        "sessions": [
            {key: session[key] for key in ("date", "status", "missing", "reason")} | {"bars": len(session["bars"])}
            for session in sessions
        ],
        "tpo": {"label": TPO_LABEL, "version": TPO_VERSION, "days": tpo_days},
        "smc": {key: value for key, value in smc.items() if key != "bars_close"},
        "overnight_gaps": overnight,
        "relative": _relative(daily, index_rows, index_symbol, symbol),
        "wyckoff_background": background,
        "conflicts": _conflicts(background, tpo_days, smc),
        "preserved": preserved,
        "shared_price": shared,
        "limitations": limitations,
        "issues": issues,
        "adjustment": {"status": adjustment_status, "breakpoints": breakpoints, "conflicts": adjustment_conflicts},
        "calendar_status": calendar_status,
    }
