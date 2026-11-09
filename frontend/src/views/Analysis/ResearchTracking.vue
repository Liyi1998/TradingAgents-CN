<template>
  <div class="research-page">
    <header class="page-heading"><div><h1>短线持续研究</h1><p>查看已建档股票，沿用完整报告并跟踪每日变化。</p></div><el-button :loading="loading" @click="loadTracks">刷新列表</el-button></header>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <el-card shadow="never" class="stock-picker">
      <div class="picker-row">
        <label for="research-stock">选择研究股票</label>
        <el-select id="research-stock" :model-value="selected || undefined" placeholder="选择已建档股票" aria-label="选择研究股票" @change="selectStock">
          <el-option v-for="stock in tracks" :key="stock.symbol" :value="stock.symbol" :label="`${stock.symbol} ${stock.name}`" />
        </el-select>
        <el-form class="open-stock" @submit.prevent="openStock">
          <el-input v-model="code" aria-label="A股六位代码" placeholder="输入其他 A 股六位代码" maxlength="6" clearable />
          <el-button native-type="submit" :disabled="!validCode(code.trim())">打开研究</el-button>
        </el-form>
      </div>
      <p v-if="current" class="stock-summary"><strong>{{ current.symbol }} {{ current.name }}</strong> · 起点报告 {{ current.baseline_date }} · {{ current.auto_enabled ? '每日自动更新已开启' : '每日自动更新未开启' }}</p>
      <p v-else class="stock-summary">可使用本账号已有的完整 A 股短线报告建档。打开页面和切换股票不调用模型。</p>
      <router-link v-if="selected" :to="{ name: 'StockDetail', params: { code: selected } }">查看个股详情与完整分析</router-link>
    </el-card>
    <ResearchTrackingBoard v-if="selected" :key="selected" :symbol="selected" @tracking-state="trackState" />
    <el-empty v-else description="选择一只已建档股票，或输入股票代码开始" />
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { researchTrackingApi, type TrackingStock } from '@/api/researchTracking'
import ResearchTrackingBoard from '@/components/ResearchTrackingBoard.vue'
const route = useRoute(), router = useRouter()
const tracks = ref<TrackingStock[]>([]), loading = ref(false), error = ref(''), code = ref('')
const validCode = (value: unknown): value is string => typeof value === 'string' && /^[0-9]{6}$/.test(value)
const selected = computed(() => validCode(route.query.symbol) ? route.query.symbol : '')
const current = computed(() => tracks.value.find(stock => stock.symbol === selected.value))
let alive = true, sequence = 0
const selectStock = (symbol: string) => validCode(symbol) && router.replace({ name: 'ResearchTracking', query: { symbol } })
function openStock() { const symbol = code.value.trim(); if (validCode(symbol)) { selectStock(symbol); code.value = '' } }
async function loadTracks() {
  const request = ++sequence; loading.value = true
  try {
    const response = await researchTrackingApi.list()
    if (!alive || request !== sequence) return
    tracks.value = response.data; error.value = ''
    if (!selected.value && tracks.value.length) await selectStock(tracks.value[0].symbol)
  } catch { if (alive && request === sequence) error.value = '研究列表加载失败，请重试' }
  finally { if (alive && request === sequence) loading.value = false }
}
function trackState(active: boolean) { if (active && !loading.value) void loadTracks() }
onMounted(loadTracks)
onBeforeUnmount(() => { alive = false; sequence++ })
</script>

<style scoped lang="scss">
.research-page { max-width: 1400px; margin: 0 auto; }
.page-heading { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 20px; h1 { font-size: 24px; margin: 0 0 8px; } p { margin: 0; color: var(--el-text-color-secondary); } }
.stock-picker { margin-bottom: 24px; }
.picker-row { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; .el-select { width: 250px; } }
.open-stock { display: flex; gap: 8px; margin-left: auto; .el-input { width: 220px; } }
.stock-summary { color: var(--el-text-color-regular); line-height: 1.7; }
@media (max-width: 640px) { .page-heading { align-items: flex-start; } .picker-row { align-items: stretch; flex-direction: column; .el-select { width: 100%; } } .open-stock { margin-left: 0; .el-input { width: auto; flex: 1; } } }
</style>
