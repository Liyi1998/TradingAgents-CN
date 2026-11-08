import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/utils/intradayResearch.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } })
const { intradayLevels, intradayStateLabel, conditionCounts } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
const value = { enabled: true, snapshot: { status: 'ready' }, zones: [{ id: 'a', label: '分钟支撑', kind: 'support', lower: 10, upper: 10.1, observed_at: '2026-11-05 11:00:00', valid_until: '2026-11-05', source: 'tencent', condition: '待确认' }] }
test('minute zones are dated candidates and never extend overnight', () => {
  const before = structuredClone(value)
  const [level] = intradayLevels(value)
  assert.equal(level.state, 'intraday_candidate')
  assert.equal(level.effective_from, '2026-11-05')
  assert.equal(level.valid_until, '2026-11-05')
  assert.match(level.label, /11:00/)
  assert.deepEqual(value, before)
})
test('missing, disabled or stale evidence never overlays price bands', () => {
  assert.deepEqual(intradayLevels(null), [])
  assert.deepEqual(intradayLevels({ ...value, enabled: false }), [])
  assert.deepEqual(intradayLevels({ ...value, snapshot: { status: 'insufficient' } }), [])
  assert.equal(intradayStateLabel('met'), '规则满足')
})
test('coverage separates missing, unsupported and waiting without counting price observations', () => {
  const rows = ['met', 'not_met', 'invalidated', 'missing_data', 'unsupported', 'pending_close', 'pending_session'].map(state => ({ category: 'strategy', state }))
  rows.push({ category: 'level', state: 'missing_data' })
  assert.deepEqual(conditionCounts(rows), { total: 7, supported: 3, missing: 1, unsupported: 1, waiting: 2 })
  assert.equal(intradayStateLabel('missing_data'), '缺少数据')
  assert.equal(intradayStateLabel('unsupported'), '暂不支持完整核验')
  assert.equal(intradayStateLabel('pending_close'), '等待收盘')
})
