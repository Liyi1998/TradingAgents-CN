import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/priceReference.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } })
const { priceRange, pricePlans, observedPrice } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)

test('explicit CNY reference ranges are visible independently of the conditions', () => {
  const input = [{ name: '承接确认型研究', reference_range_cny: [49.24, 49.95], conditions: '承接确认及回款改善', status: '未授权当前买入' },
    { reference_range_cny: [52.55, 53.08], conditions: '完成周收复' }]
  const before = structuredClone(input)
  const plans = pricePlans(input)
  assert.equal(plans[0].range, '49.24–49.95')
  assert.equal(plans[0].unit, '人民币元/股')
  assert.equal(plans[0].status, '未授权当前买入')
  assert.equal(plans[1].range, '52.55–53.08')
  assert.deepEqual(input, before)
})

test('legacy narrative is preserved; numbers are not guessed from conditions', () => {
  assert.equal(pricePlans([{ conditions: '未来1–3个月观察，当前新增0%' }])[0].range, null)
  assert.equal(pricePlans([{ entry_reference: '42.4—43.4承接后，另行确认' }])[0].range, '42.4—43.4承接后，另行确认')
  for (const x of [null, NaN, Infinity, 0, [53, 49], [49, null], {}]) assert.equal(priceRange(x), null)
})

test('observed quote requires numeric price/date/source; absent snapshots are not zero', () => {
  assert.equal(observedPrice(undefined), null)
  assert.equal(observedPrice({ price: 50.89, currency: 'CNY' }), null)
  const value = observedPrice({ price: 50.89, currency: 'CNY', trade_date: '2026-10-13', data_source: 'akshare', price_basis: 'raw' })
  assert.equal(value.price, '50.89')
  assert.equal(value.date, '2026-10-13')
  assert.equal(value.basis, '不复权')
})
