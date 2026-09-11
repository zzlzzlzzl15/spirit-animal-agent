/**
 * Electron preload — 安全桥接主进程与渲染进程。
 *
 * 通过 contextBridge 暴露有限的 API 给 Vue 前端，
 * 不直接暴露 Node.js / Electron 完整能力。
 */

import { contextBridge, ipcRenderer } from 'electron'

// ---------------------------------------------------------------------------
// 暴露给渲染进程的 API
// ---------------------------------------------------------------------------

contextBridge.exposeInMainWorld('electronAPI', {
  // ── 配置 ──────────────────────────────────────────────────

  /** 获取持久化配置（窗口位置、缩放、当前宠物） */
  getSavedConfig: (): Promise<{
    windowX: number
    windowY: number
    petScale: number
    activePetSlug: string
    wsPort: number
  }> => ipcRenderer.invoke('get-saved-config'),

  /** 获取屏幕尺寸 */
  getScreenSize: (): Promise<{ width: number; height: number }> =>
    ipcRenderer.invoke('get-screen-size'),

  /** 获取窗口在屏幕上的位置和尺寸 */
  getWindowPosition: (): Promise<{ x: number; y: number; w: number; h: number }> =>
    ipcRenderer.invoke('get-window-position'),

  /** 打开状态弹窗窗口 */
  openStatusWindow: (): Promise<boolean> =>
    ipcRenderer.invoke('open-status-window'),

  /** 关闭状态弹窗窗口 */
  closeStatusWindow: (): Promise<boolean> =>
    ipcRenderer.invoke('close-status-window'),

  /** 打开 CLI 终端窗口 */
  openCLITerminal: (): Promise<boolean> =>
    ipcRenderer.invoke('open-cli-terminal'),

  /** 关闭 CLI 终端窗口 */
  closeCLITerminal: (): Promise<boolean> =>
    ipcRenderer.invoke('close-cli-terminal'),

  /** 显示气泡对话（独立窗口） */
  showBubble: (text: string): Promise<boolean> =>
    ipcRenderer.invoke('show-bubble', { text }),

  /** 隐藏气泡对话 */
  hideBubble: (): Promise<boolean> =>
    ipcRenderer.invoke('hide-bubble'),

  /** 用浏览器打开 Memora 知识库 */
  openMemora: (): Promise<boolean> =>
    ipcRenderer.invoke('open-memora'),

  // ── 窗口控制 ──────────────────────────────────────────────

  /** 保存窗口位置 */
  savePosition: (x: number, y: number): void => {
    ipcRenderer.send('save-position', { x, y })
  },

  /** 保存缩放 */
  saveScale: (scale: number): void => {
    ipcRenderer.send('save-scale', { scale })
  },

  /** 移动窗口（相对偏移） */
  moveWindow: (deltaX: number, deltaY: number): void => {
    ipcRenderer.send('move-window', { deltaX, deltaY })
  },

  /** 调整窗口大小 */
  resizeWindow: (width: number, height: number): void => {
    ipcRenderer.send('resize-window', { width, height })
  },

  // ── 事件监听 ──────────────────────────────────────────────

  /** 监听主进程发来的切换宠物事件 */
  onSwitchPet: (callback: (data: { slug: string }) => void): void => {
    ipcRenderer.on('switch-pet', (_event, data) => callback(data))
  },

  /** 移除所有监听器 */
  removeAllListeners: (channel: string): void => {
    ipcRenderer.removeAllListeners(channel)
  },
})

// ---------------------------------------------------------------------------
// 类型声明（供 Vue 前端使用）
// ---------------------------------------------------------------------------

declare global {
  interface Window {
    electronAPI: {
      getSavedConfig: () => Promise<{
        windowX: number
        windowY: number
        petScale: number
        activePetSlug: string
        wsPort: number
      }>
      getScreenSize: () => Promise<{ width: number; height: number }>
      getWindowPosition: () => Promise<{ x: number; y: number; w: number; h: number }>
      openStatusWindow: () => Promise<boolean>
      closeStatusWindow: () => Promise<boolean>
      openCLITerminal: () => Promise<boolean>
      closeCLITerminal: () => Promise<boolean>
      showBubble: (text: string) => Promise<boolean>
      hideBubble: () => Promise<boolean>
      openMemora: () => Promise<boolean>
      savePosition: (x: number, y: number) => void
      saveScale: (scale: number) => void
      moveWindow: (deltaX: number, deltaY: number) => void
      resizeWindow: (width: number, height: number) => void
      onSwitchPet: (callback: (data: { slug: string }) => void) => void
      removeAllListeners: (channel: string) => void
    }
  }
}
