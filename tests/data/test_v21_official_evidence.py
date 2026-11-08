"""Pure evidence parsing and opt-in real official-source I/O; no mocks."""
import asyncio
import os

import pandas as pd
import pytest

from app.data.sources.cn.akshare.adapter import AKShareCNAdapter
from app.data.sources.cn.shared.margin_eligibility import parse_sse_adjustment, parse_sse_index, parse_sse_snapshot, parse_szse_eligibility
from app.data.sources.cn.shared.margin_series import derive_margin_repayments, fetch_margin_eligibility, fetch_margin_range, validate_szse_json_amount_units, validate_szse_display_amount
from app.data.sources.cn.shared.official_notices import classify_filing, fetch_official_notices, select_filings, select_note_excerpts


@pytest.mark.parametrize("title", ["2025年年度报告摘要", "2026年半年度报告摘要", "2025年年度报告（英文版）",
                                    "2026年第三季度报告", "关于2025年年度报告的问询函回复"])
def test_non_full_documents_are_not_full_filings(title):
    assert classify_filing(title) is None


def test_latest_full_correction_is_selected_without_recent_notice_displacement():
    # Disclosed filing titles/dates as records, including a future revision trap.
    rows = [
        {"title": "2025年年度报告", "publish_time": "2026-04-28 00:00:00", "url": "annual"},
        {"title": "2025年年度报告（修订版）", "publish_time": "2026-05-08 00:00:00", "url": "revised"},
        {"title": "2026年半年度报告", "publish_time": "2026-08-29 00:00:00", "url": "semiannual"},
        {"title": "关于2026年半年度报告的更正公告", "publish_time": "2026-09-03 00:00:00", "url": "correction"},
        {"title": "2026年半年度报告（修订版）", "publish_time": "2026-10-09 00:00:00", "url": "future"},
        {"title": "2026年半年度报告摘要", "publish_time": "2026-09-04 00:00:00", "url": "summary"},
    ]
    rows += [{"title": "关于召开临时股东会的通知", "publish_time": "2026-10-07 00:00:00", "url": str(i)} for i in range(30)]
    result = select_filings(rows, "2026-10-07")
    assert [row["url"] for row in result] == ["semiannual", "revised"]
    assert result[0]["filing_metadata"]["revision_status"] == "correction_notice_requires_reconciliation"
    assert result[0]["filing_metadata"]["correction_notices"][0]["url"] == "correction"


def test_multiple_financial_topics_on_one_page_are_not_dropped():
    text = "单位：人民币元\n受限货币资金 1,250,000.00\n" + "其他附注内容\n" * 200 + "对外担保余额 8,000,000.00\n"
    content, meta = select_note_excerpts(["目录\n受限货币资金......90\n担保......110", text, "担保续表\n报告期末余额 8,000,000.00"], 250, False)
    assert "restricted_cash" in meta["topics_with_excerpts"] and "guarantees" in meta["topics_with_excerpts"]
    assert "1,250,000.00" in content and "8,000,000.00" in content
    assert all(item["page"] != 1 for item in meta["evidence"])
    assert not meta["scan_complete"] and "page_or_time_budget_exhausted" in meta["limitations"]
    assert meta["excerpt_chars"] <= 16000
    assert "tables_not_structurally_verified" in meta["limitations"]


def test_scanned_pdf_pages_remain_explicitly_uncovered():
    _, meta = select_note_excerpts(["", "", "2025年年度报告"], 3, True)
    assert meta["blank_or_scanned_pages"] == [1, 2, 3]
    assert not meta["ocr_performed"] and meta["missing_topics"]


def szse_payload():
    # Official 1834_xxpl response fields, 002241 at 2026-09-30.
    return [{"metadata": {"catalogid": "1834_xxpl", "tabkey": "tab1", "subname": "2026-09-30", "recordcount": 1,
                            "conditions": [{"name": "txtDate", "defaultValue": "2026-09-30"}]},
             "data": [{"zqdm": "002241", "rzbz": "Y", "rjbz": "Y", "jrkrz": "Y", "jrkrj": "Y"}]}]


def test_szse_uses_server_result_date_not_echoed_query():
    payload = szse_payload()
    assert parse_szse_eligibility(payload, "002241", "2026-09-30")["verified"]
    payload[0]["metadata"]["subname"] = "2026-10-08"
    assert not parse_szse_eligibility(payload, "002241", "2026-09-30")["verified"]


def test_szse_comparison_accepts_official_json_billion_unit_labels():
    # Exact 1837_xxpl labels observed in the official 2026-09-30 response.
    validate_szse_json_amount_units({"cols": {"jrrzmr": "融资买入额<br>(亿元)", "jrrzye": "融资余额<br>(亿元)"}})


@pytest.mark.parametrize("label", ["融资买入额<br>(元)", "融资买入额<br>(万元)", "融资买入额", "融券余额<br>(亿元)", "", "融资买入额(亿元/元)"])
def test_szse_comparison_rejects_missing_or_conflicting_json_units(label):
    with pytest.raises(ValueError, match="monetary units unverified"):
        validate_szse_json_amount_units({"cols": {"jrrzmr": label, "jrrzye": "融资余额<br>(亿元)"}})


def test_szse_comparison_requires_both_unit_labels():
    with pytest.raises(ValueError, match="monetary units unverified"):
        validate_szse_json_amount_units({"cols": {"jrrzmr": "融资买入额<br>(亿元)"}})


@pytest.mark.parametrize("raw,display", [(4477997254, "44.77"), (52440011, "0.52"), (466272580, "4.66"),
                                        (2331862904, "23.31"), (0, "0.00"), (1000000, "0.01")])
def test_szse_raw_yuan_matches_truncated_official_display(raw, display):
    # First four amounts are the actual official 2026-09-30 XLSX/JSON pairs.
    validate_szse_display_amount(raw, display)


@pytest.mark.parametrize("raw,display", [(4477997254, "44.78"), (4477997254, "44.76"),
                                        (1000000, "0.00"), (-1, "0.00"), ("nan", "0.00"),
                                        (4477997254, "44.770"), (4477997254, "44")])
def test_szse_display_other_interval_or_unverified_precision_is_rejected(raw, display):
    with pytest.raises(ValueError):
        validate_szse_display_amount(raw, display)


@pytest.mark.parametrize("mutation", ["empty", "other_symbol", "incomplete", "unknown_flag", "wrong_catalog"])
def test_szse_missing_or_conflicting_scope_cannot_be_verified(mutation):
    payload = szse_payload()
    if mutation == "empty":
        payload[0]["data"] = []
        payload[0]["metadata"]["recordcount"] = 0
    elif mutation == "other_symbol":
        payload[0]["data"][0]["zqdm"] = "000001"
    elif mutation == "incomplete":
        payload[0]["metadata"]["recordcount"] = 2
    elif mutation == "unknown_flag":
        payload[0]["data"][0]["rzbz"] = ""
    else:
        payload[0]["metadata"]["catalogid"] = "unrelated"
    assert not parse_szse_eligibility(payload, "002241", "2026-09-30")["verified"]


def test_sse_snapshot_never_claims_a_historical_date():
    text = "tableData={staticDate:\"2026-10-08 07:30:00\", list:[['1','600363 ','联创光电'],['2','600875 ','东方电气']]};"
    assert parse_sse_snapshot(text, "2026-10-08")["verified"]
    assert not parse_sse_snapshot(text, "2026-09-30")["verified"]
    assert not parse_sse_snapshot(text.replace("['2'", "['3'"), "2026-10-08")["verified"]
    assert not parse_sse_snapshot(text[:-2], "2026-10-08")["verified"]


def test_sse_adjustment_pagination_rejects_missing_rows_and_unsorted_dates():
    html = "<dd><span>2026-09-29</span><a href='/a.shtml'>关于融资融券标的证券调整的公告</a></dd><dd><span>2026-09-08</span><a href='/b.shtml'>关于融资融券标的证券调整的公告</a></dd><script>createPageHTML('paging',1,1,'s_list','shtml',2);</script>"
    assert len(parse_sse_index(html, 1)) == 2
    with pytest.raises(ValueError):
        parse_sse_index(html.replace("shtml',2", "shtml',3"), 1)
    with pytest.raises(ValueError):
        parse_sse_index(html.replace("2026-09-29", "2026-09-01"), 1)
    with pytest.raises(ValueError):
        parse_sse_index(html, 2)


def test_sse_adjustment_uses_body_effective_date_not_publication_date():
    html = "<p>2026年9月30日，联创光电（证券代码：600363）被实施其他风险警示。根据《上海证券交易所融资融券交易实施细则》第三十二条规定，本所于2026年9月30日起将以上证券调出融资融券标的证券名单。</p><p>2026年9月29日</p>"
    result = parse_sse_adjustment(html, "https://www.sse.com.cn/disclosure/magin/announcement/ssereport/c/c_20260929_10833673.shtml")
    assert result["verified"] and result["effective_date"] == "2026-09-30"
    assert result["codes"] == {"600363"} and not result["eligible"]
    assert not parse_sse_adjustment("<p>600363融资交易风险提示</p>", "url")["verified"]


def margin_rows():
    return pd.DataFrame([
        {"证券代码": "002241", "trade_date": "2026-09-29", "source_response_date": "2026-09-29", "exchange_source": "SZSE", "monetary_unit": "元", "融资余额": 1000.0, "融资买入额": 50.0},
        {"证券代码": "002241", "trade_date": "2026-09-30", "source_response_date": "2026-09-30", "exchange_source": "SZSE", "monetary_unit": "元", "融资余额": 1020.0, "融资买入额": 50.0},
    ])


def test_balance_identity_remains_separate_from_reported_repayment():
    result = derive_margin_repayments(margin_rows(), ["2026-09-29", "2026-09-30"])
    assert result.iloc[1]["margin_repay_amount_derived"] == 30
    schema = AKShareCNAdapter().adapt_margin_trading(result)[1]
    assert schema.margin_repay_amount is None
    assert schema.margin_repay_amount_derived == 30
    assert schema.margin_repay_derivation_status == "derived_not_reported"
    assert schema.margin_repay_derivation["previous_trade_date"] == "2026-09-29"


@pytest.mark.parametrize("mutation", ["negative", "unit", "source", "date", "duplicate", "reported_conflict", "calendar_gap"])
def test_unverified_balance_identity_is_not_published(mutation):
    rows = margin_rows()
    dates = ["2026-09-29", "2026-09-30"]
    if mutation == "negative":
        rows.loc[1, "融资余额"] = 2000
    elif mutation == "unit":
        rows.loc[0, "monetary_unit"] = "万元"
    elif mutation == "source":
        rows.loc[0, "exchange_source"] = "SSE"
    elif mutation == "date":
        rows.loc[0, "source_response_date"] = "2026-09-28"
    elif mutation == "duplicate":
        rows = pd.concat([rows, rows.iloc[:1]], ignore_index=True)
    elif mutation == "reported_conflict":
        rows.loc[1, "融资偿还额"] = 40
    else:
        dates = ["2026-09-29", "2026-09-29-gap", "2026-09-30"]
    result = derive_margin_repayments(rows, dates)
    assert result.iloc[1]["margin_repay_amount_derived"] is None


def test_adapter_preserves_public_filing_evidence_metadata():
    raw = {"symbol": "603232", "title": "2026年半年度报告", "publish_time": "2026-08-29", "content": "节选",
           "filing_metadata": {"report_kind": "semiannual", "full_report": True},
           "pdf_metadata": {"missing_topics": ["guarantees"]}, "discovery_metadata": {"as_of": "2026-10-07"}}
    result = AKShareCNAdapter().adapt_news([raw])[0]
    assert result.filing_metadata == raw["filing_metadata"]
    assert result.pdf_metadata == raw["pdf_metadata"]
    assert result.discovery_metadata == raw["discovery_metadata"]


@pytest.mark.skipif(os.environ.get("RUN_FREE_DATA_LIVE") != "1", reason="opt-in real public exchange/CNINFO I/O")
def test_live_official_v21_discovery_and_eligibility():
    rows = asyncio.run(fetch_official_notices("603232", 3, "2026-10-07"))
    filings = [row for row in rows if row.get("filing_metadata")]
    assert {row["filing_metadata"]["report_kind"] for row in filings} == {"annual", "semiannual"}
    assert all(row["publish_time"][:10] <= "2026-10-07" and "摘要" not in row["title"] for row in filings)
    assert all(row.get("pdf_metadata") for row in filings)
    szse = asyncio.run(fetch_margin_eligibility("002241", "2026-09-30"))
    assert szse["status"] == "success" and szse["evidence_date"] == "2026-09-30"
    sse = asyncio.run(fetch_margin_eligibility("603232", "2026-10-07"))
    assert sse["status"] in {"success", "unknown", "not_applicable"}
    if sse["status"] == "not_applicable":
        assert sse["reason"] != "absent_from_scoped_quarterly_list_not_proof"
    positive = asyncio.run(fetch_margin_eligibility("600875", "2026-09-30"))
    assert positive["status"] == "success" and positive["eligible"] is True
    assert positive["evidence_date"] == "2026-09-30"
    assert positive["baseline_date"] == "2026-07-13"
    assert positive["reason"] == "official_quarterly_membership_with_bounded_adjustment_chain"


@pytest.mark.skipif(os.environ.get("RUN_FREE_DATA_LIVE") != "1", reason="opt-in real exchange data + Mongo trade calendar")
def test_live_szse_independent_derived_repayment_series():
    async def run():
        import app.core.database as database
        from app.core.config import settings
        from app.data.storage.mongo.client import reset_client
        from isolated_database import require_isolated_database
        require_isolated_database()
        reset_client()
        await database.init_database()
        try:
            return await fetch_margin_range("002241", "2026-09-23", "2026-09-30")
        finally:
            await database.close_database()
            reset_client()
    frame = asyncio.run(run())
    assert len(frame) == 5
    records = AKShareCNAdapter().adapt_margin_trading(frame)
    assert all(row.margin_repay_amount is None for row in records)
    assert all(row.margin_repay_amount_derived is not None for row in records)
    assert all(row.margin_repay_derivation_status == "derived_not_reported" for row in records)
