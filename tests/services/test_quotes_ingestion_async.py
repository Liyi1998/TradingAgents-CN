"""近实时行情采集的异步路径回归测试。"""

import pytest

from app.data.core.interface import DataInterface
from app.services.quotes_ingestion_service import QuotesIngestionService


class _AsyncDataInterface:
    async def fetch_market_quotes(self, market, preferred_sources=None):
        assert market == "CN"
        return {
            "source": "tencent",
            "data": [{"symbol": "600875", "close": 25.1}],
        }


@pytest.mark.asyncio
async def test_fetch_quotes_awaits_data_interface_on_current_loop(monkeypatch):
    fake = _AsyncDataInterface()
    monkeypatch.setattr(DataInterface, "get_instance", classmethod(lambda cls: fake))

    quotes, source = await QuotesIngestionService()._fetch_quotes_from_source(
        "tencent"
    )

    assert source == "tencent"
    assert quotes["600875"]["close"] == 25.1
