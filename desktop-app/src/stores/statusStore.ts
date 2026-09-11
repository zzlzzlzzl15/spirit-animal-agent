/**
 * statusStore — 系统状态数据管理。
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface AgentStatus {
  running: boolean
  model: string
  provider: string
  session_id: string
  message_count: number
  tool_count: number
  api_call_count: number
}

export interface SystemMetrics {
  cpu: { percent: number; count: number }
  memory: { total_gb: number; used_gb: number; percent: number }
  disk: { partitions: any[] }
}

export const useStatusStore = defineStore('status', () => {
  const agentStatus = ref<AgentStatus>({
    running: false,
    model: '',
    provider: '',
    session_id: '',
    message_count: 0,
    tool_count: 0,
    api_call_count: 0,
  })

  const systemMetrics = ref<SystemMetrics>({
    cpu: { percent: 0, count: 0 },
    memory: { total_gb: 0, used_gb: 0, percent: 0 },
    disk: { partitions: [] },
  })

  function syncFromServer(data: any) {
    if (data.agent) {
      agentStatus.value = { ...agentStatus.value, ...data.agent }
    }
    if (data.cpu) {
      systemMetrics.value.cpu = { ...systemMetrics.value.cpu, ...data.cpu }
    }
    if (data.memory) {
      systemMetrics.value.memory = { ...systemMetrics.value.memory, ...data.memory }
    }
  }

  return {
    agentStatus,
    systemMetrics,
    syncFromServer,
  }
})
