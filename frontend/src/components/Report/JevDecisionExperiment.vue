<template>
  <el-card class="decision-experiment" shadow="never">
    <template #header><div class="heading"><h3>Jev 动作试验</h3><el-tag type="info">手动 · 日线级 · 仅对照</el-tag></div></template>
    <p class="note">研究策略保持不变，更新市场状态后让 Jev 选择假设动作。不自动交易，不改原裁决，也不是实时盯盘。旧的“条件核验”记录与此独立。</p>
    <el-collapse>
      <el-collapse-item title="准备一次试验：确认公共策略与有效期" name="prepare">
        <el-alert title="候选条件仅在本地识别，不调用模型、不获取行情" type="info" :closable="false" />
        <p class="note">唯一、未超限的明确条件章节已填入；多个候选或未分章节的原文需要你选择。这里只定位完整原文，不代表条件齐全或当前已获授权。请核对其他章节的附带条件、例外和限制，不要遗漏盈利、回款、放量、周线或回踩前提。</p>
        <p v-if="!sourceText || sourceText.length > 200000" class="note">{{ !sourceText ? '没有可用的最终交易计划原文，不能生成试验。' : '原文过长，未自动扫描或截断；请人工核对完整原文。' }}</p>
        <details><summary>查看全部{{ sourceLabel }}原文（带行号）</summary><pre class="source">{{ numberedSource || '没有可用的最终计划原文' }}</pre></details>
        <el-form label-position="top" :disabled="busy">
          <section v-for="role in conditionRoles" :key="role" class="condition-section spaced">
            <h4>{{ conditionLabels[role] }}</h4>
            <p v-if="!candidates[role].length" class="missing">未识别到此类候选，不代表报告一定没有。请查看原文；若确实未写，不要自行编造，当前无法完成试验。</p>
            <template v-else>
              <p class="note">找到 {{ candidates[role].length }} 个候选；{{ candidates[role].length > 1 ? '请比较原文，不能默认第一段包含全部条件。' : '仍需人工核对完整性。' }}</p>
              <details v-for="candidate in candidates[role]" :key="candidate.id" class="candidate">
                <summary>{{ sourceLabel }} · 第 {{ candidate.startLine }}–{{ candidate.endLine }} 行 · {{ candidate.kind === 'section' ? '条件章节' : '整段候选（含义待核对）' }}</summary>
                <pre>{{ candidate.text }}</pre>
                <p v-if="candidate.blocked" class="missing">{{ candidate.blocked }}</p>
                <el-button size="small" :disabled="busy || !!candidate.blocked" @click="choose(role, candidate)">选用这段完整原文</el-button>
              </details>
            </template>
            <el-form-item :label="`待确认原文（最多 ${conditionLimits[role]} 字符，可人工调整为其他完整原文段落）`">
              <el-input v-model="form[role]" type="textarea" :rows="3" :aria-label="conditionLabels[role]" />
            </el-form-item>
            <p v-if="locations[role]" class="note">出处：{{ sourceLabel }} · 第 {{ locations[role]!.startLine }}–{{ locations[role]!.endLine }} 行。逐字匹配仅证明出处，不证明语义完整。</p>
            <p v-else-if="form[role]" class="missing">内容不是完整、连续的原文行，请重新选择；不能补写条件。</p>
            <p v-if="Array.from(form[role]).length > conditionLimits[role]" class="missing">超出字符上限，未截断。请选取更小且完整的策略范围。</p>
          </section>
          <p v-if="strategyBytes > 5000" class="missing">三项原文合计 {{ strategyBytes }} 字节，超过 5000 字节上限；未截断、未外发。</p>
          <el-form-item label="本次公共策略有效截止日（人工确认，不等于报告自动续期）"><el-date-picker v-model="form.valid_until" type="date" value-format="YYYY-MM-DD" placeholder="选择有效截止日" aria-label="策略有效截止日" /></el-form-item>
          <el-checkbox v-model="form.complete_conditions_confirmed">我已核对必要前提、退出条件、否决条件及依赖，未省略影响判断的内容</el-checkbox>
          <div class="actions"><el-button type="primary" :disabled="!ready" :loading="busy" @click="preview">获取日线并本地预览（不调用模型）</el-button></div>
        </el-form>
      </el-collapse-item>
    </el-collapse>
    <el-alert v-if="error" class="spaced" :title="error" type="warning" :closable="false" show-icon />
    <section v-if="draft" class="preview spaced" aria-label="动作试验预览">
      <h4>本次预览 · {{ draft.status === 'preview' ? '尚未外发' : '材料未就绪' }}</h4>
      <p v-if="draft.guards?.length">{{ draft.guards.map(guardLabel).join('；') }}。不会调用 Jev。</p>
      <template v-else>
        <p>行情截至 {{ draft.packet?.market_state.as_of }}，CNY 元/股、raw 不复权、已收盘日线；不是当前可成交价。</p>
        <p>最新已收盘价格：<strong>{{ draft.packet?.market_state.bars?.slice(-1)[0]?.close ?? '未取得' }}</strong> 元/股；来源：{{ draft.packet?.market_state.bars?.slice(-1)[0]?.data_source ?? '未核实' }}。</p>
        <p>假设新增分支：{{ draft.packet?.strategy.entry_candidate_permitted ? '允许评估候选动作，仍非买入授权' : '原报告未提供可核实的正新增额度，硬规则禁止入场候选' }}。</p>
        <p>策略有效至 {{ draft.packet?.strategy.valid_until }}。预览最多保留十分钟，编辑材料后必须重新预览。</p>
        <details><summary>查看即将发送的公共策略和行情材料</summary><pre>{{ JSON.stringify(draft.packet, null, 2) }}</pre></details>
        <p v-if="!draft.model_call_enabled" class="note">动作试验、外发开关未全部启用，或密钥不可用。请由管理员检查 Jev 设置后重新预览；当前仅本地预览。</p>
        <div class="actions"><el-button type="warning" :disabled="busy || !draft.model_call_enabled" @click="run">确认材料并调用 Jev 一次</el-button></div>
      </template>
    </section>
    <div class="heading spaced"><h4>独立试验记录</h4><el-button size="small" :disabled="busy" @click="load">只刷新记录</el-button></div>
    <p v-if="!records.length" class="note">暂无记录。打开页面、刷新记录或生成预览均不会调用 Jev。</p>
    <section v-for="row in records" :key="row.id" class="result spaced">
      <strong>{{ row.status === 'completed' && !validDecisionResult(row) ? '结果格式或约束校验未通过' : statusLabel(row.status) }}</strong>
      <p class="note">{{ experimentTime(row.created_at) }} · {{ row.external_call_attempted ? '已尝试外发' : '未尝试外发' }}<span v-if="row.elapsed_seconds !== undefined"> · {{ row.elapsed_seconds }} 秒（包含本地处理）</span></p>
      <dl v-if="validDecisionResult(row) && row.result">
        <dt>假设未持仓</dt><dd>{{ actionLabel(row.result.entry_action) }}</dd>
        <dt>假设已持仓</dt><dd>{{ actionLabel(row.result.existing_action) }}</dd>
        <dt>条件风险状态</dt><dd>{{ actionLabel(row.result.risk_state) }}</dd>
      </dl>
      <p v-else>{{ row.reason || '尚无有效动作结果；不能理解为通过或可以买入。' }}</p>
      <p class="note">规则基线：等待／需要研究。规则不推断文本条件成立。Jev 与规则一致不证明准确，不一致也不代表 Jev 更好。</p>
    </section>
    <p class="note spaced">日线不能核实当前新闻、财务、盘中承接或真实账户约束。缺少必要证据应等待或重新研究。模型动作不代表胜率或收益承诺；试验开关默认关闭，须由管理员另行启用。</p>
  </el-card>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch, onBeforeUnmount } from 'vue'
import { ElMessageBox } from 'element-plus'
import { jevDecisionsApi, type DecisionExperiment, type StrategyInput } from '@/api/jevDecisions'
import { experimentTime, validDecisionResult } from '@/utils/jevDecisionExperiment'
import { conditionRoles, conditionLabels, conditionLimits, extractStrategyCandidates, initialStrategyCandidates, sourceLocation, type ConditionRole, type StrategyCandidate } from '@/utils/jevStrategyCandidates'
const props = withDefaults(defineProps<{ analysisId: string; sourceText: string; sourceLabel?: string }>(), { sourceLabel: '最终交易计划' })
const form = reactive<StrategyInput>({ entry: '', exit: '', veto: '', valid_until: '', complete_conditions_confirmed: false })
const draft = ref<DecisionExperiment | null>(null)
const records = ref<DecisionExperiment[]>([])
const error = ref('')
const busy = ref(false)
let version = 0
const candidates = computed(() => extractStrategyCandidates(props.sourceText))
const numberedSource = computed(() => props.sourceText.split('\n').map((line, i) => `${i + 1} | ${line}`).join('\n'))
const locations = computed(() => Object.fromEntries(conditionRoles.map(role => [role, sourceLocation(props.sourceText, form[role])])) as Record<ConditionRole, ReturnType<typeof sourceLocation>>)
const strategyBytes = computed(() => conditionRoles.reduce((sum, role) => sum + new TextEncoder().encode(form[role]).length, 0))
const ready = computed(() => !!(conditionRoles.every(role => locations.value[role] && Array.from(form[role]).length <= conditionLimits[role]) && strategyBytes.value <= 5000 && form.valid_until && form.complete_conditions_confirmed))
function choose(role: ConditionRole, candidate: StrategyCandidate) {
  if (busy.value || candidate.blocked) return
  form[role] = candidate.text
  form.complete_conditions_confirmed = false
}
watch(form, () => { draft.value = null })
watch(() => [form.entry, form.exit, form.veto, form.valid_until], () => { form.complete_conditions_confirmed = false })
const message = (e: any) => e?.response?.status === 422 ? '材料长度或格式不符合要求。请保留完整条件并缩小试验范围，系统不会替你截断。' : typeof e?.response?.data?.detail === 'string' && e.response.status === 400 ? e.response.data.detail : '操作未确认成功。请刷新记录确认状态，不要重复外发；原分析不受影响。'
async function load() {
  const current = ++version
  records.value = []; error.value = ''; busy.value = true
  try { const r = await jevDecisionsApi.history(props.analysisId); if (current === version && r.success) records.value = r.data }
  catch { if (current === version) error.value = '试验记录暂不可读，原报告不受影响。' }
  finally { if (current === version) busy.value = false }
}
async function preview() {
  if (!ready.value || busy.value) return
  const current = ++version
  busy.value = true; error.value = ''; draft.value = null
  try { const r = await jevDecisionsApi.preview(props.analysisId, { ...form }); if (current === version && r.success) draft.value = r.data }
  catch (e) { if (current === version) error.value = message(e) }
  finally { if (current === version) busy.value = false }
}
async function run() {
  if (!draft.value || !draft.value.model_call_enabled || busy.value) return
  const id = props.analysisId, previewId = draft.value.id, current = ++version
  busy.value = true
  try {
    await ElMessageBox.confirm('仅发送预览中的公共策略和日线至 TypeSafe Jev，可能消耗少量额度。两个持仓分支均为假设，不发送真实账户数据；不修改原裁决、不自动交易。确认本次调用一次？', '确认动作试验', { type: 'warning', closeOnClickModal: false, confirmButtonText: '调用一次', cancelButtonText: '取消' })
    if (current !== version) return
    error.value = ''
    const r = await jevDecisionsApi.run(id, previewId)
    if (current === version && r.success) { records.value = [r.data, ...records.value.filter(x => x.id !== r.data.id)]; draft.value = null }
  } catch (e) { if (current === version && e !== 'cancel' && e !== 'close') error.value = message(e) }
  finally { if (current === version) busy.value = false }
}
function actionLabel(value: string) { return ({ WAIT: '等待', ENTRY_CANDIDATE: '入场候选（非买入授权）', HOLD_CANDIDATE: '继续持有候选（假设情景）', EXIT_REVIEW: '考虑退出，需人工复核', RESEARCH_REQUIRED: '需要重新研究', WITHIN_CONDITIONS: '所给证据支持条件（不代表安全）', VETO_OR_EXIT: '触发否决或退出条件', INSUFFICIENT: '必要证据不足' } as Record<string, string>)[value] || '未知结果，不作为有效动作' }
function statusLabel(value: string) { return ({ completed: '动作试验已记录', running: '正在处理', unavailable: '未取得有效结果', not_called: '未调用', incomplete: '处理已中断，不自动重试' } as Record<string, string>)[value] || '状态未知' }
function guardLabel(value: string) { return ({ CALENDAR_UNAVAILABLE: '交易日历证据不足', DAILY_DATA_INCOMPLETE: '日线不完整', DAILY_DATA_AMBIGUOUS: '日线缺项或重复', BASIS_OR_UNITS_UNKNOWN: '复权口径或单位不明', SOURCE_UNKNOWN: '来源未核实', PRICE_OR_VOLUME_INVALID: '价格或成交量无效', OHLC_INCONSISTENT: '日线价格不一致', MIXED_SOURCES: '没有单一来源的完整日线序列', DAILY_SOURCE_CONFLICT: '可比日线来源存在价格冲突', DATA_UNAVAILABLE: '按需行情获取失败或超时' } as Record<string, string>)[value] || '材料检查未通过' }
watch(() => [props.analysisId, props.sourceText, props.sourceLabel], () => {
  // A replacement report invalidates pending responses, selections and consent.
  draft.value = null
  Object.assign(form, initialStrategyCandidates(candidates.value), { valid_until: '', complete_conditions_confirmed: false })
  load()
}, { immediate: true })
onBeforeUnmount(() => { version++ })
</script>

<style scoped>
.decision-experiment { margin-bottom: 24px; }
.heading, .actions { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
h3 { margin: 0; font-size: 16px; } h4 { margin: 0; font-size: 14px; }
.note { font-size: 13px; line-height: 1.7; color: var(--el-text-color-secondary); }
.spaced, .actions { margin-top: 16px; }
.preview, .result { padding: 16px; border: 1px solid var(--el-border-color); border-radius: var(--el-border-radius-base); }
.preview { background: var(--el-fill-color-light); }
details { margin: 12px 0; } summary { cursor: pointer; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 360px; overflow-y: auto; font-size: 12px; line-height: 1.7; }
.source { background: var(--el-fill-color-light); padding: 12px; }
.condition-section { border-top: 1px solid var(--el-border-color-lighter); padding-top: 16px; }
.candidate { padding: 10px 12px; background: var(--el-fill-color-light); border-radius: var(--el-border-radius-base); }
.candidate summary, .missing { overflow-wrap: anywhere; font-size: 13px; line-height: 1.7; }
.missing { color: var(--el-color-warning-dark-2); }
dl { display: grid; grid-template-columns: 120px minmax(0, 1fr); gap: 12px; font-size: 14px; }
dd { margin: 0; overflow-wrap: anywhere; }
:deep(.el-checkbox) { height: auto; white-space: normal; align-items: flex-start; }
:deep(.el-checkbox__label) { white-space: normal; line-height: 1.6; }
@media (max-width: 600px) { dl { grid-template-columns: 1fr; gap: 6px; } dd { margin-bottom: 10px; } }
</style>
