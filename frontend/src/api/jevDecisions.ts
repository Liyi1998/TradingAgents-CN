import { ApiClient } from './request'

export interface StrategyInput {
  entry: string
  exit: string
  veto: string
  valid_until: string
  complete_conditions_confirmed: boolean
}
export interface DecisionExperiment {
  id: string
  status: string
  reason?: string
  guards?: string[]
  created_at?: string
  expires_at?: string
  elapsed_seconds?: number
  external_call_attempted: boolean
  affects_decision: boolean
  model_call_enabled?: boolean
  result?: { entry_action: string; existing_action: string; risk_state: string }
  packet?: {
    strategy: { symbol: string; analysis_horizon: string; valid_until: string; entry_candidate_permitted: boolean }
    market_state: { as_of?: string; bars?: Array<{ trade_date: string; close: number; data_source: string }> }
  }
}
const base = (id: string) => `/api/jev-decisions/${encodeURIComponent(id)}`
const options = { skipErrorHandler: true, retryCount: 0, timeout: 35000 }
export const jevDecisionsApi = {
  history: (id: string) => ApiClient.get<DecisionExperiment[]>(base(id), undefined, options),
  preview: (id: string, value: StrategyInput) => ApiClient.post<DecisionExperiment>(`${base(id)}/preview`, value, options),
  run: (id: string, preview: string) => ApiClient.post<DecisionExperiment>(`${base(id)}/${encodeURIComponent(preview)}/run`, { confirm_external: true }, options),
}
