/**
 * useDrag — 拖拽移动 + 滚轮缩放 + 屏幕边界约束。
 */

import { ref, onUnmounted } from 'vue'

export function useDrag() {
  const isDragging = ref(false)
  const startX = ref(0)
  const startY = ref(0)

  let moveHandler: ((e: MouseEvent) => void) | null = null
  let upHandler: ((e: MouseEvent) => void) | null = null

  function onMouseDown(e: MouseEvent): void {
    if (e.button !== 0) return // 只响应左键
    isDragging.value = false
    startX.value = e.clientX
    startY.value = e.clientY

    moveHandler = (ev: MouseEvent) => {
      const dx = ev.clientX - startX.value
      const dy = ev.clientY - startY.value
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) {
        isDragging.value = true
      }
    }

    upHandler = () => {
      cleanup()
    }

    document.addEventListener('mousemove', moveHandler)
    document.addEventListener('mouseup', upHandler)
  }

  function cleanup(): void {
    if (moveHandler) {
      document.removeEventListener('mousemove', moveHandler)
      moveHandler = null
    }
    if (upHandler) {
      document.removeEventListener('mouseup', upHandler)
      upHandler = null
    }
  }

  function clampToScreen(x: number, y: number, w: number, h: number) {
    const screenW = window.screen.availWidth
    const screenH = window.screen.availHeight
    return {
      x: Math.max(-w * 0.5, Math.min(screenW - w * 0.5, x)),
      y: Math.max(-h * 0.5, Math.min(screenH - h * 0.5, y)),
    }
  }

  onUnmounted(() => {
    cleanup()
  })

  return {
    isDragging,
    onMouseDown,
    clampToScreen,
  }
}
