<template>
  <div class="status-popup" :style="posStyle" @click.stop>
    <div class="popup-header">
      <span class="popup-title">Spirit Agent</span>
      <button class="popup-close" @click="$emit('close')">✕</button>
    </div>
    <div class="popup-body">
      <div class="status-row">
        <span class="status-label">状态</span>
        <span class="status-value" :class="{ running: props.status.running }">
          {{ props.status.running ? '运行中' : '空闲' }}
        </span>
      </div>
      <div class="status-row">
        <span class="status-label">模型</span>
        <span class="status-value">{{ props.status.model || '未配置' }}</span>
      </div>
      <div class="status-row">
        <span class="status-label">Provider</span>
        <span class="status-value">{{ props.status.provider || '-' }}</span>
      </div>
      <div class="status-row">
        <span class="status-label">会话</span>
        <span class="status-value">{{ props.status.session_id || '-' }}</span>
      </div>
      <div class="status-row">
        <span class="status-label">消息数</span>
        <span class="status-value">{{ props.status.message_count }}</span>
      </div>
      <div class="status-row">
        <span class="status-label">工具数</span>
        <span class="status-value">{{ props.status.tool_count }}</span>
      </div>
      <div class="status-row">
        <span class="status-label">API 调用</span>
        <span class="status-value">{{ props.status.api_call_count }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  status: {
    running: boolean
    model: string
    provider: string
    session_id: string
    message_count: number
    tool_count: number
    api_call_count: number
  }
  petRect?: { x: number; y: number; w: number; h: number }
}>()

defineEmits<{ (e: 'close'): void }>()

const posStyle = computed(() => {
  return {
    position: 'fixed' as const,
    bottom: '4px',
    left: '50%',
    transform: 'translateX(-50%)',
  }
})
</script>

<style scoped>
.status-popup {
  z-index: 9999;
  background: rgba(15, 15, 25, 0.95);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: 1px solid rgba(251, 146, 60, 0.4);
  border-radius: 10px;
  color: #e0e0e0;
  font-size: 11px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
  min-width: 180px;
  max-width: 220px;
}

.popup-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px 6px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.popup-title {
  font-weight: 600;
  font-size: 12px;
  color: #fff;
}

.popup-close {
  background: none;
  border: none;
  color: #888;
  cursor: pointer;
  font-size: 12px;
  padding: 2px 4px;
  border-radius: 4px;
  line-height: 1;
}
.popup-close:hover {
  color: #fff;
  background: rgba(255, 255, 255, 0.1);
}

.popup-body {
  padding: 6px 12px 10px;
}

.status-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 3px 0;
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
  max-width: 120px;
}

.status-value.running {
  color: #4ade80;
}
</style>
