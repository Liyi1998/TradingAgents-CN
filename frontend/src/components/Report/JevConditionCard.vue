<template>
  <el-card class="jev-condition-card" shadow="never">
    <template #header>
      <div class="card-heading">
        <h3>Jev 条件核验 <span>（仅供对照）</span></h3>
        <el-button size="small" :loading="loading" :disabled="!analysisId" @click="load">刷新记录</el-button>
      </div>
    </template>
    <p class="scope-note">独立核验报告生成时的文本证据，不获取当前行情，不修改价格、仓位或买卖授权。刷新只读取记录，不调用模型。</p>
    <div v-if="loading" role="status" aria-label="正在读取 Jev 核验记录"><el-skeleton :rows="3" animated /></div>
    <el-alert v-else-if="error" :title="error" type="warning" :closable="false" show-icon />
    <JevConditionResult v-else-if="record" :record="record" />
    <div v-else class="empty-state" role="status">
      <h4>暂无可访问的核验记录</h4>
      <p>可能是报告生成时未启用、后台尚未记录，或记录不可访问；不能据此判断 Jev 已通过或未通过。</p>
      <p>旧报告不会自动补跑。新报告完成后可稍候刷新记录。</p>
    </div>
    <p class="limitation">条件满足不等于可以买入；未发现冲突不等于事实已核实。核验只供对照，不代表收益预测或投资胜率。</p>
  </el-card>
</template>

<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { reportsApi } from '@/api/reports'
import JevConditionResult from './JevConditionResult.vue'

const props = defineProps<{ analysisId: string }>()
const loading = ref(false)
const error = ref('')
const record = ref<Record<string, unknown> | null>(null)
let version = 0
let controller: AbortController | undefined

async function load() {
  const current = ++version
  controller?.abort()
  controller = new AbortController()
  error.value = ''
  record.value = null
  loading.value = Boolean(props.analysisId)
  if (!props.analysisId) return
  try {
    const value = await reportsApi.conditionCheck(props.analysisId, controller.signal)
    if (current === version) record.value = value
  } catch {
    if (current === version) error.value = '核验记录读取失败，请稍后点击“刷新记录”。原分析报告仍可正常查看。'
  } finally {
    if (current === version) loading.value = false
  }
}
watch(() => props.analysisId, load, { immediate: true })
onBeforeUnmount(() => { version++; controller?.abort() })
</script>

<style scoped>
.jev-condition-card { margin-bottom: 24px; }
.card-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
h3 { margin: 0; font-size: 16px; color: var(--el-text-color-primary); }
h3 span { font-size: 13px; font-weight: normal; color: var(--el-text-color-secondary); }
.scope-note, .limitation, .empty-state p { font-size: 13px; line-height: 1.7; color: var(--el-text-color-regular); }
.scope-note { margin: 0 0 16px; }
.limitation { color: var(--el-text-color-secondary); margin: 16px 0 0; }
.empty-state { padding: 16px; border: 1px dashed var(--el-border-color); border-radius: 8px; background: var(--el-fill-color-light); }
.empty-state h4 { margin: 0; font-size: 14px; color: var(--el-text-color-primary); }
.empty-state p { margin: 8px 0 0; }
</style>
