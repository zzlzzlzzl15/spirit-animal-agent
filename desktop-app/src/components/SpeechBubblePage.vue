<template>
  <div class="bubble-wrapper">
    <div class="bubble-content">
      <span class="bubble-text">{{ bubbleText }}</span>
    </div>
    <!-- 气泡尾巴（指向下方的小三角） -->
    <div class="bubble-tail"></div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'

const bubbleText = ref('')

onMounted(() => {
  // 从 URL hash 中解析 text 参数
  const hash = window.location.hash
  const queryIndex = hash.indexOf('?')
  if (queryIndex !== -1) {
    const params = new URLSearchParams(hash.substring(queryIndex + 1))
    bubbleText.value = params.get('text') || ''
  }
})
</script>

<style>
/* 全局样式 — 气泡窗口整体透明 */
html, body {
  margin: 0;
  padding: 0;
  background: transparent;
  overflow: hidden;
  width: 100%;
  height: 100%;
  /* v0.3.4 fix: 改用 flex-end — 让 .bubble-wrapper 底边贴窗口底。
     之前 center 居中让整个气泡（content + tail）填满 60px 窗口，
     视觉上白底矩形飘在桌宠上方 70px。改后 content 紧贴 tail、
     tail 底紧贴 BrowserWindow 底（即圆顶上方 10px gap），让气泡
     看起来“站在”小狐狸头顶。 */
  display: flex;
  align-items: flex-end;
  justify-content: center;
  box-sizing: border-box;
}
</style>

<style scoped>
.bubble-wrapper {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 4px 8px 0;
  animation: bubble-in 0.3s ease-out;
}

@keyframes bubble-in {
  from { opacity: 0; transform: translateY(6px) scale(0.92); }
  to   { opacity: 1; transform: translateY(0) scale(1); }
}

.bubble-content {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 8px 14px;
  background: rgba(255, 255, 255, 0.95);
  border-radius: 16px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.15);
  max-width: 200px;
}

.bubble-text {
  color: #333;
  font-size: 12px;
  line-height: 1.4;
  text-align: center;
  word-break: break-all;
}

.bubble-tail {
  width: 0;
  height: 0;
  border-left: 8px solid transparent;
  border-right: 8px solid transparent;
  border-top: 8px solid rgba(255, 255, 255, 0.95);
  margin-top: -1px;
}
</style>
