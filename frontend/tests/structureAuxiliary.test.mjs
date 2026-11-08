/* eslint-env node */
import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/structureAuxiliary.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } })
const module = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
const { structureLevels, structureGateLabel } = module

const day = { date: '2026-11-05', status: 'ready', val: 10, vah: 10.4, reason: '测试用合成轮廓，不是行情' }
const value = {
  enabled: true, trade_authorized: false, vote: false, performance_claim: false,
  tpo: { label: '30分钟OHLC区间覆盖的bar-based TPO近似', days: [day] },
  smc: { state: 'up', order_blocks: { status: 'deferred', reason: '后置' }, latest: {
    id: 'e', kind: 'bos', status: 'confirmed', lower: 10.5, upper: 10.5,
    first_knowable_time: '2026-11-05 11:00:00', invalidation: '收回则失效',
  }, events: [] },
}

test('layers stay off until requested and never become orders', () => {
  assert.deepEqual(structureLevels(value, false), [])
  assert.deepEqual(structureLevels({ ...value, enabled: false }, true), [])
  assert.deepEqual(structureLevels({ ...value, trade_authorized: true }, true), [])
  const levels = structureLevels(value, true)
  assert.equal(levels.length, 2)
  assert.equal(levels[0].kind, 'support')
  assert.equal(levels[0].state, 'intraday_candidate')
  assert.match(levels[0].label, /TPO接受区近似/)
  assert.equal(levels[0].valid_until, '2026-11-05')
  assert.ok(!levels.some(level => ['entry', 'target'].includes(level.kind)))
})

test('stale and unverified gates are not labelled ready', () => {
  assert.equal(structureGateLabel('ready'), '可核验')
  assert.equal(structureGateLabel('stale'), '陈旧')
  assert.equal(structureGateLabel('cannot_judge'), '不可判断')
})

test('incomplete profiles produce no acceptance band', () => {
  const hidden = structureLevels({ ...value, tpo: { label: value.tpo.label, days: [{ ...day, status: 'cannot_judge', val: null, vah: null }] }, smc: { ...value.smc, latest: null } }, true)
  assert.deepEqual(hidden, [])
})
