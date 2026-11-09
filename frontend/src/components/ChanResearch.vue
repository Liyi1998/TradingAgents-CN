<template>
  <section class="chan-research" aria-label="缠论日线结构">
    <div class="chan-heading"><strong>缠论 · 日线结构</strong><span>确定性计算 · 不调用模型</span></div>
    <p v-if="!value">{{ historical ? '这个历史版本尚未保存缠论结构，未用后来的行情补画。切换到最新行情可查看当前计算。' : '缠论结构随行情加载。' }}</p>
    <template v-else>
      <p :class="{ warning: value.status !== 'ready' }">{{ value.notice || `结构截至 ${value.as_of}` }}</p>
      <div class="chan-controls">
        <el-checkbox v-for="option in options" :key="option.key" :model-value="modelValue[option.key]" :disabled="value.status === 'unavailable'" @change="toggle(option.key, Boolean($event))">{{ option.label }}</el-checkbox>
      </div>
      <template v-if="value.status !== 'unavailable'">
        <p class="chan-legend"><i class="confirmed"></i>实线／蓝色：算法已确认 <i class="candidate"></i>虚线／金色：候选，可变化。{{ value.preview ? `盘中候选截至 ${reportTime(value.preview.observed_at)}` : '未完成笔按最后完整日线绘制' }}。</p>
        <details>
          <summary>计算依据与确认时间</summary>
          <p>{{ value.window_start }} 至 {{ value.as_of }} · {{ value.bar_count }} 根 · {{ value.data_source }} · CNY / 股 · 不复权。算法 CZSC 1.0.2，最小笔长 6。</p>
          <p>“确认于”是逐日计算首次形成当前结构的日期，不是拐点当天；后续行情、数据修订或窗口移动仍可能改变结构。历史研究快照保持原样。</p>
          <ul><li v-for="note in value.limitations" :key="note">{{ note }}</li></ul>
          <div class="chan-table" v-if="recent.length"><table><thead><tr><th>结构</th><th>区间／日期</th><th>状态</th><th>确认于</th></tr></thead><tbody><tr v-for="row in recent" :key="row.id"><td>{{ row.label }}</td><td>{{ row.date || `${row.start} → ${row.end}` }}</td><td>{{ row.status === 'confirmed' ? '算法确认' : '候选' }}</td><td>{{ row.confirmed_at || '尚未确认' }}</td></tr></tbody></table></div>
        </details>
      </template>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { ChanSnapshot } from '@/api/researchTracking'
import { reportTime } from '@/utils/trackingBaseline'
type Layers = { fractals: boolean; strokes: boolean; zones: boolean; candidates: boolean }
const props = defineProps<{ value?: ChanSnapshot; historical: boolean; modelValue: Layers }>()
const emit = defineEmits<{ (event: 'update:modelValue', value: Layers): void }>()
const options: { key: keyof Layers; label: string }[] = [{ key: 'fractals', label: '分型' }, { key: 'strokes', label: '笔' }, { key: 'zones', label: '笔中枢' }, { key: 'candidates', label: '显示候选' }]
const toggle = (key: keyof Layers, value: boolean) => emit('update:modelValue', { ...props.modelValue, [key]: value })
const recent = computed(() => [
  ...(props.value?.strokes.slice(-3) || []).map(x => ({ ...x, label: x.unfinished ? '未完成笔' : '笔' })),
  ...(props.value?.zones.slice(-2) || []).map(x => ({ ...x, label: '笔中枢' }))])
</script>

<style scoped>
.chan-research { border: 1px solid var(--el-border-color-light); border-radius: 8px; padding: 14px 16px; margin: 12px 0; font-size: 12px; line-height: 1.8; }
.chan-heading, .chan-controls { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 20px; }.chan-heading strong { font-size: 14px; }.chan-heading span, p { color: var(--el-text-color-secondary); }p { margin: 8px 0; }.chan-controls :deep(.el-checkbox) { margin-right: 0; }.warning { color: var(--el-color-warning-dark-2); }
.chan-legend i { display: inline-block; width: 20px; vertical-align: middle; margin: 0 5px; }.confirmed { border-top: 2px solid #4568a2; }.candidate { border-top: 2px dashed #ba9844; }summary { cursor: pointer; color: var(--el-color-primary); }.chan-table { overflow-x: auto; }table { border-collapse: collapse; width: 100%; min-width: 420px; }th, td { text-align: left; padding: 7px; border-bottom: 1px solid var(--el-border-color-lighter); }ul { padding-left: 20px; }
@media (max-width: 640px) { .chan-research { padding: 12px; }.chan-controls { gap: 0 14px; } }
</style>
