<template>
  <canvas
    ref="canvasRef"
    class="pet-canvas"
    :class="{ dragging: props.paused }"
    :width="displayW"
    :height="displayH"
    :style="{ width: displayW + 'px', height: displayH + 'px' }"
    @mousedown="onMouseDown"
    @click="handleClick"
    @contextmenu.prevent="handleContextMenu"
  />
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { usePetAnimation } from '../composables/usePetAnimation'

const props = defineProps<{
  state: string
  scale: number
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

// 事件处理函数
function handleClick(e: MouseEvent) {
  console.log('[Canvas] click', displayW.value, displayH.value)
  emit('click', e)
}

function handleContextMenu(e: MouseEvent) {
  console.log('[Canvas] contextmenu')
  emit('contextmenu', e)
}

const canvasRef = ref<HTMLCanvasElement>()
const { FRAME_W, FRAME_H, setState, startAnimation, stopAnimation, drawFrame } = usePetAnimation()

// 显示尺寸
const displayW = computed(() => Math.round(FRAME_W * props.scale))
const displayH = computed(() => Math.round(FRAME_H * props.scale))

// 精灵图
const spriteImage = ref<HTMLImageElement | null>(null)

// 离屏画布（双缓冲，防闪烁）
let offscreen: HTMLCanvasElement | null = null
let offCtx: CanvasRenderingContext2D | null = null

// 监听状态变化
watch(() => props.state, (newState) => {
  setState(newState)
}, { immediate: true })

// 加载精灵图
function loadSprite(slug: string): void {
  const img = new Image()
  // 使用相对路径，适配 Electron file:// 协议
  img.src = `./assets/pets/${slug || 'spirit-fox'}/spritesheet.png?t=${Date.now()}`
  img.onload = () => {
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
  if (e.button === 0) {
    isDragging = true
    emit('drag-start', e)

    const onMove = (ev: MouseEvent) => {
      if (isDragging) {
        emit('drag-move', ev)
      }
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

  // 在离屏画布上绘制
  ensureOffscreen(w, h)
  if (!offCtx) return

  offCtx.clearRect(0, 0, w, h)

  if (spriteImage.value) {
    // 直接使用 canvas 尺寸作为目标尺寸，确保完整显示
    const targetScale = Math.min(w / FRAME_W, h / FRAME_H)
    drawFrame(offCtx, spriteImage.value, 0, 0, targetScale)
  } else {
    drawPlaceholder(offCtx, w, h)
  }

  // 一次性拷贝到主画布（减少闪烁）
  ctx.clearRect(0, 0, w, h)
  ctx.drawImage(offscreen, 0, 0)
}

// 渲染循环
let rafId: number | null = null

function renderLoop() {
  // 拖拽时暂停动画，只渲染静态帧
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

function drawPlaceholder(ctx: CanvasRenderingContext2D, w: number, h: number) {
  const cx = w / 2
  const cy = h / 2
  const unit = Math.min(w, h) / 10 // 基准单位

  // ── 尾巴（身后） ──
  ctx.fillStyle = '#f09030'
  ctx.beginPath()
  ctx.ellipse(cx + unit * 2.8, cy + unit * 1.5, unit * 2.2, unit * 1.2, 0.4, 0, Math.PI * 2)
  ctx.fill()
  // 尾巴尖白色
  ctx.fillStyle = '#fff5e6'
  ctx.beginPath()
  ctx.ellipse(cx + unit * 3.8, cy + unit * 0.8, unit * 0.8, unit * 0.6, 0.6, 0, Math.PI * 2)
  ctx.fill()

  // ── 身体 ──
  ctx.fillStyle = '#f5a623'
  ctx.beginPath()
  ctx.ellipse(cx, cy + unit * 1.8, unit * 2.8, unit * 2.2, 0, 0, Math.PI * 2)
  ctx.fill()

  // 白色肚子
  ctx.fillStyle = '#fff5e6'
  ctx.beginPath()
  ctx.ellipse(cx, cy + unit * 2.2, unit * 1.6, unit * 1.4, 0, 0, Math.PI * 2)
  ctx.fill()

  // ── 头部 ──
  ctx.fillStyle = '#f5a623'
  ctx.beginPath()
  ctx.ellipse(cx, cy - unit * 0.5, unit * 2.5, unit * 2.2, 0, 0, Math.PI * 2)
  ctx.fill()

  // 白色面部
  ctx.fillStyle = '#fff5e6'
  ctx.beginPath()
  ctx.ellipse(cx, cy + unit * 0.3, unit * 1.8, unit * 1.5, 0, 0, Math.PI * 2)
  ctx.fill()

  // ── 耳朵 ──
  // 左耳
  ctx.fillStyle = '#f5a623'
  ctx.beginPath()
  ctx.moveTo(cx - unit * 1.8, cy - unit * 1.5)
  ctx.lineTo(cx - unit * 2.5, cy - unit * 3.5)
  ctx.lineTo(cx - unit * 0.5, cy - unit * 2.2)
  ctx.closePath()
  ctx.fill()
  // 左耳内耳
  ctx.fillStyle = '#ffb6c1'
  ctx.beginPath()
  ctx.moveTo(cx - unit * 1.6, cy - unit * 1.7)
  ctx.lineTo(cx - unit * 2.1, cy - unit * 3.0)
  ctx.lineTo(cx - unit * 0.8, cy - unit * 2.1)
  ctx.closePath()
  ctx.fill()

  // 右耳
  ctx.fillStyle = '#f5a623'
  ctx.beginPath()
  ctx.moveTo(cx + unit * 1.8, cy - unit * 1.5)
  ctx.lineTo(cx + unit * 2.5, cy - unit * 3.5)
  ctx.lineTo(cx + unit * 0.5, cy - unit * 2.2)
  ctx.closePath()
  ctx.fill()
  // 右耳内耳
  ctx.fillStyle = '#ffb6c1'
  ctx.beginPath()
  ctx.moveTo(cx + unit * 1.6, cy - unit * 1.7)
  ctx.lineTo(cx + unit * 2.1, cy - unit * 3.0)
  ctx.lineTo(cx + unit * 0.8, cy - unit * 2.1)
  ctx.closePath()
  ctx.fill()

  // ── 眼睛 ──
  const eyeY = cy - unit * 0.3
  const eyeSpacing = unit * 1.0
  const eyeR = unit * 0.55

  // 眼白
  ctx.fillStyle = '#fff'
  ctx.beginPath()
  ctx.ellipse(cx - eyeSpacing, eyeY, eyeR, eyeR * 1.1, 0, 0, Math.PI * 2)
  ctx.fill()
  ctx.beginPath()
  ctx.ellipse(cx + eyeSpacing, eyeY, eyeR, eyeR * 1.1, 0, 0, Math.PI * 2)
  ctx.fill()

  // 瞳孔（大而有神）
  ctx.fillStyle = '#2d1b00'
  ctx.beginPath()
  ctx.ellipse(cx - eyeSpacing, eyeY + unit * 0.1, eyeR * 0.6, eyeR * 0.7, 0, 0, Math.PI * 2)
  ctx.fill()
  ctx.beginPath()
  ctx.ellipse(cx + eyeSpacing, eyeY + unit * 0.1, eyeR * 0.6, eyeR * 0.7, 0, 0, Math.PI * 2)
  ctx.fill()

  // 高光
  ctx.fillStyle = '#fff'
  ctx.beginPath()
  ctx.arc(cx - eyeSpacing - unit * 0.15, eyeY - unit * 0.15, eyeR * 0.25, 0, Math.PI * 2)
  ctx.fill()
  ctx.beginPath()
  ctx.arc(cx + eyeSpacing - unit * 0.15, eyeY - unit * 0.15, eyeR * 0.25, 0, Math.PI * 2)
  ctx.fill()

  // ─ 鼻子 ──
  ctx.fillStyle = '#ff6b8a'
  ctx.beginPath()
  ctx.ellipse(cx, cy + unit * 0.5, unit * 0.3, unit * 0.22, 0, 0, Math.PI * 2)
  ctx.fill()

  // ─ 嘴巴（ω 形） ─
  ctx.strokeStyle = '#8b5e3c'
  ctx.lineWidth = Math.max(1.5, unit * 0.12)
  ctx.lineCap = 'round'
  ctx.beginPath()
  ctx.arc(cx - unit * 0.35, cy + unit * 0.65, unit * 0.35, -0.2 * Math.PI, 0.7 * Math.PI)
  ctx.stroke()
  ctx.beginPath()
  ctx.arc(cx + unit * 0.35, cy + unit * 0.65, unit * 0.35, 0.3 * Math.PI, 1.2 * Math.PI)
  ctx.stroke()

  // ── 腮红 ─
  ctx.fillStyle = 'rgba(255, 150, 150, 0.35)'
  ctx.beginPath()
  ctx.ellipse(cx - unit * 1.6, cy + unit * 0.4, unit * 0.5, unit * 0.3, 0, 0, Math.PI * 2)
  ctx.fill()
  ctx.beginPath()
  ctx.ellipse(cx + unit * 1.6, cy + unit * 0.4, unit * 0.5, unit * 0.3, 0, 0, Math.PI * 2)
  ctx.fill()

  // ── 小爪子 ─
  ctx.fillStyle = '#fff5e6'
  ctx.beginPath()
  ctx.ellipse(cx - unit * 1.2, cy + unit * 3.2, unit * 0.6, unit * 0.4, -0.2, 0, Math.PI * 2)
  ctx.fill()
  ctx.beginPath()
  ctx.ellipse(cx + unit * 1.2, cy + unit * 3.2, unit * 0.6, unit * 0.4, 0.2, 0, Math.PI * 2)
  ctx.fill()
}

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
.pet-canvas {
  cursor: pointer;
  background: transparent;
  will-change: transform;
  /* 平滑缩放 */
  image-rendering: auto;
  -webkit-backface-visibility: hidden;
  backface-visibility: hidden;
}
.pet-canvas.dragging {
  cursor: grabbing;
  /* 拖拽时禁用合成优化 */
  opacity: 0.999;
}
</style>
