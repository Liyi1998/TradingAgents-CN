"""数据校验器 — 写入前的字段校验。"""

import logging
import math
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# 各域必填字段
_REQUIRED_FIELDS = {
    "basic_info": ["symbol", "name"],
    "trade_calendar": ["exchange", "cal_date", "is_open"],
    "daily_quotes": ["symbol", "trade_date", "close"],
    "index_quotes": ["symbol", "trade_date", "close"],
    "daily_indicators": ["symbol", "trade_date"],
    "adj_factors": ["symbol", "trade_date", "adj_factor"],
    "corporate_actions": ["symbol", "ex_date", "action_type"],
    "financial_data": ["symbol", "report_period", "statement_type"],
    "market_quotes": ["symbol", "last_price"],
    "news": ["symbol", "title", "content_hash"],
    "intraday_quotes": ["symbol", "datetime", "freq", "open", "high", "low", "close"],
    "margin_trading": ["symbol", "trade_date", "margin_balance"],
    "money_flow": ["symbol", "trade_date", "data_source", "main_net_inflow"],
    "dragon_tiger": ["symbol", "trade_date", "reason"],
    "block_trade": [
        "symbol", "trade_date", "price", "volume", "amount",
        "buyer", "seller", "trade_seq",
    ],
}

# 价格字段 → 正数检查
_PRICE_FIELDS = ["open", "high", "low", "close", "pre_close", "last_price", "adj_factor"]

# 涨跌幅范围（按市场）
_PCT_RANGE = {
    "CN": (-30, 30),
    "HK": (-100, 100),
    "US": (-100, 100),
}

# A 股新股上市前若干交易日可能不设涨跌幅限制。全市场实时快照无法仅凭
# 报价判断上市天数，因此对该域允许合法的新股首日涨幅。
# Historical raw OHLCV is not discarded solely for a close-to-close jump across
# a share-basis change; its unverified percentage is quarantined separately.
_CN_MARKET_QUOTES_PCT_RANGE = (-100, 1000)


def _quarantine_raw_change(record: Dict, domain: str, market: str) -> Dict:
    candidate = dict(record)
    pct = candidate.get("pct_chg")
    if (market != "CN" or domain != "daily_quotes" or candidate.get("price_basis") != "raw"
            or candidate.get("volume_unit") != "share" or candidate.get("amount_unit") != "CNY"
            or not isinstance(pct, (int, float)) or isinstance(pct, bool) or not math.isfinite(pct)
            or -30 <= pct <= 30):
        return candidate
    conversion = str(candidate.get("unit_conversion") or "")
    if not conversion.startswith(("akshare:stock_zh_a_hist/", "akshare:stock_zh_a_daily/", "baostock:identity")):
        return candidate
    fields = ("open", "high", "low", "close", "volume", "amount")
    if any(not isinstance(candidate.get(key), (int, float)) or isinstance(candidate.get(key), bool)
           or not math.isfinite(candidate[key]) for key in fields):
        return candidate
    low, high = candidate["low"], candidate["high"]
    if not (0 < low <= candidate["open"] <= high and low <= candidate["close"] <= high):
        return candidate
    volume, amount = candidate["volume"], candidate["amount"]
    if not ((volume == 0 and amount == 0) or
            (volume > 0 and amount > 0 and low * .98 <= amount / volume <= high * 1.02)):
        return candidate
    # Raw price jumps can be corporate actions, but neither a generic price band
    # nor matching units proves that event or the vendor's percentage reference.
    candidate["reported_pct_chg"] = pct
    candidate["pct_chg"] = None
    candidate["change"] = None
    candidate["price_change_status"] = "raw_jump_reference_unverified"
    return candidate


class Validator:
    """数据校验器。"""

    def validate(self, records: List[Dict], domain: str, market: str) -> Tuple[List[Dict], List[Dict]]:
        """校验记录，返回 (有效记录, 错误记录)。"""
        if not records:
            return [], []

        valid = []
        errors = []

        required = _REQUIRED_FIELDS.get(domain, [])

        for rec in records:
            rec = _quarantine_raw_change(rec, domain, market)
            issues = self._validate_record(rec, domain, market, required)
            if issues:
                errors.append({"record": rec, "issues": issues})
            else:
                valid.append(rec)

        if errors:
            logger.warning(f"校验 {domain}/{market}: {len(valid)} 有效, {len(errors)} 错误")

        return valid, errors

    def _validate_record(self, rec: Dict, domain: str, market: str, required: List[str]) -> List[str]:
        issues = []

        # 必填字段
        for field in required:
            if field not in rec or rec[field] is None:
                issues.append(f"缺少必填字段: {field}")

        if issues:
            return issues

        if domain == "money_flow":
            from datetime import date
            from numbers import Real

            from app.data.schema.domains.money_flow import (
                MONEY_FLOW_VALUE_FIELDS,
                normalize_money_flow_symbol,
            )

            if market == "CN":
                try:
                    if normalize_money_flow_symbol(rec["symbol"]) != rec["symbol"]:
                        issues.append("资金流股票代码不是规范六位代码")
                except (TypeError, ValueError):
                    issues.append("资金流股票代码无效")
            try:
                if date.fromisoformat(rec["trade_date"]).isoformat() != rec["trade_date"]:
                    issues.append("资金流交易日期格式无效")
            except (TypeError, ValueError):
                issues.append("资金流交易日期无效")
            for field in MONEY_FLOW_VALUE_FIELDS + ("main_net_inflow_pct", "reported_flow_ratio"):
                value = rec.get(field)
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value)
                ):
                    issues.append(f"资金流非有限数值: {field}")
            value = rec.get("main_net_inflow_pct")
            if isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value):
                if not -100 <= value <= 100:
                    issues.append("资金流占比超范围")
                if rec.get("flow_pct_unit") != "percent":
                    issues.append("资金流占比单位未经明确")
            if rec.get("flow_date_verified") not in (None, True, False) or (
                rec.get("flow_date_verified") is not None
                and type(rec["flow_date_verified"]) is not bool
            ):
                issues.append("资金流日期证据标志必须为布尔值")
            if rec.get("flow_basis") == "eastmoney_order_size_v1" and (
                rec.get("flow_source") != "eastmoney" or rec.get("amount_unit") != "CNY"
            ):
                issues.append("资金流东财口径/来源/金额单位不一致")

        # 价格正数检查
        # H5 修复：增加 NaN 和 Infinity 检查。float('nan') < 0 为 False、
        # float('inf') < 0 为 False，两者均可绕过原有的负数检查。
        for pf in _PRICE_FIELDS:
            val = rec.get(pf)
            if val is not None and isinstance(val, (int, float)):
                if math.isnan(val) or math.isinf(val):
                    issues.append(f"价格异常(非有限值): {pf}={val}")
                elif val < 0:
                    issues.append(f"价格不能为负: {pf}={val}")

        # 涨跌幅范围（仅对数值类型校验，脏数据如字符串跳过）
        pct = rec.get("pct_chg")
        if pct is not None and isinstance(pct, (int, float)):
            if market == "CN" and domain == "market_quotes":
                lo, hi = _CN_MARKET_QUOTES_PCT_RANGE
            else:
                lo, hi = _PCT_RANGE.get(market, (-100, 100))
            if pct < lo or pct > hi:
                issues.append(f"涨跌幅超范围: pct_chg={pct} (范围 {lo}~{hi})")

        return issues
