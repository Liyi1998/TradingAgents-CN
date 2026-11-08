#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一股票数据服务 — 通过 DataInterface 访问，供路由层调用。"""

import logging
from typing import Dict, List, Optional

from app.data.core.interface import DataInterface

logger = logging.getLogger(__name__)


class UnifiedStockService:
    """统一股票数据服务（跨市场，通过 DataInterface 访问）。"""

    def __init__(self, db=None):
        # db 参数保留以兼容旧调用方，实际通过 DataInterface 访问
        pass

    def _di(self) -> DataInterface:
        return DataInterface.get_instance()

    async def get_stock_info(
        self, market: str, code: str, source: Optional[str] = None
    ) -> Optional[Dict]:
        result = await self._di().read(market, "basic_info", symbol=code)
        return result.get("data")

    async def get_stock_quote(self, market: str, code: str) -> Optional[Dict]:
        result = await self._di().read(market, "market_quotes", symbol=code)
        return result.get("data")

    async def search_stocks(self, market: str, query: str, limit: int = 20) -> List[Dict]:
        """搜索股票 — 通过 DataInterface.search_basic_info 实现 regex 搜索。"""
        return await self._di().search_basic_info(
            market, query,
            fields=["symbol", "name", "name_en", "full_symbol"],
            limit=limit,
        )

    async def get_daily_quotes(
        self, market: str, code: str,
        start_date: Optional[str] = None, end_date: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict]:
        result = await self._di().read(market, "daily_quotes", symbol=code,
                                       start_date=start_date, end_date=end_date)
        data = result.get("data")
        if not data:
            return []
        if isinstance(data, list):
            return data[-limit:]
        return [data]

    async def get_cn_quote_with_basic_info(self, code6: str, force_refresh: bool = False) -> Optional[Dict]:
        """A single dated quote snapshot, with a separately labelled daily fallback."""
        import asyncio
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from app.data.query.quote_snapshot import quote_view

        di = self._di()
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        day = now.date().isoformat()
        filters = {"asset_type": "stock", "required_fields": ["last_price"],
                   "contract": "verified_spot_quote", "quote_symbol": code6, "quote_as_of": day}
        q = {}
        try:
            async with asyncio.timeout(10):
                if force_refresh:
                    await di.refresh("CN", code6, domains=["market_quotes"], force=True, timeout=8,
                        start_date=day, end_date=day, coverage_filters=filters)
                    result = await di.read("CN", "market_quotes", symbol=code6, filters=filters)
                else:
                    result = await di.read_ready("CN", "market_quotes", code6, timeout=8,
                        start_date=day, end_date=day, filters=filters)
                data = result.get("data") or {}
                q = data[0] if isinstance(data, list) and data else data
        except Exception as exc:
            logger.warning("Single-stock quote unavailable: %s", type(exc).__name__)
        daily = await di.read_latest("CN", "daily_quotes", code6) or {}
        basic = (await di.read("CN", "basic_info", symbol=code6)).get("data") or {}
        return quote_view(code6, basic, q if isinstance(q, dict) else {}, daily, datetime.now(ZoneInfo("Asia/Shanghai")))

    async def get_cn_market_chart(self, code6: str, force_refresh: bool = False) -> Dict:
        from datetime import datetime, timezone
        from app.data.query.research_tracking import collect_market_chart
        from app.data.query.intraday_chart import intraday_observation
        chart = await collect_market_chart(code6, datetime.now(timezone.utc), di=self._di())
        # A visible dashboard needs a new observation, not the 15-minute
        # tolerance used by long-running analysis. One bounded single-stock call.
        quote = await self.get_cn_quote_with_basic_info(code6,
            force_refresh=force_refresh or chart['session']['phase'] in {'trading', 'settling'})
        now = datetime.now(timezone.utc)
        live, status = intraday_observation(code6, quote, chart['bars'], chart['session'], now)
        result = {**chart, 'quote': quote, 'live_bar': live, 'live_status': status, 'checked_at': now.isoformat()}
        from app.services.chan_structure import ChanStructureService
        result['chan'] = await ChanStructureService().build(code6, result, now=now)
        return result

    async def get_cn_fundamentals(
        self, code6: str, source: Optional[str] = None,
    ) -> Optional[Dict]:
        """获取 A 股基本面快照（basic_info + financial_data + daily_indicators）。"""
        di = self._di()

        b_result = await di.read("CN", "basic_info", symbol=code6)
        b = b_result.get("data")
        if not b:
            return None

        # 获取最新财务数据
        financial_data = None
        try:
            f_result = await di.read("CN", "financial_data", symbol=code6)
            f_data = f_result.get("data")
            if f_data and isinstance(f_data, list) and f_data:
                financial_data = f_data[0]
            elif f_data and isinstance(f_data, dict):
                financial_data = f_data
        except Exception as e:
            logger.warning(f"获取财务数据失败: {e}")

        # 获取实时指标
        realtime_metrics = {}
        try:
            ind_result = await di.read("CN", "daily_indicators", symbol=code6)
            ind_data = ind_result.get("data")
            if ind_data and isinstance(ind_data, list) and ind_data:
                latest = ind_data[0]
                realtime_metrics = {
                    "pe": latest.get("pe_ttm"),
                    "pb": latest.get("pb"),
                    "pe_ttm": latest.get("pe_ttm"),
                    "ps_ttm": latest.get("ps_ttm"),
                    "market_cap": latest.get("total_mv"),
                    "turnover_rate": latest.get("turnover_rate"),
                    "source": "daily_indicators",
                    "is_realtime": False,
                    "updated_at": latest.get("updated_at"),
                }
            elif ind_data and isinstance(ind_data, dict):
                realtime_metrics = {
                    "pe": ind_data.get("pe_ttm"),
                    "pb": ind_data.get("pb"),
                    "pe_ttm": ind_data.get("pe_ttm"),
                    "ps_ttm": ind_data.get("ps_ttm"),
                    "market_cap": ind_data.get("total_mv"),
                    "turnover_rate": ind_data.get("turnover_rate"),
                    "source": "daily_indicators",
                    "is_realtime": False,
                    "updated_at": ind_data.get("updated_at"),
                }
        except Exception as e:
            logger.warning(f"获取实时PE/PB失败: {e}")

        realtime_market_cap = realtime_metrics.get("market_cap")
        # total_mv 单位统一为亿元（数据库存储为元，前端期望亿元）
        raw_total_mv = realtime_market_cap if realtime_market_cap else b.get("total_mv")
        total_mv = round(raw_total_mv / 1e8, 2) if isinstance(raw_total_mv, (int, float)) else None

        result_data = {
            "symbol": code6,
            "name": b.get("name"),
            "industry": b.get("industry"),
            "market": b.get("market"),
            "sector": b.get("market"),
            "pe": realtime_metrics.get("pe") or b.get("pe"),
            "pb": realtime_metrics.get("pb") or b.get("pb"),
            "pe_ttm": realtime_metrics.get("pe_ttm") or b.get("pe_ttm"),
            "pb_mrq": realtime_metrics.get("pb_mrq") or b.get("pb_mrq"),
            "ps": realtime_metrics.get("ps_ttm"),
            "ps_ttm": realtime_metrics.get("ps_ttm"),
            "pe_source": realtime_metrics.get("source", "unknown"),
            "pe_is_realtime": realtime_metrics.get("is_realtime", False),
            "pe_updated_at": realtime_metrics.get("updated_at"),
            "roe": None,
            "debt_ratio": None,
            "total_mv": total_mv,
            "circ_mv": b.get("circ_mv"),
            "mv_is_realtime": bool(realtime_market_cap),
            "turnover_rate": realtime_metrics.get("turnover_rate") or b.get("turnover_rate"),
            "volume_ratio": b.get("volume_ratio"),
            "updated_at": b.get("updated_at"),
            "data_source": b.get("data_source"),
        }

        if financial_data:
            if financial_data.get("financial_indicators"):
                indicators = financial_data["financial_indicators"]
                result_data["roe"] = indicators.get("roe")
                result_data["debt_ratio"] = indicators.get("debt_to_assets") or indicators.get("debt_ratio")
            if result_data["roe"] is None:
                result_data["roe"] = financial_data.get("roe")
            if result_data["debt_ratio"] is None:
                result_data["debt_ratio"] = financial_data.get("debt_to_assets") or financial_data.get("debt_ratio")

            revenue_ttm = financial_data.get("revenue_ttm")
            revenue = financial_data.get("revenue")
            revenue_for_ps = revenue_ttm if revenue_ttm and revenue_ttm > 0 else revenue

            if revenue_for_ps and revenue_for_ps > 0 and total_mv and total_mv > 0:
                revenue_yi = revenue_for_ps / 100000000
                ps_calculated = total_mv / revenue_yi
                result_data["ps"] = round(ps_calculated, 2)
                if revenue_ttm:
                    result_data["ps_ttm"] = round(ps_calculated, 2)

        if result_data["roe"] is None:
            result_data["roe"] = b.get("roe")

        roe_evidence = ((financial_data or {}).get("field_provenance") or {}).get("roe") or {}
        result_data["roe_status"] = "available" if result_data["roe"] is not None else roe_evidence.get("status", "unavailable")
        result_data["financial_report_period"] = (financial_data or {}).get("report_period")

        return result_data

    async def get_market_quotes_raw(self, code: str) -> Optional[Dict]:
        result = await self._di().read("CN", "market_quotes", symbol=code)
        return result.get("data")

    async def get_supported_markets(self) -> List[Dict]:
        """获取支持的市场列表。"""
        from app.engine.config.runtime_settings import get_timezone_name

        return [
            {"code": "CN", "name": "A股", "name_en": "China A-Share",
             "currency": "CNY", "timezone": get_timezone_name()},
            {"code": "HK", "name": "港股", "name_en": "Hong Kong Stock",
             "currency": "HKD", "timezone": "Asia/Hong_Kong"},
            {"code": "US", "name": "美股", "name_en": "US Stock",
             "currency": "USD", "timezone": "America/New_York"},
        ]
