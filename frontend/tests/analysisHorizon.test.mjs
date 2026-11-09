import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/analysisHorizon.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 }
})
const { ANALYSIS_HORIZONS, analysisHorizonLabel, isAnalysisHorizon, resolveAnalysisHorizon } =
  await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)

test('offers exactly the three supported modes with their research periods', () => {
  assert.deepEqual(ANALYSIS_HORIZONS.map(option => option.value), ['short', 'medium', 'long'])
  assert.equal(analysisHorizonLabel('short'), '短线（近期交易）')
  assert.equal(analysisHorizonLabel('medium'), '中线（1–3 个月）')
  assert.equal(analysisHorizonLabel('long'), '长线（6–12 个月）')
  for (const value of ['short', 'medium', 'long']) assert.equal(isAnalysisHorizon(value), true)
  for (const value of [null, undefined, '', 'LONG', 'year', {}, ['long']]) {
    assert.equal(isAnalysisHorizon(value), false)
  }
})

test('task/report saved horizon takes precedence over a nested parameter fallback', () => {
  assert.equal(resolveAnalysisHorizon({ analysis_horizon: 'long', parameters: { analysis_horizon: 'short' } }), 'long')
  assert.equal(resolveAnalysisHorizon({ parameters: { analysis_horizon: 'medium' } }), 'medium')
  assert.equal(resolveAnalysisHorizon({ analysis_horizon: null, parameters: { analysis_horizon: 'long' } }), 'long')
})

test('legacy records remain short-term even when no parameters were persisted', () => {
  for (const report of [null, undefined, {}, { parameters: null }, { parameters: {} }]) {
    assert.equal(resolveAnalysisHorizon(report), 'short')
    assert.equal(analysisHorizonLabel(report, true), '短线')
  }
})

test('same-stock reports retain distinct saved horizons without modifying API records', () => {
  const reports = [
    { symbol: '002241', analysis_horizon: 'short' },
    { symbol: '002241', analysis_horizon: 'medium' },
    { symbol: '002241', parameters: { analysis_horizon: 'long' } },
  ]
  const before = structuredClone(reports)
  assert.deepEqual(reports.map(report => analysisHorizonLabel(report, true)), ['短线', '中线', '长线'])
  assert.deepEqual(reports, before)
})
