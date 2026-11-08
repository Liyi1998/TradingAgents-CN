/** Render only coherent shadow decisions. Never label a choice as execution. */
export function validDecisionResult(value: unknown): boolean {
  if (!value || typeof value !== 'object') return false
  const row = value as Record<string, any>, r = row.result
  if (row.status !== 'completed' || row.affects_decision !== false || row.external_call_attempted !== true || !r) return false
  if (!['WAIT', 'RESEARCH_REQUIRED', 'ENTRY_CANDIDATE'].includes(r.entry_action) ||
      !['HOLD_CANDIDATE', 'EXIT_REVIEW', 'RESEARCH_REQUIRED'].includes(r.existing_action) ||
      !['INSUFFICIENT', 'VETO_OR_EXIT', 'WITHIN_CONDITIONS'].includes(r.risk_state)) return false
  if (r.entry_action === 'ENTRY_CANDIDATE' && (row.packet?.strategy?.entry_candidate_permitted !== true || r.risk_state !== 'WITHIN_CONDITIONS')) return false
  if (r.risk_state === 'INSUFFICIENT' && r.existing_action !== 'RESEARCH_REQUIRED') return false
  if (r.existing_action === 'EXIT_REVIEW' && r.risk_state !== 'VETO_OR_EXIT') return false
  if (r.risk_state === 'WITHIN_CONDITIONS' && (r.existing_action !== 'HOLD_CANDIDATE' || r.entry_action === 'RESEARCH_REQUIRED')) return false
  return true
}

export function experimentTime(value: unknown): string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T/.test(value)) return '时间未记录'
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`
  const date = new Date(normalized)
  return Number.isNaN(date.getTime()) ? '时间未记录' : `${date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}（北京时间）`
}
