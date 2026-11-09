export const finalActionLabel = (value: string) => ({ BUY: '买入建议', ADD: '加仓建议', HOLD: '持有建议', REDUCE: '减仓建议', SELL: '卖出建议', WAIT: '观望', RESEARCH_REQUIRED: '证据不足，需研究' } as Record<string, string>)[value] || '未知，不采纳'
export function validFinalAction(row: any): boolean {
  if (row?.status !== 'completed' || row.affects_decision !== false || row.external_call_attempted !== true) return false
  return ['suggested', 'effective'].every(key => row[key]
    && ['BUY', 'WAIT', 'RESEARCH_REQUIRED'].includes(row[key].unheld_action)
    && ['ADD', 'HOLD', 'REDUCE', 'SELL', 'RESEARCH_REQUIRED'].includes(row[key].held_action)
    && ['SUPPORTED', 'INSUFFICIENT', 'CONFLICT'].includes(row[key].evidence_state))
}
export const finalReasonLabel = (value: string) => ({
  INPUT_NOT_READY: '必要策略材料尚未就绪，未调用', EXTERNAL_DISABLED: '外发开关关闭，未调用', KEY_MISSING: '密钥不可用，未调用',
  CONFIG_CHANGED: '配置变化，已停止', DAILY_LIMIT: '已达共享日调用上限', SOURCE_OR_CONFIG_CHANGED: '报告或配置已变化，未发送',
  CALL_OR_VALIDATION_FAILED: '连接或响应校验失败，不自动重试', INTERRUPTED: '调用或保存中断，不自动重试',
  PLAN_TOO_LARGE: '最终策略超出完整发送上限', PLAN_CONSTRAINTS_REDACTED: '策略与敏感内容混写，无法确认脱敏后条件完整',
  PRIVATE_DEPENDENCY: '策略仍依赖未提供的账户规则或未明确授权', MISSING_PLAN: '缺少完整最终方案', MISSING_EVIDENCE: '缺少分析证据',
  PARTITION_INCOMPLETE: '生成时未形成完整的公开策略／账户约束分区；原报告保留，未调用',
  PARTITION_SOURCE_CHANGED: '分区与报告原文不一致，未调用', ACCOUNT_RULES_UNRESOLVED: '两个分支均有待核验的本地账户约束，未调用',
  UNHELD_ACCOUNT_RULE_UNRESOLVED: '未持仓分支的账户约束留在本地、尚未核验；不采用该分支动作',
  HELD_ACCOUNT_RULE_UNRESOLVED: '已持仓分支的账户约束留在本地、尚未核验；不影响独立的未持仓条件判断',
  INPUT_TOO_LARGE: '材料总量超限，未静默截断', INVALID_DATE: '分析日期不可核实', UNSUPPORTED_SCOPE: '代码或研究周期不支持',
  NECESSARY_EVIDENCE_UNRESOLVED: '必要证据不充分或有冲突，不能采用确定动作', NO_ENTRY_AUTHORITY: '原裁决无明确正新增额度，禁止买入建议', NO_ADD_AUTHORITY: '原裁决无明确正新增额度，禁止加仓建议',
} as Record<string, string>)[value] || '检查未通过，不作有效动作'
