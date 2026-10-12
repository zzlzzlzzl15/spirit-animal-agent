<template>
  <div class="radial-menu" @click.stop>
    <!-- 中心遮罩（点击空白关闭） -->
    <div class="radial-backdrop" @click="$emit('close')"></div>

    <!-- 4 个功能按钮：左上 90° 扇形分布（正上 → 左上 → 正左） -->
    <button
      v-for="(item, idx) in items"
      :key="item.action"
      class="radial-btn"
      :class="{ visible: showItems }"
      :style="itemStyle(idx)"
      @click="onAction(item.action)"
    >
      <span class="radial-icon">{{ item.icon }}</span>
      <span class="radial-label">{{ item.label }}</span>
    </button>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'

const props = withDefaults(defineProps<{
  /** 中心气泡直径（用于计算外推半径） */
  bubbleSize?: number
}>(), {
  bubbleSize: 28,
})

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'action', action: string): void
}>()

const showItems = ref(false)

const items = [
  { action: 'knowledge', icon: '📚', label: '知识库' },
  { action: 'system', icon: '📊', label: '系统' },
  { action: 'cli', icon: '⌨️', label: 'CLI' },
  { action: 'voice', icon: '🎤', label: '语音' },
]

// 按钮尺寸（小巧精致）
const BTN = 30          // 按钮直径
const GAP = 10          // 按钮与气泡的间距

/**
 * 4 个按钮的角度：从正上方（270°）开始，向左每 40° 一个。
 * 270°（正上）→ 230° → 190° → 150°，总弧 120°，跨越左上至左下偏左。
 */
const ANGLES_DEG = [270, 230, 190, 150]

/** 按钮中心点距离气泡中心的距离 */
const radius = computed(() => props.bubbleSize / 2 + GAP + BTN / 2)

function itemStyle(idx: number) {
  const angleDeg = ANGLES_DEG[idx]
  const angleRad = (angleDeg * Math.PI) / 180
  const x = Math.cos(angleRad) * radius.value
  const y = Math.sin(angleRad) * radius.value
  return {
    '--tx': `${x}px`,
    '--ty': `${y}px`,
    transform: showItems.value
      ? `translate(${x}px, ${y}px) scale(1)`
      : 'translate(0, 0) scale(0)',
    transitionDelay: showItems.value ? `${idx * 50}ms` : '0ms',
  }
}

function onAction(action: string) {
  emit('action', action)
}

/**
 * 把当前菜单按钮的渲染层视口坐标推送给主进程。
 * 主进程会加窗口偏移转屏幕绝对坐标，让 forceCapture=true 期间：
 * - 光标在按钮或 label 上 → 捕获（可点菜单项）
 * - 光标不在 → 透传（避免点不到屏幕下层按钮的"空气墙"）
 */
function reportButtonBoxes(): void {
  const buttons = document.querySelectorAll<HTMLElement>('.radial-btn')
  const boxes: Array<{ x: number; y: number; w: number; h: number }> = []
  buttons.forEach((el) => {
    const r = el.getBoundingClientRect()
    if (r.width > 0 && r.height > 0) {
      boxes.push({ x: r.left, y: r.top, w: r.width, h: r.height })
    }
  })
  window.electronAPI?.setMenuButtonBoxes?.(boxes)
}

onMounted(() => {
  // 延迟触发动画
  requestAnimationFrame(() => {
    showItems.value = true
    // 4 个按钮 0/50/100/150ms 错峰 + 0.3s transition
    setTimeout(reportButtonBoxes, 250)
  })
})

onUnmounted(() => {
  // 菜单卸载时清空按钮区域 → 主进程恢复仅中心气泡捕获
  window.electronAPI?.setMenuButtonBoxes?.([])
})
</script>

<style scoped>
.radial-menu {
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  z-index: 200;
  pointer-events: none;
}

.radial-backdrop {
  position: fixed;
  top: 0;
  left: 0;
  width: 100vw;
  height: 100vh;
  pointer-events: auto;
}

.radial-btn {
  position: absolute;
  top: 50%;
  left: 50%;
  width: 30px;
  height: 30px;
  margin-left: -15px;
  margin-top: -15px;
  border-radius: 50%;
  background: rgba(20, 20, 30, 0.92);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  border: 1px solid rgba(251, 146, 60, 0.55);
  color: #fff;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: auto;
  transition: all 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.4);
  z-index: 201;
}

.radial-btn:hover {
  background: rgba(255, 140, 50, 0.92);
  border-color: rgba(255, 200, 130, 0.85);
  transform: translate(var(--tx, 0), var(--ty, 0)) scale(1.15) !important;
}

.radial-icon {
  font-size: 14px;
  line-height: 1;
}

/* 功能名称 tooltip：紧贴按钮右侧，垂直居中 */
.radial-label {
  position: absolute;
  left: calc(100% + 8px);   /* 按钮右侧 8px 间距 */
  top: 50%;
  transform: translateY(-50%);
  white-space: nowrap;
  font-size: 11px;
  font-weight: 500;
  color: #fff;
  background: rgba(20, 20, 30, 0.88);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  padding: 3px 8px;
  border-radius: 10px;
  border: 1px solid rgba(251, 146, 60, 0.3);
  pointer-events: none;     /* label 不拦截鼠标，避免 hit box 抖动 */
  letter-spacing: 0.3px;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.3);
  opacity: 0.9;
}

.radial-btn:hover .radial-label {
  opacity: 1;
  background: rgba(255, 140, 50, 0.95);
  border-color: rgba(255, 200, 130, 0.7);
}
</style>
