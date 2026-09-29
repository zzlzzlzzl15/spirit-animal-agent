<template>
  <div class="status-popup-page">
    <div class="popup-header">
      <span class="popup-title">Spirit Agent</span>
      <button class="popup-close" @click="closeWindow">✕</button>
    </div>
    <div class="popup-body">
      <div class="status-row">
        <span class="status-label">状态</span>
        <span class="status-value" :class="{ running: status.running }">
          {{ status.running ? '运行中' : '空闲' }}
        </span>
      </div>
      <div class="status-row">
        <span class="status-label">模型</span>
        <span class="status-value">{{ status.model || '未配置' }}</span>
      </div>
      <div class="status-row">
        <span class="status-label">Provider</span>
        <span class="status-value">{{ status.provider || '-' }}</span>
      </div>
      <div class="status-row">
        <span class="status-label">会话</span>
        <span class="status-value">{{ sessionLabel }}</span>
      </div>
      <div class="divider"></div>
      <div class="status-row">
        <span class="status-label">消息数</span>
        <span class="status-value">{{ status.message_count }}</span>
      </div>
      <div class="status-row">
        <span class="status-label">工具数</span>
        <span class="status-value">{{ status.tool_count }}</span>
      </div>
      <div class="status-row">
        <span class="status-label">API 调用</span>
        <span class="status-value">{{ status.api_call_count }}</span>
      </div>
      <div class="status-row" v-for="u in usageRows" :key="u.label">
        <span class="status-label">{{ u.label }}</span>
        <span class="status-value" :class="{ dim: u.dim }">{{ u.value }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useStatusStore } from '../stores/statusStore'
import { useWebSocket } from '../composables/useWebSocket'
import { storeToRefs } from 'pinia'
import { computed, onMounted, ref } from 'vue'

const statusStore = useStatusStore()
// storeToRefs 保持响应式：syncFromServer 是整体替换 agentStatus.value，
// 直接赋值解包会抓住旧的空对象，模板永远不更新（弹窗数字全默认的根因）
const { agentStatus: status } = storeToRefs(statusStore)
const { connect, send } = useWebSocket()
const usagePending = ref(true)

// 会话行不裸显 UUID：显示 开始时刻 + 历时
const sessionLabel = computed(() => {
  const t = status.value.session_started_at
  if (!t) return '-'
  const d = new Date(t * 1000)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const el = Math.max(0, Math.floor(Date.now() / 1000 - t))
  const elStr = el >= 3600
    ? `${Math.floor(el / 3600)}h${Math.floor((el % 3600) / 60)}m`
    : `${Math.floor(el / 60)}m`
  return `${hh}:${mm} 起 · ${elStr}`
})

function fmtReset(s: number): string {
  if (!s || s <= 0) return ''
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return h > 0 ? `${h}h${m}m` : `${m}m`
}

// 额度行：有官方查询 API 的 provider 显示 5h 已用百分比+重置倒计时，
// 无 API 的（如阿里云 Token Plan 月度 Credits）如实标注
const usageRows = computed(() => {
  if (usagePending.value) return [{ label: '额度', value: '查询中…', dim: true }]
  const providers = status.value.usage?.providers || []
  const rows: { label: string; value: string; dim?: boolean }[] = []
  for (const p of providers) {
    if (p.available && p.window === '5h') {
      const reset = fmtReset(p.reset_seconds || 0)
      rows.push({
        label: `5h额度·${p.name}`,
        value: `已用 ${p.used_percent}%${reset ? ` · ${reset}重置` : ''}`,
      })
    } else {
      rows.push({ label: `额度·${p.name}`, value: p.reason || '无查询接口', dim: true })
    }
  }
  if (!rows.length) rows.push({ label: '额度', value: '-', dim: true })
  return rows
})

function closeWindow() {
  if (window.electronAPI?.closeStatusWindow) {
    window.electronAPI.closeStatusWindow()
  }
}

onMounted(async () => {
  // 连接 WebSocket 同步初始状态
  await connect('ws://127.0.0.1:9877', {
    onEvent(event: string, data: any) {
      if (event === 'init') {
        statusStore.syncFromServer(data)
      }
    },
  })
  // 双保险：每次开窗主动拉一次最新状态，
  // 保证点开那一刻数字就是当前值（不只依赖 init 推送）
  try {
    const data = await send('get_status')
    statusStore.syncFromServer(data)
  } catch { /* 后端未就绪等；重连后的 init 会补上 */ }
  // 额度行每次开窗强制重查官方接口（不用缓存旧值）
  try {
    const usage = await send('get_usage')
    statusStore.syncFromServer({ usage })
  } catch { /* 查询失败时行内显示降级文案 */ }
  usagePending.value = false
})
</script>

<style>
body {
  margin: 0;
  padding: 0;
  background: transparent;
  overflow: hidden;
}
</style>

<style scoped>
.status-popup-page {
  width: 100vw;
  height: 100vh;
  background: rgba(15, 15, 25, 0.95);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: 2px solid rgba(251, 146, 60, 0.8);
  border-radius: 12px;
  color: #e0e0e0;
  font-size: 12px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.6);
  display: flex;
  flex-direction: column;
}

.popup-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 14px 8px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  -webkit-app-region: drag;
  cursor: grab;
}

.popup-title {
  font-weight: 600;
  font-size: 13px;
  color: #fff;
}

.popup-close {
  background: none;
  border: none;
  color: #888;
  cursor: pointer;
  font-size: 14px;
  padding: 2px 6px;
  border-radius: 4px;
  line-height: 1;
  -webkit-app-region: no-drag;
}
.popup-close:hover {
  color: #fff;
  background: rgba(255, 255, 255, 0.1);
}

.popup-body {
  padding: 8px 14px 6px;
  flex: 1;
}

.status-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 0;
}

.status-label {
  color: #888;
  flex-shrink: 0;
}

.status-value {
  color: #e0e0e0;
  font-weight: 500;
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 140px;
}

.status-value.running {
  color: #4ade80;
}

.status-value.dim {
  color: #777;
  font-weight: 400;
}

.divider {
  height: 1px;
  background: rgba(255, 255, 255, 0.08);
  margin: 6px 0;
}
</style>
