<script setup lang="ts">
import { ref } from 'vue'
import ResearchTrackingBoard from '../src/components/ResearchTrackingBoard.vue'
const symbol = ref(new URLSearchParams(location.search).get('symbol') || '002241')
</script>
<template>
  <main style="max-width: 1240px; margin: 24px auto; padding: 0 16px">
    <p style="color: #9a7629">隔离环境验收 · 研究正文为合成样例；“最新完整日线”读取真实免费数据。未调用分析模型。</p>
    <label>验收股票 <select v-model="symbol"><option>002241</option><option>603232</option><option>000503</option></select></label>
    <ResearchTrackingBoard :symbol="symbol" style="margin-top: 16px" />
  </main>
</template>
