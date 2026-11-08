/** Display-only projection. Never turn a shadow assessment into trade authorization. */
type RecordValue = Record<string, unknown>
const object = (value: unknown): RecordValue => value && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {}
const owns = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key)

const reasons: Record<string, string> = {
  SUPPORTED: '所给证据支持完整方案的全部前提',
  EXPLICITLY_UNMET: '每个备选方案均有明确不满足的前提',
  MISSING_EVIDENCE: '缺少必要证据',
  SCOPE_MISMATCH: '日期、周期、标的或数据口径不一致',
  CONFLICTING_EVIDENCE: '同口径证据存在冲突',
  AMBIGUOUS_CONDITIONS: '条件或备选方案不明确',
  UNSUPPORTED_SYMBOL: '标的代码不在当前核验支持范围',
  INVALID_ANALYSIS_DATE: '缺少有效分析日期',
  UNKNOWN_HORIZON: '分析周期不明确',
  MISSING_PLAN: '缺少可核验的交易方案',
  TRUNCATED_INPUT: '材料超过摘录上限，为避免遗漏前提已跳过外发',
  REDACTED_INPUT: '材料需脱敏删减，为避免改变条件含义已跳过外发',
  INPUT_BUDGET_EXCEEDED: '材料总长度超过核验上限',
  insufficient_input: '输入材料不足或未通过完整性检查',
  external_calls_disabled: '当时未允许向 Jev 外发，仅保存本地记录',
  key_missing: '未配置可用的 Jev 密钥',
  configuration_changed: '发送前设置发生变化，已停止外发',
  daily_limit: '已达到当日调用尝试上限',
  interrupted_or_result_not_saved_no_auto_retry: '核验中断或结果未保存；不会自动重发',
  condition_model_version_changed: '返回的模型版本不符合预期，结果未采纳',
  condition_answers_inconsistent: '返回字段互相矛盾，结果未采纳',
  condition_answer_keys_invalid: '返回字段不符合核验协议，结果未采纳',
  condition_external_calls_disabled: '外发已关闭，未调用 Jev',
  condition_key_missing: '未配置可用的 Jev 密钥',
  TimeoutError: '核验超时',
  ReadTimeout: '等待 Jev 响应超时',
  ConnectTimeout: '连接 Jev 超时',
  ConnectError: '无法连接 Jev 服务',
}

export function conditionReason(value: unknown): string {
  if (typeof value !== 'string') return '未提供具体原因'
  if (owns(reasons, value)) return reasons[value]
  if (/^condition_http_\d{3}$/.test(value)) return `Jev 服务返回 HTTP ${value.slice(-3)}，没有可用核验结果`
  return '未识别的原因，请联系管理员查看记录'
}

function safeLabel(value: unknown): string {
  return typeof value === 'string' && /^[a-zA-Z0-9._-]{1,80}$/.test(value) ? value : '未记录'
}

function timestamp(value: unknown): string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T/.test(value)) return '未记录'
  // Mongo naive UTC timestamps must not be interpreted as browser-local time.
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`
  const date = new Date(normalized)
  return Number.isNaN(date.getTime()) ? '未记录' : `${date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}（北京时间）`
}

export function conditionCheckView(value: unknown) {
  const row = object(value)
  const result = object(row.result)
  const state = typeof row.status === 'string' ? row.status : ''
  const names: Record<string, string> = {
    running: '核验处理中', completed: '核验已完成', skipped: '已跳过 · 未调用 Jev',
    not_called: '未调用 Jev', unavailable: '核验失败', incomplete: '核验未完成',
  }
  const conditions: Record<string, string> = { met: '所给证据支持条件满足', not_met: '有明确条件未满足', insufficient_evidence: '证据不足，无法判断' }
  const conflicts: Record<string, string> = { present: '存在同口径证据冲突', not_evident: '未发现明确冲突（不代表事实已核实）', unknown: '证据不足，冲突情况未知' }
  const codes = Array.isArray(result.reason_codes) ? result.reason_codes : []
  const status = String(result.condition_status || '')
  const conflict = String(result.evidence_conflict || '')
  const reason = codes[0]
  const coherent = (status === 'met' && conflict === 'not_evident' && reason === 'SUPPORTED') ||
    (status === 'not_met' && conflict !== 'present' && reason === 'EXPLICITLY_UNMET') ||
    (status === 'insufficient_evidence' && ['MISSING_EVIDENCE', 'SCOPE_MISMATCH', 'CONFLICTING_EVIDENCE', 'AMBIGUOUS_CONDITIONS'].includes(String(reason)))
  const valid = row.mode === 'shadow' && row.affects_decision === false && owns(names, state) &&
    (!['skipped', 'not_called'].includes(state) || row.external_call_attempted === false) &&
    (state !== 'completed' || (row.external_call_attempted === true && owns(conditions, status) && owns(conflicts, conflict) && codes.length === 1 && coherent))
  const completed = Boolean(valid && state === 'completed')
  const actions: Record<string, string> = { WATCH: '观望', BUY: '买入', SELL: '卖出', HOLD: '持有', REDUCE: '减仓', 观望: '观望', 买入: '买入', 卖出: '卖出', 持有: '持有', 减仓: '减仓' }
  const reference = object(row.original_reference)
  const original = [['unheld_action', '未持仓'], ['held_action', '已持仓'], ['action', '总体动作']]
    .flatMap(([key, label]) => owns(actions, String(reference[key])) ? [`${label}：${actions[String(reference[key])]}`] : [])
  const guards = Array.isArray(row.input_guards) ? row.input_guards : []
  const reasonValues = completed ? codes : [...(row.reason ? [row.reason] : []), ...guards]
  return {
    valid: Boolean(valid), completed,
    title: valid ? names[state] : '核验记录格式无法识别',
    tone: (completed ? 'info' : state === 'unavailable' || state === 'incomplete' || !valid ? 'warning' : 'info') as 'info' | 'warning',
    condition: completed ? conditions[status] : '', conflict: completed ? conflicts[conflict] : '',
    reasons: [...new Set(reasonValues.map(conditionReason))],
    original: original.length ? original.join('；') : '未保存可展示的原系统动作',
    model: safeLabel(completed ? row.model : row.requested_model),
    modelLabel: completed ? '实际核验模型' : '配置的核验模型',
    time: timestamp(row.finished_at || row.created_at),
    elapsed: typeof row.elapsed_seconds === 'number' && Number.isFinite(row.elapsed_seconds) && row.elapsed_seconds >= 0 ? `${row.elapsed_seconds.toFixed(2)} 秒` : '未记录',
    attempted: row.external_call_attempted === true ? '已尝试调用（不代表成功）' : row.external_call_attempted === false ? '未尝试调用' : '未记录',
  }
}
