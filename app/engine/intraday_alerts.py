"""Dated price events for existing references, not new price targets or orders."""
from datetime import datetime

from app.engine.intraday_research import CN
from app.engine.research_tracking import digest


def price_alerts(snapshot, levels, *, known_at):
    if snapshot.get("status") != "ready" or not known_at:
        return []
    known = datetime.fromisoformat(str(known_at).replace("Z", "+00:00"))
    if known.tzinfo is None:
        return []
    # A report published after a bar began was not available for that interval.
    # Do not backdate a new after-close target to the morning's trades.
    from datetime import timedelta
    bars = [b for b in snapshot.get("bars", []) if
            datetime.fromisoformat(b["datetime"]).replace(tzinfo=CN)-timedelta(minutes=30) >= known]
    if not bars:
        return []
    result = []
    for level in levels:
        if level.get("historical_claim_conflict") or level.get("price_basis") != "raw" or level.get("expired") or (level.get("valid_until") and level["valid_until"] < snapshot["date"]):
            continue
        low, high = level.get("lower"), level.get("upper")
        if type(low) not in (int, float) or type(high) not in (int, float) or not 0 < low <= high:
            continue
        events = []
        hit = next((b for b in bars if b["low"] <= high and b["high"] >= low), None)
        if hit:
            events.append(("touch", hit, "原点位已触及", "价格进入原参考区间；请结合原减仓／入场条件核验，触价不代表必须全部买卖。"))
        if level.get("kind") in {"target", "resistance"}:
            pair = next(((a, b) for a, b in zip(bars, bars[1:]) if a["close"] > high and b["close"] > high), None)
            if pair:
                events.append(("above_two_closes", pair[1], "原阻力上方连续两根30分钟收盘", "原计划可能需要局部复核；这只是价格突破候选，未核验全部量能／新闻条件，不自动上调目标或取消止盈。"))
        if level.get("kind") in {"support", "exit"}:
            broken = next((b for b in bars if b["close"] < low), None)
            if broken:
                events.append(("below_close", broken, "原保护参考下方收盘", "请优先核验原退出条件；不会下移止损，也未确认账户可卖数量。"))
        for kind, bar, title, explanation in events:
            result.append({"id": digest([snapshot["date"], level, known_at, kind])[:24],
                "kind": kind, "title": title, "label": level["label"], "lower": low, "upper": high,
                "evidence_at": bar["datetime"], "known_at": str(known_at),
                "source_date": level.get("source_date") or level.get("source_research_date"),
                "message": explanation, "condition": level.get("condition", ""),
                "evidence_ids": [bar["id"]], "trade_authorized": False})
    return result
