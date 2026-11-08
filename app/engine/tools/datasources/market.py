"""
市场行情工具 - 股票行情、分钟级数据、指数数据

所有数据通过 DataInterface 统一获取，走 FallbackRouter 自动降级。
"""

import json
import logging
from typing import Optional
from datetime import datetime, timedelta

from app.utils.time_utils import now_utc, get_current_date, get_current_date_compact
from app.engine.tools.common.tool_result import success_result, error_result, format_tool_result, ErrorCodes
from app.engine.tools.common.format import format_result
from app.engine.tools.common.data_access import read_with_refresh, render_data_quality
from app.engine.tools.common.param_validators import MCPToolValidators

logger = logging.getLogger(__name__)


def _frequency_minutes(value: object) -> Optional[int]:
    """把 ``1``/``1min``/``30m`` 等历史频率写法归一为分钟数。"""
    text = str(value or "").strip().lower()
    for suffix in ("minutes", "minute", "mins", "min", "m"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    try:
        minutes = int(text)
    except (TypeError, ValueError):
        return None
    return minutes if minutes in {1, 5, 15, 30, 60} else None


def _prepare_intraday_bars(raw, requested_freq: str):
    """优先返回目标周期；只有更细粒度数据时在本地聚合 OHLCV。"""
    import pandas as pd

    if raw is None:
        return None
    frame = pd.DataFrame(raw) if isinstance(raw, list) else raw.copy()
    if frame is None or frame.empty or "datetime" not in frame.columns:
        return None

    target = _frequency_minutes(requested_freq) or 30
    frame = frame.copy()
    if "data_source" in frame.columns:
        # The former Tencent minute/query adapter fabricated highs/lows from
        # last-price samples. Do not treat retained legacy rows as native bars.
        kind = frame["ohlc_kind"] if "ohlc_kind" in frame else pd.Series(None, index=frame.index, dtype=object)
        frame = frame[(frame["data_source"] != "tencent") | (kind == "native_bar")].copy()
        if frame.empty:
            return None
    if "freq" not in frame.columns:
        frame["freq"] = "1min"
    frame["_freq_minutes"] = frame["freq"].map(_frequency_minutes)
    stamps = pd.to_datetime(frame["datetime"], errors="coerce")
    minutes = stamps.dt.hour * 60 + stamps.dt.minute
    valid_session = ((minutes >= 570) & (minutes <= 690)) | ((minutes > 780) & (minutes <= 900))
    frame = frame[stamps.notna() & valid_session].copy()
    frame = frame.sort_values("datetime").drop_duplicates(["datetime", "freq", "data_source"] if "data_source" in frame else ["datetime", "freq"], keep="last")
    exact = frame[frame["_freq_minutes"] == target]
    if not exact.empty:
        if "data_source" in exact:
            source = max(exact.groupby("data_source"), key=lambda pair: (pair[1]["datetime"].max(), len(pair[1])))[0]
            exact = exact[exact["data_source"] == source]
        return exact.drop(columns=["_freq_minutes"])

    candidates = frame[
        frame["_freq_minutes"].notna()
        & (frame["_freq_minutes"] < target)
        & (target % frame["_freq_minutes"] == 0)
    ]
    if candidates.empty:
        return None

    # 使用可用的最细周期，避免不同基础周期或不同供应商重复累加成交量。
    base = int(candidates["_freq_minutes"].min())
    candidates = candidates[candidates["_freq_minutes"] == base].copy()
    if "data_source" in candidates:
        source = max(candidates.groupby("data_source"), key=lambda pair: (pair[1]["datetime"].max(), len(pair[1])))[0]
        candidates = candidates[candidates["data_source"] == source].copy()
    candidates["_samples"] = 1
    candidates["_dt"] = pd.to_datetime(candidates["datetime"], errors="coerce")
    candidates = candidates.dropna(subset=["_dt"]).sort_values("_dt")
    # CN session boundaries: minute timestamps label bar ends. Do not create
    # lunch/after-hours bars, or split a 60-minute candle at 10:00.
    minute_of_day = candidates["_dt"].dt.hour * 60 + candidates["_dt"].dt.minute
    morning = (minute_of_day >= 570) & (minute_of_day <= 690)
    afternoon = (minute_of_day > 780) & (minute_of_day <= 900)
    candidates = candidates[morning | afternoon].copy()
    if candidates.empty:
        return None

    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column in candidates.columns:
            candidates[column] = pd.to_numeric(candidates[column], errors="coerce")

    group_columns = [column for column in ("symbol", "market", "data_source") if column in candidates.columns]
    minutes = candidates["_dt"].dt.hour * 60 + candidates["_dt"].dt.minute
    session_start = minutes.where(minutes <= 690, 780).where(minutes > 690, 570)
    # Tencent labels the opening auction 09:30. Include it in the first bar
    # rather than dropping real opening volume or making a 09:30 bucket.
    elapsed = (minutes - session_start).clip(lower=1)
    bucket_end = session_start + ((elapsed + target - 1) // target) * target
    candidates["_bucket"] = candidates["_dt"].dt.normalize() + pd.to_timedelta(bucket_end, unit="min")
    aggregations = {}
    for column, op in (
        ("open", "first"), ("high", "max"), ("low", "min"),
        ("close", "last"), ("volume", lambda values: values.sum(min_count=1)),
        ("amount", lambda values: values.sum(min_count=1)),
    ):
        if column in candidates.columns:
            aggregations[column] = op
    if not aggregations:
        return None
    aggregations["_samples"] = "sum"

    aggregated = (
        candidates.groupby(group_columns + ["_bucket"], dropna=False, as_index=False)
        .agg(aggregations)
        .rename(columns={"_bucket": "datetime"})
    )
    aggregated["datetime"] = aggregated["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
    aggregated["freq"] = f"{target}min"
    aggregated["bar_complete"] = aggregated.pop("_samples") >= target // base
    aggregated["base_freq_minutes"] = base
    if {"open", "close"}.issubset(aggregated.columns):
        aggregated["pct_chg"] = (
            (aggregated["close"] / aggregated["open"] - 1) * 100
        ).round(4)
    return aggregated


def _d8(date_str: str) -> str:
    """YYYYMMDD → YYYY-MM-DD（已是 dashed 或其他形态原样返回），供统一校验。"""
    s = str(date_str).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _validate_window(start_date: Optional[str], end_date: Optional[str], as_of: Optional[str]) -> Optional[str]:
    """校验取数窗口；不合法返回回传给模型的错误文本，合法返回 None。"""
    v = MCPToolValidators.validate_date_window(
        _d8(start_date or ""), _d8(end_date or ""), as_of=_d8(as_of) if as_of else None
    )
    if not v:
        return v.error_message
    return None


def _to_json_str(data) -> str:
    """将 DataFrame 或其他类型转为 JSON 字符串"""
    import pandas as pd

    if isinstance(data, pd.DataFrame):
        return data.to_json(orient="records", force_ascii=False, date_format="iso")
    return str(data)


def _read_daily_quotes(market: str, symbol: str, start_date: str, end_date: str):
    """统一读取日 K 线数据（CN/HK/US 共用），库空时按需刷新后重读"""
    result = read_with_refresh(
        market, "daily_quotes", symbol=symbol, start_date=start_date, end_date=end_date
    )
    raw = result.get("data") if result else None
    if not raw:
        return None
    import pandas as pd

    return pd.DataFrame(raw) if isinstance(raw, list) and raw else raw


_MARKET_MAP = {
    "is_china": ("CN", "A股"),
    "is_hk": ("HK", "港股"),
    "is_us": ("US", "美股"),
}


def get_stock_technical_snapshot(symbol: str, analysis_date: str, analysis_horizon: str = "short") -> str:
    """Derive bounded technical context through the standard data facade only."""
    from app.core.async_utils import run_async
    from app.data.core.interface import DataInterface
    from app.utils.stock_utils import StockUtils

    if not StockUtils.get_market_info(symbol).get("is_china"):
        return format_tool_result(error_result(ErrorCodes.DATA_FETCH_ERROR, "本地交易日校验目前仅支持A股"))
    try:
        snapshot = run_async(DataInterface.get_instance().technical_snapshot(
            "CN", symbol, analysis_date,
            analysis_horizon=analysis_horizon,
        ))
        if snapshot.get("status") != "success":
            return format_tool_result(error_result(ErrorCodes.DATA_FETCH_ERROR, "没有可用的历史日线，无法计算技术指标"))
        return format_tool_result(success_result(json.dumps(snapshot, ensure_ascii=False, allow_nan=False)))
    except Exception as exc:
        logger.warning("本地技术快照获取失败: %s", exc)
        return format_tool_result(error_result(ErrorCodes.DATA_FETCH_ERROR, str(exc)))


def get_stock_data(
    symbol: str,
    market_type: str = "cn",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    current_date: Optional[str] = None,
) -> str:
    """
    获取股票行情数据。

    返回开盘价、最高价、最低价、收盘价、成交量等行情数据。

    Args:
        symbol: 股票代码，如 "000001.SZ"(A股)、"AAPL"(美股)、"00700.HK"(港股)
        market_type: 市场类型: "cn"(A股)、"us"(美股)、"hk"(港股)，默认自动推断
        start_date: 开始日期，格式 YYYY-MM-DD 或 YYYYMMDD，默认基准日 1 个月前
        end_date: 结束日期，格式 YYYY-MM-DD 或 YYYYMMDD，默认基准日
        current_date: 分析基准日（注入 trade_date）；窗口锚定并截止到该日，防止历史分析前视

    Returns:
        JSON 格式的 ToolResult
    """
    try:
        from app.utils.stock_utils import StockUtils

        market_info = StockUtils.get_market_info(symbol)

        market_key = None
        for attr, (mkt, name) in _MARKET_MAP.items():
            if market_info.get(attr):
                market_key = mkt
                break

        if not market_key:
            if market_type == "hk":
                market_key = "HK"
            elif market_type == "us":
                market_key = "US"
            else:
                market_key = "CN"

        if not start_date:
            if current_date:
                start_date = (datetime.strptime(_d8(current_date), "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
            else:
                start_date = (now_utc() - timedelta(days=30)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = current_date or get_current_date()
        elif current_date and _d8(end_date) > _d8(current_date):
            end_date = current_date

        err = _validate_window(start_date, end_date, current_date)
        if err:
            return format_tool_result(
                error_result(ErrorCodes.INVALID_PARAM, err, suggestion="请修正日期窗口后重试")
            )

        data = _read_daily_quotes(market_key, symbol, start_date, end_date)

        if data is not None:
            return format_tool_result(success_result(_to_json_str(data)))
        else:
            return format_tool_result(
                error_result(
                    ErrorCodes.DATA_FETCH_ERROR,
                    f"有界免费链路未取得 {symbol} 所请求的日线行情；详见本次数据质量记录。",
                    suggestion="继续其余分析，披露缺失的价格或窗口；不填零、不反复要求手动同步。",
                )
            )

    except Exception as e:
        logger.error(f"get_stock_data failed: {e}", exc_info=True)
        return format_tool_result(error_result(ErrorCodes.DATA_FETCH_ERROR, f"获取股票数据失败: {str(e)}"))


def get_stock_data_minutes(
    market_type: str,
    symbol: str,
    start_datetime: Optional[str] = None,
    end_datetime: Optional[str] = None,
    freq: str = "30min",
    analysis_date: Optional[str] = None,
    current_date: Optional[str] = None,
) -> str:
    """
    获取分钟级 K 线数据。

    Args:
        market_type: 市场类型，目前仅支持 "cn"
        symbol: 股票代码，如 "600519.SH"
        start_datetime: 开始时间，格式 YYYY-MM-DD HH:mm:ss，默认基准日 1 天前
        end_datetime: 结束时间，格式 YYYY-MM-DD HH:mm:ss，默认基准日收盘
        freq: 频率，支持 "1min"、"5min"、"15min"、"30min"、"60min"，默认 "30min"
        current_date: 分析基准日（注入 trade_date），窗口截止到该日，防止历史分析前视

    Returns:
        JSON 格式的 ToolResult
    """
    try:
        from app.core.async_utils import run_async
        from app.data.core.interface import DataInterface
        from app.data.core.market import get_market_timezone
        market = str(market_type).upper()
        if market not in {"CN", "HK", "US"}:
            market = {"A股": "CN", "港股": "HK", "美股": "US"}.get(market_type, "CN")
        if market != "CN":
            return format_tool_result(error_result(ErrorCodes.UNKNOWN_MARKET, "分钟聚合目前只校验A股交易时段"))
        context = run_async(DataInterface.get_instance().analysis_context(
            market, (analysis_date or current_date) or now_utc().astimezone(get_market_timezone(market)).strftime("%Y-%m-%d")
        ))
        start_datetime = start_datetime or context["intraday_start"]
        end_datetime = end_datetime or context["intraday_end"]
        if not start_datetime or not end_datetime:
            return format_tool_result(error_result(ErrorCodes.DATA_FETCH_ERROR, "交易日历不足，无法确定可靠分钟窗口"))
        end_datetime = min(end_datetime, context["intraday_end"] or end_datetime)

        symbol = symbol
        result = read_with_refresh(
            market,
            "intraday_quotes",
            symbol=symbol,
            start_date=start_datetime,
            end_date=end_datetime,
            filters={"expected_dates": context["recent_trade_dates"]},
            # 不按目标周期预过滤：库里只有更细周期时，下面会在本地聚合；
            # 预先过滤会把可用的 1min 数据排除掉。
        )
        intraday_data = result.get("data") if result else None
        if intraday_data:
            data = _prepare_intraday_bars(intraday_data, freq)
            if data is not None:
                data = data[data["datetime"].astype(str) <= end_datetime]
            if data is not None and not data.empty:
                title = f"{symbol} {freq} Data"
                return format_tool_result(success_result(
                    json.dumps({"analysis_context": context, "quality": result.get("quality"), "bars": data.to_dict(orient="records")}, ensure_ascii=False, default=str)
                ))

        return format_tool_result(
            error_result(
                ErrorCodes.DATA_FETCH_ERROR, f"无法获取分钟级行情数据: {symbol}。" + render_data_quality(result),
                suggestion="分钟窗口不可用时继续其他证据，不能编造盘中走势或把日线当分时。",
            )
        )
    except Exception as e:
        logger.error(f"get_stock_data_minutes failed: {e}", exc_info=True)
        return format_tool_result(error_result(ErrorCodes.DATA_FETCH_ERROR, str(e)))


def get_index_data(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    current_date: Optional[str] = None,
) -> str:
    """
    获取指数日线行情。

    Args:
        symbol: 指数代码，如 "000001.SH"
        start_date: 开始日期，格式 YYYYMMDD，默认基准日 3 个月前
        end_date: 结束日期，格式 YYYYMMDD，默认基准日
        current_date: 分析基准日（注入 trade_date），窗口截止到该日，防止历史分析前视

    Returns:
        JSON 格式的 ToolResult
    """
    try:
        from app.utils.stock_utils import StockUtils

        anchor = _d8(current_date).replace("-", "") if current_date else None
        if not end_date:
            end_date = anchor or get_current_date_compact()
        elif anchor and end_date > anchor:
            end_date = anchor
        if not start_date:
            if anchor:
                start_date = (datetime.strptime(anchor, "%Y%m%d") - timedelta(days=90)).strftime("%Y%m%d")
            else:
                start_date = (now_utc() - timedelta(days=90)).strftime("%Y%m%d")

        err = _validate_window(start_date, end_date, anchor)
        if err:
            return format_tool_result(
                error_result(ErrorCodes.INVALID_PARAM, err, suggestion="请修正日期窗口后重试")
            )

        # 动态推导市场
        market_info = StockUtils.get_market_info(symbol)
        market = "CN"
        for attr, (mkt, _) in _MARKET_MAP.items():
            if market_info.get(attr):
                market = mkt
                break

        # Index identity is exchange-qualified; bare 000001 is a Shenzhen stock.
        symbol = symbol.upper()
        if market == "CN" and symbol not in {"000001.SH", "399001.SZ", "399006.SZ", "000300.SH"}:
            return format_tool_result(error_result(ErrorCodes.INVALID_PARAM, "必须使用明确指数与交易所代码，不能传个股代码"))
        result = read_with_refresh(
            market, "market_quotes", symbol=symbol,
            end_date=end_date if "-" in end_date else f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}",
            filters={"asset_type": "index", "exchange": symbol.split(".")[-1]} if market == "CN" else {},
        )
        index_data = result.get("data") if result else None
        if not index_data and market == "CN":
            result = read_with_refresh(market, "daily_quotes", symbol=symbol,
                start_date=start_date, end_date=end_date,
                filters={"asset_type": "index", "exchange": symbol.split(".")[-1], "period": "daily"})
            index_data = result.get("data") if result else None
        if index_data:
            import pandas as pd

            data = pd.DataFrame(index_data) if isinstance(index_data, list) else index_data
            return format_tool_result(success_result(format_result(data, f"Index: {symbol}")))

        return format_tool_result(
            error_result(
                ErrorCodes.DATA_FETCH_ERROR, f"指数行情数据暂不可用: {symbol}。" + render_data_quality(result),
                suggestion="继续其他已验证证据，不用个股价格替代指数。",
            )
        )
    except Exception as e:
        logger.error(f"get_index_data failed: {e}", exc_info=True)
        return format_tool_result(error_result(ErrorCodes.DATA_FETCH_ERROR, str(e)))


def get_stock_indicators(
    symbol: str,
    market_type: str = "cn",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    current_date: Optional[str] = None,
) -> str:
    """
    获取股票估值指标数据。

    返回 PE、PB、PS、总市值、流通市值、换手率等每日估值指标。

    Args:
        symbol: 股票代码，如 "000001.SZ"(A股)、"AAPL"(美股)、"00700.HK"(港股)
        market_type: 市场类型: "cn"(A股)、"us"(美股)、"hk"(港股)，默认自动推断
        start_date: 开始日期，格式 YYYY-MM-DD 或 YYYYMMDD，默认基准日 1 个月前
        end_date: 结束日期，格式 YYYY-MM-DD 或 YYYYMMDD，默认基准日
        current_date: 分析基准日（注入 trade_date），窗口截止到该日，防止历史分析前视

    Returns:
        JSON 格式的 ToolResult
    """
    try:
        from app.utils.stock_utils import StockUtils

        market_info = StockUtils.get_market_info(symbol)

        market_key = None
        for attr, (mkt, name) in _MARKET_MAP.items():
            if market_info.get(attr):
                market_key = mkt
                break

        if not market_key:
            if market_type == "hk":
                market_key = "HK"
            elif market_type == "us":
                market_key = "US"
            else:
                market_key = "CN"

        if not start_date:
            if current_date:
                start_date = (datetime.strptime(_d8(current_date), "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
            else:
                start_date = (now_utc() - timedelta(days=30)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = current_date or get_current_date()
        elif current_date and _d8(end_date) > _d8(current_date):
            end_date = current_date

        err = _validate_window(start_date, end_date, current_date)
        if err:
            return format_tool_result(
                error_result(ErrorCodes.INVALID_PARAM, err, suggestion="请修正日期窗口后重试")
            )

        result = read_with_refresh(
            market_key, "daily_indicators", symbol=symbol, start_date=start_date, end_date=end_date
        )
        raw = result.get("data") if result else None

        if raw:
            import pandas as pd

            data = pd.DataFrame(raw) if isinstance(raw, list) and raw else raw
            extra = ""
            if market_key == "CN":
                from app.core.async_utils import run_async
                from app.data.core.interface import DataInterface
                try:
                    relative = run_async(DataInterface.get_instance().relative_volume_context(
                        market_key, symbol, end_date))
                    extra = "\n本地独立指标（不是供应商量比，也不回填 volume_ratio）：" + json.dumps(relative, ensure_ascii=False)
                except Exception as exc:
                    logger.warning("本地相对成交量不可用 %s: %s", symbol, exc)
                    extra = "\nrelative_volume_5d 未取得完整六交易日证据，不能计算；不影响其他数据分析。"
            return format_tool_result(success_result(render_data_quality(result) + extra + "\n\n"
                + format_result(data, f"{symbol} Indicators")))

        return format_tool_result(
            error_result(
                ErrorCodes.DATA_FETCH_ERROR,
                f"无法获取 {symbol} 的估值指标数据。" + render_data_quality(result),
                suggestion="有界免费取数结束，披露缺项并继续其他分析；不得把来源未证实的PE当作TTM。",
            )
        )

    except Exception as e:
        logger.error(f"get_stock_indicators failed: {e}", exc_info=True)
        return format_tool_result(error_result(ErrorCodes.DATA_FETCH_ERROR, str(e)))
