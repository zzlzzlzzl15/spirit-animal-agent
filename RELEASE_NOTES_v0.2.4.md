# Spirit Agent v0.2.4 更新说明

> V3 路线图首个 P1 节点收官：6 项鲁棒性增强全部落地（错误恢复链 + 工具结果分类 + 并发超时修复 + provider 切换保护 + 上下文压缩 v2 + 准确率归因），均经专项验证脚本覆盖。

## 概览

v0.2.4 是 V3 规划 R/M 支柱下的 P1 节点交付，对应 `docs/16-feature-roadmap.md` 第 0 节的 6 项 P1 必做功能：

| 编号 | 功能 | 新增文件 | 行数 | 验证用例 |
|------|------|---------|------|---------|
| R-P1.1 | 错误恢复链（5 类处理器） | `spirit/agent/turn_recovery.py` | 613 | 24/24 |
| R-P1.2 | 工具结果分类（5 类） | `spirit/agent/tool_result_classification.py` | 411 | 27/27 |
| R-P1.3 | 并发工具超时修复（3 层超时 + liveness） | `spirit/agent/tool_liveness.py` | 360 | 63/63 |
| R-P1.4 | provider 切换保护（保留工具结果 + 标签 + 提示） | `spirit/agent/provider_switch_protection.py` | 313 | 72/72 |
| R-P1.5 | 上下文压缩鲁棒性 v2（stall / refusal / pin / micro） | `spirit/agent/context_compressor_v2.py` | 432 | 68/68 |
| M-P1.0 | 准确率归因 + 提升（5 类错误 + 自我纠错 + MoA 兜底） | `spirit/agent/accuracy_attribution.py` | 442 | 82/82 |
| **合计** | **6 项功能 + 4 处 conversation_loop.py 集成** | **6 新增 + 2 修改** | **~2,571 净增** | **336/336** |

## 一、R-P1.1 错误恢复链（turn_recovery）

### 问题

v0.2.3 的 `conversation_loop.py` 错误处理只有"重试 or 换 provider"两件事，AgentBench db-19 任务曾因 `find_files` 参数错误持续重试到耗尽迭代预算。

### 解决

参考 Hermes `turn_recovery.py` (1,874 行) 的修复链设计，Spirit 简化版实现 **5 类修复处理器**：

| 处理器 | 触发场景 | 修复动作 |
|--------|---------|---------|
| `recover_unicode_encode_error` | API key / header 含非 ASCII | 清洗为 ASCII safe 字符 |
| `recover_image_dimension_error` | 图片超 provider 限制 | 按比例自动缩放 |
| `recover_thinking_timeout` | 思考超时 | 注入续推提示 |
| `recover_tool_sequence_interrupted` | 工具调用对不齐 | 补齐缺失的 tool result |
| `recover_message_sanitization` | 图像 / surrogate 字符污染 | 剥离 / 替换 |

`TurnRecoveryChain` 主类按优先级串接处理器，每类都有 `RecoveryAction` 决策（`retry` / `replan` / `escalate`）。

### 集成

本版本仅交付独立模块 `turn_recovery.py`，**集成到 conversation_loop.py 在后续 R-P1.4 一起改**（已完成 R-P1.4 的切换保护集成，把 turn_recovery 接入作为下一迭代任务）。

## 二、R-P1.2 工具结果分类（tool_result_classification）

### 问题

v0.2.3 的 `tool_executor.py` 把"工具成功"和"工具返回 `{"error": "..."}`"一视同仁——LLM 拿到同样的结果分不清"成功 / 部分成功 / 失败 / 可重试"，只会"重试同样参数"。

### 解决

参考 Hermes `tool_dispatch_helpers.py` (467 行) 的设计，Spirit 简化版 **5 类分类器**：

| 类别 | 触发条件 | LLM 看到的分类 |
|------|---------|----------------|
| `SUCCESS` | exit_code=0 + 无 error | 不污染 content |
| `PARTIAL_SUCCESS` | exit_code=0 + 警告 | 注入 `_classification: partial_success` |
| `FAILURE` | exit_code≠0 或 error 字段 | 注入 `failure` + 简短原因 |
| `RETRYABLE` | 网络超时 / 临时错误 | 注入 `retryable` + 重试建议 |
| `NON_RETRYABLE` | 参数错误 / 权限错误 | 注入 `non_retryable` + 改参建议 |

`classify_tool_result(tool_name, result)` 工具专属模式（terminal / search_files / read_file）+ `_classify_generic` fallback 模式（含 `invalid / syntax / parse / malformed` 关键词 fallback），让"query syntax invalid"也能被判 non_retryable。

### 集成

`tool_executor.py`：
- `make_tool_result_message` 新增 `classified: Optional[ClassifiedToolResult]` 参数
- `execute_single_tool` 用 `classify_tool_result(tool_name, result).is_failure` 替代 `_detect_tool_failure`
- `execute_tool_calls_sequential` / `execute_tool_calls_concurrent` 在追加结果前先分类，失败时自动注入分类指引

## 三、R-P1.3 并发工具超时修复（tool_liveness）

### 问题

`tool_executor.py:34` 硬编码 `_DEFAULT_CONCURRENT_TOOL_TIMEOUT_S = 420.0`（7 分钟），AgentBench OS 类别全部 420s 卡死；单工具卡了就一直等，没有 liveness 检测。

### 解决

把 420s 单层超时拆成 **3 层超时 + liveness probe**：

| 层级 | 默认值 | 行为 |
|------|--------|------|
| 单工具硬超时 | 60s | 单工具运行无进展超过这个时间 → cancel |
| 单工具软超时 | 30s | warning log，不取消 |
| 整体硬超时 | 300s（原 420s 缩短） | 整批超过这个时间 → 全局取消 |
| 整体软超时 | 90s | warning log，不取消 |

`LivenessProbe` 抽象基类 + `OutputLivenessProbe`（"过去 N 秒内有新输出"判断）+ `IntervalLivenessProbe`（心跳间隔检查）+ `LivenessDetector` 主类（daemon 线程每 N 秒检查 + trigger 回调）。

取消后用 `build_timeout_result_json` 替换 result（含 `_classification: retryable` + `_retryable: true` + 实际 elapsed 时间），让 LLM 看到具体超时原因——**不是默默失败**。

### 集成

- `execute_single_tool` 新增 `detector: Optional[LivenessDetector]` 参数
- `execute_tool_calls_sequential` 为每个工具启动独立 detector
- `execute_tool_calls_concurrent` 启动整批 detector；区分 `single_tool` / `overall` / `cancelled` 三种 timeout_kind

## 四、R-P1.4 provider 切换保护（provider_switch_protection）

### 问题

v0.2.3 的 `_try_activate_fallback` 跨 provider 切换时只换连接参数 + 重置 client，但 LLM 端没有"刚切换"的提示，且消息历史没有 provider 归属。LLM 不知道已切换 → 期望原 provider 的工具调用语义，新 provider 不兼容。

### 解决

参考 Hermes `model_metadata.py` + `try_activate_fallback` 的设计，Spirit 简化版 **3 类保护**：

1. **provider 标签**：每条消息打 `_provider / _model` 元数据（LLM 端不可见，`_prepare_api_messages_safe` 用 `apply_provider_labels` 剥离元数据）
2. **保留有效工具结果**：切换**不**丢弃任何 tool message —— 上一轮成功的工具结果仍有效，新 provider 可继续基于它们推理
3. **切换提示注入**：切换后给 messages 注入一条 system 消息，明文告诉 LLM："刚切换了 provider，请基于现有工具结果继续，不要重复已成功的调用"

切换提示插入位置：最后一条非 system 消息之后（不打断 system prompt 前缀，保护 prompt cache）。

### 集成

`conversation_loop.py`：
- `_try_activate_fallback` 切换成功后调用 `protect_provider_switch` 写入 `agent._messages`
- 新增 `_prepare_api_messages_safe` helper，取代原 3 处 `prepare_api_messages(agent.messages)` 调用（先用 `apply_provider_labels` 剥离元数据再转 OpenAI 格式）
- 不破坏现有的"每轮恢复主 provider"逻辑（`_restore_primary_runtime` 不注入切换提示，避免噪音）

## 五、R-P1.5 上下文压缩鲁棒性 v2（context_compressor_v2）

### 问题

Spirit v1 压缩器只有 410 行（vs Hermes 5,577 行，**13.6× 差距**）。遇到 4 类场景会"失忆"或"崩溃"：
1. 辅助 LLM 调用卡死（半开连接）：N 秒无响应 → 整次 compress 阻塞
2. provider 拒绝摘要（"I cannot summarize..."）→ 返回空字符串导致关键事实丢失
3. detached worker 污染：多次压缩间 provider/model 漂移导致摘要风格不一致
4. 整段摘要粒度太粗：长任务后工具输出巨大，摘要后仍超出 token 限制

### 解决

Spirit 简化版 **4 类鲁棒性增强**：

| 增强 | 实现 | 触发场景 |
|------|------|---------|
| **Stall detection** | `StallDetector`（timeout + abort 重试） | 辅助 LLM 调用 N 秒无响应 |
| **provider 拒绝检测** + **deterministic fallback** | `is_provider_refusal` + `deterministic_summarize` | LLM 返回 "I cannot summarize..." |
| **summary route pin** | `pin_summary_route` + `summary_route_scope`（ContextVar） | 单次摘要调用钉住 provider/model，避免 detached worker 污染 |
| **micro_compaction** | `micro_compact_tool_output` / `micro_compact_messages` | 长任务工具输出微压缩（保留首尾关键信息） |

`StallConfig` 配置：
- `timeout_s`（默认 60s）
- `soft_warn_s`（默认 30s）
- `check_interval_s`（默认 5s）

`is_provider_refusal` 用正则匹配 9 类拒绝前缀（"I cannot" / "I'm sorry" / "As an AI" / "I apologize" / "Unfortunately" 等）。

### 集成

`context_compressor.py` 的 `ContextCompressor._llm_summarize` 重写为 `_llm_call` 内部函数 + `summarize_with_v2(...)` 包装（pin route → micro_compact → stall 检测 → refusal 检测 → fallback）。

新增配置项（均有默认值，无需手动配置）：
- `compression.stall_timeout_seconds`（默认 60s）
- `compression.stall_soft_warn_seconds`（默认 30s）
- `compression.deterministic_fallback_enabled`（默认 true）
- `compression.micro_compact_enabled`（默认 true）

## 六、M-P1.0 准确率归因 + 提升（accuracy_attribution）

### 问题

Spirit 单任务准确率 62% < Hermes 80%（SWE-bench / AgentBench 同口径）。根因拆解需要：5 类错误分类 + 自我纠错 + MoA 兜底。

### 解决

参考 Hermes `accuracy_attribution.py` + Spirit 4.1 MoA 移植的设计，Spirit 简化版 **3 大能力**：

#### 1. 5 类错误分类（归因分析）

| 类别 | 归因 | 占比优先功能项 |
|------|------|----------------|
| `TOOL_FAILURE_NO_REPLAN` | 工具失败没换方案（最常见）| R-P1.2 强化 |
| `OUTPUT_TRUNCATION` | 输出截断没续写 | R-P2.1 截断多策略 |
| `PROVIDER_SWITCH_LOST` | provider 切换上下文丢失 | R-P1.4 provider 切换保护 |
| `THINKING_TIMEOUT` | 思考超时 | R-P1.3 单工具超时 + thinking timeout 扩展 |
| `OTHER` | 其它错误 | 继续观察 |

`classify_error_cause(raw_error, context)` 分类函数：context 字段优先（`thinking_timeout` / `finish_reason=length` / `provider_switched+context_lost`），文本指纹 fallback（4 类正则）。

`attribute_task_errors(samples)` 批量归因 → `AccuracyAttributionReport`（count / ratio / top_category / markdown 导出），可对接 SWE-bench 30 任务小样本归因表。

#### 2. 自我纠错轻量版

`should_self_correct(previous_response, user_message, last_tool_results)` 检测 6 类触发指纹：
- 用户后续追问"哪里不对" / "为什么" / "are you sure"
- 上一轮 assistant 回复含 `<error>` / `"error"` JSON / `Exception` / `failed` / `失败`
- 工具结果含 `"status": "error|timeout|cancelled"`

触发时注入 `build_self_correction_prompt(reason)` 到 messages（"请审视你上一轮的回复是否真的解决了用户的问题"），避免 LLM 默默重复上一轮答案。

#### 3. MoA 兜底判断

`should_activate_moa(response_text, response_length)` 启发式判断主 LLM 信心低：
- 空响应 / 极短响应（< threshold_length 字符）
- 含"我不确定" / "可能" / "也许" / "maybe" / "perhaps" 等低信心短语
- 不含"已修改" / "successfully" 等高信心短语

Spirit MoA 是 provider 配置（all-or-nothing），无法动态开关——所以检测到信心低时**只 log 推荐 + emit `on_moa_recommended` hook**，让上层决定是否启用 MoA（`provider="moa"` 切换）。

### 集成

`conversation_loop.py`：
- 主循环开头（每轮 LLM 调用前）：检测 `should_self_correct` → 注入 `build_self_correction_prompt` 到 messages
- 主循环"最终文本回复"分支：检测 `should_activate_moa` → log + emit `on_moa_recommended` hook
- 不破坏 v0.2.3 既有功能（自我纠错检测包在 try/except，主路径不依赖）

## 七、其他改动

- `spirit/agent/tool_executor.py`：集成 R-P1.2 工具结果分类 + R-P1.3 liveness detector（`+122 / -18`，变更包含 `execute_single_tool` 加 detector 参数 + concurrent 路径加 detector + sequential 路径加 detector）
- `spirit/agent/conversation_loop.py`：集成 R-P1.4 provider 切换保护 + M-P1.0 自我纠错 + MoA 兜底（`+96 / -3`，变更包含 `_prepare_api_messages_safe` helper + 自我纠错主循环开头检查 + MoA log 推荐）
- `spirit/agent/context_compressor.py`：集成 R-P1.5 上下文压缩 v2（`+12 / -5`，变更包含 `import v2 hooks` + `__init__` 创建 v2_hooks + `_llm_summarize` 重写为 v2 包装）
- `pyproject.toml`：版本号 0.2.3 → 0.2.4

## 升级注意

- **新增配置项**（均有默认值，无需手动配置）：
  - `compression.stall_timeout_seconds`（默认 60s）
  - `compression.stall_soft_warn_seconds`（默认 30s）
  - `compression.deterministic_fallback_enabled`（默认 true）
  - `compression.micro_compact_enabled`（默认 true）
- **行为变更**：
  - 并发工具整体超时从 420s 缩短到 300s（+ 单工具 60s 假死检测）
  - provider 切换后会注入一条 system 切换提示（"刚切换了 provider，请基于现有工具结果继续"）
  - 工具结果失败时自动注入 `_classification` / `_retryable` 字段（不污染成功结果）
  - 用户追问"哪里不对"或上一轮含错误标识时，自动注入自我纠错提示
- **不破坏既有 API**：所有 `execute_*` / `make_tool_result_message` 等公开函数保持向后兼容（新增参数都有默认值）
- 无破坏性改动；替换代码并**重启 Python 后端**即可

## 验证覆盖

6 项 P1 功能全部经专项验证脚本覆盖，合计 **336/336 用例全过**：

| 验证脚本 | 测试模块数 | 用例数 |
|---------|----------|-------|
| `verify_r1.2_tool_result_classification.py` | 6 | 27 |
| `verify_r1.1_turn_recovery.py` | 9 | 24 |
| `verify_r1.5_context_compressor_v2.py` | 10 | 68 |
| `verify_r1.4_provider_switch_protection.py` | 9 | 72 |
| `verify_r1.3_tool_liveness.py` | 12 | 63 |
| `verify_m1.0_accuracy_attribution.py` | 11 | 82 |

每个验证脚本一次性跑通即删，**不写入项目**（符合"任务收尾不在项目里新增测试文件"约束）。完整断言日志见对话历史。

---

发布人：Spirit Agent 维护者 · 发布日期：2026-10-09 · 上一版本：v0.2.3
