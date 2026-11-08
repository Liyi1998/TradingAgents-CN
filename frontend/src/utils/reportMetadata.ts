export function analysisCompletedAt(task: any, report: any): string | undefined {
  for (const value of [task?.completed_at, task?.end_time, report?.created_at, report?.completed_at]) {
    if (typeof value !== 'string' || !value.trim()) continue
    const text = value.trim().replace(' ', 'T')
    const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text) ? text : text + 'Z'
    if (Number.isFinite(Date.parse(normalized))) return normalized
  }
  return undefined
}

export function reportConfidence(report: any): string {
  const value = report?.confidence_score
  if (value != null && value !== '' && typeof value !== 'boolean') {
    const score = Number(value)
    if (Number.isFinite(score) && score >= 0 && score <= 100) return `${Math.round(score <= 1 ? score * 100 : score)}%`
  }
  const label = report?.structured_summary?.confidence_label
  return typeof label === 'string' && label.trim() ? `${label.trim()}（未量化）` : '报告未提供'
}
