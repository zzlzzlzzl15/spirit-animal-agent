/**
 * Vue 3 入口 — Spirit Desktop Pet。
 */

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import './styles/pet.css'

const app = createApp(App)
app.use(createPinia())
app.mount('#app')
