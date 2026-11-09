import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
const source = readFileSync(new URL('../src/utils/experience.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } })
const { reflectionLabel } = await import('data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'))
const base = { id: '1', task_id: 't', symbol: '002241', decision_date: '2026-11-01', horizon_days: 7, signal: 'Buy', status: 'pending' }
test('unmatured analysis is not a failed reflection', () => assert.equal(reflectionLabel(base), '等待评估窗口结束'))
test('saved lesson is distinct from searchable memory', () => assert.equal(reflectionLabel({ ...base, status: 'settled', reflection_status: 'failed', reflection_lesson: '正文' }), '正文已保存 · 检索入库未完成'))
test('done-without-body is not a usable lesson', () => assert.equal(reflectionLabel({ ...base, status: 'settled', reflection_status: 'done' }), '已结算 · 等待复盘'))
