<template>
  <el-card class="tracking-board" shadow="never" v-loading="loading && !board">
    <template #header>
      <div class="tracking-heading">
        <div><strong>短线持续研究</strong><span class="subtitle">日线 · 条件点位 · 每日版本</span></div>
        <el-button v-if="board?.track" text :disabled="loading" @click="load(selectedVersion || undefined, selectedBaseline || undefined)">刷新记录</el-button>
      </div>
    </template>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <div v-if="!board?.track" class="start-tracking">
      <div><p>把已有短线报告加入持续跟踪</p><span>以最近一份完整报告为起点，按变化更新研究，并在 K 线上保留每个版本的条件点位。</span></div>
      <el-button type="primary" :loading="busy" @click="create">建立研究档案</el-button>
    </div>
    <template v-else>
      <el-alert v-if="board.newer_report" type="info" :closable="false" show-icon class="new-report-alert" title="发现更新的完整短线报告">
        <p>报告日期 {{ board.newer_report.as_of }} · 生成于 {{ reportTime(board.newer_report.created_at) }}。当前研究仍以 {{ board.track.baseline_date }} 的报告为起点。</p>
        <p>切换会保留旧起点和每日版本，不调用模型；后续更新使用新报告，自动更新设置保持不变。</p>
        <div class="toolbar-group"><router-link :to="{ name: 'ReportDetail', params: { id: board.newer_report.analysis_id } }">查看新报告</router-link><el-button type="primary" :disabled="busy || running" @click="switchBaseline">切换到这份报告</el-button></div>
      </el-alert>
      <div class="tracking-toolbar">
        <div class="toolbar-group"><span>每日 19:00 后更新</span><el-switch :model-value="board.track.settings.auto_enabled" :disabled="busy" aria-label="每日自动更新研究" @change="toggleAuto" /></div>
        <el-button type="primary" :loading="busy || running" :disabled="historical || availability.blocked" @click="update(false)">更新今日研究</el-button>
        <el-button v-if="canRetry" :disabled="busy || running || historical || availability.blocked" @click="update(true)">继续未完成部分</el-button>
      </div>
      <p class="tracking-note">更新研究会按需调用当前分析／辩论模型。不设每日调用次数上限，按实际请求记录用量。已完成模块会复用；刷新记录和切换历史版本不调用模型。</p>
      <p v-if="board.budget" class="tracking-note" aria-live="polite">{{ board.budget.message }}</p>
      <p v-if="availability.cooldown > 0" class="tracking-note">上次更新刚结束，稍后自动恢复继续按钮，避免连续失败消耗。已完成内容保留。</p>
      <el-alert v-if="availability.waiting && !historical" title="当前研究起点较新，需等对应交易日收盘日线到齐后再更新。行情和盘中 K 线可正常刷新，无需反复继续。" type="info" :closable="false" />
      <el-alert v-if="board.run && !historical" :title="board.run.message" :type="runType" :closable="false" show-icon class="run-alert">
        <template #default>本次模型调用 {{ board.run.model_calls }} 次<span v-if="running"> · 可以离开页面，后台继续执行</span></template>
      </el-alert>
      <div class="version-toolbar">
        <span>当前研究起点：<router-link :to="{ name: 'ReportDetail', params: { id: board.track.baseline_analysis_id } }">{{ board.track.baseline_date }} 的完整短线报告</router-link></span>
      </div>
      <div class="version-toolbar history-toolbar">
        <div class="toolbar-group"><span>查看研究起点</span><el-select v-model="selectedBaseline" aria-label="查看研究起点" :disabled="loading || busy" @change="selectBaseline">
          <el-option v-for="b in board.baselines || []" :key="b.analysis_id" :value="b.analysis_id" :label="`${b.as_of} · ${b.is_current ? '当前起点' : '历史起点'} · ${reportTime(b.created_at)}`" />
        </el-select></div>
        <el-select v-if="board.versions.length" v-model="selectedVersion" aria-label="研究版本" @change="selectVersion">
          <el-option v-for="v in board.versions" :key="v.id" :value="v.id" :label="`${v.as_of} · ${stateLabel(v.status)} · ${formatTime(v.created_at)}`" />
        </el-select>
      </div>
      <el-alert v-if="historical" type="info" :closable="false" show-icon :title="`正在查看历史起点：${board.baseline?.as_of} 的完整报告`">
        <p>下方点位与每日版本属于这个旧起点。返回当前起点后，可继续更新今日研究。</p><el-button @click="returnToCurrent">返回当前起点</el-button>
      </el-alert>
      <el-alert v-if="!version" title="已建立研究档案，可先查看原报告点位与最新行情；尚未生成每日研究版本。" type="info" :closable="false" />
      <div class="tracking-content">
        <div v-if="version" class="research-metrics">
          <div><span>研究基准日</span><strong>{{ version.as_of }}</strong></div>
          <div><span>本版本收盘参考</span><strong>{{ version.snapshot.price ? version.snapshot.price.close.toFixed(2) + ' 元' : '待补证据' }}</strong></div>
          <div><span>未持仓研究分支</span><strong>{{ actionLabel(version.review?.unheld_action) }}</strong></div>
          <div><span>已持仓研究分支</span><strong>{{ actionLabel(version.review?.held_action) }}</strong></div>
        </div>
        <div class="version-toolbar">
          <el-radio-group v-model="chartMode" aria-label="K线显示方式"><el-radio-button value="latest">最新行情（含盘中）</el-radio-button><el-radio-button value="version" :disabled="!version">研究版本快照</el-radio-button></el-radio-group>
          <el-button :loading="marketLoading" @click="loadMarket(true)">刷新行情（不调用模型）</el-button>
        </div>
        <el-alert v-if="marketError && chartMode === 'latest'" :title="marketError" type="warning" :closable="false" />
        <p class="tracking-note">{{ chartMode === 'latest' ? '最新已完成交易日' : '本研究版本基准日' }}：{{ chartTarget || '待获取' }}；完整日线实际截至：{{ completedBars.at(-1)?.date || '未取得' }}；日线来源：{{ completedBars[0]?.data_source || '未核验' }}。{{ chartMode === 'latest' ? '盘中K线与顶部报价使用同一次快照，收盘日线到齐后替换；行情刷新不会改写研究结论。' : '这是生成研究时保存的历史快照，不随行情刷新改变。' }}</p>
        <p v-if="chartMode === 'latest'" class="tracking-note" aria-live="polite">{{ pollingLabel }}<template v-if="marketChart?.live_bar"> · {{ marketChart.live_bar.date }} {{ marketChart.live_bar.status === 'intraday' ? '未收盘' : '待完整日线确认' }} · 快照 {{ marketChart.live_bar.observed_at }} · {{ marketChart.live_bar.data_source }}</template></p>
        <el-alert v-if="chartMode === 'latest' && ['delayed', 'unavailable'].includes(marketChart?.live_status || '')" title="本次行情未及时更新；已保留最后可用快照及原时间，不能当作当前成交价" type="warning" :closable="false" />
        <el-alert v-if="chartTarget && completedBars.at(-1)?.date !== chartTarget" :title="`${chartMode === 'version' ? '本研究版本生成时' : '本次行情刷新'}未取得 ${chartTarget} 的完整日线；不使用旧收盘价冒充该日价格`" type="warning" :closable="false" />
        <el-radio-group v-if="(version?.levels.length || board.previous_review?.levels.length) && board.baseline?.levels.length" v-model="levelSource" aria-label="点位来源" class="level-source">
          <el-radio-button value="review">{{ version?.levels.length ? '本次复核点位' : '上次复核点位' }}</el-radio-button><el-radio-button value="baseline">原报告点位</el-radio-button>
        </el-radio-group>
        <el-radio-group v-if="hasPointBranches" v-model="pointBranch" aria-label="原报告点位分支" class="level-source">
          <el-radio-button value="held">已持仓执行条件</el-radio-button><el-radio-button value="unheld">未持仓条件方案</el-radio-button>
        </el-radio-group>
        <el-alert v-if="usingBaselineLevels && displayLevels.length" :title="`来源报告日期：${board.baseline?.as_of} · 原报告点位，尚未由所选每日版本复核`" :description="board.baseline?.level_notice" type="info" :closable="false" />
        <el-alert v-if="usingPreviousLevels" :title="`上次复核点位 · 研究日期 ${board.previous_review?.as_of}，生成于 ${reportTime(board.previous_review?.created_at)}`" type="warning" :closable="false">
          <p>本轮综合复核未完成，灰色点位仅保留作历史对照；前提及有效期不延长，不能视为本轮已确认。</p>
          <el-button text @click="selectVersion(board.previous_review!.version_id)">查看上次完整复核（不调用模型）</el-button>
        </el-alert>
        <p class="tracking-note">{{ usingBaselineLevels ? '以下为原报告明确标注的价格，保留原文条件；有效期未明确，不推定续期。复权口径未注明的点位仅按原文标注，不作价格换算。' : `点位来源研究日期 ${levelDate}，行情刷新不会延长其有效期。` }}触及价格不代表条件全部满足或已经成交。</p>
        <section v-if="board.target_comparison?.targets.length" class="target-comparison" aria-label="目标变化依据">
          <strong>目标变化与依据 · {{ board.target_comparison.source_date }}</strong>
          <p v-if="board.target_comparison.previous_targets.length">上一版本 {{ board.target_comparison.previous_date }}：
            <span v-for="(point, index) in board.target_comparison.previous_targets" :key="index">{{ point.label }} {{ point.lower }}–{{ point.upper }} 元； </span>
          </p>
          <p>本版生成于 {{ reportTime(board.target_comparison.known_at) }}。{{ board.target_comparison.notice }}</p>
          <details v-for="(point, index) in board.target_comparison.targets" :key="index" :open="point.status === 'historical_claim_conflict'">
            <summary>{{ point.change === 'added' ? '新增参考' : point.change === 'retained' ? '沿用价位' : '本版参考' }} · {{ point.label }} {{ point.lower }}–{{ point.upper }} 元<span v-if="point.status === 'historical_claim_conflict'"> · 历史依据存在冲突</span></summary>
            <p>{{ point.condition }}</p>
            <p v-if="!point.evidence.length">尚无可自动核验的历史日期／价格字段；保留条件参考，不能据此确认上调合理或已突破。</p>
            <p v-for="(evidence, n) in point.evidence" :key="n">
              {{ evidence.date }} {{ evidence.field === 'high' ? '最高价' : '收盘价' }}：原文 {{ evidence.price }} 元，日线 {{ evidence.actual ?? '未取得' }}{{ evidence.actual == null ? '' : ' 元' }}。
              {{ evidence.status === 'mismatch' ? '日期与价格不匹配，不能作为已核验目标依据。' : evidence.status === 'matched' ? '历史字段相符；未来是否到达尚未证明。' : '缺少该日期日线证据。' }}
            </p>
          </details>
        </section>
        <WyckoffResearch :value="board.wyckoff" v-model:visible="showWyckoff" :market-date="chartTarget" @view-reference="viewPhaseReference" />
        <IntradayResearch v-if="board?.track" :key="board.track.id" :track-id="board.track.id" :baseline-id="board.track.baseline_analysis_id" :enabled="board.track.settings.intraday_enabled !== false" :historical="intradayHistorical" :busy="busy" @toggle="toggleIntraday" @levels="intradayLevels = $event" />
      <ChanResearch :value="chan" v-model="chanLayers" :historical="chartMode === 'version'" />
        <el-checkbox v-model="showLevels">图上显示报告点位</el-checkbox>
        <div v-if="chartBars.length" ref="chartContainer" class="tracking-chart" :style="{ height: `${chartHeight}px` }"><v-chart v-if="chartWidth > 0" :option="chart" :update-options="{ replaceMerge: ['series'] }" :init-options="{ height: chartHeight }" autoresize /></div>
        <el-empty v-else description="本次尚未取得可核验的日线，其他研究内容保留" :image-size="60" />
        <div v-if="displayLevels.length" class="level-grid">
          <div v-for="(level, index) in displayLevels" :key="index" class="level-card" :class="{ expired: level.expired }">
            <div><strong>{{ index + 1 }} · {{ level.label }}</strong><el-tag size="small" :type="level.state === 'report_reference' || usingPreviousLevels || level.expired ? 'info' : 'warning'">{{ usingPreviousLevels ? '上次复核·本轮未复核' : level.state === 'report_reference' ? '原报告·未复核' : level.expired ? '已到期' : '条件参考' }}</el-tag></div>
            <b>{{ formatLevelPrice(level.lower) }}{{ level.upper !== level.lower ? '–' + formatLevelPrice(level.upper) : '' }} 元</b>
            <p class="source-date">{{ levelSourceDate(usingBaselineLevels, level.source_date, board.baseline?.as_of, levelDate) }}</p>
            <details v-if="level.branch === 'unheld'"><summary>查看原文前置条件（触价不等于可以买入）</summary><p style="white-space: pre-wrap">{{ level.condition }}</p></details><p v-else>{{ level.condition }}</p><small>{{ level.valid_until ? '有效至 ' + level.valid_until : '有效期原文未明确' }}<template v-if="level.price_basis === 'unspecified'"> · 复权口径未注明</template></small>
            <details><summary>依据原文<template v-if="level.source_line"> · {{ level.source_section === 'final_trade_decision' ? '最终裁决' : '交易计划' }}第 {{ level.source_line }} 行</template></summary><p>生成于 {{ reportTime(levelCreatedAt) }}。图上标注从 {{ level.effective_from }} 开始，这不是报告更新日期。</p><p>{{ level.source_quote }}</p><router-link v-if="usingBaselineLevels && board.baseline" :to="{ name: 'ReportDetail', params: { id: board.baseline.analysis_id } }">查看来源完整报告</router-link></details>
          </div>
        </div>
        <p v-else class="tracking-note">{{ board.baseline?.level_notice || '尚无可核对来源的价格点位。' }}</p>
        <el-alert v-if="version?.status === 'partial'" title="本次部分模块未完成，尚无完整的当日综合结论；已完成研究与行情均已保存。" type="warning" :closable="false" show-icon />
        <el-alert v-if="version?.review?.requires_full_review" title="本次变化需要重新完整研究，增量报告暂不能解决全部冲突。" type="warning" :closable="false" show-icon />
        <div v-if="version?.review" class="daily-review">
          <h3>本日变化与综合复核</h3>
          <ul v-if="version.review.changes.length"><li v-for="(change, i) in version.review.changes" :key="i">{{ change }}</li></ul>
          <div class="markdown-body" v-html="renderMarkdown(version.review.content)"></div>
          <div v-if="version.review.strategy?.conditions.length" class="conditions">
            <div v-for="(condition, i) in version.review.strategy.conditions" :key="i"><el-tag size="small" :type="condition.state === 'met' ? 'success' : condition.state === 'invalidated' ? 'danger' : 'info'">{{ conditionLabel(condition.state) }}</el-tag><span>{{ condition.text }}</span></div>
          </div>
          <ul class="limitations"><li v-for="(item, i) in version.review.limitations" :key="i">{{ item }}</li></ul>
        </div>
        <ExperienceUsage v-if="version" :key="version.id" :receipts="version.experience_usage || []" />
        <el-collapse v-if="version" class="module-details">
          <el-collapse-item v-for="(module, key) in version.modules" :key="key" :name="key">
            <template #title><span>{{ moduleLabel(String(key)) }} · {{ module.as_of }}</span><el-tag size="small" :type="module.update_error ? 'warning' : 'info'">{{ module.update_error ? '更新未完成' : module.inherited ? '沿用原研究' : '本版更新' }}</el-tag></template>
            <el-alert v-if="module.update_error" :title="module.update_error" type="warning" :closable="false" />
            <div class="markdown-body" v-html="renderMarkdown(module.content)"></div>
            <ul class="limitations"><li v-for="(item, i) in module.limitations" :key="i">{{ item }}</li></ul>
          </el-collapse-item>
          <el-collapse-item title="数据日期与缺项" name="data">
            <div v-for="(domain, key) in version.snapshot.domains" :key="key" class="data-row"><strong>{{ domainLabel(String(key)) }}</strong><span>{{ domain.latest_date || '未取得' }} · {{ domain.status }}</span><p v-for="(reason, i) in domain.limitations" :key="i">{{ reason }}</p></div>
          </el-collapse-item>
        </el-collapse>
      </div>
    </template>
  </el-card>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useElementSize } from '@vueuse/core'
import { use } from 'echarts/core'
import { CandlestickChart, BarChart, ScatterChart, LineChart } from 'echarts/charts'
import { LabelLayout } from 'echarts/features'
import { GridComponent, TooltipComponent, DataZoomComponent, MarkLineComponent, MarkAreaComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'
import { ElMessageBox } from 'element-plus'
import { researchTrackingApi, type TrackingBoard, type TrackingMarketChart, type WyckoffAnnotation, type TrackingLevel } from '@/api/researchTracking'
import { trackingChart, formatLevelPrice } from '@/utils/researchTrackingChart'
import { marketBars, mergeMarketChart, nextMarketPollDelay } from '@/utils/liveMarketChart'
import { renderMarkdown } from '@/utils/markdown'
import { reportTime, levelSourceDate } from '@/utils/trackingBaseline'
import { trackingAvailability, trackingPointSource } from '@/utils/trackingAvailability'
import WyckoffResearch from '@/components/WyckoffResearch.vue'
import ChanResearch from '@/components/ChanResearch.vue'
import IntradayResearch from '@/components/IntradayResearch.vue'
import ExperienceUsage from '@/components/Settlement/ExperienceUsage.vue'

use([CandlestickChart, BarChart, ScatterChart, LineChart, LabelLayout, GridComponent, TooltipComponent, DataZoomComponent, MarkLineComponent, MarkAreaComponent, CanvasRenderer])
const props = defineProps<{ symbol: string }>()
const emit = defineEmits<{ (e: 'tracking-state', active: boolean): void; (e: 'quote-update', value: { quote?: Record<string, any>; delayed: boolean }): void }>()
const board = ref<TrackingBoard | null>(null)
const loading = ref(false), busy = ref(false), error = ref(''), selectedVersion = ref('')
const selectedBaseline = ref('')
let sequence = 0, alive = true, timer: ReturnType<typeof setTimeout> | undefined
const version = computed(() => board.value?.version)
const historical = computed(() => !!board.value?.baseline && board.value.baseline.analysis_id !== board.value.track?.baseline_analysis_id)
const marketChart = ref<TrackingMarketChart | null>(null)
const marketLoading = ref(false), marketError = ref(''), chartMode = ref<'latest' | 'version'>('latest')
const levelSource = ref<'review' | 'baseline'>('review')
const showWyckoff = ref(true)
const showLevels = ref(true)
const intradayLevels = ref<TrackingLevel[]>([])
const intradayHistorical = computed(() => historical.value || chartMode.value !== 'latest' || !!(version.value && board.value?.versions?.[0]?.id !== version.value.id))
const chartLevels = computed(() => [...(showLevels.value ? displayLevels.value : []), ...(!intradayHistorical.value ? intradayLevels.value : [])])
const chanLayers = ref({ fractals: false, strokes: true, zones: true, candidates: true })
const chan = computed(() => chartMode.value === 'latest' ? marketChart.value?.chan : version.value?.snapshot.chan)
const pointSource = computed(() => trackingPointSource(board.value, levelSource.value))
const usingBaselineLevels = computed(() => pointSource.value === 'baseline')
const usingPreviousLevels = computed(() => pointSource.value === 'previous')
const sourceLevels = computed(() => (usingBaselineLevels.value ? board.value?.baseline?.levels : usingPreviousLevels.value ? board.value?.previous_review?.levels : version.value?.levels) || [])
const levelDate = computed(() => usingPreviousLevels.value ? board.value?.previous_review?.as_of : version.value?.as_of)
const levelCreatedAt = computed(() => usingBaselineLevels.value ? board.value?.baseline?.created_at : usingPreviousLevels.value ? board.value?.previous_review?.created_at : version.value?.created_at)
const pointBranch = ref<'held' | 'unheld'>('held')
const hasPointBranches = computed(() => sourceLevels.value.some(p => p.branch === 'held') && sourceLevels.value.some(p => p.branch === 'unheld'))
const displayLevels = computed(() => sourceLevels.value.filter(p => !hasPointBranches.value || !p.branch || p.branch === pointBranch.value))
let marketSequence = 0
let marketTimer: ReturnType<typeof setTimeout> | undefined
const completedBars = computed(() => (chartMode.value === 'latest' ? marketChart.value?.bars : version.value?.snapshot.bars) || [])
const chartBars = computed(() => chartMode.value === 'latest' ? marketBars(marketChart.value) : completedBars.value)
const pollingLabel = computed(() => ({ trading: '交易时段每30秒自动刷新（页面可见时）', lunch: '午休暂停自动刷新，13:00恢复', pre_open: '开盘前暂停，09:30恢复', settling: '每60秒检查收盘日线', closed: '休市暂停自动刷新，下个已验证交易日恢复', unknown: '交易日历未核验，自动刷新暂停' }[marketChart.value?.session?.phase || 'unknown']))
const chartTarget = computed(() => chartMode.value === 'latest' ? marketChart.value?.as_of : version.value?.as_of)
const running = computed(() => ['pending', 'running'].includes(board.value?.run?.status || ''))
const availability = computed(() => trackingAvailability(board.value, marketChart.value?.as_of))
const canRetry = computed(() => availability.value.canRetry)
const runType = computed(() => running.value || availability.value.waiting ? 'info' : ['partial', 'failed', 'interrupted'].includes(board.value?.run?.status || '') ? 'warning' : 'success')
const chartContainer = ref<HTMLElement | null>(null)
const { width: chartWidth } = useElementSize(chartContainer)
const chartHeight = computed(() => Math.max(chartWidth.value < 640 ? 350 : 430, (chartLevels.value.length * (chartWidth.value < 640 ? 28 : 44) + 36) / .62))
const chart = computed(() => trackingChart(chartBars.value, chartLevels.value, (chartMode.value === 'latest' ? marketChart.value?.next_sessions : version.value?.snapshot.next_sessions) || [], chartWidth.value || 1200, chartHeight.value, showWyckoff.value ? board.value?.wyckoff?.timeline?.history || board.value?.wyckoff?.history || [] : [], chan.value, chanLayers.value, showWyckoff.value ? board.value?.wyckoff?.timeline?.boundaries || [] : []))
const stateLabel = (v: string) => ({ completed: '已更新', partial: '部分完成', review_required: '需完整重评' }[v] || v)
const actionLabel = (v?: string) => ({ BUY: '关注买入条件', WATCH: '继续观察', ADD: '关注加仓条件', HOLD: '继续持有研究', REDUCE: '关注减仓条件', SELL: '关注退出条件', RESEARCH_REQUIRED: '需要进一步研究' }[v || ''] || '复核未完成')
const conditionLabel = (v: string) => ({ met: '本轮判断满足', not_met: '尚未满足', unknown: '证据不足', invalidated: '已失效' }[v] || v)
const moduleLabel = (v: string) => ({ technical: '技术研究', capital: '资金研究', events: '新闻与市场背景', fundamentals: '基本面研究' }[v] || v)
const domainLabel = (v: string) => ({ daily_quotes: '日线行情', daily_indicators: '估值与交易指标', money_flow: '资金流', margin_trading: '两融', news: '新闻', financial_data: '财务披露' }[v] || v)
const formatTime = (v: string) => new Date(v).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit' })

async function load(id?: string, baselineId?: string) {
  if (!alive) return
  const request = ++sequence
  clearTimeout(timer)
  loading.value = true
  try {
    const response = await researchTrackingApi.board(props.symbol, id, baselineId)
    if (request !== sequence) return
    board.value = response.data
    if (response.data.track && !marketChart.value && !marketLoading.value) void loadMarket()
    selectedVersion.value = response.data.version?.id || ''
    selectedBaseline.value = response.data.baseline?.analysis_id || ''
    if (!response.data.version) chartMode.value = 'latest'
    error.value = ''
    emit('tracking-state', !!response.data.track)
    if (running.value) timer = setTimeout(() => load(id, baselineId), 5000)
    else if (availability.value.cooldown > 0) timer = setTimeout(() => load(id, baselineId), availability.value.cooldown + 500)
  } catch (e: any) {
    if (request === sequence) error.value = e?.response?.data?.detail || '持续研究暂时无法加载，请刷新重试'
  } finally { if (request === sequence) loading.value = false }
}
function scheduleMarket() {
  clearTimeout(marketTimer)
  if (!alive || document.hidden || !board.value?.track) return
  const delay = nextMarketPollDelay(marketChart.value)
  if (delay !== null) marketTimer = setTimeout(() => loadMarket(), marketError.value ? Math.max(30000, delay) : delay)
}
async function loadMarket(forceRefresh = false) {
  if (!alive || marketLoading.value) return
  clearTimeout(marketTimer)
  const request = ++marketSequence
  marketLoading.value = true
  marketError.value = ''
  try {
    const response = await researchTrackingApi.marketChart(props.symbol, forceRefresh)
    if (alive && request === marketSequence) {
      marketChart.value = mergeMarketChart(marketChart.value, response.data)
      emit('quote-update', { quote: marketChart.value.quote, delayed: ['unavailable', 'delayed'].includes(marketChart.value.live_status || '') })
    }
  } catch (e: any) {
    if (alive && request === marketSequence) {
      marketError.value = e?.response?.data?.detail || '行情暂未获取成功；已显示的数据仍保留原日期'
      if (marketChart.value?.live_bar) marketChart.value = { ...marketChart.value, chan: marketChart.value.chan ? { ...marketChart.value.chan, preview: null } : undefined, live_bar: { ...marketChart.value.live_bar, stale: true }, live_status: 'delayed' }
      emit('quote-update', { quote: marketChart.value?.quote, delayed: true })
    }
  } finally { if (alive && request === marketSequence) { marketLoading.value = false; scheduleMarket() } }
}
function visibilityChanged() {
  clearTimeout(marketTimer)
  if (!document.hidden && board.value?.track) void loadMarket()
}
onMounted(() => document.addEventListener('visibilitychange', visibilityChanged))
defineExpose({ refreshMarket: () => loadMarket(true) })
async function mutate(operation: () => Promise<unknown>) {
  const symbol = props.symbol
  busy.value = true
  try { await operation(); if (alive && symbol === props.symbol) await load() }
  catch (e: any) { if (alive && symbol === props.symbol) error.value = e?.response?.data?.detail || '操作未完成，请检查提示后重试' }
  finally { if (alive && symbol === props.symbol) busy.value = false }
}
const create = () => mutate(() => researchTrackingApi.create(props.symbol))
async function update(retry: boolean) {
  const track = board.value?.track, symbol = props.symbol
  if (!track || busy.value || running.value || historical.value || availability.value.blocked) return
  if (!alive || symbol !== props.symbol || board.value?.track?.baseline_analysis_id !== track.baseline_analysis_id) return
  await mutate(() => researchTrackingApi.update(track.id, retry, 0, track.baseline_analysis_id))
}
const toggleIntraday = (enabled: boolean) => mutate(() => researchTrackingApi.settings(board.value!.track!.id, { ...board.value!.track!.settings, intraday_enabled: enabled }))
const toggleAuto = (value: string | number | boolean) => mutate(() => researchTrackingApi.settings(board.value!.track!.id, { ...board.value!.track!.settings, auto_enabled: Boolean(value) }))
const selectVersion = (id: string) => { chartMode.value = 'version'; levelSource.value = 'review'; load(id, selectedBaseline.value) }
function viewPhaseReference(value: WyckoffAnnotation) {
  chartMode.value = 'version'; levelSource.value = 'review'
  load(value.version_id || undefined, value.source_analysis_id)
}
async function selectBaseline(id: string) {
  levelSource.value = 'review'
  await load(undefined, id)
  chartMode.value = version.value ? 'version' : 'latest'
}
const returnToCurrent = () => { levelSource.value = 'review'; chartMode.value = 'latest'; load() }
async function switchBaseline() {
  const track = board.value?.track, next = board.value?.newer_report, symbol = props.symbol
  if (!track || !next || busy.value || running.value) return
  try {
    await ElMessageBox.confirm(`将研究起点从 ${track.baseline_date} 的报告切换到 ${next.as_of}（生成于 ${reportTime(next.created_at)}）。旧报告和每日版本可继续查看；不会调用模型，自动更新设置保持不变。`, '切换研究起点', { confirmButtonText: '确认切换', cancelButtonText: '暂不切换', type: 'info' })
  } catch { return }
  if (!alive || props.symbol !== symbol) return
  levelSource.value = 'review'; chartMode.value = 'latest'
  await mutate(() => researchTrackingApi.switchBaseline(track.id, next.analysis_id, track.baseline_analysis_id))
}
watch(() => props.symbol, () => { sequence++; marketSequence++; clearTimeout(marketTimer); marketChart.value = null; intradayLevels.value = []; marketLoading.value = false; marketError.value = ''; chartMode.value = 'latest'; levelSource.value = 'review'; pointBranch.value = 'held'; clearTimeout(timer); board.value = null; busy.value = false; error.value = ''; emit('tracking-state', false); load() }, { immediate: true })
onBeforeUnmount(() => { alive = false; sequence++; marketSequence++; clearTimeout(timer); clearTimeout(marketTimer); document.removeEventListener('visibilitychange', visibilityChanged) })
</script>

<style scoped lang="scss">
.tracking-board { margin-bottom: 20px; border-color: var(--el-border-color-light); }
.tracking-heading, .tracking-toolbar, .version-toolbar, .toolbar-group, .start-tracking { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.tracking-heading, .version-toolbar, .start-tracking { justify-content: space-between; }
.subtitle { color: var(--el-text-color-secondary); margin-left: 14px; font-size: 12px; }
.start-tracking p { margin: 0 0 8px; font-weight: 600; }.start-tracking span { color: var(--el-text-color-secondary); font-size: 13px; }
.toolbar-group { font-size: 13px; }.toolbar-group .el-input-number { width: 88px; }
.tracking-note { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.8; margin: 12px 0; }
.new-report-alert { margin-bottom: 18px; }.new-report-alert p { margin: 8px 0; line-height: 1.7; }.new-report-alert .toolbar-group { margin-top: 12px; }.level-card .source-date { color: var(--el-text-color-primary); font-weight: 600; margin: 12px 0 6px; }.history-toolbar .toolbar-group { flex: 1; }.history-toolbar .toolbar-group .el-select { min-width: 270px; flex: 1; }
.run-alert { margin: 14px 0; }.version-toolbar { margin: 16px 0; font-size: 13px; color: var(--el-text-color-secondary); }.version-toolbar .el-select { width: 320px; }
.research-metrics { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; background: var(--el-fill-color-light); border-radius: 8px; padding: 16px; }
.research-metrics div { display: flex; flex-direction: column; gap: 8px; }.research-metrics span { font-size: 12px; color: var(--el-text-color-secondary); }.research-metrics strong { font-size: 16px; }
.tracking-chart { height: 430px; width: 100%; }
.level-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; margin: 18px 0; }.level-card { border: 1px solid var(--el-border-color-lighter); border-radius: 8px; padding: 14px; font-size: 13px; }.level-card > div { display: flex; justify-content: space-between; gap: 8px; margin-bottom: 12px; }.level-card b { font-size: 18px; }.level-card p { line-height: 1.7; }.level-card small { color: var(--el-text-color-secondary); }.level-card.expired { opacity: .65; }.level-card details { margin-top: 10px; }.level-card summary { cursor: pointer; color: var(--el-color-primary); }
.daily-review { margin-top: 24px; }.daily-review h3 { font-size: 16px; }.daily-review li, .limitations li { line-height: 1.8; }.limitations { color: var(--el-text-color-secondary); font-size: 13px; }
.conditions { display: grid; gap: 10px; margin: 16px 0; }.conditions > div { display: flex; align-items: flex-start; gap: 10px; font-size: 13px; line-height: 1.7; }.conditions .el-tag { flex-shrink: 0; }
.module-details { margin-top: 20px; }.module-details :deep(.el-collapse-item__header) { gap: 10px; }.markdown-body { overflow-wrap: anywhere; line-height: 1.8; }.markdown-body :deep(table) { display: block; max-width: 100%; overflow: auto; }.data-row { margin-bottom: 14px; }.data-row strong { margin-right: 12px; }.data-row p { color: var(--el-text-color-secondary); margin: 5px 0; }
@media (max-width: 760px) { .research-metrics { grid-template-columns: repeat(2, 1fr); }.tracking-chart { height: 350px; }.subtitle { display: block; margin: 6px 0 0; }.version-toolbar .el-select { width: 100%; }.tracking-toolbar > .el-button { margin-left: 0; }.conditions > div { flex-wrap: wrap; } }
</style>
