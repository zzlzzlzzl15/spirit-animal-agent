"""Spirit 本地代理服务器 —— OpenAI 兼容端点。

对标 Hermes ``.plans/openai-api-server.md``（``POST /v1/chat/completions`` +
``GET /v1/models`` + ``GET /health`` + SSE 流式 + Bearer 鉴权），按 Spirit「精简可
测试子集」约定实现，**纯标准库**（``http.server``，不引入 aiohttp/fastapi）。

让任意 OpenAI 兼容前端（Open WebUI / LobeChat / NextChat…）把 Spirit agent 当后端：
指向 ``http://127.0.0.1:8642/v1`` 即可。

分层（可测试性优先）：

- :mod:`spirit.proxy.config` —— :class:`ProxyConfig` + env/config.yaml 解析。
- :mod:`spirit.proxy.auth` —— Bearer token 校验（常量时间比较，纯函数）。
- :mod:`spirit.proxy.openai_format` —— OpenAI 线格式构造器（纯函数）。
- :mod:`spirit.proxy.completion` —— messages → Spirit agent 调用 seam（可注入）。
- :mod:`spirit.proxy.handler` —— :class:`ProxyHandler`：传输无关核心分发（离线可测）。
- :mod:`spirit.proxy.server` —— :class:`ProxyServer`：stdlib HTTP 薄适配 + 生命周期。

一站式::

    from spirit.proxy import ProxyServer
    server = ProxyServer()   # 读 SPIRIT_PROXY_* / config.yaml
    server.start()           # 后台线程；serve_forever() 则前台阻塞
"""

from spirit.proxy.config import (
    DEFAULT_HOST,
    DEFAULT_MODEL,
    DEFAULT_PORT,
    ProxyConfig,
    load_proxy_config,
)
from spirit.proxy.auth import extract_bearer, is_authorized, verify_token
from spirit.proxy.completion import (
    content_to_text,
    default_completion_fn,
    extract_system_prompt,
    messages_to_user_prompt,
)
from spirit.proxy.handler import ProxyHandler, ProxyResponse
from spirit.proxy.openai_format import (
    build_chat_completion,
    build_error,
    build_models_list,
    build_sse_chunks,
    build_usage,
    estimate_tokens,
)
from spirit.proxy.server import ProxyServer

__all__ = [
    # config
    "ProxyConfig",
    "load_proxy_config",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_MODEL",
    # auth
    "extract_bearer",
    "verify_token",
    "is_authorized",
    # completion seam
    "content_to_text",
    "extract_system_prompt",
    "messages_to_user_prompt",
    "default_completion_fn",
    # handler
    "ProxyHandler",
    "ProxyResponse",
    # format
    "build_chat_completion",
    "build_models_list",
    "build_sse_chunks",
    "build_usage",
    "build_error",
    "estimate_tokens",
    # server
    "ProxyServer",
]
