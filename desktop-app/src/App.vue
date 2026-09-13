<template>
  <!-- 状态弹窗模式 -->
  <StatusPopupPage v-if="isStatusPopup" />
  
  <!-- 气泡对话模式 -->
  <SpeechBubblePage v-else-if="isSpeechBubble" />
  
  <!-- CLI 终端模式 -->
  <CLITerminalPage v-else-if="isCLITerminal" />
  
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

    <!-- 功能面板（按需显示） -->
    <KnowledgePanel v-if="activePanel === 'knowledge'" @close="activePanel = ''" />
    <SystemPanel v-if="activePanel === 'system'" @close="activePanel = ''" />
    <TaskCLI v-if="activePanel === 'cli'" @close="activePanel = ''" />
    <VoiceChat v-if="activePanel === 'voice'" @close="activePanel = ''" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, provide } from 'vue'
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

// 随机气泡定时器
let bubbleTimer: ReturnType<typeof setInterval> | null = null

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
})

// ── 交互事件 ──────────────────────────────────────────────

function onSpriteClick(e: MouseEvent) {
  console.log('[App] leftClick, isDragging:', isDragging.value)
  if (isDragging.value) return
  // 左键 → 圆形功能菜单
  showMenu.value = !showMenu.value
  showStatus.value = false
  console.log('[App] showMenu:', showMenu.value)
}

function onSpriteRightClick(e: MouseEvent) {
  console.log('[App] rightClick')
  // 右键 → 打开独立状态弹窗窗口
  if (window.electronAPI?.openStatusWindow) {
    window.electronAPI.openStatusWindow()
  }
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
      activePanel.value = 'system'
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
