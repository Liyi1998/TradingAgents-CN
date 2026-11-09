import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from typing import Optional

import numpy as np

from app.data.schema.base.common_fields import CommonFields
from app.data.schema.base.types import _parse_date


FINANCIAL_CONTRACT = "financial_disclosed_fields_v1"
FINANCIAL_VALUE_FIELDS = (
    "revenue", "revenue_hkd", "revenue_ttm", "net_profit", "net_profit_attributable",
    "net_profit_consolidated", "net_profit_deducted", "net_profit_hkd", "net_profit_ttm",
    "total_assets", "total_equity", "total_liabilities", "total_liab",
    "cash_and_equivalents", "accounts_receivable", "capital_expenditure",
    "operating_cashflow", "free_cashflow", "roe", "roa", "gross_margin", "net_margin",
    "debt_ratio", "debt_to_equity", "current_ratio", "eps", "eps_basic", "eps_diluted",
    "bps", "dividend_per_share",
)


def financial_number(value):
    """Do not turn booleans or non-finite vendor values into financial amounts."""
    if isinstance(value, (bool, np.bool_)):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None


def financial_date(value):
    """Accept actual calendar dates, never a request/update date as disclosure."""
    parsed = _parse_date(value)
    try:
        return date.fromisoformat(parsed).isoformat() if parsed else None
    except (TypeError, ValueError):
        return None


def financial_record_as_of(record, as_of):
    """Return only disclosed fields, without letting a future channel hide others.

    This is a read-time view of a latest snapshot, not a historical revision
    archive. Unknown dates and overwritten revisions cannot be reconstructed.
    ``None`` means the record has no provably usable financial value at cutoff.
    """
    cutoff = financial_date(as_of)
    if not cutoff:
        raise ValueError("Invalid financial as-of date")
    period = financial_date(record.get("report_period"))
    if not period or period > cutoff:
        return None
    result = deepcopy(record)
    result["report_period"] = period
    proof = result.get("field_provenance")
    proof = proof if isinstance(proof, dict) else {}
    per_field = record.get("financial_contract") == FINANCIAL_CONTRACT
    dates = []
    for field in FINANCIAL_VALUE_FIELDS:
        if field not in result:
            continue
        value = financial_number(result[field])
        evidence = proof.get("total_liabilities" if field == "total_liab" else field, {})
        evidence = evidence if isinstance(evidence, dict) else {}
        announced = financial_date(evidence.get("announce_date") if per_field else record.get("announce_date"))
        valid = bool(announced and period <= announced <= cutoff)
        if per_field:
            valid = valid and (
                evidence.get("status") == "available"
                and financial_date(evidence.get("report_period")) == period
                and evidence.get("data_source") == record.get("data_source")
                and bool(evidence.get("data_source"))
                and bool(evidence.get("provider_channel"))
                and evidence.get("provider_channel") != "unknown"
                and bool(evidence.get("method"))
                and bool(evidence.get("basis"))
            )
        if value is None or not valid:
            result[field] = None
            if per_field and evidence:
                evidence["status"] = ("missing" if value is None else
                    "future_disclosure" if announced and announced > cutoff else
                    "unproven_disclosure")
        else:
            result[field] = value
            dates.append(announced)
    if not dates:
        return None
    result["announce_date"] = max(dates)
    if result.get("net_profit") is None:
        result["net_profit_basis"] = None
    result["financial_as_of"] = cutoff
    return result


@dataclass
class FinancialDataSchema(CommonFields):
    """财务数据 — 三表 + 财务指标。"""

    report_period: Optional[str] = None         # 报告期截止日
    statement_type: Optional[str] = None        # income / balance / cashflow / indicator
    report_type: Optional[str] = None           # Q1/Q2/Q3/FY/H1/H2/1/2/4
    fiscal_year: Optional[int] = None
    fiscal_period: Optional[str] = None         # 财年内期次
    announce_date: Optional[str] = None
    source_disclosures: Optional[dict] = None  # contributing statements' disclosure bounds/scopes
    financial_contract: Optional[str] = None  # per-field disclosure and accounting semantics version
    field_provenance: Optional[dict] = None    # field -> source/report period/disclosure/basis
    filing_url: Optional[str] = None            # SEC EDGAR / 披露易链接

    # 核心财务字段
    revenue: Optional[float] = None             # 营业收入
    revenue_hkd: Optional[float] = None         # 港股折算 HKD
    net_profit: Optional[float] = None          # 与 net_profit_basis 一起使用
    net_profit_attributable: Optional[float] = None
    net_profit_consolidated: Optional[float] = None
    net_profit_deducted: Optional[float] = None
    net_profit_basis: Optional[str] = None
    net_profit_hkd: Optional[float] = None      # 港股折算 HKD
    total_assets: Optional[float] = None        # 总资产
    total_equity: Optional[float] = None        # 净资产
    total_liabilities: Optional[float] = None
    cash_and_equivalents: Optional[float] = None
    accounts_receivable: Optional[float] = None
    capital_expenditure: Optional[float] = None
    currency: Optional[str] = None
    monetary_unit: Optional[str] = None
    available_statements: Optional[str] = None  # 同报告期实际取得的子表
    statement_sources: Optional[str] = None
    operating_cashflow: Optional[float] = None  # 经营活动现金流净额
    free_cashflow: Optional[float] = None       # 自由现金流 (US)

    # 财务指标
    roe: Optional[float] = None                 # 净资产收益率 %
    roa: Optional[float] = None                 # 总资产收益率 %
    gross_margin: Optional[float] = None        # 毛利率 %
    net_margin: Optional[float] = None          # 净利率 %
    debt_ratio: Optional[float] = None          # 资产负债率 %
    debt_to_equity: Optional[float] = None      # D/E (US)
    current_ratio: Optional[float] = None       # 流动比率
    eps: Optional[float] = None                 # 每股收益
    eps_basic: Optional[float] = None           # 基本每股收益 (US)
    eps_diluted: Optional[float] = None         # 稀释每股收益 (US)
    bps: Optional[float] = None                 # 每股净资产
    dividend_per_share: Optional[float] = None  # 每股股息 (HK)

    def to_db_doc(self):
        doc = super().to_db_doc()
        if self.market == "CN" and self.financial_contract == FINANCIAL_CONTRACT:
            # Complete v1 snapshots must not retain fields from a prior snapshot.
            for field in FINANCIAL_VALUE_FIELDS:
                if hasattr(self, field):
                    doc[field] = financial_number(getattr(self, field))
            doc["announce_date"] = financial_date(self.announce_date)
            doc["net_profit_basis"] = self.net_profit_basis
            doc["source_disclosures"] = self.source_disclosures or {}
            doc["field_provenance"] = self.field_provenance or {}
        return doc
