/**
 * petStore — Pinia 宠物状态管理。
 *
 * 重大改动（v0.4）：
 * - 把"精灵图缩放 scale"换成"气泡直径 size"（CSS px）。
 * - 气泡大小由前端根据屏幕宽度比例计算（screenWidth × PCT），
 *   在小屏/大屏看起来都合理。
 * - 持久化字段从 `petScale` 改成 `petSize`。
 */

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

/** 气泡占屏幕宽度的默认比例（1.5%：1920 屏 ≈ 28px，1470 屏 ≈ 22px，2560 屏 ≈ 38px） */
const DEFAULT_SIZE_PCT = 0.015
/** 最小气泡直径（CSS px）——极其小巧 */
const MIN_SIZE = 24
/** 最大气泡直径（CSS px） */
const MAX_SIZE = 80
/** 首次启动默认值（兜底，screenWidth 不可用时使用） */
const FALLBACK_SIZE = 28
/** 表示"未初始化"的 size：0 触发 onMounted 中的 computeInitialSize */
const UNINITIALIZED = 0

export const usePetStore = defineStore('pet', () => {
  // ── 状态 ──────────────────────────────────────────────────

  const currentState = ref('idle')
  const activeSlug = ref('')
  const displayName = ref('')
  // 初始值 0：触发 App.vue onMounted 中的 computeInitialSize。
  // 这样可以根据用户实际屏幕宽度（1080p / 2K / 4K）算出合理初始值。
  const size = ref<number>(UNINITIALIZED)
  const spritesheetExists = ref(false)

  const petInfo = computed(() => ({
    slug: activeSlug.value,
    displayName: displayName.value,
    spritesheetExists: spritesheetExists.value,
  }))

  // ── 操作 ──────────────────────────────────────────────────

  function setState(state: string) {
    currentState.value = state
  }

  /**
   * 设置气泡大小（CSS px）。
   * 同时同步给主进程 resize 窗口、持久化到 electron-store。
   */
  function setSize(newSize: number) {
    const clamped = Math.max(MIN_SIZE, Math.min(MAX_SIZE, newSize))
    size.value = clamped
    if (window.electronAPI) {
      window.electronAPI.saveSize(clamped)
    }
  }

  /**
   * 根据屏幕宽度计算初始气泡大小（屏幕宽 × 比例，钳制到合法区间）。
   * 由 App.vue 在 onMounted 时调用，确保首次启动尺寸合理。
   */
  function computeInitialSize(screenWidth: number): number {
    if (!Number.isFinite(screenWidth) || screenWidth <= 0) return FALLBACK_SIZE
    const raw = Math.round(screenWidth * DEFAULT_SIZE_PCT)
    return Math.max(MIN_SIZE, Math.min(MAX_SIZE, raw))
  }

  function syncFromServer(data: any) {
    if (data.pet) {
      activeSlug.value = data.pet.slug || ''
      displayName.value = data.pet.displayName || ''
      spritesheetExists.value = data.pet.spritesheetExists || false
    }
    if (data.state) {
      currentState.value = data.state.current || 'idle'
    }
    if (data.preferences) {
      // 兼容旧字段 petScale（如有），优先使用 petSize
      const serverSize = data.preferences.petSize ?? data.preferences.scale ?? null
      if (serverSize != null) {
        const n = Number(serverSize)
        if (Number.isFinite(n) && n > 0) {
          // 注意：旧 scale 字段是 0.2-3.0 的浮点数；新 size 是 44-140 的整数
          // 如果拿到的值 < 10，认为是旧 scale，转换成 size
          const pixelSize = n < 10 ? Math.round(n * 80) : Math.round(n)
          size.value = Math.max(MIN_SIZE, Math.min(MAX_SIZE, pixelSize))
          if (window.electronAPI) {
            window.electronAPI.saveSize(size.value)
          }
        }
      }
    }
  }

  return {
    currentState,
    activeSlug,
    displayName,
    size,
    spritesheetExists,
    petInfo,
    setState,
    setSize,
    computeInitialSize,
    syncFromServer,
  }
})
