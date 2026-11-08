"""Conservative, local text-direction labels; never a social-sentiment model."""
from __future__ import annotations

import re
from collections import Counter
from html import unescape

METHOD_VERSION = "news-direction-rules-v2"
_LABELS = {"positive", "negative", "neutral"}
_UNCERTAIN = re.compile(r"(?:不|未|无|尚未)(?:实现|增长|下降|盈利|收窄|扩大)|拟|预计|预期|可能|或将|有望|如果|假设|预测|传闻|目标价|问询")
_DENIAL = re.compile(r"否认|澄清")
_SUBJECT_LINK = re.compile(r"(?:公司|的|实现|录得|披露|公告|发布|今日|昨日|当日|收盘|报告期|本期|当期|上半年|下半年|全年|年度|中期|半年报|年报|季报|一季度|二季度|三季度|四季度|\d{4}年|\d{1,2}月\d{1,2}日|[:：，,\s（）()])+")
_RULES = (
    ("profit_yoy_increase", "positive", re.compile(r"(?:归母|归属于[^，。；]{0,12})?净利润[^，。；\n]{0,18}同比(?:增长|增加|上升)\s*\d+(?:\.\d+)?\s*%")),
    ("profit_yoy_decrease", "negative", re.compile(r"(?:归母|归属于[^，。；]{0,12})?净利润[^，。；\n]{0,18}同比(?:下降|减少|下滑)\s*\d+(?:\.\d+)?\s*%")),
    ("revenue_yoy_increase", "positive", re.compile(r"营业(?:总)?收入[^，。；\n]{0,18}同比(?:增长|增加|上升)\s*\d+(?:\.\d+)?\s*%")),
    ("revenue_yoy_decrease", "negative", re.compile(r"营业(?:总)?收入[^，。；\n]{0,18}同比(?:下降|减少|下滑)\s*\d+(?:\.\d+)?\s*%")),
    ("loss_yoy_narrowing", "positive", re.compile(r"(?:归母)?净亏损[^，。；\n]{0,18}同比(?:收窄|减少)\s*\d+(?:\.\d+)?\s*%")),
    ("loss_yoy_widening", "negative", re.compile(r"(?:归母)?净亏损[^，。；\n]{0,18}同比(?:扩大|增加)\s*\d+(?:\.\d+)?\s*%")),
    ("reported_main_flow_in", "positive", re.compile(r"主力(?:资金)?净(?:买入|流入)\s*\d+(?:\.\d+)?\s*(?:万|亿)?元")),
    ("reported_main_flow_out", "negative", re.compile(r"主力(?:资金)?净(?:卖出|流出)\s*\d+(?:\.\d+)?\s*(?:万|亿)?元")),
    ("reported_price_increase", "positive", re.compile(r"(?:股价)?(?:上涨|涨)\s*\d+(?:\.\d+)?\s*%")),
    ("reported_price_decrease", "negative", re.compile(r"(?:股价)?(?:下跌|跌)\s*\d+(?:\.\d+)?\s*%")),
)


def has_body_text(row: dict) -> bool:
    """A copied title or failed body fetch is not evidence of body coverage."""
    content = str(row.get("content") or "").strip()
    title = str(row.get("title") or "").strip()
    def normalize(value):
        return re.sub(r"\s+", "", unescape(re.sub(r"<[^>]*>", "", value)))
    return (len(normalize(content)) >= 30 and normalize(content) != normalize(title)
            and row.get("content_status") not in {"title_only", "fetch_failed", "empty"})


def classify_news_item(row: dict, *, target_symbol: str = "", target_name: str = "") -> dict:
    """Preserve upstream labels; infer only explicit non-ambiguous earnings text.

    A provider label is not verified truth, and a rule label describes literal
    wording, not price impact. No body, speculative/negated text, mixed directions,
    and non-matching subjects remain unknown instead of default neutral.
    """
    original = row.get("sentiment")
    source = str(row.get("data_source") or row.get("source") or "unknown")
    title = str(row.get("title") or "")
    content = str(row.get("content") or "").strip()
    evidence = {"sentiment": "unknown", "provider_sentiment": original,
                "sentiment_label_source": "unknown", "sentiment_method": METHOD_VERSION,
                "sentiment_label_verified": False, "sentiment_evidence": [],
                "sentiment_reason": "no_matching_high_precision_rule"}
    if isinstance(original, str) and original in _LABELS:
        evidence.update(sentiment=original, sentiment_label_source="provider",
                        sentiment_method="provider_label_unverified",
                        sentiment_reason="preserved_original_label_not_independently_verified",
                        sentiment_evidence=[{"data_source": source, "text": title[:200]}])
        return evidence
    if not has_body_text(row):
        evidence["sentiment_reason"] = "insufficient_body_text"
        return evidence
    text = title + "\n" + content[:6000]
    if _DENIAL.search(text):
        evidence["sentiment_reason"] = "negation_or_speculation_requires_review"
        return evidence
    symbol_pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(target_symbol)}(?![A-Za-z0-9])") if target_symbol else None
    def identifies_target(prefix: str) -> bool:
        positions = [match.end() for match in symbol_pattern.finditer(prefix)] if symbol_pattern else []
        if len(target_name) >= 3 and target_name in prefix:
            positions.append(prefix.rfind(target_name) + len(target_name))
        if not positions:
            return False
        # No arbitrary words between entity and earnings subject: mentioning a
        # competitor in the same sentence must not inherit the target identity.
        link = prefix[max(positions):]
        return not link or bool(_SUBJECT_LINK.fullmatch(link))
    matches = []
    uncertain_match = False
    for sentence in re.split(r"[。；\n]", text):
        for rule_id, label, pattern in _RULES:
            for match in pattern.finditer(sentence):
                if identifies_target(sentence[:match.start()]):
                    if _UNCERTAIN.search(sentence) or _UNCERTAIN.search(title):
                        uncertain_match = True
                        continue
                    matches.append({"rule_id": rule_id, "label": label, "text": sentence[:300],
                                    "data_source": source, "subject_evidence": "target_marker_directly_bound_to_earnings_subject"})
    labels = {match["label"] for match in matches}
    if uncertain_match:
        evidence["sentiment_reason"] = "negation_or_speculation_requires_review"
        return evidence
    if len(labels) > 1:
        evidence["sentiment_reason"] = "conflicting_text_directions"
        return evidence
    if labels:
        evidence.update(sentiment=next(iter(labels)), sentiment_label_source="local_rule",
                        sentiment_reason="literal_reported_text_direction_not_investment_conclusion",
                        sentiment_evidence=matches[:4])
    elif any(pattern.search(text) for _, _, pattern in _RULES):
        evidence["sentiment_reason"] = "earnings_subject_not_bound_to_target"
    return evidence


def sentiment_coverage(items: list[dict]) -> dict:
    total = len(items)
    counts = {key: sum(row.get("sentiment_label_source", "unknown") == key for row in items)
              for key in ("provider", "local_rule", "unknown")}
    labeled = counts["provider"] + counts["local_rule"]
    return {"sample_count": total, "label_source_counts": counts,
            "label_coverage": labeled / total if total else None,
            "body_coverage": sum(has_body_text(row) for row in items) / total if total else None,
            "body_coverage_kind": "substantive_observed_text_not_verified_full_body",
            "body_status_counts": dict(Counter(str(row.get("content_status") or "unverified") for row in items)),
            "classification_status": "unknown" if not labeled else "complete_sample_labels" if labeled == total else "partial",
            "method_version": METHOD_VERSION,
            "limitations": "标签仅描述新闻文本方向，不是事实核验、股价预测、统计置信度或社交情绪；未知不能填中性。"}
