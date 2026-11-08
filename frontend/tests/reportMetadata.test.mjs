import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/utils/reportMetadata.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } })
const { analysisCompletedAt, reportConfidence } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
test('completed_at and archived report time survive missing legacy end_time', () => {
  assert.equal(analysisCompletedAt({ completed_at: '2026-10-27T07:29:17.207000' }, {}), '2026-10-27T07:29:17.207000Z')
  assert.equal(analysisCompletedAt({ end_time: '2026-10-27T15:29:17+08:00' }, {}), '2026-10-27T15:29:17+08:00')
  assert.equal(analysisCompletedAt({ completed_at: 'bad' }, { created_at: '2026-10-27 07:29:17' }), '2026-10-27T07:29:17Z')
  assert.equal(analysisCompletedAt({}, {}), undefined)
})
test('qualitative confidence remains qualitative; null is never zero or an unrelated overall score', () => {
  assert.equal(reportConfidence({confidence_score:null,overall_score:80,structured_summary:{confidence_label:'中等偏低'}}), '中等偏低（未量化）')
  assert.equal(reportConfidence({confidence_score:0}), '0%')
  assert.equal(reportConfidence({confidence_score:0.7}), '70%')
  assert.equal(reportConfidence({confidence_score:null}), '报告未提供')
  assert.equal(reportConfidence({confidence_score:Infinity}), '报告未提供')
})
