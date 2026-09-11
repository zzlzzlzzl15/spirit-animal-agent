/**
 * 透明窗口管理 — 创建/配置桌宠窗口。
 *
 * 关键配置：
 * - transparent: true    → 窗口背景透明
 * - frame: false         → 无边框
 * - alwaysOnTop: true    → 始终置顶
 * - click-through        → 透明区域点击穿透
 * - resizable: false     → 禁止用户拖拽调整大小（由代码控制缩放）
 */

import { BrowserWindow, screen } from 'electron'
import path from 'path'

// ---------------------------------------------------------------------------
// 窗口常量
// ---------------------------------------------------------------------------

/** 精灵帧原始尺寸 */
const FRAME_W = 192
const FRAME_H = 208

/** 默认缩放 */
const DEFAULT_SCALE = 0.75

/** 窗口额外边距（防止精灵被裁剪） */
const PADDING = 4

// ---------------------------------------------------------------------------
// 窗口管理
// ---------------------------------------------------------------------------

let petWindow: BrowserWindow | null = null

interface PetWindowOptions {
  x?: number
  y?: number
  scale?: number
}

/**
 * 创建桌宠窗口。
 */
export function createPetWindow(options: PetWindowOptions = {}): BrowserWindow {
  const scale = options.scale || DEFAULT_SCALE
  const contentW = Math.round(FRAME_W * scale) + PADDING
  const contentH = Math.round(FRAME_H * scale) + PADDING

  // 默认位置：屏幕底部居中
  const display = screen.getPrimaryDisplay()
  const x = options.x ?? Math.floor(display.workAreaSize.width / 2 - contentW / 2)
  const y = options.y ?? Math.floor(display.workAreaSize.height - contentH - 50)

  petWindow = new BrowserWindow({
    x,
    y,
    width: contentW,
    height: contentH,
    transparent: true,
    frame: false,
    alwaysOnTop: true,
    resizable: false,
    skipTaskbar: true,
    hasShadow: false,
    focusable: true,
    // Windows 下减少透明窗口闪烁
    backgroundColor: '#00000000',

    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      // 防止后台节流动画
      backgroundThrottling: false,
    },
  })

  // 设置窗口名称
  petWindow.setTitle('Spirit Pet')

  // 禁用默认右键菜单，让 Vue 的 contextmenu 事件正常处理
  petWindow.webContents.on('context-menu', (e) => {
    e.preventDefault()
  })

  // 注意：不使用 setIgnoreMouseEvents，因为在 Windows 透明窗口上会导致拖拽失效
  // 透明区域的点击穿透通过 CSS pointer-events 控制

  // 开发模式加载 Vite dev server，生产模式加载构建产物
  if (process.env.VITE_DEV_SERVER_URL) {
    petWindow.loadURL(process.env.VITE_DEV_SERVER_URL)
  } else {
    petWindow.loadFile(path.join(__dirname, '..', 'dist', 'index.html'))
  }

  // 窗口关闭事件
  petWindow.on('closed', () => {
    petWindow = null
  })

  // 失去焦点时不降低优先级
  petWindow.on('blur', () => {
    // 保持置顶
  })

  return petWindow
}

/**
 * 获取当前桌宠窗口。
 */
export function getPetWindow(): BrowserWindow | null {
  return petWindow
}

/**
 * 更新窗口大小（缩放变化时调用）。
 */
export function resizePetWindow(scale: number): void {
  if (!petWindow) return

  const contentW = Math.round(FRAME_W * scale) + PADDING
  const contentH = Math.round(FRAME_H * scale) + PADDING

  petWindow.setSize(contentW, contentH)
}

/**
 * 将窗口移到指定屏幕位置。
 */
export function movePetWindow(x: number, y: number): void {
  if (!petWindow) return
  petWindow.setPosition(Math.round(x), Math.round(y))
}

/**
 * 确保窗口可见并获得焦点。
 */
export function showPetWindow(): void {
  if (!petWindow) return
  petWindow.show()
  petWindow.moveTop()
}

/**
 * 隐藏窗口。
 */
export function hidePetWindow(): void {
  if (!petWindow) return
  petWindow.hide()
}

// ---------------------------------------------------------------------------
// 状态弹窗窗口
// ---------------------------------------------------------------------------

let statusWindow: BrowserWindow | null = null

/**
 * 创建状态弹窗窗口（显示在小狐狸旁边）
 */
export function createStatusWindow(petBounds: { x: number; y: number; w: number; h: number }): BrowserWindow {
  // 如果已存在，先关闭
  if (statusWindow) {
    statusWindow.close()
  }

  const popupW = 220
  const popupH = 230
  const gap = 8
  
  // 智能定位：宠物在屏幕右半 → 弹窗在左边；否则在右边
  const display = screen.getPrimaryDisplay()
  const screenW = display.workAreaSize.width
  const petCenterX = petBounds.x + petBounds.w / 2
  const showOnLeft = petCenterX > screenW / 2
  
  const x = showOnLeft
    ? petBounds.x - popupW - gap
    : petBounds.x + petBounds.w + gap
  const y = petBounds.y

  // 确保不超出屏幕左边界
  const finalX = Math.max(0, x)

  statusWindow = new BrowserWindow({
    x: finalX,
    y,
    width: popupW,
    height: popupH,
    transparent: true,
    frame: false,
    alwaysOnTop: true,
    resizable: false,
    skipTaskbar: true,
    hasShadow: true,
    movable: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  })

  // 加载状态弹窗页面
  if (process.env.VITE_DEV_SERVER_URL) {
    statusWindow.loadURL(`${process.env.VITE_DEV_SERVER_URL}#status-popup`)
  } else {
    statusWindow.loadFile(path.join(__dirname, '../dist/index.html'), {
      hash: 'status-popup',
    })
  }

  // 点击外部关闭
  statusWindow.on('blur', () => {
    if (statusWindow) {
      statusWindow.close()
      statusWindow = null
    }
  })

  return statusWindow
}

/**
 * 关闭状态弹窗
 */
export function closeStatusWindow(): void {
  if (statusWindow) {
    statusWindow.close()
    statusWindow = null
  }
}

/**
 * 获取状态弹窗
 */
export function getStatusWindow(): BrowserWindow | null {
  return statusWindow
}

// ---------------------------------------------------------------------------
// 气泡对话窗口
// ---------------------------------------------------------------------------

let bubbleWindow: BrowserWindow | null = null
let bubbleAutoTimer: ReturnType<typeof setTimeout> | null = null

/**
 * 创建/显示气泡对话窗口（显示在小狐狸上方）
 */
export function createBubbleWindow(
  petBounds: { x: number; y: number; w: number; h: number },
  text: string
): BrowserWindow {
  // 如果已存在，先关闭
  if (bubbleWindow) {
    bubbleWindow.close()
    bubbleWindow = null
  }
  if (bubbleAutoTimer) {
    clearTimeout(bubbleAutoTimer)
    bubbleAutoTimer = null
  }

  const bubbleW = 220
  const bubbleH = 60
  const gap = 2

  // 气泡显示在宠物窗口正上方，水平居中
  const x = petBounds.x + Math.round(petBounds.w / 2) - Math.round(bubbleW / 2)
  const y = petBounds.y - bubbleH - gap

  // 确保不超出屏幕左边界
  const finalX = Math.max(0, x)
  // 确保不超出屏幕上边界
  const finalY = Math.max(0, y)

  bubbleWindow = new BrowserWindow({
    x: finalX,
    y: finalY,
    width: bubbleW,
    height: bubbleH,
    transparent: true,
    frame: false,
    alwaysOnTop: true,
    resizable: false,
    skipTaskbar: true,
    hasShadow: false,
    movable: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  })

  // 将文本通过 URL query 传递（简单方案）
  const encodedText = encodeURIComponent(text)
  if (process.env.VITE_DEV_SERVER_URL) {
    bubbleWindow.loadURL(`${process.env.VITE_DEV_SERVER_URL}#speech-bubble?text=${encodedText}`)
  } else {
    bubbleWindow.loadFile(path.join(__dirname, '../dist/index.html'), {
      hash: `speech-bubble?text=${encodedText}`,
    })
  }

  // 4 秒后自动关闭
  bubbleAutoTimer = setTimeout(() => {
    closeBubbleWindow()
  }, 4000)

  return bubbleWindow
}

/**
 * 关闭气泡窗口
 */
export function closeBubbleWindow(): void {
  if (bubbleAutoTimer) {
    clearTimeout(bubbleAutoTimer)
    bubbleAutoTimer = null
  }
  if (bubbleWindow) {
    bubbleWindow.close()
    bubbleWindow = null
  }
}

// ---------------------------------------------------------------------------
// CLI 终端窗口
// ---------------------------------------------------------------------------

let cliTerminalWindow: BrowserWindow | null = null

/**
 * 创建 CLI 终端窗口
 */
export function createCLITerminalWindow(): BrowserWindow {
  if (cliTerminalWindow) {
    cliTerminalWindow.focus()
    return cliTerminalWindow
  }

  const termW = 700
  const termH = 500

  // 居中显示
  const display = screen.getPrimaryDisplay()
  const x = Math.floor(display.workAreaSize.width / 2 - termW / 2)
  const y = Math.floor(display.workAreaSize.height / 2 - termH / 2)

  cliTerminalWindow = new BrowserWindow({
    x,
    y,
    width: termW,
    height: termH,
    minWidth: 500,
    minHeight: 350,
    transparent: false,
    frame: false,
    alwaysOnTop: false,
    resizable: true,
    skipTaskbar: false,
    hasShadow: true,
    movable: true,
    backgroundColor: '#0a0a0f',
    title: 'Spirit Agent CLI',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  })

  if (process.env.VITE_DEV_SERVER_URL) {
    cliTerminalWindow.loadURL(`${process.env.VITE_DEV_SERVER_URL}#cli-terminal`)
  } else {
    cliTerminalWindow.loadFile(path.join(__dirname, '../dist/index.html'), {
      hash: 'cli-terminal',
    })
  }

  cliTerminalWindow.on('closed', () => {
    cliTerminalWindow = null
  })

  return cliTerminalWindow
}

/**
 * 关闭 CLI 终端窗口
 */
export function closeCLITerminalWindow(): void {
  if (cliTerminalWindow) {
    cliTerminalWindow.close()
    cliTerminalWindow = null
  }
}
