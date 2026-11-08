"""A 股龙虎榜/大宗交易同步链路回归测试。"""

from pathlib import Path

import pandas as pd
import pytest
import yaml

from app.data.processor.validator import Validator
from app.data.scheduler.jobs.base.sync_job import _INITIAL_LOOKBACK_DAYS
from app.data.sources.cn.akshare.adapter import AKShareCNAdapter
from app.data.storage.mongo.repositories.key_spec import build_filter, get_domain_key
from app.routers.cn.sync import _VALID_SYNC_DOMAINS


def test_cn_manual_sync_accepts_event_domains():
    assert "dragon_tiger" in _VALID_SYNC_DOMAINS
    assert "block_trade" in _VALID_SYNC_DOMAINS


def test_cn_schedule_registers_event_domains():
    schedule_path = (
        Path(__file__).parents[2]
        / "app/data/scheduler/jobs/cn/schedule.yaml"
    )
    schedule = yaml.safe_load(schedule_path.read_text(encoding="utf-8"))

    assert schedule["dragon_tiger"]["cron"] == "0 18 * * 1-5"
    assert schedule["block_trade"]["cron"] == "30 18 * * 1-5"
    assert schedule["dragon_tiger"]["source"] == "akshare"
    assert schedule["block_trade"]["source"] == "akshare"


def test_event_domains_use_bounded_initial_incremental_window():
    assert _INITIAL_LOOKBACK_DAYS == {
        "dragon_tiger": 60,
        "block_trade": 60,
    }


def test_akshare_adapter_maps_current_dragon_tiger_columns():
    records = AKShareCNAdapter().adapt_dragon_tiger(pd.DataFrame([{
        "代码": "000011",
        "名称": "深物业A",
        "上榜日": "2026-09-01",
        "解读": "1家机构卖出",
        "收盘价": 9.16,
        "涨跌幅": -9.0367,
        "龙虎榜净买额": -46_085_510.86,
        "龙虎榜买入额": 53_578_781.34,
        "龙虎榜卖出额": 99_664_292.20,
        "上榜原因": "日跌幅偏离值达到7%的前5只证券",
    }]))

    assert len(records) == 1
    record = records[0].to_db_doc()
    assert record["symbol"] == "000011"
    assert record["trade_date"] == "2026-09-01"
    assert record["reason"] == "日跌幅偏离值达到7%的前5只证券"
    assert record["buy_amount"] == pytest.approx(53_578_781.34)
    assert record["sell_amount"] == pytest.approx(99_664_292.20)
    assert record["net_amount"] == pytest.approx(-46_085_510.86)
    valid, errors = Validator().validate([record], "dragon_tiger", "CN")
    assert valid == [record]
    assert errors == []


def test_akshare_adapter_maps_current_block_trade_columns_without_rescaling():
    records = AKShareCNAdapter().adapt_block_trade(pd.DataFrame([{
        "交易日期": "2026-08-28",
        "证券代码": "000007",
        "证券简称": "全新好",
        "成交价": 12.6,
        "成交量": 400_000,
        "成交额": 5_040_000,
        "买方营业部": "买方营业部A",
        "卖方营业部": "卖方营业部B",
    }]))

    assert len(records) == 1
    record = records[0].to_db_doc()
    assert record["symbol"] == "000007"
    assert record["trade_date"] == "2026-08-28"
    assert record["name"] == "全新好"
    assert record["volume"] == 400_000
    assert record["amount"] == 5_040_000
    assert record["trade_seq"] == 1
    valid, errors = Validator().validate([record], "block_trade", "CN")
    assert valid == [record]
    assert errors == []


def test_block_trade_keeps_separately_disclosed_identical_trades():
    row = {
        "交易日期": "2026-08-28",
        "证券代码": "000007",
        "证券简称": "全新好",
        "成交价": 12.6,
        "成交量": 400_000,
        "成交额": 5_040_000,
        "买方营业部": "买方营业部A",
        "卖方营业部": "卖方营业部B",
    }
    records = AKShareCNAdapter().adapt_block_trade(pd.DataFrame([row, row]))
    docs = [record.to_db_doc() for record in records]

    assert [doc["trade_seq"] for doc in docs] == [1, 2]
    assert build_filter("block_trade", docs[0]) != build_filter(
        "block_trade", docs[1]
    )
    assert get_domain_key("block_trade") == [
        "symbol", "trade_date", "buyer", "seller",
        "price", "volume", "amount", "trade_seq",
    ]


def test_validator_rejects_event_records_without_trade_date():
    valid, errors = Validator().validate(
        [{"symbol": "000011", "reason": "上榜原因"}],
        "dragon_tiger",
        "CN",
    )
    assert valid == []
    assert errors[0]["issues"] == ["缺少必填字段: trade_date"]
