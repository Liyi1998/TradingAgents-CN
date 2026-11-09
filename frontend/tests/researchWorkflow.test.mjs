import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/researchWorkflow.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 }
})
const { compatibleResearchWorkflows, periodAnalystSelection } =
  await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
const workflows = [
  { slug: 'default-4stage', enabled: true, valid: true },
  { slug: 'research-medium', analysis_horizon: 'medium', enabled: true, valid: true },
  { slug: 'research-long', analysis_horizon: 'long', enabled: true, valid: true },
  { slug: 'disabled-long', analysis_horizon: 'long', enabled: false, valid: true },
  { slug: 'invalid-long', analysis_horizon: 'long', enabled: true, valid: false },
]

test('period switch lists only compatible enabled workflows, legacy defaults remain short', () => {
  assert.deepEqual(compatibleResearchWorkflows(workflows).map(w => w.slug), ['default-4stage'])
  for (const period of ['medium', 'long']) {
    assert.deepEqual(compatibleResearchWorkflows(workflows, period).map(w => w.slug), [`research-${period}`])
  }
})

test('new research includes core roles and does not pass an unconfigured short/custom role', () => {
  const spec = { research_profile: { required_analysts: ['fundamentals', 'news'], roles: { fundamentals: {}, news: {}, market: {} } } }
  assert.deepEqual(periodAnalystSelection(spec, ['market', 'custom-short'], new Set(['market', 'custom-short'])),
    ['fundamentals', 'news', 'market'])
})

test('returning to short keeps the saved short selection, without adding fundamental roles', () => {
  const selected = ['market', 'capital']
  const before = structuredClone(selected)
  assert.deepEqual(periodAnalystSelection({}, selected, new Set(selected)), before)
  assert.deepEqual(selected, before)
})
