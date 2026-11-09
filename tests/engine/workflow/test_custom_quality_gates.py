"""Real deterministic quality checks retained across the compiled-workflow merge."""
import pytest

from app.engine.orchestrator.pipeline import (
    AnalysisQualityError, _is_invalid_report_content, _require_valid_reports,
    _require_valid_debate_rounds, _require_valid_summary,
)

def test_partial_reports_fail_without_losing_completed_content():
    reports = {'market_report': '完整市场分析正文',
               'short_term_capital_report': '❌ 分析失败：LLM 返回空响应'}
    with pytest.raises(AnalysisQualityError, match='已有 1 份报告正文保留'):
        _require_valid_reports(reports, list(reports), '基础分析')
    assert reports['market_report'] == '完整市场分析正文'

def test_long_report_with_a_local_data_gap_is_not_a_failed_report():
    report = '# 资金分析\n' + '有效行情与资金数据。' * 100 + '\n部分数据获取失败。'
    assert not _is_invalid_report_content(report)

def test_every_debate_side_and_round_must_be_present():
    state = {'rounds': [{'bull': '有效看多论点', 'bear': '有效看空论点'},
                        {'bull': '有效补充论点', 'bear': '❌ 执行异常'}]}
    with pytest.raises(AnalysisQualityError, match=r'bear\[2\]'):
        _require_valid_debate_rounds(state, ['bull', 'bear'], 2, '研究辩论')

def test_failed_summary_is_not_treated_as_a_completed_report():
    with pytest.raises(AnalysisQualityError):
        _require_valid_summary({'analysis_summary': 'JSON解析失败，无法生成分析摘要'})
    _require_valid_summary({'analysis_summary': '完整分析结论'})
