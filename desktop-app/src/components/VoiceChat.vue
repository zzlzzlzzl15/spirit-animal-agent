<template>
  <div class="panel voice-panel">
    <div class="panel-header">
      <span class="panel-title">🎤 语音问答</span>
      <button class="panel-close" @click="$emit('close')">✕</button>
    </div>
    <div class="voice-body">
      <!-- 对话历史 -->
      <div class="chat-history" ref="historyRef">
        <div v-for="(msg, idx) in messages" :key="idx" class="chat-msg" :class="msg.role">
          <div class="chat-avatar">{{ msg.role === 'user' ? '🧑' : '🦊' }}</div>
          <div class="chat-text">{{ msg.text }}</div>
        </div>
        <div v-if="processing" class="chat-msg assistant">
          <div class="chat-avatar">🦊</div>
          <div class="chat-text thinking">思考中...</div>
        </div>
      </div>

      <!-- 录音控制 -->
      <div class="voice-controls">
        <button
          class="voice-btn"
          :class="{ recording: isRecording }"
          @mousedown="startRecording"
          @mouseup="stopRecording"
          @mouseleave="stopRecording"
        >
          <span class="voice-icon">{{ isRecording ? '🔴' : '🎤' }}</span>
          <span class="voice-label">{{ recordStatus || (isRecording ? '松开结束' : '按住说话') }}</span>
        </button>

        <!-- 文字输入备选 -->
        <div class="text-input-row">
          <input
            v-model="textInput"
            type="text"
            class="text-input"
            placeholder="或输入文字..."
            @keyup.enter="sendText"
          />
          <button class="send-btn" @click="sendText" :disabled="!textInput.trim()">发送</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, nextTick, inject } from 'vue'

defineEmits<{
  (e: 'close'): void
}>()

const wsSend = inject<Function>('wsSend')

interface ChatMessage {
  role: 'user' | 'assistant' | 'system'
  text: string
}

const messages = ref<ChatMessage[]>([
  { role: 'assistant', text: '你好！按住麦克风按钮说话，或直接输入文字~' },
])
const isRecording = ref(false)
const processing = ref(false)
const textInput = ref('')
const historyRef = ref<HTMLElement>()
const recordStatus = ref('')

// ── 浏览器录音 ──────────────────────────────────────────
let mediaRecorder: MediaRecorder | null = null
let audioChunks: Blob[] = []

async function startRecording() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    mediaRecorder = new MediaRecorder(stream)
    audioChunks = []

    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) audioChunks.push(e.data)
    }

    mediaRecorder.onstop = async () => {
      // 停止所有音轨
      stream.getTracks().forEach(t => t.stop())
      // 将录音转为 base64 发送
      const blob = new Blob(audioChunks, { type: mediaRecorder!.mimeType })
      const base64 = await blobToBase64(blob)
      await transcribeAudio(base64, mediaRecorder!.mimeType)
    }

    mediaRecorder.start()
    isRecording.value = true
    recordStatus.value = '录音中...'
  } catch (err: any) {
    recordStatus.value = '麦克风权限被拒绝'
    messages.value.push({ role: 'system', text: '无法访问麦克风: ' + err.message })
    scrollToBottom()
  }
}

function stopRecording() {
  if (!isRecording.value || !mediaRecorder) return
  mediaRecorder.stop()
  isRecording.value = false
  recordStatus.value = '识别中...'
}

function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onloadend = () => resolve((reader.result as string).split(',')[1])
    reader.onerror = reject
    reader.readAsDataURL(blob)
  })
}

async function transcribeAudio(base64: string, mimeType: string) {
  if (!wsSend) {
    messages.value.push({ role: 'system', text: 'WebSocket 未连接' })
    recordStatus.value = ''
    scrollToBottom()
    return
  }
  try {
    const result = await wsSend('transcribe_audio', {
      audio_base64: base64,
      mime_type: mimeType,
    })
    if (result.error) {
      messages.value.push({ role: 'user', text: '(语音识别失败: ' + result.error + ')' })
    } else if (result.text) {
      messages.value.push({ role: 'user', text: result.text })
      // 自动发送识别后的文字
      await chatWithAgent(result.text)
      return
    } else {
      messages.value.push({ role: 'user', text: '(未识别到内容)' })
    }
  } catch (err: any) {
    messages.value.push({ role: 'system', text: '语音识别错误: ' + err.message })
  }
  recordStatus.value = ''
  scrollToBottom()
}

// ── 文字对话 ──────────────────────────────────────────
async function sendText() {
  const text = textInput.value.trim()
  if (!text || processing.value) return
  await chatWithAgent(text)
}

async function chatWithAgent(text: string) {
  messages.value.push({ role: 'user', text })
  textInput.value = ''
  processing.value = true
  scrollToBottom()

  try {
    if (!wsSend) {
      messages.value.push({ role: 'assistant', text: 'WebSocket 未连接，无法对话' })
    } else {
      const result = await wsSend('chat', { message: text })
      if (result.error) {
        messages.value.push({ role: 'assistant', text: '错误: ' + result.error })
      } else if (result.response) {
        messages.value.push({ role: 'assistant', text: result.response })
      } else {
        messages.value.push({ role: 'assistant', text: '(无响应)' })
      }
    }
  } catch (err: any) {
    messages.value.push({ role: 'assistant', text: '抱歉，出了点问题: ' + err.message })
  } finally {
    processing.value = false
    scrollToBottom()
  }
}

function scrollToBottom() {
  nextTick(() => {
    if (historyRef.value) {
      historyRef.value.scrollTop = historyRef.value.scrollHeight
    }
  })
}
</script>

<style scoped>
.panel {
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 340px;
  height: 420px;
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

.voice-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.chat-history {
  flex: 1;
  overflow-y: auto;
  padding: 12px 16px;
}

.chat-msg {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
  align-items: flex-start;
}

.chat-msg.user {
  flex-direction: row-reverse;
}

.chat-avatar {
  font-size: 20px;
  flex-shrink: 0;
}

.chat-text {
  background: rgba(255, 255, 255, 0.08);
  padding: 8px 12px;
  border-radius: 12px;
  font-size: 12px;
  line-height: 1.5;
  max-width: 220px;
  word-break: break-all;
}

.chat-msg.user .chat-text {
  background: rgba(255, 140, 50, 0.3);
}

.chat-msg.system .chat-text {
  background: rgba(255, 255, 255, 0.03);
  color: #888;
  font-style: italic;
  font-size: 11px;
}

.chat-text.thinking {
  color: #888;
  font-style: italic;
}

.voice-controls {
  padding: 12px 16px 16px;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
}

.voice-btn {
  width: 100%;
  padding: 12px;
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 12px;
  color: #e0e0e0;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  font-size: 13px;
  transition: all 0.2s;
  margin-bottom: 10px;
}

.voice-btn:hover {
  background: rgba(255, 255, 255, 0.1);
}

.voice-btn.recording {
  background: rgba(239, 68, 68, 0.2);
  border-color: rgba(239, 68, 68, 0.4);
  color: #f87171;
}

.voice-icon {
  font-size: 18px;
}

.text-input-row {
  display: flex;
  gap: 8px;
}

.text-input {
  flex: 1;
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 8px;
  padding: 8px 12px;
  color: #fff;
  font-size: 12px;
  outline: none;
}
.text-input:focus {
  border-color: rgba(255, 140, 50, 0.5);
}
.text-input::placeholder {
  color: #555;
}

.send-btn {
  background: rgba(255, 140, 50, 0.8);
  border: none;
  border-radius: 8px;
  padding: 8px 14px;
  color: #fff;
  cursor: pointer;
  font-size: 12px;
}
.send-btn:hover {
  background: rgba(255, 140, 50, 1);
}
.send-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
</style>
