/**
 * useWebSocket — WebSocket 连接管理。
 *
 * 负责：
 * - 连接 Python 后端 WebSocket
 * - 发送命令 / 接收事件
 * - 自动重连
 */

import { ref, onUnmounted } from 'vue'

export interface WSMessage {
  type: 'event' | 'response'
  event?: string
  action?: string
  id?: string
  data: any
  timestamp?: number
}

export interface WSOptions {
  onEvent?: (event: string, data: any) => void
  onResponse?: (action: string, data: any, id: string) => void
  onConnect?: () => void
  onDisconnect?: () => void
}

export function useWebSocket() {
  const connected = ref(false)
  const ws = ref<WebSocket | null>(null)
  let options: WSOptions = {}
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let requestCounter = 0
  const pendingRequests = new Map<string, {
    resolve: (data: any) => void
    reject: (err: Error) => void
  }>()

  async function connect(url: string, opts: WSOptions = {}): Promise<void> {
    options = opts

    return new Promise((resolve, reject) => {
      try {
        const socket = new WebSocket(url)
        ws.value = socket

        socket.onopen = () => {
          connected.value = true
          options.onConnect?.()
          resolve()
        }

        socket.onmessage = (event) => {
          try {
            const msg: WSMessage = JSON.parse(event.data)
            handleMessage(msg)
          } catch (e) {
            console.warn('WebSocket 消息解析失败:', e)
          }
        }

        socket.onclose = () => {
          connected.value = false
          ws.value = null
          options.onDisconnect?.()
          scheduleReconnect(url)
        }

        socket.onerror = (err) => {
          console.warn('WebSocket 错误:', err)
          reject(new Error('WebSocket 连接失败'))
        }
      } catch (err) {
        reject(err)
      }
    })
  }

  function disconnect(): void {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (ws.value) {
      ws.value.close()
      ws.value = null
    }
    connected.value = false
  }

  function send(action: string, data: any = {}): Promise<any> {
    return new Promise((resolve, reject) => {
      if (!ws.value || ws.value.readyState !== WebSocket.OPEN) {
        reject(new Error('WebSocket 未连接'))
        return
      }

      const id = `req-${++requestCounter}`
      const msg = JSON.stringify({
        type: 'command',
        action,
        id,
        data,
      })

      // 注册待响应请求
      pendingRequests.set(id, { resolve, reject })

      // 超时处理
      setTimeout(() => {
        if (pendingRequests.has(id)) {
          pendingRequests.delete(id)
          reject(new Error(`请求超时: ${action}`))
        }
      }, 10000)

      ws.value.send(msg)
    })
  }

  function handleMessage(msg: WSMessage): void {
    if (msg.type === 'event') {
      options.onEvent?.(msg.event || '', msg.data)
    } else if (msg.type === 'response') {
      const id = msg.id || ''
      const pending = pendingRequests.get(id)
      if (pending) {
        pendingRequests.delete(id)
        pending.resolve(msg.data)
      }
      options.onResponse?.(msg.action || '', msg.data, id)
    }
  }

  function scheduleReconnect(url: string): void {
    if (reconnectTimer) return
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      console.log('WebSocket 尝试重连...')
      connect(url, options).catch(() => {})
    }, 3000)
  }

  return {
    connected,
    connect,
    disconnect,
    send,
  }
}
