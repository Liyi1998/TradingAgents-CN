<template>
  <section class="experience-usage">
    <div class="experience-header"><strong>本次复盘经验参考</strong><el-button v-if="taskId" size="small" :loading="loading" @click="load">刷新记录</el-button></div>
    <p class="hint">记录提供给模型的历史经验；“明确引用”仅表示正文出现对应引用标识，不代表经验适用或结论正确。查看此处不调用模型。</p>
    <el-alert v-if="error" :title="error" type="warning" :closable="false" />
    <p v-else-if="!loading && !rows.length" class="hint">本报告暂无经验输入记录：可能生成于记录功能启用前，或尚未执行交易计划／每日综合复核。不能据此推断模型已使用经验。</p>
    <div v-for="receipt in rows" :key="receipt.id" class="receipt">
      <p><b>{{ receipt.kind === 'daily_research' ? '每日综合复核' : '专业交易员' }}</b> · 分析日期 {{ receipt.as_of }} · {{ receipt.reason }}<span v-if="receipt.reused_from"> · 沿用原请求经验</span></p>
      <p v-if="receipt.delivery !== 'completed'" class="hint">{{ receipt.delivery === 'submitted' ? '已进入模型请求，尚无完成回执；不代表调用成功。' : '仅准备了上下文，未确认进入模型请求。' }}</p>
      <details v-for="entry in receipt.entries" :key="entry.id" class="entry">
        <summary>{{ entry.symbol }} · {{ entry.decision_date }} 决策 · {{ entry.horizon_days }} 交易日复盘
          <el-tag size="small" :type="entry.explicitly_cited ? 'success' : 'info'">{{ entry.explicitly_cited ? '正文明确引用' : receipt.citation_status === 'checked' ? '已提供 · 未发现明确引用' : '引用情况未确认' }}</el-tag>
        </summary>
        <p class="hint">经验可用日 {{ entry.available_date }} · 保存的是该次输入正文，之后修改不会改写此记录。</p>
        <div class="markdown-body" v-html="renderMarkdown(entry.lesson)"></div>
        <router-link :to="{ name: 'ReportDetail', params: { id: entry.task_id } }">查看来源分析</router-link>
      </details>
    </div>
  </section>
</template>
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { experienceApi, type ExperienceReceipt } from '@/api/experience'
import { renderMarkdown } from '@/utils/markdown'
const props = defineProps<{ taskId?: string; receipts?: ExperienceReceipt[] }>()
const fetched = ref<ExperienceReceipt[]>([]), loading = ref(false), error = ref('')
const rows = computed(() => props.receipts ?? fetched.value)
let serial = 0
async function load() {
  const current = ++serial
  fetched.value = []; error.value = ''
  if (!props.taskId) { loading.value = false; return }
  loading.value = true
  try {
    const response = await experienceApi.usage(props.taskId)
    if (current !== serial) return
    if (!response.success) throw new Error('unavailable')
    fetched.value = response.data || []
  } catch { if (current === serial) error.value = '经验记录暂未读取成功，请重试；不代表没有使用经验。' }
  finally { if (current === serial) loading.value = false }
}
watch(() => props.taskId, load, { immediate: true })
</script>
<style scoped>
.experience-usage { margin: 20px 0; padding: 18px; border: 1px solid var(--el-border-color-light); border-radius: 8px; overflow-wrap: anywhere; }
.experience-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.hint { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.7; }
.receipt { border-top: 1px solid var(--el-border-color-lighter); margin-top: 14px; }
.entry { padding: 12px; margin-top: 10px; background: var(--el-fill-color-light); border-radius: 6px; }
summary { cursor: pointer; line-height: 1.9; }
summary .el-tag { margin-left: 8px; }
</style>
