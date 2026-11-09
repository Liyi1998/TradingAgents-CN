"""Optional method validation in the existing technical report submission."""
import json

from app.engine.wyckoff_tracking import checked_assessment, assessment_error

NOTICE = "**威科夫方法校验："


def capture_technical_snapshot(result, context):
    """Capture existing preloaded data; no extra data or model request."""
    try:
        value = json.loads(result) if isinstance(result, str) else result
        if isinstance(value, dict) and "data" in value:
            value = value["data"]
        value = json.loads(value) if isinstance(value, str) else value
        if not isinstance(value, dict) or value.get("status") != "success":
            return
        cutoff = context.get("last_completed_trade_date")
        bars = [{**b, "date": b["trade_date"], "data_source": value.get("data_source")}
                for b in value.get("recent_daily", []) if cutoff and b["trade_date"] <= cutoff]
        if value.get("price_basis") not in {"raw", "qfq", "hfq"} or not value.get("recent_calendar_complete", value.get("calendar_complete")):
            bars = []
        context["wyckoff_snapshots"] = {"as_of": cutoff or context.get("as_of"), "bars": bars,
            "price": bars[-1] if bars and bars[-1]["date"] == cutoff else None}
    except (ValueError, TypeError, KeyError):
        return


def check_submission(content, value, context):
    snapshot = {**context.get("wyckoff_snapshots", {}), "previous_wyckoff": context.get("wyckoff_prior_snapshots")}
    try:
        if not value or not snapshot.get("as_of"):
            raise ValueError("未提供可校验的阶段结构或已完成日线证据")
        assessment = checked_assessment(value, content, snapshot, require_method=True)
        return content, {"wyckoff": assessment}
    except (ValueError, TypeError, KeyError) as exc:
        reason = assessment_error(exc)
        # Keep all other technical work. Explicitly limit the failed claim for
        # downstream roles, instead of retrying or failing the whole analysis.
        note = f"{NOTICE}阶段结构未通过校验，不能作为已确认阶段。** {reason}。以下技术正文保留；有依据的价格与指标仍可用。\n\n"
        return note + content, {"wyckoff": None, "wyckoff_error": reason}
