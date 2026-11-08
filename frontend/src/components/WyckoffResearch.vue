<template>
  <section class="wyckoff-research" aria-label="威科夫阶段研究">
    <div class="wyckoff-heading">
      <strong>威科夫阶段</strong>
      <el-switch :model-value="visible" aria-label="在K线上显示威科夫阶段" active-text="图上显示" @change="$emit('update:visible', Boolean($event))" />
    </div>
    <p class="wyckoff-notice" :class="{ warning: ['failed', 'missing'].includes(value?.status || '') }" aria-live="polite">{{ value?.notice || '尚无阶段记录；原有研究与价格点位不受影响。' }}</p>
    <p v-if="value?.error" class="wyckoff-error">未完成原因：{{ value.error }}<br v-if="value.error_details_missing" /><span v-if="value.error_details_missing">这条旧记录只保存了错误数量，未保留具体字段；不能据此判断是行情缺失。</span></p>
    <div v-if="value?.pending_judgment" class="previous-phase" aria-label="最新未通过校验的阶段正文">
      <strong>最新正文判断 · {{ value.pending_judgment.as_of }} · 未通过方法校验</strong>
      <blockquote>{{ value.pending_judgment.source_quote }}</blockquote>
      <p>新报告已有判断，但尚未成为有效阶段标注；下方历史判断继续保留原日期。不是没有新报告，也不是沿用旧结论作为今日结论。</p>
    </div>
    <div v-if="!current && value?.previous_reference" class="previous-phase">
      <strong>较早研究起点的判断 · {{ phaseLabel(value.previous_reference) }}</strong>
      <p>依据日期 {{ value.previous_reference.as_of }} · {{ value.previous_reference.source_kind === 'report' ? '原报告参考' : phaseStatus(value.previous_reference.status) }}。按原日期保留为历史标注，尚未复核到本次。</p>
      <blockquote>{{ value.previous_reference.source_quote }}</blockquote>
      <el-button text type="primary" @click="$emit('view-reference', value.previous_reference)">查看该判断的来源（不调用模型）</el-button>
    </div>
    <p v-else-if="!current && !timeline.length" class="wyckoff-dates">没有找到可展示的历史阶段判断。可阅读技术正文中的未校验假说；它不会被当作已确认阶段绘图。</p>
    <router-link v-if="!current && value?.source_analysis_id" :to="{ name: 'ReportDetail', params: { id: value.source_analysis_id } }">查看当前起点的技术报告（{{ value.source_date }}）</router-link>
    <template v-if="current">
      <div class="wyckoff-summary">
        <span>所选起点最近有效判断</span>
        <b :style="{ color: phaseColors[current.phase] }">{{ phaseLabel(current) }}</b>
        <el-tag size="small" :type="current.source_kind === 'report' ? 'info' : current.status === 'supported' ? 'success' : 'warning'">{{ current.source_kind === 'report' ? '原报告参考' : phaseStatus(current.status) }}</el-tag>
        <span>依据截至 {{ current.as_of }}</span>
      </div>
      <template v-if="current.method_version === 'wyckoff-method-v2'">
        <p>趋势背景：{{ trendNames[current.trend || 'uncertain'] }}。阶段与趋势分别判断，事件出现不代表阶段已确认。</p>
        <ul v-if="current.events?.length"><li v-for="(event, index) in current.events" :key="index">{{ event.date }} · {{ event.kind }} · {{ event.status === 'candidate' ? '候选事件' : '本轮量价支持的事件解释' }}</li></ul>
        <p v-if="current.change_reason">相对上次判断：{{ current.change_reason }}</p>
        <p v-if="current.alternative_scenario">替代解释：{{ current.alternative_scenario }}</p>
      </template>
      <p class="wyckoff-dates">{{ current.source_kind === 'report' ? '来源完整报告' : '来源每日技术研究' }}：{{ current.as_of }} · {{ current.source_kind === 'report' ? '报告生成于' : '系统识别于' }} {{ reportTime(current.known_at) }}<br />结构起始日：{{ current.structure_start || '未明确，不回推' }}。盘中行情刷新不改变阶段，阶段随每日技术研究更新。</p>
      <p v-if="marketDate && marketDate > current.as_of" class="wyckoff-notice warning">行情已到 {{ marketDate }}；当前阶段仍依据 {{ current.as_of }} 的研究，尚未复核到最新行情。</p>
      <details class="wyckoff-evidence">
        <summary>查看阶段依据与确认／失效条件</summary>
        <blockquote>{{ current.source_quote }}</blockquote>
        <div class="wyckoff-conditions">
          <div><strong>后续确认条件</strong><ul v-if="current.confirmation_conditions.length"><li v-for="item in current.confirmation_conditions" :key="item">{{ item }}</li></ul><p v-else>未单独提取；不能据此视为无前提。</p></div>
          <div><strong>失效与反证条件</strong><ul v-if="current.invalidation_conditions.length"><li v-for="item in current.invalidation_conditions" :key="item">{{ item }}</li></ul><p v-else>未单独提取；请核对来源报告。</p></div>
        </div>
        <ul><li v-for="item in current.limitations" :key="item">{{ item }}</li></ul>
        <router-link v-if="current.source_kind === 'report'" :to="{ name: 'ReportDetail', params: { id: current.source_analysis_id } }">查看起点报告的技术分析</router-link>
      </details>
    </template>
    <p class="wyckoff-legend">○ 灰色空心：历史判断　▰ 彩色实心：所选起点最近判断。更新失败时，沿用判断保留原日期。虚线标出起点报告日期，实际采用时间见分界记录。</p>
    <details v-if="timeline.length" class="wyckoff-history" :open="!current">
      <summary>跨起点阶段变化记录（{{ timeline.length }}）</summary>
      <p>图中仅标记当前日线窗口覆盖的日期；图外记录仍保留在此。相同日期、不同起点的判断分别显示。</p>
      <p v-if="value?.timeline?.truncated" class="warning">较早每日记录超过展示上限；本次展示最近 300 条候选记录中的有效判断及起点报告。更早记录可切回对应起点查看。</p>
      <ol><li v-for="(entry, index) in timeline" :key="`${entry.source_analysis_id}-${entry.version_id || 'baseline'}-${index}`">
        <span>{{ entry.as_of }}</span><strong>{{ phaseLabel(entry) }}</strong>
        <el-tag size="small" :type="entry.display_role === 'historical' ? 'info' : 'warning'">{{ phaseRole(entry) }}</el-tag>
        <span>{{ entry.source_kind === 'report' ? '原报告参考' : phaseStatus(entry.status) }}</span>
        <small>识别于 {{ reportTime(entry.known_at) }}</small>
        <el-button text type="primary" @click="$emit('view-reference', entry)">查看来源</el-button>
        <details class="history-evidence"><summary>原判断与条件</summary><blockquote>{{ entry.source_quote }}</blockquote>
          <p v-for="item in entry.confirmation_conditions" :key="item">确认：{{ item }}</p>
          <p v-for="item in entry.invalidation_conditions" :key="item">失效：{{ item }}</p>
        </details>
      </li></ol>
    </details>
    <details v-if="value?.timeline?.boundaries.length" class="wyckoff-boundaries">
      <summary>研究起点分界（{{ value.timeline.boundaries.length }}）</summary>
      <ul><li v-for="boundary in value.timeline.boundaries" :key="boundary.analysis_id">
        <b>{{ boundary.selected ? '所选起点' : '历史起点' }}报告：{{ boundary.as_of }}</b>
        <div>报告生成于 {{ reportTime(boundary.report_created_at) }} · 采用时间 {{ boundary.activated_at ? reportTime(boundary.activated_at) : '此视图未记录' }}</div>
      </li></ul>
    </details>
    <p class="wyckoff-footnote">阶段标记落在原研究基准日，识别时间另行保留。切换起点不会延长旧判断；历史判断不延伸为当前观察区间。查看旧版本只显示当时已知的记录。阶段不代表买卖授权。</p>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { WyckoffBoard, WyckoffAnnotation } from '@/api/researchTracking'
import { phaseColors, phaseLabel, phaseStatus, phaseRole, trendNames } from '@/utils/wyckoffTracking'
import { reportTime } from '@/utils/trackingBaseline'
const props = defineProps<{ value?: WyckoffBoard; visible: boolean; marketDate?: string }>()
defineEmits<{ (event: 'update:visible', value: boolean): void; (event: 'view-reference', value: WyckoffAnnotation): void }>()
const current = computed(() => props.value?.current)
const timeline = computed(() => props.value?.timeline?.history || props.value?.history || [])
</script>

<style scoped>
.wyckoff-research { margin: 18px 0 12px; padding: 16px; border: 1px solid var(--el-border-color-light); border-radius: 8px; background: var(--el-bg-color); font-size: 13px; }
.wyckoff-legend, .wyckoff-history p { line-height: 1.8; color: var(--el-text-color-secondary); }.wyckoff-history ol { max-height: 360px; overflow-y: auto; }.history-evidence { flex-basis: 100%; }.wyckoff-boundaries li { padding: 6px 0; }
.wyckoff-error { line-height: 1.8; overflow-wrap: anywhere; color: var(--el-color-warning-dark-2); }.previous-phase { margin: 12px 0; padding: 14px; background: var(--el-fill-color-light); border-radius: 6px; }.previous-phase p { line-height: 1.8; }
.wyckoff-heading, .wyckoff-summary { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }.wyckoff-heading { justify-content: space-between; }.wyckoff-summary b { font-size: 19px; }
.wyckoff-notice, .wyckoff-dates, .wyckoff-footnote { color: var(--el-text-color-secondary); line-height: 1.8; margin: 10px 0; }.warning { color: var(--el-color-warning-dark-2); }.wyckoff-footnote { font-size: 12px; margin-bottom: 0; }
summary { cursor: pointer; color: var(--el-color-primary); line-height: 1.8; }details { margin-top: 10px; }blockquote { margin: 12px 0; border-left: 3px solid var(--el-border-color); padding: 8px 12px; background: var(--el-fill-color-light); white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.8; }
.wyckoff-conditions { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }li, .wyckoff-conditions p { line-height: 1.8; overflow-wrap: anywhere; }ul { padding-left: 20px; }.wyckoff-history ol { padding: 0; list-style: none; }.wyckoff-history li { display: flex; flex-wrap: wrap; gap: 6px 14px; border-bottom: 1px solid var(--el-border-color-lighter); padding: 9px 0; }.wyckoff-history small { color: var(--el-text-color-secondary); }
@media (max-width: 640px) { .wyckoff-conditions { grid-template-columns: 1fr; }.wyckoff-summary b { font-size: 17px; }.wyckoff-research { padding: 12px; } }
</style>
