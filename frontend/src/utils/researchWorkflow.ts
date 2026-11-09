import type { WorkflowListItem, WorkflowSpecDto } from '@/api/workflows'
import type { AnalysisHorizon } from '@/types/analysis'

export function compatibleResearchWorkflows(items: WorkflowListItem[], horizon: AnalysisHorizon = 'short') {
  return items.filter(item => item.enabled && item.valid && (item.analysis_horizon || 'short') === horizon)
}

/** Core research roles are visible/selected; legacy short selections are unchanged. */
export function periodAnalystSelection(spec: WorkflowSpecDto, selected: string[], available: Set<string>): string[] {
  if (!spec.research_profile) return selected.filter(slug => available.has(slug))
  return [...new Set([
    ...spec.research_profile.required_analysts,
    ...selected.filter(slug => available.has(slug) && slug in spec.research_profile!.roles),
  ])]
}
