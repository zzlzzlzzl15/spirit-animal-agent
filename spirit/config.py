"""Spirit Agent 集中式配置系统。

参考 Hermes 的 hermes_cli/config.py 设计：
- DEFAULT_CONFIG 字典定义所有默认值（模型留空，由环境变量或用户提供）
- load_config() 从 DEFAULT_CONFIG 出发，deep-merge YAML + 环境变量 + 参数覆盖
- 自动发现 ~/.spirit/config.yaml，无需显式传路径
- save_config() 持久化用户配置

优先级：参数 > 环境变量 > YAML 文件 > DEFAULT_CONFIG

环境变量映射（SPIRIT_ 前缀）：
  SPIRIT_MODEL         → llm.model
  SPIRIT_PROVIDER      → llm.provider
  SPIRIT_API_KEY       → llm.api_key
  SPIRIT_BASE_URL      → llm.base_url
  SPIRIT_CONTEXT_LENGTH → llm.context_length
  SPIRIT_MAX_ITERATIONS → agent.max_iterations
  SPIRIT_REASONING_EFFORT → agent.reasoning_effort
  SPIRIT_VERBOSE         → agent.verbose
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SPIRIT_HOME
# ---------------------------------------------------------------------------

SPIRIT_HOME = Path(os.environ.get("SPIRIT_HOME", "~/.spirit")).expanduser()


# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict[str, Any] = {
    # ── LLM 连接 ──────────────────────────────────────────────
    "llm": {
        "model": "",                          # 模型名称，留空由用户提供（如 "gpt-4o", "claude-3-5-sonnet"）
        "provider": "auto",                   # 推理 Provider: auto|openai|openrouter|ollama|anthropic|custom...
        "api_key": "",                        # API 密钥，留空从环境变量或 .env 自动读取
        "base_url": "",                       # API 端点，留空由 provider 自动解析
        "context_length": 128_000,            # 上下文窗口大小（输入+输出 token 总和）
        "max_tokens": 0,                      # 最大输出 token 数，0 = 使用模型默认值
        "anthropic_default_max_tokens": 4096, # Anthropic Transport 默认 max_tokens 上限
        "image_analysis_max_tokens": 1000,    # 图片分析 LLM 调用的 max_tokens 上限
    },
    # ── Agent 行为 ────────────────────────────────────────────
    "agent": {
        "max_iterations": 90,                 # 单次对话循环最大工具调用迭代次数
        "max_retries": 3,                     # API 调用失败后最大重试次数
        "reasoning_effort": "medium",         # 推理努力程度: high|medium|low|minimal|none
        "verbose": False,                     # 是否输出详细调试日志
        "max_memories": 1000,                 # 跨会话持久记忆最大条数（超出后 LRU 淘汰）
        "credential_max_fail_count": 10,      # 凭据池：单凭据累计失败 N 次后自动禁用
        "credential_cooldown": 300,           # 凭据池：失败后冷却时间（秒），期间不再使用
        # Tool Search（Progressive Tool Disclosure — 照 hermes 改为 3 bridge 代替 60+ schema）
        # 开启 + 工具总数 > eager_threshold 时，对 LLM 只发 3 个元工具
        # （tool_search / tool_describe / tool_call），系统 prompt 注入 manifest 透露其余。
        # 关 / 工具少时仍走原路径（发所有 schema）。
        "tool_search": {
            "enabled": False,                 # 默认关，保守（手动 ~/.spirit/config.yaml 启用）
            "eager_threshold": 15,            # ≤ 此数直接发所有 schema（不用 bridge，避免多跳开销）
            "manifest_budget_pct": 5.0,       # manifest 占 context 总长度的比例（5% ≈ 25.6K chars @ 128K）
        },
        # R-P2.2：turn 活性 watchdog — LLM 推理 / 工具调度 / 子任务跨进程等全场景假死检测
        # 单点卡死超时（秒）后弹报警 + 写 agent._turn_liveness_aborted，主循环下轮 break
        "turn_liveness": {
            "timeout_s": 600.0,               # 整体空闲超时 (0 = 禁用 watchdog)
            "poll_s": 15.0,                   # watchdog 轮询间隔
        },
    },
    # ── 上下文压缩（对话过长时自动摘要） ──────────────────────
    "compression": {
        "enabled": True,                      # 是否启用自动上下文压缩
        "threshold": 0.75,                    # 上下文窗口占用达到此比例时触发压缩（0.75 = 75%）
        "protect_last_n": 6,                  # 始终保留的最近消息数（防止压缩掉最新对话）
        "protect_first_n": 3,                 # 始终保留的开头消息数（保留系统提示和初始上下文）
        "chars_per_token": 3.5,               # 粗略 token 估算比例（1 token ≈ 3.5 字符）
        "summary_target_ratio": 0.3,          # 压缩目标比例（压缩到原文的 30%）
        "summary_max_tokens": 2000,           # 摘要 LLM 调用的 max_tokens 上限
        "summary_temperature": 0.3,           # 摘要 LLM 温度（低温度 = 更确定性输出）
        "incremental_summary_max_tokens": 2500, # 迭代合并摘要的 max_tokens 上限
    },
    # ── 流式输出 ──────────────────────────────────────────────
    "streaming": {
        "enabled": False,                     # 是否启用流式 token 输出（逐字显示）
    },
    # ── 统一日志（v0.3.5）─────────────────────────────────────
    # 终端级别由 -v 控制（默认 ERROR）；这里控制文件日志。
    # 产物：$SPIRIT_HOME/logs/agent.log（全量滚动）、errors.log（仅 ERROR+）、
    #       events.jsonl（结构化业务事件，供 `spirit logs` 查询）。
    "logging": {
        "enabled": True,                      # 是否落盘文件日志（关闭则只打终端）
        "level": "INFO",                     # agent.log 级别
        "max_bytes": 10 * 1024 * 1024,       # 单文件 10MB
        "backup_count": 5,                   # 保留 5 个滚动文件
        "events": True,                      # 是否写 events.jsonl
        "traceback": True,                   # api_error 等是否带 traceback
        "event_preview_chars": 500,          # 事件正文字段截断长度
    },
    # ── 终端显示（CLI / 桌宠）──────────────────────────────
    "display": {
        # 实时显示 LLM 中间推理内容（think / reasoning_content 块）。
        # 默认 True：对齐 Hermes 默认 show_reasoning=True；
        # 思考模型（ark-code-latest / MiniMax-M3 / DeepSeek R1 / GLM-5 等）下用户能看到
        # 模型"在思考什么"，而不是只看到 "推理完成 (42s)"。
        # False 时仅输出 THINKING / THINKING_END 耗时数字。
        "show_reasoning": True,
        # 思考内容折叠：>5 行只显示前 5 行 + ``... (+N more lines)``（对齐 Hermes reasoning_full=False）。
        "reasoning_full": False,
        # v0.4.x：工具调用详情（对齐 Hermes cli_stream_mixin._on_tool_progress）
        # 显示工具参数预览（如 ``path=/x/foo.py, recursive=true``）。True 时
        # TOOL_STARTED / TOOL_GEN_START 阶段打印参数；False 仅打印工具名。
        "show_tool_args": True,
        # 工具调用结果预览：True 时 TOOL_COMPLETED 阶段附带 result 摘要。
        # 折叠长度由 tool_result_max_len 控制（默认 300 字符）。
        "show_tool_result": True,
        # 工具结果预览截断长度（字符）。超过此长度用 ``...`` 折叠。
        "tool_result_max_len": 300,
        # v0.4.x：流式最终回答（对齐 Hermes _fire_stream_delta）
        # True 时 STREAM_TEXT_DELTA 事件逐 token 打印到终端（与原有
        # stream_callback 行为一致）；False 时仅以一行 summary 收尾。
        # 默认 True：保持现有行为，让用户实时看到模型输出。
        "show_stream_text": True,
    },
    # ── Prompt caching（Anthropic 风格 cache_control） ─────────
    "prompt_caching": {
        "enabled": True,                      # 是否启用 prompt caching（减少 ~75% 输入 token 成本）
        "ttl": "5m",                          # 缓存有效期: '5m' 或 '1h'（Anthropic 支持两种）
    },
    # ── 目标系统（Ralph Loop — 持久目标自主循环，Phase 4） ────
    "goals": {
        "enabled": True,                      # 是否启用持久目标系统（Ralph Loop）
        "max_turns": 20,                      # 单个目标自主续传的最大轮数预算（超出后暂停）
        "max_consecutive_parse_failures": 3,  # judge 连续 N 次响应解析失败后自动暂停（弱模型兜底）
        "judge": {
            "model": "",                      # judge 辅助模型，留空复用主 Agent 模型
            "max_tokens": 4096,               # judge 调用最大输出 token 数
            "temperature": 0,                 # judge 温度（0 = 最确定性裁决）
            "timeout": 30.0,                  # judge 单次调用超时（秒）
        },
    },
    # ── 后台进程注册表（terminal background=true，Phase 4.7） ──
    "process": {
        "max_output_chars": 200_000,          # 单个后台进程的滚动输出缓冲上限（字符）
        "finished_ttl_seconds": 1800,         # 已退出进程的保留时长（30 分钟），超时被 prune
        "max_processes": 64,                  # 同时追踪的进程上限（超出按 started_at 淘汰最旧已完成项）
        "max_active_age_seconds": 86400,      # 运行超过该时长视为陈旧，不再阻塞会话重置/空闲判定
        "wait_default_seconds": 180,          # process(action="wait") 的默认与最大阻塞秒数（超出被钳制）
        "daemon_term_grace_seconds": 2.0,     # SIGTERM 到升级 SIGKILL 的宽限窗口（0 = 关闭升级）
        "checkpoint_enabled": True,           # 是否写 processes.json 崩溃恢复检查点
        "notify_on_complete_default": True,   # terminal(background=true) 默认是否在退出时通知 Agent
        "watch_min_interval_seconds": 15,     # 每会话 watch 匹配通知的最小间隔（限流）
        "watch_strike_limit": 3,              # 连续 N 个限流窗口后永久关闭该会话的 watch
        "watch_global_max_per_window": 15,    # 全局熔断：单窗口内最多放行的 watch 通知数
        "watch_global_window_seconds": 10,    # 全局熔断的滑动窗口长度（秒）
        "watch_global_cooldown_seconds": 30,  # 全局熔断触发后的冷却时长（秒）
        "max_notifications_per_turn": 5,      # 单轮回灌对话的最大通知条数（防刷屏）
    },
    # ── Mixture of Agents（MoA — 多模型参考+聚合，Phase 4.1） ──
    # provider=="moa" 时，agent.client 换成 MoAClient facade：并行跑参考模型
    # （advisory）→ 注入 guidance → 调聚合器（acting model）。预设（presets）是命名
    # 的模型组合；读时容错降级到默认，写时用 validate_moa_payload 严格校验。
    "moa": {
        "default_preset": "default",          # 无显式预设名时使用的预设
        "active_preset": "",                  # /moa use <name> 设定的活动预设（空=default）
        "save_traces": False,                 # 是否把完整 MoA 轮次追踪写入 JSONL（opt-in）
        "trace_dir": None,                    # 追踪目录覆盖（默认 <spirit_home>/moa-traces）
        "presets": {
            "default": {
                "enabled": True,              # 关闭后跳过参考 fan-out，聚合器独自行动
                "reference_models": [          # 参考（advisory）模型 slot 列表
                    {"provider": "openrouter", "model": "deepseek/deepseek-v3"},
                    {"provider": "openai", "model": "gpt-4o"},
                ],
                "aggregator": {"provider": "anthropic", "model": "claude-3-5-sonnet"},
                "reference_temperature": None,     # None = 不下发（用 provider 默认）
                "aggregator_temperature": None,
                "max_tokens": 4096,                # 兼容字段；MoA 运行时不封顶聚合器
                "reference_max_tokens": None,      # 仅封顶 advisor 输出（None=不封顶）
                "fanout": "per_iteration",         # per_iteration | user_turn
            },
        },
    },
    # ── 技能中心（Skills Hub — 发现/调用/市场，Phase 4.6） ──────
    # 技能是 ~/.spirit/skills/<name>/SKILL.md（YAML frontmatter + 正文）。/<skill-name>
    # 把正文注入对话；/<bundle-name> 一次载一组。市场安装走「隔离→扫描→安装」，离线
    # 可测（hub.set_fetcher 注入获取器）。读时容错降级（load_skills_config 异常→空节）。
    "skills": {
        "template_vars": True,                # 是否替换 ${SPIRIT_SKILL_DIR}/${SPIRIT_SESSION_ID} 模板变量
        "inline_shell": False,                # 是否展开技能正文里的 !`cmd` 内联 shell（安全成本，默认关）
        "inline_shell_timeout": 10,           # 单条内联 shell 的超时（秒）
        "disabled": [],                       # 全局禁用的技能名列表（offer 面隐藏，显式加载仍放行）
        "platform_disabled": {},              # 按平台禁用：{"windows": ["name"], ...}（与全局禁用取并集）
        "external_dirs": [],                  # 额外技能根目录（相对 SPIRIT_HOME 或绝对路径；本地优先于外部）
    },
    # ── Computer Use（桌面控制可测试抽象层，Phase 4.5） ──────────
    # 对标 Hermes tools/computer_use/：backend 抽象 + 安全分级 + 审批门 + 派发 + 响应塑形。
    # 真实驱动经 tool.set_backend_factory 注入；缺省 noop 后端安全降级（动作只记录、不触碰
    # 真实桌面），故全套逻辑可离线单测。env 覆盖优先于本节（SPIRIT_COMPUTER_USE_BACKEND /
    # SPIRIT_CUA_DRIVER_CMD）；本节值是模块默认值的单一来源。
    "computer_use": {
        "enabled": True,                      # 是否暴露 computer_use 工具（check_fn 参考；关闭则工具面隐藏）
        "backend": "noop",                    # 默认后端名（noop=安全降级；真实驱动经工厂注入后改名）
        "driver_cmd": "cua-driver",           # 真实桌面驱动二进制名 / 路径（就绪度探测用）
        "default_capture_mode": "som",        # capture 默认模式：som（视觉标记）| vision（纯截图）| ax（无障碍树）
        "max_elements": 100,                  # 单次捕获默认返回的可交互元素上限（超出截断并提示）
        "max_elements_ceiling": 1000,         # max_elements 的硬上限（调用方传更大值也被钳制到此）
        "wait_max_seconds": 30,               # wait 动作单次阻塞秒数上限（超出被钳制）
        "approval_required": True,            # 改变状态的动作是否经审批门（网关外层已审批时可关）
    },
    # ── 工具循环护栏（防止工具调用死循环） ────────────────────
    "tool_loop_guardrails": {
        "warnings_enabled": True,             # 是否启用循环检测警告（向 LLM 发送提醒）
        "hard_stop_enabled": False,           # 是否启用强制停止（超过阈值直接中断循环）
        "warn_after": {
            "exact_failure": 2,               # 同一工具+相同参数连续失败 N 次后警告
            "same_tool_failure": 3,           # 同一工具（不同参数）累计失败 N 次后警告
            "idempotent_no_progress": 2,      # 幂等操作（如只读查询）无进展 N 次后警告
        },
        "hard_stop_after": {
            "exact_failure": 5,               # 同一工具+相同参数连续失败 N 次后强制停止
            "same_tool_failure": 8,           # 同一工具累计失败 N 次后强制停止
            "idempotent_no_progress": 5,      # 幂等操作无进展 N 次后强制停止
        },
    },
    # ── 超时配置（秒） ────────────────────────────────────────
    "timeouts": {
        "terminal_default": 120,              # 终端命令默认超时
        "terminal_max": 600,                  # 终端命令允许设置的最大超时上限
        "execute_code_default": 60,           # Python 代码执行默认超时
        "execute_code_max": 300,              # Python 代码执行最大超时上限
        "web_default": 30.0,                  # Web 搜索/抓取默认超时
        "delegate_default": 900,              # 子 Agent 委派单次尝试墙钟上限（超时自动重启子任务）
        "stream_ttfb_seconds": 90.0,          # 流首字节看门狗：发起请求后 N 秒无任何 chunk → 杀流重试（0=禁用）
        "stream_chunk_stale_seconds": 120.0,  # 流 chunk 间隔看门狗：N 秒无新 chunk → 判流死亡杀流重试（0=禁用）
        "memora_tool": 60,                    # Memora 知识库工具调用超时
        "gateway_confirm": 300,               # 斜杠命令确认等待超时
        "graph_client": 60.0,                 # MS Graph API 客户端请求超时
        "graph_max_retries": 3,               # MS Graph API 最大重试次数
        "health_check": 5,                    # 通用健康检查请求超时
        "tts_subprocess": 30,                 # TTS 语音合成子进程超时
        "osv_api": 10,                        # OSV 漏洞数据库 API 请求超时
        "memora_health": 5.0,                 # Memora 后端健康检查超时
        "memora_client_api": 60,              # Memora 客户端 API 调用超时（搜索/上传）
        "browser_dialog": 300.0,              # 浏览器对话框交互等待超时
        "chat_request": 1800.0,               # WebSocket 聊天请求超时（默认 30 分钟；多工具长任务 5 分钟会误杀）
        "stt_transcribe": 30.0,               # 语音识别（STT）超时（默认 30 秒）
        "tts_synthesize": 30.0,               # 语音合成（TTS）超时（默认 30 秒）
    },
    # ── 聊天超时自动续跑（超时≠结束） ────────────────────
    "chat": {
        "auto_resume_on_timeout": True,       # 超时中断当前轮后自动注入续跑指令重进循环，继续完成未完成任务
        "max_auto_resumes": 3,                # 单次聊天请求最多自动续跑轮数（防无限续跑烧 token）
        "resume_grace_seconds": 120,          # 超时后等旧对话循环退出的宽限秒数；逾放弃续跑（防双 chat 并行）
    },
    # ── 委派子任务容错（停滞监测 + 超时自动重启） ──────────────
    "delegation": {
        "child_max_attempts": 2,              # 每个子任务最大尝试次数（含首次）；停滞/单次超时/异常自动重启，耗尽才报失败
        "child_stale_idle_seconds": 450,      # 子 Agent 轮间等待（非工具内）无任何进展 N 秒 → 判停滞杀当前尝试（0=禁用）
        "child_stale_in_tool_seconds": 1200,  # 子 Agent 卡同一工具无进展 N 秒 → 判停滞杀当前尝试（0=禁用）
    },
    # ── 自主探索（Autonomy Loop — 常驻自由探索，Phase 7） ──────
    # 桌宠启动默认挂载（除非 --no-autonomy）；给一个 domain 方向即围绕它自由探索。
    "autonomy": {
        "enabled": True,                      # 是否启用常驻自主探索循环
        "domain": "",                         # 探索方向/聚焦领域（空=不限定，LLM 基于记忆自由提议）；运行时可经桌宠输入框改写并立即触发一轮
        "run_on_start": False,                # 启动即跑首轮探索（True=不等满 interval 立即探索一轮）
        "interval_hours": 5.0,                # 常驻探索间隔（小时），对齐 API token 每 5h 刷新的配额窗口
        "stop_policy": "curriculum_review",   # 单任务停止策略：curriculum_review（PASS 后可再练/再改，默认）| verifier_pass（一经验证即停，省 token）
        "max_rounds": 5,                      # 单个探索任务最大迭代轮次
        "max_iterations": 40,                 # execute 内 SpiritAgent 单次对话循环最大工具迭代次数
        "hitl_timeout": 300.0,                # HITL 气泡询问等待答复超时（秒），超时 fail-open 自主决策
    },
    # ── 错误恢复退避（秒） ────────────────────────────────────
    "backoff": {
        "overloaded_base": 10.0,              # Provider 过载时退避基础延迟
        "overloaded_max": 180.0,              # Provider 过载时退避最大延迟上限
        "rate_limit_base": 5.0,               # 触发速率限制时退避基础延迟
        "rate_limit_max": 120.0,              # 触发速率限制时退避最大延迟上限
        "server_error_base": 3.0,             # 服务端错误(5xx)时退避基础延迟
        "server_error_max": 60.0,             # 服务端错误时退避最大延迟上限
        "timeout_base": 5.0,                  # 请求超时时退避基础延迟
        "timeout_max": 90.0,                  # 请求超时时退避最大延迟上限
        "auth_base": 2.0,                     # 认证失败时退避基础延迟
        "auth_max": 30.0,                     # 认证失败时退避最大延迟上限
        "default_base": 5.0,                  # 未分类错误退避基础延迟
        "default_max": 120.0,                 # 未分类错误退避最大延迟上限
        "jitter_ratio": 0.5,                  # 退避抖动比例（0-1，防止多会话同时重试）
    },
    # ── 输出限制 ──────────────────────────────────────────────
    "limits": {
        "tool_output_max_chars": 100_000,     # 通用工具输出最大字符数（超出截断）
        "tool_output_max_lines": 2000,        # 通用工具输出最大行数（超出截断）
        "terminal_output_max_chars": 50_000,  # 终端命令输出最大字符数
        "execute_code_max_bytes": 50_000,     # Python 代码执行输出最大字节数
        "read_extract_max_chars": 30_000,     # 文档提取（ipynb/docx/xlsx）最大字符数
        "diff_max_chars": 20_000,             # Git diff 输出最大字符数
        "delegate_max_chars": 20_000,         # 子 Agent 委派结果最大字符数
        "search_max_chars": 20_000,           # 文件搜索结果最大字符数
        "image_analysis_max_chars": 5_000,    # 图片分析结果最大字符数（结果通常较小）
    },
    # ── 平台消息限制 ──────────────────────────────────────────
    "platforms": {
        "default_max_message_length": 4096,   # 未特别配置的平台的默认单条消息长度上限
        "default_reconnect_backoff": [2, 5, 10, 30, 60],  # 默认重连退避序列（秒）
        "default_heartbeat_interval": 30.0,   # 默认心跳发送间隔（秒）
        "chunk_delay": 0.5,                   # 长消息分片发送间隔（秒，防止速率限制）
        "qq": {
            "max_message_length": 2000,       # QQ 单条消息长度上限
        },
        "wecom": {
            "max_message_length": 4000,       # 企业微信单条消息长度上限
            "connect_timeout": 20.0,          # 企业微信 WebSocket 连接超时
        },
        "feishu": {
            "max_message_length": 30000,      # 飞书单条消息长度上限
        },
        "dingtalk": {
            "max_message_length": 20000,      # 钉钉单条消息长度上限
        },
        "discord": {
            "max_message_length": 2000,       # Discord 单条消息长度上限
            "max_embed_length": 4096,         # Discord Embed 描述长度上限
            "typing_interval": 5.0,           # Discord 打字指示器发送间隔（秒）
        },
        "telegram": {
            "max_message_length": 4096,       # Telegram 单条消息长度上限（UTF-16 代码单元）
            "max_caption_length": 1024,       # Telegram 媒体文件标题长度上限
            "typing_interval": 4.0,           # Telegram 打字指示器发送间隔（秒）
        },
        "slack": {
            "max_message_length": 40000,      # Slack 单条消息长度上限
            "typing_interval": 5.0,           # Slack 打字指示器发送间隔（秒）
        },
    },
    # ── Memora 知识库 ─────────────────────────────────────────
    "memora": {
        "base_url": "http://127.0.0.1:8000", # Memora 后端服务地址（compose 映射 8000->容器 8080）
        "min_file_size": 100,                 # 自动保存最小文件大小（bytes），过小不保存
        "max_file_size": 5_242_880,           # 自动保存最大文件大小（5MB），过大不自动保存
    },
    # ── 浏览器自动化（CDP） ───────────────────────────────────
    "browser": {
        "frame_tree_max_entries": 30,         # frame 树追踪最大条目数（防止内存膨胀）
        "frame_tree_max_oopif_depth": 2,      # 跨进程 iframe 最大递归深度
        "console_history_max": 50,            # 浏览器控制台事件保留条数
        "recent_dialogs_max": 20,             # 最近对话框交互记录保留条数
        "websocket_max_size": 10_485_760,     # CDP WebSocket 单条消息最大字节数（10MB）
    },
    # ── 内部缓存 TTL（秒） ────────────────────────────────────
    "internal": {
        "policy_cache_ttl": 30.0,             # 网站访问策略缓存有效期（避免频繁读磁盘）
        "check_fn_ttl": 30.0,                 # 工具 check_fn 探测结果缓存有效期
        "skills_index_cache_ttl": 3600,       # 技能索引缓存有效期（1 小时，减少目录扫描）
        "tirith_marker_ttl": 86400,           # Tirith 安全扫描标记缓存有效期（24 小时）
    },
    # ── 外部集成 URL ──────────────────────────────────────────
    "integrations": {
        "graph_base_url": "https://graph.microsoft.com/v1.0",  # Microsoft Graph API 基础 URL
        "homeassistant_url": "http://homeassistant.local:8123", # Home Assistant 智能家居地址
        "osv_api_url": "https://api.osv.dev/v1/query",         # OSV 开源漏洞数据库 API 地址
    },
    # ── 会话重置策略 ──────────────────────────────────────────
    "session_reset": {
        "idle_timeout_minutes": 60,           # 空闲超过 N 分钟后自动重置会话
        "max_messages": 500,                  # 会话消息数超过 N 条后触发重置
        "max_tokens": 100_000,                # 会话 token 数超过 N 后触发重置
    },
    # ── 网关配置 ──────────────────────────────────────────────
    "gateway": {
        "agent_cache_size": 32,               # Agent 实例 LRU 缓存上限
        "agent_idle_ttl_seconds": 3600.0,     # Agent 空闲超过 N 秒后从缓存淘汰
        "graceful_shutdown_timeout": 10.0,    # 优雅关闭等待时间（秒），超时强制终止
        "status_debounce_seconds": 2.0,       # 状态通知防抖间隔（秒），避免频繁广播
        "cleanup_interval": 300,              # 定期清理过期 Agent/会话的间隔（秒）
    },
    # ── 钩子输出溢出到磁盘 ────────────────────────────────────
    "hook_outputs": {
        "spill_max_chars": 10_000,            # 钩子输出超过此字符数时写入临时文件
        "spill_preview_head": 500,            # 溢出文件中保留的开头预览字符数
        "spill_preview_tail": 500,            # 溢出文件中保留的结尾预览字符数
    },
    # ── LSP 代码智能 ──────────────────────────────────────────────
    "lsp": {
        "auto_install": "auto",               # LSP 服务器自动安装策略: auto|manual|off
        "idle_timeout": 600,                  # LSP 服务器空闲超时回收时间（秒）
        "enabled": True,                      # 是否启用独立 LSP（非 VSCode 模式下）
    },
}


# ---------------------------------------------------------------------------
# Provider → base_url 自动解析映射
# ---------------------------------------------------------------------------

PROVIDER_BASE_URLS: Dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://127.0.0.1:11434/v1",
    "lmstudio": "http://127.0.0.1:1234/v1",
    "vllm": "http://127.0.0.1:8000/v1",
    "llamacpp": "http://127.0.0.1:8080/v1",
    "together": "https://api.together.xyz/v1",
    "deepinfra": "https://api.deepinfra.com/v1/openai",
    "groq": "https://api.groq.com/openai/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "minimax": "https://api.minimaxi.com/v1",
}

# Provider → 环境变量名（API key 查找）
PROVIDER_KEY_ENV: Dict[str, list] = {
    "openai": ["OPENAI_API_KEY"],
    "openrouter": ["OPENROUTER_API_KEY", "OPENAI_API_KEY"],
    "ollama": [],  # 本地无需 key
    "lmstudio": ["LM_API_KEY"],  # 可选
    "anthropic": ["ANTHROPIC_API_KEY"],
    "gemini": ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
    "groq": ["GROQ_API_KEY"],
    "together": ["TOGETHER_API_KEY"],
    "deepinfra": ["DEEPINFRA_API_KEY"],
    "minimax": ["MINIMAX_API_KEY"],
}


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def get_config_path() -> Path:
    """获取主配置文件路径。"""
    return SPIRIT_HOME / "config.yaml"


def get_env_path() -> Path:
    """获取 .env 文件路径（API 密钥）。"""
    return SPIRIT_HOME / ".env"


# ---------------------------------------------------------------------------
# 生效配置（DEFAULT_CONFIG ← YAML ← 环境变量，带缓存）
# ---------------------------------------------------------------------------
# v0.3.5：之前 get_config_value() 只读 DEFAULT_CONFIG，~/.spirit/config.yaml 里
# 写的 context_length / max_retries / protect_first_n / max_tokens 等全部不生效。
# 这里加载"默认值 ← YAML ← SPIRIT_ 环境变量"的合并视图（**不**跑 failover 探测，
# 避免在 import 期发网络请求），供 get_config_value() 使用。
# 测试进程（argv 含 pytest）不读用户 YAML，保证套件确定性 —— 与 storage 层
# _running_under_pytest 的隔离策略一致。

_EFFECTIVE_CACHE: Dict[str, Any] = {"path": None, "yaml": None}


def _running_under_pytest() -> bool:
    try:
        import sys
        return "pytest" in sys.argv[0] or any("pytest" in a for a in sys.argv)
    except Exception:  # noqa: BLE001
        return False


def _load_yaml_overlay() -> Dict[str, Any]:
    """读取 YAML 覆盖层（进程内缓存；env 不缓存，保持对 monkeypatch 敏感）。"""
    cfg_path = get_config_path()
    cached = _EFFECTIVE_CACHE["yaml"]
    if (
        cached is not None
        and _EFFECTIVE_CACHE["path"] == str(cfg_path)
    ):
        return copy.deepcopy(cached)

    overlay: Dict[str, Any] = {}
    if not _running_under_pytest():
        try:
            file_config = _load_yaml(cfg_path)
            _deep_merge(overlay, file_config)
        except Exception as exc:  # noqa: BLE001
            logger.debug("YAML 读取失败，忽略用户配置: %s", exc)

    _EFFECTIVE_CACHE["path"] = str(cfg_path)
    _EFFECTIVE_CACHE["yaml"] = copy.deepcopy(overlay)
    return copy.deepcopy(overlay)


def load_effective_config(*, force: bool = False) -> Dict[str, Any]:
    """返回 DEFAULT_CONFIG ← YAML ← 环境变量的合并配置。

    与 :func:`load_config` 的区别：不执行 failover 探测 / provider 解析（那两步
    可能发网络请求且会改写主连接字段），只做静态合并。

    关键语义：
    - 以**活体** ``DEFAULT_CONFIG`` 为基底（测试 monkeypatch 直接改
      DEFAULT_CONFIG 的键能立即被看到）；
    - YAML 覆盖层按配置文件路径缓存；
    - SPIRIT_* 环境变量每次实时合并（对 monkeypatch.setenv 敏感）；
    - **测试进程（argv 含 pytest）不读 YAML 且不缓存** —— 各测试会把
      ``config.SPIRIT_HOME`` monkeypatch 到不同 tmp_path，跨测试共享缓存会把
      一个 HOME 的配置漏给另一个（全量跑时的串味问题）；
    - 任何失败都回退 DEFAULT_CONFIG，绝不抛异常。

    Args:
        force: 跳过 YAML 缓存重新加载（非测试进程的配置热更新用）

    Returns:
        合并后的配置字典（深拷贝，调用方可安全修改）
    """
    config = copy.deepcopy(DEFAULT_CONFIG)
    try:
        if _running_under_pytest():
            # 测试：只叠加实时环境变量，不读 YAML、不缓存（确定性 + 隔离）
            _deep_merge(config, _load_env_config())
            return config

        if force:
            _EFFECTIVE_CACHE["yaml"] = None
        _deep_merge(config, _load_yaml_overlay())
        _deep_merge(config, _load_env_config())
    except Exception as exc:  # noqa: BLE001 - 配置读取失败必须回退默认，不能崩热路径
        logger.debug("effective config 加载失败，回退 DEFAULT_CONFIG: %s", exc)
        config = copy.deepcopy(DEFAULT_CONFIG)
    return config


def reload_effective_config() -> Dict[str, Any]:
    """清空缓存并重载生效配置（测试 / 配置变更后调用）。"""
    return load_effective_config(force=True)


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

def load_config(
    config_path: str = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """加载配置。

    优先级：overrides > 环境变量 > YAML 文件 > DEFAULT_CONFIG

    Args:
        config_path: 配置文件路径（默认 ~/.spirit/config.yaml）
        **overrides: 参数覆盖（支持嵌套 dict，如 llm={"model": "gpt-4o"}）

    Returns:
        合并后的配置字典（深拷贝，修改不影响缓存）
    """
    # 1. 从默认配置开始
    config = copy.deepcopy(DEFAULT_CONFIG)

    # 2. 合并 YAML 文件
    path = Path(config_path) if config_path else get_config_path()
    file_config = _load_yaml(path)
    _deep_merge(config, file_config)

    # 3. 合并环境变量
    env_config = _load_env_config()
    _deep_merge(config, env_config)

    # 4. 合并参数覆盖
    if overrides:
        _deep_merge(config, overrides)

    # 5. 多 provider failover：探测选中“有 token”的 provider（若启用）
    _apply_failover(config)

    # 6. 自动解析 provider → base_url / api_key
    _resolve_provider(config)

    return config


def _load_yaml(path: Path) -> Dict[str, Any]:
    """加载 YAML 配置文件。"""
    if not path.exists():
        return {}
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            logger.warning("配置文件格式错误: %s", path)
            return {}
        logger.debug("配置已加载: %s", path)
        return data
    except ImportError:
        logger.warning("yaml 包未安装，跳过配置文件: %s", path)
        return {}
    except Exception as e:
        logger.warning("配置文件加载失败: %s — %s", path, e)
        return {}


def _load_env_config() -> Dict[str, Any]:
    """从环境变量加载配置（SPIRIT_ 前缀）。"""
    config: Dict[str, Any] = {}

    # 映射: 环境变量后缀 → (配置路径, 类型转换)
    mapping = {
        "MODEL": ("llm.model", str),
        "PROVIDER": ("llm.provider", str),
        "API_KEY": ("llm.api_key", str),
        "BASE_URL": ("llm.base_url", str),
        "CONTEXT_LENGTH": ("llm.context_length", int),
        "MAX_TOKENS": ("llm.max_tokens", int),
        "MAX_ITERATIONS": ("agent.max_iterations", int),
        "REASONING_EFFORT": ("agent.reasoning_effort", str),
        "VERBOSE": ("agent.verbose", _str_to_bool),
        "COMPRESSION_ENABLED": ("compression.enabled", _str_to_bool),
        "COMPRESSION_THRESHOLD": ("compression.threshold", float),
        "STREAMING_ENABLED": ("streaming.enabled", _str_to_bool),
    }

    for suffix, (dotted_key, converter) in mapping.items():
        env_key = f"SPIRIT_{suffix}"
        raw = os.environ.get(env_key)
        if raw is not None:
            try:
                value = converter(raw)
                _set_nested(config, dotted_key, value)
            except (ValueError, TypeError) as e:
                logger.debug("环境变量 %s 转换失败: %s", env_key, e)

    return config


def _apply_failover(config: Dict[str, Any]) -> None:
    """若启用多 provider failover，探测选中“有 token”的 provider 覆盖 llm 连接字段。

    “哪个有 token 用哪个”：按 llm.failover.providers 优先级顺序探测，用第一个健康的。
    非致命：选择器缺失/异常/无健康 provider 时保持配置原样（回退 llm 顶层默认）。
    探测结果被 llm_pool 缓存 CACHE_TTL 秒，热路径反复 load_config 不会重复发请求。
    """
    llm = config.get("llm", {})
    fo = llm.get("failover", {}) or {}
    if not fo.get("enabled", False):
        return
    try:
        from spirit.llm_pool import pick_provider
        picked = pick_provider()
    except Exception as exc:  # noqa: BLE001 - failover 不可用绝不阻断配置加载
        logger.debug("[failover] 选择器不可用，保持默认: %s", exc)
        return
    if not picked:
        return
    # 用选中的健康 provider 覆盖连接字段
    llm["model"] = picked.get("model") or llm.get("model")
    llm["provider"] = picked.get("provider") or llm.get("provider")
    llm["api_key"] = picked.get("api_key") or llm.get("api_key")
    llm["base_url"] = picked.get("base_url") or llm.get("base_url")
    logger.info("[failover] 生效 provider=%s model=%s", picked.get("name"), llm.get("model"))


def _resolve_provider(config: Dict[str, Any]) -> None:
    """根据 provider 自动解析 base_url 和 api_key。

    主路径走 ProviderProfile 注册表（``spirit.providers``）——base_url 取
    ``profile.base_url``、api_key 按 ``profile.env_vars`` 顺序查环境变量。注册表无
    profile 时回退到模块级 ``PROVIDER_BASE_URLS`` / ``PROVIDER_KEY_ENV`` 字典（如
    ollama/lmstudio 等本地端点的默认 URL 仍在字典里）。仅在用户未显式设置时填充，
    不覆盖已有值。
    """
    llm = config.get("llm", {})
    provider = (llm.get("provider") or "auto").strip().lower()

    if provider in ("auto", ""):
        return

    # 从注册表取 profile（不可用则回退字典，绝不阻断配置加载）
    profile = None
    try:
        from spirit.providers import get_provider_profile
        profile = get_provider_profile(provider)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[provider] 注册表不可用，回退字典: %s", exc)

    # 自动填充 base_url：profile 优先，字典兜底
    if not llm.get("base_url"):
        if profile and profile.base_url:
            llm["base_url"] = profile.base_url
        elif provider in PROVIDER_BASE_URLS:
            llm["base_url"] = PROVIDER_BASE_URLS[provider]

    # 自动查找 api_key：profile.env_vars 优先，字典兜底（按序取首个非空环境变量）
    if not llm.get("api_key"):
        env_names = list(profile.env_vars) if profile else []
        env_names += [e for e in PROVIDER_KEY_ENV.get(provider, []) if e not in env_names]
        for env_name in env_names:
            key = os.environ.get(env_name, "")
            if key:
                llm["api_key"] = key
                logger.debug("API key 从 %s 自动解析", env_name)
                break


# ---------------------------------------------------------------------------
# 保存
# ---------------------------------------------------------------------------

def save_config(data: Dict[str, Any], config_path: str = None) -> None:
    """保存配置到 YAML 文件。

    Args:
        data: 配置数据
        config_path: 文件路径（默认 ~/.spirit/config.yaml）
    """
    path = Path(config_path) if config_path else get_config_path()

    try:
        import yaml
        path.parent.mkdir(parents=True, exist_ok=True)
        # 写入临时文件再原子替换
        tmp_path = path.with_suffix(".yaml.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                data, f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
        tmp_path.replace(path)
        logger.debug("配置已保存: %s", path)
    except ImportError:
        logger.error("yaml 包未安装，无法保存配置")
    except Exception as e:
        logger.error("配置保存失败: %s", e)


# ---------------------------------------------------------------------------
# 便捷方法
# ---------------------------------------------------------------------------

def get_model_config(config_path: str = None) -> Dict[str, Any]:
    """获取 LLM 配置节。

    Returns:
        {"model": ..., "provider": ..., "api_key": ..., "base_url": ..., ...}
    """
    cfg = load_config(config_path=config_path)
    return cfg.get("llm", {})


def get_spirit_home() -> Path:
    """获取 SPIRIT_HOME 目录（确保存在）。"""
    SPIRIT_HOME.mkdir(parents=True, exist_ok=True)
    return SPIRIT_HOME


def get_config_value(dotted_key: str, default: Any = None) -> Any:
    """通过点号路径读取**生效**配置值。

    v0.3.5：之前只读 DEFAULT_CONFIG，~/.spirit/config.yaml 里写的键全部不生效。
    现在读 DEFAULT_CONFIG ← YAML ← 环境变量的合并视图（基底是活体
    DEFAULT_CONFIG，测试 monkeypatch 直接改 DEFAULT_CONFIG 依然生效）。

    例: get_config_value("timeouts.terminal_default") → 120
    """
    try:
        value: Any = load_effective_config()
    except Exception:  # noqa: BLE001 - 任何异常都退回 DEFAULT_CONFIG
        value = copy.deepcopy(DEFAULT_CONFIG)
    for k in dotted_key.split("."):
        if isinstance(value, dict) and k in value:
            value = value[k]
        else:
            return default
    return value


# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------

def _deep_merge(base: Dict, override: Dict) -> None:
    """将 override 深度合并到 base（就地修改 base）。

    - dict + dict → 递归合并
    - 其他类型 → override 覆盖 base
    """
    for key, value in override.items():
        if (
            key in base
            and isinstance(base[key], dict)
            and isinstance(value, dict)
        ):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _set_nested(d: Dict, dotted_key: str, value: Any) -> None:
    """通过点号分隔的 key 设置嵌套字典值。

    例: _set_nested({}, "llm.model", "gpt-4o")
    → {"llm": {"model": "gpt-4o"}}
    """
    keys = dotted_key.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def _str_to_bool(value: str) -> bool:
    """字符串转布尔值。"""
    return value.strip().lower() in ("true", "1", "yes", "on")


__all__ = [
    "SPIRIT_HOME",
    "DEFAULT_CONFIG",
    "PROVIDER_BASE_URLS",
    "PROVIDER_KEY_ENV",
    "get_config_path",
    "get_env_path",
    "load_config",
    "save_config",
    "get_model_config",
    "get_spirit_home",
    "get_config_value",
]
