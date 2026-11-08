<template>
  <div class="jev-result" aria-live="polite">
    <el-alert :title="view.title" :type="view.tone" :closable="false" show-icon />
    <template v-if="view.valid">
      <dl v-if="view.completed" class="result-grid">
        <div><dt>方案条件</dt><dd>{{ view.condition }}</dd></div>
        <div><dt>证据冲突</dt><dd>{{ view.conflict }}</dd></div>
      </dl>
      <p v-else class="explanation">{{ record.status === 'running' ? '后台正在处理，稍后可刷新记录。' : '本条记录没有可采用的 Jev 核验结论。' }}</p>
      <div v-if="view.reasons.length" class="reason-section">
        <h4>{{ view.completed ? '主要依据' : '原因说明' }}</h4>
        <ul><li v-for="reason in view.reasons" :key="reason">{{ reason }}</li></ul>
      </div>
      <p class="original"><b>原系统动作（未改变）：</b>{{ view.original }}</p>
      <dl class="metadata">
        <div><dt>{{ view.modelLabel }}</dt><dd>{{ view.model }}</dd></div>
        <div><dt>外部调用</dt><dd>{{ view.attempted }}</dd></div>
        <div><dt>记录时间</dt><dd>{{ view.time }}</dd></div>
        <div><dt>处理耗时</dt><dd>{{ view.elapsed }}</dd></div>
      </dl>
    </template>
    <p v-else class="explanation">没有展示为“通过”。请联系管理员检查记录格式；原报告不受影响。</p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { conditionCheckView } from '@/utils/jevConditionCheck'
const props = defineProps<{ record: Record<string, unknown> }>()
const view = computed(() => conditionCheckView(props.record))
</script>

<style scoped>
.jev-result { min-width: 0; overflow-wrap: anywhere; }
.result-grid, .metadata { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin: 16px 0; }
.result-grid > div { padding: 16px; background: var(--el-fill-color-light); border: 1px solid var(--el-border-color-lighter); border-radius: 8px; }
dt { color: var(--el-text-color-secondary); font-size: 13px; margin-bottom: 8px; }
dd { margin: 0; line-height: 1.7; color: var(--el-text-color-primary); }
.result-grid dd { font-size: 16px; font-weight: 600; }
.metadata dd, .explanation, .original, li { font-size: 13px; line-height: 1.7; color: var(--el-text-color-regular); }
h4 { font-size: 14px; margin: 16px 0 8px; color: var(--el-text-color-primary); }
ul { margin: 0; padding-left: 20px; }
.original { padding-top: 16px; border-top: 1px solid var(--el-border-color-lighter); }
@media (max-width: 640px) { .result-grid, .metadata { grid-template-columns: 1fr; gap: 12px; } }
</style>
