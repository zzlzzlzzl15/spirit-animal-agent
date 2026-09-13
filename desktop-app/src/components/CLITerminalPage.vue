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
      if (term && isStreaming) {
        flushMarkdown()  // 先flush未完成行，避免标注插进半行中间
        term.write('\r\x1b[K')  // 清掉当前 spinner 行，避免标注接在 spinner 后面
        term.writeln(`\x1b[93m  🔧 调用工具: ${data.tool}\x1b[0m`)
        if (data.args_preview) {
          const rawPreview = String(data.args_preview).replace(/[\r\n]+/g, ' ')
          const preview = rawPreview.substring(0, 80)
          term.writeln(`\x1b[90m     参数: ${preview}${rawPreview.length > 80 ? '...' : ''}\x1b[0m`)
        }
      }
      break

    case 'tool_complete':
      if (term && isStreaming) {
        flushMarkdown()
        term.write('\r\x1b[K')
        term.writeln(`\x1b[92m  ✓ ${data.tool} 完成\x1b[0m`)
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
        }
        feedMarkdown(data.text)
      }
      break

    case 'chat_complete':
      // 对话完成：停 spinner + 工具汇总兜底 + 最终文本兜底渲染。
      // 最终文本优先由 stream_delta 实时渲染；若增量未送达（乱序/丢失），
      // 在这里用 chat_complete 携带的 response 兜底，保证结果绝不丢失。
      stopSpinner()
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

async function sendChatMessage(message: string) {
  term!.writeln('')
  isStreaming = true
  streamBuffer = ''
  toolEventCount = 0
  finalRendered = false
  mdBuffer = ''
  startSpinner('思考中')
  try {
    const data = await wsSend('chat', { message }, 300000)  // 5 分钟超时
    // 清除 spinner
    stopSpinner()
    flushMarkdown()
    if (streamBuffer) {
      // 如果有流式增量，已经显示过了，只需换行
      term!.writeln('')
    } else if (data.error) {
      term!.writeln(`\x1b[91m  ✗ ${data.error}\x1b[0m`)
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
      // Left arrow (D)
      if (seq === 'D' && cursorPos > 0) {
        cursorPos--
        term!.write('\x1b[D')
        return
      }
      // Right arrow (C)
      if (seq === 'C' && cursorPos < inputBuffer.length) {
        cursorPos++
        term!.write('\x1b[C')
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
      // 插入到光标位置，cursorPos 按实际字符数递增
      inputBuffer = inputBuffer.slice(0, cursorPos) + data + inputBuffer.slice(cursorPos)
      cursorPos += data.length
      // 简单情况（光标在末尾）：直接写入字符，避免 rewriteLine 闪烁
      if (cursorPos === inputBuffer.length) {
        term!.write(data)
      } else {
        rewriteLine()
      }
    }
  })

  // 监听窗口关闭
  window.addEventListener('beforeunload', () => {
    ws?.close()
  })
})

function rewriteLine() {
  // 清除当前行并重新写入
  term!.write('\r\x1b[K')
  printPrompt()
  term!.write(inputBuffer)
  // 回退光标到正确位置
  const back = inputBuffer.length - cursorPos
  if (back > 0) {
    term!.write('\x1b[' + back + 'D')
  }
}

onUnmounted(() => {
  ws?.close()
  term?.dispose()
  stopSpinner()
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
