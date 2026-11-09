"""Exchange-calendar analysis context shared by every analyst and debate role."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from contextvars import ContextVar

ACTIVE_ANALYSIS_CONTEXT: ContextVar[dict | None] = ContextVar("analysis_time_context", default=None)
ACTIVE_READ_TRACE: ContextVar[list | None] = ContextVar("analysis_read_trace", default=None)


def record_read_trace(item: dict) -> None:
    """Append only to this datasource invocation's trace, never another role's."""
    from copy import deepcopy
    trace = ACTIVE_READ_TRACE.get()
    if trace is not None and len(trace) < 60:
        trace.append(deepcopy(item))
    elif trace is not None and len(trace) == 60:
        trace.append({"domain": "__trace_limit__", "quality": {"status": "partial"}})

from app.data.core.market import get_market_timezone


def _calendar_days(records: list[dict]) -> dict[str, bool | None]:
    """An invalid or contradictory duplicate is unknown, never an open day."""
    days = {}
    for row in records:
        day = str(row.get("cal_date", ""))[:10]
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        value = str(row.get("is_open", "")).lower()
        opened = value in {"true", "1"} if value in {"true", "1", "false", "0"} else None
        if day not in days:
            days[day] = opened
        elif days[day] != opened:
            days[day] = None
    return days


def verified_future_sessions(records: list[dict], anchor: date, count: int = 7) -> list[str]:
    """Only contiguous exchange-calendar coverage proves the next sessions."""
    days = _calendar_days(records)
    sessions = []
    for offset in range(1, 31):
        day = (anchor + timedelta(days=offset)).isoformat()
        if days.get(day) is None:
            break
        if days[day]:
            sessions.append(day)
        if len(sessions) == count:
            break
    return sessions


async def build_context(di, market: str, as_of: str, sessions: int = 5) -> dict:
    tz = get_market_timezone(market)
    now = datetime.now(tz)
    requested = date.fromisoformat(as_of[:10])
    anchor = min(requested, now.date())
    start = anchor - timedelta(days=max(60, sessions * 4))
    calendar_errors = []
    rows = []
    try:
        result = await di.read(market, "trade_calendar", start_date=start.isoformat(),
                               end_date=(anchor + timedelta(days=30)).isoformat())
        rows = result.get("data") or []
    except Exception as exc:
        calendar_errors.append(f"calendar_cache_read_failed:{type(exc).__name__}")
    # Empty or holey caches must obtain the calendar themselves; never replace
    # an exchange calendar with weekdays. This refresh is not market backfill.
    required_calendar_days = {(anchor - timedelta(days=offset)).isoformat() for offset in range(30)}
    def covered_window(records):
        observed = {day for day, status in _calendar_days(records).items() if status is not None}
        opened_past = sorted(day for day, status in _calendar_days(records).items()
                             if status is True and day <= anchor.isoformat())
        if len(opened_past) < sessions or not required_calendar_days.issubset(observed):
            return False
        earliest = date.fromisoformat(opened_past[-sessions])
        return all((earliest + timedelta(days=offset)).isoformat() in observed
                   for offset in range((anchor - earliest).days + 1))
    if not covered_window(rows) or len(verified_future_sessions(rows, anchor)) < 7:
        try:
            await di.refresh(market, "__calendar__", domains=["trade_calendar"], timeout=10,
                             start_date=start.isoformat(), end_date=(anchor + timedelta(days=30)).isoformat())
            result = await di.read(market, "trade_calendar", start_date=start.isoformat(),
                                   end_date=(anchor + timedelta(days=30)).isoformat())
            rows = result.get("data") or rows
        except Exception as exc:
            calendar_errors.append(f"calendar_completion_failed:{type(exc).__name__}")
    opened = sorted(day for day, status in _calendar_days(rows).items() if status is True)
    past = [day for day in opened if day <= anchor.isoformat()]
    future = verified_future_sessions(rows, anchor)
    last = past[-1] if past else None
    selected = past[-max(1, sessions):]
    closing = "15:00:00" if market == "CN" else "16:00:00"
    completed = [day for day in past if day < now.date().isoformat()
                 or (day == now.date().isoformat() and now.strftime("%H:%M:%S") >= closing)]
    cutoff = f"{last} {closing}" if last else None
    if cutoff and last == now.date().isoformat():
        cutoff = min(cutoff, now.strftime("%Y-%m-%d %H:%M:%S"))
    verified = covered_window(rows) and bool(last) and len(selected) == sessions
    return {
        "analysis_date": requested.isoformat(), "as_of": anchor.isoformat(),
        "market": market, "timezone": str(tz),
        "time_semantics": "analysis_date_observation_cutoff_not_request_start_freeze",
        "observation_cutoff_local": cutoff,
        "retrieval_time_semantics": "updated_at/fetched_at/read_at are cache or acquisition times, not market observation or publication times",
        "calendar_status": "success" if verified else ("partial" if rows else "failed"),
        "last_actual_trade_date": last,
        "last_completed_trade_date": completed[-1] if completed and verified else None,
        "recent_trade_dates": selected,
        "volume_reference_trade_dates": past[-6:],
        "next_trade_dates": future,
        "future_calendar_status": "success" if len(future) == 7 else "partial",
        "intraday_start": f"{selected[0]} 09:30:00" if selected else None,
        "intraday_end": cutoff,
        "limitations": ([] if verified else ["交易日历未完整覆盖，不能把自然日当作实际交易日", *calendar_errors])
                        + ([] if len(future) == 7 else ["未来七个交易日的连续日历证据不足，仅列已验证的后续交易日"]),
        "capability_limits": ["免费报价不能观察隐形杠杆或真实主力身份", "缺少官方受限资金、债务到期、信用或流动性材料时保持未知", "未有同策略可复核回测时不得给出胜率", "短线资金评分的必要分项缺失时总分不可计算，不能用默认中性补齐"],
    }


def prompt_context(context: dict) -> str:
    import json
    return ("\n<analysis_time_context>" + json.dumps({key: value for key, value in context.items() if not key.endswith("_snapshots")}, ensure_ascii=False)
            + "</analysis_time_context>\n所有角色使用该时点；休市期间不能声称当日盘中数据。"
              "as_of 是统一分析日期，observation_cutoff_local 是市场观察上限；当前时间仅作运行说明，不是数据请求开始即冻结的证据截止秒。"
              "updated_at/fetched_at/read_at 是缓存或抓取时间，不是行情发生时间、新闻发布时间或财报公告时间；不得仅因取数晚于任务/角色启动几秒就剔除已满足分析日与实际发布日期的数据。"
              "本流程不是逐秒历史回测冻结快照：历史可得性未验证时须明确披露，不能用抓取时间替代发布证据；盘后已验证收盘价可作当日价格参考，但不是下一交易日实时可成交价。"
              "不存在的分时、完整胜率、隐藏杠杆、受限资金等不得推断为零或编造评分。"
              "role_data_checks 是按配置的数据前置检查而非完整能力保证；failed/not_fetched/unknown_capability/"
              "partial/returned_unverified 仅限制其中未验证字段所依赖的结论，不能抹去其他已验证字段；可给有来源、条件及失效界限的研究价格方案，非当前订单。未验证证据不得用作当前执行授权、完整资金评分或胜率。"
              "price_reference 是独立核验的报价快照及历史收盘参考；observed_at 是供应商快照时间，不等于成交发生时间，不能用本地取数时间替代。"
              "盘后供应商仍可能更新快照；可引用为该日盘后报价参考，不因其时间晚于15:00就剔除，但不得声称已经核验官方收盘或盘后成交。"
              "报价可在任务启动后取得，但不得超出分析日；不得混入另一来源/复权口径的历史日线计算收益或均线。"
              "报价可用不等于已获买入授权，报价缺失也不自动意味着总仓位0%；分别说明价格证据、交易条件与风险判断。"
              "复用报告的 not_rechecked_reused_report 不表示本轮重新验证通过；新报价不改变复用报告原来的证据时点。")
