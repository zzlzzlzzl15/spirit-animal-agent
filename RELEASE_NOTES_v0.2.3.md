# Spirit Agent v0.2.3 更新说明

> 补丁发布：桌宠"空气墙"根治（主进程精细命中判定 + 防御 timer + 托盘兜底）、V2.1 MoA / Tool Search（Hermes 同款 Progressive Tool Disclosure + The Agency 282 specialists 桥接）、V3 长期路线图（V2 是"能跑"、V3 是"能信"）。

## 一、桌宠"空气墙"根治

### 根因复盘

v0.2.2 修复后，渲染层不再拦截鼠标，但仍有三种诡异卡死：
- **场景 R1**：右键弹径向菜单后，移到桌宠外的网页按钮，**要点击 2 次才能穿透**——首次点击被桌宠 OS 层窗口矩形拦截。
- **场景 R2**：拖拽 / 菜单异常退出时，`forceCapture` 标志锁死为 `true`，整个 308×324 窗口永久拦截点击，桌宠就像在屏幕右侧 1/3 处立了一块空气墙。
- **场景 R3**：用户放大桌宠到 3× 占视觉位置后，透明边距随之变大，整窗拦截更严重。

### v0.2.3 修复（4 文件，0 新增）

| 文件 | 改动 | 作用 |
|------|------|------|
| `desktop-app/electron/window.ts` | +87 / -5 | `cursorOnHitTarget()` 精细判定 / `setMenuButtonBoxes()` IPC / `resetPetClickCapture()` 兜底 / 5s 防御 timer |
| `desktop-app/electron/main.ts` | +13 / -2 | IPC handler `pet-set-menu-button-boxes` + 托盘菜单"🔓 重置点击捕获（修复空气墙）" |
| `desktop-app/electron/preload.ts` | +7 / -1 | 暴露 `setMenuButtonBoxes` API |
| `desktop-app/src/components/RadialMenu.vue` | +27 / -1 | `onMounted` 250ms 后推送按钮坐标 / `onUnmounted` 清空 |

### 三处关键改动

1. **精细命中判定**：`cursorOnHitTarget()` = `cursorOnFox()` ∪ 菜单按钮 hitBoxes。原先 `cursorOnFox()` 命中整窗矩形（≈95% 都是透明边距），现在按精灵帧 alpha 实测 bbox (28,6)-(178,202) + 4 个径向按钮 44×44 单独判定；按钮外立刻 OS 层穿透。
2. **5s 防御 timer**：`setPetForceCapture(true)` 启动 5s 超时；到时强制 `forceCapture=false` + 清空 hitBoxes + 回到轮询态。即使拖拽半路异常退出也不会卡死。
3. **托盘兜底**："🔓 重置点击捕获（修复空气墙）" 一键调用 `resetPetClickCapture()`，兜底任何诡异状态。

## 二、V2.1 MoA / Tool Search（Hermes 同款）

### 背景

Spirit 默认注册 21 个 toolset / 60+ 工具，完整 schema 约 40KB。一次性塞进每次 API 请求会让 MiniMax 等上游频繁触发 `529 overloaded`。参考 Hermes `Progressive Tool Disclosure`，改用"懒加载"模式。

### `tool_search.py`（新文件，16.2KB）

- **3 个 bridge 工具**（`toolset="tool_search"`）：
  - `tool_search(query)`：按关键字模糊搜工具 name + 描述，返回候选清单
  - `tool_describe(name)`：拿到指定工具的完整 OpenAI function schema
  - `tool_call(name, args)`：直接调工具（不需要先 describe，模型可直调）
- **触发条件**：`agent.tool_search.enabled = true` 且工具总数 > `eager_threshold`（默认 15）
- **向后兼容**：关闭时所有现有路径保持不变；开启时仍允许 LLM 直接调原工具（`registry.dispatch` 兜底），不强制走 bridge
- **Manifest 修剪**：在 system prompt 末尾追加「简洁 manifest」（仅 name + toolset + 一句话描述），按 `manifest_budget_pct` 比例降级 / 裁剪

### `agency_agents.py`（新文件，12.3KB）

- The Agency 的 **282 个 specialist** lazy router，对外暴露 4 个 Spirit 工具（`agency_agents_search` / `inspect` / `load` / `delegate`）
- Specialist 名单从磁盘 JSON 加载一次，内存索引（`_SPIRIT_HOME/data/agency_agents/agents.json`）
- 复刻 Hermes `agency-agents-router` 插件协议（Hermes 用外部插件协议；Spirit 用自注册 via `spirit.tools.registry`）

### `spirit/tools/__init__.py`

注册 `agency_agents` + `tool_search`，启用按需懒加载。

## 三、V3 长期路线图（4 份规划文档）

> **V2 是"能跑"（v0.0.2 → v0.2.2，核心闭环 100%）；V3 是"能信"（让用户敢于把真实任务常驻交给 Spirit）。**

| 文档 | 内容 |
|------|------|
| `docs/14-rsiagent-architecture-and-spirit-gap.md` | RSI/Agent 架构 + Spirit 差距分析（Hermes 16 个候选模块对照表） |
| `docs/15-product-roadmap-v3.md` | V3 产品规划：核心定位 / 用户场景 / 关键能力 / 节奏 |
| `docs/16-feature-roadmap.md` | Feature 路线图（含 **16/16 Hermes 模块覆盖矩阵**——采纳 / 沿用 V2 / 部分采纳 / 显式放弃，每项都有理由） |
| `docs/17-development-plan.md` | V3 开发计划：v0.2.3 → v1.0 迭代节奏、AB 准确率对照、回归测试策略 |

### Hermes 模块覆盖矩阵（16/16 全部显式决策）

| # | Hermes 模块 | 状态 | 决策归属 |
|---|------------|------|---------|
| 1 | `turn_recovery.py` | ✅ 采纳 | R-P1.1（v0.2.3）|
| 2 | `tool_dispatch_helpers.py` | ✅ 采纳（简化）| R-P1.2（v0.2.3）|
| 3 | `turn_truncation.py` | ✅ 采纳 | R-P2.1（v0.3.0）|
| 4 | `moa_loop.py` | 🟢 **沿用 V2** | V2.1 4.1 已移植 |
| 5 | `turn_context.py` | ⚠️ 部分采纳 | R-P3.1 集成 sanitization |
| 6 | `turn_iteration_prep.py` | 🔴 **放弃** | IterationBudget 已覆盖 |
| 7 | `turn_overflow.py` | ⚠️ **部分采纳** | 含于 R-P2.1 / R-P1.5 |
| 8 | `turn_preflight.py` | ⚠️ **部分采纳** | 含于 R-P1.4 / R-P1.5 |
| 9 | `subagent_lifecycle.py` | 🔴 放弃 | IterationBudget 已覆盖 |
| 10 | `moa_alternation.py` | 🟡 改 P3 评估 | M-P3.2（V3.0.0）|
| 11 | `review_engine.py` | 🔴 放弃 | 主动审查与工具流冲突 |
| 12 | `tool_dispatch_helpers.py` | ✅ 采纳（简化）| R-P1.2 |
| 13 | `verify_hooks.py` | 🟡 改 P3 评估 | M-P3.1 |
| 14 | `model_metadata.py` | ⚠️ 部分采纳 | R-P1.4 provider 切换保护 |
| 15 | `system_prompt.py` | ⚠️ 部分采纳 | R-P1.1 turn recovery |
| 16 | `title_generator.py` | ⚠️ 部分采纳 | R-P2.2 会话摘要 |

完整决策理由见 `docs/16-feature-roadmap.md` §6。

## 四、其他改动

- `spirit/agent/agent.py`：Agent 主循环微调（tool_search / agency_agents 集成点）
- `spirit/autonomy/{bridge,explorer,integration}.py`：Autonomy 子模块与新工具桥接
- `spirit/cli/main.py`：CLI 命令补全 / `--tool-search` / `--agency-agents` 开关
- `spirit/desktop/_launcher.py`：桌面端 launcher 适配 Electron 主进程改动
- `spirit/config.py`：新增 `tool_search.*` / `agency_agents.*` 配置节
- `desktop-app/src/components/SystemPanel.vue` + `stores/statusStore.ts`：状态面板新增 tool_search / agency_agents 开关
- `desktop-app/src/App.vue`：`forceCapture` 切换时机调整（保留原语义，bug fix 不重写）
- `.gitignore`：补全 `_tmp_*` / `_pytest_run.py` / `_ver_check.py` / `_pptbuild/` / `_memora_src/` 等临时调试产物 ignore

## 升级注意

- **新增配置项**（均有默认值，无需手动配置）：
  - `agent.tool_search.enabled`（默认 `false`，>15 工具时建议开）
  - `agent.tool_search.eager_threshold`（默认 15）
  - `agent.tool_search.manifest_budget_pct`（默认 5%）
  - `agency_agents.*`（默认走 `~/.spirit/data/agency_agents/agents.json`）
- **新增工具集**（`enabled_toolsets` 默认全开）：
  - `tool_search`（3 个 bridge 工具）
  - `agency_agents`（4 个 router 工具）
- **版本号统一**：spirit / pyproject / desktop-app / vscode-extension 同步升至 0.2.3
- 无破坏性改动；替换代码并**重启桌面端**即可（electron 主进程改动需要重启，渲染层 HMR 不够）

---

发布人：Spirit Agent 维护者 · 发布日期：2026-10-09 · 上一版本：v0.2.2
