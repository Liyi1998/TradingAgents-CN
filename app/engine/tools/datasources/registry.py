"""
预注入数据源统一注册表

预注入数据源由代码（编排器）控制：分析师启动时预调用函数、
结果以 <tool_data> 注入上下文，LLM 不可主动调用——与 AI 可调用
内置工具（app/engine/tools/builtin）是两套独立方案。

本注册表集中管理数据源的元数据（数据域、市场、参数注入规则），
按数据域组织，替代各模块旧的 TOOL_FUNCTIONS / DATA_SOURCE_MAP /
ANALYST_MAP 三件套。
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from app.core.analysis_horizon import get_horizon_profile, horizon_start_date

logger = logging.getLogger(__name__)


@dataclass
class DatasourceSpec:
    """预注入数据源规格声明"""

    tool_id: str
    display_name: str
    domains: List[str]
    markets: List[str]
    fn: Callable
    inject_args: Dict[str, Any]
    description: str
    non_standard: bool = False
    availability_check: Optional[str] = None
    companion_ids: tuple[str, ...] = ()


def _resolve_market_type(ctx: dict) -> str:
    """根据 ticker 动态推导 market_type"""
    from app.utils.stock_utils import StockUtils, StockMarket

    market = StockUtils.identify_stock_market(ctx.get("ticker", ""))
    if market == StockMarket.HONG_KONG:
        return "hk"
    elif market == StockMarket.US:
        return "us"
    return "cn"


def _kb_injection_key(ctx: dict) -> str:
    """知识库注入台账键（task_id::agent_key，报告落板后引用校验用）"""
    return f"{ctx.get('task_id', '')}::{ctx.get('agent_key', '')}"


def _lazy_import(module_path: str, func_name: str) -> Callable:
    """延迟导入数据源函数，避免循环依赖

    保留 _lazy_module / _lazy_func_name 元信息，
    以便调用方通过 inspect 检测真实函数的异步属性。
    """
    _real_fn = None

    def wrapper(*args, **kwargs):
        nonlocal _real_fn
        if _real_fn is None:
            import importlib

            mod = importlib.import_module(module_path)
            _real_fn = getattr(mod, func_name)
        return _real_fn(*args, **kwargs)

    wrapper.__name__ = func_name
    wrapper.__qualname__ = func_name
    wrapper.__doc__ = ""
    wrapper._lazy_module = module_path
    wrapper._lazy_func_name = func_name
    return wrapper


def resolve_real_fn(fn: Callable) -> Callable:
    """获取 lazy wrapper 背后的真实函数（供 inspect 检测异步属性等）"""
    if hasattr(fn, "_lazy_module"):
        import importlib

        mod = importlib.import_module(fn._lazy_module)
        return getattr(mod, fn._lazy_func_name)
    return fn


def _build_registry() -> List[DatasourceSpec]:
    """构建全量数据源注册表（延迟导入避免循环依赖）"""
    _M = "app.engine.tools.datasources"

    return [
        # ── 标准数据域 ──
        DatasourceSpec(
            tool_id="daily_quotes",
            display_name="日线行情",
            domains=["daily_quotes"],
            markets=["CN", "HK", "US"],
            fn=_lazy_import(f"{_M}.market", "get_stock_data"),
            inject_args={"symbol": "ticker", "start_date": lambda ctx: horizon_start_date(ctx, "daily_quotes"), "end_date": "trade_date"},
            description="股票日线行情数据（开盘价、最高价、最低价、收盘价、成交量）",
        ),
        DatasourceSpec(
            tool_id="technical_analysis",
            display_name="本地技术指标与交易日校验",
            domains=["daily_quotes", "trade_calendar"],
            markets=["CN"],
            fn=_lazy_import(f"{_M}.market", "get_stock_technical_snapshot"),
            inject_args={"symbol": "ticker", "analysis_date": "trade_date", "analysis_horizon": lambda ctx: get_horizon_profile(ctx.get("analysis_horizon")).key},
            description="同源历史日线计算MA/MACD/RSI/BOLL/KDJ/WR，聚合周月线，按交易日历识别真实缺口",
        ),
        DatasourceSpec(
            tool_id="intraday_quotes",
            display_name="分钟级行情",
            domains=["intraday_quotes"],
            markets=["CN"],
            fn=_lazy_import(f"{_M}.market", "get_stock_data_minutes"),
            inject_args={
                "symbol": "ticker",
                "market_type": _resolve_market_type,
                "freq": "30min",
                "analysis_date": "trade_date",
            },
            description="分钟级 K 线数据（1min/5min/15min/30min/60min；窗口截止到分析日）",
        ),
        DatasourceSpec(
            tool_id="market_quotes",
            display_name="指数行情",
            domains=["market_quotes"],
            markets=["CN", "HK", "US"],
            fn=_lazy_import(f"{_M}.market", "get_index_data"),
            inject_args={"symbol": lambda ctx: {"CN": "000001.SH", "HK": "^HSI", "US": "^GSPC"}.get(_resolve_market_type(ctx).upper(), "000001.SH"), "end_date": "trade_date"},
            description="明确市场/交易所身份的基准指数，不能用个股报价冒充",
        ),
        DatasourceSpec(
            tool_id="financial_data",
            display_name="财务报表",
            domains=["financial_data"],
            markets=["CN", "HK", "US"],
            fn=_lazy_import(f"{_M}.fundamentals", "get_company_performance_unified"),
            inject_args={"symbol": "ticker", "data_type": "indicators", "start_date": lambda ctx: horizon_start_date(ctx, "financial_data"), "end_date": "trade_date", "analysis_horizon": lambda ctx: get_horizon_profile(ctx.get("analysis_horizon")).key},
            description="公司业绩预告、快报、财务指标、利润表、资产负债表、现金流量表",
            companion_ids=("official_announcements",),
        ),
        DatasourceSpec(
            tool_id="fundamentals",
            display_name="基本面综合",
            domains=["daily_quotes", "financial_data", "basic_info"],
            markets=["CN", "HK", "US"],
            fn=_lazy_import(f"{_M}.fundamentals", "get_stock_fundamentals"),
            inject_args={"symbol": "ticker", "current_date": "trade_date", "analysis_horizon": lambda ctx: get_horizon_profile(ctx.get("analysis_horizon")).key},
            description="基本面综合分析（价格走势 + 财务数据 + 公司信息）",
            companion_ids=("official_announcements",),
        ),
        DatasourceSpec(
            tool_id="news",
            display_name="新闻数据",
            domains=["news"],
            markets=["CN", "HK", "US"],
            fn=_lazy_import(f"{_M}.news", "get_stock_news"),
            inject_args={"symbol": "ticker", "max_results": 15, "end_date": "trade_date"},
            description="股票相关新闻（标题、来源、时间、摘要、情绪）",
        ),
        DatasourceSpec(
            tool_id="official_announcements",
            display_name="可核验官方公告与正文摘录",
            domains=["news"],
            markets=["CN"],
            fn=_lazy_import(f"{_M}.news", "get_stock_official_notices"),
            inject_args={"symbol": "ticker", "end_date": "trade_date"},
            description="免费官方披露，保留日期、链接、正文获取状态；无记录不等于无重大事项",
        ),
        DatasourceSpec(
            tool_id="sentiment",
            display_name="情绪分析",
            domains=["news"],
            markets=["CN", "HK", "US"],
            fn=_lazy_import(f"{_M}.sentiment", "get_stock_sentiment"),
            inject_args={"symbol": "ticker", "current_date": "trade_date"},
            description="基于新闻情感标签的市场情绪统计（正面/负面/中性计数与评分）",
        ),
        # ── 知识库（stock_news 语料向量化检索，公共事实与记忆区分开） ──
        DatasourceSpec(
            tool_id="knowledge_base",
            display_name="知识库检索",
            domains=["news"],
            markets=["CN", "HK", "US"],
            fn=_lazy_import(f"{_M}.knowledge", "get_knowledge_base_brief"),
            inject_args={
                "symbol": "ticker",
                "current_date": "trade_date",
                "injection_key": _kb_injection_key,
            },
            description=(
                "stock_news 语料向量检索（按 symbol+分析日期预检索 top-K，"
                "publish_ts<=as_of 防前视；引用以 [序号] 标注并校验）"
            ),
        ),
        # ── CN 特定数据域（已纳入标准 DataInterface 域） ──
        DatasourceSpec(
            tool_id="china_market",
            display_name="市场概览",
            domains=["market_quotes"],
            markets=["CN"],
            fn=_lazy_import(f"{_M}.china_market", "get_china_market_overview"),
            inject_args={"date": "trade_date"},
            description="A股市场指数 + 板块涨跌概览",
        ),
        DatasourceSpec(
            tool_id="dragon_tiger",
            display_name="龙虎榜",
            domains=["dragon_tiger"],
            markets=["CN"],
            fn=_lazy_import(f"{_M}.china_market", "get_dragon_tiger_inst"),
            inject_args={"symbol": "ticker", "trade_date": "last_completed_trade_date_compact"},
            description="龙虎榜机构明细",
        ),
        DatasourceSpec(
            tool_id="block_trade",
            display_name="大宗交易",
            domains=["block_trade"],
            markets=["CN"],
            fn=_lazy_import(f"{_M}.china_market", "get_block_trade"),
            inject_args={"symbol": "ticker", "start_date": "start_date_30d", "end_date": "trade_date"},
            description="大宗交易数据",
        ),
        DatasourceSpec(
            tool_id="money_flow",
            display_name="资金流向",
            domains=["money_flow"],
            markets=["CN"],
            fn=_lazy_import(f"{_M}.capital_flow", "get_money_flow"),
            inject_args={"symbol": "ticker", "query_type": "stock", "start_date": "start_date_30d", "end_date": "trade_date"},
            description="个股资金流向数据",
            companion_ids=("capital_context",),
        ),
        DatasourceSpec(
            tool_id="margin_trade",
            display_name="融资融券",
            domains=["margin_trading"],
            markets=["CN"],
            fn=_lazy_import(f"{_M}.capital_flow", "get_margin_trade"),
            inject_args={"data_type": "margin", "symbol": "ticker", "start_date": "start_date_30d", "end_date": "trade_date"},
            description="融资融券数据",
        ),
        DatasourceSpec(
            tool_id="daily_indicators",
            display_name="估值指标",
            domains=["daily_indicators"],
            markets=["CN", "HK", "US"],
            fn=_lazy_import(f"{_M}.market", "get_stock_indicators"),
            inject_args={"symbol": "ticker", "start_date": "start_date_30d", "end_date": "trade_date"},
            description="股票估值指标（PE/PB/PS/市值/换手率等）",
            companion_ids=("valuation_history",),
        ),
        DatasourceSpec(
            tool_id="basic_info",
            display_name="基本信息",
            domains=["basic_info"],
            markets=["CN", "HK", "US"],
            fn=_lazy_import(f"{_M}.fundamentals", "get_stock_basic_info"),
            inject_args={"symbol": "ticker"},
            description="股票基本信息（名称、行业、上市日期等）",
        ),
        DatasourceSpec(
            tool_id="valuation_history", display_name="历史估值实际交易日摘要",
            domains=["daily_indicators", "trade_calendar"], markets=["CN"],
            fn=_lazy_import(f"{_M}.evidence", "get_valuation_history"),
            inject_args={"symbol": "ticker", "end_date": "trade_date", "sessions": lambda ctx: get_horizon_profile(ctx.get("analysis_horizon")).valuation_sessions},
            description="按分析周期读取120/250/500个实际交易日的同口径PE/PB覆盖与样本分位，不提供目标价",
        ),
        DatasourceSpec(
            tool_id="capital_context", display_name="资金分析公开辅助证据",
            domains=["daily_indicators", "news"], markets=["CN"],
            fn=_lazy_import(f"{_M}.evidence", "get_capital_context"),
            inject_args={"symbol": "ticker", "end_date": "trade_date"},
            description="日期核验的流通市值与同一新闻文本子样本，不替代真实盘口或社交情绪",
        ),
    ]


def expand_evidence_dependencies(specs: List[DatasourceSpec], market: str) -> List[DatasourceSpec]:
    """Read-only runtime expansion; do not rewrite user agent specs or seeds."""
    expanded = list(specs)
    seen = {spec.tool_id for spec in expanded}
    registry = {spec.tool_id: spec for spec in _build_registry()}
    for spec in expanded:
        for tool_id in spec.companion_ids:
            companion = registry.get(tool_id)
            if companion and tool_id not in seen and market in companion.markets:
                expanded.append(companion)
                seen.add(tool_id)
    return expanded


DATASOURCE_REGISTRY: List[DatasourceSpec] = _build_registry()

# 按工具 ID 查找的索引
_TOOL_ID_INDEX: Dict[str, DatasourceSpec] = {s.tool_id: s for s in DATASOURCE_REGISTRY}


def get_spec_by_id(tool_id: str) -> Optional[DatasourceSpec]:
    """按 tool_id 查找数据源规格"""
    return _TOOL_ID_INDEX.get(tool_id)


def get_specs_by_ids(tool_ids: List[str]) -> List[DatasourceSpec]:
    """按 tool_id 列表查找，忽略不存在的"""
    return [_TOOL_ID_INDEX[tid] for tid in tool_ids if tid in _TOOL_ID_INDEX]
