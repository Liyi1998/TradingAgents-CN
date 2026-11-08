<template>
  <section class="intraday-panel" aria-label="盘中条件跟踪">
    <div class="heading"><strong>盘中条件跟踪</strong><span>已完成 30 分钟 K 线 · 不调用模型</span>
      <el-switch :model-value="enabled" :disabled="busy || historical" aria-label="盘中自动跟踪" active-text="自动跟踪" @change="emit('toggle', Boolean($event))" />
    </div>
    <p v-if="historical">正在查看历史研究。当前盘中判断未叠加到这个版本，原判断与原日期保持不变。</p>
    <p v-else-if="!enabled">盘中跟踪已关闭。每日 19:00 研究和原点位不受影响。</p>
    <template v-else>
      <div class="toolbar"><el-button size="small" :loading="loading" @click="load">刷新观察</el-button>
      </div>
      <p>跟踪原报告的条件是否有新变化；后台自动检查，页面可见时每分钟读取。原报告点位与日期保留。</p>
      <el-alert v-if="error" :title="error" type="warning" :closable="false" />
      <template v-if="value?.snapshot">
        <div v-if="value.alerts?.length" class="intraday-alerts" aria-live="polite" aria-label="盘中关键变化提醒">
          <el-alert v-for="alert in value.alerts" :key="alert.id" :title="alert.title + ' · ' + alert.label" type="warning" :closable="false" show-icon>
            <p>{{ alert.lower }}–{{ alert.upper }} 元 · 完整K线截至 {{ alert.evidence_at }}</p>
            <p>{{ alert.message }}</p>
            <details><summary>原条件</summary>{{ alert.condition }}</details>
          </el-alert>
        </div>
        <p class="meta">基准研究：{{ value.source_date }} · 分钟线截至 {{ value.snapshot.as_of || '尚未取得' }}（北京时间） · 来源 {{ value.snapshot.source || '未知' }} · 不复权</p>
        <p class="meta">检查于 {{ reportTime(value.snapshot.checked_at) }}。截至时点是完整分钟线的结束时间，并非实时成交价。</p>
        <el-alert v-for="note in value.snapshot.issues" :key="note" :title="note" type="warning" :closable="false" />
        <p v-if="value.volume_ratio_previous_two_bars != null">本根成交量 / 前两根均量：<strong>{{ value.volume_ratio_previous_two_bars.toFixed(2) }} 倍</strong>（相邻时段对比）</p>
        <p v-if="value.wyckoff_candidate" class="candidate">结构变化候选：{{ value.wyckoff_candidate }}。日线威科夫阶段仍以盘后确认版本为准。</p>
        <div class="coverage" aria-label="条件核验能力">
          <strong>原策略共 {{ counts.total }} 条</strong>
          <span>已核验 {{ counts.supported }}</span><span>缺数据 {{ counts.missing }}</span>
          <span>暂不支持完整核验 {{ counts.unsupported }}</span><span>等待时点 {{ counts.waiting }}</span>
        </div>
        <p class="meta">“缺数据”表示规则可计算但缺指定证据；“暂不支持”表示完整语义尚不能自动解释。二者都不等于条件已通过。</p>
        <p v-if="!strategyRows.length">当前研究没有独立策略条件，可展开下方的原报告价格观察。不会为凑齐条件自动生成新规则。</p>
        <section v-for="group in groups.filter(group => strategyRows.some(row => row.kind === group.kind))" :key="group.kind" class="strategy-group" :aria-label="group.name">
          <h4>{{ group.name }}</h4>
          <div class="condition-grid">
            <article v-for="item in strategyRows.filter(row => row.kind === group.kind)" :key="item.id">
              <div class="item-title"><strong>{{ item.label }}</strong><el-tag size="small" :type="tagType(item.state)">{{ stateLabel(item.state) }}</el-tag></div>
              <p class="original-condition">{{ item.condition }}</p>
              <p class="meta">原研究 {{ item.source_date || value.source_date }}：{{ previousLabel(item.previous_state) }} · 以下为本次核验</p>
              <p>{{ item.reason }}</p>
              <ul class="checks" v-if="item.checks?.some(check => check.state !== 'unsupported')">
                <li v-for="(check, index) in item.checks.filter(check => check.state !== 'unsupported')" :key="index">
                  <div><span class="check-state">{{ check.partial ? '仅子条件 · ' : '' }}{{ stateLabel(check.state) }}</span> {{ check.text }}</div>
                  <p>{{ check.reason }}</p><small v-if="check.source">依据：{{ check.source }} · {{ check.as_of }}</small>
                </li>
              </ul>
              <details v-if="item.checks?.some(check => check.state === 'unsupported')" class="unverified-reasons">
                <summary>其他前提及未核验原因（{{ item.checks.filter(check => check.state === 'unsupported').length }}）</summary>
                <ul class="checks"><li v-for="(check, index) in item.checks.filter(check => check.state === 'unsupported')" :key="index">
                  <div>{{ check.text }}</div><p>{{ check.reason }}</p>
                </li></ul>
              </details>
            </article>
          </div>
        </section>
        <el-alert v-if="breaches.length" :title="`${breaches.length} 处最新收盘已低于原保护参考，请查看原退出条件`" type="warning" :closable="false" />
        <details class="reference-section" :open="breaches.length > 0">
          <summary>原报告价格观察 · {{ levelRows.length }} 项 · 今日已完成时段曾触及 {{ dailyTouches }} 项</summary>
          <p>“最新时段”指最后一根完整30分钟K线；与“今天是否曾触及”分开。两者都只核验价格，其他前提仍需满足。</p>
          <div class="condition-grid">
            <article v-for="item in levelRows" :key="item.id">
              <div class="item-title"><strong>{{ item.label }}</strong><el-tag size="small" :type="tagType(item.state)">{{ stateLabel(item.state) }}</el-tag></div>
              <p><strong>{{ item.lower }}{{ item.upper !== item.lower ? '–' + item.upper : '' }} 元</strong> · 原日期 {{ item.source_date || value.source_date }}</p>
              <p class="original-condition">{{ item.condition }}</p>
              <p>{{ item.reason }}</p>
              <p v-if="item.day_touched != null">今日完整时段：{{ item.day_touched ? '曾触及；首次所在K线结束于 ' + item.first_touch_at : '尚未触及' }}</p>
              <small v-if="item.interval_end">最新完整时段结束于 {{ item.interval_end }}，收盘 {{ item.latest_close }} 元</small>
            </article>
          </div>
        </details>
        <details class="reference-section" aria-label="盘中结构辅助参考">
          <summary>盘中结构参考（辅助） · 开盘范围与30分钟局部高低点</summary>
          <p>开盘范围是 09:30—10:00 的最低至最高价，收完后固定，用来对照后续价格是否走出早盘范围。它不是买入区间，也不单独触发策略。</p>
          <p v-if="value.opening_reference"><strong>最新完整30分钟收盘 {{ value.opening_reference.latest_close }} 元：{{ value.opening_reference.position }}</strong>（{{ value.opening_reference.lower }}–{{ value.opening_reference.upper }} 元）</p>
          <p>局部高低点来自连续三根30分钟K线：中间高点高于两侧，或低点低于两侧；等右侧收盘并通过发布等待期后才显示。每完成一根重新检查，已有点位保留原价，新增拐点另记，展示最近4个；不会随每笔报价移动。</p>
          <p>局部参考尚未验证多次回踩或量能强度，不替代原支撑、阻力、止损及威科夫阶段。</p>
          <el-checkbox v-model="showZones" :disabled="value?.snapshot?.status !== 'ready'">图上显示盘中候选区间</el-checkbox>
          <div class="zones" v-if="value.zones.length">
            <article v-for="zone in value.zones" :key="zone.id"><strong>{{ zone.label }}</strong><p>{{ zone.lower }}–{{ zone.upper }} 元</p><small>证据收盘于 {{ zone.observed_at }} · 发布等待期后核验 · 仅当日候选</small></article>
          </div>
          <p v-else>暂无通过校验的盘中结构参考。</p>
        </details>
        <details class="history"><summary>最近观察与状态变化（保留 30 天）</summary>
          <p v-for="row in value.history" :key="row.id">{{ reportTime(row.created_at) }} · 基准 {{ row.source_date }} · {{ row.as_of || '无有效分钟时点' }} · {{ row.status === 'ready' ? '分钟证据齐全' : '证据不足' }}
            <span v-for="(change, index) in row.changes" :key="index" class="change">{{ change.label }}：{{ stateLabel(change.from) }} → {{ stateLabel(change.to) }}</span>
          </p>
        </details>
        <details><summary>判断范围与限制</summary><ul><li v-for="note in value.limitations" :key="note">{{ note }}</li></ul></details>
      </template>
      <p class="footer-note">这里只核验列出的规则，不生成交易授权。未核验账户可卖数量，不假定当日新买股份可卖。未调用模型。</p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { researchTrackingApi, type IntradayResearchValue, type TrackingLevel } from '@/api/researchTracking'
import { reportTime } from '@/utils/trackingBaseline'
import { conditionCounts, intradayLevels, intradayStateLabel as stateLabel } from '@/utils/intradayResearch'
const props = defineProps<{ trackId: string; baselineId: string; enabled: boolean; historical: boolean; busy: boolean }>()
const emit = defineEmits<{ (e: 'toggle', enabled: boolean): void; (e: 'levels', levels: TrackingLevel[]): void }>()
const value = ref<IntradayResearchValue | null>(null), error = ref(''), loading = ref(false), showZones = ref(false)
const strategyRows = computed(() => (value.value?.conditions || []).filter(row => row.category === 'strategy'))
const levelRows = computed(() => (value.value?.conditions || []).filter(row => row.category === 'level'))
const counts = computed(() => conditionCounts(value.value?.conditions))
const dailyTouches = computed(() => levelRows.value.filter(row => row.day_touched).length)
const breaches = computed(() => levelRows.value.filter(row => row.state === 'breach_candidate'))
const groups = [{ kind: 'entry', name: '入场需要满足什么' }, { kind: 'exit', name: '减仓或退出需要关注什么' }, { kind: 'veto', name: '哪些情况会否决原策略' }]
function tagType(state: string) { return ['invalidated', 'expired', 'breach_candidate'].includes(state) ? 'danger' : state === 'met' ? 'success' : state === 'missing_data' ? 'warning' : 'info' }
const previousLabel = (state?: string) => ({ met: '满足', not_met: '未满足', unknown: '未知', invalidated: '失效' }[state || ''] || '未记录')
let alive = true, generation = 0, timer: ReturnType<typeof setTimeout> | undefined
async function load() {
  if (!alive || !props.enabled || props.historical || document.hidden || loading.value) return
  clearTimeout(timer)
  const request = ++generation
  loading.value = true
  try {
    const response = await researchTrackingApi.intraday(props.trackId)
    if (request !== generation || !alive) return
    if (!response.data.enabled) throw new Error('盘中跟踪已关闭，请刷新研究记录同步设置')
    if (response.data.changed_basis || response.data.baseline_analysis_id !== props.baselineId) throw new Error('研究起点已改变，请刷新研究记录')
    value.value = response.data; error.value = ''
  } catch (e: any) {
    if (request !== generation || !alive) return
    value.value = null; error.value = e?.response?.data?.detail || e?.message || '盘中观察暂未获取成功，原研究保持不变'
  } finally {
    if (alive && request === generation) { loading.value = false; timer = setTimeout(load, 60000) }
  }
}
function reset() { generation++; clearTimeout(timer); loading.value = false; value.value = null; error.value = ''; emit('levels', []); void load() }
function visibility() { clearTimeout(timer); if (!document.hidden) void load() }
watch(() => [props.trackId, props.baselineId, props.enabled, props.historical], reset, { immediate: true })
watch(() => [value.value, showZones.value, props.historical] as const, () => emit('levels', showZones.value && !props.historical ? intradayLevels(value.value) : []))
onMounted(() => document.addEventListener('visibilitychange', visibility))
onBeforeUnmount(() => { alive = false; generation++; clearTimeout(timer); document.removeEventListener('visibilitychange', visibility); emit('levels', []) })
</script>

<style scoped>
.intraday-panel { margin: 14px 0; padding: 16px; border: 1px solid var(--el-border-color-light); border-radius: 8px; font-size: 12px; line-height: 1.8; }
.heading, .toolbar, .item-title { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 14px; }.heading strong { font-size: 14px; }.heading span, .meta, .footer-note { color: var(--el-text-color-secondary); }.heading :deep(.el-switch) { margin-left: auto; }.toolbar { margin-top: 12px; }p { margin: 8px 0; }
.condition-grid, .zones { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 270px), 1fr)); gap: 10px; margin: 12px 0; }article { padding: 12px; border: 1px solid var(--el-border-color-lighter); border-radius: 6px; overflow-wrap: anywhere; }.zones article { border-left: 3px dashed #397f8c; }.item-title { justify-content: space-between; }summary { cursor: pointer; color: var(--el-color-primary); }.candidate { color: var(--el-color-warning-dark-2); }.change { display: block; }.history { margin: 12px 0; }.el-alert { margin: 6px 0; }ul { padding-left: 20px; }
.strategy-group .condition-grid { grid-template-columns: repeat(auto-fit, minmax(min(100%, 430px), 1fr)); }
.strategy-group h4 { margin: 18px 0 8px; font-size: 14px; }.original-condition { font-size: 13px; color: var(--el-text-color-primary); }
.coverage { display: flex; flex-wrap: wrap; gap: 8px 18px; background: var(--el-fill-color-light); padding: 10px 12px; border-radius: 6px; }
.checks { margin: 10px 0 0; list-style: none; padding: 0; }.checks li { border-top: 1px solid var(--el-border-color-lighter); padding: 8px 0; }.checks p { margin: 2px 0; color: var(--el-text-color-secondary); }.check-state { font-weight: 600; }
.reference-section { border-top: 1px solid var(--el-border-color-lighter); margin: 16px 0 0; padding-top: 12px; }
@media (max-width: 640px) { .intraday-panel { padding: 12px; }.heading :deep(.el-switch) { margin-left: 0; }.condition-grid { grid-template-columns: 1fr; } }
</style>
