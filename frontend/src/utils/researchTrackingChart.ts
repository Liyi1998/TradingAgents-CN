import type { TrackingBar, TrackingLevel, WyckoffAnnotation, WyckoffBoundary, ChanSnapshot, ChanItem } from '@/api/researchTracking'
import { phaseColors, phaseLabel, phaseMarkers, phaseStatus, phaseRole } from './wyckoffTracking'

const colors: Record<string, string> = { support: '#4b86b4', resistance: '#9470b8', entry: '#c4a24e', exit: '#c65353', target: '#438d72' }

export const formatLevelPrice = (value: number) => value.toLocaleString('en-US', { useGrouping: false, minimumFractionDigits: 2, maximumFractionDigits: 20 })

export function chanSeries(value: ChanSnapshot | undefined, dates: string[], enabled = { fractals: false, strokes: true, zones: true, candidates: true }) {
  if (!value || value.status === 'unavailable') return []
  const series: any[] = []
  const describe = (item: ChanItem, type: string, live: boolean) => `${type} · ${item.status === 'confirmed' ? '算法确认' : '候选／可变'}\n${item.date || item.start}${item.end ? ' → ' + item.end : ''}\n${item.confirmed_at ? '确认于 ' + item.confirmed_at : '尚未确认'}\n结构更新于 ${item.updated_at_session}\n${live ? '盘中快照 ' + value.preview?.observed_at : '完整日线截至 ' + value.as_of}\n来源 ${live ? value.preview?.data_source : value.data_source} · 不复权\n后续行情可修订，不代表买卖点`
  const items = (key: 'fractals' | 'strokes' | 'zones') => [
    ...value[key].filter(x => x.status === 'confirmed' || (enabled.candidates && !value.preview)).map(item => ({ item, live: false })),
    ...(enabled.candidates ? (value.preview?.[key] || []).map(item => ({ item, live: true })) : [])]
  if (enabled.zones) for (const { item: x, live } of items('zones')) {
    if (!x.start || !x.end || !dates.includes(x.start) || !dates.includes(x.end)) continue
    series.push({ id: `chan-zone-${x.id}-${live}`, name: '笔中枢', type: 'line', data: [], z: 1, silent: false,
      markArea: { label: { show: false }, itemStyle: { color: x.status === 'confirmed' ? 'rgba(79,115,178,.10)' : 'rgba(196,162,78,.06)', borderColor: x.status === 'confirmed' ? '#688dbb' : '#c4a24e', borderWidth: 1, borderType: x.status === 'confirmed' ? 'solid' : 'dashed' },
        tooltip: { trigger: 'item', formatter: () => describe(x, `笔中枢 ${x.lower?.toFixed(2)}–${x.upper?.toFixed(2)}`, live) },
        data: [[{ xAxis: x.start, yAxis: x.lower }, { xAxis: x.end, yAxis: x.upper }]] } })
  }
  if (enabled.strokes) for (const { item: x, live } of items('strokes')) {
    if (!x.start || !x.end || !dates.includes(x.start) || !dates.includes(x.end)) continue
    series.push({ id: `chan-stroke-${x.id}-${live}`, name: x.unfinished ? '未完成笔' : '笔', type: 'line', z: 4, showSymbol: false, connectNulls: true,
      data: [[x.start, x.start_price], [x.end, x.end_price]],
      lineStyle: { color: x.status === 'confirmed' ? '#4568a2' : '#ba9844', width: 1.7, type: x.status === 'confirmed' ? 'solid' : 'dashed' },
      tooltip: { trigger: 'item', formatter: () => describe(x, x.unfinished ? '未完成笔' : '笔', live) } })
  }
  if (enabled.fractals) series.push({ id: 'chan-fractals', name: '分型', type: 'scatter', z: 6, symbolSize: 7,
    tooltip: { trigger: 'item', formatter: (p: any) => p.data.description },
    data: items('fractals').filter(({ item }) => dates.includes(item.date!)).map(({ item: x, live }) => ({ value: [x.date, x.price], symbol: 'triangle', symbolRotate: x.kind === 'top' ? 180 : 0,
      itemStyle: { color: x.status === 'confirmed' ? '#4568a2' : '#ba9844' }, description: describe(x, x.kind === 'top' ? '顶分型' : '底分型', live) })) })
  return series
}

export function trackingChart(bars: TrackingBar[], levels: TrackingLevel[], future: string[], width = 1200, height = 430, wyckoff: WyckoffAnnotation[] = [], chan?: ChanSnapshot, chanLayers?: { fractals: boolean; strokes: boolean; zones: boolean; candidates: boolean }, boundaries: WyckoffBoundary[] = []) {
  const validBars = bars.filter(b => /^\d{4}-\d{2}-\d{2}$/.test(b.date) && [b.open, b.close, b.low, b.high].every(n => Number.isFinite(n) && n > 0))
  const dates = [...new Set([...validBars.map(b => b.date), ...future])].sort()
  const byDate = new Map(validBars.map(b => [b.date, b]))
  const phases = phaseMarkers(wyckoff, validBars.filter(b => !b.status).map(b => b.date))
  const latestPhase = phases.find(p => p.display_role === 'current') || [...phases].reverse().find(p => !p.display_role)
  const phaseRows = phases.map((p, i) => phases.slice(0, i).filter(v => v.as_of === p.as_of).length)
  const phaseRowCount = Math.max(1, ...phaseRows.map(n => n + 1))
  const visibleBoundaries = boundaries.filter(b => dates.includes(b.as_of))
  const boundaryDates = [...new Set(visibleBoundaries.map(b => b.as_of))]
  const until = (p: TrackingLevel) => p.state === 'report_reference' ? dates.at(-1) || '' : p.valid_until || ''
  const visible = levels.filter(p => (p.price_basis === 'raw' || (p.state === 'report_reference' && p.price_basis === 'unspecified')) && p.currency === 'CNY' && p.effective_from <= until(p) &&
    Number.isFinite(p.lower) && Number.isFinite(p.upper) && p.lower > 0 && p.upper >= p.lower &&
    dates.some(d => d >= p.effective_from && d <= until(p)))
  const compact = width < 640
  const narrow = width < 480
  const labelWidth = narrow ? 24 : compact ? 112 : 246
  const right = visible.length ? labelWidth + 38 : 24
  const priceHeight = height * .62 - 20 // Reserve room for date labels above the phase strip.
  const color = (p: TrackingLevel) => p.state === 'intraday_candidate' ? '#397f8c' : p.state === 'report_reference' ? '#82735e' : p.expired || p.state === 'previous_review' ? '#999' : colors[p.kind]
  const priceLabel = (p: TrackingLevel) => `${formatLevelPrice(p.lower)}${p.upper !== p.lower ? '–' + formatLevelPrice(p.upper) : ''}`
  let axisMin = 0, axisMax = 1
  const labelLayout = (params: { dataIndex: number }) => {
    const gap = compact ? 28 : 44
    const top = 24 + gap / 2, bottom = 24 + priceHeight - gap / 2
    const ordered = visible.map((p, index) => ({ index, y: 24 + (axisMax - p.upper) / (axisMax - axisMin) * priceHeight }))
      .sort((a, b) => a.y - b.y || a.index - b.index)
    ordered.forEach((p, i) => { p.y = Math.max(p.y, i ? ordered[i - 1].y + gap : top) })
    for (let i = ordered.length - 1; i >= 0; i--) ordered[i].y = Math.min(ordered[i].y, i === ordered.length - 1 ? bottom : ordered[i + 1].y - gap)
    return { x: width - right + 24, y: ordered.find(p => p.index === params.dataIndex)?.y, align: 'left', verticalAlign: 'middle' }
  }
  const lines = visible.flatMap(p => {
    const start = dates.find(d => d >= p.effective_from)!
    const end = [...dates].reverse().find(d => d <= until(p))!
    return [p.lower, ...(p.upper !== p.lower ? [p.upper] : [])].map(price => [
      { name: `${p.state === 'report_reference' ? '原报告·' : p.state === 'previous_review' ? '上次未复核·' : ''}${p.label} ${formatLevelPrice(p.lower)}${p.upper !== p.lower ? '–' + formatLevelPrice(p.upper) : ''}${p.expired ? '（到期）' : ''}`,
        label: { show: false }, coord: [start, price], lineStyle: { color: color(p), type: 'dashed', width: 1.5 } },
      { coord: [end, price] }
    ])
  })
  return {
    animation: false,
    tooltip: { trigger: 'axis', renderMode: 'richText', axisPointer: { type: 'cross' },
      formatter: (params: any[]) => {
        const bar = byDate.get(params[0]?.axisValue)
        if (!bar) return String(params[0]?.axisValue || '')
        const state = bar.status === 'intraday' ? ' · 未收盘' : bar.status === 'awaiting_daily' ? ' · 待完整日线确认' : ''
        return `${bar.date}${state}${bar.stale ? ' · 行情延迟' : ''}\n开 ${bar.open.toFixed(2)}  高 ${bar.high.toFixed(2)}\n低 ${bar.low.toFixed(2)}  ${bar.status === 'intraday' ? '最新' : '收'} ${bar.close.toFixed(2)}\n成交量 ${bar.volume ?? '未知'} 股${bar.observed_at ? '\n快照 ' + bar.observed_at : ''}\n来源 ${bar.data_source}`
      } },
    grid: [{ left: compact ? 46 : 64, right, top: 24, height: priceHeight }, { left: compact ? 46 : 64, right, top: '82%', height: '7%' }, { left: compact ? 46 : 64, right, top: '72%', height: phaseRowCount > 1 ? '9%' : '5%' }],
    xAxis: [{ type: 'category', data: dates, boundaryGap: true, axisLabel: { hideOverlap: true } }, { type: 'category', data: dates, gridIndex: 1, axisLabel: { show: false } }, { type: 'category', data: dates, gridIndex: 2, show: false }],
    yAxis: [{ scale: true, name: 'CNY / 股', axisLabel: { formatter: (value: number) => Number(value.toFixed(4)).toString() }, splitLine: { lineStyle: { opacity: .25 } },
      ...(visible.length ? {
        min: (extent: { min: number }) => (axisMin = Math.min(extent.min, ...visible.map(p => p.lower)) * .99),
        max: (extent: { max: number }) => (axisMax = Math.max(extent.max, ...visible.map(p => p.upper)) * 1.01)
      } : {})
    }, { scale: true, gridIndex: 1, name: '成交量 / 股', nameLocation: 'middle', nameGap: 40, nameTextStyle: { fontSize: 10 }, axisLabel: { show: false }, splitLine: { show: false } }, { gridIndex: 2, min: 0, max: phaseRowCount, show: false }],
    dataZoom: [{ type: 'inside', xAxisIndex: [0, 1, 2], start: dates.length > 70 ? 50 : 0, end: 100 }, { type: 'slider', xAxisIndex: [0, 1, 2], bottom: 0, height: 18 }],
    series: [
      { name: '不复权日K', type: 'candlestick', data: dates.map(d => { const b = byDate.get(d); return b ? (b.status ? { value: [b.open, b.close, b.low, b.high], itemStyle: { borderWidth: 2, opacity: b.stale ? .45 : .8 } } : [b.open, b.close, b.low, b.high]) : ['-', '-', '-', '-'] }),
        itemStyle: { color: '#c65353', color0: '#438d72', borderColor: '#c65353', borderColor0: '#438d72' },
        markLine: { symbol: ['none', 'none'], label: { show: false }, data: lines },
        markArea: { silent: true, label: { show: false }, data: [...visible.filter(p => p.lower < p.upper).map(p => [
          { xAxis: dates.find(d => d >= p.effective_from), yAxis: p.lower, itemStyle: { color: color(p), opacity: .08 } },
          { xAxis: [...dates].reverse().find(d => d <= until(p)), yAxis: p.upper }
        ]), ...(latestPhase?.structure_start && byDate.has(latestPhase.structure_start) ? [[
          { xAxis: latestPhase.structure_start, itemStyle: { color: phaseColors[latestPhase.phase], opacity: .045 } },
          { xAxis: latestPhase.as_of }
        ]] : [])] }
      },
      { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: dates.map(d => { const b = byDate.get(d); return b && Number.isFinite(b.volume) ? { value: b.volume, itemStyle: { color: b.close >= b.open ? '#c65353' : '#438d72' } } : '-' }) },
      // The gutter is separate from candles. Keep labels inside the price panel;
      // guide lines attach to actual prices even when close labels move apart.
      { name: '条件点位', type: 'scatter', symbolSize: 3, clip: false, z: 5,
        tooltip: { show: false }, emphasis: { disabled: true },
        label: { show: true, position: 'right', color: '#45413a', backgroundColor: 'rgba(255,255,255,.96)',
          borderRadius: 3, padding: [3, 5], fontSize: compact ? 10 : 11, lineHeight: 16,
          formatter: (params: any) => params.data.labelText,
          rich: { title: { width: labelWidth - 10, overflow: 'truncate', fontSize: 11, lineHeight: 16 } } },
        labelLayout,
        labelLine: { show: true, length2: 8, lineStyle: { width: 1, opacity: .65 } },
        data: visible.map(p => ({ value: [[...dates].reverse().find(d => d <= until(p)), p.upper],
          labelText: narrow ? String(levels.indexOf(p) + 1) : compact ? `${levels.indexOf(p) + 1} · ${priceLabel(p)}` : `{title|${levels.indexOf(p) + 1} · ${p.state === 'report_reference' ? '原报告·' : p.state === 'previous_review' ? '上次未复核·' : ''}${p.label}${p.expired ? '（到期）' : ''}}\n${priceLabel(p)} 元`,
          itemStyle: { color: color(p) } }))
      },
      { name: '威科夫阶段', type: 'scatter', xAxisIndex: 2, yAxisIndex: 2, symbol: 'roundRect', symbolSize: [10, 16],
        tooltip: { trigger: 'item', formatter: (p: any) => `${phaseRole(p.data.phase)}\n${p.data.phase.as_of} · ${phaseLabel(p.data.phase)}\n${p.data.phase.source_kind === 'report' ? '原报告参考' : phaseStatus(p.data.phase.status)}\n系统记录：${p.data.phase.known_at || '未记录'}\n在阶段变化记录中查看原文及来源；历史判断未延续为当前结论` },
        label: { show: false },
        labelLayout: { hideOverlap: true },
        data: phases.map((p, i) => ({ value: [p.as_of, phaseRows[i] + .5], phase: p,
          symbol: p.display_role === 'historical' ? 'emptyCircle' : 'roundRect', symbolSize: [10, Math.max(4, Math.min(16, height * .09 / phaseRowCount - 2))],
          label: { show: p === latestPhase || p === [...phases].reverse().find(x => x.display_role === 'historical'), position: 'left', formatter: `${phaseRole(p)} · ${p.as_of.slice(5)} · ${phaseLabel(p)}`, fontSize: compact ? 10 : 11, color: '#45413a', backgroundColor: 'rgba(255,255,255,.95)', padding: [2, 4] },
          itemStyle: { color: p.display_role === 'historical' ? '#818791' : phaseColors[p.phase], opacity: p.source_kind === 'report' || p.status === 'invalidated' ? .65 : 1 } })) },
      { name: '研究起点分界', type: 'scatter', data: [],
        markLine: { symbol: ['none', 'none'], silent: false,
          tooltip: { trigger: 'item', formatter: (p: any) => p.data.boundaries.map((b: WyckoffBoundary) => `${b.selected ? '所选' : '历史'}研究起点报告：${b.as_of}\n生成：${b.report_created_at || '未记录'}\n采用：${b.activated_at || '此视图无采用时间'}`).join('\n\n') + '\n分界定位报告日期，不代表当时已经切换或阶段已确认' },
          data: boundaryDates.map(day => {
            const group = visibleBoundaries.filter(b => b.as_of === day), selected = group.some(b => b.selected)
            return { xAxis: day, boundaries: group,
              label: { show: true, position: 'insideEndTop', formatter: `${selected ? '所选' : '历史'}起点 ${day.slice(5)}${group.length > 1 ? `（${group.length}份）` : ''}`, fontSize: 10, color: selected ? '#a17b28' : '#818791' },
              lineStyle: { type: 'dashed', width: selected ? 1.5 : 1, color: selected ? '#a17b28' : '#818791', opacity: .65 } }
          }) } },
      ...chanSeries(chan, dates, chanLayers)
    ]
  }
}
