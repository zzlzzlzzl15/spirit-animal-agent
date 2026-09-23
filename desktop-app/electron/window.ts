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

import { BrowserWindow, screen, ipcMain } from 'electron'
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

/** 用户是否主动隐藏了宠物（托盘切换）；可见性看门狗不得覆盖用户意图自动显示 */
let petUserHidden = false

export function setPetUserHidden(v: boolean): void {
  petUserHidden = v
}

export function isPetUserHidden(): boolean {
  return petUserHidden
}

interface PetWindowOptions {
  x?: number
  y?: number
  scale?: number
}

/**
 * 创建桌宠窗口。
 */
export function createPetWindow(options: PetWindowOptions = {}): BrowserWindow {
  petUserHidden = false  // 新窗口处于可见状态
  const scale = options.scale || DEFAULT_SCALE
  const contentW = Math.round(FRAME_W * scale) + PADDING
  const contentH = Math.round(FRAME_H * scale) + PADDING

  // 默认位置：屏幕底部居中
  const display = screen.getPrimaryDisplay()
  let x = options.x ?? Math.floor(display.workAreaSize.width / 2 - contentW / 2)
  let y = options.y ?? Math.floor(display.workAreaSize.height - contentH - 50)

  // 恢复位置校验：保存的坐标若已完全落在屏幕外（拔显示器/分辨率变化），
  // 回退到主屏底部居中，避免桌宠“消失”
  const onScreen = screen.getAllDisplays().some((d) => {
    const wa = d.workArea
    return (
      x + contentW > wa.x && x < wa.x + wa.width &&
      y + contentH > wa.y && y < wa.y + wa.height
    )
  })
  if (!onScreen) {
    x = Math.floor(display.workAreaSize.width / 2 - contentW / 2)
    y = Math.floor(display.workAreaSize.height - contentH - 50)
  }

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
  // 注意：这不会影响键盘快捷键（Ctrl+C/Ctrl+V）的复制粘贴功能
  petWindow.webContents.on('context-menu', (e) => {
    e.preventDefault()
  })

  // 渲染进程崩溃自愈：渲染进程崩溃时透明窗口无内容，表现为宠物“消失”
  petWindow.webContents.on('render-process-gone', (_event, details) => {
    console.warn('[pet] renderer gone:', details.reason, '— reloading pet window')
    if (petWindow && !petWindow.isDestroyed()) {
      petWindow.webContents.reload()
    }
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
 * 将窗口移到指定屏幕位置（拖拽增量移动）。
 * 坐标经过屏幕边界钳制：防止拖拽/分辨率变化把宠物完全拖出屏幕
 * （窗口仍存在且 isVisible，但用户看不到 — 表现为“宠物消失但气泡还在”）。
 */
export function movePetWindow(x: number, y: number): void {
  if (!petWindow) return
  const [cx, cy] = clampToScreens(Math.round(x), Math.round(y), petWindow.getBounds())
  petWindow.setPosition(cx, cy)
}

/**
 * 坐标钳制：保证窗口至少保留 40px 在某个显示器的工作区内。
 * 完全离屏时钳回最近屏幕边缘；正常坐标原样返回。
 */
function clampToScreens(x: number, y: number, bounds: { width: number; height: number }): [number, number] {
  const KEEP = 40  // 至少保留在屏内的像素
  const displays = screen.getAllDisplays()
  // 已经与某个显示器工作区有有效交集 → 不动
  const onScreen = displays.some((d) => {
    const wa = d.workArea
    return (
      x + bounds.width > wa.x + KEEP && x < wa.x + wa.width - KEEP &&
      y + bounds.height > wa.y + KEEP && y < wa.y + wa.height - KEEP
    )
  })
  if (onScreen) return [x, y]
  // 离屏 → 钳到最近显示器的工作区边缘
  let best: { x: number; y: number } | null = null
  let bestDist = Infinity
  for (const d of displays) {
    const wa = d.workArea
    const cx = Math.min(Math.max(x, wa.x - bounds.width + KEEP), wa.x + wa.width - KEEP)
    const cy = Math.min(Math.max(y, wa.y - bounds.height + KEEP), wa.y + wa.height - KEEP)
    const dist = (cx - x) ** 2 + (cy - y) ** 2
    if (dist < bestDist) {
      bestDist = dist
      best = { x: cx, y: cy }
    }
  }
  return best ? [best.x, best.y] : [x, y]
}

/**
 * 诊断：窗口当前是否与任何显示器工作区有效相交。
 */
function petWindowOnScreen(): boolean {
  if (!petWindow || petWindow.isDestroyed()) return false
  const b = petWindow.getBounds()
  return screen.getAllDisplays().some((d) => {
    const wa = d.workArea
    return (
      b.x + b.width > wa.x + KEEP_ONSCREEN && b.x < wa.x + wa.width - KEEP_ONSCREEN &&
      b.y + b.height > wa.y + KEEP_ONSCREEN && b.y < wa.y + wa.height - KEEP_ONSCREEN
    )
  })
}

const KEEP_ONSCREEN = 40

/**
 * 确保窗口可见并获得焦点。
 */
export function showPetWindow(): void {
  if (!petWindow) return
  petUserHidden = false
  petWindow.show()
  petWindow.moveTop()
}

/**
 * 隐藏窗口。
 */
export function hidePetWindow(): void {
  if (!petWindow) return
  petUserHidden = true
  petWindow.hide()
}

/**
 * 强制重建合成层：hide+showInactive。
 * Windows 透明窗口在 GPU 重置/休眠恢复等场景下合成器可能停止绘制——
 * 渲染进程活着（气泡定时器还在跑）但狐狸不显示；showInactive 不抢焦点。
 */
export function forceRedrawPetWindow(): void {
  if (!petWindow || petWindow.isDestroyed()) return
  petWindow.hide()
  petWindow.showInactive()
  petWindow.moveTop()
}

// ---------------------------------------------------------------------------
// 宠物窗口看门狗
// ---------------------------------------------------------------------------

let watchdogTimer: ReturnType<typeof setInterval> | null = null
let lastHeartbeatFrames = -1
let stalledCycles = 0
let watchdogRegistered = false
let watchdogCycles = 0

/**
 * 启动宠物窗口看门狗（15 秒巡检一次），覆盖所有
 * 「渲染进程活着但宠物不可见」的失效类别：
 * - 窗口被意外销毁      → 按保存位置重建
 * - 被 hide 且非用户意图 → showInactive + moveTop
 * - z-order 掉层        → moveTop 夺回置顶
 * - 合成层停绘（rAF 停摆，连续 2 个心跳周期帧数不涨）→ 强制重建合成层
 */
export function startPetWatchdog(getSavedPosition: () => { x: number; y: number; scale: number }): void {
  // 渲染进程心跳：rAF 帧数（仅桌宠主视图窗口上报）
  if (!watchdogRegistered) {
    watchdogRegistered = true
    ipcMain.on('pet-heartbeat', (_event, { frames }: { frames: number }) => {
      if (frames === lastHeartbeatFrames) {
        stalledCycles++
      } else {
        stalledCycles = 0
      }
      lastHeartbeatFrames = frames
    })
  }

  if (watchdogTimer) clearInterval(watchdogTimer)
  watchdogTimer = setInterval(() => {
    watchdogCycles++
    // 1. 窗口已销毁（被意外关闭）→ 重建
    if (!petWindow || petWindow.isDestroyed()) {
      if (!petUserHidden) {
        console.warn('[pet watchdog] window missing — recreating')
        const { x, y, scale } = getSavedPosition()
        createPetWindow({ x, y, scale })
      }
      lastHeartbeatFrames = -1
      stalledCycles = 0
      return
    }

    // 用户主动隐藏 → 尊重用户意图，不做任何自动显示
    if (petUserHidden) return

    // 2. 窗口存在但不可见（hide 后 showInactive 静默失败等）→ 恢复显示
    if (!petWindow.isVisible()) {
      console.warn('[pet watchdog] window hidden unexpectedly — restoring')
      petWindow.showInactive()
      petWindow.moveTop()
      return
    }

    // 3. 窗口完全离屏（拖拽越界/分辨率变化）→ 钳回屏幕内
    // isVisible() 对离屏窗口仍为 true，心跳也正常，之前看门狗对此盲视
    if (!petWindowOnScreen()) {
      const b = petWindow.getBounds()
      console.warn(`[pet watchdog] window off-screen at (${b.x},${b.y}) — clamping back`)
      const [cx, cy] = clampToScreens(b.x, b.y, b)
      petWindow.setPosition(cx, cy)
      return
    }

    // 4. 合成层停绘：连续 2 个心跳周期（20s）rAF 帧数不涨 → 强制重绘
    if (stalledCycles >= 2) {
      console.warn('[pet watchdog] compositor stalled — force redraw')
      forceRedrawPetWindow()
      stalledCycles = 0
      return
    }

    // 5. z-order 掉层 → 周期性夺回置顶（moveTop 对已置顶窗口无副作用）
    petWindow.moveTop()

    // 诊断快照：每 4 个周期（1 分钟）记录一次窗口状态，
    // 下次“消失”时日志可直接定位失效维度
    if (watchdogCycles % 4 === 0) {
      const b = petWindow.getBounds()
      console.log(`[pet watchdog] pos=(${b.x},${b.y}) visible=${petWindow.isVisible()} frames=${lastHeartbeatFrames} stalled=${stalledCycles}`)
    }
  }, 15000)
}

/**
 * 停止看门狗（退出前调用，防止 before-quit 后又被重建）。
 */
export function stopPetWatchdog(): void {
  if (watchdogTimer) {
    clearInterval(watchdogTimer)
    watchdogTimer = null
  }
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
    show: false,       // 创建时不立即显示，改用 showInactive 惰性显示
    focusable: false,  // 永不接受焦点：避免周期气泡抢夺 CLI 聊天框输入焦点
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

  // 以“非激活”方式显示：不夺取系统焦点，不打断用户在任何窗口的输入
  bubbleWindow.showInactive()

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
      // 启用剪贴板访问
      enableWebSQL: false,
    },
  })

  // 渲染层 console 管道：前端渲染异常时可在主进程日志直接看到
  cliTerminalWindow.webContents.on('console-message', (_event, _level, message) => {
    console.log(`[CLI renderer] ${message}`)
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
