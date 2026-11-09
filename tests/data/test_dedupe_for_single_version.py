from app.data.scripts.dedupe_for_single_version import _select_keep_id


def test_select_keep_id_uses_domain_source_priority():
    docs = [
        {"_id": "ak", "data_source": "akshare", "updated_at": "2026-09-12"},
        {"_id": "tx", "data_source": "tencent", "updated_at": "2026-08-01"},
    ]

    assert _select_keep_id(docs, "CN", "daily_indicators") == "tx"


def test_select_keep_id_uses_latest_document_within_same_priority():
    docs = [
        {"_id": "old", "data_source": "mx_mcp", "updated_at": "2026-08-01"},
        {"_id": "new", "data_source": "mx_mcp", "updated_at": "2026-09-12"},
    ]

    assert _select_keep_id(docs, "CN", "financial_data") == "new"


def test_select_keep_id_prefers_configured_source_over_unknown_source():
    docs = [
        {"_id": "mx", "data_source": "mx_mcp", "updated_at": "2026-09-12"},
        {"_id": "sina", "data_source": "sina", "updated_at": "2026-08-01"},
    ]

    assert _select_keep_id(docs, "CN", "money_flow") == "sina"
