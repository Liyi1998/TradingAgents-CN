import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'
const source = await readFile(new URL('../src/utils/jevStrategyCandidates.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } })
const { extractStrategyCandidates: extract, initialStrategyCandidates: initial, sourceLocation: locate } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
const report = '# 最终裁决\n当前 WATCH，不是买入授权。\n\n## 入场条件\n必须放量且盈利改善，价格到达区间才考虑。\n\n### 例外\n仅有价格达标不能买入。\n\n## 退出条件\n跌破支撑且收盘确认时复核退出。\n\n## 否决条件\n盈利证据缺失不得新增；上限 0%。'
test('complete parent sections retain child prerequisites and source line numbers', () => {
  const result = extract(report)
  assert.equal(result.entry.length, 1)
  assert.equal(result.entry[0].startLine, 4)
  assert.equal(result.entry[0].endLine, 8)
  assert.match(result.entry[0].text, /仅有价格达标不能买入/)
  assert.equal(result.exit.length, 1)
  for (const role of ['entry', 'exit', 'veto']) for (const c of result[role]) {
    assert.ok(report.includes(c.text))
    assert.deepEqual(locate(report, c.text), { startLine: c.startLine, endLine: c.endLine })
  }
})
test('only unique explicit sections are prefilled; nothing confirms consent or validity', () => {
  const values = initial(extract('## 入场条件\n原文A\n## 退出条件\n原文B\n## 否决条件\n原文C'))
  assert.equal(values.entry, '## 入场条件\n原文A')
  assert.deepEqual(Object.keys(values).sort(), ['entry', 'exit', 'veto'])
})
test('multiple alternative entry setups are never silently selected or combined', () => {
  const r = extract('## A 买入条件\n放量突破。\n## B 买入条件\n缩量回踩。')
  assert.equal(r.entry.length, 2)
  assert.equal(initial(r).entry, '')
})
test('missing conditions remain missing, not invented from prices or WATCH', () => {
  const r = extract('## 技术参考\n价格 49.24–49.95 CNY。\n\nWATCH。')
  assert.deepEqual(initial(r), { entry: '', exit: '', veto: '' })
  assert.equal(r.entry.length + r.exit.length + r.veto.length, 0)
})
test('unlabelled prose is whole-paragraph only and requires explicit selection', () => {
  const s = '价格到达区间且盈利改善才可入场。\n例外：缺证据时不批准新增。'
  const r = extract(s)
  assert.equal(r.entry[0].text, s)
  assert.equal(initial(r).entry, '')
})
test('explicit inline labels retain following restrictions until next label', () => {
  const s = '入场条件：价格到达区间。\n还必须验证盈利改善。\n\n任何前提不满足就等待。\n退出条件：跌破支撑。\n否决条件：禁止追高。'
  const r = extract(s)
  assert.equal(r.entry[0].text, s.split('\n退出条件')[0])
  assert.equal(r.exit[0].text, '退出条件：跌破支撑。')
})
test('Markdown tables and subsequent notes remain intact', () => {
  const s = '## 买入条件\n| 区间 | 前提 |\n| --- | --- |\n| 10–11 | 放量 |\n\n说明：盈利未验证不得新增。\n## 结论\nWATCH'
  assert.equal(extract(s).entry[0].text, s.split('\n## 结论')[0])
})
test('CRLF remains verbatim and aligns with server line boundaries', () => {
  const s = '## 入场条件\r\n放量且盈利改善。\r\n\r\n## 退出条件\r\n收盘失守。\r\n'
  const c = extract(s).entry[0]
  assert.equal(c.text, '## 入场条件\r\n放量且盈利改善。')
  assert.ok(s.includes(c.text))
  assert.deepEqual(locate(s, c.text), { startLine: 1, endLine: 2 })
})
test('oversized sections remain complete but cannot be selected or prefilled', () => {
  const s = '## 入场条件\n'+ '完整前提。'.repeat(400)
  const r = extract(s)
  assert.equal(r.entry[0].text, s)
  assert.ok(r.entry[0].blocked)
  assert.equal(initial(r).entry, '')
})
test('code fences do not become executable-looking condition candidates', () => {
  assert.deepEqual(extract('```\n## 入场条件\n直接买入\n```'), { entry: [], exit: [], veto: [] })
  const r = extract('## 入场条件\n```\n买入\n```')
  assert.ok(r.entry[0].blocked)
  assert.equal(initial(r).entry, '')
})
test('prohibited entry remains a veto, not an entry setup', () => {
  const r = extract('## 禁止入场\n当前缺乏证据。')
  assert.equal(r.entry.length, 0)
  assert.equal(r.veto.length, 1)
  assert.equal(extract('当前 WATCH，不是买入授权。').entry.length, 0)
})
test('source matching rejects invented text and partial clauses', () => {
  assert.equal(locate('价格达标且盈利改善才可入场。', '价格达标'), null)
  assert.equal(locate('原文', '补写'), null)
  assert.equal(locate('原文', ''), null)
})
test('oversized source is not silently partially scanned', () => {
  assert.deepEqual(extract('## 入场条件\n'+'x'.repeat(200000)), { entry: [], exit: [], veto: [] })
})
test('new source does not inherit candidates from a previous report', () => {
  assert.ok(initial(extract('## 入场条件\n完整前提。')).entry)
  assert.equal(initial(extract('没有交易计划。')).entry, '')
})
test('empty headings and explicit missing placeholders never become prefilled conditions', () => {
  const r = extract('## 入场条件\n\n## 退出条件\n数据不足\n## 否决条件\n未批准新增。')
  assert.equal(r.entry.length, 0)
  assert.ok(r.exit[0].blocked)
  assert.equal(initial(r).exit, '')
  assert.match(initial(r).veto, /未批准新增/)
})
test('numbered report sections without Markdown retain complete source', () => {
  const r = extract('一、入场条件\n所有前提满足才考虑买入。\n二、退出条件\n逻辑失效退出。\n三、否决条件\n禁止追高。')
  assert.equal(initial(r).entry, '一、入场条件\n所有前提满足才考虑买入。')
  assert.equal(initial(r).exit, '二、退出条件\n逻辑失效退出。')
})
