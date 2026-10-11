<template>
  <!-- 状态弹窗模式 -->
  <StatusPopupPage v-if="isStatusPopup" />

  <!-- 气泡对话模式 -->
  <SpeechBubblePage v-else-if="isSpeechBubble" />

  <!-- CLI 终端模式 -->
  <CLITerminalPage v-else-if="isCLITerminal" />

  <!-- 系统面板模式（独立窗口，不再嵌进桌宠窗口） -->
  <SystemPanel v-else-if="isSystemPanel" @close="onSystemPanelClose" />

  <!-- 桌宠主界面 -->
  <div v-else id="pet-app">
    <!-- 精灵渲染器 -->
    <PetSprite
      ref="spriteRef"
      :state="petStore.currentState"
      :scale="petStore.scale"
      :pet-slug="petStore.activeSlug"
      :paused="isDragging"
      @click="onSpriteClick"
      @contextmenu="onSpriteRightClick"
      @drag-start="onDragStart"
      @drag-move="onDragMove"
      @drag-end="onDragEnd"
      @wheel="onWheel"
    />

    <!-- 右键径向菜单 -->
    <RadialMenu
      v-if="showMenu"
      @close="showMenu = false"
      @action="onMenuAction"
    />

    <!-- 功能面板（按需显示：知识库/语音仍嵌进桌宠；“系统”改为独立窗口） -->
    <KnowledgePanel v-if="activePanel === 'knowledge'" @close="activePanel = ''" />
    <TaskCLI v-if="activePanel === 'cli'" @close="activePanel = ''" />
    <VoiceChat v-if="activePanel === 'voice'" @close="activePanel = ''" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, provide, watch } from 'vue'
import PetSprite from './components/PetSprite.vue'
import StatusPopupPage from './components/StatusPopupPage.vue'
import SpeechBubblePage from './components/SpeechBubblePage.vue'
import CLITerminalPage from './components/CLITerminalPage.vue'
import RadialMenu from './components/RadialMenu.vue'
import KnowledgePanel from './components/KnowledgePanel.vue'
import SystemPanel from './components/SystemPanel.vue'
import TaskCLI from './components/TaskCLI.vue'
import VoiceChat from './components/VoiceChat.vue'
import { usePetStore } from './stores/petStore'
import { useStatusStore } from './stores/statusStore'
import { useWebSocket } from './composables/useWebSocket'

const petStore = usePetStore()
const statusStore = useStatusStore()
const { connect, disconnect, send, connected } = useWebSocket()

// 根据 URL hash 判断显示模式
const isStatusPopup = computed(() => window.location.hash === '#status-popup')
const isSpeechBubble = computed(() => window.location.hash.startsWith('#speech-bubble'))
const isCLITerminal = computed(() => window.location.hash === '#cli-terminal')
const isSystemPanel = computed(() => window.location.hash === '#system-panel')

// 将 WebSocket send 注入给子组件
provide('wsSend', send)
provide('wsConnected', connected)

// UI 状态
const showStatus = ref(false)
const showMenu = ref(false)
const activePanel = ref('')
const spriteRef = ref()

// 拖拽状态 — 使用屏幕绝对坐标，避免窗口移动后坐标系漂移
let isDragging = ref(false)
let dragStartScreenX = 0
let dragStartScreenY = 0
let moveThrottleTimer: ReturnType<typeof setTimeout> | null = null
let pendingDx = 0
let pendingDy = 0

// ── 交互态捕获开关 ────────────────────────────────────────
// 透明区的点击穿透与狐狸区域的捕获收回，由主进程按光标位置轮询判定
// （穿透态下渲染层收不到 mousemove，无法自行判定命中）。菜单/面板展开或
// 拖拽期间整窗必须收回捕获，否则点不到菜单项、拖拽会中途断链。
function setForceCapture(force: boolean): void {
  window.electronAPI?.setForceCapture?.(force)
}

/** 交还命中轮询；若仍有菜单/面板打开则保持整窗捕获 */
function releaseForceCapture(): void {
  setForceCapture(Boolean(showMenu.value || activePanel.value))
}

// 菜单/面板开合立即切换捕获范围（不等下一轮轮询）
watch([showMenu, activePanel], ([menu, panel]) => {
  setForceCapture(Boolean(menu || panel))
})

// 随机气泡定时器
let bubbleTimer: ReturnType<typeof setInterval> | null = null

// ── 合成层停绘自愈心跳 ────────────────────────────────────
// Windows 透明窗口的合成层可能停止绘制（渲染进程活着但狐狸不显示）。
// rAF 帧数在合成停止时不再增长；主进程对比心跳帧数判定停绘并强制重建合成层。
let rafFrames = 0
let heartbeatTimer: ReturnType<typeof setInterval> | null = null
function rafTick() {
  rafFrames++
  requestAnimationFrame(rafTick)
}

// 趣味对话列表
const IDLE_BUBBLES = [
  '今天也要加油哦~ ✨',
  '有什么需要帮忙的吗？',
  '...zzZ',
  '我在这等你回来~',
  '想聊点什么吗？',
  '系统一切正常！',
  '嘿，点我看看状态~',
]

onMounted(async () => {
  // 与主进程同步初始捕获态：页面重载/渲染进程自愈后，主进程可能残留
  // “强制捕获”标记，会把整窗鼠标捕获卡死（透明区又开始吞屏幕点击）
  releaseForceCapture()

  // 连接 WebSocket
  await connect('ws://127.0.0.1:9877', {
    onEvent(event: string, data: any) {
      if (event === 'state_change') {
        petStore.setState(data.new_state)
      } else if (event === 'pet_switch') {
        // 服务端广播的宠物切换 → 同步前端精灵图
        petStore.activeSlug = data.new_slug || ''
      } else if (event === 'init') {
        petStore.syncFromServer(data)
        statusStore.syncFromServer(data)
      } else if (event === 'autonomy_decision') {
        // 需拍板的 HITL 决策（C-P1.2：按 kind 区分——工具失败/经验确认/provider
        // 切换/探索分叉）→ 存入 store（SystemPanel 渲染应答），并让桌宠冒个对应气泡
        // 提醒用户去「系统」面板拍板（超时未答则后端 fail-open 自主继续）
        statusStore.setDecision(data)
        if (window.electronAPI?.showBubble && !activePanel.value) {
          const kind = data?.kind || 'exploration'
          const bubbles: Record<string, string> = {
            tool_failure: '🔧 工具连续失败，打开「系统」面板帮我拍板~',
            experience_review: '💾 有条经验想记下，打开「系统」面板确认一下~',
            provider_switch: '🔀 当前模型不给力，要换一个吗？打开「系统」面板~',
            exploration: '🤔 探索遇到选择，打开「系统」面板帮我拍板~',
          }
          window.electronAPI.showBubble(bubbles[kind] || bubbles.exploration)
        }
      } else if (event === 'progress') {
        // C-P1.1 长任务进度推送 → 桌宠冒泡（复用 SpeechBubblePage 独立窗口）。
        // 只显示阶段切换：task_start / tool_call / task_complete / task_aborted；
        // thinking / thinking_end / tool_result 不显示 — 太频繁会烦，4s 自动关 + 重
        // 启气泡即可。
        const phase = data?.phase
        let text = ''
        if (phase === 'task_start') {
          text = `📋 ${data.summary || '开始任务'}`
        } else if (phase === 'tool_call') {
          text = `🔧 调用: ${data.summary || '工具'}`
        } else if (phase === 'task_complete') {
          const iters = data?.iterations ?? 0
          const tcs = data?.tool_calls ?? 0
          text = `✨ 完成 (${iters} 轮${tcs > 0 ? `, ${tcs} 个工具` : ''})`
        } else if (phase === 'task_aborted') {
          text = `⛔ ${data.summary || '任务中断'}`
        }
        if (text && window.electronAPI?.showBubble) {
          window.electronAPI.showBubble(text)
        }
      }
    },
  })

  // 定期随机气泡（通过独立窗口显示）
  bubbleTimer = setInterval(() => {
    if (!showStatus.value && !showMenu.value && !activePanel.value) {
      const text = IDLE_BUBBLES[Math.floor(Math.random() * IDLE_BUBBLES.length)]
      if (window.electronAPI?.showBubble) {
        window.electronAPI.showBubble(text)
      }
    }
  }, 30000)

  // 仅桌宠主视图：启动合成心跳（气泡/状态/CLI 窗口不需要）
  if (!isStatusPopup.value && !isSpeechBubble.value && !isCLITerminal.value) {
    requestAnimationFrame(rafTick)
    heartbeatTimer = setInterval(() => {
      window.electronAPI?.petHeartbeat?.(rafFrames)
    }, 10000)
  }

  // 托盘“切换宠物”→ 转发给服务端（服务端持久化并广播 pet_switch，
  // 所有窗口经 onEvent 同步精灵图）
  if (window.electronAPI?.onSwitchPet) {
    window.electronAPI.onSwitchPet((d: { slug: string }) => {
      send('switch_pet', { slug: d.slug }).catch(() => {})
    })
  }
})

onUnmounted(() => {
  disconnect()
  if (bubbleTimer) clearInterval(bubbleTimer)
  if (heartbeatTimer) clearInterval(heartbeatTimer)
})

// ── 交互事件 ──────────────────────────────────────────────

function onSpriteClick(e: MouseEvent) {
  console.log('[App] leftClick, isDragging:', isDragging.value)
  if (isDragging.value) return
  // 左键 → 圆形功能菜单（知识库 / 系统 / CLI / 语音 4 个小气泡环绕桌宠）
  showMenu.value = !showMenu.value
  showStatus.value = false
  console.log('[App] showMenu:', showMenu.value)
}

function onSpriteRightClick(e: MouseEvent) {
  console.log('[App] rightClick')
  // 右键 → 打开独立状态弹窗（运行状态 / 模型 / provider / 会话 / 消息数 / 工具数 / API 调用 / 额度）
  if (window.electronAPI?.openStatusWindow) {
    window.electronAPI.openStatusWindow()
  }
}

/**
 * 独立系统面板窗口的关闭处理：SystemPanel 只能发 close emit，
 * 这里是独立窗口模式，所以要主动通知主进程关窗（关后 hash 路由也就没了）。
 */
function onSystemPanelClose() {
  window.electronAPI?.closeSystemWindow?.()
}

function onMenuAction(action: string) {
  showMenu.value = false
  switch (action) {
    case 'knowledge':
      // 知识库 → 用浏览器打开 Memora
      if (window.electronAPI?.openMemora) {
        window.electronAPI.openMemora()
      }
      break
    case 'system':
      // 系统面板 → 独立窗口。
      // 桌宠窗口只有 148×160，嵌不下 340×460 的 SystemPanel，原本被裁到只看得到一角。
      // 改为与 CLI 终端同模式的独立 BrowserWindow，可独立拖动、可独立关闭。
      if (window.electronAPI?.openSystemWindow) {
        window.electronAPI.openSystemWindow()
      }
      break
    case 'cli':
      // CLI → 打开独立的 CMD 终端窗口
      if (window.electronAPI?.openCLITerminal) {
        window.electronAPI.openCLITerminal()
      }
      break
    case 'voice':
      activePanel.value = 'voice'
      break
  }
}

function onDragStart(e: MouseEvent) {
  isDragging.value = false
  // 拖拽期间整窗收回捕获：窗口跟着光标移动，命中轮询可能瞬间判定脱靶
  setForceCapture(true)
  // 用屏幕绝对坐标作为锚点，窗口移动不影响后续 delta 计算
  dragStartScreenX = e.screenX
  dragStartScreenY = e.screenY
}

function onDragMove(e: MouseEvent) {
  // 基于屏幕绝对坐标计算增量，稳定不漂移
  const dx = e.screenX - dragStartScreenX
  const dy = e.screenY - dragStartScreenY
  if (Math.abs(dx) > 3 || Math.abs(dy) > 3) {
    isDragging.value = true
  }
  if (isDragging.value && window.electronAPI) {
    pendingDx += dx
    pendingDy += dy
    // 更新锚点到当前屏幕位置
    dragStartScreenX = e.screenX
    dragStartScreenY = e.screenY
    if (!moveThrottleTimer) {
      moveThrottleTimer = setTimeout(() => {
        window.electronAPI!.moveWindow(pendingDx, pendingDy)
        pendingDx = 0
        pendingDy = 0
        moveThrottleTimer = null
      }, 16)
    }
  }
}

function onDragEnd() {
  // 发送残余位移
  if (isDragging.value && window.electronAPI && (pendingDx || pendingDy)) {
    window.electronAPI.moveWindow(pendingDx, pendingDy)
    pendingDx = 0
    pendingDy = 0
  }
  if (moveThrottleTimer) {
    clearTimeout(moveThrottleTimer)
    moveThrottleTimer = null
  }
  releaseForceCapture()
  setTimeout(() => { isDragging.value = false }, 50)
}

function onWheel(e: WheelEvent) {
  e.preventDefault()
  const delta = e.deltaY > 0 ? -0.05 : 0.05
  const newScale = Math.max(0.2, Math.min(3.0, petStore.scale + delta))
  petStore.setScale(newScale)
  // 同步服务端 prefs（重启后的唯一真相源），避免重启后缩放回滚
  send('set_scale', { scale: newScale }).catch(() => {})
}
</script>

<style>
#pet-app {
  position: relative;
  width: 100vw;
  height: 100vh;
  background: transparent;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
}
</style>
