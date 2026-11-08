import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const phaseSource = await readFile(new URL('../src/utils/wyckoffTracking.ts', import.meta.url), 'utf8')
const phaseCode = ts.transpileModule(phaseSource, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const phaseUrl = `data:text/javascript;base64,${Buffer.from(phaseCode).toString('base64')}`
const source = (await readFile(new URL('../src/utils/researchTrackingChart.ts', import.meta.url), 'utf8')).replace("'./wyckoffTracking'", JSON.stringify(phaseUrl))
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } })
const { trackingChart, formatLevelPrice, chanSeries } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
const bars = [{ date: '2026-10-26', open: 10, close: 10.5, low: 9.8, high: 10.6, volume: 100, data_source: 'a' }, { date: '2026-10-27', open: 10.5, close: 10.2, low: 10.1, high: 10.8, volume: 120, data_source: 'a' }]
const level = { kind: 'entry', label: '条件入场', lower: 10, upper: 10.3, effective_from: '2026-10-28', valid_until: '2026-10-29', expired: false, price_basis: 'raw', currency: 'CNY' }
test('Chan layers preserve original charts and distinguish candidates and confirmation time', () => {
  const stroke = { id:'bi:1', start:'2026-10-26',end:'2026-10-27',start_price:10,end_price:11,status:'confirmed',confirmed_at:'2026-10-29',updated_at_session:'2026-10-29' }
  const chan = { status:'ready',as_of:'2026-10-29',data_source:'sample',fractals:[],strokes:[stroke,{...stroke,id:'bi:2',status:'candidate',confirmed_at:null}],zones:[{...stroke,id:'zs:1',lower:10,upper:11}] }
  const chart = trackingChart(bars,[level],[],1200,430,[],chan)
  assert.equal(chart.series[0].name,'不复权日K')
  assert.equal(chart.series[3].name,'威科夫阶段')
  assert.equal(chart.series.filter(s=>s.type==='line').length,3)
  assert.match(chart.series.at(-2).tooltip.formatter(),/确认于 2026-10-29/)
  assert.equal(chart.series.at(-1).lineStyle.type,'dashed')
  assert.equal(chanSeries(chan,bars.map(b=>b.date),{fractals:false,strokes:true,zones:false,candidates:false}).length,1)
  assert.equal(chanSeries(undefined,bars.map(b=>b.date)).length,0)
  assert.equal(chanSeries({...chan,status:'unavailable'},bars.map(b=>b.date)).length,0)
  assert.equal(chanSeries(chan,['2026-10-27']).length,0)
})
test('report price precision is preserved in card formatter and chart labels', () => {
  assert.equal(formatLevelPrice(7.285), '7.285')
  assert.equal(formatLevelPrice(6.7), '6.70')
  const chart = trackingChart(bars, [{ ...level, lower: 7.15, upper: 7.285 }], ['2026-10-28', '2026-10-29'])
  assert.match(chart.series[0].markLine.data[1][0].name, /7.15–7.285/)
  assert.deepEqual(chart.series[0].markLine.data[1][0].coord, ['2026-10-28', 7.285])
  assert.ok(chart.yAxis[0].min({ min: 9.8 }) < 7.15)
  assert.ok(chart.yAxis[0].max({ max: 10.8 }) > 10.8)
  assert.equal(chart.yAxis[0].axisLabel.formatter(10.899999999999999), '10.9')
  assert.equal(trackingChart(bars, [], []).yAxis[0].min, undefined)
})
test('levels start when known, never on earlier candles; future candles stay empty', () => {
  const chart = trackingChart(bars, [level], ['2026-10-28', '2026-10-29'])
  assert.deepEqual(chart.series[0].markLine.data[0][0].coord, ['2026-10-28', 10])
  assert.deepEqual(chart.series[0].data[2], ['-', '-', '-', '-'])
  assert.deepEqual(chart.series[0].data[0], [10, 10.5, 9.8, 10.6])
})
test('unknown prices, wrong adjustment and expired-before-known levels are not drawn', () => {
  for (const patch of [{ lower: NaN }, { price_basis: 'qfq' }, { currency: 'USD' }, { effective_from: '2026-11-01' }]) {
    assert.equal(trackingChart(bars, [{ ...level, ...patch }], ['2026-10-28', '2026-10-29']).series[0].markLine.data.length, 0)
  }
})
test('historical expired levels retain original interval and neutral color', () => {
  const chart = trackingChart(bars, [{ ...level, expired: true }], ['2026-10-28', '2026-10-29'])
  assert.equal(chart.series[0].markLine.data[0][0].lineStyle.color, '#999')
  assert.equal(chart.series[0].markLine.data[0][1].coord[0], '2026-10-29')
})
test('last successful review is a grey dated reference, never renewed', () => {
  const old = { ...level, state: 'previous_review' }
  const chart = trackingChart(bars, [old], ['2026-10-28', '2026-10-29', '2026-10-30'])
  const mark = chart.series[0].markLine.data[0]
  assert.match(mark[0].name, /上次未复核/)
  assert.equal(mark[0].lineStyle.color, '#999')
  assert.deepEqual(mark[1].coord, ['2026-10-29', 10])
  assert.equal(chart.series[0].markArea.data[0][0].itemStyle.color, '#999')
  assert.equal(old.valid_until, '2026-10-29')
})
test('live tooltip and candle distinguish unclosed delayed observations from completed evidence', () => {
  const live = { ...bars[1], status: 'intraday', stale: true, observed_at: '2026-10-27T13:51:33+08:00' }
  const chart = trackingChart([bars[0], live], [], [])
  assert.deepEqual(chart.series[0].data[1].value, [10.5, 10.2, 10.1, 10.8])
  const text = chart.tooltip.formatter([{ axisValue: live.date }])
  assert.match(text, /未收盘/)
  assert.match(text, /行情延迟/)
  assert.match(text, /最新 10.20/)
  assert.ok(text.includes(live.observed_at))
  assert.doesNotMatch(chart.tooltip.formatter([{ axisValue: bars[0].date }]), /未收盘|行情延迟/)
})
test('original report ranges are labeled references, not silently renewed reviewed strategies', () => {
  const original = { ...level, lower:7.7, upper:7.75, label:'第一反弹减仓位', kind:'exit', effective_from:'2026-10-27',
    valid_until:null, state:'report_reference', price_basis:'unspecified' }
  const chart = trackingChart(bars, [original], ['2026-10-28','2026-10-29'])
  const lines = chart.series[0].markLine.data
  assert.equal(lines.length,2)
  assert.deepEqual(lines[0][0].coord,['2026-10-27',7.7])
  assert.deepEqual(lines[1][1].coord,['2026-10-29',7.75])
  assert.match(lines[1][0].name,/原报告·第一反弹减仓位 7.70–7.75/)
  assert.equal(lines[0][0].label.show,false)
  assert.equal(lines[1][0].label.show,false)
  assert.equal(chart.series[2].data.length,1)
  assert.match(chart.series[2].data[0].labelText,/原报告·第一反弹减仓位/)
  assert.equal(original.valid_until,null)
  assert.equal(trackingChart(bars,[{...original,state:'conditional'}],[]).series[0].markLine.data.length,0)
  assert.equal(trackingChart(bars,[{...original,price_basis:'qfq'}],[]).series[0].markLine.data.length,0)
})

const phase = { phase: 'testing', candidate_phase: 'markup', status: 'transition', source_kind: 'daily', as_of: '2026-10-27', known_at: '2026-10-28T08:00:00+08:00', structure_start: '2026-10-26', version_id: 'one' }
test('Wyckoff observations use separate dated strip, with explicit recognition time and unchanged price marks', () => {
  const chart = trackingChart(bars, [level], ['2026-10-28'], 1200, 430, [phase])
  const marks = chart.series.find(s => s.name === '威科夫阶段')
  assert.equal(marks.xAxisIndex, 2)
  assert.deepEqual(marks.data[0].value, ['2026-10-27', .5])
  assert.match(marks.tooltip.formatter({ data: marks.data[0] }), /试盘（旧口径） → 拉升/)
  assert.match(marks.tooltip.formatter({ data: marks.data[0] }), /2026-10-28T08:00/)
  assert.deepEqual(chart.series[0].markLine, trackingChart(bars, [level], ['2026-10-28']).series[0].markLine)
  const area = chart.series[0].markArea.data.at(-1)
  assert.equal(area[0].xAxis, '2026-10-26'); assert.equal(area[1].xAxis, '2026-10-27')
  assert.deepEqual(chart.dataZoom[0].xAxisIndex, [0, 1, 2])
})
test('phase overlay does not invent an earlier start, extend to live/future dates or leak a disabled layer', () => {
  const live = { ...bars[1], date: '2026-10-28', status: 'intraday' }
  const chart = trackingChart([...bars, live], [], ['2026-10-29'], 390, 350, [{ ...phase, structure_start: null }, { ...phase, as_of: live.date }, { ...phase, as_of: '2026-10-29' }])
  assert.equal(chart.series.find(s => s.name === '威科夫阶段').data.length, 1)
  assert.equal(chart.series[0].markArea.data.length, 0)
  assert.equal(trackingChart(bars, [], []).series.find(s => s.name === '威科夫阶段').data.length, 0)
})
test('same-session correction uses last selected-version observation without mutating history', () => {
  const history = [phase, { ...phase, phase: 'uncertain', candidate_phase: null, status: 'uncertain', version_id: 'two' }]
  const chart = trackingChart(bars, [], [], 1200, 430, history)
  const data = chart.series.find(s => s.name === '威科夫阶段').data
  assert.equal(data.length, 1); assert.equal(data[0].phase.version_id, 'two')
  assert.equal(history[0].phase, 'testing')
})

test('cross-baseline same-day phases remain separate and historical judgments never paint current areas', () => {
  const old = { ...phase, source_analysis_id: 'old', display_role: 'historical' }
  const current = { ...phase, source_analysis_id: 'new', display_role: 'current' }
  const chart = trackingChart(bars, [], [], 1200, 430, [old, current])
  const data = chart.series.find(s => s.name === '威科夫阶段').data
  assert.equal(data.length, 2)
  assert.equal(data[0].symbol, 'emptyCircle'); assert.equal(data[1].symbol, 'roundRect')
  assert.notDeepEqual(data[0].value, data[1].value)
  assert.equal(data[0].value[0], phase.as_of)
  const historical = trackingChart(bars, [], [], 1200, 430, [old])
  assert.equal(historical.series[0].markArea.data.length, 0)
  assert.match(historical.series.find(s => s.name === '威科夫阶段').tooltip.formatter({data:data[0]}), /历史判断/)
})
test('baseline boundary retains report and adoption dates, groups same-date reports and clears when disabled', () => {
  const one = { analysis_id:'old', as_of:'2026-10-27', report_created_at:'2026-10-27T09:00:00Z', activated_at:'2026-10-28T01:00:00Z', selected:false }
  const two = { ...one, analysis_id:'new', activated_at:'2026-10-28T02:00:00Z', selected:true }
  const chart = trackingChart(bars, [level], [], 390, 350, [phase], undefined, undefined, [one,two])
  const marks = chart.series.find(s => s.name === '研究起点分界').markLine
  assert.equal(marks.data.length, 1); assert.equal(marks.data[0].xAxis, '2026-10-27')
  assert.equal(marks.data[0].boundaries.length, 2)
  assert.match(marks.data[0].label.formatter, /2份/)
  assert.match(marks.tooltip.formatter({data:marks.data[0]}), /2026-10-28T02:00:00Z/)
  assert.deepEqual(chart.series[0].markLine, trackingChart(bars,[level],[],390,350).series[0].markLine)
  assert.equal(trackingChart(bars,[],[]).series.find(s=>s.name==='研究起点分界').markLine.data.length,0)
})
