"""One local correction of an optional assessment; keep all other report work."""

import json
import re
import asyncio

from app.engine.wyckoff_tracking import CONTRACT, checked_assessment, assessment_error
from app.models.wyckoff import WyckoffAssessment
from app.llm.core.types import ToolDef


def section_bounds(content):
    lines = content.splitlines(keepends=True)
    headings = []
    candidates = []
    offset = 0
    fence = None
    for line in lines:
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
            offset += len(line)
            continue
        heading = re.match(r"^\s*(#{1,6})\s+(.+)", line)
        if heading and fence is None:
            item = (offset, len(heading[1]))
            headings.append(item)
            if re.search(r"威科夫|wyckoff", heading[2], re.I):
                candidates.append(item)
        offset += len(line)
    if not candidates:
        return None
    # A report title mentioning Wyckoff does not make every report section
    # replaceable. Prefer its dedicated subsection; ambiguity stays read-only.
    if candidates[0][1] == 1 and any(pos > candidates[0][0] for pos, _ in headings):
        candidates = candidates[1:]
        if not candidates:
            return None
    start, level = candidates[0]
    end = next((pos for pos, depth in headings if pos > start and depth <= level), len(content))
    if any(pos >= end for pos, _ in candidates[1:]):
        return None
    return start, end, level


def clean_content(content):
    from app.engine.wyckoff_submission import NOTICE

    return content.split("\n\n", 1)[1] if content.startswith(NOTICE) and "\n\n" in content else content


def correction_prompt(content, error, snapshot):
    content = clean_content(content)
    bounds = section_bounds(content)
    # The production snapshot also includes duplicated domain rows, news,
    # financials and chart layers. Keep the complete evidence used by the
    # method, not the unrelated whole-market/research payload. Validation
    # still runs against the original frozen snapshot in make_repair_tool.
    evidence = {key: snapshot[key] for key in (
        "as_of", "bars", "price", "previous_wyckoff", "daily_metrics",
        "price_basis", "currency", "limitations",
    ) if key in snapshot}
    packet = {
        "task": "只纠正本次威科夫结构提交，不能重做其他研究或调用取数工具。",
        "error": error,
        "content": content,
        "snapshot": evidence,
        "replaceable_section": content[bounds[0] : bounds[1]] if bounds else None,
        "rules": "调用repair_wyckoff一次。只纠正有证据的字段及对应威科夫小节；其他正文不可改。"
        "method_section为完整替换小节（保留同级标题及必要条件），不改写时null。"
        "引用必须见正文且日线支持；不能删反证、伪造引用或机械放宽状态以通过。"
        "事件无法分辨可如实列为候选并说明替代解释；阶段证据不足应明确uncertain。",
    }
    return CONTRACT, json.dumps(packet, ensure_ascii=False, default=str, allow_nan=False)


def make_repair_tool(content, snapshot, *, rejections=None):
    from app.engine.research_tracking import tool_schema

    original = clean_content(content)
    accepted = []

    def check_replacement(wyckoff, method_section, unexpected):
        if accepted:
            raise ValueError("已完成局部纠正，不可重复提交")
        if unexpected:
            raise ValueError("纠错提交包含未支持的字段，只允许wyckoff和method_section")
        candidate = original
        if method_section is not None:
            bounds = section_bounds(original)
            if not bounds or not isinstance(method_section, str) or not 8 <= len(method_section) <= 8000:
                raise ValueError("没有唯一可替换的威科夫小节或小节长度无效，不能修改其他正文")
            new_bounds = section_bounds(method_section)
            if (
                not new_bounds
                or new_bounds[0] != 0
                or new_bounds[1] != len(method_section)
                or new_bounds[2] != bounds[2]
            ):
                raise ValueError("仅能提交一个同级威科夫小节，不能附带其他报告章节")
            candidate = original[: bounds[0]] + method_section.rstrip() + "\n\n" + original[bounds[1] :]
        result = checked_assessment(wyckoff, candidate, snapshot, require_method=True)
        return {"content": candidate, "wyckoff": result}

    async def repair_wyckoff(wyckoff=None, method_section=None, **unexpected):
        try:
            output = check_replacement(wyckoff, method_section, unexpected)
        except (ValueError, TypeError) as exc:
            reason = assessment_error(exc)
            if rejections is not None:
                rejections.append(reason)
            raise ValueError(reason) from None
        accepted.append(output)
        return "威科夫局部纠正已通过；其他技术正文保留，不再追加调用"

    schema = {
        "type": "object",
        "properties": {
            "wyckoff": tool_schema(WyckoffAssessment),
            "method_section": {
                "anyOf": [{"type": "string", "minLength": 8, "maxLength": 8000}, {"type": "null"}],
                "description": "仅替换原威科夫小节，包含同级标题；保留必要条件。无需改正文时null。",
            },
        },
        "required": ["wyckoff"],
        "additionalProperties": False,
    }
    return ToolDef(
        name="repair_wyckoff",
        description="仅一次纠正威科夫字段和对应小节，其他技术正文不动",
        params_schema=schema,
        handler=repair_wyckoff,
        is_concurrency_safe=False,
    ), accepted


def record_correction_result(record, result, accepted, rejections):
    """Keep the actual correction outcome, not only the initial submission error."""
    if result is not None:
        record.update(stop_reason=result.stop_reason, tool_calls=result.tool_calls_executed)
    if rejections:
        record["validation_errors"] = rejections[:4]
    if not accepted and "error" not in record:
        record["error"] = (rejections[-1] if rejections else
                           "模型未提交有效的威科夫纠错结果" + ("（停止原因：" + result.stop_reason + "）" if result else ""))


async def repair_full_submission(
    submission, context, client, *, max_tokens=None, task_id="", user_id="", event_sink=None
):
    """No new research agent or data requests. Provider retries are disabled."""
    from app.llm.runner import run_conversation
    from app.llm.failures import describe_failure

    snapshot = {**context.get("wyckoff_snapshots", {}), "previous_wyckoff": context.get("wyckoff_prior_snapshots")}
    if not snapshot.get("as_of") or not snapshot.get("bars"):
        return False
    rejections = []
    tool, accepted = make_repair_tool(submission.content, snapshot, rejections=rejections)
    system, body = correction_prompt(submission.content, submission.fields["wyckoff_error"], snapshot)
    if len(body.encode()) > 150000:
        return False  # Do not truncate evidence or trigger compaction calls.
    record = {"attempted": True, "succeeded": False}
    result = None
    try:
        async with asyncio.timeout(120):
            result = await run_conversation(
                client,
                body,
                system=system,
                tools=[tool],
                max_turns=1,
                max_tool_calls=1,
                max_tokens=min(max_tokens or 8192, 8192),
                temperature=0.2,
                retry_times=0,
                task_id=task_id,
                user_id=user_id,
                agent_key="market",
                phase="analysts",
                event_sink=event_sink,
                enable_skill_listing=False,
                allow_compaction=False,
                stop_when=lambda: bool(accepted),
            )
    except Exception as exc:
        failure = describe_failure(exc)
        record.update(failure_kind=failure["kind"], error=failure["message"])
    record_correction_result(record, result, accepted, rejections)
    if accepted:
        submission.content = accepted[0]["content"]
        submission.fields.update(wyckoff=accepted[0]["wyckoff"])
        submission.fields.pop("wyckoff_error", None)
        record["succeeded"] = True
    else:
        from app.engine.wyckoff_submission import NOTICE
        # Persist the correction's real failure rather than leaving the first
        # (often merely formatting) error as the only visible explanation.
        reason = record.get("error") or submission.fields["wyckoff_error"]
        submission.fields["wyckoff_error"] = reason
        submission.content = (f"{NOTICE}阶段结构未通过校验，不能作为已确认阶段。** "
                              f"局部纠正未完成：{reason}。以下技术正文保留。\n\n" + clean_content(submission.content))
    submission.fields["wyckoff_repair"] = record
    return bool(accepted)
