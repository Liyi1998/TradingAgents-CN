"""Choose coherent price series without stitching providers or rebasing horizons."""

import pandas as pd


def select_quote_series(frame: pd.DataFrame, as_of: str, priorities: list[str]) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    work = frame.copy()
    if "symbol" not in work:
        work["symbol"] = ""
    work["_quote_date"] = pd.to_datetime(work["trade_date"], errors="coerce")
    work = work[work["_quote_date"].notna() & (work["_quote_date"] <= pd.Timestamp(as_of))]
    for key in ("data_source", "price_basis", "adjustment_contract", "adjustment_as_of"):
        if key not in work:
            work[key] = "unknown"
        work[key] = work[key].fillna("unknown").astype(str)
    work.loc[work["price_basis"].eq("raw"), ["adjustment_contract", "adjustment_as_of"]] = ""
    keys = ["symbol", "data_source", "price_basis", "adjustment_contract", "adjustment_as_of"]
    work = work.sort_values(["_quote_date"] + (["updated_at"] if "updated_at" in work else []))
    work = work.drop_duplicates(keys + ["_quote_date"], keep="last")
    stats = work.groupby(keys, dropna=False)["_quote_date"].agg(latest="max", count="count").reset_index()
    horizons = pd.to_datetime(stats["adjustment_as_of"], format="%Y-%m-%d", errors="coerce")
    stats["verified"] = stats["price_basis"].eq("raw") | (
        stats["price_basis"].isin(["qfq", "hfq"]) & stats["adjustment_contract"].eq("cn_adjusted_quotes_v1")
        & horizons.notna() & horizons.le(pd.Timestamp(as_of)) & horizons.ge(stats["latest"]))
    stats["length"] = stats["count"].clip(upper=260)
    stats["priority"] = -stats["data_source"].map({source: rank for rank, source in enumerate(priorities)}).fillna(len(priorities))
    chosen = stats.sort_values(["symbol", "verified", "latest", "length", "priority", "count"]).drop_duplicates("symbol", keep="last")
    return work.merge(chosen[keys], on=keys, how="inner", validate="many_to_one").drop(columns="_quote_date")
