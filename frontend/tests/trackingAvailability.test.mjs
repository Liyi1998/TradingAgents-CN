import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/utils/trackingAvailability.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } })
const { trackingAvailability, trackingPointSource, trackingManualAllowance } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
test('local daily cap and preclose waiting never offer a futile resume', () => {
  const board = { track: { baseline_date: '2026-11-03' }, run: { status: 'partial' }, budget: { remaining: 0 } }
  assert.equal(trackingAvailability(board, '2026-11-03').canRetry, false)
  assert.equal(trackingAvailability(board, '2026-11-03').blocked, true)
  board.budget.remaining = 6
  assert.equal(trackingAvailability(board, '2026-11-02').canRetry, false)
  assert.equal(trackingAvailability(board, '2026-11-02').waiting, true)
  assert.equal(trackingAvailability(board, '2026-11-03').canRetry, true)
  board.run = { status: 'failed', message: '最近已结束交易日早于原报告，等待收盘数据后再更新' }
  assert.equal(trackingAvailability(board).waiting, true)
  assert.equal(trackingAvailability(board, '2026-11-03').waiting, false)
})
test('last review fallback is explicit and a missing baseline cannot hide current levels', () => {
  const board = { version: { levels: [], review: null }, previous_review: { levels: [{}] }, baseline: { levels: [] } }
  assert.equal(trackingPointSource(board, 'review'), 'previous')
  board.version = { levels: [{}], review: {} }
  assert.equal(trackingPointSource(board, 'baseline'), 'review')
  board.version = { levels: [], review: { strategy: null } }
  assert.equal(trackingPointSource(board, 'review'), 'baseline')
})

test('manual continuation at zero budget offers a bounded one-run confirmation', () => {
  const board = { track: { baseline_date: '2026-11-03' }, run: { status: 'partial' },
    budget: { remaining: 0 }, manual_options: { update_max_calls: 10, continue_max_calls: 2, max_extra_calls: 10 } }
  assert.equal(trackingAvailability(board, '2026-11-03').canRetry, true)
  assert.equal(trackingAvailability(board, '2026-11-03').blocked, false)
  assert.deepEqual(trackingManualAllowance(board, true), { maximum: 2, remaining: 0, extra: 2 })
  assert.deepEqual(trackingManualAllowance(board, false), { maximum: 10, remaining: 0, extra: 10 })
  board.budget.remaining = 1
  assert.equal(trackingManualAllowance(board, true).extra, 1)
  board.budget.remaining = 6
  assert.equal(trackingManualAllowance(board, true).extra, 0)
  assert.equal(trackingAvailability(board, '2026-11-02').blocked, true)
})

test('retry delay disables execution and expires without extra model calls', () => {
  const now = Date.parse('2026-11-03T10:00:00+08:00')
  const board = { track: { baseline_date: '2026-11-03' }, budget: { remaining: 6 },
    run: { status: 'partial', retry_after_at: new Date(now + 60000).toISOString() } }
  assert.equal(trackingAvailability(board, '2026-11-03', now).cooldown, 60000)
  assert.equal(trackingAvailability(board, '2026-11-03', now).blocked, true)
  assert.equal(trackingAvailability(board, '2026-11-03', now + 60000).blocked, false)
})

test('unlimited usage never requests extra allowance but still respects cooldown and dates', () => {
  const board = { track: { baseline_date: '2026-11-05' }, run: { status: 'partial' },
    budget: { remaining: null, unlimited: true, used: 42 },
    manual_options: { update_max_calls: 10, continue_max_calls: 2, max_extra_calls: 10 } }
  assert.equal(trackingAvailability(board, '2026-11-05').blocked, false)
  assert.equal(trackingAvailability(board, '2026-11-05').canRetry, true)
  assert.equal(trackingManualAllowance(board, true).extra, 0)
  assert.equal(trackingAvailability(board, '2026-11-04').blocked, true)
})
