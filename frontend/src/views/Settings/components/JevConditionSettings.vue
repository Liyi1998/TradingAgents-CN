<template>
  <el-card class="jev-settings" shadow="never" v-loading="loading">
    <template #header>
      <div class="heading"><h3>Jev 自动动作建议与对照</h3><el-tag type="info">不覆盖原裁决</el-tag></div>
    </template>
    <el-alert title="不参与原分析和最终裁决" type="info" :closable="false" show-icon>
      核验在新报告保存后独立运行。不更改价格、仓位或买卖授权，不重跑历史分析。
      缺证据、超时或失败只记录状态，不阻塞主流程。
    </el-alert>
    <el-alert v-if="error" class="notice" :title="error" type="error" :closable="false" show-icon />
    <el-button v-if="!settings && !loading" class="notice" @click="load">重新读取配置</el-button>
    <template v-if="settings">
      <p class="current" aria-live="polite">当前已保存状态：<strong>{{ savedState }}</strong></p>
      <el-form label-position="top" :disabled="saving || loading">
        <el-form-item label="启用条件核验">
          <el-switch v-model="enabled" aria-label="启用条件核验" @change="onMasterChange" />
          <p class="help">只打开此项：保存本地对照记录，不调用 Jev。</p>
        </el-form-item>
        <el-form-item label="允许向 Jev 外发核验">
          <el-switch v-model="external" aria-label="允许向 Jev 外发核验" :disabled="saving || loading || !enabled || !settings.key_available" />
          <p class="help">打开并保存后，新报告可触发付费调用。仍只记录核验结果，不改变原裁决。</p>
        </el-form-item>
        <el-form-item label="每日调用尝试上限（全系统）">
          <el-input-number v-model="dailyLimit" :min="1" :max="200" :precision="0" aria-label="每日调用尝试上限" />
          <p class="help">按北京时间计日，失败尝试也计入；达到上限后仅记录，不外发。</p>
        </el-form-item>
        <el-form-item label="允许手动动作试验（独立开关）">
          <el-switch v-model="experiments" aria-label="允许手动动作试验" />
          <p class="help">默认关闭。启用后仍须在报告页确认公共策略、预览已收盘日线，并逐次确认外发；不会定时调用或自动交易。与条件核验共享上述总开关、外发开关和日调用上限。</p>
        </el-form-item>
        <el-form-item label="自动最终动作建议（推荐主入口）">
          <el-switch v-model="finalAction" aria-label="自动最终动作建议" />
          <p class="help">默认关闭。启用后，每份新报告完成时自动整理最终方案与分析证据，不再手动选段；满足外发条件时最多调用一次。同一报告不再运行旧条件核验，旧记录保留。只并列展示建议，不覆盖原裁决，不补跑旧报告。</p>
        </el-form-item>
      </el-form>
      <dl class="metadata">
        <dt>模型</dt><dd>{{ settings.model }} · 超时 {{ settings.timeout_seconds }} 秒</dd>
        <dt>密钥</dt><dd>{{ settings.key_available ? '已配置，可在后端复用（不会返回浏览器）' : '未配置，暂不能启用外发；请联系管理员' }}</dd>
        <dt>发送范围</dt><dd>脱敏的公开报告摘录和条件方案；普通报告每份最多 5,000 字节，最终方案最多 30,000 字节，状态材料合计最多 48,000 字节。发现需脱敏或截断的材料时跳过外发。</dd>
        <dt>排除内容</dt><dd>实际持仓、成本、账户金额、凭据和内网信息。</dd>
        <dt>动作试验</dt><dd>人工确认的完整公共策略原文合计最多 5,000 字节，加最近六个已收盘交易日日线，材料合计最多 8,000 字节；同时评估两个假设分支，不读取或发送实际持仓。日线不能证明当前新闻、财务或盘中条件。</dd>
        <dt>自动建议</dt><dd>按报告分析时点处理完整公共最终方案及各分析师摘录，沿用上述 5,000 / 30,000 / 48,000 字节范围；不获取新行情。关键策略因脱敏丢失、依赖账户资料或超限时只记录原因，不调用。证据缺项不得当作条件满足。</dd>
        <dt>接收方</dt><dd>https://api.typesafe.ai/v1/systemone</dd>
      </dl>
      <p class="help">关闭后停止后续外发；已经发出的请求可能仍会完成。开关不会启用旧版 Jev 报告复核。</p>
      <div class="actions">
        <el-button type="primary" :loading="saving" :disabled="loading || !dirty || !validLimit" @click="save">保存设置</el-button>
        <el-button :disabled="saving || loading" @click="load">重新读取</el-button>
        <span v-if="dirty" class="help" role="status">有未保存的修改</span>
      </div>
    </template>
  </el-card>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { jevConditionsApi, type JevConditionSettings } from '@/api/jevConditions'

const settings = ref<JevConditionSettings | null>(null)
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const enabled = ref(false)
const external = ref(false)
const experiments = ref(false)
const finalAction = ref(false)
const dailyLimit = ref<number | undefined>(20)
const validLimit = computed(() => Number.isInteger(dailyLimit.value) && Number(dailyLimit.value) >= 1 && Number(dailyLimit.value) <= 200)
const dirty = computed(() => !!settings.value && (enabled.value !== settings.value.enabled || external.value !== settings.value.external_calls_enabled || experiments.value !== Boolean(settings.value.decision_experiments_enabled) || finalAction.value !== Boolean(settings.value.final_action_enabled) || dailyLimit.value !== settings.value.max_calls_per_day))
const savedState = computed(() => !settings.value?.enabled ? '已关闭' : settings.value.external_calls_enabled ? '已启用 Jev 外发核验' : '仅本地记录（不外发）')
function apply(value: JevConditionSettings) {
  settings.value = value
  enabled.value = value.enabled
  external.value = value.external_calls_enabled
  experiments.value = Boolean(value.decision_experiments_enabled)
  finalAction.value = Boolean(value.final_action_enabled)
  dailyLimit.value = value.max_calls_per_day
}
function onMasterChange() { if (!enabled.value) external.value = false }
function failureMessage(cause: any) {
  const status = cause?.response?.status
  if (status === 403) return '只有管理员可以读取和修改 Jev 设置。'
  if (status === 409) return '配置已被其他操作修改，请点击“重新读取”后再保存。'
  return '配置操作未确认成功，请重新读取状态后重试；不会自动重试外发设置。'
}
async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await jevConditionsApi.get()
    if (!response.success) throw new Error('unavailable')
    apply(response.data)
  } catch (cause) { error.value = failureMessage(cause) }
  finally { loading.value = false }
}
async function save() {
  if (!settings.value || !validLimit.value || saving.value) return
  saving.value = true
  error.value = ''
  let confirmed = false
  try {
    if ((enabled.value && external.value && !(settings.value.enabled && settings.value.external_calls_enabled)) || (experiments.value && !settings.value.decision_experiments_enabled) || (finalAction.value && !settings.value.final_action_enabled)) {
      try {
        await ElMessageBox.confirm('接收方为 https://api.typesafe.ai/v1/systemone，可能产生用量费用。自动最终动作建议开启后，新报告完成可自动外发一次，不再逐次确认，并替代该报告的旧条件核验。两者沿用脱敏报告摘录范围（每份 5,000 / 最终方案 30,000 / 合计 48,000 字节上限）。手动动作试验仍须逐次确认，策略及日线合计最多 8,000 字节。均排除实际持仓、成本、账户金额、凭据和内网信息；不自动交易、不改变原裁决、不补跑旧报告。是否确认？', '确认 Jev 外发范围', { confirmButtonText: '确认并保存', cancelButtonText: '取消', type: 'warning', closeOnClickModal: false })
        confirmed = true
      } catch { return }
    }
    const response = await jevConditionsApi.save({ enabled: enabled.value, external_calls_enabled: external.value, decision_experiments_enabled: experiments.value, final_action_enabled: finalAction.value, max_calls_per_day: dailyLimit.value!, revision: settings.value.revision, confirm_external: confirmed })
    if (!response.success) throw new Error('unavailable')
    apply(response.data)
    ElMessage.success('已保存，对后续新分析生效；未重跑任何分析')
  } catch (cause) { error.value = failureMessage(cause) }
  finally { saving.value = false }
}
onMounted(load)
</script>

<style scoped>
.heading, .actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.heading h3 { margin: 0; }
.notice, .current { margin-top: 20px; }
.current { margin-bottom: 24px; }
.help { color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.6; margin: 6px 0 0; width: 100%; }
.metadata { display: grid; grid-template-columns: 70px minmax(0, 1fr); gap: 12px 16px; font-size: 14px; line-height: 1.7; padding: 20px; background: var(--el-fill-color-light); border-radius: var(--el-border-radius-base); }
.metadata dt { color: var(--el-text-color-secondary); }
.metadata dd { margin: 0; overflow-wrap: anywhere; }
.actions { margin-top: 24px; }
.actions .help { width: auto; }
@media (max-width: 600px) { .metadata { grid-template-columns: 1fr; gap: 4px; padding: 14px; } .metadata dd { margin-bottom: 10px; } }
</style>
