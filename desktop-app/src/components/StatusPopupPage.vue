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
        <span class="status-value session-id">{{ status.session_id || '-' }}</span>
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
    </div>
  </div>
</template>

<script setup lang="ts">
import { useStatusStore } from '../stores/statusStore'
import { useWebSocket } from '../composables/useWebSocket'
import { onMounted } from 'vue'

const statusStore = useStatusStore()
const { connect } = useWebSocket()

const status = statusStore.agentStatus

function closeWindow() {
  if (window.electronAPI?.closeStatusWindow) {
    window.electronAPI.closeStatusWindow()
  }
}

onMounted(async () => {
  // 连接 WebSocket 获取实时数据
  await connect('ws://127.0.0.1:9877', {
    onEvent(event: string, data: any) {
      if (event === 'init') {
        statusStore.syncFromServer(data)
      }
    },
  })
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

.status-value.session-id {
  font-size: 10px;
  font-family: monospace;
}

.divider {
  height: 1px;
  background: rgba(255, 255, 255, 0.08);
  margin: 6px 0;
}
</style>
