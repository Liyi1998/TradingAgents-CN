import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/jevConditionCheck.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } })
const { conditionCheckView, conditionReason } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
const record = () => ({ mode: 'shadow', affects_decision: false, status: 'completed', external_call_attempted: true,
  model: 'jev-1.13.0', requested_model: 'jev-1.13.0', created_at: '2026-10-19T02:00:00', elapsed_seconds: 4.936,
  original_reference: { unheld_action: 'WATCH', held_action: 'HOLD' },
  result: { condition_status: 'met', evidence_conflict: 'not_evident', reason_codes: ['SUPPORTED'] } })

test('completed shadow result remains separate from unchanged original action', () => {
  const input = record(), before = structuredClone(input), value = conditionCheckView(input)
  assert.equal(value.completed, true)
  assert.equal(value.condition, '所给证据支持条件满足')
  assert.match(value.conflict, /不代表事实已核实/)
  assert.equal(value.original, '未持仓：观望；已持仓：持有')
  assert.equal(value.elapsed, '4.94 秒')
  assert.match(value.time, /10:00:00.*北京时间/)
  assert.deepEqual(input, before)
})

for (const [status, conflict, reason] of [
  ['not_met', 'unknown', 'EXPLICITLY_UNMET'],
  ['insufficient_evidence', 'unknown', 'MISSING_EVIDENCE'],
  ['insufficient_evidence', 'present', 'CONFLICTING_EVIDENCE'],
]) test(`valid ${status}/${conflict} is not converted into a buy/sell decision`, () => {
  const value = conditionCheckView({ ...record(), result: { condition_status: status, evidence_conflict: conflict, reason_codes: [reason] } })
  assert.equal(value.completed, true)
  assert.doesNotMatch(value.condition, /可以买入|建议卖出/)
})

for (const status of ['running', 'skipped', 'not_called', 'unavailable', 'incomplete']) test(`${status} never presents a model conclusion`, () => {
  const value = conditionCheckView({ ...record(), status, external_call_attempted: status === 'unavailable', reason: 'external_calls_disabled' })
  assert.equal(value.valid, true)
  assert.equal(value.completed, false)
  assert.equal(value.condition, '')
  assert.equal(value.conflict, '')
  assert.equal(value.modelLabel, '配置的核验模型')
})

for (const change of [
  { external_call_attempted: false }, { mode: 'enforce' }, { affects_decision: true },
  { status: 'surprise' }, { result: {} }, { result: { condition_status: 'met', evidence_conflict: 'present', reason_codes: ['SUPPORTED'] } },
]) test(`incompatible record ${JSON.stringify(change)} is not shown as passed`, () => {
  const value = conditionCheckView({ ...record(), ...change })
  assert.equal(value.valid, false)
  assert.equal(value.completed, false)
  assert.equal(value.condition, '')
})

test('skipped redacted and truncated material is explicitly local, not a Jev failure', () => {
  const value = conditionCheckView({ ...record(), status: 'skipped', external_call_attempted: false,
    reason: 'insufficient_input', input_guards: ['REDACTED_INPUT', 'TRUNCATED_INPUT'] })
  assert.equal(value.title, '已跳过 · 未调用 Jev')
  assert.equal(value.attempted, '未尝试调用')
  assert.equal(value.reasons.length, 3)
  assert.match(value.reasons.join(' '), /脱敏.*跳过外发/)
})

test('unknown error details, raw input, and private fields never leak through projection', () => {
  const value = conditionCheckView({ ...record(), status: 'unavailable', reason: 'Bearer secret',
    input: { private: 'secret' }, api_key: 'secret', requested_model: 'Bearer secret' })
  assert.doesNotMatch(JSON.stringify(value), /secret|Bearer/)
  assert.equal(conditionReason('condition_http_503'), 'Jev 服务返回 HTTP 503，没有可用核验结果')
  assert.match(conditionReason('daily_limit'), /当日/)
  assert.match(conditionReason('interrupted_or_result_not_saved_no_auto_retry'), /不会自动重发/)
})

test('missing or invalid timing is not displayed as zero or current time', () => {
  for (const elapsed_seconds of [NaN, Infinity, -1, null, '4']) {
    const value = conditionCheckView({ ...record(), elapsed_seconds, created_at: 'garbage' })
    assert.equal(value.elapsed, '未记录')
    assert.equal(value.time, '未记录')
  }
})
