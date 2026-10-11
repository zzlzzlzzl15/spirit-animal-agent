<template>
  <div class="panel system-panel">
    <div class="panel-header">
      <span class="panel-title">📊 系统状态</span>
      <button class="panel-close" @click="$emit('close')">✕</button>
    </div>
    <div class="panel-body">
      <div v-if="refreshError" class="error-msg">{{ refreshError }}</div>

      <!-- 🧭 自主探索：给一个方向 → 自由探索 -->
      <div class="autonomy-section">
        <div class="autonomy-head">
          <span class="autonomy-title">🧭 自主探索</span>
          <span class="autonomy-badge" :class="{ on: autonomy.running }">
            {{ autonomy.running ? '运行中' : '未启动' }}
          </span>
        </div>

        <!-- HITL 决策询问：按 kind 分类渲染（工具失败/经验确认/provider 切换/探索分叉） -->
        <div v-if="decision" class="decision-box" :class="'kind-' + decisionKind">
          <div class="decision-head">
            <span class="decision-icon">{{ decisionMeta.icon }}</span>
            <span class="decision-title">{{ decisionMeta.title }}</span>
          </div>
          <div class="decision-q">{{ decision.question }}</div>
          <div v-if="decision.context" class="decision-ctx">{{ decision.context }}</div>
          <div class="decision-opts">
            <button
              v-for="(opt, i) in decision.options"
              :key="i"
              class="decision-opt"
              @click="answerDecision(opt)"
            >{{ opt }}</button>
          </div>
        </div>

        <!-- 方向输入 -->
        <div class="direction-row">
          <input
            v-model="direction"
            class="direction-input"
            type="text"
            placeholder="给个方向，如：金融数据分析…"
            @keyup.enter="startExplore"
            :disabled="sending"
          />
          <button
            class="explore-btn"
            @click="startExplore"
            :disabled="sending || !direction.trim()"
          >{{ sending ? '…' : '开始探索' }}</button>
        </div>
        <div class="autonomy-hint">
          <span v-if="autonomy.domain">当前方向：{{ autonomy.domain }}</span>
          <span v-else class="text-gray">未设方向 · 基于记忆自由提议</span>
          <span v-if="autonomy.cycle_no"> · 已探索 {{ autonomy.cycle_no }} 轮</span>
        </div>

        <!-- 近期探索进展 -->
        <div v-if="recentCycles.length" class="cycle-list">
          <div v-for="c in recentCycles" :key="c.cycle_no" class="cycle-item">
            <span class="cycle-no">#{{ c.cycle_no }}</span>
            <span class="cycle-status" :class="statusClass(c.status)">{{ statusText(c.status) }}</span>
            <span class="cycle-task" :title="c.task">{{ c.task || '（空转）' }}</span>
          </div>
        </div>
      </div>

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
import { storeToRefs } from 'pinia'
import { useStatusStore } from '../stores/statusStore'

defineEmits<{
  (e: 'close'): void
}>()

const wsSend = inject<Function>('wsSend')
const statusStore = useStatusStore()
const refreshError = ref('')

// 自主探索：方向输入 + 发送态；进展/决策从 store 响应式读取（决策事件由 App.vue 路由入 store）
const { autonomy, pendingDecision: decision } = storeToRefs(statusStore)
const direction = ref('')
const sending = ref(false)
const recentCycles = computed(() => (autonomy.value.cycles || []).slice(-6).reverse())

// C-P1.2：按 HITL 决策 kind 分类的图标 + 面板标题（与后端 DecisionKind 对齐）
const decisionKind = computed<string>(() => decision.value?.kind || 'exploration')
const DECISION_META: Record<string, { icon: string; title: string; bubble: string }> = {
  tool_failure:      { icon: '🔧', title: '工具连续失败', bubble: '🔧 工具连续失败，打开「系统」面板帮我拍板~' },
  experience_review: { icon: '💾', title: '经验沉淀确认', bubble: '💾 有条经验想记下，打开「系统」面板确认一下~' },
  provider_switch:   { icon: '🔀', title: '模型切换确认', bubble: '🔀 当前模型不给力，要换一个吗？打开「系统」面板~' },
  exploration:       { icon: '🤔', title: '探索分叉',     bubble: '🤔 探索遇到选择，打开「系统」面板帮我拍板~' },
}
const decisionMeta = computed(() => DECISION_META[decisionKind.value] || DECISION_META.exploration)

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

// 自主探索进展轮询（后端 autonomy_status → {state:{...}}）
async function fetchAutonomy() {
  if (!wsSend) return
  try {
    const data = await wsSend('autonomy_status')
    if (data && !data.error) statusStore.syncAutonomy(data)
  } catch { /* 未挂载 autonomy/后端未就绪：静默降级 */ }
}

function statusClass(s: string): string {
  if (s === 'CONVERGED' || s === 'READY_FOR_RETRY') return 'ok'
  if (s === 'STALLED') return 'warn'
  if (s === 'IDLE') return 'dim'
  return ''
}

function statusText(s: string): string {
  const map: Record<string, string> = {
    CONVERGED: '收敛', STALLED: '停滞', IDLE: '空转', READY_FOR_RETRY: '待重试',
  }
  return map[s] || s || '-'
}

// 给一个方向 → 立即自由探索（autonomy_set_domain, run_now）
async function startExplore() {
  const dir = direction.value.trim()
  if (!dir || sending.value || !wsSend) return
  sending.value = true
  try {
    const res = await wsSend('autonomy_set_domain', { domain: dir, run_now: true })
    if (res && res.ok) {
      direction.value = ''
      refreshError.value = ''
      await fetchAutonomy()
    } else if (res && res.error) {
      refreshError.value = `设置探索方向失败：${res.error}`
    }
  } catch (err: any) {
    refreshError.value = err.message || '设置探索方向失败'
  } finally {
    sending.value = false
  }
}

// 应答 HITL 决策（点选选项 → autonomy_answer）
async function answerDecision(text: string) {
  if (!wsSend) return
  try {
    await wsSend('autonomy_answer', { text })
  } catch { /* 忽略应答失败 */ }
  statusStore.clearDecision()
}

onMounted(() => {
  fetchSystemStatus()
  fetchAutonomy()
  refreshTimer = setInterval(() => {
    fetchSystemStatus()
    fetchAutonomy()
  }, 5000)
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

/* ── 自主探索 ─────────────────────── */
.autonomy-section {
  margin-bottom: 14px;
  padding-bottom: 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.autonomy-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.autonomy-title {
  font-size: 12px;
  font-weight: 600;
  color: #fff;
}
.autonomy-badge {
  font-size: 10px;
  padding: 1px 8px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.08);
  color: #888;
}
.autonomy-badge.on {
  background: rgba(74, 222, 128, 0.16);
  color: #4ade80;
}
.direction-row {
  display: flex;
  gap: 6px;
}
.direction-input {
  flex: 1;
  min-width: 0;
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 8px;
  color: #e0e0e0;
  font-size: 11px;
  padding: 6px 8px;
  outline: none;
}
.direction-input:focus {
  border-color: rgba(251, 146, 60, 0.7);
}
.direction-input:disabled {
  opacity: 0.5;
}
.explore-btn {
  flex-shrink: 0;
  background: linear-gradient(90deg, #fb923c, #f97316);
  border: none;
  border-radius: 8px;
  color: #1a1a1a;
  font-size: 11px;
  font-weight: 600;
  padding: 6px 10px;
  cursor: pointer;
}
.explore-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.autonomy-hint {
  font-size: 10px;
  color: #999;
  margin-top: 6px;
  line-height: 1.4;
}
.cycle-list {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.cycle-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 10px;
}
.cycle-no {
  color: #666;
  flex-shrink: 0;
}
.cycle-status {
  flex-shrink: 0;
  font-size: 9px;
  padding: 0 5px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.08);
  color: #aaa;
}
.cycle-status.ok { background: rgba(74, 222, 128, 0.16); color: #4ade80; }
.cycle-status.warn { background: rgba(251, 146, 60, 0.16); color: #fb923c; }
.cycle-status.dim { color: #666; }
.cycle-task {
  flex: 1;
  min-width: 0;
  color: #ccc;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.decision-box {
  background: rgba(251, 146, 60, 0.1);
  border: 1px solid rgba(251, 146, 60, 0.4);
  border-radius: 10px;
  padding: 8px 10px;
  margin-bottom: 10px;
}
/* C-P1.2：按 kind 区分配色（工具失败=橙/经验=绿/provider=蓝/探索=黄） */
.decision-box.kind-tool_failure      { background: rgba(251, 146, 60, 0.1); border-color: rgba(251, 146, 60, 0.45); }
.decision-box.kind-experience_review { background: rgba(74, 222, 128, 0.1); border-color: rgba(74, 222, 128, 0.4); }
.decision-box.kind-provider_switch   { background: rgba(96, 165, 250, 0.1); border-color: rgba(96, 165, 250, 0.45); }
.decision-box.kind-exploration       { background: rgba(250, 204, 21, 0.1); border-color: rgba(250, 204, 21, 0.4); }
.decision-head {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-bottom: 5px;
}
.decision-icon { font-size: 13px; }
.decision-title {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.5px;
  color: #ccc;
  text-transform: uppercase;
}
.decision-q {
  font-size: 11px;
  color: #ffd9b3;
  font-weight: 600;
  margin-bottom: 4px;
  line-height: 1.4;
}
.decision-ctx {
  font-size: 10px;
  color: #999;
  margin-bottom: 6px;
  line-height: 1.4;
  max-height: 48px;
  overflow: hidden;
}
.decision-opts {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.decision-opt {
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 6px;
  color: #e0e0e0;
  font-size: 10px;
  padding: 5px 8px;
  cursor: pointer;
  text-align: left;
}
.decision-opt:hover {
  background: rgba(251, 146, 60, 0.2);
  border-color: rgba(251, 146, 60, 0.5);
}
</style>
