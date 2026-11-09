"""Industry labels need a named taxonomy and a source-effective date."""

from datetime import date
import math
import re

INDUSTRY_CONTRACT = "vendor_industry_v1"
INDUSTRY_FIELDS = (
    "industry", "industry_source", "industry_classification",
    "industry_as_of", "industry_contract",
)


def industry_label(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    text = str(value).strip()
    if text.lower() in {"", "nan", "inf", "none", "null", "unknown", "n/a", "-"}:
        return None
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        return None
    return text


def industry_verified(record, as_of=None):
    if not industry_label(record.get("industry")):
        return False
    if record.get("industry_contract") != INDUSTRY_CONTRACT:
        return False
    if not industry_label(record.get("industry_source")) or not industry_label(record.get("industry_classification")):
        return False
    try:
        effective_text = str(record.get("industry_as_of"))
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", effective_text):
            return False
        effective = date.fromisoformat(effective_text)
        cutoff = date.fromisoformat(str(as_of)) if as_of else date.today()
    except (ValueError, TypeError):
        return False
    return effective <= cutoff
