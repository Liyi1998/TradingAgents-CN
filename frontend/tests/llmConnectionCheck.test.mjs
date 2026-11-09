import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/api/config.ts', import.meta.url), 'utf8')
const file = ts.createSourceFile('config.ts', source, ts.ScriptTarget.Latest, true)
let method
function visit(node) {
  if (ts.isMethodDeclaration(node) && node.name.getText(file) === 'testConfig') method = node
  ts.forEachChild(node, visit)
}
visit(file)

test('only LLM connection checks extend the browser deadline and never auto-retry', () => {
  assert.ok(method)
  const statement = method.body.statements.find(ts.isReturnStatement)
  const args = statement.expression.arguments
  assert.equal(args[0].text, '/api/config/test')
  const options = args[2]
  assert.ok(ts.isConditionalExpression(options))
  assert.equal(options.condition.getText(file), "testRequest.config_type === 'llm'")
  const values = Object.fromEntries(options.whenTrue.properties.map(p => [p.name.getText(file), p.initializer.getText(file)]))
  assert.equal(Number(values.timeout), 210000)
  assert.equal(Number(values.retryCount), 0)
  assert.equal(values.retryNonIdempotent, 'false')
  assert.equal(options.whenFalse.getText(file), 'undefined')
})
