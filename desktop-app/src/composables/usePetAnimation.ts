/**
 * usePetAnimation — 精灵动画循环。
 *
 * 负责：
 * - requestAnimationFrame 驱动的帧动画
 * - 按 PetState 切换精灵图行
 * - 帧计时 + 状态过渡
 */

import { ref, watch, onUnmounted } from 'vue'

// 帧几何常量（与 Python 端 pet_constants.py 一致）
const FRAME_W = 192
const FRAME_H = 208
const FRAMES_PER_STATE = 6
const LOOP_MS = 1100

// PetState → 精灵图行索引映射
const STATE_ROW: Record<string, number> = {
  idle: 0,
  'running-right': 1,
  'running-left': 2,
  waving: 3,
  jumping: 4,
  failed: 5,
  waiting: 6,
  running: 7,
  review: 8,
}

// 内部状态名 → 精灵图行名
const STATE_ALIASES: Record<string, string> = {
  idle: 'idle',
  wave: 'waving',
  run: 'running',
  failed: 'failed',
  review: 'review',
  jump: 'jumping',
  waiting: 'waiting',
}

export function usePetAnimation() {
  const currentFrame = ref(0)
  const currentState = ref('idle')
  let animFrameId: number | null = null
  let lastFrameTime = 0
  let frameInterval = LOOP_MS / FRAMES_PER_STATE

  function setState(state: string): void {
    const mapped = STATE_ALIASES[state] || state
    if (mapped !== currentState.value) {
      currentState.value = mapped
      currentFrame.value = 0 // 状态切换时重置帧
    }
  }

  function getRow(): number {
    return STATE_ROW[currentState.value] ?? 0
  }

  function startAnimation(): void {
    if (animFrameId !== null) return
    lastFrameTime = performance.now()
    tick(lastFrameTime)
  }

  function stopAnimation(): void {
    if (animFrameId !== null) {
      cancelAnimationFrame(animFrameId)
      animFrameId = null
    }
  }

  function tick(now: number): void {
    const elapsed = now - lastFrameTime
    if (elapsed >= frameInterval) {
      currentFrame.value = (currentFrame.value + 1) % FRAMES_PER_STATE
      lastFrameTime = now - (elapsed % frameInterval)
    }
    animFrameId = requestAnimationFrame(tick)
  }

  /**
   * 在 Canvas 上绘制当前帧。
   */
  function drawFrame(
    ctx: CanvasRenderingContext2D,
    image: HTMLImageElement | null,
    x: number,
    y: number,
    scale: number,
  ): void {
    if (!image || !image.complete || image.naturalWidth === 0) return

    const row = getRow()
    const col = currentFrame.value

    const sx = col * FRAME_W
    const sy = row * FRAME_H
    const sw = FRAME_W
    const sh = FRAME_H

    const dw = FRAME_W * scale
    const dh = FRAME_H * scale

    // 开启平滑缩放，避免像素化/截断效果
    ctx.imageSmoothingEnabled = true
    ctx.imageSmoothingQuality = 'high'
    ctx.drawImage(
      image,
      sx, sy, sw, sh,  // 源矩形（从精灵图裁剪一帧）
      x, y, dw, dh,     // 目标矩形（按 scale 整体缩放）
    )
  }

  // 自动清理
  onUnmounted(() => {
    stopAnimation()
  })

  return {
    currentFrame,
    currentState,
    setState,
    getRow,
    startAnimation,
    stopAnimation,
    drawFrame,
    FRAME_W,
    FRAME_H,
  }
}
