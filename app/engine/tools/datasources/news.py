"""
新闻工具 - 股票新闻数据获取
"""

import logging
import re
import json
from datetime import datetime, timedelta

from app.utils.time_utils import now_utc
from app.engine.tools.common.tool_result import (
    success_result,
    no_data_result,
    error_result,
    format_tool_result,
    ErrorCodes,
)
from app.core.async_utils import run_async

logger = logging.getLogger(__name__)


def _get_market_for_code(symbol: str) -> str:
    """根据股票代码判断市场。"""
    try:
        from app.utils.stock_utils import StockUtils

        market_info = StockUtils.get_market_info(symbol)
        if market_info.get("is_hk"):
            return "HK"
        if market_info.get("is_us"):
            return "US"
    except Exception as e:
        logger.debug(f"市场类型检测失败，默认CN: {e}")
        pass
    return "CN"


def _clean_symbol(symbol: str) -> str:
    from app.data.core.symbols import normalize_symbol
    return normalize_symbol(symbol, _get_market_for_code(symbol))


def _fetch_news_snapshot(symbol: str, max_results: int = 15, end_date: str = None) -> dict:
    from contextlib import nullcontext
    from app.engine.data_preflight import ACTIVE_PREFLIGHT
    session = ACTIVE_PREFLIGHT.get()
    # News and sentiment preloads run in worker threads. Freeze one actual
    # population before either renders it, instead of racing two refreshes.
    with session.news_lock if session is not None else nullcontext():
        return _load_news_snapshot(symbol, max_results, end_date)


def _news_publication_metadata(items: list, end_date: str) -> dict:
    """Describe observed publication dates without treating a refresh as complete coverage."""
    cutoff = datetime.fromisoformat(end_date).date()
    dates, invalid, after_cutoff = [], 0, 0
    for item in items:
        try:
            published = datetime.fromisoformat(str(item.get("publish_time") or "")[:10]).date()
        except (TypeError, ValueError):
            invalid += 1
            continue
        if published > cutoff:
            after_cutoff += 1
            continue
        dates.append(published)
    latest = max(dates) if dates else None
    return {
        "latest_publication_date": latest.isoformat() if latest else None,
        "publication_lag_calendar_days": (cutoff - latest).days if latest else None,
        "invalid_publication_time_count": invalid,
        "after_cutoff_publication_count": after_cutoff,
        "current_day_news_coverage": "unverified",
        "completeness": "observed_sample_not_all_news",
        "freshness_limit": "缓存刷新成功不证明今日或整个窗口新闻已收齐；以每条发布时间为准，未取得不等于没有事件。",
    }


def _load_news_snapshot(symbol: str, max_results: int = 15, end_date: str = None) -> dict:
    from app.engine.tools.common.data_access import read_with_refresh
    from app.data.core.interface import DataInterface

    from copy import deepcopy
    from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT
    market, symbol = _get_market_for_code(symbol), _clean_symbol(symbol)
    context = ACTIVE_ANALYSIS_CONTEXT.get()
    end_date = end_date or (context or {}).get("as_of") or now_utc().strftime("%Y-%m-%d")
    if context and context.get("as_of"):
        end_date = min(end_date, context["as_of"])
    lookback, read_limit, max_results = news_research_scope(context, max_results)
    start_date = (datetime.fromisoformat(end_date) - timedelta(days=lookback)).strftime("%Y-%m-%d")
    cache_key = f"{market}:{symbol}:{start_date}:{end_date}:{max_results}"
    cache = context.setdefault("news_snapshots", {}) if context is not None else {}
    if cache_key in cache:
        from app.data.core.analysis_context import record_read_trace
        for read in cache[cache_key].get("read_provenance", []):
            record_read_trace(read)
        return deepcopy(cache[cache_key])
    di = DataInterface.get_instance()
    basic = run_async(di.read(market, "basic_info", symbol=symbol)).get("data") or []
    if isinstance(basic, dict):
        basic = [basic]
    name = basic[0].get("name", "") if basic else ""
    result = read_with_refresh(market, "news", symbol=symbol, start_date=start_date,
                               end_date=end_date, filters={"limit": read_limit}) or {}
    items_by_title = {}
    from app.engine.tools.datasources.news_classification import classify_news_item, has_body_text
    for row in result.get("data") or []:
        text = f"{row.get('title', '')} {row.get('content', '')}"
        if market == "CN" and row.get("source_kind") != "official" and not (
            (name and name in text) or re.search(rf"(?<!\d){re.escape(symbol)}(?!\d)", text)
        ):
            continue
        title_key = re.sub(r"\W+", "", str(row.get("title") or "")).lower()
        if not title_key:
            continue
        previous = items_by_title.get(title_key)
        if previous and (has_body_text(previous), len(str(previous.get("content") or ""))) >= (has_body_text(row), len(str(row.get("content") or ""))):
            continue
        item = dict(row)
        item["source"] = row.get("source") or row.get("data_source") or "unknown"
        item.update(classify_news_item(row, target_symbol=symbol, target_name=name))
        items_by_title[title_key] = item
    items = sorted(items_by_title.values(), key=lambda row: str(row.get("publish_time") or ""), reverse=True)[:max_results]
    from app.engine.tools.datasources.news_classification import sentiment_coverage
    snapshot = {"items": items, "quality": result.get("quality") or {},
            "analysis_date": end_date, "range_start": start_date,
            "population": f"same_{lookback}_calendar_days_deduplicated_news",
            "read_provenance": [{"market": market, "domain": "news", "symbol": symbol,
                                 "start_date": start_date, "end_date": end_date, "filters": {"limit": read_limit},
                                 "quality": result.get("quality") or {}}] if result.get("domain") == "news" and result.get("symbol") == symbol else [],
            "classification_coverage": sentiment_coverage(items)}
    snapshot.update(_news_publication_metadata(items, end_date))
    if lookback != 30:
        snapshot["research_sampling"] = {
            "requested_calendar_days": lookback, "read_limit": read_limit,
            "deduplicated_candidates": len(items_by_title), "returned_sample_limit": max_results,
            "sample_truncated": len(items_by_title) > len(items),
            "limitation": "按时间倒序的有界样本，不保证覆盖整个研究期；年度重大事项须与官方公告交叉核验。",
        }
    cache[cache_key] = deepcopy(snapshot)
    if context is not None:
        context.setdefault("news_classification_coverage", {})[cache_key] = snapshot["classification_coverage"]
    return snapshot


def news_research_scope(context: dict | None, max_results: int = 15) -> tuple[int, int, int]:
    """Bounded shared news/sentiment population; a query window is not coverage.

    Legacy short queries remain byte-for-byte identical. Longer periods inspect
    more dated evidence, without an unbounded historical crawl or market backfill.
    """
    from app.core.analysis_horizon import normalize_analysis_horizon
    horizon = normalize_analysis_horizon((context or {}).get("analysis_horizon"))
    if horizon == "short":
        return 30, 100, max_results
    days, limit, default_sample = (90, 200, 30) if horizon == "medium" else (365, 400, 60)
    return days, limit, min(80, default_sample if max_results == 15 else max(1, max_results))


def _fetch_news_data(symbol: str, max_results: int = 15, end_date: str = None) -> list:
    return _fetch_news_snapshot(symbol, max_results, end_date)["items"]


def get_stock_official_notices(symbol: str, end_date: str) -> str:
    """Read official records via the facade; refresh free providers once if absent."""
    from app.data.core.interface import DataInterface

    if _get_market_for_code(symbol) != "CN":
        return format_tool_result(no_data_result(message="此通道目前仅覆盖A股官方披露"))
    symbol = _clean_symbol(symbol)
    from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT
    context = ACTIVE_ANALYSIS_CONTEXT.get()
    if context and context.get("as_of"):
        end_date = min(end_date, context["as_of"])
    start = (datetime.fromisoformat(end_date) - timedelta(days=420)).strftime("%Y-%m-%d")
    di = DataInterface.get_instance()
    read_args = {"symbol": symbol, "start_date": start, "end_date": end_date,
                 "filters": {"category": "official_announcement", "limit": 200}}
    result = run_async(di.read("CN", "news", **read_args))
    records = result.get("data") or []
    refresh_status = "cached"
    # Expired cache triggers bounded free-source refresh too, not just empty cache.
    filing_kinds = {(row.get("filing_metadata") or {}).get("report_kind") for row in records}
    if not records or result.get("freshness") not in {"fresh", "FRESH"} or not {"annual", "semiannual"}.issubset(filing_kinds):
        refreshed = run_async(di.refresh("CN", symbol, domains=["news"], force=False, timeout=35,
            start_date=start, end_date=end_date,
            coverage_filters={"include_filings": True, "category": "official_announcement"}))
        detail = refreshed.domains.get("news")
        refresh_status = detail.status if detail else "unknown"
        records = run_async(di.read("CN", "news", **read_args)).get("data") or []
    cutoff = (datetime.fromisoformat(end_date) - timedelta(days=90)).strftime("%Y-%m-%d")
    filings = [row for row in records if row.get("filing_metadata")]
    recent = [row for row in records if not row.get("filing_metadata") and str(row.get("publish_time") or "")[:10] >= cutoff][:10]
    records = filings[:6] + recent
    payload = {"analysis_date": end_date, "refresh_status": refresh_status, "count": len(records),
               "filing_lookback_days": 420, "recent_notice_lookback_days": 90,
               "records": [{key: item.get(key) for key in ("symbol", "title", "publish_time", "url", "data_source", "source", "source_kind", "content_status", "content", "filing_metadata", "pdf_metadata", "discovery_metadata")}
                           for item in records],
               "limitations": "正文为有页号的定向附注摘录，最多扫描200页/16000字；扫描覆盖见content_status，不是全文。未取得记录不等于没有公告或重大事项。"}
    return format_tool_result(success_result(json.dumps(payload, ensure_ascii=False, default=str)))


def _format_news_list(news_list: list, source_label: str = None) -> str:
    """格式化新闻列表为 Markdown"""
    if not news_list:
        return "暂无新闻数据"

    report = f"# 最新新闻 {'(' + source_label + ')' if source_label else ''}\n\n"
    report += f"查询时间: {now_utc().strftime('%Y-%m-%d %H:%M:%S')}\n"
    report += f"新闻数量: {len(news_list)} 条\n\n"

    for i, news in enumerate(news_list, 1):
        title = news.get("title", "无标题")
        content = news.get("content", "")
        source = news.get("source", "未知来源")
        pub_time = news.get("publish_time", now_utc())
        if isinstance(pub_time, datetime):
            pub_time_str = pub_time.strftime("%Y-%m-%d %H:%M")
        else:
            pub_time_str = str(pub_time)

        sentiment = news.get("sentiment", "unknown")

        report += f"## {i}. {title}\n\n"
        report += f"**来源**: {source} | **时间**: {pub_time_str}\n"
        report += f"**类型**: {news.get('source_kind', 'aggregator')} | **正文状态**: {news.get('content_status', 'unverified')}\n"
        discovery = news.get("discovery_metadata") or {}
        public_coverage = {key: discovery[key] for key in
                           ("channel", "subject_binding", "body_completeness", "body_fetch_status", "automated_article")
                           if key in discovery}
        if public_coverage:
            report += "**关联与覆盖说明**: " + json.dumps(public_coverage, ensure_ascii=False) + "（非公司事实核验）\n"
        if news.get("url"):
            report += f"**原文**: {news['url']}\n"
        if sentiment:
            report += f"**情绪**: {sentiment}\n"
            report += f"**标签来源/方法**: {news.get('sentiment_label_source', 'unknown')} / {news.get('sentiment_method', 'unknown')}（未独立核验，不是股价预测）\n"
            if news.get("sentiment_evidence"):
                report += "**分类依据**: " + json.dumps(news["sentiment_evidence"], ensure_ascii=False) + "\n"
            report += f"**分类状态说明**: {news.get('sentiment_reason', 'unverified')}\n"
        report += "\n"

        if content:
            # 内容全量返回，不截断：新闻正文是分析师的信息源，截断会丢关键事实
            report += f"{content}\n\n"

        report += "---\n\n"

    return report


def _fetch_market_news(max_results: int, end_date: str = None) -> list:
    """读取市场快讯兜底（symbol 为空的全市场新闻），来源标注为市场新闻。"""
    try:
        from app.data.core.interface import DataInterface
        from app.core.async_utils import run_async

        di = DataInterface.get_instance()
        end_date = end_date or now_utc().strftime("%Y-%m-%d")
        start_date = (datetime.fromisoformat(end_date) - timedelta(days=30)).strftime("%Y-%m-%d")
        result = run_async(di.read("CN", "news", start_date=start_date, end_date=end_date, filters={"limit": max_results}))
        data = result.get("data")
        if not data or not isinstance(data, list):
            return []
        out = []
        for item in data[:max_results]:
            out.append(
                {
                    "title": item.get("title", "无标题"),
                    "content": item.get("content", "") or item.get("summary", ""),
                    "source": f"{item.get('data_source', item.get('source', '未知'))} (市场新闻,非个股)",
                    "publish_time": item.get("publish_time", now_utc()),
                    "sentiment": item.get("sentiment") or "unknown",
                    "url": item.get("url", ""),
                }
            )
        if out:
            logger.info(f"[新闻工具] 市场新闻兜底: {len(out)} 条")
        return out
    except Exception as e:
        logger.warning(f"[新闻工具] 市场新闻兜底失败: {e}")
        return []


def _filter_news_asof(news_list: list, as_of: str) -> list:
    """按分析基准日过滤新闻：发布日期晚于 as_of 的一律剔除（防前视）。

    publish_time 缺失或无法解析时保留该条（缺失元数据不是泄密的证据，
    强行剔除会误伤无时间戳的存量语料）。
    """
    if not as_of:
        return news_list
    cutoff = as_of[:10]
    out = []
    for item in news_list:
        pub = item.get("publish_time")
        if isinstance(pub, datetime):
            day = pub.strftime("%Y-%m-%d")
        elif isinstance(pub, str) and len(pub) >= 10:
            day = pub[:10]
        else:
            out.append(item)
            continue
        if day <= cutoff:
            out.append(item)
    return out


def get_stock_news(symbol: str, max_results: int = 15, end_date: str = None, current_date: str = None) -> str:
    """
    获取指定股票的最新新闻。

    返回格式化的新闻列表，包含标题、来源、时间和摘要。

    Args:
        symbol: 股票代码，如 "000001.SZ"(A股)、"AAPL"(美股)、"00700.HK"(港股)
        max_results: 返回的最大新闻数，建议范围 5-20，默认 10

    Returns:
        JSON 格式的 ToolResult，包含 status、data、error_code、suggestion 字段
    """
    if not symbol:
        return format_tool_result(error_result(ErrorCodes.MISSING_PARAM, "未提供股票代码"))

    try:
        if current_date:
            end_date = min(end_date, current_date) if end_date else current_date
        snapshot = _fetch_news_snapshot(symbol, max_results, end_date=end_date)
        news_list = snapshot["items"]

        if news_list:
            source = news_list[0].get("source", "Unknown")
            if "(DB)" in source:
                source_label = "数据库缓存"
            elif "(Refreshed)" in source:
                source_label = "实时刷新"
            else:
                source_label = "聚合数据"

            metadata = json.dumps({key: value for key, value in snapshot.items() if key != "items"}, ensure_ascii=False, default=str)
            return format_tool_result(success_result(metadata + "\n\n" + _format_news_list(news_list, source_label)))

        # 兜底：个股新闻无数据时，回退到市场快讯（库里大量新闻挂在空 symbol 下），
        # 让分析师至少有市场面信息可用，而不是直接 no_data 结束。
        market_news = _fetch_market_news(max_results, end_date=end_date)
        if market_news:
            return format_tool_result(success_result(
                _format_news_list(market_news, f"市场新闻兜底（未找到 {symbol} 个股新闻）")
            ))

        return format_tool_result(error_result(ErrorCodes.DATA_FETCH_ERROR,
            f"未取得 {symbol} 的可验证个股新闻；不代表没有事件",
            suggestion="该范围已做一次有界刷新，保留缺失状态，不反复重试"))
    except Exception as e:
        logger.error(f"get_stock_news failed: {e}", exc_info=True)
        return format_tool_result(error_result(ErrorCodes.DATA_FETCH_ERROR, str(e)))
