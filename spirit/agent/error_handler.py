"""错误分类与自适应重试 — Spirit Agent 的容错核心。

参考 Hermes 的 error_classifier.py (1647行) + retry_utils.py (155行)。
提供：
- FailoverReason 枚举：结构化错误分类
- classify_api_error()：从异常自动判断错误类型
- IterationBudget：线程安全迭代计数器
- jittered_backoff()：去相关抖动退避
- ErrorRecoveryPlan：根据分类给出恢复策略
"""

from __future__ import annotations

import enum
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# =========================================================================
# 错误分类枚举
# =========================================================================

class FailoverReason(enum.Enum):
    """API 失败原因 — 决定恢复策略。"""

    # 认证/授权
    auth = "auth"                        # 瞬时 401/403 — 刷新/轮换
    auth_permanent = "auth_permanent"    # 刷新后仍失败 — 中止

    # 计费/配额
    billing = "billing"                  # 402 或确认额度耗尽 — 立即切换
    rate_limit = "rate_limit"            # 429 或配额限流 — 退避后重试
    upstream_rate_limit = "upstream_rate_limit"  # 上游模型限流 — 切换模型

    # 服务端
    overloaded = "overloaded"            # 503/529 — 提供者过载，退避
    server_error = "server_error"        # 500/502 — 内部错误，重试

    # 传输层
    timeout = "timeout"                  # 连接/读取超时 — 重建客户端+重试
    ssl_cert_verification = "ssl_cert_verification"  # TLS 证书验证失败

    # 上下文/载荷
    context_overflow = "context_overflow"  # 上下文过大 — 压缩，不是 failover
    payload_too_large = "payload_too_large"  # 413 — 压缩载荷
    image_too_large = "image_too_large"   # 图片过大 — 缩小重试

    # 模型/提供者策略
    model_not_found = "model_not_found"  # 404 或无效模型 — 切换模型
    content_policy_blocked = "content_policy_blocked"  # 安全过滤拒绝 — 不重试
    format_error = "format_error"        # 400 错误请求 — 中止或修复重试

    # 兜底
    unknown = "unknown"                  # 无法分类 — 退避重试


# =========================================================================
# 分类结果
# =========================================================================

@dataclass
class ClassifiedError:
    """结构化错误分类结果，附带恢复提示。"""

    reason: FailoverReason
    status_code: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    message: str = ""
    error_context: Dict[str, Any] = field(default_factory=dict)

    # 恢复动作提示
    retryable: bool = True
    should_compress: bool = False
    should_rotate_credential: bool = False
    should_fallback: bool = False

    @property
    def is_auth(self) -> bool:
        return self.reason in {FailoverReason.auth, FailoverReason.auth_permanent}


# =========================================================================
# 错误模式库
# =========================================================================

_BILLING_PATTERNS = [
    "insufficient credits", "insufficient_quota", "insufficient balance",
    "credit balance", "credits exhausted", "credits have been exhausted",
    "no usable credits", "top up your credits", "payment required",
    "billing hard limit", "exceeded your current quota",
    "account is deactivated", "plan does not include",
    "out of extra usage", "out of funds", "run out of funds",
    "balance_depleted", "model_not_supported_on_free_tier",
    "not available on the free tier",
]

_BILLING_ERROR_CODES = frozenset({
    "insufficient_quota", "billing_not_active", "payment_required",
    "insufficient_credits", "no_usable_credits", "balance_depleted",
    "model_not_supported_on_free_tier",
})

_RATE_LIMIT_PATTERNS = [
    "rate limit", "rate_limit", "too many requests", "throttled",
    "requests per minute", "tokens per minute", "requests per day",
    "try again in", "please retry after", "resource_exhausted",
    "rate increased too quickly", "throttlingexception",
    "too many concurrent requests", "servicequotaexceededexception",
]

_OVERLOADED_PATTERNS = [
    "overloaded", "temporarily overloaded", "service is temporarily overloaded",
    "service may be temporarily overloaded", "server is overloaded",
    "server overloaded", "service overloaded", "service is overloaded",
    "upstream overloaded", "currently overloaded", "at capacity", "over capacity",
]

_USAGE_LIMIT_PATTERNS = [
    "usage limit", "quota", "limit exceeded", "key limit exceeded",
]

_USAGE_LIMIT_TRANSIENT_SIGNALS = [
    "try again", "retry", "temporarily", "shortly", "wait",
]

_CONTEXT_OVERFLOW_PATTERNS = [
    "context length", "context_length", "maximum context",
    "too many tokens", "token limit", "max_tokens",
    "context window", "context too large", "reduce your prompt",
    "reduce the length", "maximum number of tokens",
    "string too long", "input is too long",
    "reduce the number of tokens",
]

_AUTH_PATTERNS = [
    "unauthorized", "invalid api key", "incorrect api key",
    "invalid token", "expired token", "authentication failed",
    "access denied", "forbidden", "invalid credentials",
]

_SSL_PATTERNS = [
    "certificate verify failed", "ssl certificate problem",
    "self signed certificate", "unable to get local issuer certificate",
    "certificate has expired", "ssl/tls",
]


# =========================================================================
# 分类器
# =========================================================================

def _error_text(error: Any) -> str:
    """从异常对象提取最佳文本用于匹配。"""
    parts = [
        error,
        getattr(error, "message", None),
        getattr(error, "body", None),
        getattr(error, "response", None),
    ]
    return " ".join(str(p) for p in parts if p is not None).lower()


def _status_code(error: Any) -> Optional[int]:
    """从异常对象提取 HTTP 状态码。"""
    for attr in ("status_code", "status", "http_status"):
        val = getattr(error, attr, None)
        if isinstance(val, int):
            return val
    resp = getattr(error, "response", None)
    if resp is not None:
        for attr in ("status_code", "status"):
            val = getattr(resp, attr, None)
            if isinstance(val, int):
                return val
    return None


def _has_pattern(text: str, patterns: list[str]) -> bool:
    """检查文本是否包含任一模式。"""
    return any(p in text for p in patterns)


def _has_error_code(error: Any, codes: frozenset[str]) -> bool:
    """检查异常是否携带结构化错误码。"""
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        err = body.get("error", {})
        if isinstance(err, dict):
            code = err.get("code", "")
            if code in codes:
                return True
    text = _error_text(error)
    return any(c in text for c in codes)


def classify_api_error(
    error: Exception,
    *,
    provider: str = "",
    model: str = "",
) -> ClassifiedError:
    """从 API 异常自动分类错误并给出恢复策略。

    优先级链：
    1. SSL 证书验证（确定性，不重试）
    2. 上下文溢出（需压缩，不切换）
    3. 计费耗尽（需切换凭证/提供者）
    4. 速率限制（退避重试）
    5. 服务过载（退避重试，不切换凭证）
    6. 认证错误（瞬时 → 刷新；永久 → 中止）
    7. 模型未找到（切换模型）
    8. 内容策略阻止（不重试）
    9. 格式错误（可修复重试）
    10. 服务端错误（重试）
    11. 超时（重建客户端重试）
    12. 兜底（退避重试）
    """
    status = _status_code(error)
    text = _error_text(error)
    error_name = type(error).__name__.lower()

    # 超时检测
    is_timeout = (
        "timeout" in error_name
        or "timedout" in error_name
        or "timed out" in text
        or isinstance(error, TimeoutError)
    )

    # 1. SSL 证书验证
    if _has_pattern(text, _SSL_PATTERNS) or "sslerror" in error_name:
        return ClassifiedError(
            reason=FailoverReason.ssl_cert_verification,
            status_code=status, provider=provider, model=model,
            message=f"SSL/TLS 证书验证失败: {str(error)[:200]}",
            retryable=False,
        )

    # 2. 上下文溢出
    if _has_pattern(text, _CONTEXT_OVERFLOW_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.context_overflow,
            status_code=status, provider=provider, model=model,
            message="上下文长度超出限制",
            retryable=True, should_compress=True,
        )

    # 3. HTTP 状态码优先路径
    if status == 402:
        return ClassifiedError(
            reason=FailoverReason.billing,
            status_code=status, provider=provider, model=model,
            message="需要付款或额度不足",
            retryable=False, should_rotate_credential=True, should_fallback=True,
        )

    if status == 401 or status == 403:
        # 区分计费 vs 认证
        if _has_pattern(text, _BILLING_PATTERNS) or _has_error_code(error, _BILLING_ERROR_CODES):
            return ClassifiedError(
                reason=FailoverReason.billing,
                status_code=status, provider=provider, model=model,
                message="额度不足或计费问题",
                retryable=False, should_rotate_credential=True, should_fallback=True,
            )
        return ClassifiedError(
            reason=FailoverReason.auth,
            status_code=status, provider=provider, model=model,
            message=f"认证失败 ({status})",
            retryable=True, should_rotate_credential=True,
        )

    if status == 404:
        if "model" in text or "not found" in text:
            return ClassifiedError(
                reason=FailoverReason.model_not_found,
                status_code=status, provider=provider, model=model,
                message="模型未找到",
                retryable=True, should_fallback=True,
            )
        return ClassifiedError(
            reason=FailoverReason.format_error,
            status_code=status, provider=provider, model=model,
            message="资源未找到",
            retryable=False,
        )

    if status == 413:
        return ClassifiedError(
            reason=FailoverReason.payload_too_large,
            status_code=status, provider=provider, model=model,
            message="载荷过大",
            retryable=True, should_compress=True,
        )

    if status == 429:
        # 区分过载 vs 限流
        if _has_pattern(text, _OVERLOADED_PATTERNS):
            return ClassifiedError(
                reason=FailoverReason.overloaded,
                status_code=status, provider=provider, model=model,
                message="服务暂时过载",
                retryable=True,
            )
        return ClassifiedError(
            reason=FailoverReason.rate_limit,
            status_code=status, provider=provider, model=model,
            message="速率限制",
            retryable=True,
        )

    if status in (500, 502):
        return ClassifiedError(
            reason=FailoverReason.server_error,
            status_code=status, provider=provider, model=model,
            message=f"服务端错误 ({status})",
            retryable=True,
        )

    if status in (503, 529):
        return ClassifiedError(
            reason=FailoverReason.overloaded,
            status_code=status, provider=provider, model=model,
            message=f"服务不可用/过载 ({status})",
            retryable=True,
        )

    # 4. 文本模式匹配（无明确状态码时）
    if _has_pattern(text, _BILLING_PATTERNS) or _has_error_code(error, _BILLING_ERROR_CODES):
        return ClassifiedError(
            reason=FailoverReason.billing,
            status_code=status, provider=provider, model=model,
            message="额度/计费问题",
            retryable=False, should_rotate_credential=True, should_fallback=True,
        )

    if _has_pattern(text, _OVERLOADED_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.overloaded,
            status_code=status, provider=provider, model=model,
            message="服务过载",
            retryable=True,
        )

    # 使用限制需要消歧
    if _has_pattern(text, _USAGE_LIMIT_PATTERNS):
        if _has_pattern(text, _USAGE_LIMIT_TRANSIENT_SIGNALS):
            return ClassifiedError(
                reason=FailoverReason.rate_limit,
                status_code=status, provider=provider, model=model,
                message="瞬时使用限制",
                retryable=True,
            )
        return ClassifiedError(
            reason=FailoverReason.billing,
            status_code=status, provider=provider, model=model,
            message="使用限制（可能是计费）",
            retryable=False, should_rotate_credential=True,
        )

    if _has_pattern(text, _RATE_LIMIT_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.rate_limit,
            status_code=status, provider=provider, model=model,
            message="速率限制",
            retryable=True,
        )

    if _has_pattern(text, _CONTEXT_OVERFLOW_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.context_overflow,
            status_code=status, provider=provider, model=model,
            message="上下文溢出",
            retryable=True, should_compress=True,
        )

    if _has_pattern(text, _AUTH_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.auth,
            status_code=status, provider=provider, model=model,
            message="认证错误",
            retryable=True, should_rotate_credential=True,
        )

    if "content" in text and ("policy" in text or "filter" in text or "refusal" in text):
        return ClassifiedError(
            reason=FailoverReason.content_policy_blocked,
            status_code=status, provider=provider, model=model,
            message="内容策略阻止",
            retryable=False,
        )

    # 5. 超时
    if is_timeout:
        return ClassifiedError(
            reason=FailoverReason.timeout,
            status_code=status, provider=provider, model=model,
            message="请求超时",
            retryable=True,
        )

    # 6. 连接错误
    if "connection" in error_name or "connection" in text:
        return ClassifiedError(
            reason=FailoverReason.timeout,
            status_code=status, provider=provider, model=model,
            message="连接错误",
            retryable=True,
        )

    # 7. 兜底
    return ClassifiedError(
        reason=FailoverReason.unknown,
        status_code=status, provider=provider, model=model,
        message=str(error)[:300],
        retryable=True,
    )


# =========================================================================
# 退避策略
# =========================================================================

_jitter_counter = 0
_jitter_lock = threading.Lock()


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
) -> float:
    """计算带抖动的指数退避延迟。

    抖动去相关并发重试，防止多会话同时冲击同一提供者。

    Args:
        attempt: 1-based 重试次数
        base_delay: 基础延迟（秒）
        max_delay: 最大延迟上限（秒）
        jitter_ratio: 抖动比例（0-1）

    Returns:
        等待秒数
    """
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    if exponent >= 63 or base_delay <= 0:
        delay = max_delay
    else:
        delay = min(base_delay * (2 ** exponent), max_delay)

    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    rng = random.Random(seed)
    jitter = rng.uniform(0, jitter_ratio * delay)

    return delay + jitter


def compute_retry_delay(
    classified: ClassifiedError,
    attempt: int,
) -> float:
    """根据错误分类计算重试等待时间。"""
    from spirit.config import get_config_value

    if classified.reason == FailoverReason.overloaded:
        return jittered_backoff(
            attempt,
            base_delay=get_config_value("backoff.overloaded_base", 10.0),
            max_delay=get_config_value("backoff.overloaded_max", 180.0),
        )
    if classified.reason == FailoverReason.rate_limit:
        return jittered_backoff(
            attempt,
            base_delay=get_config_value("backoff.rate_limit_base", 5.0),
            max_delay=get_config_value("backoff.rate_limit_max", 120.0),
        )
    if classified.reason == FailoverReason.server_error:
        return jittered_backoff(
            attempt,
            base_delay=get_config_value("backoff.server_error_base", 3.0),
            max_delay=get_config_value("backoff.server_error_max", 60.0),
        )
    if classified.reason == FailoverReason.timeout:
        return jittered_backoff(
            attempt,
            base_delay=get_config_value("backoff.timeout_base", 5.0),
            max_delay=get_config_value("backoff.timeout_max", 90.0),
        )
    if classified.reason == FailoverReason.auth:
        return jittered_backoff(
            attempt,
            base_delay=get_config_value("backoff.auth_base", 2.0),
            max_delay=get_config_value("backoff.auth_max", 30.0),
        )
    # 默认
    return jittered_backoff(
        attempt,
        base_delay=get_config_value("backoff.default_base", 5.0),
        max_delay=get_config_value("backoff.default_max", 120.0),
    )


# =========================================================================
# 迭代预算
# =========================================================================

class IterationBudget:
    """线程安全迭代计数器。

    每个 agent 持有自己的 IterationBudget。
    父 agent 上限为 max_iterations（默认 90）。
    子 agent 上限独立（默认 50）。
    """

    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """消耗一个迭代。返回 True 表示允许。"""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """退还一个迭代（如 execute_code 轮次不计数）。"""
        with self._lock:
            if self._used > 0:
                self._used -= 1

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)

    def __repr__(self) -> str:
        return f"IterationBudget({self.used}/{self.max_total})"


# =========================================================================
# 恢复策略
# =========================================================================

@dataclass
class ErrorRecoveryPlan:
    """根据错误分类生成的恢复计划。"""

    action: str  # retry | compress | rotate | fallback | abort
    wait_seconds: float = 0.0
    message: str = ""
    should_retry: bool = False
    should_compress: bool = False
    should_switch_model: bool = False
    should_abort: bool = False

    @classmethod
    def from_classification(
        cls,
        classified: ClassifiedError,
        attempt: int = 1,
    ) -> "ErrorRecoveryPlan":
        """从分类结果生成恢复计划。"""
        reason = classified.reason

        if not classified.retryable:
            return cls(
                action="abort",
                message=classified.message,
                should_abort=True,
            )

        if classified.should_compress:
            return cls(
                action="compress",
                message="上下文过大，需要压缩后重试",
                should_compress=True,
                should_retry=True,
            )

        delay = compute_retry_delay(classified, attempt)

        if reason in (FailoverReason.rate_limit, FailoverReason.overloaded):
            return cls(
                action="retry",
                wait_seconds=delay,
                message=f"{'速率限制' if reason == FailoverReason.rate_limit else '服务过载'}，等待 {delay:.1f}s 后重试",
                should_retry=True,
            )

        if reason in (FailoverReason.server_error, FailoverReason.timeout):
            return cls(
                action="retry",
                wait_seconds=delay,
                message=f"{'服务端错误' if reason == FailoverReason.server_error else '超时'}，等待 {delay:.1f}s 后重试",
                should_retry=True,
            )

        if reason in (FailoverReason.auth,):
            return cls(
                action="rotate",
                wait_seconds=delay,
                message="认证失败，尝试刷新凭证",
                should_retry=True,
            )

        if reason in (FailoverReason.billing, FailoverReason.model_not_found):
            return cls(
                action="fallback",
                message="需要切换到其他模型/提供者",
                should_switch_model=True,
                should_retry=True,
            )

        # 默认重试
        return cls(
            action="retry",
            wait_seconds=delay,
            message=f"未知错误，等待 {delay:.1f}s 后重试",
            should_retry=True,
        )


__all__ = [
    "FailoverReason",
    "ClassifiedError",
    "classify_api_error",
    "jittered_backoff",
    "compute_retry_delay",
    "IterationBudget",
    "ErrorRecoveryPlan",
]
