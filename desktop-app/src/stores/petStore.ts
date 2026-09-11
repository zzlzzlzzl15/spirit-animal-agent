/**
 * petStore — Pinia 宠物状态管理。
 */

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const usePetStore = defineStore('pet', () => {
  // ── 状态 ──────────────────────────────────────────────────

  const currentState = ref('idle')
  const activeSlug = ref('')
  const displayName = ref('')
  const scale = ref(0.75)
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

  function setScale(newScale: number) {
    scale.value = Math.max(0.2, Math.min(3.0, newScale))
    if (window.electronAPI) {
      window.electronAPI.saveScale(scale.value)
    }
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
      const serverScale = data.preferences.scale || 0.75
      scale.value = Math.max(0.5, Math.min(3.0, serverScale))
    }
  }

  return {
    currentState,
    activeSlug,
    displayName,
    scale,
    spritesheetExists,
    petInfo,
    setState,
    setScale,
    syncFromServer,
  }
})
