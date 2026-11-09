<template>
  <el-dialog v-model="visible" title="任务结果" width="60%">
    <div v-if="result">
      <el-tag type="info" effect="plain">{{ analysisHorizonLabel(result) }}</el-tag>
      <h4>建议</h4>
      <div class="markdown-content" v-html="renderMarkdown(result.recommendation || '无')"></div>
      <h4 style="margin-top: 16px;">摘要</h4>
      <div class="markdown-content" v-html="renderMarkdown(result.summary || '无')"></div>
    </div>
    <template #footer>
      <el-button @click="emit('close')">关闭</el-button>
      <el-button type="primary" @click="emit('view-report')">查看报告详情</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { renderMarkdown } from '@/utils/markdown'
import { analysisHorizonLabel } from '@/utils/analysisHorizon'
import type { AnalysisHorizon } from '@/types/analysis'

const props = defineProps<{
  modelValue: boolean
  result: { recommendation?: string; summary?: string; analysis_horizon?: AnalysisHorizon; parameters?: Record<string, unknown> } | null
}>()
const emit = defineEmits(['update:modelValue','close','view-report'])

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v)
})
</script>

