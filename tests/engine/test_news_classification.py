from copy import deepcopy

import pytest

from app.engine.tools.datasources.news_classification import classify_news_item, sentiment_coverage
from app.engine.tools.datasources.sentiment import build_sentiment_payload


BODY = "测试股份（000503）归母净利润同比增长20%，报告期营业收入达到三亿元，财务报表已经董事会审议通过。"


def test_explicit_earnings_direction_is_auditable_not_verified_fact():
    result = classify_news_item({"title": "公司披露半年报", "content": BODY, "data_source": "official"}, target_symbol="000503")
    assert result["sentiment"] == "positive"
    assert result["sentiment_label_source"] == "local_rule"
    assert result["sentiment_label_verified"] is False
    assert result["sentiment_evidence"][0]["rule_id"] == "profit_yoy_increase"
    assert "20%" in result["sentiment_evidence"][0]["text"]


@pytest.mark.parametrize("body", [
    "净利润同比增长20%",  # title-sized body
    "预计" + BODY,
    "公司否认相关增长消息。" + BODY,
    BODY + "测试股份（000503）报告期净利润同比下降15%。",
    "公司发布日常经营公告，公告详细介绍了产品生产和市场销售情况，董事会通过了有关事项。",
])
def test_ambiguous_unlabeled_news_is_unknown_not_neutral(body):
    result = classify_news_item({"title": "公告", "content": body}, target_symbol="000503")
    assert result["sentiment"] == "unknown"
    assert result["sentiment_label_source"] == "unknown"


def test_provider_label_is_preserved_separate_from_local_rules():
    result = classify_news_item({"title": "公告", "content": BODY, "sentiment": "negative", "data_source": "provider-a"})
    assert result["sentiment"] == result["provider_sentiment"] == "negative"
    assert result["sentiment_label_source"] == "provider"
    assert result["sentiment_method"] == "provider_label_unverified"
    assert result["sentiment_label_verified"] is False


def test_same_population_label_coverage_and_score_do_not_mutate_snapshot():
    rows = [{"title": "增长", "content": BODY}, {"title": "短讯", "content": "", "sentiment": "neutral"}, {"title": "未知", "content": ""}]
    items = [{**row, **classify_news_item(row, target_symbol="000503")} for row in rows]
    snapshot = {"items": items, "classification_coverage": sentiment_coverage(items), "population": "shared"}
    original = deepcopy(snapshot)
    payload = build_sentiment_payload(snapshot, "000503")
    assert snapshot == original
    assert payload["classification_coverage"]["label_source_counts"] == {"provider": 1, "local_rule": 1, "unknown": 1}
    assert payload["classification_coverage"]["label_coverage"] == 2 / 3
    assert payload["sentiment_score"] == 0.5
    assert payload["confidence"] is None
    assert len(payload["samples"]) == 3


def test_empty_population_never_makes_a_neutral_score():
    snapshot = {"items": [], "classification_coverage": sentiment_coverage([])}
    payload = build_sentiment_payload(snapshot, "000503")
    assert payload["sentiment_score"] is None
    assert payload["classification_coverage"]["label_coverage"] is None


def test_other_company_and_query_assigned_symbol_cannot_prove_subject():
    row = {"title": "行业消息", "content": BODY, "symbol": "002241", "source_kind": "official"}
    result = classify_news_item(row, target_symbol="002241", target_name="另一股份")
    assert result["sentiment"] == "unknown"
    assert result["sentiment_reason"] == "earnings_subject_not_bound_to_target"
    assert classify_news_item(row)["sentiment"] == "unknown"


def test_target_mentioned_before_competitor_does_not_bind_competitor_profit():
    row = {"title": "行业新闻", "content": "测试股份（000503）发布行业研究，另一公司归母净利润同比增长20%，文章还介绍了行业市场变化情况。"}
    assert classify_news_item(row, target_symbol="000503")["sentiment"] == "unknown"


@pytest.mark.parametrize(("phrase", "label"), [
    ("营业收入同比增长20%", "positive"), ("营业收入同比下降20%", "negative"),
    ("净亏损同比收窄20%", "positive"), ("净亏损同比扩大20%", "negative"),
])
def test_explicit_reported_revenue_and_loss_direction_has_bound_subject(phrase, label):
    body = "测试股份（000503）" + phrase + "。报告详细介绍了经营业绩，董事会已经审议通过相关财务报表。"
    result = classify_news_item({"title": "半年报", "content": body}, target_symbol="000503")
    assert result["sentiment"] == label
    assert result["sentiment_label_verified"] is False


def test_unrelated_negative_word_does_not_erase_reported_profit_fact():
    result = classify_news_item({"title": "半年报", "content": BODY + "本次公告不涉及重大资产重组。"}, target_symbol="000503")
    assert result["sentiment"] == "positive"


def test_speculative_title_or_bound_clause_is_not_a_reported_profit_fact():
    assert classify_news_item({"title": "预计利润增长", "content": BODY}, target_symbol="000503")["sentiment"] == "unknown"


@pytest.mark.parametrize(("phrase", "label"), [("主力资金净买入5349.33万元", "positive"),
    ("主力资金净流出483.99万元", "negative"), ("跌4.40%", "negative"), ("涨2.30%", "positive")])
def test_bound_reported_flow_and_price_direction_is_not_future_sentiment(phrase, label):
    row = {"title": "收盘数据", "content": "测试股份（000503）9月29日" + phrase + "。本文介绍当日已发生的公开交易统计，不构成投资建议。"}
    result = classify_news_item(row, target_symbol="000503")
    assert result["sentiment"] == label
    assert result["sentiment_label_verified"] is False


def test_title_only_and_speculative_price_do_not_gain_direction_labels():
    title = "测试股份（000503）9月29日主力资金净流出483.99万元，报道了当日资金情况"
    assert classify_news_item({"title": title, "content": title}, target_symbol="000503")["sentiment"] == "unknown"
    assert classify_news_item({"title": "可能上涨", "content": BODY}, target_symbol="000503")["sentiment"] == "unknown"


def test_publication_metadata_ignores_malformed_dates_without_losing_valid_news():
    from app.engine.tools.datasources.news import _news_publication_metadata
    rows = [{"publish_time": "2026-09-30T08:15:00+08:00"},
        {"publish_time": "not-a-date"}, {}, {"publish_time": "2026-10-10"}]
    metadata = _news_publication_metadata(rows, "2026-10-09")
    assert metadata["latest_publication_date"] == "2026-09-30"
    assert metadata["publication_lag_calendar_days"] == 9
    assert metadata["invalid_publication_time_count"] == 2
    assert metadata["after_cutoff_publication_count"] == 1
    assert metadata["current_day_news_coverage"] == "unverified"
    assert metadata["completeness"] == "observed_sample_not_all_news"


def test_empty_or_invalid_publication_times_do_not_imply_no_events():
    from app.engine.tools.datasources.news import _news_publication_metadata
    for rows in ([], [{"publish_time": "unknown"}]):
        metadata = _news_publication_metadata(rows, "2026-10-09")
        assert metadata["latest_publication_date"] is None
        assert metadata["publication_lag_calendar_days"] is None
        assert metadata["current_day_news_coverage"] == "unverified"
