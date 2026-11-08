import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/utils/liveMarketChart.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } })
const { marketBars, mergeMarketChart, nextMarketPollDelay } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
const completed = { date:'2026-10-26', open:7.55, high:7.7, low:7.39, close:7.58, volume:26133641, data_source:'akshare' }
const live = { ...completed, date:'2026-10-27', close:7.10, data_source:'tencent', status:'intraday', observed_at:'2026-10-27T13:51:33+08:00' }
const state = { bars:[completed], live_bar:live, live_status:'intraday', session:{date:'2026-10-27',phase:'trading',next_poll_at:'2026-10-27T13:52:00+08:00'}, quote:{price:7.1} }
test('updating today replaces a candle, and completed daily evidence wins without mutating history', () => {
  const next = { ...state, live_bar:{...live,close:7.09,observed_at:'2026-10-27T13:51:50+08:00'} }
  assert.equal(marketBars(mergeMarketChart(state,next)).length,2)
  assert.equal(marketBars(next)[1].close,7.09)
  const closed = {...next,bars:[completed,{...live,close:7.05,status:undefined}],live_bar:null,live_status:'completed'}
  assert.equal(marketBars(mergeMarketChart(next,closed)).length,2)
  assert.equal(marketBars(closed)[1].close,7.05)
  assert.equal(state.bars.length,1)
})
test('failed or out-of-order data retains the original timestamp and marks delay; rollover drops old live data', () => {
  for(const next of [{...state,live_bar:null,live_status:'unavailable'}, {...state,live_bar:{...live,observed_at:'2026-10-27T13:50:00+08:00'}}]) {
    const merged=mergeMarketChart(state,next)
    assert.equal(merged.live_bar.observed_at,live.observed_at)
    assert.equal(merged.live_bar.stale,true)
    assert.equal(merged.live_status,'delayed')
  }
  const tomorrow={...state,live_bar:null,live_status:'unavailable',session:{...state.session,date:'2026-10-28'}}
  assert.equal(marketBars(mergeMarketChart(state,tomorrow)).length,1)
})
test('polling uses server exchange-session boundaries and can recover a failed first request', () => {
  assert.equal(nextMarketPollDelay(state,Date.parse('2026-10-27T13:51:30+08:00')),30000)
  const lunch={...state,session:{...state.session,next_poll_at:'2026-10-27T13:00:00+08:00'}}
  assert.equal(nextMarketPollDelay(lunch,Date.parse('2026-10-27T12:00:00+08:00')),3600000)
  assert.equal(nextMarketPollDelay({...state,session:{next_poll_at:null}}),null)
  assert.equal(nextMarketPollDelay(null),30000)
})
