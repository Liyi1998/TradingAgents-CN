<template>
  <section class="structure-panel" aria-label="结构辅助研究">
    <div class="heading">
      <strong>结构辅助</strong>
      <span>日线威科夫背景 · 30分钟 TPO 近似 · 30分钟 SMC</span>
      <el-switch :model-value="enabled" :disabled="busy || historical" aria-label="结构辅助" active-text="开启" @change="emit('toggle', Boolean($event))" />
    </div>
    <p v-if="historical">正在查看历史研究。结构辅助不叠加到这个版本，原裁决和威科夫结论保持不变。</p>
    <p v-else-if="!enabled">结构辅助默认关闭。打开后只增加旁路证据，不改历史报告、HOLD/SELL 或已保存的威科夫阶段。</p>
    <template v-else>
      <div class="toolbar">
        <el-button size="small" :loading="loading" @click="load">刷新结构</el-button>
        <el-checkbox v-model="showLayers" :disabled="!value">图上显示接受区与结构</el-checkbox>
      </div>
      <el-alert v-if="error" :title="error" type="warning" :closable="false" />
      <template v-if="value">
        <p class="meta">分钟截至 {{ value.as_of || '没有完整K线' }} · 来源 {{ value.source || '未取得' }} · 不复权 · 不调用模型</p>
        <p class="meta">{{ value.tpo?.label }}。三者共享本标的价格身份，冲突保留，不投票。</p>
        <div class="cards">
          <article>
            <h3>日线威科夫背景</h3>
            <p v-if="value.wyckoff_background?.status === 'stored'">阶段 {{ value.wyckoff_background.phase }} · 趋势 {{ value.wyckoff_background.trend }} · 研究日 {{ value.wyckoff_background.as_of || '未知' }}</p>
            <p v-else>不可判断：{{ value.wyckoff_background?.reason }}</p>
            <small>原结论未改写。持有动作 {{ value.preserved?.held_action || '未记录' }}，未持有动作 {{ value.preserved?.unheld_action || '未记录' }}。</small>
          </article>
          <article>
            <h3>30分钟接受区</h3>
            <p v-if="latestTpo">{{ latestTpo.date }} VAL {{ latestTpo.val }} · VAH {{ latestTpo.vah }}</p>
            <p v-else>不可判断：{{ value.tpo?.days?.[0]?.reason || '没有完整交易日轮廓' }}</p>
            <small>按交易日统计。未完成、午休、缺失和除权断点都不填平。</small>
          </article>
          <article>
            <h3>30分钟结构</h3>
            <p>状态 {{ value.smc?.state || 'undetermined' }}<template v-if="value.smc?.latest"> · {{ value.smc.latest.kind }} {{ value.smc.latest.status }}</template></p>
            <p v-if="value.smc?.latest">事件 {{ value.smc.latest.event_time || '—' }}，首次可知 {{ value.smc.latest.first_knowable_time }}</p>
            <small>{{ value.smc?.order_blocks?.reason }}</small>
          </article>
        </div>
        <div class="gates" aria-label="数据门槛">
          <span v-for="gate in value.gates || []" :key="gate.name">{{ gate.name }}：{{ gate.status === 'ready' ? '可核验' : '不可判断' }}</span>
        </div>
        <el-alert v-for="note in value.issues || []" :key="note" :title="note" type="warning" :closable="false" />
        <section v-if="value.conflicts?.length" aria-label="保留的冲突">
          <h3>保留的冲突</h3>
          <article v-for="(item, index) in value.conflicts" :key="index">
            <p>{{ item.left }}（{{ reading(item.left_reading) }}）与 {{ item.right }}（{{ reading(item.right_reading) }}）不一致。</p>
            <small>{{ item.resolution }}</small>
          </article>
        </section>
        <section aria-label="相对表现">
          <h3>相对表现</h3>
          <div class="cards">
            <article><h3>个股</h3><p>{{ leg(value.relative?.stock) }}</p><small>时点 {{ value.relative?.stock?.as_of || '无' }}</small></article>
            <article><h3>指数</h3><p>{{ leg(value.relative?.index) }}</p><small>{{ value.relative?.index?.symbol || '000300.SH' }} · {{ value.relative?.index?.as_of || '无' }}</small></article>
            <article><h3>板块</h3><p>{{ leg(value.relative?.sector) }}</p><small>时点 {{ value.relative?.sector?.as_of || '无' }}</small></article>
          </div>
          <p>{{ value.relative?.attribution }}</p>
        </section>
        <details>
          <summary>隔夜价差与限制</summary>
          <p v-for="gap in value.overnight_gaps || []" :key="gap.id">{{ gap.event_time }} {{ gap.status === 'confirmed' ? '相邻交易日价差' : '不可判断' }}：{{ gap.reason }}</p>
          <ul><li v-for="note in value.limitations || []" :key="note">{{ note }}</li></ul>
        </details>
      </template>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { researchTrackingApi } from '@/api/researchTracking'
import { structureLevels, type StructureAuxiliaryValue } from '@/utils/structureAuxiliary'
import type { TrackingLevel } from '@/api/researchTracking'

const props = defineProps<{ trackId: string; baselineId: string; enabled: boolean; historical: boolean; busy: boolean }>()
const emit = defineEmits<{ (e: 'toggle', enabled: boolean): void; (e: 'levels', levels: TrackingLevel[]): void }>()
const value = ref<StructureAuxiliaryValue | null>(null)
const error = ref('')
const loading = ref(false)
const showLayers = ref(false)
const latestTpo = computed(() => [...(value.value?.tpo?.days || [])].reverse().find(item => item.status === 'ready'))
const reading = (valueText: string) => ({ bullish: '偏多', bearish: '偏空' }[valueText] || valueText)
const leg = (item?: { status: string; reason: string }) => item ? (item.status === 'ready' ? '已对齐收盘变化，见限制' : `不可判断：${item.reason}`) : '不可判断'
let generation = 0
async function load() {
  if (!props.enabled || props.historical) return
  const request = ++generation
  loading.value = true
  try {
    const response = await researchTrackingApi.structureAuxiliary(props.trackId)
    if (request !== generation) return
    if (!response.data.enabled) throw new Error('结构辅助已关闭，请刷新研究记录')
    if (response.data.baseline_analysis_id && response.data.baseline_analysis_id !== props.baselineId) throw new Error('研究起点已改变，请刷新研究记录')
    value.value = response.data
    error.value = ''
  } catch (e: any) {
    if (request !== generation) return
    value.value = null
    error.value = e?.response?.data?.detail || e?.message || '结构辅助暂未取得，原研究保持不变'
  } finally {
    if (request === generation) loading.value = false
  }
}
function reset() {
  generation += 1
  loading.value = false
  value.value = null
  error.value = ''
  emit('levels', [])
  void load()
}
watch(() => [props.trackId, props.baselineId, props.enabled, props.historical], reset, { immediate: true })
watch(() => [value.value, showLayers.value, props.historical] as const, () => {
  emit('levels', showLayers.value && !props.historical ? structureLevels(value.value, true) : [])
})
</script>

<style scoped>
.structure-panel { margin: 14px 0; padding: 16px; border: 1px solid var(--el-border-color-light); border-radius: 8px; font-size: 12px; line-height: 1.7; }
.heading, .toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 14px; }
.heading strong { font-size: 14px; }
.heading span, .meta, small { color: var(--el-text-color-secondary); }
.heading :deep(.el-switch) { margin-left: auto; }
.cards, .gates { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr)); gap: 10px; margin: 12px 0; }
article, .gates span { padding: 12px; border: 1px solid var(--el-border-color-lighter); border-radius: 6px; overflow-wrap: anywhere; }
h3 { margin: 0 0 6px; font-size: 13px; }
p { margin: 6px 0; }
.el-alert { margin: 6px 0; }
@media (max-width: 640px) {
  .structure-panel { padding: 12px; }
  .heading :deep(.el-switch) { margin-left: 0; }
  .cards, .gates { grid-template-columns: 1fr; }
}
</style>
