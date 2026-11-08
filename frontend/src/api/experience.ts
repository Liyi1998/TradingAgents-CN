import { ApiClient } from './request'

export interface ExperienceEntry {
  id: string; task_id: string; symbol: string; decision_date: string; available_date: string
  horizon_days: number; lesson: string; explicitly_cited: boolean
}
export interface ExperienceReceipt {
  id: string; context_id: string; kind: string; stage: string; as_of: string; created_at: string
  status: string; reason: string; delivery: string; citation_status: string; entries: ExperienceEntry[]
  reused_from?: string
}
export interface ReflectionRecord {
  id: string; task_id: string; symbol: string; stock_name?: string; decision_date: string
  horizon_days: number; signal: string; status: string; exit_date?: string; return_pct?: number
  reflection_status?: string; reflection_lesson?: string; reflection_available_date?: string; reflected_at?: string
}
export const experienceApi = {
  usage: (id: string) => ApiClient.get<ExperienceReceipt[]>(`/api/settlement/experience-usage/${encodeURIComponent(id)}`),
  reflections: (symbol?: string, page = 1) => ApiClient.get<{ enabled: boolean; total: number; items: ReflectionRecord[] }>('/api/settlement/reflections', { symbol, page, page_size: 20 })
}
