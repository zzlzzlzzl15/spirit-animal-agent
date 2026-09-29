# Spirit Agent v0.2.1 更新说明

> 补丁发布：CLI 终端运行可见性、输入修复、状态弹窗数据链路端到端修复、provider 订阅用量查询。

## 一、CLI 终端运行可见性

- 状态栏常驻运行指示：运行时 spinner + 阶段（思考中 / 执行 <tool> / 输出中）+ 本步耗时；空闲显示 `◌ 空闲`。
- 内联 spinner 补齐工具执行与思考之间的空档，任何时刻都能看出 Agent 是否在干活。
- 每步计时：工具完成行带 `✓ <tool> 完成 (X.Xs)`，FIFO 配对 tool_start/tool_complete。

## 二、输入修复

- 修复 Backspace 重绘时折行续行旧文本残留导致的"重复行"：按格数计算物理行号上移光标，`\x1b[J` 清到屏末一次性抹掉整条逻辑行。
- 新增 `cellWidth()`（CJK/全角/emoji = 2 格），统一 Backspace 光标回退与左右方向键移动的格数计算。

## 三、状态弹窗数据链路端到端修复

- 后端新增 `_status_with_agent()` 单一真相源：`get_status` 命令与 `init` 事件推送共用；`running` 不再是"agent 活着就 True"，而是进行中 chat 任务计数（`_active_chats`，try/finally 覆盖全部退出路径）。
- store `syncFromServer` 认顶层扁平 Agent 字段（兼容旧嵌套 `data.agent` 形态）。
- 修复弹窗页把 pinia ref 解包成普通对象的响应式陷阱（改 `storeToRefs`）；每次开窗主动拉 `get_status` + `get_usage` 双保险。
- 会话行不再裸显 session UUID：改为「开始时刻 + 历时」（如 `17:13 起 · 2h13m`）。

## 四、Provider 订阅用量查询（新增）

- 新增 `get_usage` 命令：线程池并行查询各 provider 官方用量接口；60s 缓存挡 init/重连高频调用，弹窗每次打开强制重查。
- **MiniMax Token Plan**：官方 `GET /v1/token_plan/remains`，取 5h 固定窗口条目，弹窗显示「已用 X% · Xh Xm 重置」（附周窗口百分比）。
- **阿里云 Token Plan（sk-sp）**：官方无 key 级查询 API（个人版为月度 Credits 限额、仅控制台可查，OpenAPI 需 AK/SK 签名），弹窗如实标注「月度Credits·控制台查」，不编数字。
- 弹窗额度行按查询结果动态渲染；状态窗口尺寸 220×230 → 240×276 容纳新行。

## 五、其他修复

- Memora 客户端/代理 httpx 加 `trust_env=False`：防止 Windows 下 httpx 读注册表里 Clash/V2Ray 写入的系统代理，把本地 Memora（8000）请求劫持到代理端口导致无法连通。

## 升级注意

- 无配置变更、无破坏性改动；直接替换代码并重启桌面端即可（electron 主进程会重新拉起 Python 后端）。
