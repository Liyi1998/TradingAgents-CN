"""腾讯财经 A 股 HTTP API 封装。"""

from .market_quotes import fetch_market_quotes

__all__ = ["fetch_market_quotes"]
