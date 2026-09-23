# Spirit Agent v0.1.0 发布说明

从 v0.0.2 到 v0.1.0，Spirit Agent 从「桌宠 + 对话循环」扩展为一套具备**长程自治能力**的本地 Agent 框架。

## 新增能力

### Computer Use（桌面应用控制）
截图 → 视觉路由 → 点击/输入，让 Agent 直接操作本地桌面应用。内置权限确认（`permissions`）、安全护栏（`safety`）与 noop 降级后端。
- 代码：`spirit/computer_use/`、`spirit/tools/computer_use_tool.py`

### Ralph Loop 目标系统
长程目标管理：目标状态机（`goal_state`）、自评判（`judge`）、主循环（`loop`）、管理器（`manager`）、持久化（`store`），支持大目标分解并迭代收敛。
- 代码：`spirit/goals/`

### MoA 混合智能体
移植 Hermes `moa_loop`，实现 mixture-of-agents 多智能体协作与轨迹追踪（`moa_trace`）。
- 代码：`spirit/moa/`

### Skills Hub（技能中枢）
技能发现（`discovery`）、分发（`dispatch`）、打包（`bundles`）、预处理（`preprocessing`）、溯源（`provenance`）统一入口。
- 代码：`spirit/skills_hub/`

### 进程管理
后台进程注册（`registry`）、会话（`session`）、通知（`notifications`）。
- 代码：`spirit/process/`、`spirit/tools/process_tool.py`

### TUI 终端
独立 TUI 服务（`tui_server`）+ 项目树（`project_tree`）+ slash 命令（`slash_worker`）+ 渲染（`render`）。
- 代码：`spirit/tui/`

## 其它

- 版本号全仓对齐 **0.1.0**（`desktop-app/package.json`、`spirit/desktop/ws_server.py`、`spirit/__init__.py`、`pyproject.toml`、`vscode-extension/package.json`）。
- 测试套件大幅扩充：新增 computer_use / goals / moa / process / skills_hub / tui / tools / hooks / cli / desktop 等测试。
- 新增 as-built 系统架构文档 `docs/12-system-architecture-asbuilt.md`。

## 说明

本版本聚焦 Agent 本体能力。SWE-bench 评测脚手架与数据（`benchmarks/`）为本地评测用途，已排除在发布仓库外。
