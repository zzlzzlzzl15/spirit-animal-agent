# Spirit Agent — 产品愿景与定位

> **一句话定位**：一个统一后端 AI Agent 服务，同时服务 VSCode 编辑器、Web Dashboard、CLI 终端和消息平台，复用 Hermes 工具生态。

---

## 一、产品愿景

### 1.1 为什么需要 Spirit Agent？

当前 AI 编程助手市场存在一个核心矛盾：

| 场景 | 现有方案 | 痛点 |
|------|---------|------|
| **编辑器辅助编程** | Copilot / Cursor / Cline | 只能在编辑器内用，无法操控系统 |
| **系统级 AI Agent** | Hermes / Open Interpreter | 只能在终端用，无法深度集成编辑器 |
| **多平台统一 Agent** | 不存在 | 每个平台一个独立 Agent，记忆/工具/会话互不相通 |

**Spirit Agent 要解决的**：一个 Agent 核心，同时是"编辑器助手"和"系统级 Agent"和"多平台服务"。

### 1.2 核心场景

```
┌─────────────────────────────────────────────────────────────┐
│                    Spirit Agent 后端服务                      │
│                  (常驻运行的守护进程)                          │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │ VSCode   │  │ Web      │  │ CLI      │  │ Telegram │   │
│  │ 扩展     │  │ Dashboard│  │ 终端     │  │ Discord  │   │
│  │          │  │          │  │          │  │ Slack    │   │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘   │
│       └──────────────┴──────────────┴──────────────┘        │
│                          │                                   │
│                   ┌──────┴──────┐                            │
│                   │  Agent 核心  │                            │
│                   │ (同一套逻辑) │                            │
│                   └──────┬──────┘                            │
│                          │                                   │
│              ┌───────────┼───────────┐                       │
│              ↓           ↓           ↓                       │
│         文件系统      LLM API     数据库                     │
└─────────────────────────────────────────────────────────────┘
```

**场景举例**：

1. **在 VSCode 中**：Spirit 帮你写代码、重构、调试，能读写文件、运行终端
2. **在 Telegram 中**：Spirit 帮你查项目状态、部署、搜索文档
3. **在 Web Dashboard 中**：查看所有会话历史、配置管理、监控 Agent 状态
4. **关键**：三个平台共享同一个 Agent 核心、同一套记忆、同一个会话历史

---

## 二、产品特性

### 2.1 核心特性

| 特性 | 说明 |
|------|------|
| **统一 Agent 核心** | 所有前端共享同一个 Agent 对话循环和工具系统 |
| **Hermes 工具复用** | 直接复用 `tools/` 目录下的 90+ 工具（终端/文件/浏览器/搜索等） |
| **VSCode 深度集成** | 作为扩展安装，提供代码补全、内联操作、文件 diff 预览 |
| **多平台消息网关** | 支持 Telegram/Discord/Slack/钉钉/飞书等消息平台 |
| **会话持久化** | SQLite + FTS5 全文搜索，跨会话记忆 |
| **上下文压缩** | 长对话自动压缩，不丢失关键信息 |
| **插件化扩展** | 技能/插件/MCP 服务器三层扩展机制 |
| **多 Provider 支持** | OpenAI/Anthropic/Google/Azure/本地模型，凭证池轮换 |

### 2.2 与 Hermes 的关系

| 维度 | Hermes | Spirit Agent |
|------|--------|-------------|
| **工具层** | 自研 90+ 工具 | **直接复用** Hermes 的 `tools/` 目录 |
| **Agent 核心** | AIAgent 转发器架构 | **参考设计**，简化重构 |
| **对话循环** | conversation_loop.py (5700行) | **精简复刻**，去掉不需要的复杂度 |
| **数据库** | SQLite + FTS5 | **复用** hermes_state.py 的设计 |
| **前端** | CLI + TUI + Electron + Gateway | **新增** VSCode 扩展 + Web Dashboard |
| **通信协议** | JSON-RPC over stdio / HTTP | **新增** WebSocket + LSP |

### 2.3 差异化优势

```
vs Copilot/Cursor:
  ✅ 系统级能力（终端/文件/浏览器/进程管理）
  ✅ 多平台统一（不只是编辑器）
  ✅ 自托管（数据不经过第三方）
  ✅ 可自定义工具/技能/插件

vs Hermes:
  ✅ VSCode 深度集成（编辑器内体验）
  ✅ Web Dashboard（可视化管理）
  ✅ 更精简的核心（去掉不需要的复杂度）
  ✅ 中文优先（系统提示词/文档/社区）
```

---

## 三、目标用户

### 3.1 主要用户画像

| 用户类型 | 需求 | Spirit 如何满足 |
|---------|------|----------------|
| **全栈开发者** | 编码 + 部署 + 调试一站式 | VSCode 扩展 + 终端工具 |
| **AI 爱好者** | 自托管、可定制、隐私友好 | 本地运行、多 Provider、插件化 |
| **团队用户** | 多人共享 Agent、统一配置 | Gateway 多平台接入、配置管理 |
| **效率极客** | Telegram/Discord 随时调用 Agent | 消息网关、斜杠命令 |

### 3.2 非目标（明确不做的事）

- ❌ 不做云端 SaaS（纯本地部署）
- ❌ 不做闭源（MIT 开源）
- ❌ 不做重型 IDE（不做第二个 VSCode）
- ❌ 不做训练/微调（只做推理层）

---

## 四、产品形态

### 4.1 安装方式

```bash
# 方式 1：pip 安装（后端服务）
pip install spirit-agent

# 方式 2：VSCode 扩展市场安装
# 在 VSCode 扩展商店搜索 "Spirit Agent"

# 方式 3：Docker 一键部署
docker run -d spirit-agent/server:latest
```

### 4.2 启动方式

```bash
# 启动后端守护进程
spirit start

# 启动 Web Dashboard（默认 :9000）
spirit dashboard

# 启动 Gateway（消息平台）
spirit gateway

# 交互式 CLI
spirit chat
```

### 4.3 前端入口

| 入口 | 端口 | 说明 |
|------|------|------|
| **VSCode 扩展** | - | 编辑器内使用，连接本地 Spirit 后端 |
| **Web Dashboard** | :9000 | 配置管理、会话历史、聊天界面 |
| **CLI** | - | `spirit chat` 交互式终端 |
| **消息平台** | - | Telegram/Discord/Slack 等 |
| **API** | :9001 | OpenAI 兼容 HTTP API |

---

## 五、商业模式

Spirit Agent 本身完全开源免费，通过以下方式可持续运营：

| 方式 | 说明 |
|------|------|
| **云服务（可选）** | 提供托管版 Spirit Cloud，免部署 |
| **企业版** | 多用户权限、审计日志、SSO 集成 |
| **插件市场** | 第三方插件/技能分成 |
| **技术支持** | 企业级 SLA 和技术咨询 |

---

## 六、项目命名

**Spirit Agent** — "灵"

- "Spirit" 意为"灵魂/精灵"，寓意 Agent 是系统的灵魂
- 与 Hermes（信使之神）呼应，Spirit 是更内层的"核心灵魂"
- 简短好记，适合做品牌

---

*下一步：[02-hermes-architecture-analysis.md](./02-hermes-architecture-analysis.md) — Hermes 架构深度分析*
