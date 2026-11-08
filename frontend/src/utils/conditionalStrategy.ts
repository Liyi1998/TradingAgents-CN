import type { ConditionalStrategy } from '@/types/analysis'

function text(value: unknown): string {
  if (typeof value === 'string') return value.trim()
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  if (Array.isArray(value)) return value.map(text).filter(Boolean).join('、')
  return ''
}

/** Display source-supplied references as conditional, never current orders. */
export function conditionalStrategyText(value: unknown): string {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return '条件未明确（非当前授权）'
  }
  const plan = value as ConditionalStrategy
  const parts: string[] = []
  const add = (label: string, keys: string[]) => {
    const values = [...new Set(keys.map(key => text(plan[key])).filter(Boolean))]
    if (values.length) parts.push(`${label}：${values.join('；')}`)
  }
  add('策略', ['name', 'type'])
  add('状态', ['status'])
  add('条件', ['condition', 'conditions', 'trigger', 'requirement'])
  add('入场参考', ['entry_reference', 'entry_reference_range'])
  add('人民币入场参考区间', ['reference_range_cny'])
  add('失效/保护参考', ['invalidation_reference', 'protection_reference'])
  add('技术目标/压力参考', ['technical_targets', 'conditional_targets', 'first_target', 'second_target_snapshot'])
  add('失效条件', ['invalidation'])
  const limitKeys = ['limit_pct', 'future_position_limit_pct', 'future_total_position_limit_pct']
  const limits = [...new Set(limitKeys.filter(key => key in plan).map(key => {
    const limit = plan[key]
    return typeof limit === 'number' && Number.isFinite(limit) && limit >= 0 && limit <= 100
      ? `${limit}%` : '未明确'
  }))]
  if (limits.length) parts.push(`后续仓位上限：${limits.join('；')}`)
  return parts.length ? parts.join('；') : '条件未明确（非当前授权）'
}

export function conditionalStrategyTexts(value: unknown): string[] {
  return Array.isArray(value) ? [...new Set(value.map(conditionalStrategyText))] : []
}
