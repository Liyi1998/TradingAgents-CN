"""Price-only daily Chan structures; pinned CZSC core, no model or data access.

Confirmation means the algorithm's state at that closed session. Later bars or
vendor corrections can revise it. Replay records when each structure became
available instead of assigning its turning-point date as its confirmation date.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from functools import lru_cache
import hashlib
from importlib import metadata, util
import json
import math
from pathlib import Path

ENGINE = "czsc-1.0.2/chan-daily-v1"
PARAMETERS = {"min_bi_len": 6, "max_bi_num": 200}
MAX_BARS = 400


class ChanInputError(ValueError):
    pass


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@lru_cache(maxsize=1)
def native_core():
    """Load the pinned extension without CZSC's eager backtest/chart imports.

    The private native ABI is deliberately version-pinned and covered by Mac
    and Linux import/replay acceptance. No Python package stub is injected.
    Only the installed distribution's own extension may be loaded.
    """
    package = metadata.distribution("czsc")
    if package.version != "1.0.2":
        raise ImportError("缠论核心版本不匹配，需要 CZSC 1.0.2")
    candidates = [
        f for f in package.files or [] if str(f).startswith("czsc/_native") and str(f).endswith((".so", ".pyd"))
    ]
    if len(candidates) != 1:
        raise ImportError("未找到当前平台的缠论计算核心")
    path = Path(package.locate_file(candidates[0])).resolve()
    spec = util.spec_from_file_location("_tradingagents_chan._native", path)
    if spec is None or spec.loader is None:
        raise ImportError("无法加载缠论计算核心")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _day(value):
    if not isinstance(value, str) or len(value) != 10 or date.fromisoformat(value).isoformat() != value:
        raise ChanInputError("日线日期无效")
    return value


def prepare(symbol, bars, context, as_of):
    """Require explicit basis/calendar; never silently bridge a missing session."""
    _day(as_of)
    if context.get("price_basis") != "raw" or context.get("currency") != "CNY":
        raise ChanInputError("缠论图层需要已核验的不复权人民币日线")
    if not context.get("calendar_complete"):
        raise ChanInputError("交易日历不完整，暂不跨缺失日期连接缠论结构")
    if not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise ChanInputError("股票代码无效")
    if not bars or len(bars) > MAX_BARS:
        raise ChanInputError("缠论日线窗口为空或超过计算上限")
    clean = []
    for bar in bars:
        day = _day(bar.get("date"))
        if day > as_of or bar.get("status"):
            raise ChanInputError("未收盘或未来行情不能进入已确认结构")
        if clean and day <= clean[-1]["date"]:
            raise ChanInputError("日线日期重复或顺序错误")
        if bar.get("price_basis", "raw") != "raw" or bar.get("currency", "CNY") != "CNY":
            raise ChanInputError("日线混合了不同复权或货币口径")
        if not bar.get("data_source"):
            raise ChanInputError("日线缺少来源")
        prices = {k: bar.get(k) for k in ("open", "close", "high", "low")}
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in prices.values()):
            raise ChanInputError("日线价格缺失或无效")
        if (
            not prices["low"]
            <= min(prices["open"], prices["close"])
            <= max(prices["open"], prices["close"])
            <= prices["high"]
        ):
            raise ChanInputError("日线高低价关系无效")
        clean.append({"date": day, **prices, "data_source": bar["data_source"]})
    if len({b["data_source"] for b in clean}) != 1:
        raise ChanInputError("完整日线来源混合，暂不拼接缠论结构")
    expected = [_day(d) for d in context.get("expected_sessions", []) if clean[0]["date"] <= d <= as_of]
    if expected != sorted(set(expected)) or not expected or any(b["date"] not in expected for b in clean):
        raise ChanInputError("日线日期与交易日历不一致")
    present = {b["date"] for b in clean}
    gaps = [d for d in expected if d not in present and d < clean[-1]["date"]]
    limitations = [
        "仅计算日线分型、笔与笔中枢；不提供线段中枢或一二三类买卖点。",
        "不复权结构可能受除权除息影响；确认是截至该日的算法状态，后续可修订。",
    ]
    if gaps:
        clean = [b for b in clean if b["date"] > gaps[-1]]
        limitations.append(f"窗口缺少{len(gaps)}个交易日（最后缺{gaps[-1]}），仅连接其后的连续日线。")
    if len(clean) < 20:
        raise ChanInputError("连续日线不足20根，暂不绘制缠论结构")
    stale = clean[-1]["date"] != as_of
    if stale:
        limitations.append(f"结构实际截至{clean[-1]['date']}，尚未取得{as_of}的完整日线。")
    return clean, limitations, "stale" if stale else "partial" if gaps else "ready"


def _date(value):
    return value.strftime("%Y-%m-%d")


def _key(kind, *parts):
    return kind + ":" + fingerprint(parts)[:24]


def _fx(fx):
    day, price = _date(fx.dt), float(fx.fx)
    mark = "top" if str(fx.mark) == "顶分型" else "bottom"
    return {"id": _key("fx", day, price, mark), "date": day, "price": price, "kind": mark}


def _stroke(bi):
    start, end = _date(bi.fx_a.dt), _date(bi.fx_b.dt)
    a, b = float(bi.fx_a.fx), float(bi.fx_b.fx)
    return {
        "id": _key("bi", start, end, a, b),
        "start": start,
        "end": end,
        "start_price": a,
        "end_price": b,
        "direction": "up" if b > a else "down",
    }


def _geometry(value):
    return fingerprint({k: v for k, v in value.items() if k not in {"confirmed_at", "updated_at_session", "status"}})


def parts(core):
    finished = {_stroke(b)["id"] for b in core.finished_bis}
    strokes = [
        {**_stroke(b), "status": "confirmed" if _stroke(b)["id"] in finished else "candidate"} for b in core.bi_list
    ]
    fxs = {x["id"]: x for x in [_fx(f) for f in [*(b.fx_a for b in core.bi_list), *core.fx_list]]}
    fractals = [{**x, "status": "confirmed"} for x in sorted(fxs.values(), key=lambda x: x["date"])]
    zones = []
    for zs in core.zs_list:
        if len(zs.bis) < 3 or not zs.is_valid() or zs.zd >= zs.zg:
            continue
        seed = [_stroke(b)["id"] for b in zs.bis[:3]]
        zones.append(
            {
                "id": _key("zs", seed, float(zs.zd), float(zs.zg)),
                "start": _date(zs.sdt),
                "end": _date(zs.edt),
                "lower": float(zs.zd),
                "upper": float(zs.zg),
                "stroke_count": len(zs.bis),
                "status": "confirmed" if all(_stroke(b)["id"] in finished for b in zs.bis) else "candidate",
            }
        )
    # A developing stroke is a price path, not a confirmed opposite fractal.
    if core.bi_list and core.bars_ubi:
        last = core.bi_list[-1].fx_b
        start, start_price = _date(last.dt), float(last.fx)
        raw = (
            [r for b in core.bars_ubi for r in b.raw_bars]
            if hasattr(core.bars_ubi[0], "raw_bars")
            else [r for b in core.bars_ubi for r in b.elements]
        )
        following = [r for r in raw if _date(r.dt) > start]
        if following:
            rising = str(last.mark) == "底分型"
            end_bar = max(following, key=lambda r: r.high) if rising else min(following, key=lambda r: r.low)
            price = float(end_bar.high if rising else end_bar.low)
            if (price > start_price) == rising and price != start_price:
                strokes.append(
                    {
                        "id": _key("ubi", start, _date(end_bar.dt), start_price, price),
                        "start": start,
                        "end": _date(end_bar.dt),
                        "start_price": start_price,
                        "end_price": price,
                        "direction": "up" if rising else "down",
                        "status": "candidate",
                        "unfinished": True,
                    }
                )
    return {"fractals": fractals, "strokes": strokes, "zones": zones}


def _bar(native, symbol, value, index):
    # Volume/amount are deliberately unused: this layer makes no volume,
    # divergence, strength or trading-signal claims. Only OHLC forms structures.
    return native.RawBar(
        symbol,
        datetime.fromisoformat(value["date"]),
        native.Freq.D,
        open=value["open"],
        close=value["close"],
        high=value["high"],
        low=value["low"],
        vol=0.0,
        amount=0.0,
        id=index,
    )


def replay(symbol, bars):
    native = native_core()
    core = native.CZSC([_bar(native, symbol, bars[0], 0)], **PARAMETERS)
    seen, revised = {}, {}
    output = {"fractals": [], "strokes": [], "zones": []}
    for index, bar in enumerate(bars):
        if index:
            core.update(_bar(native, symbol, bar, index))
        output = parts(core)
        next_seen, next_revised = {}, {}
        for values in output.values():
            for item in values:
                identity, geometry = item["id"], _geometry(item)
                if item["status"] == "confirmed":
                    next_seen[identity] = seen.get(identity, bar["date"])
                    item["confirmed_at"] = next_seen[identity]
                else:
                    item["confirmed_at"] = None
                state_key = geometry + ":" + item["status"]
                next_revised[state_key] = revised.get(state_key, bar["date"])
                item["updated_at_session"] = next_revised[state_key]
        seen, revised = next_seen, next_revised
    return output


def calculate(symbol, bars, context, as_of):
    clean, limitations, status = prepare(symbol, bars, context, as_of)
    return {
        "engine": ENGINE,
        "parameters": PARAMETERS.copy(),
        "symbol": symbol,
        "as_of": clean[-1]["date"],
        "requested_as_of": as_of,
        "window_start": clean[0]["date"],
        "price_basis": "raw",
        "currency": "CNY",
        "data_source": clean[0]["data_source"],
        "status": status,
        "limitations": limitations,
        "bar_count": len(clean),
        "volume_metrics": False,
        **replay(symbol, clean),
    }


def preview(symbol, bars, context, as_of, live, closed):
    """An unclosed candle only changes an ephemeral candidate overlay."""
    if not live or live.get("stale") or live.get("status") not in {"intraday", "awaiting_daily"}:
        return None
    if live.get("price_basis") != "raw" or live.get("currency") != "CNY" or not live.get("observed_at"):
        return None
    clean, _, _ = prepare(symbol, bars, context, as_of)
    if clean[-1]["date"] != as_of or _day(live["date"]) <= as_of:
        return None
    next_session = context.get("next_session")
    if live["date"] != next_session:
        return None
    # Validate the live OHLC separately; keep its actual independent source.
    live_prices = {k: live.get(k) for k in ("open", "close", "high", "low")}
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in live_prices.values()):
        return None
    if not live["low"] <= min(live["open"], live["close"]) <= max(live["open"], live["close"]) <= live["high"]:
        return None
    fresh = replay(symbol, clean + [{"date": live["date"], **live_prices}])
    confirmed = {
        _geometry(x) for key in ("fractals", "strokes", "zones") for x in closed[key] if x["status"] == "confirmed"
    }
    output = {
        key: [
            {**deepcopy(x), "status": "candidate", "confirmed_at": None, "updated_at_session": live["date"]}
            for x in values
            if _geometry(x) not in confirmed
        ]
        for key, values in fresh.items()
    }
    return {
        **output,
        "as_of": live["date"],
        "observed_at": live["observed_at"],
        "data_source": live["data_source"],
        "status": "candidate",
    }
