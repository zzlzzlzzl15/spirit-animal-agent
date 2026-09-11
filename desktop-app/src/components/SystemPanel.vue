<template>
  <div class="panel system-panel">
    <div class="panel-header">
      <span class="panel-title">📊 系统状态</span>
      <button class="panel-close" @click="$emit('close')">✕</button>
    </div>
    <div class="panel-body">
      <div v-if="refreshError" class="error-msg">{{ refreshError }}</div>
      <!-- CPU -->
      <div class="metric-group">
        <div class="metric-label">CPU</div>
        <div class="metric-bar">
          <div class="metric-fill cpu" :style="{ width: cpuPercent + '%' }"></div>
        </div>
        <div class="metric-value">{{ cpuPercent }}%</div>
      </div>

      <!-- 内存 -->
      <div class="metric-group">
        <div class="metric-label">内存</div>
        <div class="metric-bar">
          <div class="metric-fill memory" :style="{ width: memPercent + '%' }"></div>
        </div>
        <div class="metric-value">{{ memPercent }}%</div>
      </div>

      <!-- Agent 信息 -->
      <div class="info-section">
        <div class="info-title">Agent 信息</div>
        <div class="info-row">
          <span>状态</span>
          <span :class="{ 'text-green': agent.running, 'text-gray': !agent.running }">
            {{ agent.running ? '运行中' : '空闲' }}
          </span>
        </div>
        <div class="info-row">
          <span>模型</span>
          <span>{{ agent.model || '未配置' }}</span>
        </div>
        <div class="info-row">
          <span>Provider</span>
          <span>{{ agent.provider || '-' }}</span>
        </div>
        <div class="info-row">
          <span>消息数</span>
          <span>{{ agent.message_count }}</span>
        </div>
        <div class="info-row">
          <span>工具数</span>
          <span>{{ agent.tool_count }}</span>
        </div>
        <div class="info-row">
          <span>API 调用</span>
          <span>{{ agent.api_call_count }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, inject, ref } from 'vue'
import { useStatusStore } from '../stores/statusStore'

defineEmits<{
  (e: 'close'): void
}>()

const wsSend = inject<Function>('wsSend')
const statusStore = useStatusStore()
const refreshError = ref('')

const cpuPercent = computed(() => {
  const p = statusStore.systemMetrics.cpu.percent
  return p >= 0 ? p : 0
})

const memPercent = computed(() => {
  const p = statusStore.systemMetrics.memory.percent
  return p >= 0 ? p : 0
})

const agent = computed(() => statusStore.agentStatus)

// 定期刷新系统状态
let refreshTimer: ReturnType<typeof setInterval> | null = null

async function fetchSystemStatus() {
  if (!wsSend) {
    refreshError.value = 'WebSocket 未连接'
    return
  }
  try {
    const data = await wsSend('get_system_status')
    if (data.error) {
      refreshError.value = data.error
    } else {
      refreshError.value = ''
      statusStore.syncFromServer(data)
    }
  } catch (err: any) {
    refreshError.value = err.message || '获取失败'
  }
}

onMounted(() => {
  fetchSystemStatus()
  refreshTimer = setInterval(fetchSystemStatus, 5000)
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<style scoped>
.panel {
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 340px;
  max-height: 460px;
  background: rgba(20, 20, 30, 0.92);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 16px;
  color: #e0e0e0;
  z-index: 500;
  box-shadow: 0 12px 48px rgba(0, 0, 0, 0.5);
  display: flex;
  flex-direction: column;
  animation: panel-in 0.25s ease-out;
}

@keyframes panel-in {
  from { opacity: 0; transform: translate(-50%, -50%) scale(0.9); }
  to   { opacity: 1; transform: translate(-50%, -50%) scale(1); }
}

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 14px 16px 10px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.panel-title {
  font-weight: 600;
  font-size: 14px;
  color: #fff;
}

.panel-close {
  background: none;
  border: none;
  color: #888;
  cursor: pointer;
  font-size: 16px;
  padding: 4px 6px;
  border-radius: 6px;
}
.panel-close:hover {
  color: #fff;
  background: rgba(255, 255, 255, 0.1);
}

.panel-body {
  padding: 12px 16px 16px;
  overflow-y: auto;
}

.metric-group {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.metric-label {
  width: 36px;
  font-size: 11px;
  color: #999;
  flex-shrink: 0;
}

.metric-bar {
  flex: 1;
  height: 8px;
  background: rgba(255, 255, 255, 0.08);
  border-radius: 4px;
  overflow: hidden;
}

.metric-fill {
  height: 100%;
  border-radius: 4px;
  transition: width 0.5s ease;
}
.metric-fill.cpu {
  background: linear-gradient(90deg, #4ade80, #22c55e);
}
.metric-fill.memory {
  background: linear-gradient(90deg, #60a5fa, #3b82f6);
}

.metric-value {
  width: 36px;
  text-align: right;
  font-size: 11px;
  font-weight: 600;
  color: #e0e0e0;
}

.info-section {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
}

.info-title {
  font-size: 12px;
  font-weight: 600;
  color: #fff;
  margin-bottom: 8px;
}

.info-row {
  display: flex;
  justify-content: space-between;
  padding: 3px 0;
  font-size: 11px;
}
.info-row span:first-child {
  color: #999;
}

.text-green { color: #4ade80; }
.text-gray { color: #666; }

.error-msg {
  text-align: center;
  color: #f87171;
  font-size: 11px;
  padding: 8px 0;
}
</style>
