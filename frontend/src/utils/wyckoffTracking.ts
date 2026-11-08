import type { WyckoffAnnotation, WyckoffPhase } from '@/api/researchTracking'

export const phaseNames: Record<WyckoffPhase, string> = { accumulation: '吸筹', testing: '试盘（旧口径）', markup: '拉升', distribution: '派发', markdown: '下跌', uncertain: '待判断' }
export const phaseColors: Record<WyckoffPhase, string> = { accumulation: '#457a9b', testing: '#a17b28', markup: '#398168', distribution: '#b45353', markdown: '#875970', uncertain: '#80858e' }
export const trendNames = { up: '上涨', down: '下跌', range: '横盘', uncertain: '未明确' }
export const phaseStatus = (value: WyckoffAnnotation['status']) => ({ candidate: '候选', supported: '本轮证据支持', transition: '转换待确认', invalidated: '原判断失效', uncertain: '证据不足' }[value])
export const phaseLabel = (value: WyckoffAnnotation) => `${phaseNames[value.phase]}${value.candidate_phase ? ' → ' + phaseNames[value.candidate_phase] : ''}`
export const phaseRole = (value: WyckoffAnnotation) => value.display_role === 'historical' ? '历史判断' : value.carried_forward ? '所选起点·沿用判断' : '所选起点·最近判断'

// Several retries can share a session. Show only the last known assessment on
// that candle per baseline. Different baselines on the same date remain visible.
export function phaseMarkers(history: WyckoffAnnotation[], dates: string[]) {
  const available = new Set(dates)
  const days = new Map<string, WyckoffAnnotation>()
  for (const value of history) if (available.has(value.as_of)) days.set(`${value.as_of}:${value.source_analysis_id}`, value)
  return [...days.values()].sort((a, b) => a.as_of.localeCompare(b.as_of))
}
