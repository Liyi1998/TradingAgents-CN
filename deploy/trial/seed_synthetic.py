"""Seed labeled synthetic research cards. Reads secrets from the environment and does not print them."""
from __future__ import annotations

import http.cookiejar
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

SYMBOLS = {
    "900001": "合成测试-完整日",
    "900002": "合成测试-缺日历",
    "900003": "合成测试-缺K线",
    "900004": "合成测试-陈旧",
    "900005": "合成测试-来源冲突",
    "900006": "合成测试-缺复权",
}
REPORT = "\n".join([
    "# 合成测试报告",
    "这是隔离试运行的合成研究档，不是真实持仓，也不是交易所回报。",
    "## 威科夫",
    "趋势背景：下跌。当前阶段：下跌。这是原报告判断，辅助层不得改写。",
    "## 最终裁决",
    "已持仓继续持有研究。未持仓继续观察。不授权交易。",
])


_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def _request(method: str, url: str, payload: dict | None = None, headers: dict | None = None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    with _OPENER.open(req, timeout=30) as response:
        return json.loads(response.read().decode())


def main() -> None:
    base = "http://127.0.0.1:8000"
    username = os.environ["INITIAL_ADMIN_USERNAME"]
    password = os.environ["INITIAL_ADMIN_PASSWORD"]
    login = _request("POST", base + "/api/auth/login", {"username": username, "password": password})
    data = login["data"]
    headers = {
        "Authorization": "Bearer " + data["access_token"],
        "X-CSRF-Token": data["csrf_token"],
    }
    from pymongo import MongoClient

    client = MongoClient(
        host=os.environ["MONGODB_HOST"],
        port=int(os.environ["MONGODB_PORT"]),
        username=os.environ["MONGODB_USERNAME"],
        password=os.environ["MONGODB_PASSWORD"],
        authSource=os.environ.get("MONGODB_AUTH_SOURCE", "admin"),
    )
    db = client[os.environ["MONGODB_DATABASE"]]
    user_id = str(data["user"]["id"])
    now = datetime.now(timezone.utc)
    for symbol, name in SYMBOLS.items():
        analysis_id = "synthetic-" + symbol
        db.analysis_reports.replace_one(
            {"analysis_id": analysis_id},
            {
                "analysis_id": analysis_id,
                "stock_symbol": symbol,
                "stock_name": name,
                "market_type": "A股",
                "status": "completed",
                "analysis_horizon": "short",
                "analysis_date": "2026-11-05",
                "created_at": now,
                "user_id": user_id,
                "reports": {
                    "market_report": REPORT,
                    "final_trade_decision": "已持仓：继续持有研究。合成测试，不是真实持仓，不授权交易。",
                },
                "decision": {"action": "HOLD"},
            },
            upsert=True,
        )
        created = _request("POST", f"{base}/api/research-tracking/stocks/{symbol}", {}, headers)
        track_id = created["data"]["id"]
        _request(
            "PUT",
            f"{base}/api/research-tracking/{track_id}/settings",
            {
                "auto_enabled": False,
                "max_calls_per_day": 6,
                "intraday_enabled": False,
                "structure_auxiliary_enabled": False,
            },
            headers,
        )
        version_id = "synthetic-version-" + symbol
        db.research_versions.replace_one(
            {"_id": version_id},
            {
                "_id": version_id,
                "track_id": track_id,
                "user_id": user_id,
                "baseline_analysis_id": analysis_id,
                "as_of": "2026-11-05",
                "created_at": now,
                "status": "completed",
                "errors": [],
                "snapshot": {"bars": [], "next_sessions": [], "domains": {}},
                "modules": {
                    "technical": {
                        "content": "合成测试技术结论。当前阶段：下跌。",
                        "as_of": "2026-11-05",
                        "changes": [],
                        "evidence_ids": [],
                        "limitations": ["合成测试，不是真实持仓"],
                        "inherited": False,
                        "wyckoff": {
                            "phase": "markdown",
                            "trend": "down",
                            "status": "candidate",
                            "method_version": "wyckoff-method-v2",
                            "source_quote": "趋势背景：下跌。当前阶段：下跌。这是原报告判断，辅助层不得改写。",
                            "evidence_ids": [],
                        },
                    }
                },
                "review": {
                    "content": "合成测试复核。已持仓继续按原结论观察，不授权交易。",
                    "changes": [],
                    "evidence_ids": [],
                    "limitations": ["合成测试，不是真实持仓"],
                    "requires_full_review": False,
                    "research_view": "bearish",
                    "unheld_action": "WATCH",
                    "held_action": "HOLD",
                    "strategy": None,
                },
            },
            upsert=True,
        )
        db.research_tracks.update_one({"_id": track_id}, {"$set": {"latest_version_id": version_id}})
        print("seeded", symbol, name)
    print("synthetic cards ready; auxiliary remains off")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        print("seed failed", exc.code)
        raise SystemExit(1) from exc
