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
    # ── Prompt caching（Anthropic 风格 cache_control） ─────────
    "prompt_caching": {
        "enabled": True,                      # 是否启用 prompt caching（减少 ~75% 输入 token 成本）
        "ttl": "5m",                          # 缓存有效期: '5m' 或 '1h'（Anthropic 支持两种）
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
        "delegate_default": 300,              # 子 Agent 委派任务默认超时
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
        "chat_request": 300.0,                # WebSocket 聊天请求超时（默认 5 分钟）
        "stt_transcribe": 30.0,               # 语音识别（STT）超时（默认 30 秒）
        "tts_synthesize": 30.0,               # 语音合成（TTS）超时（默认 30 秒）
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
        "base_url": "http://127.0.0.1:8080", # Memora 后端服务地址
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

    # 5. 自动解析 provider → base_url / api_key
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


def _resolve_provider(config: Dict[str, Any]) -> None:
    """根据 provider 自动解析 base_url 和 api_key。

    仅在用户未显式设置时填充，不覆盖已有值。
    """
    llm = config.get("llm", {})
    provider = (llm.get("provider") or "auto").strip().lower()

    if provider in ("auto", ""):
        return

    # 自动填充 base_url
    if not llm.get("base_url") and provider in PROVIDER_BASE_URLS:
        llm["base_url"] = PROVIDER_BASE_URLS[provider]

    # 自动查找 api_key
    if not llm.get("api_key"):
        for env_name in PROVIDER_KEY_ENV.get(provider, []):
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
    """通过点号路径读取配置值。

    例: get_config_value("timeouts.terminal_default") → 120
    无需加载 YAML — 直接读 DEFAULT_CONFIG（模块级缓存）。
    调用方可按需覆盖。
    """
    keys = dotted_key.split(".")
    value: Any = DEFAULT_CONFIG
    for k in keys:
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
