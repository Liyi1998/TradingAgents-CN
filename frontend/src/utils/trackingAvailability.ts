import type { TrackingBoard } from '@/api/researchTracking'

export function trackingAvailability(board: TrackingBoard | null, completedDay?: string, now = Date.now()) {
  const status = board?.run?.status || ''
  const exhausted = !board?.budget?.unlimited && board?.budget?.remaining === 0
  const manualAvailable = (board?.manual_options?.max_extra_calls || 0) > 0
  const retryAt = board?.run?.retry_after_at
  const cooldown = ['partial', 'failed'].includes(status) && retryAt
    ? Math.max(0, new Date(retryAt).getTime() - now) : 0
  const waiting = !!board?.track && (completedDay
    ? completedDay < board.track.baseline_date
    : status === 'waiting_data' || (board.run?.message || '').startsWith('最近已结束交易日早于原报告'))
  return {
    blocked: (exhausted && !manualAvailable) || waiting || cooldown > 0,
    canRetry: (!exhausted || manualAvailable) && !waiting && ['partial', 'failed', 'interrupted'].includes(status),
    cooldown,
    waiting,
  }
}

export function trackingManualAllowance(board: TrackingBoard | null, retry: boolean) {
  const options = board?.manual_options
  const maximum = (retry ? options?.continue_max_calls : options?.update_max_calls) || 10
  const remaining = board?.budget?.remaining || 0
  const extra = board?.budget?.unlimited ? 0 : options ? Math.min(options.max_extra_calls, Math.max(0, maximum - remaining)) : 0
  return { maximum, remaining, extra }
}

export function trackingPointSource(board: TrackingBoard | null, requested: 'review' | 'baseline') {
  if (requested === 'baseline' && board?.baseline?.levels.length) return 'baseline'
  if (board?.version?.levels.length) return 'review'
  // A successful new review may deliberately remove all levels. Never revive
  // older levels in that case; the API only exposes fallback for missing review.
  if (!board?.version?.review && board?.previous_review?.levels.length) return 'previous'
  return 'baseline'
}
