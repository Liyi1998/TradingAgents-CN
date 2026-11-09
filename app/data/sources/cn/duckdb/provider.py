"""通过金策智算只读 HTTP API 访问现有 DuckDB 行情库。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import AsyncIterator

import httpx
import pandas as pd

from app.core.env import get_env
from app.data.sources.base.exceptions import (
    DataFormatError,
    DataNotFoundError,
    DataSourceUnavailableError,
)
from app.data.sources.base.mappers import (
    map_http_status_to_error,
    map_network_exception,
)
from app.data.sources.base.provider import BaseProvider

logger = logging.getLogger(__name__)

_SOURCE = "duckdb"
_DOMAIN = "daily_quotes"


def _default_base_url() -> str:
    in_docker = str(get_env("DOCKER_CONTAINER", "")).lower() in {
        "1",
        "true",
        "yes",
    }
    return "http://host.docker.internal:8000" if in_docker else "http://127.0.0.1:8000"


def _full_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if "." in value:
        return value
    if value.startswith(("SH", "SZ", "BJ")) and len(value) == 8:
        return f"{value[2:]}.{value[:2]}"
    if value.startswith(("4", "8")):
        suffix = "BJ"
    elif value.startswith(("6", "9")):
        suffix = "SH"
    else:
        suffix = "SZ"
    return f"{value.zfill(6)}.{suffix}"


def _lookback_start(start_date: str) -> str:
    """多取前十天，用于计算请求区间首日的昨收和涨跌幅。"""
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        if start.year <= 1970:
            return start_date
        return (start - timedelta(days=10)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return start_date


class JinceDuckDBCNProvider(BaseProvider):
    """金策智算 DuckDB 的只读 HTTP Provider。

    TradingAgents-CN 不直接打开 88GB DuckDB 文件，避免与金策智算盘后写入
    进程争用文件锁。当前复用金策智算 ``/api/backtest/kline_data`` 接口。
    """

    def __init__(self):
        super().__init__(name=_SOURCE, market="CN")
        self.base_url = str(
            get_env("JINCE_API_BASE_URL", _default_base_url()) or _default_base_url()
        ).rstrip("/")
        self.timeout = float(get_env("JINCE_API_TIMEOUT_SECONDS", 20) or 20)

    async def connect(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{self.base_url}/api/duckdb/coverage")
                response.raise_for_status()
                payload = response.json()
            self.connected = payload.get("status") == "success"
        except Exception as exc:
            logger.debug("金策智算 DuckDB API 连通性检查失败: %s", exc)
            self.connected = False
        return self.connected

    def is_available(self) -> bool:
        # HTTP 服务的真实可用性由 connect/fetch 与断路器共同判断。
        return bool(self.base_url)

    async def get_coverage(self) -> dict:
        """返回 DuckDB 日线覆盖概览。"""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{self.base_url}/api/duckdb/coverage")
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise map_network_exception(exc, _SOURCE, _DOMAIN) from exc
        if payload.get("status") != "success":
            raise DataSourceUnavailableError(
                _SOURCE, _DOMAIN, str(payload.get("msg") or "DuckDB coverage 查询失败")
            )
        return payload

    async def iter_daily_quotes(
        self,
        start_date: str,
        end_date: str,
        batch_size: int = 20000,
    ) -> AsyncIterator[pd.DataFrame]:
        """通过 code+trade_date 游标分页读取全市场日线。"""
        cursor_code = ""
        cursor_date = ""
        while True:
            params = {
                "start_date": start_date,
                "end_date": end_date,
                "limit": max(100, min(int(batch_size), 50000)),
            }
            if cursor_code:
                params["cursor_code"] = cursor_code
                params["cursor_date"] = cursor_date
            try:
                timeout = httpx.Timeout(max(self.timeout, 60.0))
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.get(
                        f"{self.base_url}/api/duckdb/daily_quotes", params=params
                    )
                    if response.status_code >= 400:
                        raise map_http_status_to_error(
                            response.status_code,
                            _SOURCE,
                            _DOMAIN,
                            f"金策智算返回 HTTP {response.status_code}",
                        )
                    payload = response.json()
            except httpx.HTTPError as exc:
                raise map_network_exception(exc, _SOURCE, _DOMAIN) from exc
            except ValueError as exc:
                raise DataFormatError(
                    _SOURCE, _DOMAIN, f"批量响应不是有效 JSON: {exc}"
                ) from exc

            if payload.get("status") != "success":
                raise DataSourceUnavailableError(
                    _SOURCE, _DOMAIN, str(payload.get("msg") or "DuckDB 批量查询失败")
                )
            records = payload.get("records") or []
            if records:
                frame = pd.DataFrame(records).rename(columns={"code": "symbol"})
                if payload.get("volume_unit") == "lot" and "vol" in frame.columns:
                    frame["volume"] = pd.to_numeric(frame["vol"], errors="coerce") * 100
                elif "vol" in frame.columns:
                    frame["volume"] = pd.to_numeric(frame["vol"], errors="coerce")
                yield frame

            next_cursor = payload.get("next_cursor") or {}
            if not payload.get("has_more") or not next_cursor:
                break
            next_code = str(next_cursor.get("code") or "")
            next_date = str(next_cursor.get("trade_date") or "")
            if not next_code or not next_date or (
                next_code == cursor_code and next_date == cursor_date
            ):
                raise DataFormatError(_SOURCE, _DOMAIN, "DuckDB 分页游标未前进")
            cursor_code, cursor_date = next_code, next_date

    async def get_daily_quotes(
        self, symbol: str, start_date: str, end_date: str, **kwargs
    ) -> pd.DataFrame:
        if not symbol or symbol == "__all__":
            raise NotImplementedError("全市场日线请使用 iter_daily_quotes 分页接口")

        params = {
            "stock": _full_symbol(symbol),
            "start": _lookback_start(start_date),
            "end": end_date,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/api/backtest/kline_data", params=params
                )
                if response.status_code >= 400:
                    raise map_http_status_to_error(
                        response.status_code,
                        _SOURCE,
                        _DOMAIN,
                        f"金策智算返回 HTTP {response.status_code}",
                    )
                payload = response.json()
        except httpx.HTTPError as exc:
            raise map_network_exception(exc, _SOURCE, _DOMAIN) from exc
        except ValueError as exc:
            raise DataFormatError(_SOURCE, _DOMAIN, f"响应不是有效 JSON: {exc}") from exc

        if payload.get("status") != "success":
            raise DataSourceUnavailableError(
                _SOURCE, _DOMAIN, str(payload.get("msg") or "金策智算查询失败")
            )

        candles = payload.get("candles") or []
        if not candles:
            raise DataNotFoundError(
                _SOURCE, _DOMAIN, f"{params['stock']} {start_date}~{end_date} 无数据"
            )

        volume_map = {
            str(item.get("time")): item.get("value")
            for item in (payload.get("volumes") or [])
        }
        rows = []
        raw_symbol = str(symbol).split(".")[0].replace("SH", "").replace("SZ", "").replace("BJ", "")
        for candle in candles:
            trade_date = str(candle.get("time") or "")[:10]
            rows.append(
                {
                    "symbol": raw_symbol.zfill(6),
                    "trade_date": trade_date,
                    "open": candle.get("open"),
                    "high": candle.get("high"),
                    "low": candle.get("low"),
                    "close": candle.get("close"),
                    # 金策智算库的 vol 为手；标准 Schema 使用股。
                    "volume": (
                        float(volume_map[trade_date]) * 100
                        if volume_map.get(trade_date) is not None
                        else None
                    ),
                }
            )

        frame = pd.DataFrame(rows).sort_values("trade_date").reset_index(drop=True)
        frame["pre_close"] = frame["close"].shift(1)
        frame["change"] = frame["close"] - frame["pre_close"]
        frame["pct_chg"] = frame["change"] / frame["pre_close"] * 100
        return frame[
            (frame["trade_date"] >= start_date) & (frame["trade_date"] <= end_date)
        ].reset_index(drop=True)
