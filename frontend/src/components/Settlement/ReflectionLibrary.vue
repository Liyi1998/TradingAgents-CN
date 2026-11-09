<template>
  <section v-loading="loading" class="reflection-library">
    <p>复盘在评估窗口结束并完成结算后生成。正文入库后，后续同一用户、同一评估周期的分析会检索相关经验；每日研究使用短线经验。历史日期分析不能读取当时尚不可用的经验。</p>
    <div class="filters"><el-input v-model="symbol" placeholder="按股票代码筛选" clearable aria-label="复盘股票代码" @keyup.enter="query" /><el-button @click="query">查询／刷新</el-button></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-alert v-else-if="!enabled" title="自动复盘目前关闭；已保存正文仍可查看。" type="info" :closable="false" />
    <el-empty v-if="!loading && !error && !items.length" description="暂无本用户的结算记录；生成分析后须等待对应评估窗口结束，不会立即产生复盘。" :image-size="60" />
    <article v-for="row in items" :key="row.id" class="reflection">
      <header><strong>{{ row.symbol }} {{ row.stock_name }} · {{ row.decision_date }}</strong><el-tag :type="row.reflection_status === 'done' ? 'success' : 'info'">{{ reflectionLabel(row) }}</el-tag></header>
      <p class="meta">评估窗口 {{ row.horizon_days }} 交易日 · 原信号 {{ row.signal }}<span v-if="row.status === 'settled' && row.exit_date"> · 结算结束日 {{ row.exit_date }}</span><span v-if="row.reflection_available_date"> · 经验可用日 {{ row.reflection_available_date }}</span></p>
      <details v-if="row.reflection_lesson"><summary>查看复盘正文</summary><div class="markdown-body" v-html="renderMarkdown(row.reflection_lesson)"></div></details>
      <p v-else class="meta">{{ reflectionLabel(row) }}，当前没有可展示的复盘正文。不会用占位内容替代经验。</p>
      <router-link :to="{ name: 'ReportDetail', params: { id: row.task_id } }">查看来源分析与本次经验记录</router-link>
    </article>
    <el-pagination v-if="total" v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next" @current-change="load" />
  </section>
</template>
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { experienceApi, type ReflectionRecord } from '@/api/experience'
import { reflectionLabel } from '@/utils/experience'
import { renderMarkdown } from '@/utils/markdown'
const items = ref<ReflectionRecord[]>([]), total = ref(0), page = ref(1), symbol = ref('')
const loading = ref(false), error = ref(''), enabled = ref(true)
let serial = 0
async function load() {
  const current = ++serial; loading.value = true; error.value = ''
  try {
    const response = await experienceApi.reflections(symbol.value.trim() || undefined, page.value)
    if (current !== serial) return
    if (!response.success) throw new Error('unavailable')
    items.value = response.data.items; total.value = response.data.total; enabled.value = response.data.enabled
  } catch { if (current === serial) error.value = '复盘记录读取失败，请重试。' }
  finally { if (current === serial) loading.value = false }
}
function query() { page.value = 1; load() }
onMounted(load)
</script>
<style scoped>
.reflection-library { line-height: 1.8; overflow-wrap: anywhere; }
.filters, .reflection header { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.filters .el-input { width: min(100%, 220px); }
.reflection { border: 1px solid var(--el-border-color-light); border-radius: 8px; padding: 18px; margin: 16px 0; }
.meta { color: var(--el-text-color-secondary); font-size: 12px; }
summary { cursor: pointer; color: var(--el-color-primary); }
</style>
