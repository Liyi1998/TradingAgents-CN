"""妙想 MCP 结果的标准化与写回。

MCP 返回 ``[{type: text, text: JSON}]``，此前只存在于单次 Agent 上下文中。
本模块把可结构化的新闻/公告和财务指标写入现有业务集合，同时保留原始快照。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.data.schema.domains.money_flow import MoneyFlowSchema
from app.data.schema.domains.stock_news import StockNewsSchema

logger = logging.getLogger(__name__)


def _canonical_mx_tool_name(tool_name: str) -> str:
    """把 ``mcp__server__mx_tool`` 还原为 MCP 服务端原始工具名。"""
    return str(tool_name).rsplit("__", 1)[-1]


def _resolve_symbol(
    tool_args: Any,
    context: Optional[Dict[str, str]] = None,
) -> str:
    """从显式上下文、结构化参数或自然语言 query 中提取 A 股代码。"""
    context = context or {}
    candidates: List[Any] = [context.get("ticker")]
    if isinstance(tool_args, dict):
        candidates.extend(
            tool_args.get(key)
            for key in ("ticker", "symbol", "stock_code", "ts_code", "code")
        )
        candidates.append(tool_args.get("query"))

    for candidate in candidates:
        if candidate in (None, ""):
            continue
        text = str(candidate).strip()
        match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
        if match:
            return match.group(1)
        if text.isdigit() and 1 <= len(text) <= 6:
            return text.zfill(6)
    return ""


def _raw_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        texts = [str(item.get("text")) for item in raw if isinstance(item, dict) and item.get("text")]
        if texts:
            return "\n".join(texts)
    if isinstance(raw, dict) and raw.get("text"):
        return str(raw["text"])
    try:
        return json.dumps(raw, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(raw)


def extract_sheets(raw: Any) -> List[Dict[str, Any]]:
    """从 LangChain MCP ContentBlock 中提取妙想表格。"""
    text = _raw_text(raw).strip()
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    data = payload.get("data", []) if isinstance(payload, dict) else []
    return [sheet for sheet in data if isinstance(sheet, dict)]


def normalize_mx_news(
    raw: Any, symbol: str, *, category: str = "news"
) -> List[Dict[str, Any]]:
    """把妙想新闻/公告二维表转换为 StockNewsSchema 文档。"""
    records: List[Dict[str, Any]] = []
    aliases = {
        "title": ("标题", "公告标题", "名称"),
        "content": ("摘要", "内容", "正文"),
        "publish_time": ("发布时间", "公告日期", "时间", "日期"),
        "source": ("来源", "发布机构"),
        "url": ("跳转链接", "链接", "URL", "url"),
    }
    for sheet in extract_sheets(raw):
        columns = [str(value) for value in sheet.get("columns", [])]
        for values in sheet.get("items", []):
            if not isinstance(values, list):
                continue
            row = dict(zip(columns, values))

            def pick(field: str) -> str:
                for name in aliases[field]:
                    value = row.get(name)
                    if value not in (None, ""):
                        return str(value).strip()
                return ""

            title = pick("title")
            publish_time = pick("publish_time")
            if not title:
                continue
            schema = StockNewsSchema(
                symbol=str(symbol).split(".", 1)[0].zfill(6),
                market="CN",
                data_source="mx_mcp",
                title=title,
                content=pick("content") or None,
                content_hash=StockNewsSchema.compute_hash(title, publish_time, str(symbol).split(".", 1)[0].zfill(6)),
                source=pick("source") or "妙想 MCP",
                publish_time=publish_time or datetime.now(timezone.utc).isoformat(),
                url=pick("url") or None,
                category=category,
            )
            records.append(schema.to_db_doc())
    return records


def _number(value: Any, *, percent: bool = False) -> Optional[float]:
    text = str(value if value is not None else "").strip().replace(",", "")
    if not text or text in {"--", "-", "nan"}:
        return None
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", text)
    if not match:
        return None
    number = float(match.group())
    if "万亿" in text:
        number *= 1e12
    elif "亿元" in text or text.endswith("亿"):
        number *= 1e8
    elif "万元" in text or text.endswith("万"):
        number *= 1e4
    if percent or "%" in text:
        number /= 100
    return number


def _report_period(label: str) -> Optional[str]:
    # A dated column is a period end, not automatically an annual report.
    dated = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", str(label))
    if dated:
        try:
            date = datetime(*(int(part) for part in dated.groups()))
            if date.strftime("%m-%d") not in {"03-31", "06-30", "09-30", "12-31"} or "(日)" in str(label):
                return None
            return date.strftime("%Y-%m-%d")
        except ValueError:
            return None
    match = re.search(r"(20\d{2})", str(label))
    if not match:
        return None
    year = match.group(1)
    text = str(label)
    if "一季" in text or "Q1" in text.upper():
        return f"{year}-03-31"
    if "中报" in text or "半年" in text or "Q2" in text.upper():
        return f"{year}-06-30"
    if "三季" in text or "Q3" in text.upper():
        return f"{year}-09-30"
    if "年报" in text or "年度" in text or "Q4" in text.upper() or "FY" in text.upper() or re.fullmatch(r"20\d{2}年?", text.strip()):
        return f"{year}-12-31"
    return None


def normalize_mx_financial(raw: Any, symbol: str) -> List[Dict[str, Any]]:
    """解析妙想横向财务指标表，生成每个报告期一条标准记录。"""
    metric_map = {
        "营业总收入": ("revenue", False),
        "营业收入": ("revenue", False),
        "归属于母公司股东的净利润": ("net_profit", False),
        "净利润": ("net_profit", False),
        "净资产收益率ROE(加权)": ("roe", True),
        "净资产收益率ROE": ("roe", True),
        "销售毛利率": ("gross_margin", True),
        "销售净利率": ("net_margin", True),
        "资产负债率": ("debt_ratio", True),
        "流动比率": ("current_ratio", False),
        "每股收益EPS(基本)": ("eps", False),
        "每股净资产BPS": ("bps", False),
        "经营活动产生的现金流量净额": ("operating_cashflow", False),
    }
    by_period: Dict[str, Dict[str, Any]] = {}
    clean_symbol = str(symbol).split(".", 1)[0].zfill(6)
    for sheet in extract_sheets(raw):
        columns = [str(value) for value in sheet.get("columns", [])]
        periods = [_report_period(value) for value in columns[1:]]
        if not any(periods):
            continue
        for values in sheet.get("items", []):
            if not isinstance(values, list) or not values:
                continue
            mapping = metric_map.get(str(values[0]).strip())
            if mapping is None:
                continue
            field, is_percent = mapping
            for period, value in zip(periods, values[1:]):
                if not period:
                    continue
                if period > datetime.now(timezone.utc).strftime("%Y-%m-%d"):
                    continue
                if not is_percent and "%" in str(value):
                    # e.g. a vendor snapshot labeled BPS as "0.088%". Its
                    # units are contradictory; do not corrupt a valid value.
                    continue
                number = _number(value, percent=is_percent)
                if number is None:
                    continue
                # FinancialDataSchema stores percentages in percentage points.
                if is_percent:
                    number *= 100
                record = by_period.setdefault(period, {
                    "symbol": clean_symbol, "market": "CN", "data_source": "mx_mcp",
                    "report_period": period, "statement_type": "indicator",
                })
                # 更明确的“营业总收入/归母净利润”优先于宽泛同义项。
                if field not in record or values[0] in ("营业总收入", "归属于母公司股东的净利润"):
                    record[field] = number
    now = datetime.now(timezone.utc).isoformat()
    return [{**record, "updated_at": now} for record in by_period.values()]


def normalize_mx_money_flow(raw: Any, symbol: str) -> List[Dict[str, Any]]:
    """把妙想返回的逐日订单资金表写入标准资金流集合。

    仅解析包含“时间 + 主力净流入金额”的明细表，避免把新闻摘要中的数字或
    融资余额横表误当成资金流。妙想金额列标明单位为万元，统一转换为元。
    """
    clean_symbol = str(symbol).split(".", 1)[0].zfill(6)
    aliases = {
        "trade_date": ("时间", "日期", "交易日期"),
        "main": ("主力净流入金额(万元)", "主力净流入金额（万元）"),
        "main_pct": ("主力资金净流入比率(%)", "主力净流入占比(%)"),
        "huge": ("超大单净买入金额(万元)", "超大单净流入金额(万元)"),
        "large": ("大单净买入金额(万元)", "大单净流入金额(万元)"),
        "medium": ("中单净买入金额(万元)", "中单净流入金额(万元)"),
        "small": ("小单净买入金额(万元)", "小单净流入金额(万元)"),
    }

    def pick(row: Dict[str, Any], names) -> Any:
        for name in names:
            if row.get(name) not in (None, ""):
                return row[name]
        return None

    records: List[Dict[str, Any]] = []
    for sheet in extract_sheets(raw):
        columns = [str(value).strip() for value in sheet.get("columns", [])]
        if not any(name in columns for name in aliases["trade_date"]):
            continue
        if not any(name in columns for name in aliases["main"]):
            continue
        for values in sheet.get("items", []):
            if not isinstance(values, list):
                continue
            row = dict(zip(columns, values))
            date_text = str(pick(row, aliases["trade_date"]) or "").strip()[:10]
            if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", date_text):
                continue

            def amount(field: str) -> Optional[float]:
                value = _number(pick(row, aliases[field]))
                return value * 10_000 if value is not None else None

            schema = MoneyFlowSchema(
                symbol=clean_symbol,
                market="CN",
                data_source="mx_mcp",
                trade_date=date_text,
                main_net_inflow=amount("main"),
                main_net_inflow_pct=_number(pick(row, aliases["main_pct"])),
                huge_net_inflow=amount("huge"),
                large_net_inflow=amount("large"),
                medium_net_inflow=amount("medium"),
                small_net_inflow=amount("small"),
            )
            records.append(schema.to_db_doc())
    return records


async def _is_news_fresh_impl(symbol: str, max_age_hours: int = 24) -> bool:
    """按新闻实际发布时间判断，而不是按最近一次缓存写入时间判断。"""
    from app.data.storage.mongo.repositories.news_repo import NewsRepo

    records = await NewsRepo().get_by_symbol(str(symbol).split(".", 1)[0].zfill(6), "CN", limit=20)
    latest: Optional[datetime] = None
    for record in records:
        value = record.get("publish_time")
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            latest = parsed if latest is None or parsed > latest else latest
        except ValueError:
            continue
    return bool(latest and latest >= datetime.now(timezone.utc) - timedelta(hours=max_age_hours))


async def is_news_fresh(symbol: str, max_age_hours: int = 24) -> bool:
    """跨 LangGraph 任务 loop 安全地检查新闻时效。"""
    from app.core.async_utils import await_on_main_loop

    return await await_on_main_loop(
        _is_news_fresh_impl(symbol, max_age_hours=max_age_hours)
    )


async def _persist_mcp_result_impl(
    tool_name: str,
    tool_args: Any,
    raw: Any,
    context: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """尽力写回 MCP 结果；写回失败不能中断分析。"""
    original_tool_name = str(tool_name)
    tool_name = _canonical_mx_tool_name(original_tool_name)
    if not tool_name.startswith("mx_"):
        return {"domain": None, "count": 0}
    context = context or {}
    symbol = _resolve_symbol(tool_args, context)
    raw_text = _raw_text(raw)
    now = datetime.now(timezone.utc)
    query = tool_args.get("query", "") if isinstance(tool_args, dict) else ""
    query_hash = hashlib.sha256(f"{tool_name}|{symbol}|{query}".encode()).hexdigest()

    from app.data.storage.mongo.repositories.mcp_cache_repo import MCPDataCacheRepo

    cache_date = now.strftime("%Y-%m-%d")
    await MCPDataCacheRepo().upsert_daily_snapshot(
        query_hash=query_hash,
        cache_date=cache_date,
        payload={
            "tool_name": tool_name, "wrapped_tool_name": original_tool_name,
            "symbol": symbol, "market": "CN",
            "query": query, "query_hash": query_hash, "cache_date": cache_date,
            "raw_text": raw_text, "data_source": "mx_mcp", "updated_at": now.isoformat(),
        },
    )

    domain: Optional[str] = None
    count = 0
    domain_counts: Dict[str, int] = {}
    if tool_name in {"mx_finance_search_news", "mx_finance_search_notice"} and symbol:
        domain = "news"
        category = "notice" if tool_name.endswith("notice") else "news"
        records = normalize_mx_news(raw, symbol, category=category)
        if records:
            from app.data.storage.mongo.repositories.news_repo import NewsRepo
            count = await NewsRepo().upsert_many(records, "CN")
            domain_counts[domain] = count
    elif tool_name in {"mx_ashare_finance_data", "mx_comprehensive_finance_data"} and symbol:
        domain = "financial_data"
        records = normalize_mx_financial(raw, symbol)
        if records:
            from app.data.storage.mongo.repositories.financial_data_repo import FinancialDataRepo
            count = await FinancialDataRepo().upsert_many(records, "CN")
            domain_counts[domain] = count

        money_flow_records = normalize_mx_money_flow(raw, symbol)
        if money_flow_records:
            from app.data.storage.mongo.repositories.money_flow_repo import MoneyFlowRepo
            money_flow_count = await MoneyFlowRepo().upsert_many(money_flow_records, "CN")
            domain_counts["money_flow"] = money_flow_count

    if domain and domain not in domain_counts:
        domain_counts[domain] = count
    if domain_counts:
        from app.data.storage.mongo.repositories.metadata_repo import MetadataRepo
        for current_domain, current_count in domain_counts.items():
            await MetadataRepo().update_checkpoint(
                market="CN", domain=current_domain, source="mx_mcp",
                last_sync_date=now.strftime("%Y-%m-%d"), record_count=current_count,
                status="success" if current_count else "empty", scope="single",
                trigger="analysis", symbol=str(symbol).split(".", 1)[0].zfill(6),
            )
    total_count = sum(domain_counts.values())
    return {
        "domain": domain,
        "count": count,
        "domains": domain_counts,
        "total_count": total_count,
    }


async def persist_mcp_result(
    tool_name: str,
    tool_args: Any,
    raw: Any,
    context: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """跨 LangGraph 任务 loop 安全地写回妙想结果。"""
    from app.core.async_utils import await_on_main_loop

    return await await_on_main_loop(
        _persist_mcp_result_impl(tool_name, tool_args, raw, context)
    )
