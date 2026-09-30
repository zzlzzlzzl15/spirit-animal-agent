<template>
  <div class="cli-terminal-page">
    <div class="terminal-header">
      <span class="terminal-title">Spirit Agent CLI</span>
      <span class="terminal-hint">使用 Ctrl+C/V 复制粘贴</span>
      <div class="terminal-controls">
        <button class="ctrl-btn" @click="clearTerminal" title="清屏"></button>
        <button class="ctrl-btn" @click="closeWindow" title="关闭">✕</button>
      </div>
    </div>
    <div ref="terminalRef" class="terminal-container"></div>
    <div class="status-bar" v-if="statusInfo">
      <span class="sb-run" v-if="runInfo.running">{{ runInfo.frame }} {{ runInfo.phase }} {{ runInfo.elapsed }}</span>
      <span class="sb-idle" v-else>◌ 空闲</span>
      <span class="sb-sep">│</span>
      <span class="sb-model">⚡ {{ statusInfo.model || '未连接' }}</span>
      <span class="sb-sep">│</span>
      <span class="sb-session">⏱ {{ statusInfo.duration }}</span>
      <span class="sb-sep">│</span>
      <span class="sb-msgs">💬 {{ statusInfo.messages }}</span>
      <span class="sb-sep">│</span>
      <span class="sb-tools">🔧 {{ statusInfo.tools }}</span>
      <span class="sb-status" :class="{ connected: wsConnected, disconnected: !wsConnected }">
        {{ wsConnected ? '●' : '○' }}
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, reactive } from 'vue'
import { Terminal } from 'xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import 'xterm/css/xterm.css'

// ── WebSocket 连接 ──────────────────────────────────────────

let ws: WebSocket | null = null
let reqId = 0
let wsConnected = ref(false)
const pendingRequests = new Map<string, { resolve: (v: any) => void; reject: (e: any) => void }>()

// ── 状态栏信息 ──────────────────────────────────────────────
const statusInfo = reactive({
  model: '',
  duration: '0s',
  messages: 0,
  tools: 0,
})
let sessionStartTime = Date.now()
let durationTimer: ReturnType<typeof setInterval> | null = null

function formatDuration(ms: number): string {
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const rs = s % 60
  if (m < 60) return `${m}m${rs}s`
  const h = Math.floor(m / 60)
  const rm = m % 60
  return `${h}h${rm}m`
}

function startDurationTimer() {
  if (durationTimer) return
  durationTimer = setInterval(() => {
    statusInfo.duration = formatDuration(Date.now() - sessionStartTime)
  }, 1000)
}

// ── 运行指示（底部状态栏 spinner + 当前步骤计时）─────────────
// 运行中底部状态栏不停转动（当前阶段 + 本步已耗时），空闲显示"空闲"，
// 一眼可辨 Agent 是在干活还是停了。
const runInfo = reactive({ running: false, phase: '', elapsed: '0.0s', frame: '⠋' })
let runTimer: ReturnType<typeof setInterval> | null = null
let runPhaseStart = 0
let runFrameIdx = 0
const pendingToolStarts: number[] = []  // tool_start 起始时间戳 FIFO，tool_complete 取出做每步计时

function setRunning(phase: string) {
  runInfo.running = true
  runInfo.phase = phase
  runPhaseStart = Date.now()
  runInfo.elapsed = '0.0s'
  if (runTimer) return
  runTimer = setInterval(() => {
    runFrameIdx = (runFrameIdx + 1) % SPINNER_FRAMES.length
    runInfo.frame = SPINNER_FRAMES[runFrameIdx]
    runInfo.elapsed = ((Date.now() - runPhaseStart) / 1000).toFixed(1) + 's'
  }, 100)
}

function setIdle() {
  runInfo.running = false
  pendingToolStarts.length = 0
  if (runTimer) {
    clearInterval(runTimer)
    runTimer = null
  }
}

function wsConnect(): Promise<void> {
  return new Promise((resolve, reject) => {
    ws = new WebSocket('ws://127.0.0.1:9877')
    ws.onopen = () => {
      wsConnected.value = true
      term?.writeln('\x1b[90m✓ 已连接到 Spirit Agent 后端\x1b[0m')
      resolve()
    }
    ws.onerror = () => {
      term?.writeln('\x1b[91m✗ 无法连接到后端，请确保 Spirit 服务正在运行\x1b[0m')
      reject(new Error('WebSocket 连接失败'))
    }
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        if (msg.type === 'response' && msg.id) {
          const pending = pendingRequests.get(msg.id)
          if (pending) {
            pendingRequests.delete(msg.id)
            pending.resolve(msg.data)
          }
        } else if (msg.type === 'event') {
          handleEvent(msg)
        }
      } catch { /* ignore parse errors */ }
    }
    ws.onclose = () => {
      wsConnected.value = false
      term?.writeln('\x1b[93m 连接已断开\x1b[0m')
    }
  })
}

function wsSend(action: string, data: any = {}, timeoutMs: number = 30000): Promise<any> {
  return new Promise((resolve, reject) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      reject(new Error('未连接'))
      return
    }
    const id = `req-${++reqId}`
    pendingRequests.set(id, { resolve, reject })
    ws.send(JSON.stringify({ type: 'command', action, id, data }))
    setTimeout(() => {
      if (pendingRequests.has(id)) {
        pendingRequests.delete(id)
        reject(new Error('请求超时'))
      }
    }, timeoutMs)
  })
}

function handleEvent(msg: any) {
  const event = msg.event || msg.data?.event || ''
  const data = msg.data || {}

  switch (event) {
    case 'init':
      // 初始状态同步 — 宠物状态，启动计时器
      startDurationTimer()
      // 主动获取 Agent 状态以填充状态栏
      wsSend('get_status').then((data: any) => {
        if (data.model) statusInfo.model = data.model
        if (data.message_count !== undefined) statusInfo.messages = data.message_count
        if (data.tool_count !== undefined) statusInfo.tools = data.tool_count
      }).catch(() => {})
      break

    case 'tool_start':
      // 工具开始执行 — 在终端显示进度（内联标注）
      toolEventCount++
      pendingToolStarts.push(Date.now())
      setRunning(`执行 ${data.tool}`)
      if (term && isStreaming) {
        flushMarkdown()  // 先flush未完成行，避免标注插进半行中间
        term.write('\r\x1b[K')  // 清掉当前 spinner 行，避免标注接在 spinner 后面
        term.writeln(`\x1b[93m  🔧 调用工具: ${data.tool}\x1b[0m`)
        if (data.args_preview) {
          const rawPreview = String(data.args_preview).replace(/[\r\n]+/g, ' ')
          const preview = rawPreview.substring(0, 80)
          term.writeln(`\x1b[90m     参数: ${preview}${rawPreview.length > 80 ? '...' : ''}\x1b[0m`)
        }
        startSpinner(`执行 ${data.tool}`)  // 工具执行期间也保持转动（带本步计时）
      }
      break

    case 'tool_complete':
      // 每步计时：与 tool_start FIFO 配对，完成行显示本步耗时
      const toolStart = pendingToolStarts.shift()
      const toolDur = toolStart ? ` (${((Date.now() - toolStart) / 1000).toFixed(1)}s)` : ''
      setRunning('思考中')
      if (term && isStreaming) {
        flushMarkdown()
        term.write('\r\x1b[K')
        term.writeln(`\x1b[92m  ✓ ${data.tool} 完成\x1b[0m\x1b[90m${toolDur}\x1b[0m`)
        startSpinner('思考中')  // 到下一个事件（思考/等流）的空档继续转
      }
      break

    case 'delegate_event':
      // 子 Agent 进度（后端 delegate_tool sink 广播）：流内树状行 + 状态块
      handleDelegateEvent(data)
      break

    case 'resume_round':
      // 超时续跑边界：清空工具计时 FIFO，防续跑轮工具完成行
      // 配对到上一轮的起始时间戳（曾现 list_dir 1356s 假耗时）
      pendingToolStarts.length = 0
      break

    case 'stream_notice':
      // 过程通知（fallback 切换等）：可见但不计入 streamBuffer ——
      // 若计入，失败轮轮末 chat_complete 携带的错误结论兜底渲染会被
      // "已流式送达"判定吞掉，整轮对用户完全静默（静默事故根因）。
      console.log('[cli] stream_notice len=', (data.text || '').length)
      if (term && isStreaming && data.text) {
        stopSpinner()
        feedMarkdown(data.text)
        startSpinner('思考中')  // 通知后是退避重试空档，继续转
      }
      break

    case 'stream_delta':
      // 流式文本增量 — 逐行缓冲 + Markdown 格式化渲染
      // （不能直接 term.write：裸 \n 在 xterm 中不换列，会导致阶梯状错行）
      console.log('[cli] stream_delta len=', (data.text || '').length)
      if (term && isStreaming && data.text) {
        streamBuffer += data.text
        // 第一个 delta，停止 spinner 并换行到输出区域
        if (streamBuffer.length === data.text.length) {
          stopSpinner()
          setRunning('输出中')  // 流式输出期间状态栏继续转
        }
        feedMarkdown(data.text)
      }
      break

    case 'chat_complete':
      // 对话完成：停 spinner + 工具汇总兜底 + 最终文本兜底渲染。
      // 最终文本优先由 stream_delta 实时渲染；若增量未送达（乱序/丢失），
      // 在这里用 chat_complete 携带的 response 兜底，保证结果绝不丢失。
      stopSpinner()
      setIdle()
      console.log('[cli] chat_complete resp_len=', (data.response || '').length, 'streamBuffer=', streamBuffer.length, 'toolEvents=', toolEventCount)
      flushMarkdown()

      if (data.tool_calls_formatted && toolEventCount === 0) {
        term!.writeln('')
        for (const line of String(data.tool_calls_formatted).split('\n')) {
          term!.writeln(line)
        }
      }

      if (!streamBuffer && !finalRendered && data.response) {
        finalRendered = true
        term!.writeln('')
        for (const line of String(data.response).split('\n')) {
          const formatted = formatMarkdownLine(line)
          if (formatted !== null) term!.writeln(formatted)
        }
      }
      break

    case 'state_change':
      // 状态变化
      break
  }
}

// ── 斜杠命令系统（参考 Hermes COMMAND_REGISTRY 分类设计） ─────

interface SlashCommand {
  desc: string
  category: string
  usage: string
  handler: (args: string) => Promise<void>
}

const SLASH_COMMANDS: Record<string, SlashCommand> = {
  // ═══ 会话 Session ═══
  dlog: {
    desc: '回看最近一次 delegate_task 子Agent 完整执行过程',
    category: 'Info',
    usage: '/dlog [子Agent序号，如 1]',
    handler: async (args) => {
      const n = args.trim()
      dumpDlgLog(n ? `子Agent-${n}` : undefined)
    },
  },
  help: {
    desc: '显示帮助信息',
    category: 'Info',
    usage: '/help [category]',
    handler: async (args) => {
      const cat = args.trim().toLowerCase()
      if (!cat) {
        term!.writeln('\x1b[1;93m═══ Spirit Agent CLI 命令 ═══\x1b[0m')
        term!.writeln('')
        // 按分类显示
        const categories: Record<string, [string, SlashCommand][]> = {}
        for (const [name, cmd] of Object.entries(SLASH_COMMANDS)) {
          if (!categories[cmd.category]) categories[cmd.category] = []
          categories[cmd.category].push([name, cmd])
        }
        const catOrder = ['Session', 'Configuration', 'Tools & Skills', 'Info']
        for (const catName of catOrder) {
          const cmds = categories[catName]
          if (!cmds) continue
          term!.writeln(`  \x1b[1;96m── ${catName} ──\x1b[0m`)
          for (const [name, cmd] of cmds) {
            term!.writeln(`    \x1b[93m/${name.padEnd(12)}\x1b[0m ${cmd.desc}`)
          }
          term!.writeln('')
        }
        term!.writeln('\x1b[90m  直接输入文字即可与 Agent 对话\x1b[0m')
        term!.writeln('\x1b[90m  ↑/↓ 方向键浏览历史  Enter 发送  Ctrl+C 取消\x1b[0m')
        term!.writeln('\x1b[90m  Tab 自动补全命令  Ctrl+L 清屏  Ctrl+W 删词\x1b[0m')
        term!.writeln('\x1b[90m  底部状态栏显示: 模型 | 会话时长 | 消息数 | 工具数\x1b[0m')
        term!.writeln('')
      } else {
        // 显示某个分类的详细
        const cmds = Object.entries(SLASH_COMMANDS).filter(([, c]) => c.category.toLowerCase() === cat)
        if (cmds.length === 0) {
          term!.writeln(`\x1b[91m  未知分类: ${cat}\x1b[0m`)
        } else {
          term!.writeln(`\x1b[1;96m── ${cat} 命令详情 ──\x1b[0m`)
          for (const [name, cmd] of cmds) {
            term!.writeln(`  \x1b[93m/${name}\x1b[0m  ${cmd.desc}`)
            term!.writeln(`    \x1b[90m用法: ${cmd.usage}\x1b[0m`)
          }
          term!.writeln('')
        }
      }
    },
  },
  new: {
    desc: '开始新会话（清空对话历史）',
    category: 'Session',
    usage: '/new',
    handler: async () => {
      try {
        await wsSend('new_session')
        term!.writeln('\x1b[92m  已开始新会话，对话历史已清空\x1b[0m')
        term!.writeln('')
      } catch (e: any) {
        term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
      }
    },
  },
  tasks: {
    desc: '显示最近 5 个任务会话（点击可恢复并继续）',
    category: 'Session',
    usage: '/tasks',
    handler: async () => {
      await showRecentSessions()
    },
  },
  history: {
    desc: '显示对话历史摘要',
    category: 'Session',
    usage: '/history [N]',
    handler: async (args) => {
      try {
        const n = parseInt(args.trim()) || 10
        const data = await wsSend('get_history', { count: n })
        if (data.messages?.length) {
          term!.writeln(`\x1b[1;93m═══ 对话历史 (最近 ${data.messages.length} 条) ═══\x1b[0m`)
          for (const msg of data.messages) {
            const role = msg.role === 'user' ? '\x1b[96m你\x1b[0m' : '\x1b[93mAgent\x1b[0m'
            const text = (msg.content || '').substring(0, 120)
            term!.writeln(`  ${role}: ${text}${msg.content?.length > 120 ? '...' : ''}`)
          }
        } else {
          term!.writeln('\x1b[90m  暂无对话历史\x1b[0m')
        }
        term!.writeln('')
      } catch (e: any) {
        term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
      }
    },
  },
  status: {
    desc: '查看 Agent 运行状态',
    category: 'Session',
    usage: '/status',
    handler: async () => {
      try {
        const data = await wsSend('get_status')
        term!.writeln('\x1b[1;93m═══ Agent 状态 ═══\x1b[0m')
        term!.writeln(`  状态:     \x1b[${data.running ? '92m运行中' : '90m空闲'}\x1b[0m`)
        term!.writeln(`  模型:     \x1b[97m${data.model || '未配置'}\x1b[0m`)
        term!.writeln(`  Provider: \x1b[97m${data.provider || '-'}\x1b[0m`)
        term!.writeln(`  会话:     \x1b[90m${(data.session_id || '-').substring(0, 8)}\x1b[0m`)
        term!.writeln(`  消息数:   \x1b[97m${data.message_count ?? 0}\x1b[0m`)
        term!.writeln(`  工具数:   \x1b[97m${data.tool_count ?? 0}\x1b[0m`)
        term!.writeln(`  API调用:  \x1b[97m${data.api_call_count ?? 0}\x1b[0m`)
        term!.writeln('')
      } catch (e: any) {
        term!.writeln(`\x1b[91m  获取状态失败: ${e.message}\x1b[0m`)
      }
    },
  },
  compress: {
    desc: '压缩对话上下文',
    category: 'Session',
    usage: '/compress',
    handler: async () => {
      try {
        const data = await wsSend('compress_context')
        term!.writeln(`\x1b[92m  ${data.message || '上下文已压缩'}\x1b[0m`)
        term!.writeln('')
      } catch (e: any) {
        term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
      }
    },
  },
  stop: {
    desc: '停止当前运行',
    category: 'Session',
    usage: '/stop',
    handler: async () => {
      try {
        await wsSend('idle')
        term!.writeln('\x1b[92m  已停止\x1b[0m')
        term!.writeln('')
      } catch (e: any) {
        term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
      }
    },
  },

  // ═══ 配置 Configuration ═══
  config: {
    desc: '查看或修改配置',
    category: 'Configuration',
    usage: '/config [get|set|list]',
    handler: async (args) => {
      const parts = args.trim().split(/\s+/)
      const sub = parts[0]?.toLowerCase()
      if (!sub || sub === 'list') {
        try {
          const data = await wsSend('get_config')
          term!.writeln('\x1b[1;93m═══ 当前配置 ═══\x1b[0m')
          const cfg = data.config || data
          for (const [key, val] of Object.entries(cfg)) {
            if (key === 'api_key') {
              term!.writeln(`  \x1b[96m${key}\x1b[0m: \x1b[90m${val ? '***已设置***' : '未设置'}\x1b[0m`)
            } else {
              term!.writeln(`  \x1b[96m${key}\x1b[0m: \x1b[97m${val ?? '-'}\x1b[0m`)
            }
          }
          term!.writeln('')
          term!.writeln('\x1b[90m  用法: /config set <key> <value>\x1b[0m')
          term!.writeln('')
        } catch (e: any) {
          term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
        }
      } else if (sub === 'set' && parts.length >= 3) {
        const key = parts[1]
        const value = parts.slice(2).join(' ')
        try {
          await wsSend('set_config', { key, value })
          term!.writeln(`\x1b[92m  已设置 ${key} = ${value}\x1b[0m`)
          term!.writeln('')
        } catch (e: any) {
          term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
        }
      } else if (sub === 'get' && parts.length >= 2) {
        const key = parts[1]
        try {
          const data = await wsSend('get_config_value', { key })
          term!.writeln(`  \x1b[96m${key}\x1b[0m = \x1b[97m${data.value ?? '未设置'}\x1b[0m`)
          term!.writeln('')
        } catch (e: any) {
          term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
        }
      } else {
        term!.writeln('\x1b[91m  用法: /config [list] | /config set <key> <value> | /config get <key>\x1b[0m')
      }
    },
  },
  model: {
    desc: '切换 AI 模型',
    category: 'Configuration',
    usage: '/model [name]',
    handler: async (args) => {
      const modelName = args.trim()
      if (!modelName) {
        try {
          const data = await wsSend('get_config_value', { key: 'model' })
          term!.writeln(`  当前模型: \x1b[97m${data.value || '未配置'}\x1b[0m`)
          term!.writeln('\x1b[90m  用法: /model <模型名称>\x1b[0m')
          term!.writeln('\x1b[90m  示例: /model gpt-4o, /model claude-3-sonnet, /model qwen-plus\x1b[0m')
          term!.writeln('')
        } catch (e: any) {
          term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
        }
        return
      }
      try {
        await wsSend('set_config', { key: 'model', value: modelName })
        term!.writeln(`\x1b[92m  模型已切换为: ${modelName}\x1b[0m`)
        term!.writeln('')
      } catch (e: any) {
        term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
      }
    },
  },
  provider: {
    desc: '切换 AI 提供商',
    category: 'Configuration',
    usage: '/provider [name]',
    handler: async (args) => {
      const providerName = args.trim()
      if (!providerName) {
        try {
          const data = await wsSend('get_config_value', { key: 'provider' })
          term!.writeln(`  当前提供商: \x1b[97m${data.value || 'auto'}\x1b[0m`)
          term!.writeln('\x1b[90m  用法: /provider <名称>\x1b[0m')
          term!.writeln('\x1b[90m  可选: openai, anthropic, azure, ollama, dashscope, auto\x1b[0m')
          term!.writeln('')
        } catch (e: any) {
          term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
        }
        return
      }
      try {
        await wsSend('set_config', { key: 'provider', value: providerName })
        term!.writeln(`\x1b[92m  提供商已切换为: ${providerName}\x1b[0m`)
        term!.writeln('')
      } catch (e: any) {
        term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
      }
    },
  },
  scale: {
    desc: '设置桌宠缩放',
    category: 'Configuration',
    usage: '/scale <0.2-3.0>',
    handler: async (args) => {
      const val = parseFloat(args.trim())
      if (isNaN(val) || val < 0.2 || val > 3.0) {
        term!.writeln('\x1b[91m  用法: /scale <0.2-3.0>\x1b[0m')
        return
      }
      try {
        await wsSend('set_scale', { scale: val })
        term!.writeln(`\x1b[92m  缩放已设置为 ${val}\x1b[0m`)
        term!.writeln('')
      } catch (e: any) {
        term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
      }
    },
  },

  // ═══ 工具与技能 Tools & Skills ═══
  tools: {
    desc: '列出可用工具',
    category: 'Tools & Skills',
    usage: '/tools',
    handler: async () => {
      try {
        const data = await wsSend('list_tools')
        if (data.tools?.length) {
          term!.writeln('\x1b[1;93m═══ 可用工具 ═══\x1b[0m')
          for (const t of data.tools) {
            term!.writeln(`  \x1b[96m${(t.name || t).toString().padEnd(20)}\x1b[0m ${t.description || ''}`)
          }
        } else {
          term!.writeln('\x1b[90m  暂无可用工具\x1b[0m')
        }
        term!.writeln('')
      } catch (e: any) {
        term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
      }
    },
  },
  pets: {
    desc: '列出可用宠物',
    category: 'Tools & Skills',
    usage: '/pets',
    handler: async () => {
      try {
        const data = await wsSend('list_pets')
        term!.writeln('\x1b[1;93m═══ 可用宠物 ═══\x1b[0m')
        if (data.pets?.length) {
          for (const pet of data.pets) {
            term!.writeln(`  \x1b[96m${(pet.slug || pet.name || '').padEnd(15)}\x1b[0m  ${pet.name || ''}`)
          }
        } else {
          term!.writeln('  \x1b[90m暂无宠物\x1b[0m')
        }
        term!.writeln('')
      } catch (e: any) {
        term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
      }
    },
  },
  switch: {
    desc: '切换宠物',
    category: 'Tools & Skills',
    usage: '/switch <slug>',
    handler: async (args) => {
      const slug = args.trim()
      if (!slug) {
        term!.writeln('\x1b[91m  用法: /switch <slug>\x1b[0m')
        return
      }
      try {
        const data = await wsSend('switch_pet', { slug })
        term!.writeln(data.success ? `\x1b[92m  已切换到 ${slug}\x1b[0m` : `\x1b[91m  切换失败\x1b[0m`)
        term!.writeln('')
      } catch (e: any) {
        term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
      }
    },
  },
  search: {
    desc: '搜索知识库',
    category: 'Tools & Skills',
    usage: '/search <关键词>',
    handler: async (args) => {
      if (!args.trim()) {
        term!.writeln('\x1b[91m  用法: /search <关键词>\x1b[0m')
        return
      }
      try {
        const data = await wsSend('search_knowledge', { query: args.trim() })
        if (data.results?.length) {
          term!.writeln(`\x1b[1;93m  找到 ${data.results.length} 条结果:\x1b[0m`)
          for (const r of data.results) {
            term!.writeln(`  \x1b[96m•\x1b[0m ${r.title || r.name || '未知'}`)
          }
        } else {
          term!.writeln('\x1b[90m  未找到相关结果\x1b[0m')
        }
        term!.writeln('')
      } catch (e: any) {
        term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
      }
    },
  },

  // ═══ 信息 Info ═══
  sys: {
    desc: '查看系统信息',
    category: 'Info',
    usage: '/sys',
    handler: async () => {
      try {
        const data = await wsSend('get_system_status')
        term!.writeln('\x1b[1;93m═══ 系统信息 ═══\x1b[0m')
        if (data.cpu) {
          term!.writeln(`  CPU:  \x1b[97m${data.cpu.brand || '未知'}\x1b[0m`)
          term!.writeln(`  使用: \x1b[97m${data.cpu.usage_percent ?? 0}%\x1b[0m`)
        }
        if (data.memory) {
          term!.writeln(`  内存: \x1b[97m${data.memory.used_gb ?? 0} / ${data.memory.total_gb ?? 0} GB\x1b[0m`)
          term!.writeln(`  使用: \x1b[97m${data.memory.usage_percent ?? 0}%\x1b[0m`)
        }
        term!.writeln('')
      } catch (e: any) {
        term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
      }
    },
  },
  version: {
    desc: '显示版本信息',
    category: 'Info',
    usage: '/version',
    handler: async () => {
      try {
        const data = await wsSend('get_version')
        term!.writeln('\x1b[1;93m═══ 版本信息 ═══\x1b[0m')
        term!.writeln(`  Spirit Agent:  \x1b[97m${data.version || '1.0.0'}\x1b[0m`)
        term!.writeln(`  Python:        \x1b[97m${data.python || '-'}\x1b[0m`)
        term!.writeln(`  Node:          \x1b[97m${data.node || '-'}\x1b[0m`)
        term!.writeln(`  Electron:      \x1b[97m${data.electron || '-'}\x1b[0m`)
        term!.writeln('')
      } catch (e: any) {
        term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
      }
    },
  },
  chat: {
    desc: '与 Agent 对话',
    category: 'Info',
    usage: '/chat <消息>',
    handler: async (args) => {
      if (!args.trim()) {
        term!.writeln('\x1b[91m  用法: /chat <消息>\x1b[0m')
        return
      }
      await sendChatMessage(args.trim())
    },
  },
  ping: {
    desc: '测试连接',
    category: 'Info',
    usage: '/ping',
    handler: async () => {
      const start = Date.now()
      try {
        await wsSend('ping')
        const latency = Date.now() - start
        term!.writeln(`\x1b[92m  Pong! 延迟: ${latency}ms\x1b[0m`)
        term!.writeln('')
      } catch (e: any) {
        term!.writeln(`\x1b[91m  ${e.message}\x1b[0m`)
      }
    },
  },
  clear: {
    desc: '清屏',
    category: 'Info',
    usage: '/clear',
    handler: async () => {
      term!.clear()
    },
  },
  exit: {
    desc: '关闭终端',
    category: 'Info',
    usage: '/exit',
    handler: async () => {
      closeWindow()
    },
  },
}

// ── 终端初始化 ──────────────────────────────────────────────

const terminalRef = ref<HTMLElement>()
let term: Terminal | null = null
let fitAddon: FitAddon | null = null
let inputBuffer = ''
let cursorPos = 0
let history: string[] = []
let historyIndex = -1
let isProcessing = false
let isStreaming = false
let streamBuffer = ''
let toolEventCount = 0
let finalRendered = false

// ── Spinner 动画（参考 Hermes _COMMAND_SPINNER_FRAMES）────────

const SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
let spinnerTimer: ReturnType<typeof setInterval> | null = null
let spinnerFrameIdx = 0
let spinnerText = ''
let spinnerStartTime = 0

function startSpinner(text: string) {
  spinnerText = text
  spinnerStartTime = Date.now()
  spinnerFrameIdx = 0
  stopSpinner()
  spinnerTimer = setInterval(() => {
    if (!term) return
    spinnerFrameIdx = (spinnerFrameIdx + 1) % SPINNER_FRAMES.length
    const elapsed = ((Date.now() - spinnerStartTime) / 1000).toFixed(1)
    const frame = SPINNER_FRAMES[spinnerFrameIdx]
    // 写入 spinner 行（覆盖当前行）
    term.write(`\r\x1b[K\x1b[93m  ${frame} ${spinnerText}\x1b[0m\x1b[90m (${elapsed}s)\x1b[0m`)
  }, 100)
}

function stopSpinner() {
  if (spinnerTimer) {
    clearInterval(spinnerTimer)
    spinnerTimer = null
  }
  if (term) {
    term.write('\r\x1b[K')
  }
}

// ── 子 Agent 实时视图（delegate_task 流内树状行 + 底部状态块）──────────
// 参考 Hermes delegate_tool 的 spinner.print_above 树状行与 Claude Code 的
// condensed/transcript 双档密度：默认把子 Agent 工具/思考树状行流式打进
// scrollback（带子 Agent 名称前缀），Ctrl+D 收起为仅状态块，/dlog 回看全程。
interface DChild {
  id: string
  name: string
  goal: string
  status: 'running' | 'done' | 'error'
  turn: number
  toolCount: number
  genChars: number
  cur: string
  curSince: number
  doneLine: string
  attempt: number
}
const dlg = reactive({ active: false, expanded: true, startedAt: 0, children: [] as DChild[] })
let dlgLog: { name: string; line: string }[] = []
let dlgBlockLines = 0
let dlgTimer: ReturnType<typeof setInterval> | null = null

function dlgChild(id: string): DChild | undefined {
  return dlg.children.find(c => c.id === id)
}

function dlgFmtAge(ms: number): string {
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m${s % 60}s`
}

// 擦除底部状态块（光标回到块下方原位）
function dlgClearBlock() {
  if (!term || dlgBlockLines <= 0) return
  term.write(`\x1b[${dlgBlockLines}A`)
  for (let i = 0; i < dlgBlockLines; i++) term.write('\r\x1b[K\r\n')
  dlgBlockLines = 0
}

// 原地重绘状态块：头行 + 每子 Agent 一行（行数固定，无残留问题）
function dlgRenderBlock() {
  if (!term || !dlg.active) return
  const running = dlg.children.filter(c => c.status === 'running').length
  const done = dlg.children.length - running
  const lines: string[] = []
  lines.push(
    `\x1b[36m🔀 子Agent ×${dlg.children.length} · 运行 ${running} · 完成 ${done} · 总耗时 ${dlgFmtAge(Date.now() - dlg.startedAt)} · Ctrl+D ${dlg.expanded ? '收起' : '展开'}\x1b[0m`
  )
  for (const c of dlg.children) {
    if (c.status === 'running') {
      const step = c.cur ? ` · ${c.cur} (${dlgFmtAge(Date.now() - c.curSince)})` : ''
      const gen = c.genChars > 0 ? ` · 生成 ${(c.genChars / 1000).toFixed(1)}k 字` : ''
      const att = c.attempt > 1 ? ` · 尝试 ${c.attempt}` : ''
      lines.push(`\x1b[90m [${c.name}] ⏳ 轮 ${c.turn} · 工具 ${c.toolCount}${att}${step}${gen}\x1b[0m`)
    } else if (c.status === 'done') {
      lines.push(`\x1b[92m [${c.name}] ${c.doneLine}\x1b[0m`)
    } else {
      lines.push(`\x1b[91m [${c.name}] ${c.doneLine}\x1b[0m`)
    }
  }
  if (dlgBlockLines > 0) term.write(`\x1b[${dlgBlockLines}A`)
  lines.forEach(ln => term!.write(`\r\x1b[K${ln}\r\n`))
  dlgBlockLines = lines.length
}

// 永久行：状态块在底部时先擦块、写行、再重绘块
function dlgPrint(line: string) {
  if (!term) return
  flushMarkdown()
  if (dlg.active) {
    dlgClearBlock()
    term.writeln(line)
    dlgRenderBlock()
  } else {
    term.writeln(line)
  }
}

function dumpDlgLog(filterName?: string) {
  if (!term) return
  if (!dlgLog.length) {
    term.writeln('\x1b[90m（暂无 delegate_task 执行记录）\x1b[0m')
    return
  }
  term.writeln(`\x1b[36m── delegate_task 完整执行过程${filterName ? `（${filterName}）` : ''} ──\x1b[0m`)
  for (const e of dlgLog) {
    if (filterName && e.name !== filterName) continue
    term.writeln(e.line)
  }
  term.writeln('\x1b[36m── 回看结束 ──\x1b[0m')
}

function handleDelegateEvent(data: any) {
  if (!data || !data.phase) return
  const c = data.id ? dlgChild(data.id) : undefined
  switch (data.phase) {
    case 'start': {
      dlg.active = true
      dlg.expanded = true
      dlg.startedAt = Date.now()
      dlg.children = (data.children || []).map((ch: any) => ({
        id: ch.id, name: ch.name, goal: ch.goal || '',
        status: 'running' as const, turn: 0, toolCount: 0, genChars: 0,
        cur: '', curSince: Date.now(), doneLine: '', attempt: 1,
      }))
      dlgLog = []
      stopSpinner()
      setRunning('委派子Agent')
      dlgPrint(`\x1b[36m🔀 delegate_task · ${dlg.children.length} 个子Agent 已派发\x1b[0m\x1b[90m（树状行直播中 · Ctrl+D 收起 · /dlog 回看）\x1b[0m`)
      for (const ch of dlg.children) {
        dlgPrint(`\x1b[90m ├─ \x1b[36m[${ch.name}]\x1b[0m\x1b[90m 🔀 ${ch.goal}\x1b[0m`)
      }
      if (dlgTimer) clearInterval(dlgTimer)
      dlgTimer = setInterval(() => dlgRenderBlock(), 1000)
      dlgRenderBlock()
      break
    }
    case 'child_turn':
      if (c) { c.turn = data.turn || c.turn + 1; c.cur = '💭 思考/生成中'; c.curSince = Date.now(); c.genChars = 0 }
      dlgRenderBlock()
      break
    case 'child_think': {
      if (!c) break
      c.cur = `💭 "${data.text || ''}"`
      c.curSince = Date.now()
      const thinkLine = `\x1b[90m [${c.name}] ├─ 💭 "${data.text || ''}"\x1b[0m`
      dlgLog.push({ name: c.name, line: thinkLine })
      if (dlg.expanded) dlgPrint(thinkLine)
      else dlgRenderBlock()
      break
    }
    case 'child_tool': {
      if (!c) break
      c.toolCount++
      c.cur = `🔧 ${data.tool}`
      c.curSince = Date.now()
      const prev = data.preview ? ` "${data.preview}"` : ''
      const toolLine = `\x1b[90m [${c.name}] ├─ 🔧 ${data.tool}\x1b[0m\x1b[90m${prev}\x1b[0m`
      dlgLog.push({ name: c.name, line: toolLine })
      if (dlg.expanded) dlgPrint(toolLine)
      else dlgRenderBlock()
      break
    }
    case 'child_tool_done': {
      if (!c) break
      const icon = data.ok ? '✓' : '✗'
      const doneToolLine = ` [${c.name}] ├─ \x1b[${data.ok ? 92 : 91}m${icon} ${data.tool} 完成\x1b[0m\x1b[90m (${data.duration ?? '?'}s)\x1b[0m`
      dlgLog.push({ name: c.name, line: doneToolLine })
      c.cur = `${icon} ${data.tool}`
      c.curSince = Date.now()
      if (dlg.expanded) dlgPrint(doneToolLine)
      else dlgRenderBlock()
      break
    }
    case 'child_gen':
      if (c) { c.genChars = data.chars || 0; if (data.turn) c.turn = data.turn }
      dlgRenderBlock()
      break
    case 'child_restart': {
      // 子任务停滞/单次超时/异常 → 后端自动重启：重置计数并打生命周期行
      if (c) {
        c.attempt = data.attempt || c.attempt + 1
        c.status = 'running'
        c.turn = 0; c.toolCount = 0; c.genChars = 0
        c.cur = '♻ 重启中'; c.curSince = Date.now()
      }
      const name = c?.name || data.name || '?'
      const rstLine = `\x1b[93m [${name}] ♻ 重启子任务 · 第 ${data.attempt ?? '?'} 次尝试\x1b[0m\x1b[90m · ${String(data.reason || '').substring(0, 60)}\x1b[0m`
      dlgLog.push({ name, line: rstLine })
      dlgPrint(rstLine)
      break
    }
    case 'child_done': {
      if (c) {
        c.status = data.success ? 'done' : 'error'
        const att = (data.attempts || 1) > 1 ? ` · 尝试 ${data.attempts} 次` : ''
        c.doneLine = data.success
          ? `└─ ✓ 完成 · ${data.iterations ?? '?'} 轮 · ${dlgFmtAge((data.duration || 0) * 1000)}${att}`
          : `└─ ✗ 失败: ${String(data.error || '未知错误').substring(0, 60)}${att}`
      }
      const name = c?.name || data.name || '?'
      const lifeLine = c
        ? `\x1b[${c.status === 'done' ? 92 : 91}m [${name}] ${c.doneLine}\x1b[0m`
        : ` [${name}] └─ ${data.success ? '✓' : '✗'}`
      dlgLog.push({ name, line: lifeLine })
      dlgPrint(lifeLine)  // 生命周期行：收起时也始终可见
      break
    }
    case 'end': {
      const wasActive = dlg.active
      dlg.active = false
      if (dlgTimer) { clearInterval(dlgTimer); dlgTimer = null }
      if (wasActive) dlgClearBlock()
      const total = (data.succeeded ?? 0) + (data.failed ?? 0)
      const head = (data.failed || 0) === 0 ? '\x1b[92m✓' : '\x1b[93m✓'
      const rst = (data.restarted || 0) > 0 ? `\x1b[93m · 重启 ${data.restarted} 次\x1b[0m` : ''
      dlgPrint(`${head} delegate_task 完成 · 成功 ${data.succeeded ?? '?'}/${total}\x1b[0m${rst}\x1b[90m · ${dlgFmtAge((data.duration || 0) * 1000)} · 完整过程: /dlog\x1b[0m`)
      break
    }
  }
}

// ── Markdown 逐行渲染器 ────────────────────────────────
// 流式增量先缓冲，凑满整行再格式化为 ANSI 输出。
// 解决两个问题：
// 1. 裸 \n 直接 term.write 在 xterm 中不换列 → 阶梯状错行
// 2. Markdown 符号（** / ## / - ）原样裸奔 → 转为终端样式

let mdBuffer = ''

function formatMarkdownLine(raw: string): string | null {
  let out = raw.replace(/\s+$/, '')
  if (!out.trim()) return ''

  // 标题：# / ## / ### → 彩色加粗 + 层级缩进
  const h = out.match(/^(#{1,6})\s*(.*)$/)
  if (h) {
    const level = h[1].length
    const color = level === 1 ? '1;93' : level === 2 ? '1;96' : '1;95'
    const prefix = level === 1 ? '■ ' : level === 2 ? '▎' : '  · '
    const title = h[2].replace(/\*\*([^*]+)\*\*/g, '$1')
    return `\x1b[${color}m${prefix}${title}\x1b[0m`
  }

  // 分隔线：--- / *** / ___ → 灰色细线（返回 null 表示跳过该行）
  if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(out)) return null

  // 引用：> text → 灰色竖栏
  const q = out.match(/^\s*>\s?(.*)$/)
  if (q) {
    return `\x1b[90m  ┃ ${q[1]}\x1b[0m`
  }

  // 表格：| a | b | → 竖栏对齐；分隔行（|---|）跳过
  if (/^\s*\|.*\|\s*$/.test(out)) {
    if (/^\s*\|[\s:|-]+\|\s*$/.test(out)) return null
    const cells = out.split('|').slice(1, -1).map(c => c.trim())
    return '  ' + cells.join(' \x1b[90m│\x1b[0m ')
  }

  // 无序列表：- / * → 缩进圆点
  out = out.replace(/^(\s*)[-*]\s+/, '$1  • ')
  // 有序列表：统一两个空格缩进
  out = out.replace(/^(\s*)(\d+)\.\s+/, '  $2. ')
  // 加粗 ***x*** / **x** → ANSI bold
  out = out.replace(/\*\*\*([^*]+)\*\*\*/g, '\x1b[1m$1\x1b[0m')
  out = out.replace(/\*\*([^*]+)\*\*/g, '\x1b[1m$1\x1b[0m')
  // 行内代码 `x` → 青色
  out = out.replace(/`([^`]+)`/g, '\x1b[96m$1\x1b[0m')
  // 链接 [t](u) → 下划线文本 + 灰色 URL
  out = out.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '\x1b[4m$1\x1b[0m\x1b[90m ($2)\x1b[0m')
  // 残留的孤立 ** 标记 → 直接清掉
  out = out.replace(/\*\*/g, '')
  return out
}

function feedMarkdown(text: string) {
  mdBuffer += text
  let idx: number
  while ((idx = mdBuffer.indexOf('\n')) >= 0) {
    const line = mdBuffer.slice(0, idx)
    mdBuffer = mdBuffer.slice(idx + 1)
    const formatted = formatMarkdownLine(line)
    if (formatted !== null) term!.writeln(formatted)
  }
}

function flushMarkdown() {
  if (mdBuffer) {
    const formatted = formatMarkdownLine(mdBuffer)
    if (formatted !== null) term!.writeln(formatted)
    mdBuffer = ''
  }
}

// 提示符可见文本（"spirit ❯ "）占的终端格数 —— rewriteLine 折行行号计算用
const PROMPT_CELLS = 9

function printPrompt() {
  term!.write('\x1b[1;96mspirit\x1b[0m\x1b[90m ❯ \x1b[0m')
}

function printBanner() {
  term!.writeln('')
  term!.writeln('\x1b[1;93m  ╔══════════════════════════════════════════╗\x1b[0m')
  term!.writeln('\x1b[1;93m  ║                                          ║\x1b[0m')
  term!.writeln('\x1b[1;93m  ║    \x1b[0m🦊 Spirit Agent CLI v1.0              \x1b[1;93m║\x1b[0m')
  term!.writeln('\x1b[1;93m  ║    \x1b[0m输入 /help 查看可用命令               \x1b[1;93m║\x1b[0m')
  term!.writeln('\x1b[1;93m  ║    \x1b[0m直接输入文字与 Agent 对话             \x1b[1;93m║\x1b[0m')
  term!.writeln('\x1b[1;93m  ║                                          ║\x1b[0m')
  term!.writeln('\x1b[1;93m  ╚══════════════════════════════════════════╝\x1b[0m')
  term!.writeln('')
}

// ── 最近任务会话（启动/手动 /tasks 展示，点击编号恢复并继续）──────

interface TaskItem {
  session_id: string
  question: string
  current?: boolean
}

// 可点击任务行登记：buffer 行号 → 该行编号与所属会话（LinkProvider 消费）
let sessionLinkLines: { line: number; index: number; sessionId: string }[] = []
// 最近一次任务列表编号对应的会话 id（输入 [n] 恢复的兜底）
let lastTaskIds: string[] = []

async function showRecentSessions() {
  if (!term) return
  try {
    const data = await wsSend('list_sessions', { limit: 5 })
    const tasks: TaskItem[] = data.tasks || []
    if (!tasks.length) return
    term.writeln('\x1b[90m── 最近任务（点击编号或输入 [n] 恢复并继续，/tasks 重显）──\x1b[0m')
    const startLine = term.buffer.active.baseY + term.buffer.active.cursorY + 1
    tasks.forEach((t, i) => {
      const tag = t.current ? ' \x1b[90m(当前)\x1b[0m' : ''
      term!.writeln(`  \x1b[4;96m[${i + 1}]\x1b[0m \x1b[0;97m${t.question}\x1b[0m${tag}`)
      sessionLinkLines.push({ line: startLine + i, index: i, sessionId: t.session_id })
    })
    if (sessionLinkLines.length > 40) sessionLinkLines.splice(0, sessionLinkLines.length - 40)
    lastTaskIds = tasks.map(t => t.session_id)
    term.writeln('')
  } catch {
    // 列表拉取失败不打扰输入（后端未就绪/超时）
  }
}

async function resumeSessionById(sessionId: string) {
  if (!term) return
  if (isProcessing) {
    term.writeln('\x1b[93m  有任务正在运行，请先等待完成再切换会话\x1b[0m')
    return
  }
  isProcessing = true
  try {
    term.writeln('')
    term.writeln('\x1b[90m  正在恢复会话…\x1b[0m')
    const data = await wsSend('resume_session', { session_id: sessionId }, 15000)
    if (data.error) {
      term.writeln(`\x1b[91m  ${data.error}\x1b[0m`)
      return
    }
    term.write('\x1b[2J\x1b[H')  // 清屏，回放历史会话
    sessionLinkLines = []  // 旧列表行已清掉，注销点击命中防误触
    printBanner()
    const history: { role: string; content: string }[] = data.history || []
    const shown = history.slice(-20)
    if (data.total_history > shown.length) {
      term.writeln(`\x1b[90m  …… 更早 ${data.total_history - shown.length} 条已省略 ……\x1b[0m`)
    }
    for (const m of shown) {
      if (m.role === 'user') {
        term.writeln(`\x1b[1;96m  你 ❯\x1b[0m ${m.content.substring(0, 200)}`)
      } else {
        term.writeln(`\x1b[1;93m  Spirit ❯\x1b[0m ${m.content.substring(0, 200)}`)
      }
      term.writeln('')
    }
    term.writeln(
      `\x1b[92m  ✓ 会话已恢复（共 ${data.message_count} 条上下文），直接输入消息即可继续任务\x1b[0m`
    )
    term.writeln('')
  } catch (e: any) {
    term.writeln(`\x1b[91m  恢复失败: ${e.message}\x1b[0m`)
  } finally {
    isProcessing = false
    printPrompt()
  }
}

async function sendChatMessage(message: string) {
  term!.writeln('')
  isStreaming = true
  streamBuffer = ''
  toolEventCount = 0
  finalRendered = false
  mdBuffer = ''
  setRunning('思考中')
  startSpinner('思考中')
  try {
    const data = await wsSend('chat', { message }, 14400000)  // 4 小时看门狗：需覆盖后端单轮 30min ×(1+自动续跑 3)+宽限的总时长；后端真死时 WS 关闭会立即报错，不会干等
    // 清除 spinner
    stopSpinner()
    setIdle()
    flushMarkdown()
    // 错误优先：超时/中断等终止原因必须可见，不得被流式缓冲分支吞掉
    // （旧顺序 if(streamBuffer) 优先 → 有流式文本时 error 永远不渲染，任务静默结束）
    if (data.error) {
      term!.writeln(`\x1b[91m  ✗ ${data.error}\x1b[0m`)
    } else if (streamBuffer) {
      // 如果有流式增量，已经显示过了，只需换行
      term!.writeln('')
    } else if (data.response) {
      // chat_complete 应已兜底渲染；若因乱序未渲染，这里补上
      if (!finalRendered) {
        finalRendered = true
        const lines = data.response.split('\n')
        for (const line of lines) {
          const formatted = formatMarkdownLine(line)
          if (formatted !== null) term!.writeln(formatted)
        }
      }
    } else if (data.message) {
      term!.writeln(`  \x1b[97m${data.message}\x1b[0m`)
    } else {
      term!.writeln(`\x1b[90m  ${JSON.stringify(data)}\x1b[0m`)
    }
    // 更新状态栏
    if (data.usage) {
      statusInfo.messages++
    }
  } catch (e: any) {
    stopSpinner()
    term!.writeln(`\x1b[91m  ✗ ${e.message}\x1b[0m`)
  }
  setIdle()
  isStreaming = false
  streamBuffer = ''
  term!.writeln('')
}

async function processInput(line: string) {
  if (isProcessing) return
  const trimmed = line.trim()
  if (!trimmed) {
    printPrompt()
    return
  }

  // 输入 [n] = 恢复最近任务列表第 n 项（点击编号的键盘兜底）
  const taskPick = trimmed.match(/^\[(\d+)\]$/)
  if (taskPick) {
    const sid = lastTaskIds[parseInt(taskPick[1], 10) - 1]
    if (sid) {
      await resumeSessionById(sid)
      return
    }
  }

  // 添加到历史
  if (history.length === 0 || history[history.length - 1] !== trimmed) {
    history.push(trimmed)
  }
  historyIndex = history.length

  isProcessing = true

  if (trimmed.startsWith('/')) {
    // 斜杠命令
    const parts = trimmed.substring(1).split(/\s+/)
    const cmdName = parts[0].toLowerCase()
    const args = parts.slice(1).join(' ')
    const cmd = SLASH_COMMANDS[cmdName]
    if (cmd) {
      try {
        await cmd.handler(args)
      } catch (e: any) {
        term!.writeln(`\x1b[91m  错误: ${e.message}\x1b[0m`)
      }
    } else {
      // 模糊匹配
      const matches = Object.keys(SLASH_COMMANDS).filter(k => k.startsWith(cmdName))
      if (matches.length === 1) {
        try {
          await SLASH_COMMANDS[matches[0]].handler(args)
        } catch (e: any) {
          term!.writeln(`\x1b[91m  错误: ${e.message}\x1b[0m`)
        }
      } else if (matches.length > 1) {
        term!.writeln(`\x1b[91m  模糊匹配: ${matches.map(m => '/' + m).join(', ')}\x1b[0m`)
      } else {
        term!.writeln(`\x1b[91m  未知命令: /${cmdName}，输入 /help 查看帮助\x1b[0m`)
      }
    }
  } else {
    // 普通对话 → 发送给 Agent
    await sendChatMessage(trimmed)
  }

  isProcessing = false
  printPrompt()
}

function closeWindow() {
  if (window.electronAPI?.closeCLITerminal) {
    window.electronAPI.closeCLITerminal()
  }
}

function clearTerminal() {
  term?.clear()
  printBanner()
  printPrompt()
}

onMounted(async () => {
  if (!terminalRef.value) return

  // 创建 xterm 终端
  term = new Terminal({
    cursorBlink: true,
    cursorStyle: 'bar',
    fontSize: 13,
    fontFamily: "'Cascadia Code', 'Fira Code', 'Consolas', monospace",
    theme: {
      background: '#0a0a0f',
      foreground: '#e0e0e0',
      cursor: '#fb923c',
      cursorAccent: '#0a0a0f',
      selectionBackground: 'rgba(251, 146, 60, 0.3)',
      black: '#0a0a0f',
      red: '#f87171',
      green: '#4ade80',
      yellow: '#fbbf24',
      blue: '#60a5fa',
      magenta: '#c084fc',
      cyan: '#22d3ee',
      white: '#e0e0e0',
      brightBlack: '#555',
      brightRed: '#fca5a5',
      brightGreen: '#86efac',
      brightYellow: '#fde047',
      brightBlue: '#93c5fd',
      brightMagenta: '#d8b4fe',
      brightCyan: '#67e8f9',
      brightWhite: '#fff',
    },
    scrollback: 2000,
    allowProposedApi: true,
  })

  fitAddon = new FitAddon()
  term.loadAddon(fitAddon)
  term.loadAddon(new WebLinksAddon())

  term.open(terminalRef.value)

  // ── 最近任务行点击恢复（DOM 级命中：点中任务行任意位置即恢复其会话）──
  // 不走 xterm registerLinkProvider（激活依赖 hover 命中，Electron 无边框
  // 窗口下不可靠）：直接用鼠标坐标换算点击落在哪个 buffer 行。
  const hitTaskSession = (ev: MouseEvent): string | null => {
    const screenEl = term?.element?.querySelector('.xterm-screen') as HTMLElement | null
    if (!screenEl || !term) return null
    const rect = screenEl.getBoundingClientRect()
    const cellH = rect.height / term.rows
    if (cellH <= 0) return null
    const rowInViewport = Math.floor((ev.clientY - rect.top) / cellH)
    const absLine = term.buffer.active.viewportY + rowInViewport + 1
    const entry = sessionLinkLines.find(l => l.line === absLine)
    return entry ? entry.sessionId : null
  }
  terminalRef.value.addEventListener('click', (ev) => {
    const sid = hitTaskSession(ev)
    if (sid) {
      ev.preventDefault()
      ev.stopPropagation()
      void resumeSessionById(sid)
    }
  })
  terminalRef.value.addEventListener('mousemove', (ev) => {
    if (terminalRef.value) {
      terminalRef.value.style.cursor = hitTaskSession(ev) ? 'pointer' : ''
    }
  })

  // ── 剪贴板快捷键修复（Ctrl+V 粘贴 / Ctrl+C 复制）─────────────
  // Electron 无边框窗口默认不带 Edit 菜单角色，Ctrl+C/V 不会自动
  // 触发浏览器 copy/paste 事件，xterm 的 helper textarea 收不到粘贴。
  // 这里显式接管按键，直接经 preload 暴露的 clipboard API 读写系统剪贴板。
  term.attachCustomKeyEventHandler((e) => {
    const api = window.electronAPI
    const key = e.key?.toLowerCase()

    // 粘贴：Ctrl+V / Ctrl+Shift+V / Shift+Insert
    const isPaste = (e.ctrlKey && key === 'v') || (e.shiftKey && e.key === 'Insert')
    if (isPaste && e.type === 'keydown') {
      if (isProcessing) { e.preventDefault(); return false }  // 处理中忽略粘贴
      if (!api?.clipboardRead) return true                    // 回退：交给默认行为
      e.preventDefault()
      const text = api.clipboardRead()
      if (text) insertText(text)
      return false
    }

    // 子 Agent 视图：Ctrl+D — 运行中收起/展开树状行直播；空闲时回看上次全程
    if (e.ctrlKey && !e.shiftKey && key === 'd' && e.type === 'keydown') {
      e.preventDefault()
      if (dlg.active) {
        dlg.expanded = !dlg.expanded
        dlgClearBlock()
        term?.writeln(dlg.expanded
          ? '\x1b[90m[已展开] 子Agent 执行过程树状行直播到 scrollback\x1b[0m'
          : '\x1b[90m[已收起] 仅显示状态块与生命周期行，/dlog 回看完整过程\x1b[0m')
        dlgRenderBlock()
      } else {
        dumpDlgLog()
      }
      return false
    }

    // 复制：Ctrl+C — 有选区则复制选区，无选区放行（触发 SIGINT 取消当前输入）
    if (e.ctrlKey && !e.shiftKey && key === 'c' && e.type === 'keydown') {
      const sel = term!.getSelection()
      if (sel && api?.clipboardWrite) {
        e.preventDefault()
        api.clipboardWrite(sel)
        return false
      }
      return true
    }

    return true
  })

  // ── IME 焦点修复 ────────────────────────────────────────────
  // 中文输入法确认字符后，xterm.js 内部 textarea 会失去焦点，
  // 导致后续按键无法被捕获。监听 compositionend 事件并重新聚焦。
  const termTextarea = terminalRef.value?.querySelector('.xterm-helper-textarea') as HTMLTextAreaElement | null

  if (termTextarea) {
    // IME 组合输入结束后，延迟重新聚焦 textarea
    termTextarea.addEventListener('compositionend', () => {
      setTimeout(() => {
        termTextarea.focus()
      }, 50)
    })
  }

  // 点击终端区域时确保焦点在 textarea 上
  terminalRef.value?.addEventListener('click', () => {
    const ta = terminalRef.value?.querySelector('.xterm-helper-textarea') as HTMLTextAreaElement | null
    ta?.focus()
  })

  // 全局焦点守卫：窗口获得焦点时确保 terminal textarea 聚焦
  window.addEventListener('focus', () => {
    if (!isProcessing) {
      const ta = terminalRef.value?.querySelector('.xterm-helper-textarea') as HTMLTextAreaElement | null
      ta?.focus()
    }
  })

  // 适配窗口大小
  setTimeout(() => fitAddon?.fit(), 100)
  window.addEventListener('resize', () => fitAddon?.fit())

  // 打印欢迎信息
  printBanner()

  // 连接 WebSocket
  try {
    await wsConnect()
    // 连上后展示最近 5 个任务（点击编号可恢复历史会话并继续）
    await showRecentSessions()
  } catch {
    // 连接失败已在 onerror 中显示
  }

  printPrompt()

  // 键盘输入处理
  term.onData((data) => {
    // 处理中时完全忽略输入（包括流式输出期间）
    if (isProcessing) return

    const code = data.charCodeAt(0)

    // Enter
    if (code === 13) {
      term!.write('\r\n')
      const line = inputBuffer
      inputBuffer = ''
      cursorPos = 0
      processInput(line)
      return
    }

    // Backspace
    if (code === 127 || code === 8) {
      if (cursorPos > 0) {
        inputBuffer = inputBuffer.slice(0, cursorPos - 1) + inputBuffer.slice(cursorPos)
        cursorPos--
        rewriteLine()
      }
      return
    }

    // ESC sequences (arrow keys, etc.)
    if (code === 27 && data.length > 1) {
      const seq = data.substring(2) // skip ESC [
      // Up arrow (A) → 历史上一条
      if (seq === 'A' && history.length > 0) {
        if (historyIndex > 0) historyIndex--
        inputBuffer = history[historyIndex] || ''
        cursorPos = inputBuffer.length
        rewriteLine()
        return
      }
      // Down arrow (B) → 历史下一条
      if (seq === 'B') {
        if (historyIndex < history.length - 1) {
          historyIndex++
          inputBuffer = history[historyIndex] || ''
        } else {
          historyIndex = history.length
          inputBuffer = ''
        }
        cursorPos = inputBuffer.length
        rewriteLine()
        return
      }
      // 左箭头 (D) —— 按格数移动（CJK 占 2 格）
      if (seq === 'D' && cursorPos > 0) {
        cursorPos--
        term!.write('\x1b[' + cellWidth(inputBuffer[cursorPos]) + 'D')
        return
      }
      // 右箭头 (C) —— 按格数移动（CJK 占 2 格）
      if (seq === 'C' && cursorPos < inputBuffer.length) {
        term!.write('\x1b[' + cellWidth(inputBuffer[cursorPos]) + 'C')
        cursorPos++
        return
      }
      // Home (H or 1)
      if (seq === 'H' || seq === '1') {
        cursorPos = 0
        rewriteLine()
        return
      }
      // End (F or 4)
      if (seq === 'F' || seq === '4') {
        cursorPos = inputBuffer.length
        rewriteLine()
        return
      }
      return
    }

    // Ctrl+C → 取消当前输入
    if (code === 3) {
      inputBuffer = ''
      cursorPos = 0
      term!.write('^C\r\n')
      printPrompt()
      return
    }

    // Ctrl+L → 清屏
    if (code === 12) {
      clearTerminal()
      return
    }

    // Ctrl+U → 清除当前行
    if (code === 21) {
      inputBuffer = ''
      cursorPos = 0
      rewriteLine()
      return
    }

    // Ctrl+W → 删除前一个单词
    if (code === 23) {
      if (cursorPos > 0) {
        let i = cursorPos - 1
        while (i >= 0 && inputBuffer[i] === ' ') i--
        while (i >= 0 && inputBuffer[i] !== ' ') i--
        inputBuffer = inputBuffer.slice(0, i + 1) + inputBuffer.slice(cursorPos)
        cursorPos = i + 1
        rewriteLine()
      }
      return
    }

    // Tab → 命令补全
    if (code === 9) {
      if (inputBuffer.startsWith('/') && cursorPos === inputBuffer.length) {
        const partial = inputBuffer.substring(1).toLowerCase()
        const matches = Object.keys(SLASH_COMMANDS).filter(k => k.startsWith(partial))
        if (matches.length === 1) {
          inputBuffer = '/' + matches[0] + ' '
          cursorPos = inputBuffer.length
          rewriteLine()
        } else if (matches.length > 1) {
          term!.write('\r\n')
          for (const m of matches) {
            term!.write(`  \x1b[93m/${m}\x1b[0m`)
          }
          term!.write('\r\n')
          rewriteLine()
        }
      }
      return
    }

    // 可打印字符（支持中文等多字节字符）
    if (code >= 32) {
      insertText(data)
    }
  })

  // 监听窗口关闭
  window.addEventListener('beforeunload', () => {
    ws?.close()
  })
})

// 字符串的终端格宽（CJK/emoji/全角占 2 格）—— 光标移动与折行行号计算必须按格数而非字符数
function cellWidth(s: string): number {
  let w = 0
  for (const ch of s) {
    const c = ch.codePointAt(0)!
    w += (c >= 0x1100 && (c <= 0x115f || c === 0x2329 || c === 0x232a ||
      (c >= 0x2e80 && c <= 0xa4cf && c !== 0x303f) ||
      (c >= 0xac00 && c <= 0xd7a3) ||
      (c >= 0xf900 && c <= 0xfaff) ||
      (c >= 0xfe30 && c <= 0xfe6f) ||
      (c >= 0xff00 && c <= 0xff60) ||
      (c >= 0xffe0 && c <= 0xffe6) ||
      (c >= 0x1f300 && c <= 0x1f64f) ||
      (c >= 0x1f900 && c <= 0x1f9ff) ||
      (c >= 0x20000 && c <= 0x3fffd))) ? 2 : 1
  }
  return w
}

function rewriteLine() {
  // 清除整条逻辑行（含折行续行）：先把光标移回逻辑行起始行，
  // 再用 \x1b[J（清到屏末）一次性抹掉当前行及下方所有折行。
  // 旧写法 \r\x1b[K 只清当前物理行——折行续行上的旧文本残留，
  // 重绘后看起来像"多复制出一行"（Backspace/方向键历史最明显）。
  const cols = term!.cols || 80
  const beforeCells = PROMPT_CELLS + cellWidth(inputBuffer.slice(0, cursorPos))
  const rowsUp = Math.floor(beforeCells / cols)
  term!.write('\r')
  if (rowsUp > 0) term!.write(`\x1b[${rowsUp}A`)
  term!.write('\x1b[J')
  printPrompt()
  term!.write(inputBuffer)
  // 回退光标到正确位置（按格数，CJK 占 2 格）
  const back = cellWidth(inputBuffer.slice(cursorPos))
  if (back > 0) {
    term!.write('\x1b[' + back + 'D')
  }
}

function insertText(data: string) {
  // 在光标处插入文本（普通键入与剪贴板粘贴共用此逻辑）
  if (!data || !term) return
  inputBuffer = inputBuffer.slice(0, cursorPos) + data + inputBuffer.slice(cursorPos)
  cursorPos += data.length
  // 光标在末尾：直接写入避免闪烁；否则整行重绘以修正光标位置
  if (cursorPos === inputBuffer.length) {
    term.write(data)
  } else {
    rewriteLine()
  }
}

onUnmounted(() => {
  ws?.close()
  term?.dispose()
  stopSpinner()
  setIdle()
  if (durationTimer) clearInterval(durationTimer)
  window.removeEventListener('resize', () => fitAddon?.fit())
})
</script>

<style>
html, body {
  margin: 0;
  padding: 0;
  background: #0a0a0f;
  overflow: hidden;
  height: 100vh;
}
</style>

<style scoped>
.cli-terminal-page {
  width: 100vw;
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: #0a0a0f;
}

.terminal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 6px 12px;
  background: rgba(251, 146, 60, 0.15);
  border-bottom: 1px solid rgba(251, 146, 60, 0.3);
  -webkit-app-region: drag;
  cursor: grab;
  flex-shrink: 0;
}

.terminal-title {
  font-size: 12px;
  font-weight: 600;
  color: #fb923c;
  font-family: 'Cascadia Code', 'Fira Code', monospace;
}

.terminal-hint {
  font-size: 10px;
  color: #888;
  font-family: 'Cascadia Code', 'Fira Code', monospace;
  opacity: 0.7;
}

.terminal-controls {
  display: flex;
  gap: 4px;
  -webkit-app-region: no-drag;
}

.ctrl-btn {
  background: none;
  border: none;
  color: #888;
  cursor: pointer;
  font-size: 12px;
  padding: 2px 6px;
  border-radius: 4px;
  line-height: 1;
}
.ctrl-btn:hover {
  color: #fff;
  background: rgba(255, 255, 255, 0.1);
}

.terminal-container {
  flex: 1;
  overflow: hidden;
  padding: 4px;
}

.terminal-container :deep(.xterm) {
  height: 100%;
}

/* ── 状态栏（参考 Hermes status bar）────────────────── */

.status-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 12px;
  background: rgba(251, 146, 60, 0.08);
  border-top: 1px solid rgba(251, 146, 60, 0.2);
  font-size: 11px;
  font-family: 'Cascadia Code', 'Fira Code', monospace;
  color: #888;
  flex-shrink: 0;
  user-select: none;
}

.sb-sep {
  color: rgba(251, 146, 60, 0.3);
}

.sb-run {
  color: #facc15;
  font-weight: 600;
}

.sb-idle {
  color: #666;
}

.sb-model {
  color: #fb923c;
}

.sb-session {
  color: #888;
}

.sb-msgs {
  color: #60a5fa;
}

.sb-tools {
  color: #4ade80;
}

.sb-status {
  margin-left: auto;
  font-size: 10px;
}

.sb-status.connected {
  color: #4ade80;
}

.sb-status.disconnected {
  color: #f87171;
}
</style>
