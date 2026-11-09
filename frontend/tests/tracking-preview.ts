// Local acceptance entry only. Not referenced by the production build.
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import TrackingPreview from './TrackingPreview.vue'

const login = await fetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: 'tracking-ui', password: 'tracking-ui-test-only' }) })
if (!login.ok) throw Error('isolated acceptance login failed')
const result = await login.json()
localStorage.setItem('auth-token', result.data.access_token)
localStorage.setItem('user-info', JSON.stringify(result.data.user))
const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div />' } }, { path: '/report/:id', name: 'ReportDetail', component: { template: '<div>验收起点报告</div>' } }] })
createApp(TrackingPreview).use(createPinia()).use(ElementPlus).use(router).mount('#app')
