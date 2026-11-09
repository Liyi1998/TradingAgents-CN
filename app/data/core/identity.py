"""Explicit CN benchmark identity; bare numeric codes are never inferred as indices."""
_INDEX_CODES = {"000001.SH", "000300.SH", "399001.SZ", "399006.SZ"}

def cn_index_identity(symbol: str) -> dict | None:
    text = str(symbol).upper()
    if text.startswith(("SH.", "SZ.")):
        prefix, code = text.split(".", 1)
        text = code + "." + prefix
    if text not in _INDEX_CODES:
        return None
    return {"symbol": text, "asset_type": "index", "exchange": text.rsplit(".", 1)[1]}
