import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/conditionalStrategy.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 }
})
const { conditionalStrategyText, conditionalStrategyTexts } =
  await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)

test('legacy singular conditions remain visible and do not become current orders', () => {
  assert.equal(conditionalStrategyText({ condition: '站稳后另获授权', limit_pct: 5 }),
    '条件：站稳后另获授权；后续仓位上限：5%')
})

test('structured price plans render plural conditions, references and technical targets', () => {
  const plan = { status: '未触发、非当前订单', entry_reference: 21.78,
    invalidation_reference: 20.96, technical_targets: [22.43, 22.76, 22.901, 23.231],
    conditions: '日线收复并回测守住21.78元；须更新总暴露及原风险预算，不自动BUY。' }
  const before = structuredClone(plan)
  const result = conditionalStrategyText(plan)
  for (const phrase of ['未触发、非当前订单', '不自动BUY', '入场参考：21.78',
    '失效/保护参考：20.96', '22.43、22.76、22.901、23.231']) assert.ok(result.includes(phrase))
  assert.deepEqual(plan, before)
  assert.doesNotMatch(result, /现在可建仓|当前额度/)
})

test('older structured variants retain ranges, array conditions and unknown caps', () => {
  const result = conditionalStrategyText({ name: '条件方案', entry_reference_range: [49.95, 50.12],
    protection_reference: 48.85, first_target: 52, second_target_snapshot: 53,
    conditions: ['站稳', '另获授权'], invalidation: ['破位', '证据失效'], future_total_position_limit_pct: null })
  for (const phrase of ['49.95、50.12', '48.85', '52；53', '站稳、另获授权', '破位、证据失效', '未明确']) {
    assert.ok(result.includes(phrase))
  }
  assert.doesNotMatch(result, /0%/)
  assert.match(conditionalStrategyText({ trigger: '放量确认', requirement: '更新预算' }), /放量确认.*更新预算/)
})

test('missing/malformed values are explicit unknowns rather than blank or invented zero', () => {
  for (const value of [null, undefined, {}, [], { conditions: {}, entry_reference: NaN }]) {
    assert.equal(conditionalStrategyText(value), '条件未明确（非当前授权）')
  }
  assert.deepEqual(conditionalStrategyTexts(null), [])
  assert.match(conditionalStrategyText({ limit_pct: 0 }), /0%/)
  for (const limit_pct of [NaN, Infinity, -1, 101, {}, 'unknown']) {
    assert.equal(conditionalStrategyText({ limit_pct }), '后续仓位上限：未明确')
  }
})

test('deduplicates equivalent text without merging the two account branches', () => {
  assert.deepEqual(conditionalStrategyTexts([{ condition: '等待' }, { conditions: '等待' }]), ['条件：等待'])
})

test('the authorized private latest submission is presented without a blank item', async t => {
  if (!process.env.CONDITIONAL_REPORT_SNAPSHOT) return t.skip('Private report replay input not supplied')
  const data = JSON.parse(await readFile(process.env.CONDITIONAL_REPORT_SNAPSHOT, 'utf8'))
  const report = data.reports[0]
  const submission = data.events[report.task_id][0].payload.input
  const plans = submission.decision_semantics.conditional_strategy
  assert.equal(plans.length, 1)
  const results = conditionalStrategyTexts(plans)
  assert.equal(results.length, 1)
  assert.match(results[0], /21\.78.*20\.96|20\.96.*21\.78/)
  assert.match(results[0], /不自动BUY/)
})
