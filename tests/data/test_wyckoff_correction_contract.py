"""Evidence-bound trend wording, section isolation and rejection diagnostics."""

from copy import deepcopy

import pytest

from app.engine.wyckoff_quotes import complete_phase_paragraph
from app.engine.wyckoff_repair import make_repair_tool, section_bounds, record_correction_result
from app.engine.wyckoff_tracking import checked_assessment
from app.engine.wyckoff_trend import check_trend_quote
from app.llm.runner import RunResult
from tests.data.test_wyckoff_method import method_case


@pytest.mark.parametrize("trend,background", [
    ("up", "上涨"), ("up", "持续上行"), ("up", "上升通道"),
    ("down", "下跌"), ("down", "中短期下行趋势中的超跌企稳（down / 弱反弹）"),
    ("down", "下降通道"), ("range", "横盘"), ("range", "低位震荡整理"),
    ("range", "窄幅盘整"), ("range", "下跌趋势后低位矩形震荡"),
    ("range", "此前下跌，但当前横盘"), ("up", "由横盘转为上行"),
])
def test_explicit_current_trend_accepts_equivalent_wording(trend, background):
    value, content, snapshot = method_case()
    quote = f"趋势背景：{background}；当前阶段：吸筹；状态：候选。"
    value.update(trend=trend, source_quote=quote)
    result = checked_assessment(value, content + "\n\n" + quote, snapshot, require_method=True)
    assert result["trend"] == trend and result["source_quote"] == quote


@pytest.mark.parametrize("trend,quote", [
    ("down", "趋势背景：上涨；当前阶段：下跌；状态：候选。"),
    ("down", "趋势背景：下跌趋势后低位矩形震荡；当前阶段：吸筹；状态：候选。"),
    ("down", "趋势背景：此前下跌，但当前横盘；当前阶段：吸筹；状态：候选。"),
    ("range", "趋势背景：由横盘转为上行；当前阶段：拉升；状态：候选。"),
    ("up", "趋势背景：尚未确认上涨；当前阶段：吸筹；状态：候选。"),
    ("up", "趋势背景：不是上涨；当前阶段：吸筹；状态：候选。"),
    ("down", "趋势背景：未发生下跌；当前阶段：吸筹；状态：候选。"),
    ("range", "趋势背景：不属于盘整；当前阶段：吸筹；状态：候选。"),
    ("up", "趋势背景：若放量则可能上涨；当前阶段：吸筹；状态：候选。"),
    ("range", "趋势背景：如果下跌结束后横盘；当前阶段：吸筹；状态：候选。"),
    ("range", "趋势背景：并非下跌趋势后横盘；当前阶段：吸筹；状态：候选。"),
    ("up", "当前阶段：拉升；未来上涨仍待确认。"),
    ("up", "趋势背景：震荡 当前阶段：上涨仅为假说；状态：候选。"),
    ("down", "趋势背景：下跌；趋势背景：上涨；当前阶段：吸筹；状态：候选。"),
])
def test_old_negated_conditional_or_other_field_words_do_not_validate_current_trend(trend, quote):
    with pytest.raises(ValueError):
        check_trend_quote(trend, quote)


def test_short_phase_quote_recovers_only_one_complete_original_paragraph():
    value, content, snapshot = method_case()
    paragraph = "**趋势背景**：低位震荡整理；当前阶段：吸筹；状态：候选。"
    short = "当前阶段：吸筹；状态：候选。"
    content += "\n\n" + paragraph + "\n\n其他技术证据仍保留。"
    value.update(trend="range", source_quote=short)
    result = checked_assessment(value, content, snapshot, require_method=True)
    assert result["source_quote"] == paragraph and result["source_quote"] in content
    assert complete_phase_paragraph(short, paragraph + "\n\n" + paragraph) == short
    separated = "趋势背景：横盘。\n\n" + short
    assert complete_phase_paragraph(short, separated) == short
    with pytest.raises(ValueError):
        checked_assessment(value, content.replace(paragraph, separated), snapshot, require_method=True)


def test_new_validation_does_not_relabel_or_erase_old_accepted_history():
    value, content, snapshot = method_case()
    quote = "当前阶段：吸筹候选；下跌背景尚待进一步研究。"
    value["source_quote"] = quote
    content += "\n\n" + quote
    original = deepcopy(value)
    assert checked_assessment(value, content, snapshot)["phase"] == value["phase"]
    with pytest.raises(ValueError, match="趋势背景"):
        checked_assessment(value, content, snapshot, require_method=True)
    assert value == original


@pytest.mark.asyncio
async def test_report_title_does_not_make_unrelated_sections_replaceable():
    value, section, snapshot = method_case()
    prefix = "# 个股技术分析与威科夫每日更新\n\n## 价格依据\n其他价格内容不能修改。\n\n"
    suffix = "\n\n## 账户与风险限制\n原有账户未知及风险限制必须保留。"
    content = prefix + section + suffix
    start, end, level = section_bounds(content)
    assert start == len(prefix) and content[start:end].strip() == section.strip() and level == 2
    tool, accepted = make_repair_tool(content, snapshot)
    await tool.handler(wyckoff=value, method_section=section)
    assert accepted[0]["content"].startswith(prefix)
    assert accepted[0]["content"].endswith(suffix.lstrip("\n"))
    assert accepted[0]["content"].count("其他价格内容不能修改。") == 1


def test_ambiguous_or_title_only_sections_are_not_replaceable_and_fences_are_ignored():
    assert section_bounds("# 技术与威科夫报告\n\n## 价格\n10元\n\n## 风险\n未知") is None
    assert section_bounds("# 技术与威科夫报告\n\n## 威科夫当前\n候选\n\n## 威科夫历史\n旧判断") is None
    text = "```markdown\n# 威科夫示例\n```\n\n## 威科夫当前\n趋势背景：下跌；当前阶段：吸筹候选。\n\n## 其他\n保留"
    bounds = section_bounds(text)
    assert text[bounds[0]:bounds[1]].startswith("## 威科夫当前")


@pytest.mark.asyncio
async def test_correction_records_its_own_validation_error_and_stop_reason_without_raw_inputs():
    value, content, snapshot = method_case()
    rejections = []
    tool, accepted = make_repair_tool(content, snapshot, rejections=rejections)
    bad = deepcopy(value)
    bad["trend"] = "up"
    with pytest.raises(ValueError, match="趋势字段up"):
        await tool.handler(wyckoff=bad)
    record = {"attempted": True, "succeeded": False}
    record_correction_result(record, RunResult(stop_reason="max_tool_calls", tool_calls_executed=1), accepted, rejections)
    assert record["error"] == rejections[0] and record["validation_errors"] == rejections
    assert record["tool_calls"] == 1 and record["stop_reason"] == "max_tool_calls"
    assert "source_quote" not in record and "content" not in record
    with pytest.raises(ValueError, match="未支持的字段"):
        await tool.handler(wyckoff=value, private_key="must-never-enter-diagnostics")
    assert "must-never-enter-diagnostics" not in str(rejections)
    with pytest.raises(ValueError, match="其他报告章节"):
        await tool.handler(wyckoff=value, method_section=content + "\n## 其他章节\n不得改写")
    assert "其他报告章节" in rejections[-1]


def test_no_correction_submission_and_budget_denial_have_distinct_messages():
    no_submit = {"attempted": True, "succeeded": False}
    record_correction_result(no_submit, RunResult(stop_reason="end_turn", tool_calls_executed=0), [], [])
    assert "未提交有效" in no_submit["error"] and "end_turn" in no_submit["error"]
    denied = {"attempted": False, "succeeded": False, "error": "每日调用上限"}
    record_correction_result(denied, None, [], [])
    assert denied == {"attempted": False, "succeeded": False, "error": "每日调用上限"}
