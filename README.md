# Spirit Agent — 桌宠形态的本地 AI Agent

> v0.0.2 · Electron 桌面宠物 + Python Agent 后端 + 交互式 CLI 终端
> 对话循环架构对标 [Hermes Agent](https://github.com/zzlzzlzzl15/spirit-animal-agent/tree/master) v0.18.2：单主循环 + 迭代预算 + 分支化响应处理。

Spirit Agent 是一只住在你桌面上的小狐狸：它既是**桌面宠物**（会动、会说话、会感知 Agent 工作状态），也是一个**完整的本地 AI Agent**（工具调用、多轮循环、流式输出、知识库、语音）。所有能力通过一个 WebSocket 桥接层对多表面（桌宠窗口 / CLI 终端 / VSCode 扩展）统一开放。

---

## 一、主要功能

### 1. 桌面宠物系统 🦊

- **精灵动画引擎**：spritesheet 逐帧渲染（双缓冲离屏画布防闪烁），`Agent 活动信号 → PetState 状态机` 自动推导动画（空闲 / 思考 / 工具执行 / 庆祝 / 报错 …）。
- **窗口特性**：透明无边框、始终置顶、不抢焦点；拖拽移动、滚轮缩放（0.5–3.0）、位置与缩放持久化。
- **交互**：左键径向菜单（知识库 / 系统面板 / CLI / 语音）、右键独立状态弹窗（失焦自动关闭）、系统托盘菜单（显隐 / 切换宠物 / 退出）。
- **趣味气泡**：独立窗口显示在小狐狸头顶，4 秒自动消失；**非焦点窗口实现（`focusable: false` + `showInactive`），弹出时绝不打断任何窗口的键盘输入**。
- **宠物管理**：`~/.spirit/pets` 宠物仓库 + `pet_prefs.json` 偏好持久化；首次启动自动引导安装内置灵狐；托盘 / CLI `/switch` 切换宠物，服务端广播 `pet_switch` 事件全窗口实时同步。
- **健壮性**：休眠恢复 / 显示器变化时强制重绘透明窗口（修复"狐狸消失"）；恢复坐标离屏校验自动回退主屏。

### 2. 交互式 CLI 终端 💬

基于 xterm.js 的全功能终端（独立窗口）：

- **Slash 命令系统**（参考 Hermes COMMAND_REGISTRY 分类）：`/help` `/new` `/history` `/status` `/compress` `/stop` `/config` `/model` `/provider` `/tools` `/pets` `/switch` `/search` `/sys` `/version` `/clear` `/exit`，支持 Tab 补全与模糊匹配。
- **终端体验**：命令历史（↑/↓）、Ctrl+C/L/U/W 快捷键、中文 IME 焦点修复、Ctrl+C/V 复制粘贴。
- **工具调用内联标注**：`🔧 调用工具: web_search（参数预览）` → `✓ web_search 完成 (耗时)`，与 spinner 思考动画交替呈现。
- **Markdown → ANSI 逐行渲染器**：流式增量先按行缓冲再格式化输出（根治阶梯错行）；标题彩色层级、加粗 / 行内代码 / 链接 / 列表 / 引用 / 表格 / 分隔线全部转终端样式。
- **底部状态栏**：模型 | 会话时长 | 消息数 | 工具数 | 连接状态。

### 3. Hermes 同款对话循环 🔄

`spirit/agent/conversation_loop.py` — 唯一主循环，流式作为传输层集成在循环内：

- **分支 A**：响应含 tool_calls → 先持久化消息 → 并发（>1）/ 顺序执行工具 → 结果回填 → `continue` 回循环顶。
- **分支 B**：纯文本最终回答 → 清理 think/工具标签 → 门控回补推送 → 结束轮次。
- **分支 C**：异常分类（`classify_api_error`）+ 指数退避重试。
- **IterationBudget 迭代预算**：防止无限循环；tool_call 先持久化后执行保证会话可恢复。
- **双通道工具调用解析**：原生 OpenAI `tool_calls` 优先，文本 XML/JSON 回退（`parse_text_tool_calls`，支持单块多 JSON 扫描）。
- **流式门控**：检测到 `<think>`/`<tool_call>` 等标签开头即暂扣实时推送（防原始 JSON 泄露终端），轮次结束后清理回补；干净文本则逐 token 真流式。
- **性能**：系统提示词字节稳定保 KV cache、prompt caching 断点注入、上下文压缩引擎（延迟检查）。

### 4. 工具系统 

- **90+ 工具**全量移植自 Hermes：`web_search` / `web_fetch` / 文件读写 / shell / 代码编辑 / LSP 代码智能 / 知识库 / 任务管理 / 宠物控制 …
- **统一执行入口** `invoke_tool`：`on_tool_start / on_tool_complete` 回调单一触发源，并发与顺序路径行为一致。
- **工具护栏**：调用前后安全检查（路径越界 / 危险命令拦截）。

### 5. WebSocket 桥接层 🌉

`ws://127.0.0.1:9877` — 所有表面的统一接入点：

- **事件协议**：`stream_delta` / `tool_start` / `tool_complete` / `chat_complete` / `state_change` / `pet_switch` / `init`。
- **命令协议**：`chat` / `get_status` / `get_history` / `switch_pet` / `set_scale` / `list_tools` / `compress_context` …
- **线程安全调度**：执行器线程 → 事件循环统一 `run_coroutine_threadsafe` 阻塞等待，保证 delta 与 complete 事件严格有序（修复过乱序丢文本）。

### 6. 生态集成 🧩

- **Memora 个人知识库**（Docker Compose：MySQL / Redis / Qdrant / Neo4j）：Agent 文件记忆载体，CLI `/search` 可检索。
- **多 Provider**：MiniMax-M3（默认）/ OpenAI / Anthropic / DashScope / Ollama，`/model` `/provider` 热切换。
- **语音引擎**：STT/TTS 接口预留（径向菜单入口）。
- **VSCode 扩展**：双向 WebSocket 协议接入代码智能与编辑能力。

---

## 二、架构总览

```
┌─────────────────────────── Electron 主进程 ───────────────────────────┐
│  桌宠窗口(透明置顶)   CLI 终端窗口(xterm)   状态弹窗   气泡窗口(非焦点)   │
│        │ Vue3 + Pinia          │ Vue3 + xterm.js                      │
│        └──────────────┬────────┘                                      │
│                  preload IPC                                          │
│   托盘 / 窗口管理 / 位置持久化 / 休眠重绘 / spawn Python 后端             │
└──────────────────────────────┬────────────────────────────────────────┘
                               │ ws://127.0.0.1:9877 (事件 + 命令)
┌──────────────────────────────┴────────────────────────────────────────┐
│  Python 后端 (_launcher.py)                                            │
│   WSServer ── PetEngine(状态机/宠物/偏好) ── SpiritAgent                │
│                  │                      │   conversation_loop 单主循环 │
│                  │                      ├─ tool_executor (90+ 工具)    │
│                  │                      ├─ context 压缩 / prompt cache │
│                  │                      └─ MiniMax / OpenAI / ...      │
│   Memora (Docker: MySQL/Redis/Qdrant/Neo4j)                           │
└───────────────────────────────────────────────────────────────────────┘
```

## 三、目录结构

```
spirit-agent-main/
├── spirit/
│   ├── agent/            # 对话循环 / 工具执行 / 上下文压缩 / LSP / 文本工具解析
│   ├── tools/            # 90+ 工具实现
│   ├── desktop/          # PetEngine / ws_server / pet_store / 语音引擎
│   ├── cli/              # 增强版 CLI 入口
│   └── config.py         # 配置加载 (~/.spirit/config.yaml)
├── desktop-app/
│   ├── electron/         # 主进程：窗口 / 托盘 / IPC / 后端 spawn
│   ├── src/              # Vue3：桌宠 / CLI 终端 / 状态弹窗 / 气泡 / 面板
│   └── public/assets/pets/  # 内置宠物精灵图
├── e2e_tool_loop_test.py # 后端循环层端到端回归
├── e2e_ws_event_test.py  # WS 事件层端到端回归
└── README.md
```

## 四、快速开始

**依赖**：Python 3.10+ · Node 18+ · （可选）Docker（Memora 知识库）

```bash
# 1. 配置模型（~/.spirit/config.yaml）
#    model: MiniMax-M3 / provider: minimax / api_key: *** / streaming.enabled: true

# 2. 安装前端依赖并构建
cd desktop-app
npm install
npx vite build          # 产出 dist/ 与 dist-electron/

# 3. 启动（Electron 主进程会自动 spawn Python 后端与 Memora）
npx electron .
```

启动后：桌面出现小狐狸 → 左键菜单选 **CLI** 打开交互终端 → 直接输入「帮我查看一下今日的财经新闻」体验 工具标注 → 多轮循环 → 流式 Markdown 总结 的完整链路。

**回归测试**：

```bash
python e2e_tool_loop_test.py   # 后端循环层：迭代/工具/总结
python e2e_ws_event_test.py    # WS 事件层：tool_start/complete + stream_delta 有序性
```

## 五、版本记录

### v0.0.2（当前）

- 对话循环重构为 Hermes 同款单主循环（分支 A/B/C + 迭代预算 + 错误分类重试）。
- CLI 终端：工具调用内联标注、Markdown→ANSI 逐行渲染、流式门控回补、chat_complete 兜底渲染。
- 修复 WS 事件竞态（阻塞式线程安全调度），最终文本零丢失。
- 气泡窗口非焦点化，不再打断聊天输入；休眠恢复强制重绘修复"狐狸消失"。
- 宠物链路端到端：引导安装内置灵狐、切换广播同步、缩放单一真相源。
- 90+ Hermes 工具全量迁移；Memora 知识库集成；prompt caching 与上下文压缩。

### v0.0.1

- 初始版本：桌宠窗口 + 精灵动画 + WebSocket 后端 + 基础对话。

---

## 许可

仅用于学习与个人研究。
