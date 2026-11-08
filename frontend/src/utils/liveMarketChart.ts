import type { TrackingMarketChart } from '@/api/researchTracking'

export function marketBars(value: TrackingMarketChart | null | undefined) {
  const bars = [...(value?.bars || [])]
  const live = value?.live_bar
  // Completed evidence always wins. Never append an old or duplicate day.
  if (live && live.date === value?.session?.date && !bars.some(b => b.date >= live.date)) bars.push(live)
  return bars
}

export function mergeMarketChart(previous: TrackingMarketChart | null, incoming: TrackingMarketChart) {
  const old = previous?.live_bar
  const current = incoming.live_bar
  if (old && old.date === incoming.session?.date && !incoming.bars.some(b => b.date >= old.date) &&
      (incoming.live_status === 'unavailable' || (current?.observed_at && old.observed_at && current.observed_at < old.observed_at))) {
    return { ...incoming, chan: incoming.chan ? { ...incoming.chan, preview: null } : undefined, live_bar: { ...old, stale: true }, quote: previous?.quote, live_status: 'delayed' }
  }
  return incoming
}

export function nextMarketPollDelay(value: TrackingMarketChart | null, now = Date.now()) {
  if (!value?.session) return 30000 // bounded recovery after a failed initial read
  if (!value.session.next_poll_at) return null
  const next = Date.parse(value.session.next_poll_at)
  return Number.isFinite(next) ? Math.max(1000, Math.min(next - now, 2147483647)) : null
}
