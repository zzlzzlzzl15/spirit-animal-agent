<template>
  <!-- 圆形气泡容器：CSS 圆角 + 阴影 + 内部画狐狸精灵 -->
  <div
    ref="bubbleRef"
    class="pet-bubble"
    :class="{ dragging: props.paused }"
    :style="bubbleStyle"
    @mousedown="onMouseDown"
    @click="handleClick"
    @contextmenu.prevent="handleContextMenu"
    @wheel="onWheel"
  >
    <canvas
      ref="canvasRef"
      class="pet-canvas"
      :width="displayW"
      :height="displayH"
      :style="{ width: displayW + 'px', height: displayH + 'px' }"
    />
    <!-- 圆形高光叠加层：让气泡有点 3D 感（可选，不抢狐狸的可见性） -->
    <div class="pet-bubble-shine"></div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { usePetAnimation } from '../composables/usePetAnimation'

const props = defineProps<{
  /** 气泡直径（CSS px），由 App.vue 根据屏幕尺寸计算 */
  size: number
  state: string
  petSlug: string
  paused?: boolean
}>()

const emit = defineEmits<{
  (e: 'click', event: MouseEvent): void
  (e: 'contextmenu', event: MouseEvent): void
  (e: 'drag-start', event: MouseEvent): void
  (e: 'drag-move', event: MouseEvent): void
  (e: 'drag-end', event: MouseEvent): void
  (e: 'wheel', event: WheelEvent): void
}>()

const bubbleRef = ref<HTMLDivElement>()
const canvasRef = ref<HTMLCanvasElement>()
const { FRAME_W, FRAME_H, setState, startAnimation, stopAnimation, drawFrame } = usePetAnimation()

/** canvas 显示尺寸：max(1, size) 避免 size=0 初始态触发 drawImage 错误。
 *  父组件 onMounted 中会调 computeInitialSize 把 size 设成正常值，UI 不会卡在 size=0 */
const displayW = computed(() => Math.max(1, props.size))
const displayH = computed(() => Math.max(1, props.size))

/** 气泡容器样式：size=0 时容器不显示（避免渲染空白圆） */
const bubbleStyle = computed(() => ({
  width: `${Math.max(1, props.size)}px`,
  height: `${Math.max(1, props.size)}px`,
  opacity: props.size > 0 ? 1 : 0,
  pointerEvents: props.size > 0 ? 'auto' : 'none',
}))

// 精灵图
const spriteImage = ref<HTMLImageElement | null>(null)

// 离屏画布（双缓冲）
let offscreen: HTMLCanvasElement | null = null
let offCtx: CanvasRenderingContext2D | null = null

// 监听状态变化
watch(() => props.state, (newState) => {
  setState(newState)
}, { immediate: true })

// 加载精灵图
function loadSprite(slug: string): void {
  const img = new Image()
  // v0.3.2 fix: 用相对路径（去掉前导 /），让 file:// 加载时基于 dist/ 目录解析
  //   - 之前 /assets/... 在 file:// 协议下变成 file:///assets/...（指向 macOS / 根）
  //   - 现在 ./assets/... 会从 dist/index.html 所在目录找
  const spritePath = `./assets/pets/${slug || 'spirit-fox'}/spritesheet.png`
  img.src = `${spritePath}?t=${Date.now()}`
  console.log('[PetSprite] Loading sprite:', img.src)
  img.onload = () => {
    console.log('[PetSprite] Sprite loaded successfully:', img.width, 'x', img.height)
    spriteImage.value = img
  }
  img.onerror = () => {
    console.error(`[PetSprite] Failed to load sprite: ${img.src}`)
    spriteImage.value = null
    // 5 秒后重试一次
    setTimeout(() => {
      if (!spriteImage.value) loadSprite(slug)
    }, 5000)
  }
}

// 拖拽
let isDragging = false

function onMouseDown(e: MouseEvent) {
  if (e.button !== 0) return
  isDragging = true
  emit('drag-start', e)

  const onMove = (ev: MouseEvent) => {
    if (isDragging) emit('drag-move', ev)
  }
  const onUp = (ev: MouseEvent) => {
    isDragging = false
    emit('drag-end', ev)
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
  }
  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}

function handleClick(e: MouseEvent) {
  emit('click', e)
}

function handleContextMenu(e: MouseEvent) {
  emit('contextmenu', e)
}

function onWheel(e: WheelEvent) {
  emit('wheel', e)
}

// ── 渲染（双缓冲） ────────────────────────────────────────

function ensureOffscreen(w: number, h: number) {
  if (!offscreen || offscreen.width !== w || offscreen.height !== h) {
    offscreen = document.createElement('canvas')
    offscreen.width = w
    offscreen.height = h
    offCtx = offscreen.getContext('2d')
  }
}

function renderFrame() {
  const canvas = canvasRef.value
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  if (!ctx) return

  const w = canvas.width
  const h = canvas.height

  ensureOffscreen(w, h)
  const buffer = offscreen
  if (!offCtx || !buffer) return

  offCtx.clearRect(0, 0, w, h)

  if (spriteImage.value) {
    // 狐狸精灵图按帧原始比例缩放适配到圆形容器：
    // 帧 192x208 → 容器正方形 → 取 min 缩放保证完整显示，留点 padding
    const padding = 0.04  // 4% 内边距，避免狐狸贴边被圆形剪裁
    const targetScale = Math.min(w / FRAME_W, h / FRAME_H) * (1 - padding)
    // 居中绘制
    const drawW = FRAME_W * targetScale
    const drawH = FRAME_H * targetScale
    const ox = (w - drawW) / 2
    const oy = (h - drawH) / 2
    drawFrame(offCtx, spriteImage.value, ox, oy, targetScale)
  } else {
    // 精灵图未加载完成 → 画一个橙色占位圆
    offCtx.fillStyle = 'rgba(251, 146, 60, 0.4)'
    offCtx.beginPath()
    offCtx.arc(w / 2, h / 2, Math.min(w, h) * 0.35, 0, Math.PI * 2)
    offCtx.fill()
  }

  ctx.clearRect(0, 0, w, h)
  ctx.drawImage(buffer, 0, 0)
}

// 渲染循环
let rafId: number | null = null

function renderLoop() {
  if (!props.paused) {
    renderFrame()
  }
  rafId = requestAnimationFrame(renderLoop)
}

// 暂停时渲染一帧静态画面
watch(() => props.paused, (paused) => {
  if (paused) {
    renderFrame()
  }
})

onMounted(() => {
  loadSprite(props.petSlug)
  startAnimation()
  renderLoop()
})

onUnmounted(() => {
  stopAnimation()
  if (rafId !== null) cancelAnimationFrame(rafId)
})

watch(() => props.petSlug, (newSlug) => {
  loadSprite(newSlug)
})
</script>

<style scoped>
.pet-bubble {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  cursor: grab;
  user-select: none;
  -webkit-user-select: none;
  /* 容器：白底 + 细描边 + 柔光阴影，圆形剪裁 */
  background: rgba(255, 255, 255, 0.96);
  border: 1.5px solid rgba(251, 146, 60, 0.35);
  box-shadow:
    0 4px 14px rgba(0, 0, 0, 0.18),
    0 1px 3px rgba(0, 0, 0, 0.08),
    inset 0 1px 2px rgba(255, 255, 255, 0.6);
  overflow: hidden;
  transition: transform 0.18s cubic-bezier(0.34, 1.56, 0.64, 1),
              box-shadow 0.18s ease;
  will-change: transform;
  -webkit-backface-visibility: hidden;
  backface-visibility: hidden;
  -webkit-transform: translateZ(0);
  transform: translateZ(0);
}

.pet-bubble:hover {
  transform: scale(1.05);
  box-shadow:
    0 6px 20px rgba(0, 0, 0, 0.22),
    0 2px 6px rgba(0, 0, 0, 0.1),
    inset 0 1px 2px rgba(255, 255, 255, 0.7);
}

.pet-bubble.dragging {
  cursor: grabbing;
  transform: scale(0.97);
  box-shadow:
    0 2px 8px rgba(0, 0, 0, 0.18),
    inset 0 1px 2px rgba(255, 255, 255, 0.5);
}

.pet-canvas {
  position: absolute;
  inset: 0;
  display: block;
  background: transparent;
  pointer-events: none;
}

/* 圆形顶部高光：增强立体感 */
.pet-bubble-shine {
  position: absolute;
  inset: 0;
  border-radius: 50%;
  background: radial-gradient(
    ellipse at 30% 20%,
    rgba(255, 255, 255, 0.55) 0%,
    rgba(255, 255, 255, 0) 45%
  );
  pointer-events: none;
  z-index: 1;
}
</style>
