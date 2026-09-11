<template>
  <div class="radial-menu" @click.stop>
    <!-- 中心遮罩（点击空白关闭） -->
    <div class="radial-backdrop" @click="$emit('close')"></div>

    <!-- 4 个功能按钮环绕宠物 -->
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
import { ref, onMounted } from 'vue'

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

// 按钮环绕布局（上/右/下/左）
const radius = 55

function itemStyle(idx: number) {
  const angle = (idx * 90 - 90) * (Math.PI / 180) // 从上方开始，顺时针
  const x = Math.cos(angle) * radius
  const y = Math.sin(angle) * radius
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

onMounted(() => {
  // 延迟触发动画
  requestAnimationFrame(() => {
    showItems.value = true
  })
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
  width: 44px;
  height: 44px;
  margin-left: -22px;
  margin-top: -22px;
  border-radius: 50%;
  background: rgba(20, 20, 30, 0.95);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 2px solid rgba(251, 146, 60, 0.6);
  color: #fff;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  pointer-events: auto;
  transition: all 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
  z-index: 201;
}

.radial-btn:hover {
  background: rgba(255, 140, 50, 0.8);
  transform: translate(var(--tx, 0), var(--ty, 0)) scale(1.15) !important;
}

.radial-icon {
  font-size: 18px;
  line-height: 1;
}

.radial-label {
  font-size: 9px;
  margin-top: 2px;
  opacity: 0.8;
}
</style>
