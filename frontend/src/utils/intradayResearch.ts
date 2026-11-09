import type { IntradayResearchValue, TrackingLevel } from '@/api/researchTracking'

export const intradayStateLabel = (state: string) => ({ candidate: '最新时段触及', not_triggered: '最新时段未触及', breach_candidate: '已低于保护参考', met: '规则满足', not_met: '尚未满足', invalidated: '否决规则触发', missing_data: '缺少数据', unsupported: '暂不支持完整核验', pending_close: '等待收盘', pending_session: '等待交易时段', insufficient: '旧版未分类结果', expired: '原条件已到期' }[state] || state)

export function conditionCounts(rows: { category?: string; state: string }[] = []) {
  const strategy = rows.filter(row => row.category === 'strategy')
  return { total: strategy.length, supported: strategy.filter(row => ['met', 'not_met', 'invalidated'].includes(row.state)).length,
    missing: strategy.filter(row => row.state === 'missing_data').length,
    unsupported: strategy.filter(row => row.state === 'unsupported').length,
    waiting: strategy.filter(row => ['pending_close', 'pending_session'].includes(row.state)).length }
}

export function intradayLevels(value: IntradayResearchValue | null): TrackingLevel[] {
  if (!value?.enabled || value.snapshot?.status !== 'ready') return []
  return value.zones.map(zone => ({
    kind: zone.kind === 'resistance' ? 'resistance' : 'support', label: `盘中候选·${zone.label} ${zone.observed_at.slice(11, 16)}`,
    lower: zone.lower, upper: zone.upper, condition: zone.condition, source_quote: `分钟结构 ${zone.observed_at} · ${zone.source}`,
    effective_from: zone.observed_at.slice(0, 10), valid_until: zone.valid_until, expired: false, price_basis: 'raw', currency: 'CNY', state: 'intraday_candidate'
  }))
}
