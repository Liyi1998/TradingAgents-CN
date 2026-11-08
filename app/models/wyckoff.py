"""Optional, evidence-bound output of the existing daily technical analyst."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Phase = Literal["accumulation", "testing", "markup", "distribution", "markdown", "uncertain"]


class WyckoffEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["SC", "AR", "ST", "Spring", "Test", "SOS", "LPS", "BC", "UT", "UTAD", "SOW", "LPSY"] = Field(
        description="单个事件代码，严格使用枚举。禁止PSY/BC、Spring/SC等合并值；Testing应写Test。枚举外观察可在正文描述，不得伪装成另一个事件。")
    date: date
    status: Literal["candidate", "observed"]
    source_quote: str = Field(min_length=4, max_length=600)


class WyckoffRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date
    end: date
    support: float = Field(gt=0, allow_inf_nan=False)
    resistance: float = Field(gt=0, allow_inf_nan=False)
    source_quote: str = Field(min_length=8, max_length=600)


class WyckoffAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # None is retained solely for immutable v1 historical versions.
    method_version: Literal["wyckoff-method-v2"] | None = None
    trend: Literal["up", "down", "range", "uncertain"] = Field(default="uncertain",
        description="与阶段原文中明确的当前趋势背景一致：up上涨/上行，down下跌/下行，range横盘/震荡整理；不能用旧趋势或未来条件佐证。")
    events: list[WyckoffEvent] = Field(default_factory=list, max_length=8)
    trading_range: WyckoffRange | None = None
    change_reason: str | None = Field(default=None, max_length=900)
    alternative_scenario: str | None = Field(default=None, max_length=600)
    phase: Phase
    candidate_phase: Phase | None = Field(default=None, description="仅status=transition时填写不同的目标阶段；candidate不是转换，必须为null。")
    status: Literal["candidate", "supported", "transition", "invalidated", "uncertain"]
    source_quote: str = Field(min_length=8, max_length=1800,
        description="逐字复制含趋势背景、当前阶段和状态的完整独立原段，不仅摘当前阶段一句，不拼接其他段落。")
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)
    structure_start: date | None = Field(default=None, description="结构开始日期，须在提供的日线窗口内，且见source_quote或structure_start_quote；未明示则null。")
    structure_start_quote: str | None = Field(default=None, max_length=600,
        description="逐字引用正文中明示结构起始日的完整句子；日期位于阶段摘要之外时填写。")
    confirmation_conditions: list[str] = Field(default_factory=list, max_length=6)
    invalidation_conditions: list[str] = Field(default_factory=list, max_length=6)
    limitations: list[str] = Field(default_factory=list, max_length=8)
