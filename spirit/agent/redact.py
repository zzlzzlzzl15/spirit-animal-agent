"""基于正则的密钥脱敏 —— 用于日志与工具输出。

移植自 Hermes ``agent/redact.py``（精简可测试子集）。在文本落到日志文件、
verbose 输出或网关日志之前，用模式匹配遮蔽 API key、token 与凭据。

短 token（< floor）整体遮蔽；长 token 保留首尾若干字符以便排查。

设计约定：
- **零 Spirit 依赖**：仅用标准库，可在任何上下文安全导入（含 agent 启动早期）。
- **默认开启**：``SPIRIT_REDACT_SECRETS`` 环境变量在导入时快照，运行期改环境
  变量无法中途关闭脱敏（安全默认）。``force=True`` 用于绝不能吐原始密钥的边界。
- **fail-safe**：任何非匹配文本原样返回，绝不抛异常。
"""

import logging
import os
import re
import shlex

logger = logging.getLogger(__name__)

# 敏感的 query-string 参数名（大小写不敏感、精确匹配）。
# 捕获那些值不匹配任何已知厂商前缀正则的 token（不透明 token、短 OAuth code）。
_SENSITIVE_QUERY_PARAMS = frozenset({
    "access_token", "refresh_token", "id_token", "token", "api_key", "apikey",
    "client_secret", "password", "auth", "jwt", "session", "secret", "key",
    "code",           # OAuth 授权码
    "signature",      # 预签名 URL 签名
    "x-amz-signature",
})

# 敏感的 form-urlencoded / JSON body key 名（大小写不敏感、精确匹配，非子串）。
# "token_count" / "session_id" 不应匹配。
_SENSITIVE_BODY_KEYS = frozenset({
    "access_token", "refresh_token", "id_token", "token", "api_key", "apikey",
    "client_secret", "password", "auth", "jwt", "secret", "private_key",
    "authorization", "key",
})

# 导入时快照，运行期环境变量改动无法中途关闭脱敏。默认 ON（安全默认）。
# 需要原始凭据值的场景（如开发脱敏器本身）可设 ``SPIRIT_REDACT_SECRETS=false``。
_REDACT_ENABLED = os.getenv("SPIRIT_REDACT_SECRETS", "true").lower() in {"1", "true", "yes", "on"}

# 已知 API key 前缀 —— 匹配前缀 + 连续 token 字符
_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",           # OpenAI / OpenRouter / Anthropic (sk-ant-*)
    r"ghp_[A-Za-z0-9]{10,}",            # GitHub PAT (classic)
    r"github_pat_[A-Za-z0-9_]{10,}",    # GitHub PAT (fine-grained)
    r"gho_[A-Za-z0-9]{10,}",            # GitHub OAuth access token
    r"ghu_[A-Za-z0-9]{10,}",            # GitHub user-to-server token
    r"ghs_[A-Za-z0-9]{10,}",            # GitHub server-to-server token
    r"ghr_[A-Za-z0-9]{10,}",            # GitHub refresh token
    r"xapp-\d+-[A-Za-z0-9-]{10,}",      # Slack app-Level token
    r"xox[baprs]-[A-Za-z0-9-]{10,}",    # Slack bot/app/user tokens
    r"AIza[A-Za-z0-9_-]{30,}",          # Google API keys
    r"pplx-[A-Za-z0-9]{10,}",           # Perplexity
    r"fal_[A-Za-z0-9_-]{10,}",          # Fal.ai
    r"fc-[A-Za-z0-9]{10,}",             # Firecrawl
    r"bb_live_[A-Za-z0-9_-]{10,}",      # BrowserBase
    r"gAAAA[A-Za-z0-9_=-]{20,}",        # 加密 token
    r"AKIA[A-Z0-9]{16}",                # AWS Access Key ID
    r"sk_live_[A-Za-z0-9]{10,}",        # Stripe secret key (live)
    r"sk_test_[A-Za-z0-9]{10,}",        # Stripe secret key (test)
    r"rk_live_[A-Za-z0-9]{10,}",        # Stripe restricted key
    r"SG\.[A-Za-z0-9_-]{10,}",          # SendGrid API key
    r"hf_[A-Za-z0-9]{10,}",             # HuggingFace token
    r"r8_[A-Za-z0-9]{10,}",             # Replicate API token
    r"npm_[A-Za-z0-9]{10,}",            # npm access token
    r"pypi-[A-Za-z0-9_-]{10,}",         # PyPI API token
    r"dop_v1_[A-Za-z0-9]{10,}",         # DigitalOcean PAT
    r"doo_v1_[A-Za-z0-9]{10,}",         # DigitalOcean OAuth
    r"sk_[A-Za-z0-9_]{10,}",            # ElevenLabs TTS key (sk_ 下划线)
    r"tvly-[A-Za-z0-9]{10,}",           # Tavily search API key
    r"exa_[A-Za-z0-9]{10,}",            # Exa search API key
    r"gsk_[A-Za-z0-9]{10,}",            # Groq Cloud API key
    r"xai-[A-Za-z0-9]{30,}",            # xAI (Grok) API key
    r"ntn_[A-Za-z0-9]{10,}",            # Notion internal integration token
    r"fw-[A-Za-z0-9]{30,}",             # Fireworks AI API key
    r"fw_[A-Za-z0-9]{30,}",             # Fireworks AI API key
]

# ENV 赋值模式：KEY=value，KEY 含密钥样名称。全大写 key 容忍 "=" 两侧空格。
_SECRET_ENV_NAMES = r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)"
_ENV_ASSIGN_RE = re.compile(
    rf"([A-Z0-9_]{{0,50}}{_SECRET_ENV_NAMES}[A-Z0-9_]{{0,50}})\s*=\s*(['\"]?)(\S+)\2",
)

# 小写 / 点分 / 连字符配置文件 key（application.properties、.env、YAML dump）。
_SECRET_CFG_NAMES = r"(?:api[ _.\-]?key|token|secret|passwd|password|credential|auth)"
_CFG_VALUE = r"(['\"]?)([^\s&]+?)\2(?=[\s&]|$)"

# 程序化 env 查找（os.getenv(...) 等）引用变量*名*而非密钥值 —— 跳过脱敏。
_ENV_LOOKUP_VALUE_RE = re.compile(
    r"^(?:os\.(?:getenv|environ)|process\.env|\$ENV\{)"
)
# 命名空间（点分）key：密钥词可出现在点路径任意处。
_CFG_DOTTED_RE = re.compile(
    rf"((?:[A-Za-z0-9_\-]+\.)+[A-Za-z0-9_.\-]*{_SECRET_CFG_NAMES}[A-Za-z0-9_.\-]*"
    rf"|[A-Za-z0-9_.\-]*{_SECRET_CFG_NAMES}[A-Za-z0-9_.\-]*\.[A-Za-z0-9_.\-]+)"
    rf"={_CFG_VALUE}",
    re.IGNORECASE,
)
# 行首锚定的裸 key：``password=…`` / ``export api_key=…``。
_CFG_ANCHORED_RE = re.compile(
    rf"(^[ \t]*(?:export[ \t]+)?[A-Za-z0-9_\-]*{_SECRET_CFG_NAMES}[A-Za-z0-9_\-]*)={_CFG_VALUE}",
    re.IGNORECASE | re.MULTILINE,
)

# 无引号 YAML / 冒号配置（``password: secret``）。密钥词必须是 KEY 的一部分，
# 值为单个无空白 token —— 散文 ``note: secret meeting`` 不受影响。
_YAML_CFG_NAMES = r"(?:api[ _.\-]?key|token|secret|passwd|password|credential)"
_YAML_ASSIGN_RE = re.compile(
    rf"(^[ \t]*[A-Za-z0-9_.\-]*{_YAML_CFG_NAMES}[A-Za-z0-9_.\-]*)(:[ \t]*)(?!['\"])([^\s&]+)",
    re.IGNORECASE | re.MULTILINE,
)

# JSON 字段："apiKey": "value" 等。
_JSON_KEY_NAMES = r"(?:api_?[Kk]ey|token|secret|password|access_token|refresh_token|auth_token|bearer|secret_value|raw_secret|key_material)"
_JSON_FIELD_RE = re.compile(
    rf'("{_JSON_KEY_NAMES}")\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)

# Authorization 头 —— 任意 scheme（Bearer/Basic/Token/Digest）+ 裸凭据 + Proxy-Authorization。
# 凭据类排除引号，避免把闭合引号吞进匹配导致语法损坏。
_AUTH_HEADER_RE = re.compile(
    r"((?:Proxy-)?Authorization:\s*)([A-Za-z][\w.+-]*\s+)?([^\s\"']+)",
    re.IGNORECASE,
)

# API-key 样认证头（x-api-key 等），携带单个不透明值（无 scheme 词）。
_SECRET_HEADER_NAMES = (
    r"(?:x-api-key|x-goog-api-key|api-key|apikey|x-api-token|x-auth-token|x-access-token)"
)
_SECRET_HEADER_RE = re.compile(
    rf"({_SECRET_HEADER_NAMES}\s*:\s*)(\S+)",
    re.IGNORECASE,
)

# Telegram bot token：bot<digits>:<token> 或 <digits>:<token>，token >= 30。
_TELEGRAM_RE = re.compile(
    r"(bot)?(\d{8,}):([-A-Za-z0-9_]{30,})",
)

# 私钥块：-----BEGIN ... PRIVATE KEY----- ... -----END ... PRIVATE KEY-----
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
)

# 数据库连接串：protocol://user:PASSWORD@host（postgres/mysql/mongodb/redis/amqp）。
# userinfo 与 password 组禁止空白，匹配永不跨行。
_DB_CONNSTR_RE = re.compile(
    r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:\s]+:)([^@\s]+)(@)",
    re.IGNORECASE,
)

# web/transport URL 中的裸 token 凭据：``scheme://TOKEN@host``（无 user:pass 冒号）。
_URL_BARE_TOKEN_RE = re.compile(
    r"((?:https?|wss?|git|ssh|ftp|ftps|sftp)://)"  # scheme
    r"([^\s:@/]{8,})"                               # 裸 token（无冒号/斜杠/@），8+ 字符
    r"(@[^\s]+)",                                   # @host...
    re.IGNORECASE,
)

# JWT：header.payload[.signature] —— 总是以 "eyJ" 开头（base64 的 "{"）。
_JWT_RE = re.compile(
    r"eyJ[A-Za-z0-9_-]{10,}"           # Header
    r"(?:\.[A-Za-z0-9_=-]{4,}){0,2}"   # 可选 payload / signature
)

# E.164 电话号码：+<国家码><号码>，7-15 位。负向前瞻避免匹配 hex/标识符。
_SIGNAL_PHONE_RE = re.compile(r"(\+[1-9]\d{6,14})(?![A-Za-z0-9])")

# 含 query string 的 URL：``scheme://...?...[# 或结尾]``。
_URL_WITH_QUERY_RE = re.compile(
    r"(https?|wss?|ftp)://"          # scheme
    r"([^\s/?#]+)"                    # authority
    r"([^\s?#]*)"                     # path
    r"\?([^\s#]+)"                    # query（必需）
    r"(#\S*)?",                       # 可选 fragment
)

# 含 userinfo 的 URL：``scheme://user:password@host``（任意 scheme）。
_URL_USERINFO_RE = re.compile(
    r"(https?|wss?|ftp)://([^/\s:@]+):([^/\s@]+)@",
)

# HTTP 访问日志的相对请求目标：``"POST /webhook?password=... HTTP/1.1"``。
_HTTP_REQUEST_TARGET_QUERY_RE = re.compile(
    r"\b((?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE|CONNECT)\s+[^ \t\r\n\"']*?)"
    r"\?([^ \t\r\n\"']+)",
    re.IGNORECASE,
)

# form-urlencoded body 检测：保守 —— 仅当整段文本形如 query string（k=v&k=v，无换行）。
_FORM_BODY_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.-]*=[^&\s]*(?:&[A-Za-z_][A-Za-z0-9_.-]*=[^&\s]*)+$"
)

# 把已知前缀模式编译成一个 alternation
_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join(_PREFIX_PATTERNS) + r")(?![A-Za-z0-9_-])"
)


def mask_secret(
    value,
    *,
    head: int = 4,
    tail: int = 4,
    floor: int = 12,
    placeholder: str = "***",
    empty: str = "",
) -> str:
    """遮蔽密钥用于显示，保留首 ``head`` / 尾 ``tail`` 字符。

    跨 Spirit 的显示时脱敏规范助手 —— config / status / dump 等任何需要在
    保留可排查性的同时隐藏主体的地方都用它。

    Examples:
        >>> mask_secret("sk-proj-abcdef1234567890")
        'sk-p...7890'
        >>> mask_secret("short")                         # 整体遮蔽
        '***'
        >>> mask_secret("")                              # 空默认
        ''
        >>> mask_secret("", empty="(not set)")           # 空覆盖
        '(not set)'
    """
    if not value:
        return empty
    if len(value) < floor:
        return placeholder
    return f"{value[:head]}...{value[-tail:]}"


def _mask_token(token: str) -> str:
    """遮蔽日志 token —— 保守 18 字符下限，保留 6 前缀 / 4 后缀。"""
    if not token:
        return "***"
    return mask_secret(token, head=6, tail=4, floor=18)


def _redact_query_string(query: str) -> str:
    """遮蔽 URL query string 中敏感参数的值。

    处理 ``k=v&k=v``。敏感 key（大小写不敏感）值替换为 ``***``，其余原样通过。
    """
    if not query:
        return query
    parts = []
    for pair in query.split("&"):
        if "=" not in pair:
            parts.append(pair)
            continue
        key, _, value = pair.partition("=")
        if key.lower() in _SENSITIVE_QUERY_PARAMS:
            parts.append(f"{key}=***")
        else:
            parts.append(pair)
    return "&".join(parts)


def _redact_url_query_params(text: str) -> str:
    """扫描文本中带 query string 的 URL 并遮蔽敏感参数。"""
    def _sub(m):
        scheme = m.group(1)
        authority = m.group(2)
        path = m.group(3)
        query = _redact_query_string(m.group(4))
        fragment = m.group(5) or ""
        return f"{scheme}://{authority}{path}?{query}{fragment}"
    return _URL_WITH_QUERY_RE.sub(_sub, text)


def _redact_url_userinfo(text: str) -> str:
    """剥离 HTTP/WS/FTP URL 的 ``user:password@``。DB 协议由 ``_DB_CONNSTR_RE`` 处理。"""
    return _URL_USERINFO_RE.sub(
        lambda m: f"{m.group(1)}://{m.group(2)}:***@",
        text,
    )


def redact_cdp_url(value) -> str:
    """在记录日志前遮蔽 CDP / 浏览器端点 URL 中的密钥。

    全局 ``redact_sensitive_text`` 有意放过 web-URL 的 query 参数与 ``user:pass@``
    userinfo（OAuth 回调、magic-link / 预签名 URL 是 agent 应跟随的工作流）。
    CDP 发现端点不是这类工作流：其 query token 与 userinfo 密码是纯凭据，
    绝不能进日志，故对 CDP URL 显式启用这两个 URL 脱敏器。
    """
    text = redact_sensitive_text("" if value is None else str(value))
    if not text:
        return text
    text = _redact_url_query_params(text)
    text = _redact_url_userinfo(text)
    return text


def _redact_http_request_target_query_params(text: str) -> str:
    """遮蔽 HTTP 访问日志请求目标中的敏感 query 参数。"""
    def _sub(m):
        prefix = m.group(1)
        query = _redact_query_string(m.group(2))
        return f"{prefix}?{query}"
    return _HTTP_REQUEST_TARGET_QUERY_RE.sub(_sub, text)


def _redact_form_body(text: str) -> str:
    """遮蔽 form-urlencoded body 中的敏感值。仅当整段输入形似纯 form body 时触发。"""
    if not text or "\n" in text or "&" not in text:
        return text
    if not _FORM_BODY_RE.match(text.strip()):
        return text
    return _redact_query_string(text.strip())


def _mask_token_nonreusable(token: str) -> str:
    """把前缀匹配的凭据遮蔽为**不可复用**哨兵。

    与 ``_mask_token``（保留首尾字符，适合永不回灌配置的日志）不同，本函数发出的
    标记：(1) 不会被误认为可用但被截断的 key，故 agent 从配置读到它再写回不会把
    存储凭据损坏成死字符串；(2) 仍不泄露密钥材料（无首尾字符）。保留厂商前缀标签
    以便排查（能分辨是 GitHub PAT 还是 OpenAI key，却看不到任何字节）。
    """
    if not token:
        return "«redacted-secret»"
    label = ""
    for sub in _PREFIX_SUBSTRINGS:
        if token.startswith(sub):
            label = sub
            break
    return f"«redacted:{label}…»" if label else "«redacted-secret»"


def redact_sensitive_text(
    text,
    *,
    force: bool = False,
    code_file: bool = False,
    file_read: bool = False,
):
    """对一段文本应用全部脱敏模式。

    对任意字符串安全 —— 非匹配文本原样通过。默认开启，可用
    ``SPIRIT_REDACT_SECRETS=false`` 关闭。``force=True`` 用于绝不能返回原始密钥
    的安全边界，无视全局偏好。

    ``code_file=True``：文本已知是源码时跳过 ENV 赋值与 JSON 字段正则（避免
    ``MAX_TOKENS=***`` 常量、``"apiKey": "test"`` 夹具的误伤）；前缀模式、认证头、
    私钥、DB 连接串、JWT、URL 密钥仍脱敏。

    ``file_read=True``：返回给 agent 的文件*内容*（read_file / search / cat）。密钥
    仍脱敏，但前缀匹配的凭据替换为不可复用哨兵（``«redacted:ghp_…»``）而非保留首尾
    的遮蔽（``ghp_S1...Pn2T``）—— 后者看起来像真实但被截断的 key，agent 从 config
    读到再写回会静默损坏存储凭据。隐含 ``code_file=True``。
    """
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    if not (force or _REDACT_ENABLED):
        return text

    if file_read:
        code_file = True

    # 已知前缀（sk-、ghp_ 等）—— 以子串存在性做廉价预筛
    if _has_known_prefix_substring(text):
        _prefix_sub = _mask_token_nonreusable if file_read else _mask_token
        text = _PREFIX_RE.sub(lambda m: _prefix_sub(m.group(1)), text)

    # ENV 赋值：OPENAI_API_KEY=***（源码文件跳过 —— 误伤）
    if not code_file:
        if "=" in text:
            def _redact_env(m):
                name, quote, value = m.group(1), m.group(2), m.group(3)
                if _ENV_LOOKUP_VALUE_RE.match(value):
                    return m.group(0)
                return f"{name}={quote}{_mask_token(value)}{quote}"
            text = _ENV_ASSIGN_RE.sub(_redact_env, text)
            if "://" not in text:
                text = _CFG_DOTTED_RE.sub(_redact_env, text)
                text = _CFG_ANCHORED_RE.sub(_redact_env, text)

        # JSON 字段："apiKey": "***"（源码文件跳过）
        if ":" in text and '"' in text:
            def _redact_json(m):
                key, value = m.group(1), m.group(2)
                if _ENV_LOOKUP_VALUE_RE.match(value):
                    return m.group(0)
                return f'{key}: "{_mask_token(value)}"'
            text = _JSON_FIELD_RE.sub(_redact_json, text)

        # 无引号 YAML / 冒号配置：password: ***（在 JSON 之后，跳过 URL）
        if ":" in text and "://" not in text:
            def _redact_yaml(m):
                key, sep, value = m.group(1), m.group(2), m.group(3)
                if _ENV_LOOKUP_VALUE_RE.match(value):
                    return m.group(0)
                return f"{key}{sep}{_mask_token(value)}"
            text = _YAML_ASSIGN_RE.sub(_redact_yaml, text)

    # Authorization 头
    if "uthorization" in text or "UTHORIZATION" in text:
        text = _AUTH_HEADER_RE.sub(
            lambda m: m.group(1) + (m.group(2) or "") + _mask_token(m.group(3)),
            text,
        )

    # API-key 样头（x-api-key 等）
    if ":" in text:
        text = _SECRET_HEADER_RE.sub(
            lambda m: m.group(1) + _mask_token(m.group(2)),
            text,
        )

    # Telegram bot token
    if ":" in text:
        def _redact_telegram(m):
            prefix = m.group(1) or ""
            digits = m.group(2)
            return f"{prefix}{digits}:***"
        text = _TELEGRAM_RE.sub(_redact_telegram, text)

    # 私钥块
    if "BEGIN" in text and "-----" in text:
        text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)

    # 数据库连接串密码
    if "://" in text:
        if code_file:
            def _redact_db(m):
                pw = m.group(2)
                if pw.startswith("{") and pw.endswith("}"):
                    return m.group(0)
                return f"{m.group(1)}***{m.group(3)}"
            text = _DB_CONNSTR_RE.sub(_redact_db, text)
        else:
            text = _DB_CONNSTR_RE.sub(lambda m: f"{m.group(1)}***{m.group(3)}", text)

        # web/transport URL 中的裸 token userinfo：``scheme://TOKEN@host``
        text = _URL_BARE_TOKEN_RE.sub(
            lambda m: f"{m.group(1)}{_mask_token(m.group(2))}{m.group(3)}",
            text,
        )

    # JWT token（eyJ...）
    if "eyJ" in text:
        text = _JWT_RE.sub(lambda m: _mask_token(m.group(0)), text)

    # 注意：web-URL 脱敏（query 参数 + userinfo + 访问日志请求目标）有意关闭。
    # 许多合法工作流通过 query string 传不透明 token（magic-link 结账、OAuth 回调、
    # 预签名分享 URL），按名 blanket 遮蔽会打断这些流程。URL 内的已知凭据形状
    # （sk-、ghp_、JWT）仍被上面的 _PREFIX_RE / _JWT_RE 捕获；DB 连接串密码仍被
    # _DB_CONNSTR_RE 捕获。

    # form-urlencoded body（仅在干净 k=v&k=v 输入触发）
    if "&" in text and "=" in text:
        text = _redact_form_body(text)

    # E.164 电话号码（Signal / WhatsApp）
    if "+" in text:
        def _redact_phone(m):
            phone = m.group(1)
            if len(phone) <= 8:
                return phone[:2] + "****" + phone[-2:]
            return phone[:4] + "****" + phone[-4:]
        text = _SIGNAL_PHONE_RE.sub(_redact_phone, text)

    return text


# stdout 是环境变量 dump（KEY=value 行）而非源码的命令。对这些命令，终端输出脱敏
# 必须跑 ENV 赋值 pass（code_file=False），以便无已知厂商前缀的不透明 token 也被遮蔽。
_ENV_DUMP_COMMANDS = frozenset({"env", "printenv", "set", "export", "declare"})


def is_env_dump_command(command) -> bool:
    """若 ``command`` 把环境变量 dump 到 stdout 则返回 True。

    检测 ``env`` / ``printenv`` / ``set`` / ``export`` / ``declare`` 作为管道或序列
    （``;`` / ``&&`` / ``||`` / ``|``）中任一段的首个 token。保守：解析失败或不识别
    返回 False（调用方随后回退到更安全的 code_file=True 路径）。
    """
    if not command or not isinstance(command, str):
        return False
    segments = re.split(r"[|;&]+", command)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        if tokens and tokens[0] in _ENV_DUMP_COMMANDS:
            return True
    return False


def redact_terminal_output(output, command=None, *, force: bool = False):
    """从终端 / 进程 stdout 脱敏密钥。

    所有终端输出面（前台 terminal 结果 + 后台 process poll/log/wait）的统一脱敏策略。
    依据 ``command`` 是否为环境变量 dump 选择 ``code_file``：
    - env-dump 命令 → ``code_file=False``，ENV 赋值 pass 遮蔽不透明 token。
    - 其它（或未知命令）→ ``code_file=True``，避免源码/配置 dump 的误伤。
    """
    if not output:
        return output
    code_file = not is_env_dump_command(command or "")
    return redact_sensitive_text(output, force=force, code_file=code_file)


def _extract_literal_prefix(pattern: str) -> str:
    """返回一个正则模式的前导字面字符（在首个元字符处停止）。

    返回任何匹配都*必须*包含的字面子串，使预筛永不产生假阴性。
    """
    out = []
    for ch in pattern:
        if ch in r"[(\.?*+|{^$":
            break
        out.append(ch)
    return "".join(out)


# 用于 gate ``_PREFIX_RE`` 执行的子串。若输入不含任何一个，前缀正则不可能匹配，跳过。
# 从 ``_PREFIX_PATTERNS`` 在模块加载时自动派生，未来新增前缀不会静默破坏预筛。
_PREFIX_SUBSTRINGS = tuple(
    s for s in (_extract_literal_prefix(p) for p in _PREFIX_PATTERNS) if s
)


def _has_known_prefix_substring(text: str) -> bool:
    """廉价预筛：text 是否含任一已知前缀字面子串。"""
    return any(p in text for p in _PREFIX_SUBSTRINGS)


__all__ = [
    "mask_secret",
    "redact_sensitive_text",
    "redact_cdp_url",
    "redact_terminal_output",
    "is_env_dump_command",
]
