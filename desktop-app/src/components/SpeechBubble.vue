<template>
  <div class="speech-bubble" @click.stop>
    <div class="bubble-content">
      <span class="bubble-text">{{ text }}</span>
      <button class="bubble-close" @click="$emit('close')">✕</button>
    </div>
    <!-- 气泡尾巴 -->
    <div class="bubble-tail"></div>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  text: string
}>()

defineEmits<{
  (e: 'close'): void
}>()
</script>

<style scoped>
.speech-bubble {
  position: fixed;
  bottom: 10px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 90;
  animation: bubble-in 0.3s ease-out;
}

@keyframes bubble-in {
  from { opacity: 0; transform: translateX(-50%) translateY(4px) scale(0.9); }
  to   { opacity: 1; transform: translateX(-50%) translateY(0) scale(1); }
}

.bubble-content {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: rgba(255, 255, 255, 0.95);
  border-radius: 16px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.15);
  max-width: 200px;
}

.bubble-text {
  color: #333;
  font-size: 12px;
  line-height: 1.4;
  word-break: break-all;
}

.bubble-close {
  background: none;
  border: none;
  color: #999;
  cursor: pointer;
  font-size: 10px;
  flex-shrink: 0;
  padding: 2px;
  border-radius: 50%;
}
.bubble-close:hover {
  color: #333;
  background: rgba(0, 0, 0, 0.05);
}

.bubble-tail {
  width: 0;
  height: 0;
  border-left: 8px solid transparent;
  border-right: 8px solid transparent;
  border-top: 8px solid rgba(255, 255, 255, 0.95);
  margin: 0 auto;
}
</style>
