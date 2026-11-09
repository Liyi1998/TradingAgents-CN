<template>
  <section class="price-references" aria-label="价格证据与条件方案">
    <h4>价格证据</h4>
    <div class="price-grid">
      <div class="price-panel">
        <span class="caption">分析时已验证报价快照</span>
        <strong v-if="spot">{{ spot.price }} <small>人民币元/股</small></strong>
        <strong v-else class="unavailable">未取得已验证报价快照</strong>
        <p v-if="spot">{{ spot.date }} · {{ spot.source }} · {{ spot.basis }}<br>{{ spot.label }}</p>
        <p v-else>{{ reference ? '该缺项不影响其他已验证证据；不能将历史收盘当作实时价格。' : '旧报告未保存独立报价快照，请查看原报告行情日期；不补写为当前价格。' }}</p>
      </div>
      <div class="price-panel">
        <span class="caption">已完成日线收盘参考</span>
        <strong v-if="historical">{{ historical.price }} <small>人民币元/股</small></strong>
        <strong v-else class="unavailable">未保存独立收盘参考</strong>
        <p v-if="historical">{{ historical.date }} · {{ historical.source }} · {{ historical.basis }} · 非实时报价</p>
        <p v-else>原始报告中的历史价格仍保留，不从叙述中猜测或拼接数字。</p>
      </div>
    </div>
    <p class="price-note">以上是报告生成时保存的证据，不是打开页面时的实时报价，也不是委托价格。</p>
    <div v-if="plans.length" class="conditional-plans">
      <h4>条件入场方案 <small>参考区间与触发条件分开展示</small></h4>
      <article v-for="(plan, i) in plans" :key="i" class="price-panel">
        <span class="caption">{{ plan.name }}</span>
        <strong>{{ plan.range || '未提供结构化价格区间，见原始说明' }} <small v-if="plan.range">{{ plan.unit }}</small></strong>
        <p><b>触发条件：</b>{{ plan.conditions }}</p>
        <p><b>方案状态：</b>{{ plan.status }}</p>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { observedPrice, pricePlans } from '@/utils/priceReference'
const props = defineProps<{ reference?: any; strategies?: unknown }>()
const spot = computed(() => observedPrice(props.reference?.spot))
const historical = computed(() => observedPrice(props.reference?.historical_close))
const plans = computed(() => pricePlans(props.strategies))
</script>

<style scoped>
.price-references { margin-top: 24px; }
h4 { margin: 0 0 12px; color: var(--el-text-color-primary); }
h4 small { font-size: 12px; font-weight: normal; color: var(--el-text-color-secondary); }
.price-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.price-panel { padding: 16px; border: 1px solid var(--el-border-color-light); border-radius: 8px; background: var(--el-fill-color-light); overflow-wrap: anywhere; min-width: 0; }
.caption { display: block; font-size: 13px; color: var(--el-text-color-secondary); margin-bottom: 8px; }
strong { display: block; font-size: 20px; color: var(--el-text-color-primary); }
strong small { font-size: 12px; font-weight: normal; }
.unavailable { font-size: 15px; font-weight: 500; }
p { margin: 10px 0 0; font-size: 13px; line-height: 1.7; color: var(--el-text-color-regular); }
.price-note { color: var(--el-text-color-secondary); }
.conditional-plans { margin-top: 18px; }
.conditional-plans article + article { margin-top: 12px; }
@media (max-width: 640px) { .price-grid { grid-template-columns: 1fr; } }
</style>
