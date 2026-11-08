<template>
  <el-card class="decision-semantics-card" shadow="never">
    <template #header>
      <div class="decision-header">
        <span>🎯 分账户裁决</span>
        <el-tag v-if="decision.derived_from_legacy" type="info" size="small">
          旧版兼容解析
        </el-tag>
      </div>
    </template>

    <div class="decision-grid" :class="{ compact }">
      <div class="decision-field">
        <span class="field-label">账户持仓</span>
        <strong>{{ accountLabel }}</strong>
        <small v-if="accountPosition === 'UNKNOWN'">未提供账户事实，主标签按未持仓分支</small>
      </div>
      <div class="decision-field">
        <span class="field-label">研究观点</span>
        <el-tag :type="researchTagType">{{ researchLabel }}</el-tag>
      </div>
      <div class="decision-field action-field">
        <span class="field-label">未持仓动作</span>
        <el-tag :type="actionTagType(unheldAction)" size="large">
          {{ actionLabel(unheldAction) }} · {{ unheldAction }}
        </el-tag>
        <small>{{ unheldAction === 'BUY' ? '现在可建仓' : '现在不买，等待条件' }}</small>
      </div>
      <div class="decision-field action-field">
        <span class="field-label">已持仓动作</span>
        <el-tag :type="actionTagType(heldAction)" size="large">
          {{ actionLabel(heldAction) }} · {{ heldAction }}
        </el-tag>
        <small>{{ heldActionDescription }}</small>
      </div>
      <div class="decision-field">
        <span class="field-label">当前总仓位上限</span>
        <strong>{{ positionLimitText }}</strong>
      </div>
      <div class="decision-field">
        <span class="field-label">当前新增上限</span>
        <strong>{{ entryLimitText }}</strong>
      </div>
    </div>

    <div v-if="conditionalStrategies.length" class="exit-conditions">
      <span class="field-label">后续条件策略（非当前额度）</span>
      <ul><li v-for="(strategy, index) in conditionalStrategies" :key="index">{{ strategy }}</li></ul>
    </div>

    <div v-if="exitConditions.length" class="exit-conditions">
      <span class="field-label">退出条件</span>
      <ul>
        <li v-for="condition in exitConditions" :key="condition">{{ condition }}</li>
      </ul>
    </div>

    <el-alert
      v-if="decision.derived_from_legacy"
      type="info"
      :closable="false"
      class="compatibility-note"
      title="以上字段由旧版裁决原文在读取时确定性拆分；原报告未被改写。"
    />
  </el-card>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { AnalysisDecision, DecisionAction } from '@/types/analysis'
import { conditionalStrategyTexts } from '@/utils/conditionalStrategy'

const props = withDefaults(defineProps<{
  decision: AnalysisDecision
  compact?: boolean
}>(), {
  compact: false,
})

const accountPosition = computed(() => props.decision.account_position || 'UNKNOWN')
const unheldAction = computed(() => props.decision.unheld_action || 'WATCH')
const heldAction = computed(() => props.decision.held_action || 'HOLD')
const exitConditions = computed(() => props.decision.exit_conditions || [])
const conditionalStrategies = computed(() => conditionalStrategyTexts(props.decision.conditional_strategy))

const ACCOUNT_LABELS = {
  UNKNOWN: '未提供',
  NONE: '未持仓',
  HELD: '已持仓',
} as const
const RESEARCH_LABELS = {
  BULLISH: '偏多',
  NEUTRAL: '中性',
  BEARISH: '偏空',
  UNCERTAIN: '不确定',
} as const
const ACTION_LABELS: Record<DecisionAction, string> = {
  BUY: '买入',
  WATCH: '观望',
  HOLD: '持有',
  REDUCE: '减仓',
  SELL: '卖出',
}

const accountLabel = computed(() => ACCOUNT_LABELS[accountPosition.value])
const researchView = computed(() => props.decision.research_view || 'UNCERTAIN')
const researchLabel = computed(() => RESEARCH_LABELS[researchView.value])
const researchTagType = computed<'success' | 'danger' | 'info'>(() => {
  if (researchView.value === 'BULLISH') return 'success'
  if (researchView.value === 'BEARISH') return 'danger'
  return 'info'
})
const positionLimitText = computed(() => {
  const value = "total_position_limit_pct" in props.decision ? props.decision.total_position_limit_pct : props.decision.position_limit_pct
  return value === null || value === undefined ? '未明确' : `${value}%`
})
const entryLimitText = computed(() => props.decision.current_entry_limit_pct == null ? '未明确' : String(props.decision.current_entry_limit_pct) + '%')
const heldActionDescription = computed(() => ({
  HOLD: '仅表示已有仓位继续持有',
  REDUCE: '降低已有仓位',
  SELL: '退出已有仓位',
}[heldAction.value]))

function actionLabel(action: DecisionAction): string {
  return ACTION_LABELS[action]
}

function actionTagType(action: DecisionAction): 'success' | 'warning' | 'info' | 'danger' {
  if (action === 'BUY') return 'success'
  if (action === 'SELL') return 'danger'
  if (action === 'HOLD' || action === 'REDUCE') return 'warning'
  return 'info'
}
</script>

<style scoped>
.decision-semantics-card {
  margin: 16px 0;
  border-color: var(--el-border-color-light);
}

.decision-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-weight: 700;
}

.decision-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 12px;
}

.decision-grid.compact {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.decision-field {
  display: flex;
  min-height: 86px;
  flex-direction: column;
  align-items: flex-start;
  justify-content: center;
  gap: 7px;
  padding: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  background: var(--el-fill-color-lighter);
  text-wrap: pretty;
}

.field-label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  font-weight: 600;
}

.decision-field small {
  color: var(--el-text-color-secondary);
  line-height: 1.4;
}

.exit-conditions {
  margin-top: 14px;
  padding: 12px 14px;
  border-left: 3px solid var(--el-color-warning);
  background: var(--el-color-warning-light-9);
}

.exit-conditions ul {
  margin: 8px 0 0;
  padding-left: 20px;
  line-height: 1.7;
  overflow-wrap: anywhere;
  text-wrap: pretty;
}

.compatibility-note {
  margin-top: 14px;
}

@media (max-width: 1100px) {
  .decision-grid,
  .decision-grid.compact {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 640px) {
  .decision-grid,
  .decision-grid.compact {
    grid-template-columns: 1fr;
  }
}
</style>
