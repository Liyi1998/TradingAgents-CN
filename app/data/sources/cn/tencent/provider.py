"""腾讯财经 A 股 Provider。"""

import asyncio

import pandas as pd

from app.data.sources.base.provider import BaseProvider


class TencentCNProvider(BaseProvider):
    def __init__(self):
        super().__init__(name="tencent", market="CN")

    async def connect(self) -> bool:
        self.connected = True
        return True

    def is_available(self) -> bool:
        return True

    async def get_stock_list(self, **kwargs) -> pd.DataFrame:
        symbol = str(kwargs.get("symbol") or "")
        if not symbol or symbol == "__all__":
            raise NotImplementedError("腾讯行情仅支持按股票查询基础信息")
        from .api.market_quotes import fetch_market_quotes

        return await fetch_market_quotes([symbol])

    async def get_market_quotes(self, symbols=None, **kwargs) -> pd.DataFrame:
        from .api.market_quotes import fetch_market_quotes

        requested = [str(item) for item in (symbols or []) if str(item)]
        if not requested or "__all__" in requested:
            from app.data.sources.cn.akshare.api.stock_basic import fetch_stock_list

            stock_frame = await fetch_stock_list()
            code_col = "code" if "code" in stock_frame.columns else "symbol"
            requested = stock_frame[code_col].astype(str).str.zfill(6).tolist()

        # 控制 URL 长度并限制并发，避免单次 5000+ 代码请求被网关拒绝。
        chunks = [requested[i : i + 100] for i in range(0, len(requested), 100)]
        semaphore = asyncio.Semaphore(5)

        async def _fetch_chunk(chunk):
            async with semaphore:
                return await fetch_market_quotes(chunk)

        frames = await asyncio.gather(
            *[_fetch_chunk(chunk) for chunk in chunks], return_exceptions=True
        )
        valid = [frame for frame in frames if isinstance(frame, pd.DataFrame) and not frame.empty]
        return pd.concat(valid, ignore_index=True) if valid else pd.DataFrame()

    async def get_daily_indicators_batch(
        self, trade_date: str, **kwargs
    ) -> pd.DataFrame:
        """腾讯全市场行情本身包含 PE/PB/市值/换手率，无需额外接口。"""
        return await self.get_market_quotes(["__all__"])

    async def get_intraday_quotes(
        self, symbol: str, start_date: str, end_date: str, **kwargs
    ) -> pd.DataFrame:
        from .api.intraday_quotes import fetch_intraday_quotes

        frame = await fetch_intraday_quotes(symbol)
        if frame.empty:
            return frame
        date_values = frame["datetime"].astype(str).str[:10]
        mask = pd.Series(True, index=frame.index)
        if start_date:
            mask &= date_values >= start_date[:10]
        if end_date:
            mask &= date_values <= end_date[:10]
        return frame[mask].copy()
