<template>
  <div class="panel cli-panel">
    <div class="panel-header">
      <span class="panel-title">⌨️ 任务 CLI</span>
      <button class="panel-close" @click="$emit('close')">✕</button>
    </div>
    <div class="cli-body">
      <!-- 输出区域 -->
      <div class="cli-output" ref="outputRef">
        <div v-for="(line, idx) in outputLines" :key="idx" class="cli-line" :class="line.type">
          <span v-if="line.type === 'command'" class="cli-prompt">$</span>
          <span class="cli-text">{{ line.text }}</span>
        </div>
        <div v-if="executing" class="cli-line running">
          <span class="cli-text">执行中...</span>
        </div>
      </div>

      <!-- 输入区域 -->
      <div class="cli-input-row">
        <span class="cli-prompt">$</span>
        <input
          ref="inputRef"
          v-model="command"
          type="text"
          class="cli-input"
          placeholder="输入命令..."
          @keyup.enter="execute"
          :disabled="executing"
        />
        <button class="cli-run-btn" @click="execute" :disabled="executing || !command.trim()">
          ▶
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, nextTick, onMounted, inject } from 'vue'

defineEmits<{
  (e: 'close'): void
}>()

const wsSend = inject<Function>('wsSend')

interface OutputLine {
  text: string
  type: 'command' | 'output' | 'error' | 'system'
}

const command = ref('')
const executing = ref(false)
const outputLines = ref<OutputLine[]>([
  { text: 'Spirit Agent CLI — 输入命令与 Agent 交互', type: 'system' },
  { text: '支持: 执行命令、查看状态、搜索知识库等', type: 'system' },
  { text: '', type: 'system' },
])
const outputRef = ref<HTMLElement>()
const inputRef = ref<HTMLInputElement>()

async function execute() {
  const cmd = command.value.trim()
  if (!cmd || executing.value) return

  outputLines.value.push({ text: cmd, type: 'command' })
  command.value = ''
  executing.value = true

  try {
    if (!wsSend) {
      outputLines.value.push({ text: 'WebSocket 未连接，无法执行命令', type: 'error' })
    } else {
      const result = await wsSend('execute_command', { command: cmd })
      if (result.error) {
        outputLines.value.push({ text: result.error, type: 'error' })
      } else if (result.output) {
        outputLines.value.push({ text: result.output, type: 'output' })
      } else if (result.message) {
        outputLines.value.push({ text: result.message, type: 'output' })
      } else {
        outputLines.value.push({ text: JSON.stringify(result), type: 'output' })
      }
    }
  } catch (err: any) {
    outputLines.value.push({ text: err.message || '执行失败', type: 'error' })
  } finally {
    executing.value = false
    scrollToBottom()
  }
}

function scrollToBottom() {
  nextTick(() => {
    if (outputRef.value) {
      outputRef.value.scrollTop = outputRef.value.scrollHeight
    }
  })
}

onMounted(() => {
  inputRef.value?.focus()
})
</script>

<style scoped>
.panel {
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 400px;
  height: 360px;
  background: rgba(10, 10, 15, 0.95);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 16px;
  color: #e0e0e0;
  z-index: 500;
  box-shadow: 0 12px 48px rgba(0, 0, 0, 0.6);
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
  padding: 12px 16px 8px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

.panel-title {
  font-weight: 600;
  font-size: 13px;
  color: #fff;
  font-family: 'Cascadia Code', 'Fira Code', monospace;
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

.cli-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.cli-output {
  flex: 1;
  overflow-y: auto;
  padding: 10px 16px;
  font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
  font-size: 12px;
  line-height: 1.5;
}

.cli-line {
  display: flex;
  gap: 8px;
  padding: 1px 0;
}

.cli-line.command .cli-text { color: #4ade80; }
.cli-line.output .cli-text { color: #ccc; }
.cli-line.error .cli-text { color: #f87171; }
.cli-line.system .cli-text { color: #888; font-style: italic; }
.cli-line.running .cli-text { color: #fbbf24; }

.cli-prompt {
  color: #fb923c;
  font-weight: bold;
  flex-shrink: 0;
}

.cli-input-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px 12px;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
}

.cli-input {
  flex: 1;
  background: transparent;
  border: none;
  color: #4ade80;
  font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
  font-size: 12px;
  outline: none;
}
.cli-input::placeholder {
  color: #555;
}

.cli-run-btn {
  background: rgba(255, 140, 50, 0.8);
  border: none;
  border-radius: 6px;
  padding: 4px 10px;
  color: #fff;
  cursor: pointer;
  font-size: 12px;
}
.cli-run-btn:hover {
  background: rgba(255, 140, 50, 1);
}
.cli-run-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
</style>
