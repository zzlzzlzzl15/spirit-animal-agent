# Spirit Agent v0.3.3 Release Notes

> 本次版本聚焦 **桌宠交互细节修复 + watchdog 日志降级 + `/pet` 命令自动恢复**。

## 1. 桌宠气泡偏移修复（desktop-app）

**问题**：桌宠气泡（speech bubble）位置看起来**不在桌宠正上方**，白底矩形视觉上飘在桌宠上方约 70px。

**根因**：
- v0.3.2 用 `align-items: center` 让气泡 wrapper 在 60px 高的 BrowserWindow 内撑满 + 居中
- 结果 content 矩形顶在窗口顶 0，content 中心距 pet 中心 70px（看着远）

**修复** [desktop-app/src/components/SpeechBubblePage.vue](desktop-app/src/components/SpeechBubblePage.vue)：
- `align-items: center` → `align-items: flex-end`
- wrapper 底贴 BrowserWindow 底，content 紧贴 tail，tail 紧贴圆顶 10px gap

**效果**：气泡看起来"站在"小狐狸头顶，对齐干净。

## 2. turn_liveness watchdog 日志降级

**问题**：watchdog 强制结束 turn（idle 超 600s）时打印 `ERROR [R-P2.2]`，用户**误以为系统崩溃**。

**实际**：watchdog abort 是**设计内行为**（idle 超时回收资源），不是 bug。

**修复** [spirit/agent/turn_liveness.py#L312](spirit/agent/turn_liveness.py#L312)：
- `logger.error` → `logger.info`
- `spirit/cli/main.py` 默认 log level = `ERROR`，INFO 级别在终端**不显示**
- 避免"看着像报错"的错觉
- `-v` 详细模式（DEBUG）下仍能看到

## 3. `/pet` 命令四态智能判断

**问题**：v0.3.2 `/pet` 只检测 launcher 端口（9877）。如果 Electron GUI 进程死了但 launcher 还在，命令会误判"桌宠已在运行"——**什么都不做**，用户看到提示但桌宠窗口已经没了。

**修复** [spirit/cli/main.py#L1455-1709](spirit/cli/main.py#L1455-L1709)：四态判断

| 状态 | launcher 端口 | Electron 进程 | 行为 |
|---|---|---|---|
| A | ✗ | ✗ | 完整启动 |
| B | ✓ | ✓ | 提示已运行（**显示两个 PID**） |
| C | ✓ | ✗ | **自动只重启 Electron，保留 launcher**（核心修复） |
| D | ✗ | ✓ | 杀 Electron 走完整启动（罕见兜底） |

**新增 helper**：
- `_find_electron_main_pids()` — 用 `ps -eo pid,command` 找含 `dist-electron/main.js` 的 PID
- `_spawn_electron_and_wait()` — 抽出现有 spawn + wait 逻辑
- `_spawn_electron_only()` — 状态 C 专用

**效果**：下次桌宠窗口莫名消失时，跑 `/pet` 就能**自动恢复**（不用手动重启 Electron）。

## 4. 元数据

- `pyproject.toml`: `0.3.2` → `0.3.3`
- `spirit/__init__.py`: `__version__ 0.3.2` → `0.3.3`

## 5. 升级方式

```bash
git pull
pip install -e .         # 重装以同步版本号
```

CLI / Electron 桌宠会自动使用新代码（重启 spirit / 桌宠进程即可）。
