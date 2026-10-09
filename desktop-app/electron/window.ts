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

/**
 * 径向菜单外扩留白（每侧）：4 个按钮在半径 55 + 按钮半宽 22 之外还要 3px 余量。
 * 此前 PADDING=4 时窗口 148×160，左右按钮中心 ±77 已超过窗口半宽 74，被裁 3px。
 * 加大到 80 后窗口 308×320，按钮完全落在窗口内。
 */
const MENU_PADDING = 80

/**
 * 狐狸可见包围盒（帧内坐标，按 spritesheet alpha 通道实测 (28,6)-(178,202)）。
 * 包围盒之外全是帧的透明边距——这块隐形矩形正是“看不见却挡住点击”的区域。
 */
const FOX_BBOX = { x: 28, y: 6, w: 151, h: 197 }

/** 光标命中轮询间隔（ms）：光标移到狐狸上后收回鼠标捕获的最大延迟 */
const HIT_POLL_MS = 50

/** forceCapture 防御性超时（ms）：超过此时长强制释放，防止 IPC/拖拽异常导致整窗捕获锁死 */
const FORCE_CAPTURE_GUARD_MS = 5000

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
  // 窗口 = 精灵 + 精灵防裁剪留白 + 径向菜单外扩留白（每侧 MENU_PADDING）
  // 精灵在窗口内居中，四周各留 (PADDING/2 + MENU_PADDING) 的透明边距
  const contentW = Math.round(FRAME_W * scale) + PADDING + 2 * MENU_PADDING
  const contentH = Math.round(FRAME_H * scale) + PADDING + 2 * MENU_PADDING

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

  // 点击穿透：初始整窗在 OS 层放开鼠标捕获，之后由命中轮询按狐狸可见
  // 区域动态收回（详见下方“光标命中轮询”一节）。
  forceCapture = false
  ignoring = false  // 窗口可能是重建的：清掉缓存，确保下一次真正下发
  hitBoxes = []
  if (forceGuardTimer) {
    clearTimeout(forceGuardTimer)
    forceGuardTimer = null
  }
  applyMouseIgnore(true)
  startHitPolling()

  // 开发模式加载 Vite dev server，生产模式加载构建产物
  if (process.env.VITE_DEV_SERVER_URL) {
    petWindow.loadURL(process.env.VITE_DEV_SERVER_URL)
  } else {
    petWindow.loadFile(path.join(__dirname, '..', 'dist', 'index.html'))
  }

  // 窗口关闭事件
  petWindow.on('closed', () => {
    stopHitPolling()
    if (forceGuardTimer) {
      clearTimeout(forceGuardTimer)
      forceGuardTimer = null
    }
    hitBoxes = []
    forceCapture = false
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

  // 同步 createPetWindow 的尺寸公式：精灵 + PADDING + 2*MENU_PADDING
  const contentW = Math.round(FRAME_W * scale) + PADDING + 2 * MENU_PADDING
  const contentH = Math.round(FRAME_H * scale) + PADDING + 2 * MENU_PADDING

  petWindow.setSize(contentW, contentH)
}

// ---------------------------------------------------------------------------
// 点击穿透：光标命中轮询
// ---------------------------------------------------------------------------
//
// Windows 透明窗口按整个窗口矩形命中（CSS pointer-events 只作用于渲染层），
// 于是狐狸四周的透明边距会整块吞掉屏幕点击。setIgnoreMouseEvents(true,
// { forward: true }) 本应把 move 事件转发进渲染层做命中判定，但 Electron 29
// 在本环境下转发不生效（实测穿透态渲染层收不到任何 mousemove），因此改由
// 主进程轮询光标位置：落在狐狸可见包围盒内 → 收回捕获（可点可拖），
// 否则 → OS 层穿透（点击直达下层窗口）。

let hitTimer: ReturnType<typeof setInterval> | null = null

/** 渲染层交互态（菜单/面板/拖拽）要求整窗收回捕获 */
let forceCapture = false

/** 当前 OS 层鼠标忽略状态（缓存，避免每轮重复调用 setIgnoreMouseEvents） */
let ignoring = true

/** 菜单/面板按钮屏幕坐标列表（由渲染层 RadialMenu/Panel 推送到主进程，屏幕绝对坐标） */
let hitBoxes: Array<{ x: number; y: number; w: number; h: number }> = []

/** forceCapture 防御 timer：到时强制释放，防止 forceCapture 标志锁死 */
let forceGuardTimer: ReturnType<typeof setTimeout> | null = null

function applyMouseIgnore(ignore: boolean): void {
  if (!petWindow || petWindow.isDestroyed()) return
  if (ignore === ignoring) return
  ignoring = ignore
  petWindow.setIgnoreMouseEvents(ignore)
}

/** 光标是否落在狐狸可见包围盒内（窗口 DIP 坐标 → 精灵帧内坐标）。 */
function cursorOnFox(): boolean {
  if (!petWindow || petWindow.isDestroyed() || !petWindow.isVisible()) return false
  const b = petWindow.getBounds()
  const p = screen.getCursorScreenPoint()
  const lx = p.x - b.x
  const ly = p.y - b.y
  if (lx < 0 || ly < 0 || lx >= b.width || ly >= b.height) return false
  // 画布在窗口内居中：反解缩放 = (窗口宽 - 全部留白) / 帧宽
  const scale = (b.width - PADDING - 2 * MENU_PADDING) / FRAME_W
  if (scale <= 0) return false
  // 精灵相对窗口左上角的偏移（窗口内居中）
  const offsetX = (b.width - FRAME_W * scale) / 2
  const offsetY = (b.height - FRAME_H * scale) / 2
  const fx = (lx - offsetX) / scale
  const fy = (ly - offsetY) / scale
  return (
    fx >= FOX_BBOX.x && fx <= FOX_BBOX.x + FOX_BBOX.w &&
    fy >= FOX_BBOX.y && fy <= FOX_BBOX.y + FOX_BBOX.h
  )
}

/**
 * 光标是否落在某个“捕获目标”区域：狐狸可见包围盒 + 渲染层推送的菜单/面板按钮。
 * 主进程轮询以这个为唯一判定——让 forceCapture=true 期间仍能在按钮外穿透。
 */
function cursorOnHitTarget(): boolean {
  if (cursorOnFox()) return true
  if (hitBoxes.length === 0) return false
  const p = screen.getCursorScreenPoint()
  for (let i = 0; i < hitBoxes.length; i++) {
    const b = hitBoxes[i]
    if (p.x >= b.x && p.x <= b.x + b.w && p.y >= b.y && p.y <= b.y + b.h) {
      return true
    }
  }
  return false
}

/**
 * 接收渲染层推送的菜单/面板按钮坐标（渲染层视口坐标）。
 * 主进程换算为屏幕绝对坐标（加窗口位置偏移）后用于 hitTarget 判定。
 * 传空数组表示清除所有按钮区域（菜单卸载时调用）。
 */
export function setMenuButtonBoxes(boxes: Array<{ x: number; y: number; w: number; h: number }>): void {
  if (!petWindow || petWindow.isDestroyed()) {
    hitBoxes = []
    applyMouseIgnore(true)
    return
  }
  const winBounds = petWindow.getBounds()
  // 渲染层视口坐标 → 屏幕绝对坐标：加窗口位置偏移。
  hitBoxes = (Array.isArray(boxes) ? boxes : []).map((b) => ({
    x: b.x + winBounds.x,
    y: b.y + winBounds.y,
    w: b.w,
    h: b.h,
  }))
  // 坐标更新后立即同步一次捕获态，不等下一轮轮询
  applyMouseIgnore(!cursorOnHitTarget())
}

function startHitPolling(): void {
  if (hitTimer) return
  hitTimer = setInterval(() => {
    if (!petWindow || petWindow.isDestroyed()) {
      stopHitPolling()
      return
    }
    // forceCapture 期间也用 hitTarget 判定：能点菜单按钮/拖拽中能拖窗口；
    // 光标在按钮/fox 外则透传（避免菜单期间点不到屏幕下层按钮的“空气墙”）。
    applyMouseIgnore(!cursorOnHitTarget())
  }, HIT_POLL_MS)
}

function stopHitPolling(): void {
  if (hitTimer) {
    clearInterval(hitTimer)
    hitTimer = null
  }
}

/**
 * 渲染层交互态开关：force=true → 整窗轮询按 hitTarget 判定（菜单按钮区可点），
 * 拖拽期间 force=true 能保证拖拽动作不被命中轮询打断；
 * force=false → 走光标轮询（只有狐狸可见像素 + 推送的按钮区域捕获鼠标）。
 *
 * 加 5s 防御 timer：拖拽/菜单异常退出未释放时强制清除 forceCapture，
 * 防止“空气墙”锁死（用户可在托盘菜单里“Reset Click Capture”手动加速释放）。
 */
export function setPetForceCapture(force: boolean): void {
  forceCapture = force
  if (forceGuardTimer) {
    clearTimeout(forceGuardTimer)
    forceGuardTimer = null
  }
  if (force) {
    // 防御性超时：5s 后强制释放，防 IPC 丢包或 drag 异常中断导致 forceCapture 锁死
    forceGuardTimer = setTimeout(() => {
      forceCapture = false
      hitBoxes = []
      if (!petWindow || petWindow.isDestroyed()) return
      applyMouseIgnore(!cursorOnFox())
    }, FORCE_CAPTURE_GUARD_MS)
  }
  if (!petWindow || petWindow.isDestroyed()) return
  applyMouseIgnore(!cursorOnHitTarget())
}

/**
 * 托盘/快捷键调用：强制释放 forceCapture + 清除所有 hitBoxes，
 * 用户遇到空气墙时一键恢复桌宠可穿透状态。
 */
export function resetPetClickCapture(): void {
  forceCapture = false
  hitBoxes = []
  if (forceGuardTimer) {
    clearTimeout(forceGuardTimer)
    forceGuardTimer = null
  }
  if (!petWindow || petWindow.isDestroyed()) return
  applyMouseIgnore(!cursorOnFox())
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

  const popupW = 240
  const popupH = 276
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
// 系统面板窗口（独立窗口，不再嵌入桌宠窗口）
// ---------------------------------------------------------------------------
// 原本在径向菜单点“系统”会在桌宠窗口（148×160）里弹出 340×460 的 SystemPanel，
// 面板溢出被裁，只能看到一角。现在改为独立窗口，与 CLI 终端同模式。
// ---------------------------------------------------------------------------

let systemWindow: BrowserWindow | null = null

/**
 * 创建/聚焦系统面板窗口。
 */
export function createSystemWindow(petBounds: { x: number; y: number; w: number; h: number }): BrowserWindow {
  if (systemWindow) {
    if (systemWindow.isDestroyed()) {
      systemWindow = null
    } else {
      systemWindow.focus()
      return systemWindow
    }
  }

  const panelW = 360
  const panelH = 500
  const gap = 8

  // 智能定位：宠物在屏幕右半 → 面板在左边；否则在右边
  const display = screen.getPrimaryDisplay()
  const screenW = display.workAreaSize.width
  const petCenterX = petBounds.x + petBounds.w / 2
  const showOnLeft = petCenterX > screenW / 2

  const x = showOnLeft
    ? petBounds.x - panelW - gap
    : petBounds.x + petBounds.w + gap
  const y = petBounds.y
  // 确保不超出屏幕左边界
  const finalX = Math.max(0, x)

  systemWindow = new BrowserWindow({
    x: finalX,
    y,
    width: panelW,
    height: panelH,
    transparent: true,
    frame: false,
    alwaysOnTop: true,
    resizable: false,
    skipTaskbar: true,
    hasShadow: true,
    movable: true,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  })

  // 加载系统面板页面（与状态弹窗走同一个 hash 路由机制）
  if (process.env.VITE_DEV_SERVER_URL) {
    systemWindow.loadURL(`${process.env.VITE_DEV_SERVER_URL}#system-panel`)
  } else {
    systemWindow.loadFile(path.join(__dirname, '../dist/index.html'), {
      hash: 'system-panel',
    })
  }

  systemWindow.on('closed', () => {
    systemWindow = null
  })

  return systemWindow
}

/**
 * 关闭系统面板窗口
 */
export function closeSystemWindow(): void {
  if (systemWindow && !systemWindow.isDestroyed()) {
    systemWindow.close()
  }
  systemWindow = null
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

  // 纯展示窗口：完全点击穿透，气泡的透明矩形不吞屏幕点击
  bubbleWindow.setIgnoreMouseEvents(true)

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
