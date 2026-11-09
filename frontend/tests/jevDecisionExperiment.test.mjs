import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'
const source = await readFile(new URL('../src/utils/jevDecisionExperiment.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } })
const { validDecisionResult, experimentTime } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
const row = () => ({ status: 'completed', external_call_attempted: true, affects_decision: false,
  result: { entry_action: 'WAIT', existing_action: 'RESEARCH_REQUIRED', risk_state: 'INSUFFICIENT' } })
test('valid shadow abstention displays as evidence-limited, not a buy', () => assert.equal(validDecisionResult(row()), true))
for (const change of [{ status: 'unavailable' }, { external_call_attempted: false }, { affects_decision: true },
  { result: {} }, { result: { entry_action: 'BUY', existing_action: 'HOLD_CANDIDATE', risk_state: 'WITHIN_CONDITIONS' } },
  { result: { entry_action: 'ENTRY_CANDIDATE', existing_action: 'HOLD_CANDIDATE', risk_state: 'WITHIN_CONDITIONS' } },
  { result: { entry_action: 'WAIT', existing_action: 'HOLD_CANDIDATE', risk_state: 'INSUFFICIENT' } }])
  test(`invalid state ${JSON.stringify(change)} never displays as completed decision`, () => assert.equal(validDecisionResult({ ...row(), ...change }), false))
test('naive Mongo UTC timestamps are displayed in Shanghai time', () => {
  assert.match(experimentTime('2026-10-19T05:30:00'), /13:30:00.*北京时间/)
  assert.equal(experimentTime('bad'), '时间未记录')
})
