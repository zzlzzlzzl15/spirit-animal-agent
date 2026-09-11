<template>
  <div class="panel knowledge-panel">
    <div class="panel-header">
      <span class="panel-title">📚 知识库</span>
      <button class="panel-close" @click="$emit('close')">✕</button>
    </div>
    <div class="panel-body">
      <!-- 搜索框 -->
      <div class="search-box">
        <input
          v-model="query"
          type="text"
          placeholder="搜索知识库..."
          class="search-input"
          @keyup.enter="search"
        />
        <button class="search-btn" @click="search">🔍</button>
      </div>

      <!-- 搜索结果 -->
      <div class="results">
        <div v-if="loading" class="loading">搜索中...</div>
        <div v-else-if="errorMsg" class="empty">{{ errorMsg }}</div>
        <div v-else-if="results.length === 0 && query" class="empty">
          未找到相关内容
        </div>
        <div
          v-for="(item, idx) in results"
          :key="idx"
          class="result-item"
        >
          <div class="result-title">{{ item.title }}</div>
          <div class="result-preview">{{ item.content }}</div>
        </div>
        <div v-if="!query" class="hint">
          输入关键词搜索 Memora 知识库
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, inject } from 'vue'

defineEmits<{
  (e: 'close'): void
}>()

const wsSend = inject<Function>('wsSend')

const query = ref('')
const loading = ref(false)
const errorMsg = ref('')
const results = ref<{ title: string; content: string }[]>([])

async function search() {
  if (!query.value.trim()) return
  loading.value = true
  errorMsg.value = ''
  results.value = []
  try {
    if (!wsSend) {
      errorMsg.value = 'WebSocket 未连接'
      return
    }
    const data = await wsSend('search_knowledge', { query: query.value })
    if (data.error) {
      errorMsg.value = data.error
    } else {
      results.value = data.results || []
      if (results.value.length === 0 && data.message) {
        errorMsg.value = data.message
      }
    }
  } catch (err: any) {
    errorMsg.value = err.message || '搜索失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.panel {
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 360px;
  max-height: 480px;
  background: rgba(20, 20, 30, 0.92);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 16px;
  color: #e0e0e0;
  z-index: 500;
  box-shadow: 0 12px 48px rgba(0, 0, 0, 0.5);
  display: flex;
  flex-direction: column;
  animation: panel-in 0.25s ease-out;
}

@keyframes panel-in {
  from { opacity: 0; transform: translate(-50%, -50%) scale(0.9); }
  to   { opacity: 1; transform: translate(-50%, -50%) scale(1); }
}

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 14px 16px 10px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.panel-title {
  font-weight: 600;
  font-size: 14px;
  color: #fff;
}

.panel-close {
  background: none;
  border: none;
  color: #888;
  cursor: pointer;
  font-size: 16px;
  padding: 4px 6px;
  border-radius: 6px;
}
.panel-close:hover {
  color: #fff;
  background: rgba(255, 255, 255, 0.1);
}

.panel-body {
  padding: 12px 16px 16px;
  overflow-y: auto;
  flex: 1;
}

.search-box {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}

.search-input {
  flex: 1;
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 8px;
  padding: 8px 12px;
  color: #fff;
  font-size: 12px;
  outline: none;
}
.search-input:focus {
  border-color: rgba(255, 140, 50, 0.5);
}
.search-input::placeholder {
  color: #666;
}

.search-btn {
  background: rgba(255, 140, 50, 0.8);
  border: none;
  border-radius: 8px;
  padding: 8px 12px;
  cursor: pointer;
  font-size: 14px;
}
.search-btn:hover {
  background: rgba(255, 140, 50, 1);
}

.loading, .empty, .hint {
  text-align: center;
  color: #888;
  font-size: 12px;
  padding: 20px 0;
}

.result-item {
  padding: 10px 12px;
  background: rgba(255, 255, 255, 0.04);
  border-radius: 8px;
  margin-bottom: 8px;
  cursor: pointer;
}
.result-item:hover {
  background: rgba(255, 255, 255, 0.08);
}

.result-title {
  font-size: 12px;
  font-weight: 600;
  color: #fb923c;
  margin-bottom: 4px;
}

.result-preview {
  font-size: 11px;
  color: #999;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
