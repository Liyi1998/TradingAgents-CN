"""
中国A股市场工具 - 市场概览、龙虎榜、大宗交易

主要数据通过 DataInterface 统一获取，走 FallbackRouter 自动降级。
板块行情属于高频实时快照，通过东方财富 API 直接获取（不经过 DataInterface）。
"""
# data-access-exempt: 板块行情为高频实时快照，标准库无此 domain；akshare 板块接口
# 在本环境实测不可用（ConnectionError），需 akshare 优先 + eastmoney 直连兜底双通道。
# 若后续板块数据入标准库（market_quotes 扩展），应移除本豁免。

import json
import asyncio
import logging
from typing import Optional
from datetime import timedelta

from app.utils.time_utils import now_utc, get_current_date, get_current_date_compact
from app.engine.tools.common.tool_result import success_result, no_data_result, error_result, format_tool_result, ErrorCodes
from app.engine.tools.common.data_access import read_with_refresh, render_data_quality
from app.data.core.interface import DataInterface
from app.core.async_utils import run_async

logger = logging.getLogger(__name__)


def get_china_market_overview(date: str = None, include_indices: bool = True, include_sectors: bool = True) -> str:
    """
    获取中国A股市场整体概览。

    返回市场指数、板块表现等宏观市场数据。

    Args:
        date: 查询日期，格式 YYYY-MM-DD，默认今天
        include_indices: 是否包含主要指数数据（上证、深证、创业板等）
        include_sectors: 是否包含板块表现数据

    Returns:
        JSON 格式的 ToolResult
    """
    logger.info("[中国市场工具] 获取市场概览")
    start_time = now_utc()

    if not date:
        date = get_current_date()

    result_sections = []

    if include_indices:
        indices_to_fetch = [
            ("000001.SH", "上证指数"),
            ("399001.SZ", "深证成指"),
            ("399006.SZ", "创业板指"),
        ]

        indices_data = []
        try:
            for symbol, name in indices_to_fetch:
                try:
                    result = read_with_refresh("CN", "market_quotes", symbol=symbol, end_date=date, timeout=6,
                        filters={"asset_type": "index", "exchange": symbol.split(".")[-1]})
                    data = result.get("data")
                    if data:
                        rows = data if isinstance(data, list) else [data]
                        row = max(rows, key=lambda item: str(item.get("trade_date") or ""))
                        price = row.get("close") if row.get("close") is not None else row.get("last_price")
                        indices_data.append(f"- **{name}**: {price if price is not None else '未取得'}；交易日 {row.get('trade_date')}；来源 {row.get('data_source')}")
                    else:
                        indices_data.append(f"- **{name}**: 未取得。{render_data_quality(result)}")
                except Exception as e:
                    logger.warning(f"获取 {name} 失败: {e}")
                    indices_data.append(f"- **{name}**: 未取得（{type(e).__name__}）；继续其余指数，不填零。")
        except Exception as e:
            logger.warning(f"获取指数数据异常: {e}")

        if indices_data:
            result_sections.append("## 主要指数\n\n" + "\n".join(indices_data))
        else:
            result_sections.append("## 主要指数\n\n有界免费链路未取得指数数据，继续其他资料，不将未知填为零。")

    if include_sectors and date != now_utc().astimezone(__import__("zoneinfo").ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d"):
        result_sections.append("## 板块表现\n历史板块快照不可用；未使用今日板块冒充分析日数据。")
        include_sectors = False
    if include_sectors:
        try:
            sector_df = None

            # 板块行情是高频实时快照：优先 akshare 库（由其维护反爬），
            # 失败时回退到数据层 eastmoney 直连通道（push2delay，实战验证过的替代路径）
            try:
                import akshare as ak

                raw = run_async(asyncio.wait_for(asyncio.to_thread(ak.stock_board_industry_name_em), timeout=6))
                if raw is not None and not raw.empty:
                    import pandas as pd

                    sector_df = pd.DataFrame(raw)
                    # akshare 返回中文列名（板块名称/涨跌幅/最新价等），直接使用
                    sector_df = sector_df.sort_values("涨跌幅", ascending=False)
                    logger.info(f"akshare 板块数据获取成功: {len(sector_df)} 条")
            except Exception as e:
                logger.warning(f"akshare 板块数据获取失败，回退 eastmoney 直连: {e}")

            if sector_df is None or sector_df.empty:
                try:
                    from app.data.sources.cn.eastmoney.direct import fetch_em_board_direct

                    board_data = run_async(asyncio.wait_for(asyncio.to_thread(fetch_em_board_direct), timeout=6))
                    if board_data:
                        import pandas as pd

                        sector_df = pd.DataFrame(board_data)
                        sector_df = sector_df.rename(columns={"f14": "板块名称", "f3": "涨跌幅", "f2": "最新价"})
                        sector_df = sector_df.sort_values("涨跌幅", ascending=False)
                        logger.info(f"eastmoney 直连板块数据获取成功: {len(sector_df)} 条")
                except Exception as e:
                    logger.warning(f"eastmoney 直连板块数据失败: {e}")

            if sector_df is not None and not sector_df.empty:
                top_sectors = sector_df.head(5)
                bottom_sectors = sector_df.tail(5)

                sector_info = "## 板块表现\n\n"
                sector_info += "### 涨幅前5\n"
                for _, row in top_sectors.iterrows():
                    name = row.get("板块名称", "N/A")
                    change = row.get("涨跌幅", "N/A")
                    sector_info += f"- {name}: {change}%\n"

                sector_info += "\n### 跌幅前5\n"
                for _, row in bottom_sectors.iterrows():
                    name = row.get("板块名称", "N/A")
                    change = row.get("涨跌幅", "N/A")
                    sector_info += f"- {name}: {change}%\n"

                result_sections.append(sector_info)
            else:
                result_sections.append("## 板块表现\n\n免费备用链路未取得板块数据，不影响已返回的指数；板块涨跌/资金结论保持未知。")

        except Exception as e:
            logger.error(f"[中国市场工具] 获取板块数据失败: {e}", exc_info=True)
            result_sections.append(f"## 板块表现\n\n获取失败: {e}")

    execution_time = (now_utc() - start_time).total_seconds()

    combined_result = f"""# 中国A股市场概览

**查询日期**: {date}
**执行时间**: {execution_time:.2f}秒

{chr(10).join(result_sections)}

---
*数据来源: 标准数据库（DataInterface，自动降级）；板块行情为实时快照，经标准字段（板块名称/涨跌幅/最新价）转换*
"""
    logger.info(f"[中国市场工具] 数据获取完成，总长度: {len(combined_result)}")
    return format_tool_result(success_result(combined_result))


def get_dragon_tiger_inst(trade_date: Optional[str] = None, symbol: Optional[str] = None) -> str:
    """
    获取龙虎榜机构明细。

    Args:
        trade_date: 交易日期，格式 YYYYMMDD；分析日默认对齐最近已完成交易日
        symbol: 股票代码，可选

    Returns:
        JSON 格式的 ToolResult
    """
    try:
        from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT

        context = ACTIVE_ANALYSIS_CONTEXT.get() or {}
        analysis_day = str(context.get("analysis_date") or "").replace("-", "")
        if context and (not trade_date or str(trade_date).replace("-", "") == analysis_day):
            completed = context.get("last_completed_trade_date")
            if not completed:
                return format_tool_result(error_result(
                    ErrorCodes.DATA_FETCH_ERROR,
                    "最近已完成交易日未核验；未向休市日或未收盘日期请求龙虎榜，继续其他分析。",
                ))
            trade_date = completed.replace("-", "")
        if not trade_date:
            trade_date = get_current_date_compact()

        logger.info(f"获取龙虎榜数据: 日期{trade_date}, 股票{symbol}")

        from app.engine.tools.common.data_access import read_with_refresh

        filters = {"limit": 100}

        if symbol:
            day = trade_date if "-" in trade_date else f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
            result = read_with_refresh("CN", "dragon_tiger", symbol=symbol, start_date=day, end_date=day, filters=filters)
        else:
            result = run_async(DataInterface.get_instance().read("CN", "dragon_tiger", start_date=trade_date, filters=filters))

        data = result.get("data") if result else None

        if data:
            if isinstance(data, list):
                records = data[:50]
            else:
                records = [data]

            json_data = json.dumps(records, ensure_ascii=False, default=str)
            return format_tool_result(success_result(data=json_data))
        if (result or {}).get("quality", {}).get("status") == "no_events":
            return format_tool_result(no_data_result(
                f"免费源正常查询后，在 {trade_date} 未返回 {symbol or '所查询范围'} 的龙虎榜事项记录；这不是网络获取失败，也不证明无风险。",
                suggestion="不用重复同步；可继续其余分析，不虚构席位或机构交易。"))

        return format_tool_result(
            error_result(ErrorCodes.DATA_FETCH_ERROR, f"无法获取龙虎榜数据: {trade_date}。" + render_data_quality(result),
                         suggestion="披露源失败或未确认缺项并继续其他分析，不重复请求或编造席位。")
        )
    except Exception as e:
        logger.error(f"get_dragon_tiger_inst failed: {e}", exc_info=True)
        return format_tool_result(error_result(ErrorCodes.DATA_FETCH_ERROR, str(e)))


def get_block_trade(
    start_date: Optional[str] = None, end_date: Optional[str] = None, symbol: Optional[str] = None
) -> str:
    """
    获取大宗交易数据。

    Args:
        start_date: 开始日期，格式 YYYYMMDD，默认 7 天前
        end_date: 结束日期，格式 YYYYMMDD，默认今天
        symbol: 股票代码，可选

    Returns:
        JSON 格式的 ToolResult
    """
    try:
        if not end_date:
            end_date = get_current_date_compact()
        if not start_date:
            start_date = (now_utc() - timedelta(days=7)).strftime("%Y%m%d")

        logger.info(f"获取大宗交易数据: 日期范围 {start_date}-{end_date}, 股票{symbol}")

        from app.engine.tools.common.data_access import read_with_refresh

        if symbol:
            result = read_with_refresh("CN", "block_trade", symbol=symbol, start_date=start_date, end_date=end_date)
        else:
            result = run_async(
                DataInterface.get_instance().read("CN", "block_trade", start_date=start_date, end_date=end_date)
            )

        data = result.get("data") if result else None

        if data:
            if isinstance(data, list):
                records = data[:50]
            else:
                records = [data]

            json_data = json.dumps(records, ensure_ascii=False, default=str)
            return format_tool_result(success_result(data=json_data))
        if (result or {}).get("quality", {}).get("status") == "no_events":
            return format_tool_result(no_data_result(
                f"免费源正常查询后，在 {start_date}..{end_date} 未返回 {symbol or '所查询范围'} 的大宗交易事项；不等于网络错误，也不证明不存在未披露事项。",
                suggestion="继续其余分析，不虚构交易或反复要求手动同步。"))

        return format_tool_result(
            error_result(ErrorCodes.DATA_FETCH_ERROR, "无法获取大宗交易数据。" + render_data_quality(result),
                         suggestion="披露实际链路缺项并继续，不把源失败当成无事项。")
        )
    except Exception as e:
        logger.error(f"get_block_trade failed: {e}", exc_info=True)
        return format_tool_result(error_result(ErrorCodes.DATA_FETCH_ERROR, str(e)))
