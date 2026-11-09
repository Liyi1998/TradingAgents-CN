"""CN 复权纯函数契约：真实 DataFrame、明确数学预期，不访问数据源或数据库。"""

import numpy as np
import pandas as pd
import pytest

from app.data.processor.adjustment_contract import (
    align_price_adjustments,
    normalize_adjustment_events,
)
from app.data.sources.cn.akshare.adapter import AKShareCNAdapter
from app.data.sources.cn.baostock.adapter import BaoStockCNAdapter


AS_OF = "2026-09-30"
SYMBOL = "600000"
CONTRACT = "cn_price_multiplier_v1"
BASIS = "raw_price_multiplier"


def _raw_events(source="akshare"):
    if source == "akshare":
        return pd.DataFrame({
            "date": ["2026-09-01", "2026-09-29"],
            "qfq_factor": [4.0, 2.0],
            # 后复权列不能被误当成前复权乘数。
            "hfq_factor": [17.0, 19.0],
        })
    return pd.DataFrame({
        "dividOperateDate": ["2026-09-01", "2026-09-29"],
        "foreAdjustFactor": [0.4, 0.8],
        "backAdjustFactor": [17.0, 19.0],
    })


def _events(source="akshare", symbol=SYMBOL):
    return normalize_adjustment_events(
        _raw_events(source), source=source, symbol=symbol, end_date=AS_OF,
    )


def _quotes(symbol=SYMBOL):
    return pd.DataFrame({
        "symbol": [symbol] * 4,
        "trade_date": ["2026-09-25", "2026-09-28", "2026-09-29", AS_OF],
        "close": [100.0, 100.0, 50.0, 51.0],
        "price_basis": ["raw"] * 4,
        # 行情源与复权源不同是正常路径，不能要求两者相同。
        "data_source": ["duckdb"] * 4,
        "volume": [1000, 1200, 2000, 2200],
    }, index=pd.Index([41, 7, 93, 12], name="original_row"))


def _assert_unknown(frame):
    assert frame["close_adj"].isna().all()
    assert pd.api.types.is_float_dtype(frame["close_adj"])
    assert frame["_adjustment_status"].notna().all()


@pytest.mark.parametrize("source", ["akshare", "baostock"])
def test_normalizes_source_specific_factors_to_raw_price_multipliers(source):
    """AK 的除数取倒数，Bao 的 fore 是乘数；截止日归一化后都应为 0.5/1。"""
    got = _events(source).sort_values("trade_date")
    assert got["trade_date"].tolist() == ["2026-09-01", "2026-09-29"]
    assert got["adj_factor"].tolist() == pytest.approx([0.5, 1.0])
    assert got["symbol"].eq(SYMBOL).all()
    assert got["data_source"].eq(source).all()
    assert got["adjustment_contract"].eq(CONTRACT).all()
    assert got["adjustment_basis"].eq(BASIS).all()
    assert got["record_type"].eq("event").all()
    assert got["coverage_end"].eq(AS_OF).all()
    assert got["adjustment_method"].notna().all()
    assert got["adjustment_method"].astype(str).str.len().gt(0).all()
    assert got["adjustment_series_id"].notna().all()
    assert got["adjustment_series_id"].nunique() == 1


def test_normalization_keeps_only_latest_start_anchor_and_in_range_events():
    raw = pd.DataFrame({
        "date": ["2026-01-01", "2026-09-29", "2026-09-20", "2026-10-09"],
        "qfq_factor": [8.0, 2.0, 4.0, 0.25],
    })
    got = normalize_adjustment_events(
        raw, source="akshare", symbol=SYMBOL,
        start_date="2026-09-25", end_date=AS_OF,
    ).sort_values("trade_date")
    assert got["trade_date"].tolist() == ["2026-09-20", "2026-09-29"]
    assert got["adj_factor"].tolist() == pytest.approx([0.5, 1.0])


def test_event_on_start_date_is_itself_the_anchor():
    raw = pd.DataFrame({
        "date": ["2026-09-01", "2026-09-25", "2026-09-29"],
        "qfq_factor": [8.0, 4.0, 2.0],
    })
    got = normalize_adjustment_events(
        raw, source="akshare", symbol=SYMBOL,
        start_date="2026-09-25", end_date=AS_OF,
    ).sort_values("trade_date")
    assert got["trade_date"].tolist() == ["2026-09-25", "2026-09-29"]
    assert got["adj_factor"].tolist() == pytest.approx([0.5, 1.0])


def test_future_event_does_not_change_historical_normalization():
    raw = pd.concat([
        _raw_events(),
        pd.DataFrame([{"date": "2026-10-09", "qfq_factor": 0.01}]),
    ], ignore_index=True)
    got = normalize_adjustment_events(
        raw, source="akshare", symbol=SYMBOL, end_date=AS_OF,
    ).sort_values("trade_date")
    assert got["trade_date"].max() <= AS_OF
    assert got["adj_factor"].tolist() == pytest.approx([0.5, 1.0])


@pytest.mark.parametrize("source", ["akshare", "baostock"])
@pytest.mark.parametrize("bad_factor", [0.0, -1.0, np.nan, np.inf, -np.inf])
def test_normalization_rejects_invalid_source_factors(source, bad_factor):
    raw = _raw_events(source)
    column = "qfq_factor" if source == "akshare" else "foreAdjustFactor"
    raw.loc[raw.index[0], column] = bad_factor
    with pytest.raises(ValueError):
        normalize_adjustment_events(
            raw, source=source, symbol=SYMBOL, end_date=AS_OF,
        )


def test_normalization_rejects_conflicting_events_on_the_same_date():
    raw = _raw_events()
    conflict = raw.iloc[:1].copy()
    conflict["qfq_factor"] = 3.0
    with pytest.raises(ValueError):
        normalize_adjustment_events(
            pd.concat([raw, conflict], ignore_index=True),
            source="akshare", symbol=SYMBOL, end_date=AS_OF,
        )


def test_normalization_deduplicates_equivalent_events():
    raw = _raw_events()
    got = normalize_adjustment_events(
        pd.concat([raw, raw.iloc[:1]], ignore_index=True),
        source="akshare", symbol=SYMBOL, end_date=AS_OF,
    ).sort_values("trade_date")
    assert got["trade_date"].tolist() == ["2026-09-01", "2026-09-29"]
    assert got["adj_factor"].tolist() == pytest.approx([0.5, 1.0])


@pytest.mark.parametrize("symbol", ["000000", "60000", "600000.SH"])
def test_normalization_requires_an_explicit_six_digit_nonplaceholder_symbol(symbol):
    with pytest.raises(ValueError):
        normalize_adjustment_events(
            _raw_events(), source="akshare", symbol=symbol, end_date=AS_OF,
        )


def test_unchanged_event_history_keeps_snapshot_identity_across_coverage_extension():
    original = _events()
    extended = normalize_adjustment_events(
        _raw_events(), source="akshare", symbol=SYMBOL, end_date="2026-10-08",
    )
    assert set(original["adjustment_series_id"]) == set(extended["adjustment_series_id"])
    assert extended["coverage_end"].eq("2026-10-08").all()


@pytest.mark.parametrize("source", ["akshare", "baostock"])
def test_normalization_does_not_mutate_source_frame(source):
    raw = _raw_events(source)
    raw.index = pd.Index([11, 4], name="provider_row")
    original = raw.copy(deep=True)
    normalize_adjustment_events(
        raw, source=source, symbol=SYMBOL,
        start_date="2026-09-25", end_date=AS_OF,
    )
    pd.testing.assert_frame_equal(raw, original)


@pytest.mark.parametrize(("source", "adapter_type"), [
    ("akshare", AKShareCNAdapter),
    ("baostock", BaoStockCNAdapter),
])
def test_real_adapter_schema_serialization_preserves_normalized_event_evidence(source, adapter_type):
    factors = _events(source)
    records = [schema.to_db_doc() for schema in adapter_type().adapt_adj_factors(factors)]
    assert len(records) == len(factors)
    fields = [
        "symbol", "market", "data_source", "trade_date", "adj_factor",
        "fore_adj_factor", "back_adj_factor", "raw_factor",
        "adjustment_basis", "adjustment_contract", "adjustment_method",
        "adjustment_series_id", "record_type", "coverage_end",
    ]
    for original, record in zip(factors.to_dict("records"), records):
        assert {field: record[field] for field in fields} == {
            field: original[field] for field in fields
        }


@pytest.mark.parametrize(("source", "adapter_type"), [
    ("akshare", AKShareCNAdapter),
    ("baostock", BaoStockCNAdapter),
])
def test_real_adapter_does_not_certify_unconverted_provider_or_legacy_rows(source, adapter_type):
    with pytest.raises(ValueError):
        adapter_type().adapt_adj_factors(_raw_events(source))
    legacy = _events(source).drop(columns=["adjustment_contract"])
    with pytest.raises(ValueError):
        adapter_type().adapt_adj_factors(legacy)


@pytest.mark.parametrize("source", ["akshare", "baostock"])
def test_sparse_events_expand_and_split_price_is_continuous(source):
    quotes = _quotes()
    got = align_price_adjustments(quotes, _events(source), as_of=AS_OF)
    assert got["close_adj"].tolist() == pytest.approx([50.0, 50.0, 50.0, 51.0])
    assert got["close_adj"].pct_change().iloc[2] == pytest.approx(0.0)
    assert got["_adjustment_source"].eq(source).all()
    assert got["_adjustment_contract"].eq(CONTRACT).all()
    assert got["_adjustment_status"].notna().all()
    pd.testing.assert_frame_equal(got[quotes.columns], quotes)


def test_cash_dividend_drop_is_removed_without_reversing_factor_direction():
    quotes = _quotes().iloc[[0, 2]].copy()
    quotes["close"] = [10.0, 9.0]
    raw = pd.DataFrame({
        "date": ["2026-09-01", "2026-09-29"],
        "qfq_factor": [10.0 / 9.0, 1.0],
    })
    factors = normalize_adjustment_events(
        raw, source="akshare", symbol=SYMBOL, end_date=AS_OF,
    )
    got = align_price_adjustments(quotes, factors, as_of=AS_OF)
    assert got["close_adj"].tolist() == pytest.approx([9.0, 9.0])


def test_quote_order_duplicate_index_and_multiple_symbols_are_preserved():
    first = _quotes().iloc[[2, 0]].copy()
    second = _quotes("000001").iloc[[3, 1]].copy()
    second["close"] = [20.0, 40.0]
    quotes = pd.concat([first.iloc[:1], second.iloc[:1], first.iloc[1:], second.iloc[1:]])
    quotes.index = pd.Index([7, 7, 3, 9], name="original_row")
    factors = pd.concat([_events(), _events("baostock", "000001")], ignore_index=True)
    got = align_price_adjustments(quotes, factors, as_of=AS_OF)
    pd.testing.assert_frame_equal(got[quotes.columns], quotes)
    assert got["close_adj"].tolist() == pytest.approx([50.0, 20.0, 50.0, 20.0])
    assert got["_adjustment_source"].tolist() == ["akshare", "baostock", "akshare", "baostock"]


def test_identical_duplicate_events_do_not_invalidate_or_multiply_quotes():
    factors = _events()
    factors = pd.concat([factors, factors.iloc[:1]], ignore_index=True)
    got = align_price_adjustments(_quotes(), factors, as_of=AS_OF)
    assert len(got) == 4
    assert got["close_adj"].tolist() == pytest.approx([50.0, 50.0, 50.0, 51.0])


def test_conflicting_duplicate_event_makes_price_adjustment_unknown():
    factors = _events()
    conflict = factors.iloc[:1].copy()
    conflict["adj_factor"] = 0.75
    got = align_price_adjustments(
        _quotes(), pd.concat([factors, conflict], ignore_index=True), as_of=AS_OF,
    )
    _assert_unknown(got)


@pytest.mark.parametrize(("column", "value"), [
    ("data_source", "baostock"),
    ("adjustment_series_id", "different-snapshot"),
    ("adjustment_contract", "cn_price_multiplier_v0"),
    ("adjustment_basis", "price_divisor"),
])
def test_mixed_source_snapshot_contract_or_basis_is_not_spliced(column, value):
    factors = _events()
    factors.loc[factors.index[-1], column] = value
    _assert_unknown(align_price_adjustments(_quotes(), factors, as_of=AS_OF))


@pytest.mark.parametrize("column", [
    "data_source", "adjustment_series_id", "adjustment_contract",
    "adjustment_basis", "coverage_end",
])
def test_legacy_factor_rows_without_required_provenance_are_unknown(column):
    factors = _events().drop(columns=[column])
    _assert_unknown(align_price_adjustments(_quotes(), factors, as_of=AS_OF))


def test_old_contract_version_is_not_accepted_even_when_consistent():
    factors = _events().assign(adjustment_contract="cn_price_multiplier_v0")
    _assert_unknown(align_price_adjustments(_quotes(), factors, as_of=AS_OF))


@pytest.mark.parametrize("coverage", ["2026-09-29", None, "not-a-date"])
def test_unproven_or_insufficient_coverage_does_not_extend_to_asof(coverage):
    factors = _events().assign(coverage_end=coverage)
    _assert_unknown(align_price_adjustments(_quotes(), factors, as_of=AS_OF))


def test_one_relevant_event_with_short_coverage_invalidates_the_series():
    factors = _events()
    factors.loc[factors.index[0], "coverage_end"] = "2026-09-29"
    _assert_unknown(align_price_adjustments(_quotes(), factors, as_of=AS_OF))


def test_future_conflicting_events_are_ignored_before_validation():
    factors = _events()
    future = factors.iloc[-1:].copy()
    future["trade_date"] = "2026-10-09"
    future["adj_factor"] = -99.0
    future["data_source"] = "unrelated-future-source"
    future["adjustment_contract"] = "future-contract"
    future["adjustment_series_id"] = "future-snapshot"
    got = align_price_adjustments(
        _quotes(), pd.concat([factors, future], ignore_index=True), as_of=AS_OF,
    )
    assert got["close_adj"].tolist() == pytest.approx([50.0, 50.0, 50.0, 51.0])
    assert got["_adjustment_source"].eq("akshare").all()


def test_historical_asof_rebases_snapshot_without_future_split_influencing_price():
    """9 月 30 日快照含 29 日拆分，28 日历史复权价仍应以当时最后事件为 1。"""
    quotes = _quotes().iloc[:2].copy()
    from_later_snapshot = align_price_adjustments(quotes, _events(), as_of="2026-09-28")
    historical_factors = normalize_adjustment_events(
        _raw_events(), source="akshare", symbol=SYMBOL, end_date="2026-09-28",
    )
    from_historical_snapshot = align_price_adjustments(
        quotes, historical_factors, as_of="2026-09-28",
    )
    assert from_later_snapshot["close_adj"].tolist() == pytest.approx([100.0, 100.0])
    pd.testing.assert_series_equal(
        from_later_snapshot["close_adj"], from_historical_snapshot["close_adj"],
    )


def test_obsolete_events_before_latest_anchor_do_not_mix_snapshots():
    factors = _events()
    obsolete = factors.iloc[:1].copy()
    obsolete["trade_date"] = "2025-01-01"
    obsolete["data_source"] = "old-source"
    obsolete["adjustment_series_id"] = "old-snapshot"
    obsolete["adjustment_contract"] = "old-contract"
    got = align_price_adjustments(
        _quotes(), pd.concat([obsolete, factors], ignore_index=True), as_of=AS_OF,
    )
    assert got["close_adj"].tolist() == pytest.approx([50.0, 50.0, 50.0, 51.0])


@pytest.mark.parametrize("basis", [None, "", "qfq", "hfq", "unknown"])
def test_unproven_quote_basis_is_not_inferred_from_duckdb_source(basis):
    quotes = _quotes().assign(price_basis=basis)
    _assert_unknown(align_price_adjustments(quotes, _events(), as_of=AS_OF))


def test_missing_quote_basis_is_not_inferred_from_source():
    quotes = _quotes().drop(columns=["price_basis"])
    _assert_unknown(align_price_adjustments(quotes, _events(), as_of=AS_OF))


@pytest.mark.parametrize("basis", ["qfq", "hfq"])
def test_verified_already_adjusted_quotes_are_not_multiplied_again(basis):
    quotes = _quotes().assign(
        price_basis=basis, adjustment_contract="cn_adjusted_quotes_v1",
        adjustment_as_of=AS_OF, close=[49.0, 50.0, 50.0, 51.0],
    )
    # 完整的已复权行情契约自足，不应再消费另一张事件表。
    got = align_price_adjustments(quotes, _events().assign(adj_factor=np.nan), as_of=AS_OF)
    pd.testing.assert_series_equal(got["close_adj"], quotes["close"], check_names=False)
    pd.testing.assert_frame_equal(got[quotes.columns], quotes)
    assert got["_adjustment_source"].eq("duckdb").all()
    assert got["_adjustment_contract"].eq("cn_adjusted_quotes_v1").all()


@pytest.mark.parametrize("defect", [
    "different_horizon", "mixed_horizon", "mixed_source", "missing_contract",
])
def test_already_adjusted_quotes_require_one_verified_source_and_asof(defect):
    quotes = _quotes().assign(
        price_basis="qfq", adjustment_contract="cn_adjusted_quotes_v1",
        adjustment_as_of=AS_OF,
    )
    if defect == "different_horizon":
        quotes["adjustment_as_of"] = "2026-09-29"
    elif defect == "mixed_horizon":
        quotes.loc[quotes.index[0], "adjustment_as_of"] = "2026-09-29"
    elif defect == "mixed_source":
        quotes.loc[quotes.index[0], "data_source"] = "akshare"
    else:
        quotes = quotes.drop(columns=["adjustment_contract"])
    _assert_unknown(align_price_adjustments(quotes, _events(), as_of=AS_OF))


@pytest.mark.parametrize("basis", ["raw", "qfq"])
@pytest.mark.parametrize("invalid_close", [True, "100", 0.0, -1.0, np.nan, np.inf, -np.inf])
def test_raw_and_already_adjusted_paths_reject_invalid_close_values(basis, invalid_close):
    quotes = _quotes().assign(price_basis=basis, close=invalid_close)
    if basis == "qfq":
        quotes = quotes.assign(
            adjustment_contract="cn_adjusted_quotes_v1", adjustment_as_of=AS_OF,
        )
    _assert_unknown(align_price_adjustments(quotes, _events(), as_of=AS_OF))


@pytest.mark.parametrize("conflicting_close", [False, True])
def test_duplicate_quote_dates_make_the_entire_symbol_unknown(conflicting_close):
    quotes = _quotes()
    duplicate = quotes.iloc[:1].copy()
    if conflicting_close:
        duplicate["close"] = 999.0
    quotes = pd.concat([quotes, duplicate])
    got = align_price_adjustments(quotes, _events(), as_of=AS_OF)
    _assert_unknown(got)
    pd.testing.assert_frame_equal(got[quotes.columns], quotes)


@pytest.mark.parametrize("bad_multiplier", [0.0, -1.0, np.nan, np.inf, -np.inf])
def test_invalid_relevant_multiplier_does_not_become_identity(bad_multiplier):
    factors = _events()
    factors.loc[factors.index[0], "adj_factor"] = bad_multiplier
    _assert_unknown(align_price_adjustments(_quotes(), factors, as_of=AS_OF))


def test_missing_factors_do_not_fall_back_to_raw_close():
    _assert_unknown(align_price_adjustments(_quotes(), _events().iloc[:0], as_of=AS_OF))


def test_missing_first_quote_anchor_makes_the_entire_symbol_unknown():
    factors = _events().iloc[1:].copy()
    _assert_unknown(align_price_adjustments(_quotes(), factors, as_of=AS_OF))


def test_alignment_does_not_mutate_quotes_or_factors():
    quotes = _quotes()
    factors = _events()
    quote_snapshot = quotes.copy(deep=True)
    factor_snapshot = factors.copy(deep=True)
    align_price_adjustments(quotes, factors, as_of=AS_OF)
    pd.testing.assert_frame_equal(quotes, quote_snapshot)
    pd.testing.assert_frame_equal(factors, factor_snapshot)


def test_missing_auxiliary_factor_is_explicit_null_when_replacing_cn_contract():
    factors = normalize_adjustment_events(
        _raw_events().drop(columns=["hfq_factor"]), source="akshare",
        symbol=SYMBOL, end_date=AS_OF,
    )
    records = [item.to_db_doc() for item in AKShareCNAdapter().adapt_adj_factors(factors)]
    assert all("back_adj_factor" in item and item["back_adj_factor"] is None
               for item in records)


def test_provider_cannot_relabel_another_stock_as_the_requested_symbol():
    raw = _raw_events("baostock").assign(code="sz.000001")
    with pytest.raises(ValueError):
        normalize_adjustment_events(raw, source="baostock", symbol=SYMBOL,
                                    end_date=AS_OF)
