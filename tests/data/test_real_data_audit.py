"""Regression checks against the archived 300094 analysis, without mocks/LLM.

Set DATA_AUDIT_DIR to a real audit archive; otherwise these integration tests skip.
"""
import copy
import json
import os
from pathlib import Path
import pytest
from bson import json_util

@pytest.fixture(scope='module')
def archive():
    path = Path(os.environ.get('DATA_AUDIT_DIR','.ai_temp/data-audit-20260907'))
    if not (path/'data-before.json').exists():
        pytest.skip('Requires real archived analysis evidence')
    return json_util.loads((path/'data-before.json').read_text()), json.loads((path/'verification.json').read_text())

def test_capital_fields_not_truncated(archive):
    _, verified = archive
    data = json.loads(verified['money_flow_output'])['data']
    assert '69737645.02' in data and 'main_net_inflow' in data
    assert 'rows x' not in data

def test_intraday_session_and_auction_conservation(archive):
    _, verified = archive
    bars = verified['hour_bars']
    assert len(bars) == 4
    assert {b['datetime'][-8:] for b in bars} == {'10:30:00','11:30:00','14:00:00','15:00:00'}
    assert sum(b['volume'] for b in bars) == 136500500

def test_actual_margin_adapter_keeps_candidate_date(archive):
    import pandas as pd
    from app.data.sources.cn.akshare.adapter import AKShareCNAdapter
    _, verified = archive
    records = AKShareCNAdapter().adapt_margin_trading(pd.DataFrame(verified['margin_raw']))
    assert records[0].trade_date == '2026-09-04'
    assert records[0].margin_balance == 162804516

def test_actual_financial_snapshot_periods_and_units(archive):
    from app.services.mcp_data_cache import normalize_mx_financial
    original, _ = archive
    merged = {}
    for snapshot in sorted(original['mcp_data_cache'],key=lambda s:str(s.get('updated_at',''))):
        for record in normalize_mx_financial(snapshot.get('raw_text',''),'300094'):
            assert record['report_period'] <= '2026-09-07'
            merged.setdefault(record['report_period'],{}).update(record)
    current = merged['2026-06-30']
    assert current['debt_ratio'] == pytest.approx(95.19)
    assert current['gross_margin'] == pytest.approx(11.41)
    assert current['revenue'] == 1445000000
    assert current.get('bps') != .00088

def test_actual_summary_false_missing_claim(archive):
    from app.engine.agents.stage_4.summary_agent import _repair_false_coverage_claims
    original,_ = archive
    report = original['analysis_reports'][0]
    assert report['reports']['financial_news_report'] and report['reports']['social_media_report']
    updated = _repair_false_coverage_claims(copy.deepcopy(report['structured_summary']),{'news':True,'sentiment':True})
    assert '新闻报告与情绪报告缺失' not in updated['risk_assessment']['description']
    assert updated['final_signal'] == report['structured_summary']['final_signal']

def test_dataframe_formatter_keeps_real_sparse_fields(archive):
    import pandas as pd
    from app.engine.tools.common.format import format_result
    original,_ = archive
    output = format_result(pd.DataFrame(original['stock_financial_data']), 'real financial records')
    assert 'operating_cashflow' in output and '192300000' in output
    assert 'rows x' not in output
