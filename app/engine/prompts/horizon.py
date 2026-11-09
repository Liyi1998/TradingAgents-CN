"""Upstream horizon alias adapted to the deployed multi-horizon research contract.

Assessment windows are measurement horizons, never mandatory exit dates.
The role-specific research contract remains authoritative for evidence and plans.
"""
from typing import Literal, Optional
from app.core.analysis_horizon import get_horizon_profile
from app.core.horizons import horizon_days_of

HorizonRole = Literal["analyst", "debater", "judge", "trader"]

def build_horizon_anchor(horizon: Optional[str], role: HorizonRole) -> str:
    key = {"mid": "medium", "medium": "medium", "long": "long"}.get(horizon or "")
    if key is None:
        return ""
    profile = get_horizon_profile(key)
    days = horizon_days_of(key)
    return (
        f"\n\n【投资期限指令】本次为{profile.label}研究，观察周期 {profile.holding_period}。"
        "角色定义中的短线 3/7 日条款不得覆盖本周期；按本角色的周期研究契约分析。"
        f"事后结算以 {days} 个交易日为统一评估窗口，仅用于检验历史结论，"
        "不是固定持有期、收益承诺或到期强制买卖指令；退出条件由研究依据与失效条件决定。"
    )
