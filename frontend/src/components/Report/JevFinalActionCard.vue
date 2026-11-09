<template>
  <el-card class="final-action" shadow="never" v-loading="loading">
    <template #header><div class="heading"><h3>Jev 自动动作建议</h3><el-tag type="info">与原裁决对照</el-tag><el-button size="small" @click="load">刷新结果</el-button></div></template>
    <p class="note">新报告完成后自动准备材料，无需选段。按报告分析时点判断，不是实时盯盘；不覆盖上方原裁决，不自动交易。</p>
    <el-alert v-if="error" :title="error" type="warning" :closable="false" />
    <p v-else-if="!row" class="note">暂无自动建议。报告生成时可能尚未启用；旧报告不会自动补跑。刷新只读取记录，不调用模型。</p>
    <template v-else>
      <p>分析时点：{{ row.analysis_date }} · {{ row.external_call_attempted ? '已尝试调用 Jev' : '未调用 Jev' }}<span v-if="row.elapsed_seconds !== undefined"> · {{ row.elapsed_seconds }} 秒（含本地处理）</span></p>
      <template v-if="validFinalAction(row)">
        <table><thead><tr><th>假设分支</th><th>Jev 原始建议</th><th>规则检查后</th></tr></thead>
          <tbody><tr><th>未持仓</th><td>{{ finalActionLabel(row.suggested.unheld_action) }}</td><td>{{ finalActionLabel(row.effective.unheld_action) }}</td></tr>
          <tr><th>已持仓</th><td>{{ finalActionLabel(row.suggested.held_action) }}</td><td>{{ finalActionLabel(row.effective.held_action) }}</td></tr></tbody>
        </table>
        <p v-for="reason in row.guards" :key="reason">{{ finalReasonLabel(reason) }}</p>
        <p class="note">这里的持仓状态都是假设，不推断你的实际账户；原始建议被规则拦截时，不作为有效动作。</p>
      </template>
      <template v-else>
        <p>{{ row.status === 'running' ? '正在处理，可稍后刷新；原报告已完成。' : row.status === 'completed' ? '结果格式未通过，不作为有效动作。' : finalReasonLabel(row.reason) }}</p>
        <p v-for="reason in row.input_guards" :key="reason">{{ finalReasonLabel(reason) }}</p>
      </template>
    </template>
    <p class="note">明确动作不等于准确预测；不将模型置信度作为胜率。自动建议、旧核验和调试试验共享日调用预算。</p>
  </el-card>
</template>
<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { ApiClient } from '@/api/request'
import { finalActionLabel, finalReasonLabel, validFinalAction } from '@/utils/jevFinalAction'
const props = defineProps<{ analysisId: string }>()
const row = ref<any>(null), loading = ref(false), error = ref('')
let version = 0
async function load() {
  const current = ++version
  loading.value = true; row.value = null; error.value = ''
  try {
    const r = await ApiClient.get(`/api/jev-decisions/${encodeURIComponent(props.analysisId)}/final-action`, undefined, { skipErrorHandler: true, retryCount: 0 })
    if (current === version && r.success) row.value = r.data
  } catch (e: any) { if (current === version && e?.response?.status !== 404) error.value = '自动建议暂不可读；原报告不受影响。' }
  finally { if (current === version) loading.value = false }
}
watch(() => props.analysisId, load, { immediate: true })
onBeforeUnmount(() => { version++ })
</script>
<style scoped>
.final-action { margin-bottom: 24px; }
.heading { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
h3 { margin: 0; font-size: 16px; }
.note { color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.7; }
table { width: 100%; border-collapse: collapse; font-size: 14px; table-layout: fixed; }
th, td { border: 1px solid var(--el-border-color-lighter); padding: 10px; text-align: left; overflow-wrap: anywhere; }
th { background: var(--el-fill-color-light); }
</style>
