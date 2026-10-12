# Spirit Agent v0.3.2 Release Notes

> 本次版本聚焦 **桌宠 UI 收尾 + turn_liveness watchdog 误杀根治 + CLI 默认进入对话**。

## 1. 桌宠 UI 收尾（Electron desktop-app）

### 1.1 圆形气泡 + 智能环绕
- 桌宠气泡尺寸缩到 24px（豆包风格），内部画小狐狸
- 默认锚定桌面边缘（不抢占工作区）
- 点击后弹出 **4 按钮径向菜单**，从正上方 270° 起向左每 40° 一个（[270, 230, 190, 150]）

### 1.2 按钮名称显示
- 每个按钮右侧加 tooltip 风格 label（系统 / CLI / 用量 / 设置）
- 透明窗口点击穿透 + 隐形块 bug 根治（`ignoring` 缓存初始值改为 `null` 而非 `true`）

### 1.3 系统按钮 + CLI 按钮
- 系统按钮：安装 psutil 后真实显示 CPU/内存/磁盘 + Agent 状态
- CLI 按钮：从内嵌 xterm 改为系统原生终端（macOS osascript / Windows cmd / Linux x-terminal-emulator），执行 `spirit` 自动进入对话

## 2. turn_liveness watchdog 误杀根治（R-P2.2）

**问题**：长 thinking 模型（MiniMax-M3 / GLM-5 / DeepSeek-R1）流式 chunk 间间隔可能 > 600s，spirit 默认 600s watchdog 会把 turn 强制结束。

**根因**：`_call_llm` 之前只在调用前后 touch 一次 activity，期间流式 chunk 不刷新。

**修复** [spirit/agent/conversation_loop.py#L1501](spirit/agent/conversation_loop.py#L1501)：流式 chunk 每次到达都 `_touch_activity('stream_chunk')`。

**效果**：长推理 + 长生成内容场景下不再误杀。

## 3. CLI 默认进入对话（跨平台对齐）

**变更** [spirit/cli/main.py#L834](spirit/cli/main.py#L834)：`@click.group(invoke_without_command=True)`，无子命令调用 `spirit` 自动进入对话（与 Windows 行为一致）。

**保留向后兼容**：`spirit chat` 仍可用，`spirit start / sessions / tools / storage *` 全部不受影响。

**桌面端额外收益**：原"CLI 按钮"调用的就是 `spirit`，现在会自动进入对话（无需改 Electron 代码）。

## 4. TASK_START UX 改进

**问题**：spirit 主代理 + 2 个子代理各发 1 次 task_start，3 次看起来"重复"（子代理的 user_message 模板是 `上下文:\n...\n\n任务: ...`，多行渲染后视觉上一样）。

**修复**：
- 主代理 task_start：`─── 任务开始  上下文:… (+3 行)`（黄色）
- 子代理 task_start：`─── 👥 子任务开始  上下文:… (+3 行)`（青色）
- 多行 summary 折叠为单行（`_fold_summary_single_line` 辅助函数）

## 5. 元数据

- `pyproject.toml`: `0.3.1` → `0.3.2` + build-backend 修正（`setuptools.backends._legacy:_Backend` → `setuptools.build_meta`）
- `spirit/__init__.py`: `__version__ 0.3.1` → `0.3.2`

## 6. LLM 配置推荐（Agent Plan / ark-... key）

```yaml
llm:
  model: "minimax-m3"   # 或 doubao-seed-2.1-pro / glm-5.3 / deepseek-v4-pro 等
  provider: "custom"
  base_url: "https://ark.cn-beijing.volces.com/api/plan/v3"   # 注意不是 /api/coding/v3
  api_key: "ark-..."    # 火山方舟 Agent Plan 专属 Key
```

## 7. 升级方式

```bash
git pull
pip install -e .         # 重装以同步版本号
pip install psutil       # 桌宠系统按钮需要
```

CLI / Electron 桌宠会自动使用新代码（重启 spirit / 桌宠进程即可）。