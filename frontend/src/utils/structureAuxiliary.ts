import type { TrackingLevel } from '@/api/researchTracking'

export interface StructureTpoDay {
  date: string
  status: string
  val: number | null
  vah: number | null
  first_knowable_time?: string
  reason: string
}
export interface StructureEvent {
  id: string
  kind: string
  status: string
  direction?: string | null
  event_time?: string
  price?: number | null
  lower?: number | null
  upper?: number | null
  first_knowable_time?: string
  invalidation?: string
}
export interface StructureAuxiliaryValue {
  enabled: boolean
  baseline_analysis_id?: string
  model_called?: boolean
  decision_effect?: string
  report_mutated?: boolean
  status?: string
  as_of?: string | null
  source?: string | null
  trade_authorized?: boolean
  vote?: boolean
  performance_claim?: boolean
  issues?: string[]
  limitations?: string[]
  conflicts?: { left: string; right: string; left_reading: string; right_reading: string; resolution: string }[]
  gates?: { name: string; status: string; reason: string; as_of?: string | null }[]
  tpo?: { label: string; days: StructureTpoDay[] }
  smc?: { state: string; latest: StructureEvent | null; events: StructureEvent[]; order_blocks: { status: string; reason: string } }
  overnight_gaps?: { id: string; status: string; direction?: string; event_time: string; first_knowable_time: string; reason: string; previous_close?: number; next_open?: number }[]
  relative?: {
    stock: { status: string; reason: string; as_of?: string | null }
    index: { status: string; reason: string; as_of?: string | null; symbol?: string }
    sector: { status: string; reason: string; as_of?: string | null }
    excess_close_to_close: number | null
    attribution: string
  }
  wyckoff_background?: { status: string; phase: string | null; trend: string | null; direction: string; as_of: string | null; reason: string; mutated: boolean }
  preserved?: { held_action: string | null; unheld_action: string | null; wyckoff_phase: string | null; report_mutated: boolean }
}

export function structureLevels(value: StructureAuxiliaryValue | null, show: boolean): TrackingLevel[] {
  if (!show || !value?.enabled || value.trade_authorized || value.vote || value.performance_claim) return []
  const levels: TrackingLevel[] = []
  const day = [...(value.tpo?.days || [])].reverse().find(item => item.status === 'ready' && item.val != null && item.vah != null)
  if (day) {
    levels.push({
      kind: 'support', label: `TPO接受区近似 ${day.date}`, lower: day.val as number, upper: day.vah as number,
      condition: '30分钟OHLC区间覆盖的bar-based TPO近似，不是成交分布，也不是目标价或下单区间。',
      source_quote: day.reason, effective_from: day.date, valid_until: day.date, expired: false,
      price_basis: 'raw', currency: 'CNY', state: 'intraday_candidate',
    })
  }
  const event = value.smc?.latest
  if (event && ['confirmed', 'candidate', 'partially_filled'].includes(event.status) && event.lower != null && event.upper != null) {
    const dayText = (event.first_knowable_time || '').slice(0, 10)
    levels.push({
      kind: 'resistance', label: `SMC${event.kind} ${event.first_knowable_time || ''}`.trim(),
      lower: Math.min(event.lower, event.upper), upper: Math.max(event.lower, event.upper),
      condition: event.invalidation || '结构事件只记录确认和失效，不构成交易指令。',
      source_quote: `首次可知 ${event.first_knowable_time || '未知'}`, effective_from: dayText, valid_until: dayText || null,
      expired: false, price_basis: 'raw', currency: 'CNY', state: 'intraday_candidate',
    })
  }
  return levels
}
