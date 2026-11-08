import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/utils/stockQuote.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } })
const { stockQuoteView } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
test('a new incomplete observation clears previous fields without inventing zero', () => {
  const state = stockQuoteView({ price: 7.58, volume: 100, amount: 99, trade_date: '2026-10-23' })
  Object.assign(state, stockQuoteView({ price: 7.1, volume: 0, change_percent: 0, trade_date: '2026-10-27', data_source: 'tencent' }))
  assert.equal(state.price, 7.1)
  assert.equal(state.volume, 0)
  assert.equal(state.changePercent, 0)
  assert.equal(state.tradeDate, '2026-10-27')
  assert.ok(Number.isNaN(state.amount))
  Object.assign(state, stockQuoteView({ price: null, quote_kind: 'unavailable' }))
  assert.ok(Number.isNaN(state.price))
  assert.equal(state.tradeDate, null)
})
