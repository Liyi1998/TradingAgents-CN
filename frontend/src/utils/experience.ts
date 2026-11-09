import type { ReflectionRecord } from '@/api/experience'

export function reflectionLabel(row: ReflectionRecord): string {
  if (row.reflection_status === 'done' && row.reflection_lesson) return '复盘已入库'
  if (row.reflection_lesson) return '正文已保存 · 检索入库未完成'
  if (row.reflection_status === 'failed') return '复盘生成未完成'
  if (row.signal === 'Hold') return '观望记录不生成方向复盘'
  if (row.status === 'pending') return '等待评估窗口结束'
  if (row.status === 'unfillable') return '无法结算 · 不生成收益复盘'
  return '已结算 · 等待复盘'
}
