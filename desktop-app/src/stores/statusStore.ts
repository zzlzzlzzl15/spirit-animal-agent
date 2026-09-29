/**
 * statusStore — 系统状态数据管理。
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface ProviderUsage {
  name: string
  available: boolean
  window?: string
  used_percent?: number
  weekly_used_percent?: number
  reset_seconds?: number
  reason?: string
}

export interface AgentStatus {
  running: boolean
  model: string
  provider: string
  session_id: string
  message_count: number
  tool_count: number
  api_call_count: number
  session_started_at: number
  usage: { ts: number; providers: ProviderUsage[] } | null
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
    session_started_at: 0,
    usage: null,
  })

  const systemMetrics = ref<SystemMetrics>({
    cpu: { percent: 0, count: 0 },
    memory: { total_gb: 0, used_gb: 0, percent: 0 },
    disk: { partitions: [] },
  })

  function syncFromServer(data: any) {
    // 服务端 Agent 字段是扁平合并在顶层的（get_status / init 同一真相源），
    // 同时兼容旧的嵌套 data.agent 形态
    const flat: Partial<AgentStatus> = {}
    for (const k of ['running', 'model', 'provider', 'session_id', 'message_count', 'tool_count', 'api_call_count', 'session_started_at', 'usage'] as const) {
      if (k in data) (flat as any)[k] = data[k]
    }
    if (data.agent) Object.assign(flat, data.agent)
    if (Object.keys(flat).length) {
      agentStatus.value = { ...agentStatus.value, ...flat }
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
