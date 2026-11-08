import pytest

from app.data.sources.cn.baostock.adapter import BaoStockCNAdapter
from app.data.sources.cn.baostock.provider import BaoStockCNProvider
from app.data.sources.base.exceptions import DataNotFoundError


def test_index_adaptation_preserves_exchange_and_stock_separation():
    rows = [{"code": "sh.000300", "date": "2026-11-05", "open": "4500", "high": "4520", "low": "4490", "close": "4510", "preclose": "4500", "volume": "1000", "amount": "5000"},
            {"code": "sz.000300", "date": "2026-11-05", "close": "10"}]
    result = BaoStockCNAdapter().adapt_index_quotes(rows)
    assert len(result) == 1 and result[0].symbol == "000300.SH"
    assert result[0].volume == 1000 and result[0].amount == 5000 and result[0].change == 10
    from app.data.processor.normalizer import Normalizer
    normalized = Normalizer().normalize_with_status(rows, "index_quotes", BaoStockCNAdapter())
    assert normalized.status == "ok" and len(normalized.records) == 1
    assert normalized.records[0]["symbol"] == "000300.SH" and normalized.records[0]["close"] == 4510


@pytest.mark.parametrize("symbol", ["000300", "899050.BJ", "000688.SH", "600000.SH"])
async def test_unsupported_indices_do_not_become_stock_quotes(symbol):
    with pytest.raises(DataNotFoundError):
        await BaoStockCNProvider().get_index_quotes(symbol, "2026-11-01", "2026-11-05")
