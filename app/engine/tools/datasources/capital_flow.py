"""
资金流向工具 - 资金流向数据、融资融券数据
"""

import logging
import json
from typing import Optional
from datetime import datetime, timedelta

from app.utils.time_utils import now_utc, get_current_date
from app.engine.tools.common.tool_result import success_result, error_result, format_tool_result, ErrorCodes
from app.engine.tools.common.format import format_result
import pandas as pd

logger = logging.getLogger(__name__)


def _normalize_storage_date(value: Optional[str]) -> Optional[str]:
    """把工具兼容的日期输入统一成 Mongo 标准日期 ``YYYY-MM-DD``。

    旧工具契约使用 ``YYYYMMDD``，而标准数据集合保存 ISO 短日期。字符串
    范围查询不会自动做日期转换，因此 compact 日期会造成“库中有数据但查不到”。
    """
    if value is None:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"日期格式无效: {value}，应为 YYYY-MM-DD 或 YYYYMMDD")


def _format_money_flow(data, title: str) -> str:
    """免费源优先展示，并明确不同供应商的资金流定义不可直接混用。"""
    df = pd.DataFrame(data) if isinstance(data, list) else data.copy()
    if isinstance(df, pd.DataFrame) and "data_source" in df.columns:
        priority = {"sina": 0, "tencent": 1, "akshare": 2, "mx_mcp": 3}
        df = df.assign(
            _source_priority=df["data_source"].map(priority).fillna(99)
        ).sort_values(["_source_priority", "trade_date"], ascending=[True, False])
        df = df.drop(columns=["_source_priority"])
        sources = [str(v) for v in df["data_source"].dropna().unique()]
    else:
        sources = []

    notice = (
        "数据口径说明：优先展示无需付费的新浪/腾讯/AKShare标准库数据。"
        "新浪 netamount 与妙想/东方财富按超大单、大单拆分的‘主力资金’定义不同，"
        "数值可能方向相反，不能相互覆盖或直接相加；如存在多源结果，分析时必须"
        "分别列出来源、数值和日期，并将差异作为不确定性披露。"
    )
    if len(sources) > 1:
        notice += f" 当前命中来源：{', '.join(sources)}。"
    return f"{notice}\n\n{format_result(df, title)}"


def get_money_flow(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    query_type: Optional[str] = None,
    symbol: Optional[str] = None,
    content_type: Optional[str] = None,
    trade_date: Optional[str] = None,
) -> str:
    """
    获取资金流向数据。

    Args:
        start_date: 开始日期，格式 YYYY-MM-DD 或 YYYYMMDD，默认 1 个月前
        end_date: 结束日期，格式 YYYY-MM-DD 或 YYYYMMDD，默认今天
        query_type: 查询类型，支持 stock(个股)、market(大盘)、sector(板块)
        symbol: 股票或板块代码
        content_type: 板块类型，支持 industry(行业)、concept(概念)、area(地域)
        trade_date: 指定交易日期，格式 YYYY-MM-DD 或 YYYYMMDD

    Returns:
        JSON 格式的 ToolResult，包含 status、data、error_code、suggestion 字段
    """
    try:
        if trade_date:
            start_date = end_date = _normalize_storage_date(trade_date)
        else:
            if not end_date:
                end_date = get_current_date()
            if not start_date:
                start_date = (now_utc() - timedelta(days=30)).strftime("%Y-%m-%d")
            start_date = _normalize_storage_date(start_date)
            end_date = _normalize_storage_date(end_date)

        try:
            from app.engine.tools.common.data_access import read_with_refresh, render_data_quality

            # symbol 为 "market" 时是市场级查询，无法按需刷新，helper 内部会自动跳过
            result = read_with_refresh(
                "CN", "money_flow", symbol=symbol or "market", start_date=start_date, end_date=end_date
            )
            data = result.get("data") if result else None
            if data:
                return format_tool_result(success_result(
                    render_data_quality(result) + "\n" + json.dumps(result.get("derived_context"), ensure_ascii=False)
                    + "\n\n" + _format_money_flow(data, f"Money Flow: {symbol or query_type}")
                ))
            if result:
                return format_tool_result(error_result(
                    ErrorCodes.DATA_FETCH_ERROR, render_data_quality(result),
                    suggestion="已完成有界取数；披露资金流缺项并继续其余分析，不要补零或重复请求。"))
        except Exception as e:
            logger.debug(f"资金流向数据获取失败: {e}")

        return format_tool_result(
            error_result(
                ErrorCodes.DATA_FETCH_ERROR,
                f"资金流向数据暂不可用: {symbol or query_type}",
                suggestion="披露当前免费链路未取得该数据，继续其余分析；不得据此推算资金评分。",
            )
        )
    except Exception as e:
        logger.error(f"get_money_flow failed: {e}", exc_info=True)
        return format_tool_result(error_result(ErrorCodes.DATA_FETCH_ERROR, str(e)))


def get_margin_trade(
    data_type: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    symbol: Optional[str] = None,
    exchange: Optional[str] = None,
) -> str:
    """
    获取融资融券数据。

    Args:
        data_type: 数据类型，支持 margin_secs、margin、margin_detail、slb_len_mm
        start_date: 开始日期，格式 YYYY-MM-DD 或 YYYYMMDD，默认 1 个月前
        end_date: 结束日期，格式 YYYY-MM-DD 或 YYYYMMDD，默认今天
        symbol: 股票代码
        exchange: 交易所，支持 SSE、SZSE、BSE

    Returns:
        JSON 格式的 ToolResult，包含 status、data、error_code、suggestion 字段
    """
    try:
        if not end_date:
            end_date = get_current_date()
        if not start_date:
            start_date = (now_utc() - timedelta(days=30)).strftime("%Y-%m-%d")
        start_date = _normalize_storage_date(start_date)
        end_date = _normalize_storage_date(end_date)

        symbol = symbol or "market"
        try:
            from app.engine.tools.common.data_access import read_with_refresh

            from app.core.async_utils import run_async
            from app.data.core.interface import DataInterface
            context = run_async(DataInterface.get_instance().analysis_context("CN", end_date))
            eligibility = run_async(DataInterface.get_instance().margin_eligibility("CN", symbol, context.get("last_actual_trade_date") or end_date))
            if eligibility.get("status") == "not_applicable":
                return format_tool_result(success_result(json.dumps({"quality": {"status": "not_applicable"}, "eligibility": eligibility, "records": []}, ensure_ascii=False)))
            result = read_with_refresh(
                "CN", "margin_trading", symbol=symbol, start_date=start_date, end_date=end_date,
                filters={"expected_dates": context.get("recent_trade_dates") or []},
            )
            data = result.get("data") if result else None
            if data:
                df = pd.DataFrame(data) if isinstance(data, list) else data
                return format_tool_result(success_result(json.dumps({
                    "analysis_context": context, "eligibility": eligibility,
                    "quality": result.get("quality"), "records": df.to_dict(orient="records"),
                    "repayment_field_contract": {
                        "margin_repay_amount": "来源直报的融资偿还额；空值仍表示未取得直报。",
                        "margin_repay_amount_derived": "同来源、同单位、相邻真实交易日余额恒等式计算值，非官方直报。",
                        "usage_limit": "仅在margin_repay_derivation_status=derived_not_reported时可引用为计算值；不得填充直报字段、声称直报完整，或据此伪造完整资金评分。",
                    },
                }, ensure_ascii=False, default=str)))
        except Exception as e:
            logger.debug(f"融资融券数据获取失败: {e}")

        return format_tool_result(
            error_result(
                ErrorCodes.DATA_FETCH_ERROR,
                f"融资融券数据暂不可用: {data_type}",
                suggestion="请先通过同步任务获取融资融券数据，或确认数据源已配置",
            )
        )
    except Exception as e:
        logger.error(f"get_margin_trade failed: {e}", exc_info=True)
        return format_tool_result(error_result(ErrorCodes.DATA_FETCH_ERROR, str(e)))
