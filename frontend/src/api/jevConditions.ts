import { ApiClient } from './request'

export interface JevConditionSettings {
  enabled: boolean
  external_calls_enabled: boolean
  decision_experiments_enabled: boolean
  final_action_enabled: boolean
  max_calls_per_day: number
  model: string
  timeout_seconds: number
  mode: 'shadow'
  key_available: boolean
  key_source: 'conditions' | 'saved_review' | 'missing'
  revision: string
}

export interface JevConditionUpdate {
  enabled: boolean
  external_calls_enabled: boolean
  max_calls_per_day: number
  revision: string
  confirm_external: boolean
  decision_experiments_enabled?: boolean
  final_action_enabled?: boolean
}

export const jevConditionsApi = {
  get: () => ApiClient.get<JevConditionSettings>('/api/config/jev-conditions', undefined, { skipErrorHandler: true, retryCount: 0 }),
  save: (value: JevConditionUpdate) => ApiClient.put<JevConditionSettings>('/api/config/jev-conditions', value, { skipErrorHandler: true, retryCount: 0 }),
}
