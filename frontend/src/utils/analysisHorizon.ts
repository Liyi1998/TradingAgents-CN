import type { AnalysisHorizon } from '@/types/analysis'

export const ANALYSIS_HORIZONS = [
  { value: 'short', label: '短线', period: '近期交易', description: '关注近期量价、资金和事件，制定未来 3 / 7 个交易日的条件观察与交易计划。' },
  { value: 'medium', label: '中线', period: '1–3 个月', description: '关注阶段性盈利、估值与催化因素，给出建仓条件、逻辑失效条件和复查节点。' },
  { value: 'long', label: '长线', period: '6–12 个月', description: '关注经营趋势、现金流和长期价值，给出估值情景、持有依据与退出条件。' },
] as const

export function isAnalysisHorizon(value: unknown): value is AnalysisHorizon {
  return value === 'short' || value === 'medium' || value === 'long'
}

/** Read the saved task/report only; form preferences must never relabel historical reports. */
export function resolveAnalysisHorizon(value: unknown): AnalysisHorizon {
  if (isAnalysisHorizon(value)) return value
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const record = value as Record<string, unknown>
    if (isAnalysisHorizon(record.analysis_horizon)) return record.analysis_horizon
    const parameters = record.parameters
    if (parameters && typeof parameters === 'object' && !Array.isArray(parameters)) {
      const saved = (parameters as Record<string, unknown>).analysis_horizon
      if (isAnalysisHorizon(saved)) return saved
    }
  }
  // Tasks created before the horizon feature used the short-term workflow.
  return 'short'
}

export function analysisHorizonLabel(value: unknown, compact = false): string {
  const horizon = resolveAnalysisHorizon(value)
  const option = ANALYSIS_HORIZONS.find(item => item.value === horizon)!
  return compact ? option.label : `${option.label}（${option.period}）`
}
