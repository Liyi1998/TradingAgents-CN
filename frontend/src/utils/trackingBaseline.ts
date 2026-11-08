export function reportTime(value?: string | null): string {
  if (!value) return '生成时间未记录'
  // Mongo legacy timestamps without an offset are UTC, not browser-local time.
  const stamp = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : value + 'Z'
  const date = new Date(stamp)
  if (!Number.isFinite(date.getTime())) return '生成时间未记录'
  return date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
}

export function levelSourceDate(original: boolean, sourceDate?: string, baselineDate?: string, reviewDate?: string): string {
  return original ? `来源完整报告日期：${sourceDate || baselineDate || '未记录'}` : `来源每日研究日期：${reviewDate || '未记录'}`
}
