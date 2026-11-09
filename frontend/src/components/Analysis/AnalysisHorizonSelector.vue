<template>
  <div class="analysis-horizon-selector">
    <el-select
      :model-value="modelValue"
      aria-label="分析周期"
      size="large"
      @update:model-value="updateHorizon"
    >
      <el-option
        v-for="option in ANALYSIS_HORIZONS"
        :key="option.value"
        :value="option.value"
        :label="`${option.label}（${option.period}）`"
      />
    </el-select>
    <p class="horizon-description" aria-live="polite">{{ description }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { AnalysisHorizon } from '@/types/analysis'
import { ANALYSIS_HORIZONS, isAnalysisHorizon } from '@/utils/analysisHorizon'

const props = defineProps<{ modelValue: AnalysisHorizon }>()
const emit = defineEmits<{ 'update:modelValue': [value: AnalysisHorizon] }>()
const description = computed(() =>
  ANALYSIS_HORIZONS.find(option => option.value === props.modelValue)?.description ?? '',
)

function updateHorizon(value: unknown) {
  if (isAnalysisHorizon(value)) emit('update:modelValue', value)
}
</script>

<style scoped>
.analysis-horizon-selector {
  width: 100%;
  min-width: 0;
}
.analysis-horizon-selector .el-select {
  width: 100%;
}
.horizon-description {
  margin: 6px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
</style>
