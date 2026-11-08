import { ApiClient } from './request'

export interface TrackingLevel {
  kind: 'support' | 'resistance' | 'entry' | 'exit' | 'target'
  label: string
  lower: number
  upper: number
  condition: string
  source_quote: string
  effective_from: string
  valid_until: string | null
  expired: boolean
  price_basis: 'raw' | 'unspecified'
  currency: 'CNY'
  state?: 'conditional' | 'expired' | 'report_reference' | 'previous_review' | 'intraday_candidate'
  source_section?: string; source_line?: number; source_date?: string; source_analysis_id?: string
  branch?: 'held' | 'unheld'
}
export interface TrackingBar { date: string; open: number; close: number; low: number; high: number; volume?: number; data_source: string; observed_at?: string; status?: 'intraday' | 'awaiting_daily'; stale?: boolean }
export interface TrackingMarketChart {
  chan?: ChanSnapshot
  as_of: string; bars: TrackingBar[]; price: TrackingBar | null; quality: string; limitations: string[]; next_sessions: string[]
  live_bar?: TrackingBar | null; live_status?: string; quote?: Record<string, any>; checked_at?: string
  session?: { date: string; phase: string; is_open: boolean | null; next_poll_at: string | null }
}
export interface ChanItem {
  id: string; status: 'confirmed' | 'candidate'; confirmed_at: string | null; updated_at_session: string
  date?: string; price?: number; kind?: 'top' | 'bottom'
  start?: string; end?: string; start_price?: number; end_price?: number; direction?: 'up' | 'down'; unfinished?: boolean
  lower?: number; upper?: number; stroke_count?: number
}
export interface ChanSnapshot {
  engine: string; status: 'ready' | 'partial' | 'stale' | 'unavailable'; notice?: string
  as_of?: string; window_start?: string; data_source?: string; recorded_at?: string; persisted?: boolean
  snapshot_id?: string; revision_reason?: string; bar_count?: number; limitations: string[]
  fractals: ChanItem[]; strokes: ChanItem[]; zones: ChanItem[]
  preview?: { as_of: string; observed_at: string; data_source: string; fractals: ChanItem[]; strokes: ChanItem[]; zones: ChanItem[] } | null
}
export interface TrackingModule { content: string; as_of: string; inherited: boolean; changes: string[]; limitations: string[]; update_error?: string }
export interface TrackingBaseline { analysis_id: string; as_of: string; created_at: string | null; is_current?: boolean }
export type WyckoffPhase = 'accumulation' | 'testing' | 'markup' | 'distribution' | 'markdown' | 'uncertain'
export interface WyckoffAnnotation {
  display_role?: 'historical' | 'current'; carried_forward?: boolean
  method_version?: 'wyckoff-method-v2' | null
  trend?: 'up' | 'down' | 'range' | 'uncertain'
  events?: { kind: string; date: string; status: 'candidate' | 'observed'; source_quote: string }[]
  change_reason?: string | null; alternative_scenario?: string | null
  phase: WyckoffPhase; candidate_phase: WyckoffPhase | null
  status: 'candidate' | 'supported' | 'transition' | 'invalidated' | 'uncertain'
  source_quote: string; source_line?: number; structure_start: string | null
  confirmation_conditions: string[]; invalidation_conditions: string[]; limitations: string[]
  as_of: string; known_at: string | null; source_kind: 'report' | 'daily'
  source_analysis_id: string; version_id: string | null
}
export interface WyckoffBoundary {
  analysis_id: string; as_of: string; report_created_at: string | null
  activated_at: string | null; selected: boolean
}
export interface WyckoffBoard {
  pending_judgment?: { as_of: string; source_quote: string; source_kind: string; known_at: string | null; status: string } | null
  timeline?: { history: WyckoffAnnotation[]; boundaries: WyckoffBoundary[]; truncated: boolean; cutoff_at: string | null }
  previous_reference?: WyckoffAnnotation | null
  error?: string | null; error_details_missing?: boolean; source_analysis_id?: string; source_date?: string
  current: WyckoffAnnotation | null; history: WyckoffAnnotation[]
  status: 'baseline' | 'current' | 'inherited' | 'failed' | 'missing'; notice: string
}
export interface TargetComparison {
  source_date: string; previous_date: string | null; known_at: string | null; notice: string
  previous_targets: TrackingLevel[]
  targets: { label: string; lower: number; upper: number; condition: string; change: string; status: string; evidence: { date: string; field: string; actual: number | null; price: number; status: string }[] }[]
}
export interface TrackingBoard {
  target_comparison?: TargetComparison
  previous_review?: { version_id: string; as_of: string; created_at: string; levels: TrackingLevel[] } | null
  budget?: { date: string; used: number; limit: number | null; owner_used: number; owner_limit: number | null; remaining: number | null; unlimited?: boolean; resets_at: string; message: string }
  manual_options?: { update_max_calls: number; continue_max_calls: number; max_extra_calls: number }
  wyckoff?: WyckoffBoard
  baseline?: TrackingBaseline & { levels: TrackingLevel[]; level_notice: string }
  baselines?: TrackingBaseline[]
  newer_report?: TrackingBaseline | null
  track: null | { id: string; symbol: string; name: string; baseline_date: string; baseline_analysis_id: string; settings: { auto_enabled: boolean; max_calls_per_day: number; intraday_enabled?: boolean } }
  versions: { id: string; as_of: string; created_at: string; status: string }[]
  run: null | { id: string; status: string; message: string; model_calls: number; retry_after_at?: string }
  version: null | {
    experience_usage?: import('./experience').ExperienceReceipt[]
    id: string; as_of: string; created_at: string; status: string; errors: string[]; levels: TrackingLevel[]
    snapshot: { chan?: ChanSnapshot; bars: TrackingBar[]; next_sessions: string[]; price: TrackingBar | null; domains: Record<string, { status: string; latest_date: string | null; limitations: string[] }> }
    modules: Record<string, TrackingModule>
    review: null | { content: string; changes: string[]; limitations: string[]; requires_full_review: boolean; unheld_action: string; held_action: string; research_view: string; strategy: null | { valid_until: string; conditions: { kind: string; text: string; state: string; evidence_ids: string[] }[] } }
  }
}

export interface TrackingStock { symbol: string; name: string; baseline_date: string; auto_enabled: boolean }
export interface IntradayResearchValue {
  alerts?: { id: string; title: string; label: string; lower: number; upper: number; evidence_at: string; message: string; condition: string }[]
  enabled: boolean; model_review_enabled: false; changed_basis?: boolean
  source_date: string; source_revision: string; baseline_analysis_id: string
  snapshot?: { status: string; as_of: string | null; checked_at: string; source: string | null; issues: string[]; date: string }
  conditions: IntradayCondition[]
  zones: { id: string; label: string; kind: string; lower: number; upper: number; observed_at: string; valid_until: string; source: string; condition: string }[]
  volume_ratio_previous_two_bars: number | null; wyckoff_candidate: string | null; limitations: string[]
  opening_reference?: { lower: number; upper: number; latest_close: number; position: string } | null
  history: { id: string; created_at: string; as_of: string | null; status: string; source_date: string; changes: { label: string; from: string; to: string; at: string }[] }[]
}
export interface IntradayCondition {
  id: string; label: string; category?: 'level' | 'strategy'; kind?: string; state: string; reason: string; condition: string
  source_date?: string; previous_state?: string; lower?: number; upper?: number
  day_touched?: boolean; first_touch_at?: string | null; latest_close?: number; interval_end?: string
  checks?: { text: string; state: string; reason: string; partial?: boolean; source?: string; as_of?: string }[]
  missing_data?: string[]; unsupported?: string[]
}
export const researchTrackingApi = {
  intraday: (id: string) => ApiClient.get<IntradayResearchValue>(`/api/research-tracking/${id}/intraday`),
  list: () => ApiClient.get<TrackingStock[]>('/api/research-tracking/tracks'),
  marketChart: (symbol: string, forceRefresh = false) => ApiClient.get<TrackingMarketChart>(`/api/research-tracking/stocks/${symbol}/market-chart`, { force_refresh: forceRefresh }),
  board: (symbol: string, version_id?: string, baseline_analysis_id?: string) => ApiClient.get<TrackingBoard>(`/api/research-tracking/stocks/${symbol}`, { version_id, baseline_analysis_id }),
  create: (symbol: string) => ApiClient.post(`/api/research-tracking/stocks/${symbol}`, {}),
  settings: (id: string, value: { auto_enabled: boolean; max_calls_per_day: number; intraday_enabled?: boolean }) => ApiClient.put(`/api/research-tracking/${id}/settings`, value),
  update: (id: string, retry_failed = false, manual_extra_calls = 0, expected_baseline_analysis_id?: string) => ApiClient.post(`/api/research-tracking/${id}/updates`, { retry_failed, manual_extra_calls, expected_baseline_analysis_id }),
  switchBaseline: (id: string, analysis_id: string, expected_baseline_analysis_id: string) => ApiClient.post(`/api/research-tracking/${id}/baseline`, { analysis_id, expected_baseline_analysis_id })
}
