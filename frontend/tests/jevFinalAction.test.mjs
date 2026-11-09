import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'
const source = await readFile(new URL('../src/utils/jevFinalAction.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } })
const { validFinalAction, finalActionLabel, finalReasonLabel } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
const fields = { unheld_action: 'WAIT', held_action: 'RESEARCH_REQUIRED', evidence_state: 'INSUFFICIENT' }
const row = { status: 'completed', affects_decision: false, external_call_attempted: true, suggested: fields, effective: fields }
test('only valid separate hypothetical branches render', () => {
  assert.ok(validFinalAction(row))
  assert.ok(!validFinalAction({ ...row, affects_decision: true }))
  assert.ok(!validFinalAction({ ...row, external_call_attempted: false }))
  assert.ok(!validFinalAction({ ...row, effective: { ...fields, unheld_action: 'SELL' } }))
  assert.ok(!validFinalAction({ ...row, effective: null }))
  assert.equal(finalActionLabel('RESEARCH_REQUIRED'), '证据不足，需研究')
})
test('partition failures and local branch guards are explained without private values', () => {
  assert.match(finalReasonLabel('PARTITION_INCOMPLETE'), /原报告保留/)
  assert.match(finalReasonLabel('HELD_ACCOUNT_RULE_UNRESOLVED'), /不影响独立/)
  assert.match(finalReasonLabel('ACCOUNT_RULES_UNRESOLVED'), /未调用/)
  assert.ok(validFinalAction({ ...row, effective: { ...fields, unheld_action: 'WAIT', evidence_state: 'SUPPORTED' } }))
})
