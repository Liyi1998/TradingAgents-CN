"""Cross-check native bars without filling missing auctions or turnover."""
from app.data.core.requirements import finite_number
from app.data.storage.mongo.repositories.daily_quotes_repo import trusted_raw_quote


def compare_intraday_daily(bars, daily, dates):
    checks = []
    for day in dates:
        samples = sorted([row for row in bars if str(row.get("datetime", ""))[:10] == day],
                         key=lambda row: row.get("datetime", ""))
        quotes = [row for row in daily if row.get("trade_date") == day and trusted_raw_quote(row)]
        if not samples or not quotes or not all(all(finite_number(row.get(key)) for key in
            ("open", "high", "low", "close")) for row in samples):
            checks.append({"trade_date": day, "status": "unverified", "reason": "native_bars_or_verified_daily_missing"})
            continue
        # Never choose an arbitrary value when multiple raw sources disagree.
        keys = ("open", "high", "low", "close", "volume")
        if any(len({row.get(key) for row in quotes if finite_number(row.get(key))}) != 1 for key in keys):
            checks.append({"trade_date": day, "status": "unverified", "reason": "daily_sources_missing_or_conflicting"})
            continue
        quote = quotes[0]
        values = {"open": samples[0].get("open"), "high": max(row["high"] for row in samples),
                  "low": min(row["low"] for row in samples), "close": samples[-1].get("close")}
        price_mismatches = [key for key, value in values.items() if not finite_number(value)
                            or abs(value - quote[key]) > .011]
        volume_known = all(finite_number(row.get("volume")) for row in samples)
        total_volume = sum(row["volume"] for row in samples) if volume_known else None
        # Tencent bars publish rounded lots. This tolerance records precision,
        # not a percentage chosen to hide an unexplained volume gap.
        tolerance = len(samples) * 100 if any(row.get("data_source") == "tencent" for row in samples) else 1
        difference = total_volume - quote["volume"] if total_volume is not None else None
        volume_matches = difference is not None and abs(difference) <= tolerance
        amount_known = (all(row.get("amount_unit") == "CNY" and finite_number(row.get("amount")) for row in samples)
                        and quote.get("amount_unit") == "CNY" and finite_number(quote.get("amount")))
        total_amount = sum(row["amount"] for row in samples) if amount_known else None
        amount_difference = total_amount - quote["amount"] if total_amount is not None else None
        amount_matches = amount_difference is not None and abs(amount_difference) <= 1
        checks.append({"trade_date": day, "status": "matched" if not price_mismatches and volume_matches else "partial",
            "price_mismatches": price_mismatches, "bar_volume_sum": total_volume,
            "daily_volume": quote["volume"], "volume_difference": difference,
            "volume_tolerance_shares": tolerance, "full_session_volume_verified": volume_matches,
            "bar_amount_sum": total_amount, "amount_difference_CNY": amount_difference,
            "full_session_vwap_verified": volume_matches and amount_matches,
            "limitation": "成交量不一致时不能宣称覆盖全部竞价/盘后成交；不反填分钟量额，日线技术与可验证OHLC继续使用。"})
    return {"status": "matched" if checks and all(row["status"] == "matched" for row in checks) else "partial",
            "checks": checks, "method": "native_intraday_vs_verified_raw_daily",
            "limitations": "只核对收盘后的同日原始行情；尚未完成的交易日不能据此判定全日缺口。"}
