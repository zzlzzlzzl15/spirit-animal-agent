# Release notes v0.3.5 — API 调用失败死循环修复 + 统一日志系统

## 用户可见的故障修复

### 1. 长任务后 `[API 调用失败，已重试 3 次]` 死循环（根因）

上下文压缩把 `assistant(tool_calls)` 与其对应的 `tool` 结果切到不同区段，
产生 provider 直接拒绝的请求体（`400 tool result's tool id not found` /
`tool call result does not follow tool call`）。该错误被当成"可重试未知错误"，
同一个非法 payload 被重发 3 次，且坏消息已写入内存 → 同一进程内此后每一轮都
失败，只有重开终端才能恢复。

修复：
- 新增 `spirit/agent/message_pairing.py`：发请求前校验并修复
  tool_call/tool_result 配对（丢弃孤儿 tool 结果、摘除无应答调用）。
- 压缩器出口（`context_compressor.compress`）自动修复配对。
- `prepare_api_messages` 改为按条过滤缺 id 的 tool_call，不再整组丢弃；
  `serialize_tool_call` 在 provider 未给 id 时补占位 id。
- 新增 `FailoverReason.invalid_request`：400 请求体非法不再空转重试，
  先自愈一次，仍失败则立即给出明确错误并提示 `/new`。

### 2. v0.3.4 存储层回归（会话不落库）

schema DDL 误用 SQLite 不支持的 `#` 行注释 → `SessionDB()` 初始化必抛
`unrecognized token: "#"` → 会话/消息全不落库。已改为 `--`，并补 fresh/
reopen 回归测试；顺带修复 `branch_session` 的 SQL 绑定数（14 占位给 13 值）。

### 3. 配置不生效

`get_config_value()` 此前只读内置默认值，`~/.spirit/config.yaml` 里的
`context_length` / `max_retries` / `protect_first_n` / `max_tokens` 等全部
静默失效。现改为读「默认值 ← YAML ← 环境变量」的合并视图；CLI / 桌宠 /
API / gateway / 子代理统一走 `build_agent_config()`。

## 统一日志系统

- 新增 `spirit/logging_setup.py`：终端 + `~/.spirit/logs/agent.log`（INFO
  滚动 10MB×5）+ `errors.log`（仅 ERROR）+ `events.jsonl`（结构化事件）。
  文件 handler 挂在 `spirit` logger，不受思考期间终端静音影响。
- 结构化埋点：`llm_request` / `llm_response` / `api_error`（含异常原文与
  traceback）/ `compression` / `payload_invalid` / `provider_switch` /
  `tool_error` / `storage_error` / `session_end`；正文截断 + 密钥脱敏。
- 查询面：
  - `spirit logs errors [--since 1h] [--reason invalid_request] [--session ...] [--grep ...]`
  - `spirit logs events [-k api_error]`
  - `spirit logs tail errors.log`
  - `spirit logs doctor`（配置生效值与来源、存储健康、日志、配对自检）
  - 交互内 `/logs`、`/logs doctor`。

## 测试

新增 `test_message_pairing.py`（含压缩配对不变量矩阵）、
`test_context_compressor_pairing`、`test_schema_init.py`、
`test_logging_setup.py`、`test_logs_commands.py`，并扩展 error_handler
400 分类用例。

## 注意

排查过程中发现两个后端的现象（**与本次代码修复无关**，请勿混淆）：

- `api.minimaxi.com` 曾短暂返回 `402 已达到 Token Plan 用量上限`。这是 Token Plan
  的**滚动用量窗口**被压测打满所致（诊断时发送过 80 万 token 的超大请求），
  与账户积分余额无关；窗口滚动后即恢复 200。可用模型：
  `MiniMax-M3` / `MiniMax-M3.1-Flash` / `MiniMax-M2.7` / `MiniMax-M2.5` / `MiniMax-M2.1`。
- 火山 ark 的 `.../api/plan/v3` 端点对所有模型返回
  `404 该模型不支持 agent plan 功能` —— 该 key 鉴权正常（报 404 而非 401），
  但 `minimax-m3` 不在其 plan 覆盖的模型列表里，需要换成 ark 控制台该 plan
  实际支持的模型 ID。

（排查细节见提交信息与 `docs/`；本节只记录结论。）
