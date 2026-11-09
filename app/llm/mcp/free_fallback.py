"""Known read-only MCP failures may expose bounded builtin data, never invented equivalence."""
import asyncio
from copy import deepcopy

_KNOWN = {
    "mx_finance_search_news": ("news",),
    "mx_finance_search_notice": ("official_announcements",),
    "mx_ashare_finance_data": ("financial_data", "money_flow"),
    "mx_comprehensive_finance_data": ("financial_data", "money_flow"),
}


async def builtin_fallback(tool_name: str, payload: dict) -> dict | None:
    from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT
    from app.engine.tools.datasources.registry import get_spec_by_id
    from app.engine.orchestrator.agents import _resolve_inject_args
    context = ACTIVE_ANALYSIS_CONTEXT.get()
    identifiers = _KNOWN.get(tool_name)
    if not identifiers or not context or context.get("market") != "CN":
        return None
    symbol = str(context.get("symbol") or "").split(".")[0]
    if len(symbol) != 6 or not symbol.isdigit() or not context.get("as_of"):
        return None
    for key in ("symbol", "ticker", "stock_code", "ts_code", "code"):
        if payload.get(key) and str(payload[key]).split(".")[0] != symbol:
            return None
    import re
    query_codes = set(re.findall(r"(?<!\d)\d{6}(?!\d)", str(payload.get("query") or "")))
    if query_codes and query_codes != {symbol}:
        return None
    cache = context.setdefault("mcp_fallback_snapshots", {})
    sections = []
    for identifier in identifiers:
        key = f"{symbol}:{context['as_of']}:{identifier}"
        if key not in cache:
            # Claim before await: parallel failed tools do not fan out duplicate requests.
            cache[key] = {"tool_id": identifier, "status": "pending"}
            spec = get_spec_by_id(identifier)
            args = _resolve_inject_args(spec, {"ticker": symbol, "trade_date": context["as_of"]})
            try:
                data = await asyncio.wait_for(asyncio.to_thread(spec.fn, **args), timeout=30)
                from app.llm.tools.result_status import classify_tool_result
                cache[key] = {"tool_id": identifier, "status": classify_tool_result(data), "data": data}
            except Exception as exc:
                cache[key] = {"tool_id": identifier, "status": "failed", "error_type": type(exc).__name__}
        sections.append(deepcopy(cache[key]))
    return {"status": "partial_substitute", "symbol": symbol, "analysis_date": context["as_of"],
            "scope": "Only listed standard builtin data; not a verified equivalent of the entire MCP query.",
            "limitations": "MCP failure remains disclosed. Unavailable custom metrics, social posts or full notes stay unknown.",
            "sections": sections}
