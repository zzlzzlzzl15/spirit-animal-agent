# Spirit Agent v0.2.2 更新说明

> 补丁发布：委派子任务容错与实时视图、流式停滞看门狗、CLI 静默轮根治、会话持久化与恢复、桌宠点击穿透修复、Memora 记忆自愈。

## 一、委派子任务：容错自动重启 + 实时视图

- **停滞监测与自动重启**：子 Agent 轮间空闲超 `child_stale_idle_seconds`（默认 450s）、或卡同一工具超 `child_stale_in_tool_seconds`（默认 1200s）判为停滞，杀当前尝试并自动重启；单次尝试墙钟上限由 300s 提升到 `delegate_default`（默认 900s）。每个子任务最多 `child_max_attempts`（默认 2）次尝试，耗尽才报失败。
- **进度上报**：新增 delegate 进度 sink，子任务的工具启动/完成、思考、流式增量实时广播到前端；未完成任务可追踪（`get_unfinished_delegate_tasks`），必要时中断在跑子任务（`interrupt_live_children`）。
- **CLI 实时视图**：子 Agent 工具/思考以带名称前缀的树状行流式打进 scrollback，底部固定状态块每子 Agent 一行（原地重绘、无残影）；`Ctrl+D` 收起为仅状态块，`dlog` 回看全程。设计参考 Hermes delegate_tool 的 print_above 与 Claude Code 的 condensed/transcript 双档密度。

## 二、流式停滞看门狗

- **首字节看门狗** `stream_ttfb_seconds`（默认 90s）：发起请求后 N 秒无任何 chunk → 杀流重试。
- **chunk 间隔看门狗** `stream_chunk_stale_seconds`（默认 120s）：流中途 N 秒无新 chunk → 判流死亡杀流重试。
- 两者均可设 0 禁用；解决 provider 半开连接导致的流式无限阻塞。

## 三、CLI 静默轮根治

- 过程通知（fallback 切换等）改走独立的 `stream_notice` 通道，**不计入** streamBuffer：此前失败轮由 `chat_complete` 携带的错误结论兜底渲染，会被“已流式送达”判定吞掉，导致整轮对用户完全静默。现在过程通知可见、错误结论也可见，静默事故根因消除。
- 超时续跑边界修复：续跑轮清空工具计时 FIFO（`pendingToolStarts`），防止工具完成行配对到上一轮的起始时间戳（曾出现 list_dir 1356s 假耗时）。

## 四、会话持久化与恢复

- 后端新增增量持久化 hook：会话进行中增量落库、结束时补全（`_install_incremental_persist_hook` / `_persist_session`）。
- 新增 `list_sessions` / `resume_session` 命令；CLI `/tasks` 展示最近任务会话，点击编号或输入 `[n]` 即可恢复并继续；列表拉取失败不打扰输入。

## 五、桌宠点击穿透修复

- **根因**：Windows 透明窗口按整个窗口矩形命中，狐狸四周的透明边距会整块吞掉屏幕点击；`setIgnoreMouseEvents(forward:true)` 在 Electron 29 本环境下转发不生效（穿透态渲染层收不到 mousemove）。
- **修复**：改由主进程轮询光标位置——落在狐狸可见包围盒（按 spritesheet alpha 实测 (28,6)-(178,202)）内则收回捕获（可点可拖），否则 OS 层穿透（点击直达下层窗口）；菜单/面板展开或拖拽期间整窗强制收回捕获（`setForceCapture`）。
- 越界保护与滚轮缩放同界，不再钳制用户保存的缩放上限（放大后透明边距随之变大的问题一并解决）；气泡等纯展示窗口完全点击穿透。

## 六、Memora 记忆自愈

- 自动保存后新增验证与自愈：`_schedule_verify` 异步核对落库结果，失败自动 `reprocess_document` 重处理；文档状态可查（`get_document_status`），待验证队列可观测（`get_pending_verify`）。

## 七、其他

- provider 配额冷却标记（`_mark_primary_quota_cooldown`）：主 provider 触发配额上限时进入冷却，避免反复撞墙。

## 升级注意

- **新增配置项**（均有默认值，无需手动配置）：`timeouts.stream_ttfb_seconds`、`timeouts.stream_chunk_stale_seconds`、`delegation.*`；`timeouts.delegate_default` 默认值 300 → 900。
- 无破坏性改动；替换代码并重启桌面端即可（electron 主进程会重新拉起 Python 后端）。
