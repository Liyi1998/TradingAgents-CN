"""Source-bound old report annotations, including the observed 000503 failure."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.engine.report_chart_levels import report_chart_levels
from app.engine.strategy_partition import MARKERS
from app.services.research_tracking import ResearchTrackingService, TRACKS, VERSIONS

# Exact public price section from the 2026-10-26 report. The rejected scenario
# and account block below exercise scope isolation, not an inferred decision.
PRICE_SECTION = """### (2) 关键价格点位设定（人民币计价）
- **当前买入指令价**：**禁止买入（无有效买入区间）**
- **第一反弹减仓位**：**7.70 元 - 7.75 元**（触及强制减持既有持仓的 50%）
- **清仓目标价格**：**7.93 元 - 7.95 元**（触及强制市价清空剩余 50% 持仓）
- **统一止损价位**：**7.10 元**（盘中击穿且 30 分钟未收回，或收盘跌破 7.14 元，强制市价全额出清）
"""


def baseline():
    rejected = "## 驳回方案\n### 关键价格点位\n- 止损价：7.40 元（已驳回）\n## 最终执行指令\n"
    public = rejected + PRICE_SECTION + "### 退出条件\n原报告的所有退出与否决条件保持有效，触价不等于条件满足。"
    source = f"{MARKERS[0]}\n{public}\n{MARKERS[1]}\n\n{MARKERS[2]}\n### 关键价格点位\n- 持仓成本：6.00元\n{MARKERS[3]}"
    return {"analysis_id": "source-report", "as_of": "2026-10-26", "reports": {"final_trade_decision": source}}


def test_actual_report_prices_survive_missing_daily_review_without_new_authority():
    b = baseline(); original = deepcopy(b)
    levels, notice = report_chart_levels(b, "2026-10-27T13:25:47+08:00")
    assert [(p['lower'], p['upper']) for p in levels] == [(7.7, 7.75), (7.93, 7.95), (7.1, 7.1)]
    assert [p['kind'] for p in levels] == ['exit', 'target', 'exit']
    for p in levels:
        assert p['source_quote'] == b['reports']['final_trade_decision'].splitlines()[p['source_line']-1]
        assert p['state'] == 'report_reference' and p['valid_until'] is None
        assert p['effective_from'] == '2026-10-27' and p['price_basis'] == 'unspecified'
    assert '7.14' in levels[2]['condition'] and '30 分钟' in levels[2]['condition']
    assert '尚未' in notice and b == original


@pytest.mark.parametrize('value', ['7.10%', '2026-10-27', '8.00-7.00元', '-7.10元', 'NaN元', '7.10元（前复权）', '7.10美元', '7.10元（已作废）', '7_10元', '9'*400+'元'])
def test_percent_dates_unsupported_basis_and_revoked_values_are_not_plotted(value):
    b = baseline();b['reports']['final_trade_decision'] = '## 关键价格点位\n- 保护价：'+value
    assert report_chart_levels(b, '2026-10-27')[0] == []


def test_final_decision_wins_and_unknown_or_malformed_sections_do_not_guess():
    b = baseline();b['reports']['trader_investment_plan'] = '## 关键价格点位\n- 买入价：6.78元'
    assert len(report_chart_levels(b, '2026-10-27')[0]) == 3
    b['reports']['final_trade_decision'] = '当前观望，没有可执行买入区间。'
    assert report_chart_levels(b, '2026-10-27')[0] == []  # don't resurrect superseded trader plan
    b['reports']['final_trade_decision'] = MARKERS[0]+'\n'+PRICE_SECTION
    assert report_chart_levels(b, '2026-10-27')[0] == []
    b['reports']['final_trade_decision'] = '## 已驳回\n'+PRICE_SECTION
    assert report_chart_levels(b, '2026-10-27')[0] == []
    b['reports']['final_trade_decision'] = {'content': PRICE_SECTION}
    assert report_chart_levels(b, '2026-10-27')[0] == []


def test_table_prices_and_conditional_unapproved_ranges_keep_original_meaning():
    b = baseline();b['reports']['final_trade_decision'] = '## 关键价格点位（人民币、不复权）\n| 入场参考 | 10.00–10.50元（仅未来研究，未批准买入） |\n| 止损价 | 9.5元（失效退出） |'
    levels, _ = report_chart_levels(b, '2026-10-27')
    assert len(levels) == 2 and levels[0]['lower'] == 10 and levels[0]['upper'] == 10.5
    assert levels[0]['price_basis'] == 'raw' and '未批准' in levels[0]['condition']


def test_named_nested_price_list_in_final_summary_keeps_bounds_and_conditions():
    section = '''## 风控裁决摘要
- **核心基准价格区间**：
  - 减仓/第一阻力区间：**14.00 元 - 14.63 元**（测试 5 日均线压制）
  - 第二强阻力区间：**15.50 元 - 15.65 元**（前期平台）
  - 核心防守止损红线：**12.50 元**（击穿前低12.66元后需核验）
- 另一方案：
  - 支撑价：1.00元（不属于该点位段落）
## 辩论摘要
- 止损价：2.00元
'''
    b = exit_baseline(section)
    points, _ = report_chart_levels(b, '2026-11-03')
    assert [(p['lower'], p['upper']) for p in points] == [(14, 14.63), (15.5, 15.65), (12.5, 12.5)]
    assert '12.66' in points[-1]['condition']
    assert all(p['state'] == 'report_reference' and p['valid_until'] is None for p in points)
    assert all(p['source_quote'] == b['reports']['final_trade_decision'].splitlines()[p['source_line']-1] for p in points)
    assert report_chart_levels(exit_baseline(section.replace('## 风控裁决摘要', '## 驳回方案')), '2026-11-03')[0] == []
    assert report_chart_levels(exit_baseline('无公共价格', section), '2026-11-03')[0] == []
    b['reports']['final_trade_decision'] = section
    assert report_chart_levels(b, '2026-11-03')[0] == []


EXIT_CLAUSES = """## 最终执行指令与调整后交易方案
### 已持仓账户的具体清仓执行节奏
  - 若盘中出现技术性超跌反抽触及 **7.05 元 - 7.15 元**，立即市价挂单减仓 **50%**；
  - 若反抽动能延伸至前期颈线及 MA60 强阻力带 **7.15 元 - 7.285 元**，立即市价清空剩余 **50%** 头寸，达成 100% 清仓。
  - 若开盘惯性下挫，以 **6.75 元**（第一支撑线）作为动态观察线；
  - 若盘中有效击穿 **6.70 元**，放弃反抽幻想，立即以市价指令全部卖出，坚决规避向 6.54 元及 6.12 元的加速下跌。
### 未来条件研究方案（非当前执行订单）
  - 若实质性收复 7.30 元，必须同时满足资金条件，保持禁买状态。
"""


def exit_baseline(public=EXIT_CLAUSES, account='账户事实不参与图表提取。'):
    return {'analysis_id': 'exit-report', 'as_of': '2026-10-27', 'reports': {'final_trade_decision':
        f'{MARKERS[0]}\n{public}\n{MARKERS[1]}\n\n{MARKERS[2]}\n{account}\n{MARKERS[3]}'}}


def test_explicit_public_exit_clauses_preserve_full_conditions_and_three_decimal_price():
    b = exit_baseline(); original = deepcopy(b)
    levels, _ = report_chart_levels(b, '2026-10-28')
    assert [(p['lower'], p['upper']) for p in levels] == [(7.05, 7.15), (7.15, 7.285), (6.75, 6.75), (6.7, 6.7)]
    assert [p['kind'] for p in levels] == ['exit', 'exit', 'support', 'exit']
    assert '50%' in levels[1]['condition'] and '6.54 元及 6.12 元' in levels[3]['condition']
    for p in levels:
        assert p['source_quote'] == b['reports']['final_trade_decision'].splitlines()[p['source_line']-1]
        assert p['source_date'] == '2026-10-27' and p['effective_from'] == '2026-10-28'
        assert p['state'] == 'report_reference' and p['valid_until'] is None
    assert b == original


@pytest.mark.parametrize('clause', [
    '若盘中触及 7.15 元，暂不卖出。', '若盘中触及 7.15 元，禁止清仓。',
    '若盘中未触及 7.15 元，立即清仓。', '若盘中触及 -7.15 元，立即清仓。',
    '若盘中触及 8.00 元 - 7.15 元，立即清仓。', '若盘中触及 7.15 美元，立即清仓。',
    '若盘中触及 7.15 元，立即清仓（前复权）。', '若盘中触及 7.15%，立即清仓。',
    '若盘中触及 2026-10-27，立即清仓。',
])
def test_exit_clause_does_not_turn_negation_or_invalid_prices_into_exit(clause):
    b = exit_baseline('### 具体清仓执行节奏\n- '+clause)
    assert report_chart_levels(b, '2026-10-27')[0] == []


def test_exit_clause_excludes_unpartitioned_rejected_account_and_ambiguous_sources():
    b = exit_baseline(); b['reports']['final_trade_decision'] = EXIT_CLAUSES
    assert report_chart_levels(b, '2026-10-27')[0] == []
    for b in [exit_baseline('## 已驳回\n'+EXIT_CLAUSES.replace('## 最终执行指令与调整后交易方案', '### 旧指令').replace('### 已持仓', '#### 已持仓')),
              exit_baseline('## 公共研究\n当前观望', EXIT_CLAUSES),
              exit_baseline(EXIT_CLAUSES, EXIT_CLAUSES)]:
        assert report_chart_levels(b, '2026-10-27')[0] == []


EXECUTION_TABLE = """## 4. 最终执行指令与交易计划（修正版）
### 4.2 已持仓执行指令（REDUCE 分步执行手册）
| 触发场景 | 执行价格/条件 | 执行动作 | 仓位变化 | 依据说明 |
|:---|:---|:---:|:---:|:---|
| **弱势反抽减持** | **16.20 元 - 16.50 元** | **主动减仓 50%** | 持仓减半 | 等待反抽 |
| **强阻力位出清** | **16.76 元**（MA20） | **全部清仓 (SELL ALL)** | 归零 | 阻力出清 |
| **首道破位防御** | **日线收盘价跌破 15.60 元** | **被动减仓 50%** | 减半 | 确认收盘 |
| **绝对底线止损** | **有效跌破 15.00 元** | **全部强制止损出清** | 归零 | 防范向 11.42 元下探 |
### 4.3 未持仓条件式研究方案（非实时订单，分栏推演）
当前严禁开仓。仅未来 3 个交易日条件观察。
- **观察基准支撑位**：**15.16 元**（BOLL下轨）
- **买入动作转换前置条件（必须全部满足，缺一不可）**：
  1. 价格缩量企稳，日内成交量萎缩 40% 以上；
  2. 已验证的主力资金单日净流入超过 3,000 万元；
  3. 放量阳线突破前一日高点。
- **条件参数**：
  - 条件入场位：**15.16 元**；
  - 保护性止损位：**14.50 元**（止损幅度 -4.35%）；
  - 第一反弹目标位：**16.76 元**（收益空间 +10.55%）；
  - 首次尝试仓位上限：**≤ 3.0%**。
### 4.4 否决条件
连续两日失守则重新核验完整方案。
"""


def test_multi_column_execution_tables_and_future_parameters_keep_separate_branches():
    b = exit_baseline(EXECUTION_TABLE)
    points, _ = report_chart_levels(b, '2026-10-27')
    held = [p for p in points if p.get('branch') == 'held']
    unheld = [p for p in points if p.get('branch') == 'unheld']
    assert [(p['lower'],p['upper']) for p in held] == [(16.2,16.5),(16.76,16.76),(15.6,15.6),(15,15)]
    assert [p['lower'] for p in unheld] == [15.16,15.16,14.5,16.76]
    assert '日线收盘价跌破' in held[2]['condition']
    assert all('3,000 万元' in p['condition'] and '当前严禁开仓' in p['condition'] for p in unheld)
    for p in points:
        assert p['source_quote'] == b['reports']['final_trade_decision'].splitlines()[p['source_line']-1]
        assert p['state'] == 'report_reference' and p['valid_until'] is None


@pytest.mark.parametrize('text', [EXECUTION_TABLE.replace('最终执行指令', '已驳回执行指令'),
    EXECUTION_TABLE.replace('执行价格/条件', '收益百分比').split('### 4.3')[0],
    EXECUTION_TABLE.replace('主动减仓', '禁止减仓').replace('全部清仓', '禁止清仓').replace('被动减仓','暂缓减仓').replace('全部强制止损','禁止止损').split('### 4.3')[0]])
def test_table_rejection_context_missing_headers_and_negation_do_not_create_prices(text):
    assert report_chart_levels(exit_baseline(text), '2026-10-27')[0] == []


def test_ladder_table_preserves_observation_prerequisites_and_stop_condition():
    section = '''## 最终执行指令与交易计划修正
### 价格参数修正与执行梯队
| 梯队阶段 | 价格参考区间 | 触发前提条件与量价特征 | 仓位调度指令 |
|:---|:---:|:---|:---:|
| 未持仓观察梯队1 | 22.67 元 — 22.85 元 | 30分钟K线缩量企稳 | 首笔建仓30%（占计划额度） |
| 第一止盈目标 | 24.31 元 — 24.60 元 | 触及公允价值及平台下沿 | 已持仓主动减持50% |
| 硬性止损线 | 21.90 元 | 日K线有效收于21.90元下方，或盘中放量跌破21.50元 | 无条件清仓止损，严禁补仓 |
'''
    levels, _ = report_chart_levels(exit_baseline(section), '2026-10-27')
    assert [(p['lower'],p['upper'],p['branch']) for p in levels] == [(22.67,22.85,'unheld'),(24.31,24.6,'held'),(21.9,21.9,'held')]
    assert '30分钟' in levels[0]['condition'] and '21.50元' in levels[2]['condition']


@pytest.mark.asyncio
async def test_research_landing_lists_only_owner_tracks_and_no_report_bodies(real_mongo_db):
    db = real_mongo_db; owner, other = uuid4().hex, uuid4().hex
    try:
        await db[TRACKS].insert_many([
            {'_id':uuid4().hex,'user_id':u,'symbol':s,'name':'验收股票','baseline_date':'2026-10-27',
             'created_at':datetime.now(timezone.utc),'baseline':{'reports':{'private':'not for listing'}},'settings':{'auto_enabled':False}}
            for u,s in [(owner,'603232'),(owner,'000503'),(other,'002241')]])
        before = await db[TRACKS].count_documents({})
        rows = await ResearchTrackingService(db).list_tracks(owner)
        assert {row['symbol'] for row in rows} == {'603232','000503'}
        assert all(set(row) == {'symbol','name','baseline_date','auto_enabled'} for row in rows)
        assert await db[TRACKS].count_documents({}) == before
        assert await ResearchTrackingService(db).list_tracks(uuid4().hex) == []
    finally:
        await db[TRACKS].delete_many({'user_id':{'$in':[owner,other]}})


@pytest.mark.asyncio
async def test_board_returns_base_references_before_update_and_after_failed_review_without_writes(real_mongo_db):
    # Real isolated MongoDB: also checks ownership and immutable records.
    mongo_db = real_mongo_db
    owner, tid, vid = uuid4().hex, uuid4().hex, uuid4().hex
    b = baseline(); now = datetime.now(timezone.utc)
    track = {'_id':tid,'user_id':owner,'symbol':'000503','baseline':b,'baseline_analysis_id':b['analysis_id'],
             'baseline_date':b['as_of'],'created_at':now,'latest_version_id':None}
    await mongo_db[TRACKS].insert_one(track)
    service = ResearchTrackingService(mongo_db)
    try:
        before = await service.board('000503', owner)
        assert before['version'] is None and len(before['baseline']['levels']) == 3
        await mongo_db[VERSIONS].insert_one({'_id':vid,'track_id':tid,'user_id':owner,'as_of':'2026-10-26',
            'created_at':now,'status':'partial','review':None,'modules':{},'snapshot':{'bars':[]}})
        await mongo_db[TRACKS].update_one({'_id':tid},{'$set':{'latest_version_id':vid}})
        original = await mongo_db[VERSIONS].find_one({'_id':vid})
        after = await service.board('000503', owner)
        assert after['version']['levels'] == [] and len(after['baseline']['levels']) == 3
        assert after['version']['review'] is None
        assert await mongo_db[VERSIONS].find_one({'_id':vid}) == original
        assert (await service.board('000503', uuid4().hex))['track'] is None
    finally:
        await mongo_db[TRACKS].delete_one({'_id':tid})
        await mongo_db[VERSIONS].delete_one({'_id':vid})
