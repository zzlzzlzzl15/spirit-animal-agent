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

// 一轮自主探索的结果（对齐后端 ExploreCycle.to_dict）
export interface AutonomyCycle {
  cycle_no: number
  task: string
  status: string
  decision_source: string
  decision_text: string
  note_path: string
  summary: string
}

// 常驻自主探索状态（对齐后端 AutonomyLoop.state）
export interface AutonomyState {
  running: boolean
  domain: string
  cycle_no: number
  cycles: AutonomyCycle[]
  run_on_start?: boolean
  interval_seconds?: number
}

// HITL 决策询问（对齐后端 autonomy_decision 广播事件 payload）
export type DecisionKind =
  | 'exploration'        // 自主探索分叉/停滞
  | 'tool_failure'       // 工具连续失败
  | 'experience_review'  // 经验沉淀确认
  | 'provider_switch'    // 跨 provider 切换

export interface AutonomyDecision {
  id: number
  kind: DecisionKind
  question: string
  options: string[]
  context: string
  is_fork: boolean
  timeout: number
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

  // 自主探索：当前状态 + 待应答的 HITL 决策（气泡询问）
  const autonomy = ref<AutonomyState>({
    running: false,
    domain: '',
    cycle_no: 0,
    cycles: [],
  })
  const pendingDecision = ref<AutonomyDecision | null>(null)

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

  // 用 autonomy_status 响应（{state:{...}}）或直接 state 对象刷新探索进展
  function syncAutonomy(data: any) {
    const s = data && data.state ? data.state : data
    if (!s || typeof s !== 'object') return
    autonomy.value = {
      running: Boolean(s.running),
      domain: typeof s.domain === 'string' ? s.domain : '',
      cycle_no: Number(s.cycle_no || 0),
      cycles: Array.isArray(s.cycles) ? s.cycles : [],
      run_on_start: Boolean(s.run_on_start),
      interval_seconds: s.interval_seconds != null ? Number(s.interval_seconds) : undefined,
    }
  }

  function setDecision(data: any) {
    if (!data || typeof data !== 'object') return
    pendingDecision.value = {
      id: Number(data.id || 0),
      kind: (String(data.kind || 'exploration') as DecisionKind),
      question: String(data.question || ''),
      options: Array.isArray(data.options) ? data.options : [],
      context: String(data.context || ''),
      is_fork: Boolean(data.is_fork),
      timeout: Number(data.timeout || 0),
    }
  }

  function clearDecision() {
    pendingDecision.value = null
  }

  return {
    agentStatus,
    systemMetrics,
    autonomy,
    pendingDecision,
    syncFromServer,
    syncAutonomy,
    setDecision,
    clearDecision,
  }
})
