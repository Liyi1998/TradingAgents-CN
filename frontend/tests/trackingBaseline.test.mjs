import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/utils/trackingBaseline.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } })
const { reportTime, levelSourceDate } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)

test('original report date takes priority over the chart visibility and current daily dates', () => {
  assert.equal(levelSourceDate(true, '2026-10-26', '2026-10-27', '2026-10-28'), '来源完整报告日期：2026-10-26')
  assert.equal(levelSourceDate(false, '2026-10-26', '2026-10-26', '2026-10-27'), '来源每日研究日期：2026-10-27')
  assert.equal(levelSourceDate(true), '来源完整报告日期：未记录')
})
test('legacy UTC generation times match explicit timestamps and unknown stays unknown', () => {
  const expected = reportTime('2026-10-27T07:29:20+00:00')
  assert.equal(reportTime('2026-10-27T07:29:20'), expected)
  assert.match(expected, /15:29/)
  assert.equal(reportTime(null), '生成时间未记录')
  assert.equal(reportTime('invalid'), '生成时间未记录')
})
