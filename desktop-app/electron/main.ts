/**
 * Spirit Desktop Pet — Electron 主进程入口。
 *
 * 职责：
 * - 创建透明无边框桌宠窗口
 * - 系统托盘图标 + 右键菜单
 * - 启动 Python WebSocket 后端
 * - 窗口位置持久化
 */

import { app, BrowserWindow, Tray, Menu, ipcMain, screen, nativeImage, powerMonitor } from 'electron'
import { createPetWindow, getPetWindow, resizePetWindow, createStatusWindow, closeStatusWindow, createBubbleWindow, closeBubbleWindow, createCLITerminalWindow, closeCLITerminalWindow } from './window'
import path from 'path'
import { spawn, ChildProcess } from 'child_process'

// ---------------------------------------------------------------------------
// 全局状态
// ---------------------------------------------------------------------------

let tray: Tray | null = null
let pythonProcess: ChildProcess | null = null
let memoraStarted = false

// 窗口位置持久化（简易 JSON 存储）
const Store = require('electron-store')
const store = new Store({
  defaults: {
    windowX: -1,
    windowY: -1,
    petScale: 0.75,
    activePetSlug: '',
  },
})

// Python WebSocket 后端端口
const WS_PORT = parseInt(process.env.SPIRIT_WS_PORT || '9877', 10)

// ---------------------------------------------------------------------------
// 应用生命周期
// ---------------------------------------------------------------------------

app.whenReady().then(async () => {
  // 1. 启动 Python WebSocket 后端
  await startPythonBackend()

  // 2. 启动 Memora 知识库服务（Docker Compose）
  startMemoraServices()

  // 3. 创建桌宠窗口
  const savedX = store.get('windowX') as number
  const savedY = store.get('windowY') as number
  let savedScale = store.get('petScale') as number
  // 强制使用新的默认缩放（旧值太小）
  if (savedScale < 0.75 || savedScale > 3.0) {
    savedScale = 0.75
    store.set('petScale', savedScale)
  }

  createPetWindow({
    x: savedX >= 0 ? savedX : undefined,
    y: savedY >= 0 ? savedY : undefined,
    scale: savedScale,
  })

  // 4. 创建系统托盘
  createTray()

  // 5. 注册 IPC 处理器
  registerIPC()

  // 6. 定期保存窗口位置
  setInterval(saveWindowPosition, 5000)

  // 7. 休眠恢复 / 显示器变化时强制重绘桌宠窗口
  // （Windows 透明无边框窗口在休眠恢复后合成器可能停止绘制，
  //   渲染进程活着但狐狸不显示；hide+showInactive 强制重建合成层，
  //   showInactive 不抢焦点）
  const refreshPetWindow = (): void => {
    const win = getPetWindow()
    if (win && !win.isDestroyed()) {
      win.hide()
      win.showInactive()
    }
  }
  powerMonitor.on('resume', refreshPetWindow)
  screen.on('display-metrics-changed', refreshPetWindow)
})

app.on('window-all-closed', () => {
  // macOS 惯例：关闭窗口不退出应用
  if (process.platform !== 'darwin') {
    cleanupAndQuit()
  }
})

app.on('before-quit', () => {
  saveWindowPosition()
  stopPythonBackend()
})

// ---------------------------------------------------------------------------
// 系统托盘
// ---------------------------------------------------------------------------

function createTray(): void {
  // 创建一个 16x16 的小图标（内联生成，无需外部文件）
  const icon = nativeImage.createFromBuffer(createTrayIconBuffer())
  tray = new Tray(icon.resize({ width: 16, height: 16 }))

  tray.setToolTip('Spirit Desktop Pet')

  const contextMenu = Menu.buildFromTemplate([
    {
      label: '🐾 Spirit 桌宠',
      enabled: false,
    },
    { type: 'separator' },
    {
      label: '显示/隐藏',
      click: () => {
        const win = getPetWindow()
        if (win) {
          win.isVisible() ? win.hide() : win.show()
        }
      },
    },
    {
      label: '重置位置',
      click: () => {
        const win = getPetWindow()
        if (win) {
          const { width, height } = screen.getPrimaryDisplay().workAreaSize
          win.setPosition(
            Math.floor(width / 2 - 96),
            Math.floor(height - 200)
          )
        }
      },
    },
    { type: 'separator' },
    {
      label: '切换宠物',
      submenu: [
        { label: '灵狐 (Spirit Fox)', click: () => sendToRenderer('switch-pet', { slug: 'spirit-fox' }) },
        { label: '赛博猫 (Cyber Cat)', click: () => sendToRenderer('switch-pet', { slug: 'cyber-cat' }) },
      ],
    },
    { type: 'separator' },
    {
      label: '退出',
      click: () => {
        saveWindowPosition()
        stopPythonBackend()
        app.quit()
      },
    },
  ])

  tray.setContextMenu(contextMenu)

  // 左键点击托盘 → 显示/隐藏
  tray.on('click', () => {
    const win = getPetWindow()
    if (win) {
      win.isVisible() ? win.hide() : win.show()
    }
  })
}

// ---------------------------------------------------------------------------
// IPC 通信
// ---------------------------------------------------------------------------

function registerIPC(): void {
  // 保存窗口位置
  ipcMain.on('save-position', (_event, { x, y }: { x: number; y: number }) => {
    store.set('windowX', x)
    store.set('windowY', y)
  })

  // 保存缩放
  ipcMain.on('save-scale', (_event, { scale }: { scale: number }) => {
    store.set('petScale', scale)
    // 实时调整桌宠窗口尺寸，匹配新缩放
    resizePetWindow(scale)
  })

  // 获取保存的配置
  ipcMain.handle('get-saved-config', () => {
    return {
      windowX: store.get('windowX'),
      windowY: store.get('windowY'),
      petScale: store.get('petScale'),
      activePetSlug: store.get('activePetSlug'),
      wsPort: WS_PORT,
    }
  })

  // 窗口拖拽移动
  ipcMain.on('move-window', (_event, { deltaX, deltaY }: { deltaX: number; deltaY: number }) => {
    const win = getPetWindow()
    if (!win) return
    const [x, y] = win.getPosition()
    win.setPosition(x + deltaX, y + deltaY)
  })

  // 窗口大小调整（缩放）
  ipcMain.on('resize-window', (_event, { width, height }: { width: number; height: number }) => {
    const win = getPetWindow()
    if (!win) return
    win.setSize(Math.round(width), Math.round(height))
  })

  // 获取屏幕尺寸
  ipcMain.handle('get-screen-size', () => {
    const display = screen.getPrimaryDisplay()
    return {
      width: display.workAreaSize.width,
      height: display.workAreaSize.height,
    }
  })

  ipcMain.handle('get-window-position', () => {
    const win = getPetWindow()
    if (!win) return { x: 0, y: 0, w: 0, h: 0 }
    const bounds = win.getBounds()
    return { x: bounds.x, y: bounds.y, w: bounds.width, h: bounds.height }
  })

  ipcMain.handle('open-status-window', () => {
    const win = getPetWindow()
    if (!win) return false
    const bounds = win.getBounds()
    createStatusWindow({ x: bounds.x, y: bounds.y, w: bounds.width, h: bounds.height })
    return true
  })

  ipcMain.handle('close-status-window', () => {
    closeStatusWindow()
    return true
  })

  // 打开 CLI 终端窗口
  ipcMain.handle('open-cli-terminal', () => {
    createCLITerminalWindow()
    return true
  })

  // 关闭 CLI 终端窗口
  ipcMain.handle('close-cli-terminal', () => {
    closeCLITerminalWindow()
    return true
  })

  // 显示气泡对话（独立窗口，在宠物上方）
  ipcMain.handle('show-bubble', (_event, { text }: { text: string }) => {
    const win = getPetWindow()
    if (!win) return false
    const bounds = win.getBounds()
    createBubbleWindow({ x: bounds.x, y: bounds.y, w: bounds.width, h: bounds.height }, text)
    return true
  })

  // 隐藏气泡对话
  ipcMain.handle('hide-bubble', () => {
    closeBubbleWindow()
    return true
  })

  // 用默认浏览器打开 Memora 知识库
  ipcMain.handle('open-memora', () => {
    const { shell } = require('electron')
    shell.openExternal('http://localhost:8000')
    return true
  })
}

// ---------------------------------------------------------------------------
// Python 后端管理
// ---------------------------------------------------------------------------

async function startPythonBackend(): Promise<void> {
  // 查找 Python 后端入口
  const projectRoot = app.isPackaged
    ? path.join(process.resourcesPath, '..')
    : path.resolve(app.getAppPath(), '..')

  const scriptPath = path.join(projectRoot, 'spirit', 'desktop', '_launcher.py')

  try {
    pythonProcess = spawn('python', [scriptPath, '--ws-port', String(WS_PORT)], {
      cwd: projectRoot,
      env: { ...process.env, SPIRIT_WS_PORT: String(WS_PORT), PYTHONUTF8: '1' },
      stdio: ['pipe', 'pipe', 'pipe'],
    })

    pythonProcess.stdout?.on('data', (data: Buffer) => {
      console.log(`[Python] ${data.toString().trim()}`)
    })

    pythonProcess.stderr?.on('data', (data: Buffer) => {
      console.error(`[Python] ${data.toString().trim()}`)
    })

    pythonProcess.on('exit', (code: number | null) => {
      console.log(`Python 后端已退出 (code: ${code})`)
      pythonProcess = null
    })

    // 等待后端启动
    await new Promise(resolve => setTimeout(resolve, 1000))
    console.log(`Python WebSocket 后端已启动 (port: ${WS_PORT})`)
  } catch (err) {
    console.warn('Python 后端启动失败，将以无后端模式运行:', err)
  }
}

function stopPythonBackend(): void {
  if (pythonProcess) {
    try {
      pythonProcess.kill('SIGTERM')
    } catch {
      // ignore
    }
    pythonProcess = null
  }
}

// ---------------------------------------------------------------------------
// Memora 知识库服务管理
// ---------------------------------------------------------------------------

/**
 * 启动 Memora 知识库服务（Docker Compose）。
 * 如果 Docker 不可用或服务已在运行，静默跳过。
 */
function startMemoraServices(): void {
  if (memoraStarted) return
  memoraStarted = true

  const projectRoot = app.isPackaged
    ? path.join(process.resourcesPath, '..')
    : path.resolve(app.getAppPath(), '..')
  // Memora 在 hermes/Memora，与 spirit-agent-main 同级
  const memoraDir = path.resolve(projectRoot, '..', 'Memora')

  // 检查 Memora 目录是否存在
  const fs = require('fs')
  if (!fs.existsSync(memoraDir)) {
    console.log('[Memora] 目录不存在，跳过启动:', memoraDir)
    return
  }

  console.log('[Memora] 正在启动 Docker Compose 服务...')

  // 使用 docker compose up -d 启动所有服务
  const composeProcess = spawn('docker', ['compose', 'up', '-d'], {
    cwd: memoraDir,
    detached: true,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  })

  composeProcess.stdout?.on('data', (data: Buffer) => {
    console.log(`[Memora] ${data.toString().trim()}`)
  })

  composeProcess.stderr?.on('data', (data: Buffer) => {
    const msg = data.toString().trim()
    if (msg) console.warn(`[Memora] ${msg}`)
  })

  composeProcess.on('exit', (code: number | null) => {
    if (code === 0) {
      console.log('[Memora] Docker Compose 服务启动成功！')
    } else {
      console.warn(`[Memora] Docker Compose 启动失败 (code: ${code})，请确保 Docker Desktop 已运行`)
    }
  })

  // 分离进程，不阻塞 Electron
  composeProcess.unref()
}

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function saveWindowPosition(): void {
  const win = getPetWindow()
  if (win && !win.isDestroyed()) {
    const [x, y] = win.getPosition()
    store.set('windowX', x)
    store.set('windowY', y)
  }
}

function sendToRenderer(channel: string, data: any): void {
  const win = getPetWindow()
  if (win && !win.isDestroyed()) {
    win.webContents.send(channel, data)
  }
}

/**
 * 生成 16x16 托盘图标（内联 RGBA buffer，无需外部文件）。
 * 简单的小狐狸头像 — 橙色方块 + 白色眼睛。
 */
function createTrayIconBuffer(): Buffer {
  const size = 16
  const channels = 4
  const buf = Buffer.alloc(size * size * channels, 0)

  function setPixel(x: number, y: number, r: number, g: number, b: number, a: number = 255) {
    if (x < 0 || x >= size || y < 0 || y >= size) return
    const idx = (y * size + x) * channels
    buf[idx] = r
    buf[idx + 1] = g
    buf[idx + 2] = b
    buf[idx + 3] = a
  }

  // 填充矩形区域
  function fillRect(x1: number, y1: number, x2: number, y2: number, r: number, g: number, b: number, a: number = 255) {
    for (let y = y1; y <= y2; y++) {
      for (let x = x1; x <= x2; x++) {
        setPixel(x, y, r, g, b, a)
      }
    }
  }

  // 橙色脸部
  fillRect(3, 3, 12, 13, 255, 140, 50)

  // 白色眼睛
  fillRect(5, 6, 6, 7, 255, 255, 255)
  fillRect(9, 6, 10, 7, 255, 255, 255)

  // 黑色瞳孔
  setPixel(5, 7, 0, 0, 0)
  setPixel(10, 7, 0, 0, 0)

  // 白色鼻子
  fillRect(7, 9, 8, 10, 255, 255, 255)

  // 耳朵（三角形近似）
  fillRect(2, 1, 4, 3, 255, 140, 50)
  fillRect(11, 1, 13, 3, 255, 140, 50)
  // 内耳
  setPixel(3, 2, 255, 180, 120)
  setPixel(12, 2, 255, 180, 120)

  return buf
}

function cleanupAndQuit(): void {
  saveWindowPosition()
  stopPythonBackend()
  app.quit()
}
