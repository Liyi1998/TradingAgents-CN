"""新浪财经 A 股 Provider。"""

import pandas as pd

from app.data.sources.base.provider import BaseProvider


class SinaCNProvider(BaseProvider):
    def __init__(self):
        super().__init__(name="sina", market="CN")

    async def connect(self) -> bool:
        self.connected = True
        return True

    def is_available(self) -> bool:
        return True

    async def get_money_flow(
        self, symbol: str, start_date: str, end_date: str, **kwargs
    ) -> pd.DataFrame:
        from .api.money_flow import fetch_money_flow

        frame = await fetch_money_flow(symbol, days=kwargs.get("days", 120))
        dates = frame["opendate"].astype(str).str[:10]
        mask = pd.Series(True, index=frame.index)
        if start_date:
            mask &= dates >= start_date[:10]
        if end_date:
            mask &= dates <= end_date[:10]
        return frame[mask].copy()
