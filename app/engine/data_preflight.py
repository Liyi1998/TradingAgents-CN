"""Task-scoped, bounded checks of explicitly configured datasource tools.

This wraps the existing pre-injection path; it does not invent requirements from
agent names, add providers, or run an additional market-wide refresh pass.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import inspect
import json
import threading
from copy import deepcopy
from typing import Any

from app.llm.tools.result_status import classify_tool_result


@dataclass
class PreflightSession:
    calls: dict = field(default_factory=dict)
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(3))
    news_lock: threading.Lock = field(default_factory=threading.Lock)
    max_calls: int = 24
    timeout_seconds: float = 40

    async def close(self) -> None:
        pending = [task for task in self.calls.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


ACTIVE_PREFLIGHT: ContextVar[PreflightSession | None] = ContextVar("data_preflight", default=None)


@dataclass(frozen=True)
class PreflightResult:
    result: Any
    coverage: tuple[dict, ...]


def declare_role(context: dict, role: str, declared_ids: list[str], specs: list[Any], *, reused: bool = False) -> dict:
    """Only a configured data_tool creates a requirement; unknown IDs stay visible."""
    checks = context.setdefault("role_data_checks", {})
    spec_map = {spec.tool_id: spec for spec in specs}
    ids = list(dict.fromkeys([*declared_ids, *spec_map]))
    row = {"scope": "configured_data_tools_only", "report_reused": reused, "tools": []}
    for tool_id in ids:
        spec = spec_map.get(tool_id)
        row["tools"].append({
            "tool_id": tool_id,
            "domains": list(spec.domains) if spec else [],
            "status": "not_rechecked_reused_report" if reused else "not_fetched" if spec else "unknown_capability",
            "reason": "reused_report_not_revalidated" if reused else "pending_preload" if spec else "unregistered_data_tool",
        })
    if not ids:
        row["limitations"] = ["未配置可识别的数据工具，无法核验此角色必需数据；不按角色名称推断能力。"]
    checks[role] = row
    return row


def record_tool(context: dict, role: str, tool_id: str, status: str, reason: str = "", *,
                coverage: tuple[dict, ...] = ()) -> None:
    for item in context.get("role_data_checks", {}).get(role, {}).get("tools", []):
        if item["tool_id"] == tool_id:
            item.update(status=status, reason=reason)
            latest = {}
            for read in coverage:
                quality = read.get("quality") or {}
                key = (read.get("domain"), read.get("symbol"), read.get("start_date"), read.get("end_date"))
                latest[key] = {"domain": read.get("domain"), "symbol": read.get("symbol"),
                    **{field: quality[field] for field in (
                        "status", "missing_fields", "missing_dates", "missing_fields_by_date",
                        "contract_issues", "requirement_contract", "optional_field_status",
                        "refresh_status", "refresh_source", "fallback_from", "refresh_error") if field in quality}}
            if latest:
                item["coverage"] = list(latest.values())
            break


def summarize_result(result: Any, domains: list[str], context: dict, *, coverage: tuple[dict, ...] = (),
                     args: dict | None = None, tool_id: str = "") -> tuple[str, str]:
    if any(item.get("domain") == "__trace_limit__" for item in coverage):
        return "returned_unverified", "per_tool_read_trace_budget_exhausted"
    status = classify_tool_result(result)
    if status == "error":
        return "failed", "tool_returned_error"
    payload = result
    for _ in range(3):
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                break
        elif isinstance(payload, dict) and "data" in payload:
            payload = payload["data"]
        else:
            break
    if isinstance(payload, dict):
        if payload.get("classification_status") in {"partial", "unknown"}:
            return "partial", "news_labels_not_fully_classified_or_verified"
        for row in payload.get("records") or []:
            if not isinstance(row, dict):
                continue
            pdf = row.get("pdf_metadata") or {}
            if row.get("content_status") in {"title_only", "failed", "unavailable"} or pdf.get("missing_topics"):
                return "partial", "official_document_body_or_required_notes_missing"
            if (row.get("discovery_metadata") or {}).get("missing_filing_kinds"):
                return "partial", "target_financial_filing_not_found"
    args = args or {}
    expected_symbol = next((str(args[key]) for key in ("symbol", "stock_code", "ts_code", "code") if args.get(key)), None)
    if expected_symbol and tool_id != "market_quotes" and context.get("market") == "CN":
        expected_symbol = expected_symbol.split(".")[0]
    expected_end = next((str(args[key]) for key in ("end_date", "analysis_date", "current_date", "date", "trade_date") if args.get(key)), context.get("as_of"))
    if expected_end and len(expected_end) == 8 and expected_end.isdigit():
        expected_end = f"{expected_end[:4]}-{expected_end[4:6]}-{expected_end[6:]}"
    expected_start = str(args.get("start_date") or "")
    if len(expected_start) == 8 and expected_start.isdigit():
        expected_start = f"{expected_start[:4]}-{expected_start[4:6]}-{expected_start[6:]}"
    def bound_read(item: dict) -> bool:
        if context.get("market") and item.get("market") != context["market"]:
            return False
        if item.get("domain") != "trade_calendar" and expected_symbol and item.get("symbol") != expected_symbol:
            return False
        if item.get("domain") not in {"trade_calendar", "basic_info"} and expected_end:
            allowed_ends = {expected_end[:10]}
            if expected_end[:10] == context.get("as_of") and context.get("last_actual_trade_date"):
                allowed_ends.add(context["last_actual_trade_date"])
            if not item.get("end_date") or str(item["end_date"])[:10] not in allowed_ends:
                return False
        if item.get("domain") not in {"trade_calendar", "basic_info"} and expected_start:
            if not item.get("start_date") or str(item["start_date"])[:10] > expected_start[:10]:
                return False
        return True
    latest = {}
    for item in coverage:
        key = tuple(str(item.get(field)) for field in ("market", "domain", "symbol", "start_date", "end_date")) + (json.dumps(item.get("filters"), sort_keys=True, default=str),)
        latest[key] = item
    matching = [item for item in latest.values() if item.get("domain") in domains and bound_read(item)]
    missing = sorted(set(domains) - {item["domain"] for item in matching})
    if missing:
        if status != "success":
            return "empty", "no_records_not_proof_of_no_events"
        return ("partial" if matching else "returned_unverified"), "missing_bound_facade_reads:" + ",".join(missing)
    qualities = [item.get("quality") or {} for item in matching]
    incomplete = sorted({str(q.get("status") or "unknown") for q in qualities
                         if q.get("status") not in {"success", "no_events", "not_applicable"}})
    if incomplete:
        return "partial", "facade_coverage:" + ",".join(incomplete)
    if status != "success":
        if qualities and all(q.get("status") in {"no_events", "not_applicable"} for q in qualities):
            return "no_events", "source_confirmed_no_returned_events_or_not_applicable"
        return "empty", "no_records_not_proof_of_no_events"
    if not qualities:
        return "returned_unverified", "tool_returned_data_without_facade_coverage"
    return "available", "preloaded_actual_data_not_complete_capability_proof"


async def invoke_shared(spec: Any, args: dict) -> Any:
    """Coalesce identical tool+arguments once per task, including failure results.

    Normal datasource functions remain responsible for DataInterface refresh.
    Cancelling a synchronous worker bounds the wait, not the worker's own I/O;
    those providers must also retain their existing network timeouts.
    """
    from app.engine.tools.datasources.registry import resolve_real_fn

    session = ACTIVE_PREFLIGHT.get()
    key = spec.tool_id + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)

    async def execute():
        from app.data.core.analysis_context import ACTIVE_READ_TRACE
        async def call():
            if inspect.iscoroutinefunction(resolve_real_fn(spec.fn)):
                return await spec.fn(**args)
            return await asyncio.to_thread(spec.fn, **args)
        async def traced_call(timeout):
            trace: list[dict] = []
            token = ACTIVE_READ_TRACE.set(trace)
            try:
                result = await asyncio.wait_for(call(), timeout=timeout)
                return PreflightResult(result, tuple(deepcopy(trace)))
            finally:
                ACTIVE_READ_TRACE.reset(token)
        if session is None:
            return await traced_call(55 if spec.tool_id == "official_announcements" else 40)
        async with session.semaphore:
            timeout = max(session.timeout_seconds, 55) if spec.tool_id == "official_announcements" else session.timeout_seconds
            return await traced_call(timeout)

    if session is None:
        return await execute()
    if key not in session.calls:
        if len(session.calls) >= session.max_calls:
            raise PreflightBudgetExceeded("task_preload_budget_exhausted")
        session.calls[key] = asyncio.create_task(execute())
    return await asyncio.shield(session.calls[key])


class PreflightBudgetExceeded(RuntimeError):
    """The task-wide read budget is exhausted; do not start another provider call."""


def render_role_checks(context: dict, role: str | None = None) -> str:
    checks = context.get("role_data_checks") or {}
    if role is not None:
        checks = {role: checks[role]} if role in checks else {}
    lines = ["## 角色数据前置检查", "",
             "按已配置 data_tools 核验，不证明角色全部业务需求已覆盖。缺项不得补零、默认中性、编造评分或胜率；",
             "单项缺失不终止整体分析；完成有界备用源尝试后如实披露并继续其他证据。依赖缺失字段的结论不得表述为可执行结论；有依据的条件研究价位、技术失效界限和定性风险可继续给出，清楚标注非订单。账户参数未知不等同于行情不可分析。",
             "partial/returned_unverified 只表示有部分输出；not_fetched/failed/unknown_capability 不能视为已完成取数。",
             "", "| 角色 | 必需工具（配置） | 状态 | 原因 |", "|---|---|---|---|"]
    for key, row in checks.items():
        for item in row.get("tools", []):
            cells = [key, item["tool_id"], item["status"], item.get("reason") or "未知"]
            lines.append("| " + " | ".join(str(value).replace("|", "/").replace("\n", " ") for value in cells) + " |")
        for limitation in row.get("limitations", []):
            lines.append(f"- {key}: {limitation}")
    return "\n".join(lines)


def render_role_gaps(context: dict, role: str) -> str:
    """Deterministic appendix: a model cannot omit the recorded acquisition gaps."""
    tools = context.get("role_data_checks", {}).get(role, {}).get("tools", [])
    gaps = []
    for item in tools:
        problems = [row for row in item.get("coverage", []) if row.get("status") not in {"success", "no_events", "not_applicable"}
                    or row.get("optional_field_status")]
        if item.get("status") not in {"available", "no_events", "not_applicable"} or problems:
            gaps.append({"tool": item["tool_id"], "status": item.get("status"),
                         "reason": item.get("reason"), "coverage": problems})
    if not gaps:
        return ""
    return ("\n\n### 系统记录的数据缺项与影响\n\n"
            "以下为实际取数结果，不是模型判断。单项缺失未中止整体分析；相关合计、评分或依赖结论须保留未知/待验证，其他证据可继续使用。\n\n"
            "```json\n" + json.dumps(gaps, ensure_ascii=False, indent=2, default=str) + "\n```")
