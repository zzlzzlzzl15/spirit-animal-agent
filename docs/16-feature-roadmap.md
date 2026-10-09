# 16 · Spirit Agent V3 功能规划

> **目标读者**：Spirit 维护者、贡献者、PM
> **生效版本**：V0.2.3 → V3
> **状态**：草案 v1 · 2026-10-09
> **前置文档**：[15-product-roadmap-v3.md](./15-product-roadmap-v3.md)（V3 产品规划）
> **下一份文档**：[17-development-plan.md](./17-development-plan.md)（V3 开发规划）

---

## 0. 功能规划总览

V3 的所有功能按三大战略支柱分组（P1/P2/P3 = 优先级）：

| 支柱 | 战略目标 | P1（必做，v0.2.3-v0.4.0） | P2（应该做，v0.3.0-v0.5.0） | P3（可探索，V3.0.0） |
|------|---------|---------------------------|----------------------|----------------------|
| **R · 可靠性** | 不再卡死 | R-P1.1 错误恢复链 / R-P1.2 工具结果分类 / R-P1.3 并发工具超时 / R-P1.4 provider 切换保护 / R-P1.5 上下文压缩鲁棒性 v2 / M-P1.0 准确率归因 + 提升 | R-P2.1 截断多策略 / R-P2.2 活性检测 / R-P2.3 provider 切换保护 / R-P2.4 动态 prompt 组成 | R-P3.1 消息清理 / R-P3.2 敏感信息脱敏 |
| **M · 可量化** | 自进化有数字 | M-P1.1 BRS 接线 | M-P1.2 成效 harness / M-P1.3 领域验证器扩充 / M-P2.1 日报 / M-P2.2 经验确认 / M-P2.3 wave 可视化 | M-P3.1 成效 regression suite / M-P3.2 strict-alternation 适配评估 |
| **C · 桌面伴侣** | 桌宠会盯任务 | C-P1.1 进度推送 / C-P1.2 HITL 面板 | C-P2.1 历史回看 / C-P2.2 结束摘要 / C-P2.3 状态可视化 | C-P3.1 多 session PoC / C-P3.2 经验导入导出 / C-P3.3 任务调度 |

**功能总量**：P1 = 8 项 / P2 = 9 项 / P3 = 7 项 = **共 24 项**（M-P1.0 准确率归因 + 提升跨 R/M 支柱归在 P1）

**关键原则**：
- **P1 必做**：不完成不发 v0.3.0
- **P2 应该做**：v0.4.0 内尽量完成，缺的要在 v0.5.0 收尾
- **P3 可探索**：V3 末或 V4 初评估，**不阻塞版本发布**

---

## 1. 支柱 R：可靠性（Reliability）

> **支柱战略目标**：让 Agent 在"工具失败 / 输出溢出 / 思考超时 / provider 切换"等异常路径上不卡死。
> **架构依据**：`docs/14-rsiagent-architecture-and-spirit-gap.md` 与上一轮 Spirit vs Hermes 源码对比。

### R-P1.1 错误恢复链（turn_recovery）

**问题**：
- 当前 Spirit `conversation_loop.py` 错误处理只有"重试 or 换 provider"两件事
- AgentBench db-19 任务：`find_files 参数错误` 持续重试到耗尽迭代预算（致命）
- Hermes 的 `turn_recovery.py` 1874 行有一整条修复链：Unicode 编码 / 图像尺寸 / 思考超时 / 工具序列中断 / 消息清理

**功能**：
- 新增 `spirit/agent/turn_recovery.py`（参考 Hermes 1874 行，**Spirit 简化版 ~400-600 行**）
- 至少实现 5 类修复处理器：
  1. `recover_unicode_encode_error`：清洗 API key / header 非 ASCII（参考 Hermes #6843）
  2. `recover_image_dimension_error`：按 provider 限制自动裁剪图片
  3. `recover_thinking_timeout`：思考超时注入续推提示
  4. `recover_tool_sequence_interrupted`：补齐中断的工具调用对
  5. `recover_message_sanitization`：剥离图像 / surrogate 字符

**验收**：
- AgentBench db 类别工具失败循环次数 ≤2 次/任务
- 单元测试覆盖 5 类处理器
- 不破坏现有 conversation_loop.py 的 fallback 切换逻辑

**估算**：中等（5 个处理器 + 集成 + 测试，约 2 周）

### R-P1.2 工具结果分类（tool_result_classification）

**问题**：
- 当前 Spirit `tool_executor.py` 把"工具成功"和"工具返回 `{"error": "..."}`"一视同仁
- LLM 拿到同样的结果，分不清"成功 / 部分成功 / 失败 / 可重试"
- 后果：LLM 重复失败的工具调用，无法"换方案"

**功能**：
- 新增 `spirit/agent/tool_result_classification.py`（参考 Hermes 44 行 + tool_dispatch_helpers.py 467 行，**Spirit 简化版 ~150-250 行**）
- 工具结果分类器：
  - 成功（exit_code=0 + 无 error 字段）
  - 部分成功（exit_code=0 + 警告）
  - 失败（exit_code≠0 或 error 字段）
  - 可重试（网络超时、临时错误）
  - 不可重试（参数错误、权限错误）
- 工具调用护栏改造：失败时返回分类结果而非原始字符串

**验收**：
- AgentBench db-19 任务不再循环卡死
- 单元测试覆盖 5 类分类
- 工具调用后 LLM 能看到分类（"这次失败是参数错误，请改参数"）

**估算**：小（约 1 周）

### R-P1.3 并发工具超时修复

**问题**：
- `spirit/agent/tool_executor.py:34` 硬编码 `_DEFAULT_CONCURRENT_TOOL_TIMEOUT_S = 420.0`（7 分钟）
- AgentBench OS 类别全部 420s 卡死
- 没有 liveness 检测：单个工具卡了就一直等

**功能**：
- 把 420s 拆成"软超时 90s + 硬超时 300s + 单工具超时 60s"三层
- 新增 liveness 检测：单工具运行 N 秒无进展（无 stdout/stderr / 无文件变化）→ 提前取消
- 取消后返回分类结果（"工具超时已取消"），不是默默失败

**验收**：
- OS 类别 420s 超时事故率从 100% 降到 0%
- 单元测试：liveness 检测能识别"假死"工具
- 现有 SPIRIT 工具全部能跑（不退化）

**估算**：小（约 1 周）

### R-P1.4 fallback 切换后消息连续性

**问题**：
- 当前 `conversation_loop.py:318-332` 跨 provider 切换时**清空 retry_count 重新计数**
- 但 `api_messages` 重新构建，可能丢失"刚才那次响应的内容"
- 切换后再 LLM 看到的是不一致的上下文

**功能**：
- 切换 provider 时保留上一轮的有效工具结果（只丢弃未消费的 token）
- 消息历史做"provider 标签"，让 LLM 知道"这段是 provider A 回答的，那段是 provider B 回答的"
- 切换时给 LLM 注入"系统提示：刚才切换了 provider，请基于现有工具结果继续"

**验收**：
- SWE-bench Spirit 准确率从 62% 提升到 ≥68%
- 单元测试：跨 provider 切换后消息序列完整
- 不破坏现有的"每轮恢复主 provider"逻辑

**估算**：小（约 1 周）

### R-P1.5 上下文压缩鲁棒性 v2（context_compressor v2）

**问题**：
- Spirit `spirit/agent/context_compressor.py` 只有 410 行（vs Hermes `context_compressor.py` 5,577 行，**13.6× 差距**）
- Spirit 模块头注释明说"参考 Hermes 的 context_compressor.py (3522行)"——只是简化版
- 缺失的关键能力（上一轮架构对比确认）：
  - `stall detection`（辅助模型卡死的进度感知超时，参考 #78981）
  - `DETERMINISTIC_SUMMARY_ROUTE`（provider 拒绝回答时降级到确定性模板摘要）
  - `_SUMMARY_REFUSAL_PREFIX_RE`（检测 provider 拒绝 + 重新调度）
  - `_SUMMARY_ROUTE_PIN`（ContextVar 钉住单次调用的 provider/model，防止 stalled worker 污染）
  - `micro_compaction` mixin（细粒度微压缩，不只"整段摘要"）
  - `redact_sensitive_text` 集成
- AgentBench 实测：Spirit 长任务后"失忆"；SWE-bench 长 trajectory 处理不佳
- 决策 V3-D1 已确定要补这个差距，补充为明确 P1 项

**功能**：
- 扩展现有 `spirit/agent/context_compressor.py`（**Spirit 简化版 ~700-900 行**，保留原 4 类压缩策略 + 加 4 类鲁棒性）
- 至少实现 4 类鲁棒性增强：
  1. **stall detection**（进度感知超时）：辅助 LLM 调用 N 秒无 chunk → abort 重试（pin 路由到 fallback chain）
  2. **provider 拒绝检测** + **deterministic fallback**：LLM 返回 refusal（"I cannot summarize..."）→ 降级到模板化摘要（不报错）
  3. **summary route pin**（ContextVar）：单次摘要调用钉住 provider/model，避免 detached stalled worker 污染后续迭代
  4. **micro_compaction**：不只"整段摘要"，针对工具输出 / 用户消息 / assistant 思考的细粒度微压缩
- 保留原有 `protect_first_n` / `protect_last_n` / `summary_target_ratio` 配置接口
- 新增配置：`compression.stall_timeout_seconds`（默认 60s）/ `compression.deterministic_fallback_enabled`（默认 true）

**验收**：
- AgentBench 长任务（>20 轮工具调用）后不"失忆"（关键事实保留率 ≥90%）
- SWE-bench 长 trajectory 准确率从 62% 提升到 ≥65%
- 单元测试覆盖 4 类鲁棒性增强
- stall 后能优雅降级到 deterministic fallback（不是崩溃）
- 不破坏现有 4 类压缩策略（剪枝 / 头尾保护 / 摘要 / 迭代合并）
- 不破坏现有 2,858 项既有测试

**估算**：中（约 2 周）

### M-P1.0 准确率归因 + 提升（accuracy-attribution-and-boost）

**问题**：
- **Spirit 单任务准确率 62% < Hermes 80%**（SWE-bench / AgentBench 两个 benchmark 同口径）
- 根因拆解（上一轮架构对比）：
  1. **多轮自我修正缺失**：Hermes 的 review_engine + verify_hooks 允许主 LLM 看到自己上轮结果后"自我纠错"，Spirit 无此机制
  2. **结果分类驱动换方案缺失**：Spirit 拿到工具失败不知道"换方案"，只会"重试同样参数"
  3. **MoA 多模型参考未启用**：Spirit 已有 MoA facade（V2 已做），但日常任务没用上
- 决策 V3-D1 已放弃 subagent / review_engine / verify_hooks——这条路径**不通**
- 准确率提升需要靠"补结果分类 + MoA 启用 + 自我纠错混合策略"组合拳

**功能**（两阶段）：
- **阶段 1：归因分析（1 周，P1 必做）**
  - 跑 30 任务 SWE-bench 小样本，分类每条错误的原因（5 类）：
    - 工具失败没换方案 / 输出截断没续写 / provider 切换上下文丢失 / 思考超时 / 其它
  - 输出"准确率损失归因表"：每类损失占比 → 决定后续功能项的具体目标
- **阶段 2：按归因结果补能力（1 周，P1 必做）**
  - 如果"工具失败没换方案"是主因 → 强化 R-P1.2 工具结果分类（提升 LLM 看到的错误信息密度）
  - 如果"provider 切换上下文丢失"是主因 → 强化 R-P1.4 provider 切换保护
  - 如果"截断"是主因 → 强化 R-P2.1 截断多策略
  - 启用 MoA facade（已有）作为可选增强：当主 LLM 信心低时调一次参考模型
  - 增加"自我纠错轻量版"：上一轮结果如果"看起来不对"（如包含 `<error>` / `failed` / 用户后续追问"哪里不对"）→ 注入"请审视你上一轮的答案是否正确"提示

**验收**：
- 阶段 1：归因表覆盖 30 任务，5 类错误占比清晰
- 阶段 2：SWE-bench Spirit 准确率从 62% 提升到 ≥72%（+10pp）
- AgentBench kg/db 准确率对应提升
- 单元测试覆盖归因分类器
- 不破坏 v0.2.3 既有功能

**估算**：中（约 2 周，2 阶段各 1 周）

### R-P2.1 截断多策略（turn_truncation）

**问题**：
- 当前 Spirit 截断处理只有"再推一轮"（`truncation_continuations < _MAX_TRUNCATION_CONTINUATIONS`）
- Hermes `turn_truncation.py` 605 行有多策略：截断前手动缩短 / 切到 thinking 关闭模式 / 切到 fallback provider / 注入续写 hint

**功能**：
- 新增 `spirit/agent/turn_truncation.py`（参考 Hermes 605 行，**Spirit 简化版 ~150-250 行**）
- 至少实现 3 类策略：
  1. 自动缩短（truncate 上文工具输出）
  2. 切到 thinking=disabled 模式
  3. 切到 fallback provider

**验收**：
- 思考模型（thinking=on）的截断事故从单策略提升到 3 策略
- 单元测试覆盖 3 类策略
- 不破坏现有"再推一轮"逻辑

**估算**：中（约 1.5 周）

### R-P2.2 活性检测（turn_liveness）

**问题**：
- 流式输出中途 provider 可能"半开"（socket 不报错但不返回）
- v0.2.2 已加 `stream_ttfb_seconds` / `stream_chunk_stale_seconds` 看门狗，**但只覆盖流式路径**
- 非流式 / 工具执行 / 后台进程没覆盖

**功能**：
- 新增 `spirit/agent/turn_liveness.py`（参考 Hermes 176 行，**Spirit 简化版 ~80-120 行**）
- 抽象 LivenessProbe 接口：每个长操作可挂 probe
- 内置实现：
  - 流式 LLM 响应（沿用 v0.2.2 看门狗）
  - 工具执行（stdout/stderr 进度）
  - 后台进程（PID 存在 + 资源使用率）

**验收**：
- 所有长操作都有 liveness probe
- liveness 失败时优雅取消 + 返回分类结果
- 单元测试覆盖 3 类 probe

**估算**：小（约 1 周）

### R-P2.3 provider 切换保护

**问题**：
- 当前 `conversation_loop.py:302-332` provider 切换时直接换，可能导致 LLM 看到"半切换状态"
- 切换后第一次 LLM 响应可能因 prompt cache 失效而爆炸

**功能**：
- provider 切换时清空 prompt cache 标记，强制首次 LLM 调用重新建立 cache
- 切换前 1 轮给 LLM 注入"准备切换"提示，让它把思考收尾

**验收**：
- provider 切换成功率 ≥95%
- 单元测试：切换后第一次 LLM 调用正常
- 不破坏现有 fallback 链

**估算**：小（约 0.5 周）

### R-P2.4 动态 prompt 组成（dynamic prompt composition）

**问题**：
- Spirit `spirit/agent/prompt_builder.py` 只有 452 行（vs Hermes `prompt_builder.py` 2,044 行 + `system_prompt.py` 593 行，**4.8× 差距**）
- 当前实现：所有 prompt 块都是**模块级常量字符串**（`DEFAULT_AGENT_IDENTITY` / `TOOL_USE_GUIDANCE` / `TASK_WORKFLOW_RULES` 等）
- 缺失的能力：
  - **按模型动态调整**：Anthropic Claude / OpenAI GPT / 本地 Qwen / MiniMax 不同模型的最佳 prompt 策略不同
  - **按任务类型调整**：代码任务 / 问答任务 / 自主探索任务 / VSCode 任务 / 桌宠任务——prompt 侧重点不同
  - **按上下文动态增减**：长对话后期减少 volatile 块、早期增加 context 块
  - **prompt 性能分析**：cache hit rate / 命中率指标
- AgentBench 实测：Spirit 换 provider 时 prompt 质量不一致（MiniMax vs Claude 提示策略可能冲突）
- SWE-bench 实测：Spirit 平均 20.1s（快 3×）但准确率低——可能与 prompt 静态化有关（不快不慢才合理）

**功能**：
- 重构现有 `spirit/agent/prompt_builder.py`（**Spirit 简化版 ~700-900 行**）
- 至少实现 4 类动态调整：
  1. **模型特定模板**：按 `provider` + `model` 选择 prompt 块（Claude 加 `<thinking>` 引导 / GPT 加简洁指令 / 本地模型加安全护栏）
  2. **任务类型分流**：根据首轮用户消息关键词识别任务类型，加载对应 prompt 块
  3. **上下文动态增减**：根据当前 messages 长度 / token 数动态调整 volatile 块（如压缩前先减少冗余）
  4. **prompt 缓存指标**：暴露 cache hit rate 到 usage_tracker，便于后续优化
- 保留原有 `DEFAULT_AGENT_IDENTITY` / `TOOL_USE_GUIDANCE` / `TASK_WORKFLOW_RULES` 常量接口（向下兼容）
- 新增配置：`prompt.model_templates` 字典 / `prompt.task_classifier` 启用开关

**验收**：
- 至少 3 个模型有特定 prompt 模板（Claude / GPT / 本地 Qwen）
- 至少 3 类任务有 prompt 分流（代码 / 问答 / 自主探索）
- AgentBench kg 准确率从 100%（5/5）维持或提升（基线已高，主要看稳定性）
- prompt cache hit rate 可观测（usage_tracker 暴露 metric）
- 不破坏现有 prompt 接口
- 不破坏现有 2,858 项既有测试

**估算**：中（约 1 周）

### R-P3.1 消息清理（message_sanitization）

**问题**：
- 当前 `spirit/agent/turn_context.py:72` 只有一个 `_sanitize_surrogates`
- Hermes `agent/message_sanitization.py` 有完整的 non_ascii / surrogates / strip_images / close_interrupted_tool_sequence

**功能**：
- 扩展 `turn_context.py` 引入完整 message_sanitization 模块
- 至少 4 类清洗：non_ascii / surrogates / strip_images / close_interrupted_tool_sequence

**验收**：
- 视觉消息（图像）能干净剥离不影响对话
- surrogate 字符不再让 LLM 报 UnicodeEncodeError
- 单元测试覆盖 4 类清洗

**估算**：中（约 1.5 周）

### R-P3.2 敏感信息脱敏（redact）

**问题**：
- 当前 Spirit 没有 redact 模块
- 对话历史可能含 API key / 邮箱 / 身份证号，存入 SQLite 长期可访问
- Hermes `agent/redact.py` 811 行

**功能**：
- 新增 `spirit/agent/redact.py`（**Spirit 简化版 ~150-200 行**，先做正则 + 关键词，不做 ML 分类）
- 在 4 个位置脱敏：
  1. 用户消息入库前
  2. LLM 响应入库前
  3. 工具结果入对话历史前
  4. 日志输出

**验收**：
- 至少 5 类敏感信息能脱敏（API key / 邮箱 / 手机号 / 身份证 / 信用卡）
- 入库对话历史不包含明文敏感信息
- 单元测试覆盖 5 类脱敏规则

**估算**：中（约 1.5 周）

---

## 2. 支柱 M：可量化（Measurability）

> **支柱战略目标**：让自进化 RSI 从"机制存在"升级为"成效可证"。
> **架构依据**：`docs/14-rsiagent-architecture-and-spirit-gap.md` §4.1 ③操作化层 + ④度量层。

### M-P1.1 BRS 广度并行接线进 autonomy

**问题**：
- `spirit/evolution/phase1_wave.py` 已建好（参考 RSIAgent BRS），单元测试全绿
- 但 `autonomy/explorer.py` **只调用了 `EvolutionLoop`（DRS）**，没调用 `Phase1Wave`（BRS）
- 实际只跑"单目标深挖"，广度并行未上线

**功能**：
- `autonomy/explorer.py` 增加 `run_wave_mode(direction, num_branches=3)` 入口
- BRS wave 启动时，Curriculum 一次出 N 个练习项目
- 每个分支从同一份 pre-wave 记忆快照出发独立执行
- 所有分支都完成有效校验后，按 authored 顺序串行蒸馏
- Budget 检查只在完整 wave 边界发生

**验收**：
- `autonomy/` 跑 BRS 模式能产出多分支轨迹
- wave memory barrier 真实生效（未完成分支阻塞整个 wave）
- 单元测试：wave 模式集成 EvolutionLoop + Phase1Wave
- 不破坏现有 DRS 模式

**估算**：中（约 2 周）

### M-P1.2 成效度量 harness

**问题**：
- 当前 Spirit 只能证明"机制离线正确"（单元测试通过）
- 没有"成效数字"：探索了多少子问题、沉淀了多少新经验、复用了多少次冻结记忆
- RSIAgent 论文报告"71.97 → 78.98"是因为有密封官方评测器；Spirit 没有这个

**功能**：
- 新增 `spirit/evolution/metrics_harness.py`（参考 RSIAgent 论文 §6 报告口径）
- 自动跑"前后对比"测试：
  - 跑前：基线准确率
  - 自进化跑 N wave
  - 跑后：新准确率
  - 报告：Δ 数字 + 报告口径（哪些任务被保留 baseline）
- 至少 3 个可重复小 benchmark：
  1. Spirit 自带的代码重构任务（10 个）
  2. Spirit 自带的工具调用任务（10 个）
  3. Spirit 自带的文档生成任务（10 个）

**验收**：
- harness 能跑出"前后 Δ 数字"
- 报告格式清晰：哪些任务 / 哪些被保留 baseline / 哪些是 RSI 实测
- 单元测试：harness 跑出数字格式正确
- 不破坏 evolution/ 现有接口

**估算**：中（约 2 周）

### M-P1.3 领域验证器扩充

**问题**：
- 当前 `spirit/evolution/domains/` 只有 `FinanceDomain`（金融示例）
- 没有代码 / 文档 / 数据领域的验证器
- 用户真实使用场景是"我自己的领域"，需要更多示例

**功能**：
- 新增 3 个领域验证器：
  1. `CodeDomain`：lint 0 警告 + 单元测试通过率 ≥X%
  2. `DocDomain`：文档结构完整 + 关键章节覆盖
  3. `DataDomain`：回测准确率 ≥X% + 数据完整性
- 每个领域验证器是**注入式设计**（用户可扩展自己的领域）
- 跟 RSIAgent 的"密封官方评测器"做对比说明（见 15-product-roadmap-v3.md §6 决策 V3-D2）

**验收**：
- 4 个领域验证器都能跑
- 单元测试覆盖每个验证器的核心规则
- 文档说明如何扩展自定义领域

**估算**：中（约 2 周）

### M-P2.1 自进化日报

**问题**：
- `spirit/evolution/scheduler.py` 已有 interval/daily 调度
- 但"调度执行了什么"没有日报
- 用户不知道 Spirit 在后台到底探索了什么

**功能**：
- 在 `evolution/scheduler.py` 增加日报生成
- 日报内容：
  - 跑了多少 wave / branch
  - 蒸馏了多少新经验（按领域分类）
  - Phase 3 冻结复用了多少次
  - 验证通过率 / 失败率
  - 内存增长 / token 消耗
- 日报推送：桌宠气泡 + Web Dashboard

**验收**：
- 用户每天能在桌宠看到 Spirit 自主探索日报
- 日报格式可读（不是 JSON 堆）
- 单元测试：日报内容计算正确
- 不破坏现有调度

**估算**：小（约 1 周）

### M-P2.2 经验沉淀确认

**问题**：
- 当前 evolution 蒸馏经验是**直接写入**长期记忆
- 用户没机会审阅
- RSIAgent 论文三大失败模式之一"Unreliable Memory Consolidation"在 Spirit 上没法控制

**功能**：
- evolution 蒸馏经验前**生成摘要**（不是完整内容）
- 摘要推送给桌宠，询问用户"这条经验要不要存？"
- HITL 三种选择：存 / 跳过 / 改写
- 改写后写入记忆

**验收**：
- 经验沉淀前桌宠弹气泡询问
- 用户选择能正确影响记忆写入
- 单元测试：HITL 流程完整
- 不破坏 evolution/ 现有 DRS 流程

**估算**：小（约 1 周）

### M-P2.3 wave 状态可视化

**问题**：
- BRS wave 运行中，**用户看不到进度**
- `Phase1Wave` 内部有 wave barrier / branch state，但没暴露给 UI

**功能**：
- 暴露 `Phase1Wave` 的状态到桌宠：
  - 当前 wave 编号
  - 已完成分支数 / 总分支数
  - wave barrier 状态
  - 各分支当前在做什么
- 桌宠可点击查看 wave 详情

**验收**：
- 桌宠能实时显示 wave 进度
- wave 详情面板能展开看每个分支状态
- 单元测试：状态 API 正确

**估算**：小（约 1 周）

### M-P3.1 成效 regression suite

**问题**：
- 没有 CI 能跑"自进化后准确率不退化"
- 改 evolution/ 代码后无法确认"前后一致"

**功能**：
- 引入 CI 跑"小 benchmark suite"（30 个固定任务）
- 每次 evolution/ 改动后自动跑 baseline 对比
- Δ < 阈值则合入，Δ ≥ 阈值则报警

**验收**：
- CI 集成 regression suite
- baseline 数据持久化
- 报警机制工作

**估算**：中（约 1.5 周）

### M-P3.2 strict-alternation 适配评估（strict-alternation-adapter-assessment）

**问题**：
- AgentBench kg-2 任务**实测遇到** strict-alternation chat template 400 错误
- Hermes `moa_alternation.py` 51 行处理此类问题（合并同角色相邻消息 + retry）
- 原决策 V3-D1 说"主流 provider 不需要"——**但没有数据支持**这个判断
- kg-2 实测表明存在 400 问题（可能是某个聚合 provider / 本地 Qwen 模板 / Anthropic strict mode）

**功能**（评估导向）：
- 阶段 1（0.5 周）：**复现 + 诊断**
  - 重跑 AgentBench kg-2 + kg-5 + kg-9，严格记录 400 错误的 provider / model / 模板名
  - 采样 3 个问题 provider，记录错误原文、响应头、provider docs
- 阶段 2（0.5 周）：**决定是否做 moa_alternation**
  - 如果 3 个 provider 都修复不了 → 升级为 P2（加进 v0.3.0）
  - 如果仅 1 个非主流 provider → 保持 P3（只做"上上签"：检测 + 跳过该任务）
  - 如果主流 provider 也遇到 → 升级为 P1（进 v0.2.3）
- 阶段 3（可选）：**实装修复**
  - 参考 Hermes `moa_alternation.py` 51 行实现 Spirit 简化版（~30-50 行）
  - ContextVar 钉住"该 destination 是否预合并"状态，避免重试一遍

**验收**：
- 评估报告：3 个 provider 的 400 原因分析 + 修复成本估算
- 明确决定：P1 / P2 / P3 / 放弃
- 如果决定实装：AgentBench kg 准确率提升 ≥10pp
- 不破坏现有 kg 任务的 100% 准确率基线

**估算**：小（约 1 周，阶段 1 + 2 必做，阶段 3 视评估结果决定）

---

## 3. 支柱 C：桌面伴侣（Companion）

> **支柱战略目标**：让桌宠从"装饰 + 输入框"升级为"任务监控 + HITL 入口 + 经验沉淀确认"。
> **架构依据**：`spirit/desktop/` + `spirit/desktop/ws_server.py` 现有桌宠 WS 协议。

### C-P1.1 长任务进度推送

**问题**：
- 当前 Spirit 执行长任务时桌宠**完全无感**
- 用户不知道 Agent 在干嘛、卡没卡
- v0.2.2 的 delegate 进度 sink 只覆盖"子任务"，不覆盖"主任务"

**功能**：
- 在 `conversation_loop.py` 主循环关键节点广播进度：
  - 开始任务 / 工具调用 / 工具完成 / 思考开始 / 思考结束 / 任务完成
- 进度推送到桌宠（沿用 v0.2.2 的 WS 协议）
- 桌宠显示进度条 + 当前步骤文字
- 进度可折叠（避免打扰用户）

**验收**：
- 桌宠能实时显示主任务进度
- 进度条准确（实际进度 vs 显示进度）
- 单元测试：进度广播正确
- 不破坏现有 WS 协议

**估算**：小（约 1 周）

### C-P1.2 HITL 决策面板

**问题**：
- v0.2.2 Autonomy 的 HITL 已经接通（桌宠 → WS → autonomy_decision）
- 但 HITL 触发条件是"STALLED/分叉"，覆盖不全
- 工具失败 / 经验沉淀 / 跨 provider 切换等场景需要 HITL 决策

**功能**：
- 扩展 `autonomy/hitl.py` 的触发条件
- 至少新增 3 类 HITL 触发：
  1. 工具失败超过 3 次
  2. 经验沉淀确认（见 M-P2.2）
  3. 跨 provider 切换 + 用户偏好询问
- 桌宠决策面板 UI 升级：3 类决策用不同气泡样式

**验收**：
- 3 类触发条件都接通
- 桌宠气泡能区分不同决策类型
- 单元测试：触发条件正确
- 不破坏现有 HITL

**估算**：中（约 1.5 周）

### C-P2.1 任务历史回看

**问题**：
- v0.2.2 新增 `list_sessions` / `resume_session`
- 桌宠能"看到任务列表"但不能"回看任务内容"
- 用户想知道"昨天我让 Spirit 干了什么"

**功能**：
- 桌宠新增"任务历史"面板：
  - 列表显示最近 N 个任务（标题 + 时间 + 状态）
  - 点击展开看任务详情（消息历史 + 工具调用 + 结果）
- 历史可搜索（按关键词 / 时间范围）
- 历史可导出（markdown 格式）

**验收**：
- 桌宠能查看任务历史
- 搜索 / 导出功能工作
- 单元测试：历史数据 API 正确
- 不破坏现有 list_sessions

**估算**：中（约 1.5 周）

### C-P2.2 任务结束摘要推送

**问题**：
- 当前 `conversation_loop.py:582` 返回结果只到 CLI/桌宠
- 没有"任务完成推送"机制
- 用户离开后不知道 Spirit 完成了

**功能**：
- 长任务（>5 分钟或 >10 工具调用）完成时推送摘要到桌宠
- 摘要包含：做了什么 / 结果 / 是否需要后续动作
- 用户可点开看完整对话

**验收**：
- 长任务完成时桌宠收到气泡
- 摘要准确
- 单元测试：推送逻辑正确

**估算**：小（约 1 周）

### C-P2.3 桌宠状态可视化

**问题**：
- 桌宠精灵只有"在/不在"两种状态
- 不知道 Spirit 当前在"思考 / 调用工具 / 等待 HITL / 自主探索"

**功能**：
- 桌宠精灵新增状态机：
  - 空闲（默认）
  - 思考中（眼睛转圈）
  - 调用工具中（手举起）
  - 等待 HITL（气泡飘动）
  - 自主探索中（背景任务图标）
- 状态切换通过 WS 协议广播

**验收**：
- 5 种状态正确显示
- 状态切换平滑（不闪烁）
- 单元测试：状态机正确

**估算**：小（约 1 周）

### C-P3.1 多 session 监控 PoC

**问题**：
- 当前桌宠只能"看自己这个 session"
- 团队用户想看"其他人的 session 进度"

**功能**：
- 桌宠新增"session 列表"视图：
  - 本地所有 session（按时间排序）
  - 远程共享 session（如果配置了）
- session 状态：活跃 / 暂停 / 完成
- 点击可"旁路查看"（只读）

**验收**：
- PoC 能跑通（多 session 列表 + 旁路查看）
- 不做"远程共享"（V4 评估）

**估算**：中（约 2 周）

### C-P3.2 团队经验导入导出

**问题**：
- 当前 Spirit 经验是单机本地
- 团队用户想"把我的经验给你用"

**功能**：
- 经验导出：选 wave / domain → 导出 .zip（带 metadata）
- 经验导入：选 .zip → 校验签名 → 合并到本地记忆
- 签名验证：避免恶意经验

**验收**：
- 导出 / 导入能跑通
- 签名验证工作
- 单元测试：经验合并正确

**估算**：中（约 2 周）

### C-P3.3 任务优先级调度

**问题**：
- 桌宠只有一个"输入框"
- 用户想"我有 3 个任务想并行做"

**功能**：
- 桌宠新增"任务队列"：
  - 多个任务排队
  - 优先级排序
  - 暂停 / 取消 / 调整优先级

**验收**：
- 任务队列能跑
- 优先级排序正确
- 单元测试：调度逻辑正确

**估算**：中（约 2 周）

---

## 4. 功能依赖图

```
R-P1.1 错误恢复链 ─┬─→ R-P1.2 工具结果分类
                  ├─→ R-P1.3 并发工具超时
                  ├─→ R-P2.1 截断多策略
                  └─→ M-P1.0 准确率归因（按归因结果反向引用）

R-P1.4 provider 切换保护 ──→ R-P2.3 provider 切换保护 v2
                                    └─→ M-P1.0 准确率归因（如果“切换上下文丢失”是主因）

R-P1.5 上下文压缩鲁棒性 v2 ─┬─→ M-P1.0 准确率归因（“长任务失忆”修复）
                            └─→ C-P1.1 进度推送（压缩后 LLM 看到一致上下文）

R-P2.4 动态 prompt 组成 ──→ M-P1.0 准确率归因（不同任务类型 prompt 质量提升）

M-P1.1 BRS 接线 ──┬─→ M-P1.2 成效 harness
                  ├─→ M-P2.3 wave 可视化
                  └─→ C-P2.1 任务历史回看

M-P1.3 领域验证器 ──→ M-P2.1 日报

M-P3.2 strict-alternation 评估 ──→ （可进 R-P2 补充修订 / 可不依赖上游独立跑）

C-P1.1 进度推送 ──┬─→ C-P1.2 HITL 面板
                  ├─→ C-P2.2 结束摘要
                  └─→ C-P2.3 状态可视化
```

**关键路径**：
- R-P1.1（错误恢复链）→ R-P1.2（工具结果分类）→ R-P1.3（并发工具超时）→ R-P1.5（压缩鲁棒性） → 可靠性达标
- M-P1.0 准确率归因（按归因结果补能力）→ M-P1.1 BRS 接线 → M-P1.2 成效 harness → M-P2.x → 可量化达标
- C-P1.1（进度推送）→ C-P1.2（HITL 面板）→ C-P2.x → 桌面伴侣达标

---

## 5. 版本组合

| 版本 | 包含功能 | 验证 |
|------|---------|------|
| **v0.2.3** | R-P1.1 错误恢复链 / R-P1.2 工具结果分类 / R-P1.3 并发工具超时 / R-P1.4 provider 切换保护 / **R-P1.5 上下文压缩鲁棒性 v2** / **M-P1.0 准确率归因 + 提升** | AgentBench / SWE-bench 回归（准确率从 62%→72%） |
| **v0.3.0** | + R-P2.1 截断多策略 / R-P2.2 活性检测 / R-P2.3 provider 切换保护 / **R-P2.4 动态 prompt 组成** | AgentBench 完整 + SWE-bench 小样本 |
| **v0.4.0** | + C-P1.1 进度推送 / C-P1.2 HITL 面板 / M-P1.1 BRS 接线 | 桌宠 demo + evolution 真实跑 |
| **v0.5.0** | + M-P1.2 成效 harness / M-P1.3 领域验证器 / M-P2.1 日报 / M-P2.2 经验确认 / M-P2.3 wave 可视化 / C-P2.1 历史回看 / C-P2.2 结束摘要 / C-P2.3 状态可视化 | 完整 V3 验证 |
| **V3.0.0** | + R-P3.1 消息清理 / R-P3.2 敏感信息脱敏 / M-P3.1 regression / **M-P3.2 strict-alternation 适配评估** / C-P3.1 多 session PoC / C-P3.2 经验导入导出 / C-P3.3 任务调度 | V3 大版本发布 |

---

## 6. 不做的功能（V3 明确放弃）

| 候选 | 状态 | 理由 |
|------|------|------|
| `moa_loop.py` | 🟢 **沿用 V2** | Spirit v2.1 已完成 4.1 MoA 移植（Hermes moa_loop 结构与 Spirit transports 能力映射）；V3 只需复用 `autonomy/` + `transports/` 现成实现，**不重列入区** |
| `turn_iteration_prep.py` | 🔴 放弃 | Hermes 迭代前的参数准备 / 限流 / 计数器模块，Spirit 已有 `IterationBudget` + `TurnContext` 覆盖同样职责，不需要独立模块 |
| `turn_overflow.py` | ⚠️ **部分采纳**（含于 R-P2.1） | Hermes 处理输出溢出 / token 超限；Spirit 以 R-P2.1 截断多策略 + R-P1.5 上下文压缩 v2 覆盖同类需求，**不重复独立模块** |
| `turn_preflight.py` | ⚠️ **部分采纳**（含于 R-P1.4 / R-P1.5） | Hermes 出发前的 provider 健康检查 + token 预检；Spirit 以 R-P1.4 provider 切换保护 + R-P1.5 stall detection 覆盖同类需求，**不重复独立模块** |
| `subagent_lifecycle.py` | 🔴 放弃 | Hermes 批量跑 benchmark 的特性，Spirit 常驻模型不适用 |
| `moa_alternation.py` | 🟡 **改 P3 评估** | 之前判断"主流 provider 不需要"无数据支持；AgentBench kg-2 实测遇到 400；改进 P3 评估（见 M-P3.2），根据结果决定是否实装 |
| `turn_facade_lease.py` | 🔴 放弃 | Hermes turn 拆解的细颗粒，Spirit 单 turn 流程够用 |
| `verify_hooks.py` | 🔴 放弃 | Spirit 已有 `evolution/verifier.py` 完整版，不需要 Hook 形式 |
| `model_metadata.py` | 🔴 放弃 | Hermes 用了 ~2700 行管理元数据，Spirit 走集中式 config 即可 |
| `system_prompt.py` | 🔴 放弃 | Spirit `prompt_builder.py` 393 行够用，V3 重构为动态组成（见 R-P2.4）而非拆分 |
| `title_generator.py` / `insights.py` / `learning_graph.py` | 🔴 放弃 | Hermes 高阶分析模块，Spirit 暂未发现使用场景 |
| **云端 SaaS** | 🔴 放弃 | 与 V2 一致，纯本地部署 |
| **第二个 VSCode** | 🔴 放弃 | 桌宠不替代编辑器 |

---

## 7. 参考

- **架构差距**：上一轮 Spirit vs Hermes 源码对比
- **评测证据**：SWE-bench 双 Agent 评测 / AgentBench 自定义评测
- **自进化定位**：`docs/14-rsiagent-architecture-and-spirit-gap.md` §4.1 / §4.2
- **现有功能清单**：`docs/07-development-checklist.md`
- **V3 战略**：[15-product-roadmap-v3.md](./15-product-roadmap-v3.md) §3
- **下一份文档**：[17-development-plan.md](./17-development-plan.md)（V3 开发规划）
