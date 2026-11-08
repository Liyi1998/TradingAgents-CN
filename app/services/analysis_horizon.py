"""Read immutable horizon snapshots and isolate task report archives."""

import re
from pathlib import Path
from typing import Any, Mapping

from app.core.analysis_horizon import AnalysisHorizon, normalize_analysis_horizon


def horizon_from_snapshot(record: Mapping[str, Any] | None) -> AnalysisHorizon:
    """Old records without a horizon belong to the original short-term mode."""
    record = record or {}
    value = record.get("analysis_horizon")
    parameters = record.get("parameters")
    if value is None and isinstance(parameters, Mapping):
        value = parameters.get("analysis_horizon")
    return normalize_analysis_horizon(value)


def require_matching_horizon(source: Mapping[str, Any], target: Mapping[str, Any]) -> AnalysisHorizon:
    """Never relabel a saved task or reuse reports from a different horizon."""
    horizon = horizon_from_snapshot(source)
    if horizon != horizon_from_snapshot(target):
        raise ValueError("分析周期与原任务不一致，请重新发起完整分析")
    parameters = source.get("parameters")
    if isinstance(parameters, Mapping) and parameters.get("analysis_horizon") is not None:
        if normalize_analysis_horizon(parameters["analysis_horizon"]) != horizon:
            raise ValueError("原任务分析周期快照不一致，请重新发起完整分析")
    return horizon


def task_report_directory(
    base: Path, symbol: str, analysis_date: str, horizon: AnalysisHorizon, task_id: str
) -> Path:
    """One archive per task, including same-stock same-date same-horizon reruns."""
    for value in (symbol, analysis_date, task_id):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value in {".", ".."}:
            raise ValueError("报告归档标识无效")
    return base / symbol / analysis_date / normalize_analysis_horizon(horizon) / task_id / "reports"
