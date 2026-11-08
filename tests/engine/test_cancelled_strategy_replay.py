"""Withdrawn templates must not survive read-time or new-report normalization."""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from app.engine.decision_semantics import normalize_decision
from app.services.reports_service import _extract_structured_fields


def test_cancelled_default_does_not_survive_comma_fragment_or_read_adaptation():
    critique = '采纳保守派对仓位与退出模板的质疑：未来30%首次新增、3%计划亏损并非账户既有规则，也不是已生效额度'
    plan = {'conditions': '站稳21.78后更新预算，不自动BUY', 'entry_reference': 21.78}
    raw = {'action': '持有', 'account_position': 'HELD', 'unheld_action': 'WATCH', 'held_action': 'HOLD',
        'conditional_strategy': [plan, {'kind': 'conditional', 'limit_pct': 30, 'condition': critique}]}
    source = critique + '。取消默认30%新增、3%亏损及固定三分之一兑现。'
    before = deepcopy(raw)
    result = normalize_decision(raw, source)
    assert result['conditional_strategy'] == [plan]
    assert result['current_entry_limit_pct'] == 0
    assert result['total_position_limit_pct'] is None
    assert result['primary_action'] == 'HOLD'
    assert raw == before
    assert normalize_decision(result, source) == result


def test_later_explicit_withdrawal_removes_only_that_future_percentage():
    source = '未来首笔仓位30%。取消默认30%新增。未来首笔仓位5%，另获授权后才可执行。'
    result = normalize_decision({'unheld_action': 'WATCH'}, source)
    assert [item['limit_pct'] for item in result['conditional_strategy']] == [5]


def test_not_a_current_order_does_not_cancel_a_valid_future_plan():
    plan = {'status': '未触发、非当前订单', 'conditions': '须另获授权，不自动BUY', 'limit_pct': 30}
    source = '未来首笔仓位不超过30%，非当前订单。当前新增上限0%。'
    result = normalize_decision({'account_position': 'NONE', 'unheld_action': 'WATCH',
        'conditional_strategy': [plan]}, source)
    assert plan in result['conditional_strategy']
    assert result['current_entry_limit_pct'] == 0


@pytest.mark.parametrize('status', ['已取消', '撤销', 'rejected', 'revoked'])
def test_explicitly_withdrawn_structured_items_are_not_displayable_plans(status):
    result = normalize_decision({'conditional_strategy': [{'status': status, 'conditions': '原模板'}]}, '当前观望。')
    assert result['conditional_strategy'] == []


def test_latest_actual_held_report_preserves_one_valid_plan_without_rewriting_history():
    path = os.environ.get('CONDITIONAL_REPORT_SNAPSHOT')
    if not path:
        pytest.skip('Requires the authorized private latest-report snapshot')
    data = json.loads(Path(path).read_text())
    report = data['reports'][0]
    assert report['analysis_id'] == '002241_20261010_101304'
    submission = data['events'][report['task_id']][0]['payload']['input']
    raw = {**submission, **submission['decision_semantics']}
    before = deepcopy(report)
    normalized = normalize_decision(raw, submission['content'])
    expected = submission['decision_semantics']['conditional_strategy']
    assert len(expected) == 1
    assert normalized['conditional_strategy'] == expected
    adapted = _extract_structured_fields(report)
    assert adapted['decision']['conditional_strategy'] == expected
    assert adapted['decision']['primary_action'] == 'HOLD'
    assert adapted['decision']['current_entry_limit_pct'] == 0
    assert adapted['decision']['total_position_limit_pct'] is None
    assert adapted['decision']['confidence'] is None
    assert adapted['decision']['risk_level'] == 'High'
    assert report == before
