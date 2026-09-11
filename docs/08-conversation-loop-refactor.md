# Spirit Agent 对话循环重构开发文档

> **版本**: v1.0  
> **最后更新**: 2026-09-11  
> **参考源**: Hermes Agent `agent/conversation_loop.py` (L584-L5732)  
> **目标**: 将 Spirit Agent 的对话循环重构为与 Hermes 一致的 6 步流程

---

##  目录

1. [现状分析](#1-现状分析)
2. [目标架构](#2-目标架构)
3. [实施步骤](#3-实施步骤)
4. [代码对比](#4-代码对比)
5. [测试计划](#5-测试计划)
6. [迁移指南](#6-迁移指南)

---

## 1. 现状分析

### 1.1 Spirit Agent 当前实现

**位置**: `spirit/agent/conversation_loop.py` (373 行)  
**问题**: 
- ❌ 缺少完整的 6 步流程
- ❌ 没有迭代预算管理 (`IterationBudget`)
- ❌ 没有错误分类与智能重试
- ❌ 没有工具定义懒加载
- ❌ 没有 XML 工具调用解析回退
- ❌ 上下文压缩检查不完整

### 1.2 Hermes Agent 参考实现

**位置**: `agent/conversation_loop.py` (5737 行)  
**优势**:
- ✅ 完整的 6 步主循环
- ✅ 智能错误分类与恢复
- ✅ 工具定义懒加载（加速首次响应）
- ✅ XML 工具调用解析（兼容非 OpenAI provider）
- ✅ 完善的上下文压缩机制
- ✅ Prompt Caching 支持

---

## 2. 目标架构

### 2.1 核心流程图

```
┌─────────────────────────────────────────────────────────────┐
│                    User Input                                 │
──────────────────────┬──────────────────────────────────────┘
                       ▼
─────────────────────────────────────────────────────────────┐
│              Step 0: build_turn_context()                  │
│  • Stdio 保护                                               │
│  • 用户消息清洗                                             │
│  • 系统提示词管理                                           │
│  • 会话状态初始化                                           │
│  • 外部记忆预取                                             │
──────────────────────┬──────────────────────────────────────┘
                       ▼
─────────────────────────────────────────────────────────────┐
│           Main Loop (while budget > 0)                      │
│                                                             │
│  ─────────────────────────────────────────────────────┐   │
│  │ Step 1: Check Interrupt & Budget                    │   │
│  │    - agent._interrupt_requested? → break            │   │
│  │    - iteration_budget.consume()? → break            │   │
│  └──────────────────────┬──────────────────────────────┘   │
│                         ▼                                   │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ Step 2: Build API Request                           │   │
│  │    - prepare_api_messages(messages)                 │   │
│  │    - get_tool_definitions() [lazy load]             │   │
│  │    - apply_prompt_caching()                         │   │
│  └──────────────────────┬──────────────────────────────┘   │
│                         ▼                                   │
│  ─────────────────────────────────────────────────────┐   │
│  │ Step 3: Call LLM API                                │   │
│  │    - _interruptible_streaming_api_call()            │   │
│  │    - Retry logic (max_retries=3)                    │   │
│  │    - Error classification                           │   │
│  └──────────────────────┬──────────────────────────────┘   │
│                         ▼                                   │
│  ─────────────────────────────────────────────────────┐   │
│  │ Step 4: Parse Response                              │   │
│  │    - Extract tool_calls OR text_content             │   │
│  │    - Parse XML tool calls if needed                 │   │
│  └──────────────────────┬──────────────────────────────┘   │
│                         ▼                                   │
│  ─────────────────────────────────────────────────────┐   │
│  │ Step 5: Branch                                      │   │
│  │                                                     │   │
│  │  A) Has tool_calls?                                 │   │
│  │     ├─ Execute tools                               │   │
│  │     ├─ Append results to messages                  │   │
│  │     ─ continue                                    │   │
│  │                                                     │   │
│  │  B) No tool_calls?                                  │   │
│  │     ├─ Append assistant response                   │   │
│  │     └─ break                                       │   │
│  └─────────────────────────────────────────────────────┘   │
│                         ▼                                   │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ Step 6: Context Compression Check                   │   │
│  │    - estimate_messages_tokens()                     │   │
│  │    - should_compress()?                             │   │
│  │    - compress() if needed                           │   │
│  └─────────────────────────────────────────────────────┘   │
─────────────────────────────────────────────────────────────┘
                       ▼
─────────────────────────────────────────────────────────────┐
│                 Return ConversationResult                   │
─────────────────────────────────────────────────────────────┘
```

### 2.2 关键组件清单

| 组件 | Hermes 位置 | Spirit 当前位置 | 状态 |
|------|------------|----------------|------|
| `IterationBudget` | `agent/iteration_budget.py` | ❌ 缺失 | 🔴 待实现 |
| `ErrorClassifier` | `agent/error_classifier.py` | `spirit/agent/error_handler.py` | 🟡 需升级 |
| `ContextCompressor` | `agent/context_compressor.py` | `spirit/agent/context_compressor.py` |  已有 |
| `PromptCaching` | `agent/prompt_caching.py` | `spirit/agent/prompt_caching.py` | 🟢 已有 |
| `TextToolParser` | `agent/message_content.py` | `spirit/agent/text_tool_parser.py` | 🟢 已有 |
| `TurnContext` | `agent/turn_context.py` | ❌ 缺失 | 🔴 待实现 |

---

## 3. 实施步骤

### Phase 1: 基础组件准备（P0）

#### 3.1.1 创建 `IterationBudget` 类

**文件**: `spirit/agent/iteration_budget.py`  
**参考**: `agent/iteration_budget.py` (Hermes)

```python
"""Iteration budget management for conversation loop."""

class IterationBudget:
    """迭代预算管理"""
    
    def __init__(self, max_total: int = 50):
        self.max_total = max_total
        self.used = 0
        self.remaining = max_total
    
    def consume(self) -> bool:
        """消耗一次迭代，返回是否还有剩余"""
        if self.remaining <= 0:
            return False
        self.used += 1
        self.remaining -= 1
        return True
    
    def refund(self):
        """退还一次迭代（用于压缩后重试）"""
        if self.used > 0:
            self.used -= 1
            self.remaining += 1
```

#### 3.1.2 升级 `ErrorHandler` 为 `ErrorClassifier`

**文件**: `spirit/agent/error_handler.py` → `spirit/agent/error_classifier.py`  
**参考**: `agent/error_classifier.py` (Hermes)

需要添加：
- `FailoverReason` 枚举
- `ErrorRecoveryPlan` 数据类
- `classify_api_error()` 函数

#### 3.1.3 创建 `TurnContext` 模块

**文件**: `spirit/agent/turn_context.py`  
**参考**: `agent/turn_context.py` (Hermes)

职责：
- Stdio 保护
- 用户消息清洗
- 系统提示词管理
- 会话状态初始化

### Phase 2: 主循环重构（P0）

#### 3.2.1 重写 `run_conversation()` 函数

**文件**: `spirit/agent/conversation_loop.py`  
**参考**: `agent/conversation_loop.py:L584-L5600` (Hermes)

关键改动：
1. 引入 `build_turn_context()` 初始化
2. 实现 6 步主循环
3. 添加工具定义懒加载
4. 实现 XML 工具调用解析回退
5. 完善错误重试逻辑
6. 添加上下文压缩检查

#### 3.2.2 实现工具定义懒加载

在主循环中添加：

```python
# 初始化
tools = None
tools_loaded = False

# 主循环中
if not tools_loaded and has_tool_calls:
    tools = agent.get_tool_definitions()
    tools_loaded = True
    logger.info(
        "工具懒加载触发: 检测到 %d 个工具调用，已加载 %d 个工具定义",
        len(tool_calls), len(tools)
    )
```

#### 3.2.3 实现 XML 工具调用解析

在响应解析中添加：

```python
# 提取工具调用（优先 OpenAI 结构化，回退 XML 解析）
tool_calls = list(message.tool_calls) if message.tool_calls else []
text_content = message.content or ""

if not tool_calls and text_content:
    # 尝试从文本中解析 XML 格式的工具调用
    from spirit.agent.text_tool_parser import parse_text_tool_calls
    parsed_calls, cleaned_text = parse_text_tool_calls(text_content)
    if parsed_calls:
        tool_calls = parsed_calls
        text_content = cleaned_text
```

### Phase 3: 集成测试（P1）

#### 3.3.1 单元测试

- [ ] `test_iteration_budget.py` - 测试预算消耗/退还
- [ ] `test_error_classifier.py` - 测试错误分类
- [ ] `test_turn_context.py` - 测试上下文构建
- [ ] `test_conversation_loop.py` - 测试完整循环

#### 3.3.2 集成测试

- [ ] 测试工具定义懒加载（首轮不发送工具）
- [ ] 测试 XML 工具调用解析（MiniMax provider）
- [ ] 测试错误重试（模拟 API 失败）
- [ ] 测试上下文压缩（长对话场景）

---

## 4. 代码对比

### 4.1 Spirit 当前实现 vs Hermes 参考

#### 当前 Spirit 实现（简化版）

```python
def run_conversation(agent, user_message: str, **kwargs) -> ConversationResult:
    # 初始化
    if not agent.messages:
        system_prompt = agent.get_system_prompt()
        agent.add_message("system", system_prompt)
    
    agent.add_message("user", user_message)
    
    # 简单循环
    while budget.remaining > 0:
        # 构建请求
        api_messages = prepare_api_messages(agent.messages)
        tools = agent.get_tool_definitions()  # ❌ 每轮都发送
        
        # 调用 API
        response = call_llm(api_messages, tools)
        
        # 解析响应
        tool_calls = response.choices[0].message.tool_calls
        
        if tool_calls:
            # 执行工具
            results = execute_tools(tool_calls)
            agent.messages.append(...)
            continue
        else:
            # 返回最终答案
            return ConversationResult(...)
```

#### Hermes 参考实现（完整版）

```python
def run_conversation(agent, user_message: Any, ...) -> Dict[str, Any]:
    # ── Step 0: 初始化 ──
    _ctx = build_turn_context(
        agent, user_message, system_message, ...
    )
    messages = _ctx.messages
    active_system_prompt = _ctx.active_system_prompt
    
    # 初始化计数器
    api_call_count = 0
    final_response = None
    interrupted = False
    failed = False
    
    # ─ Main Loop ──
    while (api_call_count < agent.max_iterations and 
           agent.iteration_budget.remaining > 0):
        
        # ─ Step 1: 中断与预算检查 ──
        if agent._interrupt_requested:
            interrupted = True
            break
        
        if not agent.iteration_budget.consume():
            break
        
        api_call_count += 1
        
        # ── Step 2: 构建 API 请求 ──
        api_messages = prepare_api_messages(messages)
        
        # 懒加载工具定义
        if not tools_loaded:
            tools = agent.get_tool_definitions()
            tools_loaded = True
        
        # 应用 Prompt Caching
        if use_cache:
            api_messages = apply_anthropic_cache_control(api_messages)
        
        # ── Step 3: 调用 LLM API ──
        retry_count = 0
        while retry_count < max_retries:
            try:
                response = agent._interruptible_streaming_api_call(
                    api_kwargs, on_first_delta=_stop_spinner
                )
                break
                
            except Exception as e:
                classified = classify_api_error(e, agent.provider)
                plan = ErrorRecoveryPlan.from_classification(classified, retry_count)
                
                if plan.should_abort:
                    return error_result
                
                if plan.should_retry:
                    retry_count += 1
                    time.sleep(plan.wait_seconds)
                    continue
                
                if plan.should_compress:
                    messages = compressor.compress(messages, approx_tokens)
                    api_messages = prepare_api_messages(messages)
                    break
        
        # ── Step 4: 解析响应 ──
        choice = response.choices[0]
        message = choice.message
        
        tool_calls = list(message.tool_calls) if message.tool_calls else []
        text_content = message.content or ""
        
        # XML 工具调用解析回退
        if not tool_calls and text_content:
            parsed_calls, cleaned_text = parse_text_tool_calls(text_content)
            if parsed_calls:
                tool_calls = parsed_calls
                text_content = cleaned_text
        
        # ── Step 5: 分支处理 ──
        if tool_calls:
            # A) 有工具调用 → 执行工具 → 继续循环
            tool_results = execute_tool_calls(agent, tool_calls)
            messages.append({
                "role": "assistant",
                "tool_calls": tool_calls
            })
            for result in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": result["id"],
                    "content": result["result"]
                })
            continue
            
        else:
            # B) 无工具调用 → 最终答案 → 结束循环
            messages.append({
                "role": "assistant",
                "content": text_content
            })
            final_response = text_content
            break
        
        # ── Step 6: 上下文压缩检查 ──
        if compressor and len(messages) > 5:
            approx_tokens = estimate_messages_tokens(messages)
            if compressor.should_compress(approx_tokens):
                messages = compressor.compress(messages, approx_tokens)
    
    # ── 返回结果 ──
    return {
        "final_response": final_response,
        "messages": messages,
        "api_calls": api_call_count,
        "completed": not interrupted and not failed,
        "failed": failed,
    }
```

---

## 5. 测试计划

### 5.1 单元测试清单

| 测试项 | 文件 | 覆盖内容 |
|--------|------|---------|
| 迭代预算 | `tests/agent/test_iteration_budget.py` | consume/refund/reset |
| 错误分类 | `tests/agent/test_error_classifier.py` | 各类错误分类与恢复计划 |
| TurnContext | `tests/agent/test_turn_context.py` | 消息清洗、提示词管理 |
| 工具懒加载 | `tests/agent/test_lazy_tool_loading.py` | 首轮不发送工具定义 |
| XML 解析 | `tests/agent/test_xml_tool_parsing.py` | MiniMax provider 兼容性 |

### 5.2 集成测试场景

| 场景 | 预期行为 | 验证点 |
|------|---------|--------|
| 纯文本对话 | 快速返回，不加载工具 | 首轮 API payload 小 |
| 工具调用对话 | 检测到工具后加载定义 | 第二轮才发送工具 |
| API 超时 | 指数退避重试 | 1s, 2s, 4s 间隔 |
| 上下文超长 | 自动压缩中间消息 | 保留首尾，压缩中间 |
| 本地 Bug | 立即终止，不重试 | 避免无限循环 |

---

## 6. 迁移指南

### 6.1 向后兼容性

Spirit Agent 的新对话循环应该保持向后兼容：

- ✅ 现有的 `AgentConfig` 不变
- ✅ 现有的 `ConversationResult` 不变
- ✅ 现有的工具注册机制不变

### 6.2 配置项新增

需要在 `config.yaml.example` 中添加：

```yaml
agent:
  # 迭代控制
  max_iterations: 50              # 最大迭代次数
  
  # 错误处理
  max_retries: 3                  # API 调用最大重试次数
  retry_backoff_base: 2           # 指数退避基数
  
  # 工具优化
  lazy_load_tools: true           # 启用工具定义懒加载
  
  # 上下文管理
  enable_compression: true        # 启用上下文压缩
  compression_threshold: 0.75     # 压缩阈值（窗口利用率）
  
  # Prompt Caching
  enable_prompt_cache: true       # 启用 Prompt Caching
```

### 6.3 迁移步骤

1. **备份现有代码**
   ```bash
   cp spirit/agent/conversation_loop.py spirit/agent/conversation_loop.py.bak
   ```

2. **创建新组件**
   - `spirit/agent/iteration_budget.py`
   - `spirit/agent/error_classifier.py`
   - `spirit/agent/turn_context.py`

3. **重写主循环**
   - 修改 `spirit/agent/conversation_loop.py:run_conversation()`

4. **运行测试**
   ```bash
   pytest tests/agent/ -v
   ```

5. **验证功能**
   - 启动 Spirit Agent
   - 测试纯文本对话
   - 测试工具调用
   - 测试错误重试

---

## 7. 性能优化预期

### 7.1 首次响应时间

| 场景 | 当前 | 优化后 | 提升 |
|------|------|--------|------|
| 纯文本对话 | ~25s | ~5s | **5x** |
| 工具调用对话 | ~30s | ~8s | **3.75x** |

**原因**: 工具定义懒加载减少首轮 payload（通常数万 token）

### 7.2 Token 成本

| 场景 | 当前 | 优化后 | 节省 |
|------|------|--------|------|
| Prompt Caching 命中 | 100% | 100% | - |
| Prompt Caching 未命中 | 100% | ~60% | **40%** |

**原因**: Prompt Caching 复用系统提示词和长文档

### 7.3 稳定性

| 错误类型 | 当前处理 | 优化后处理 |
|---------|---------|-----------|
| 速率限制 | 固定等待 | 指数退避 |
| 上下文超长 | 直接失败 | 自动压缩后重试 |
| 网络抖动 | 立即失败 | 智能重试 |
| 本地 Bug | 可能重试 | 立即终止 |

---

## 8. 下一步行动

### 本周（P0）
- [ ] 创建 `IterationBudget` 类
- [ ] 升级 `ErrorHandler` 为 `ErrorClassifier`
- [ ] 创建 `TurnContext` 模块
- [ ] 编写单元测试

### 下周（P0）
- [ ] 重写 `run_conversation()` 主循环
- [ ] 实现工具定义懒加载
- [ ] 实现 XML 工具调用解析
- [ ] 集成测试

### 下下周（P1）
- [ ] 性能基准测试
- [ ] 文档更新
- [ ] 配置项完善
- [ ] 用户手册

---

**最后更新**: 2026-09-11  
**维护者**: Spirit Agent 开发团队  
**参考源**: Hermes Agent v0.18.2
