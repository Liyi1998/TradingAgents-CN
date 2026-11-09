/** Local source navigation, NOT semantic approval or strategy generation. */
export type ConditionRole = 'entry' | 'exit' | 'veto'
export interface StrategyCandidate {
  id: string
  text: string
  startLine: number
  endLine: number
  kind: 'section' | 'paragraph'
  blocked: string
}
export const conditionRoles: ConditionRole[] = ['entry', 'exit', 'veto']
export const conditionLabels = { entry: '入场必要条件', exit: '退出与失效条件', veto: '否决、限制及授权条件' }
export const conditionLimits = { entry: 1500, exit: 1000, veto: 1000 }
const signals = {
  entry: /入场|买入|建仓|加仓|新增(?:条件|前提|计划)|entry/i,
  exit: /退出|止损|止盈|减仓|卖出|清仓|失效|exit|stop.loss/i,
  veto: /否决|禁止|不得|不批准|未批准|未授权|不授权|不允许|暂不|不能买|限制|授权|上限|前提|例外|veto/i,
}
const label = /^(?:入场|买入|建仓|新增|退出|止损|止盈|卖出|减仓|失效|否决|限制|授权|风控|风险否决)[^：:。；;]{0,24}[：:]/

export function extractStrategyCandidates(source: string): Record<ConditionRole, StrategyCandidate[]> {
  const result: Record<ConditionRole, StrategyCandidate[]> = { entry: [], exit: [], veto: [] }
  // No silent clipping: the caller can still inspect the original report.
  if (!source || source.length > 200000) return result
  const lines = source.split('\n')
  const boundaries: { index: number; level: number; title: string }[] = []
  const fenced = new Set<number>()
  let fence = ''
  for (let i = 0; i < lines.length; i++) {
    const text = lines[i].trim()
    const marker = text.match(/^(`{3,}|~{3,})/)
    if (marker && !fence) { fence = marker[1][0]; fenced.add(i); continue }
    if (fence) { fenced.add(i); if (marker?.[1][0] === fence) fence = ''; continue }
    const md = text.match(/^(#{1,6})\s+(.+?)\s*#*$/)
    const clean = text.replace(/\*\*/g, '').replace(/^(?:[-*+]\s+|(?:\d+[.)、]|[一二三四五六七八九十]+[、.．])\s*)/, '')
    const standalone = text.startsWith('**') && text.endsWith('**') && !/[。；;]/.test(clean)
    const numbered = /^(?:\d+[.)、]|[一二三四五六七八九十]+[、.．])\s*/.test(text) && !/[。；;：:]/.test(clean)
    if (md || label.test(clean) || ((standalone || numbered) && clean.length <= 80)) {
      boundaries.push({ index: i, level: md ? md[1].length : 7, title: md ? md[2] : clean.split(/[：:]/)[0] })
    }
  }
  const boundaryIndexes = new Set(boundaries.map(b => b.index))
  const ends = new Map<number, number>()
  const stack: typeof boundaries = []
  for (const b of boundaries) {
    while (stack.length && stack[stack.length - 1].level >= b.level) ends.set(stack.pop()!.index, b.index)
    stack.push(b)
  }
  const covered = new Set<number>()
  function add(start: number, end: number, kind: StrategyCandidate['kind'], roles: ConditionRole[]) {
    while (end > start && !lines[end - 1].trim()) end--
    if (end <= start) return
    const text = lines.slice(start, end).join('\n').replace(/\r$/, '')
    const body = lines.slice(start + 1, end).join('\n').trim()
      || (lines[start].split(/[：:]/).slice(1).join('：').trim())
    if (kind === 'section' && !body) return // A heading alone is not a condition.
    for (const role of roles) {
      const blocked = Array.from(text).length > conditionLimits[role] || new TextEncoder().encode(text).length > 5000
        ? '完整段落超出材料上限，未截断；请人工选择更小且完整的策略范围'
        : [...fenced].some(i => i >= start && i < end) ? '包含代码块，请人工核对并选取公共策略原文'
        : kind === 'section' && /^(?:N\/?A|暂无|未知|未提供|数据不足|待补充|无)[。.!！\s]*$/i.test(body)
          ? '本节仅有缺失说明，未提供可用条件；不能自动补写' : ''
      result[role].push({ id: `${role}:${start + 1}:${end}`, text, startLine: start + 1, endLine: end, kind, blocked })
    }
  }
  for (let i = 0; i < boundaries.length; i++) {
    const b = boundaries[i]
    const roles = conditionRoles.filter(role => signals[role].test(b.title))
    if (!roles.length) continue
    // A prohibition is not an affirmative entry setup. It still remains in the source.
    if (/^(?:禁止|不得|暂不|未批准|未授权)/.test(b.title)) roles.splice(0, roles.length, 'veto')
    const end = ends.get(b.index) ?? lines.length
    add(b.index, end, 'section', roles)
    for (let j = b.index; j < end; j++) covered.add(j)
  }
  // Unlabelled prose is offered as a WHOLE paragraph, never a clause or price.
  // Such candidates are not auto-selected, even if only one matches.
  for (let i = 0; i < lines.length;) {
    if (!lines[i].trim() || covered.has(i) || fenced.has(i) || boundaryIndexes.has(i)) { i++; continue }
    const start = i
    while (i < lines.length && lines[i].trim() && !covered.has(i) && !fenced.has(i) && !boundaryIndexes.has(i)) i++
    const text = lines.slice(start, i).join('\n')
    add(start, i, 'paragraph', conditionRoles.filter(role => signals[role].test(text)
      && (role !== 'entry' || /若|如果|当.{1,80}时|须|必须|才|且|条件|确认|突破|回踩|收盘/.test(text))))
  }
  for (const role of conditionRoles) result[role].sort((a, b) => a.startLine - b.startLine)
  return result
}

export function initialStrategyCandidates(candidates: ReturnType<typeof extractStrategyCandidates>) {
  return Object.fromEntries(conditionRoles.map(role => {
    const rows = candidates[role]
    return [role, rows.length === 1 && rows[0].kind === 'section' && !rows[0].blocked ? rows[0].text : '']
  })) as Record<ConditionRole, string>
}

export function sourceLocation(source: string, selected: string): { startLine: number; endLine: number } | null {
  const text = selected.replace(/^[\r\n]+|[\r\n]+$/g, '')
  if (!text) return null
  let start = source.indexOf(text)
  while (start >= 0) {
    if ((start === 0 || source[start - 1] === '\n') && (start + text.length === source.length || /[\r\n]/.test(source[start + text.length]))) {
      const startLine = source.slice(0, start).split('\n').length
      return { startLine, endLine: startLine + text.split('\n').length - 1 }
    }
    start = source.indexOf(text, start + 1)
  }
  return null
}
