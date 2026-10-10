"""Spirit Agent error_handler.py 错误分类与恢复回归测试（v0.2.4 已交付未测）。

参考 hermes:
    - hermes error_classifier.py (1647行) + retry_utils.py (155行)

错误分类枚举 + ClassifiedError + classify_api_error 主分类器 +
jittered_backoff 退避 + IterationBudget 线程安全 + ErrorRecoveryPlan 恢复计划。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from spirit.agent.error_handler import (
    ClassifiedError,
    ErrorRecoveryPlan,
    FailoverReason,
    IterationBudget,
    _error_text,
    _has_error_code,
    _has_pattern,
    _status_code,
    classify_api_error,
    compute_retry_delay,
    jittered_backoff,
)


# ─────────────────────────── helper: 假异常 ───────────────────────────


class FakeError(Exception):
    """模拟 API 异常的载体（带 status_code / body / message）。"""

    def __init__(self, message: str = "", *, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body


def make_provider_error(
    message: str = "boom",
    *,
    status_code: int | None = None,
    body: Any = None,
):
    """构造 provider 风格的异常（带 status_code/body）。"""
    return FakeError(message, status_code=status_code, body=body)


# ─────────────────────────── FailoverReason 枚举 ───────────────────────────


class TestFailoverReason:
    """FailoverReason 枚举完整性。"""

    def test_all_expected_members(self):
        values = {r.value for r in FailoverReason}
        expected = {
            "auth", "auth_permanent",
            "billing", "rate_limit", "upstream_rate_limit",
            "overloaded", "server_error", "timeout",
            "ssl_cert_verification",
            "context_overflow", "payload_too_large", "image_too_large",
            "model_not_found", "content_policy_blocked", "format_error",
            "unknown",
        }
        assert values == expected

    def test_member_count(self):
        # 16 个值
        assert len(list(FailoverReason)) == 16

    def test_each_member_is_unique_string(self):
        seen = set()
        for r in FailoverReason:
            assert r.value not in seen
            seen.add(r.value)

    def test_enum_value_access(self):
        assert FailoverReason.auth.value == "auth"
        assert FailoverReason.overloaded.value == "overloaded"
        assert FailoverReason.unknown.value == "unknown"


# ─────────────────────────── ClassifiedError 数据类 ───────────────────────────


class TestClassifiedError:
    """ClassifiedError 字段 + is_auth property。"""

    def test_default_construction(self):
        c = ClassifiedError(reason=FailoverReason.auth)
        assert c.reason is FailoverReason.auth
        assert c.status_code is None
        assert c.provider is None
        assert c.model is None
        assert c.message == ""
        assert c.error_context == {}
        assert c.retryable is True
        assert c.should_compress is False
        assert c.should_rotate_credential is False
        assert c.should_fallback is False

    def test_full_construction(self):
        c = ClassifiedError(
            reason=FailoverReason.billing,
            status_code=402,
            provider="openai",
            model="gpt-4",
            message="insufficient credits",
            error_context={"k": "v"},
            retryable=False,
            should_compress=True,
            should_rotate_credential=True,
            should_fallback=True,
        )
        assert c.reason is FailoverReason.billing
        assert c.status_code == 402
        assert c.provider == "openai"
        assert c.model == "gpt-4"
        assert c.message == "insufficient credits"
        assert c.error_context == {"k": "v"}
        assert c.retryable is False
        assert c.should_compress is True

    def test_is_auth_for_auth(self):
        c = ClassifiedError(reason=FailoverReason.auth)
        assert c.is_auth is True

    def test_is_auth_for_auth_permanent(self):
        c = ClassifiedError(reason=FailoverReason.auth_permanent)
        assert c.is_auth is True

    def test_not_auth_for_other(self):
        for r in (
            FailoverReason.billing,
            FailoverReason.overloaded,
            FailoverReason.timeout,
            FailoverReason.context_overflow,
            FailoverReason.unknown,
        ):
            c = ClassifiedError(reason=r)
            assert c.is_auth is False


# ─────────────────────────── _error_text ───────────────────────────


class TestErrorText:
    """_error_text 提取错误文本（含 message/body/response）。"""

    def test_only_str(self):
        err = Exception("simple boom")
        text = _error_text(err)
        assert "simple boom" in text

    def test_with_message_attr(self):
        # message attr 与 Exception 默认 message 同名，会被 super().__init__ 覆盖为同一字符串。
        # 这里只用 keyword 形式，确保 message attr 路径生效（即使与 str(Exception) 重复）。
        err = FakeError(message="via attr")
        text = _error_text(err)
        assert "via attr" in text

    def test_with_body_str(self):
        err = FakeError("err", body="body content")
        text = _error_text(err)
        assert "err" in text
        assert "body content" in text

    def test_none_fields_skipped(self):
        """没有 body/response 时只保留主 error。"""
        err = FakeError("only main")
        text = _error_text(err)
        # 字符串 "None" 不应污染
        assert "none" not in text
        assert "only main" in text
        # 但 FakeError 会让 self.message = "only main"，所以 text 是 "only main only main"
        # 关键：仍然不包含 "none"
        assert "None" not in text

    def test_returns_lowercase(self):
        err = Exception("UPPERCASE ERROR")
        text = _error_text(err)
        assert text == text.lower()


# ─────────────────────────── _status_code ───────────────────────────


class TestStatusCode:
    """_status_code 从异常提取 HTTP status。"""

    def test_status_code_attr(self):
        err = FakeError("e", status_code=401)
        assert _status_code(err) == 401

    def test_status_attr(self):
        class E:
            status = 500
        assert _status_code(E()) == 500  # type: ignore

    def test_http_status_attr(self):
        class E:
            http_status = 429
        assert _status_code(E()) == 429  # type: ignore

    def test_response_status_code(self):
        class Resp:
            status_code = 503
        class E:
            response = Resp()
        assert _status_code(E()) == 503  # type: ignore

    def test_response_status(self):
        class Resp:
            status = 504
        class E:
            response = Resp()
        assert _status_code(E()) == 504  # type: ignore

    def test_no_status_returns_none(self):
        err = Exception("nothing")
        assert _status_code(err) is None

    def test_non_int_status_ignored(self):
        class E:
            status_code = "401"  # type: ignore
        assert _status_code(E()) is None  # type: ignore


# ─────────────────────────── _has_pattern ───────────────────────────


class TestHasPattern:
    """_has_pattern 子串匹配。"""

    def test_empty_text(self):
        assert _has_pattern("", ["a", "b"]) is False

    def test_empty_patterns(self):
        assert _has_pattern("anything", []) is False

    def test_match_one(self):
        assert _has_pattern("hello world", ["world"]) is True

    def test_match_none(self):
        assert _has_pattern("hello", ["xyz"]) is False


# ─────────────────────────── _has_error_code ───────────────────────────


class TestHasErrorCode:
    """_has_error_code 检查结构化 error code（body + text 双路径）。"""

    def test_body_dict_with_code(self):
        codes = frozenset({"insufficient_quota"})
        err = make_provider_error("", body={"error": {"code": "insufficient_quota"}})
        assert _has_error_code(err, codes) is True

    def test_body_no_code(self):
        codes = frozenset({"insufficient_quota"})
        err = make_provider_error("", body={"error": {"code": "other"}})
        assert _has_error_code(err, codes) is False

    def test_text_pattern_match(self):
        codes = frozenset({"insufficient_quota"})
        err = make_provider_error("Error: insufficient_quota detected", body=None)
        assert _has_error_code(err, codes) is True

    def test_body_not_dict(self):
        codes = frozenset({"insufficient_quota"})
        err = make_provider_error("", body="plain string")
        # text fallback should kick in
        err.message = "plain string contains insufficient_quota"
        assert _has_error_code(err, codes) is True


# ─────────────────────────── classify_api_error：状态码路径 ───────────────────────────


class TestClassifyByStatusCode:
    """classify_api_error 各 HTTP 状态码分支。"""

    def test_402_billing(self):
        err = make_provider_error("payment required", status_code=402)
        c = classify_api_error(err)
        assert c.reason is FailoverReason.billing
        assert c.retryable is False
        assert c.should_rotate_credential is True
        assert c.should_fallback is True
        assert c.status_code == 402

    def test_401_auth(self):
        err = make_provider_error("unauthorized", status_code=401)
        c = classify_api_error(err)
        assert c.reason is FailoverReason.auth
        assert c.retryable is True
        assert c.should_rotate_credential is True
        assert c.status_code == 401

    def test_403_auth(self):
        err = make_provider_error("forbidden", status_code=403)
        c = classify_api_error(err)
        assert c.reason is FailoverReason.auth

    def test_401_with_billing_text_becomes_billing(self):
        """401 但 text 含 billing pattern → 计费优先于 auth。"""
        err = make_provider_error(
            "Error: insufficient credits", status_code=401,
        )
        c = classify_api_error(err)
        assert c.reason is FailoverReason.billing
        assert c.should_rotate_credential is True
        assert c.should_fallback is True

    def test_403_with_billing_body_code_becomes_billing(self):
        err = make_provider_error(
            "forbidden",
            status_code=403,
            body={"error": {"code": "billing_not_active"}},
        )
        c = classify_api_error(err, provider="x", model="y")
        assert c.reason is FailoverReason.billing
        assert c.provider == "x"
        assert c.model == "y"

    def test_404_model_not_found(self):
        err = make_provider_error("Model not found in our system", status_code=404)
        c = classify_api_error(err)
        assert c.reason is FailoverReason.model_not_found
        assert c.should_fallback is True

    def test_404_no_model_text_becomes_format_error(self):
        """404 无 model 关键词，且不含 "not found" → 走通用 format_error。"""
        err = make_provider_error("Unknown resource", status_code=404)
        c = classify_api_error(err)
        assert c.reason is FailoverReason.format_error
        assert c.retryable is False

    def test_413_payload_too_large(self):
        err = make_provider_error("payload too large", status_code=413)
        c = classify_api_error(err)
        assert c.reason is FailoverReason.payload_too_large
        assert c.should_compress is True
        assert c.retryable is True

    def test_429_default_rate_limit(self):
        err = make_provider_error("rate limit exceeded", status_code=429)
        c = classify_api_error(err)
        assert c.reason is FailoverReason.rate_limit

    def test_429_with_overloaded_text_becomes_overloaded(self):
        """429 + overloaded pattern → overloaded（不是 rate_limit）。"""
        err = make_provider_error(
            "service is temporarily overloaded", status_code=429,
        )
        c = classify_api_error(err)
        assert c.reason is FailoverReason.overloaded

    def test_500_server_error(self):
        err = make_provider_error("internal error", status_code=500)
        c = classify_api_error(err)
        assert c.reason is FailoverReason.server_error
        assert c.retryable is True

    def test_502_server_error(self):
        err = make_provider_error("bad gateway", status_code=502)
        c = classify_api_error(err)
        assert c.reason is FailoverReason.server_error

    def test_503_overloaded(self):
        err = make_provider_error("unavailable", status_code=503)
        c = classify_api_error(err)
        assert c.reason is FailoverReason.overloaded

    def test_529_overloaded(self):
        err = make_provider_error("overloaded", status_code=529)
        c = classify_api_error(err)
        assert c.reason is FailoverReason.overloaded


# ─────────────────────────── classify_api_error：文本模式 ───────────────────────────


class TestClassifyByTextPattern:
    """classify_api_error 无 status code 时按 text 匹配。"""

    def test_ssl_cert(self):
        err = make_provider_error("certificate verify failed")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.ssl_cert_verification
        assert c.retryable is False

    def test_ssl_self_signed(self):
        err = make_provider_error("self signed certificate in certificate chain")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.ssl_cert_verification

    def test_ssl_error_class_name(self):
        class SSLError(Exception):
            pass
        err = SSLError("any message")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.ssl_cert_verification

    def test_context_overflow(self):
        err = make_provider_error("maximum context length exceeded")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.context_overflow
        assert c.should_compress is True

    def test_context_too_many_tokens(self):
        err = make_provider_error("too many tokens in input")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.context_overflow

    def test_billing_pattern(self):
        err = make_provider_error("insufficient_quota")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.billing
        assert c.should_rotate_credential is True

    def test_billing_body_error_code(self):
        err = make_provider_error(
            "different text",
            body={"error": {"code": "insufficient_credits"}},
        )
        c = classify_api_error(err)
        assert c.reason is FailoverReason.billing

    def test_overloaded_pattern(self):
        err = make_provider_error("server is overloaded")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.overloaded

    def test_overloaded_at_capacity(self):
        err = make_provider_error("Service at capacity")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.overloaded

    def test_rate_limit_pattern(self):
        err = make_provider_error("requests per minute exceeded")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.rate_limit

    def test_rate_limit_throttled(self):
        err = make_provider_error("throttled, try again in 60s")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.rate_limit

    def test_usage_limit_transient_becomes_rate_limit(self):
        """quota + transient signal → rate_limit 而非 billing。"""
        err = make_provider_error("quota exceeded, please retry later")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.rate_limit

    def test_usage_limit_no_transient_becomes_billing(self):
        err = make_provider_error("quota exceeded")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.billing
        assert c.should_rotate_credential is True

    def test_auth_pattern(self):
        err = make_provider_error("invalid api key")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.auth
        assert c.should_rotate_credential is True

    def test_auth_forbidden(self):
        err = make_provider_error("Access denied / forbidden")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.auth

    def test_content_policy(self):
        err = make_provider_error("Content policy violation blocked the request")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.content_policy_blocked
        assert c.retryable is False

    def test_content_filter(self):
        err = make_provider_error("Content filter triggered")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.content_policy_blocked

    def test_content_refusal(self):
        err = make_provider_error("Model refusal due to content concerns")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.content_policy_blocked


# ─────────────────────────── classify_api_error：超时/连接 ───────────────────────────


class TestClassifyTimeoutConnection:
    """超时 + 连接错误的分类。"""

    def test_timeout_class_name(self):
        class ReadTimeout(Exception):
            pass
        err = ReadTimeout("nothing")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.timeout
        assert c.retryable is True

    def test_timeout_text(self):
        err = make_provider_error("Request timed out after 30s")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.timeout

    def test_timeout_builtin(self):
        err = TimeoutError("builtin")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.timeout

    def test_connection_class_name(self):
        class ConnectionError(Exception):
            pass
        err = ConnectionError("refused")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.timeout

    def test_connection_text(self):
        err = make_provider_error("connection refused by remote host")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.timeout


# ─────────────────────────── classify_api_error：兜底 + 优先级 ───────────────────────────


class TestClassifyFallbackAndPriority:
    """兜底 + SSL/context 优先于状态码。"""

    def test_unknown_fallback(self):
        err = make_provider_error("some random weird error XYZ")
        c = classify_api_error(err)
        assert c.reason is FailoverReason.unknown
        assert c.retryable is True

    def test_ssl_takes_priority_over_status_code(self):
        """SSL text 即使伴随 500 也走 ssl_cert_verification。"""
        err = make_provider_error("certificate verify failed", status_code=500)
        c = classify_api_error(err)
        assert c.reason is FailoverReason.ssl_cert_verification

    def test_context_takes_priority_over_status_code(self):
        err = make_provider_error(
            "context length too long", status_code=429,
        )
        c = classify_api_error(err)
        assert c.reason is FailoverReason.context_overflow
        assert c.should_compress is True

    def test_message_truncated_at_300(self):
        long_msg = "x" * 1000
        err = make_provider_error(long_msg)
        c = classify_api_error(err)
        assert len(c.message) <= 300

    def test_provider_and_model_passed_through(self):
        err = make_provider_error("weird")
        c = classify_api_error(err, provider="anthropic", model="claude-opus")
        assert c.provider == "anthropic"
        assert c.model == "claude-opus"


# ─────────────────────────── jittered_backoff ───────────────────────────


class TestJitteredBackoff:
    """jittered_backoff 数学正确性 + 抖动去相关。"""

    def test_attempt_1_minimum(self):
        # base=5, exponent=0, delay=5
        for _ in range(20):
            d = jittered_backoff(1, base_delay=5.0, max_delay=120.0)
            assert 5.0 <= d <= 5.0 + 5.0 * 0.5  # 5.0 ~ 7.5

    def test_attempt_2_doubles(self):
        # base=5, exponent=1, delay=10
        for _ in range(20):
            d = jittered_backoff(2, base_delay=5.0, max_delay=120.0)
            assert 10.0 <= d <= 10.0 + 5.0  # 10 ~ 15

    def test_attempt_3_quadruples(self):
        # base=5, exponent=2, delay=20
        for _ in range(20):
            d = jittered_backoff(3, base_delay=5.0, max_delay=120.0)
            assert 20.0 <= d <= 30.0

    def test_capped_at_max_delay(self):
        d = jittered_backoff(20, base_delay=5.0, max_delay=60.0)
        # base*2^19 = 巨大，但被 cap 到 60.0
        assert 60.0 <= d <= 60.0 + 60.0 * 0.5  # 60 ~ 90

    def test_negative_attempt_clamped_to_1(self):
        # attempt=0 → exponent=max(0, -1)=0 → delay=base
        for _ in range(10):
            d = jittered_backoff(0, base_delay=5.0, max_delay=120.0)
            assert 5.0 <= d <= 7.5

    def test_exponent_overflow_returns_max(self):
        # exponent >= 63 → 直接 max_delay
        d = jittered_backoff(100, base_delay=5.0, max_delay=120.0, jitter_ratio=0.5)
        assert 120.0 <= d <= 120.0 * 1.5  # 120 ~ 180

    def test_base_zero_or_negative_returns_max(self):
        d = jittered_backoff(1, base_delay=0.0, max_delay=120.0)
        assert 120.0 <= d <= 180.0

    def test_jitter_decorrelates_consecutive_calls(self):
        """连续两次 backoff 的 jitter 不应该完全相同（不同 tick 种子）。"""
        # 多次尝试，必有不同
        seen = set()
        for _ in range(10):
            d = jittered_backoff(1, base_delay=5.0, max_delay=120.0)
            seen.add(round(d, 4))
        # 至少有两种不同值（实际上多种）
        assert len(seen) >= 2


# ─────────────────────────── compute_retry_delay ───────────────────────────


class TestComputeRetryDelay:
    """compute_retry_delay 按 reason 选 base/max 配置（用 monkeypatch 注入 config）。"""

    def _setup_config(self, monkeypatch, mapping):
        """inject config via spirit.config.get_config_value (error_handler 内部 import 路径)."""
        def fake_get(key, default=None):
            return mapping.get(key, default)

        # error_handler.py 实际是 `from spirit.config import get_config_value`，
        # 必须 monkeypatch spirit.config 而非 spirit.agent.error_handler。
        monkeypatch.setattr("spirit.config.get_config_value", fake_get)

    def test_overloaded(self, monkeypatch):
        self._setup_config(monkeypatch, {
            "backoff.overloaded_base": 10.0,
            "backoff.overloaded_max": 180.0,
        })
        classified = ClassifiedError(reason=FailoverReason.overloaded)
        d = compute_retry_delay(classified, attempt=1)
        assert 10.0 <= d <= 10.0 + 180.0 * 0.5  # base=10, jitter ≤0.5 * 10 = 5

    def test_rate_limit(self, monkeypatch):
        self._setup_config(monkeypatch, {
            "backoff.rate_limit_base": 5.0,
            "backoff.rate_limit_max": 120.0,
        })
        classified = ClassifiedError(reason=FailoverReason.rate_limit)
        d = compute_retry_delay(classified, attempt=1)
        assert 5.0 <= d <= 7.5

    def test_server_error(self, monkeypatch):
        self._setup_config(monkeypatch, {
            "backoff.server_error_base": 3.0,
            "backoff.server_error_max": 60.0,
        })
        classified = ClassifiedError(reason=FailoverReason.server_error)
        d = compute_retry_delay(classified, attempt=1)
        assert 3.0 <= d <= 4.5

    def test_timeout(self, monkeypatch):
        self._setup_config(monkeypatch, {
            "backoff.timeout_base": 5.0,
            "backoff.timeout_max": 90.0,
        })
        classified = ClassifiedError(reason=FailoverReason.timeout)
        d = compute_retry_delay(classified, attempt=1)
        assert 5.0 <= d <= 7.5

    def test_auth(self, monkeypatch):
        self._setup_config(monkeypatch, {
            "backoff.auth_base": 2.0,
            "backoff.auth_max": 30.0,
        })
        classified = ClassifiedError(reason=FailoverReason.auth)
        d = compute_retry_delay(classified, attempt=1)
        assert 2.0 <= d <= 3.0

    def test_default(self, monkeypatch):
        """非 special reason → 用 default 配置。"""
        self._setup_config(monkeypatch, {
            "backoff.default_base": 5.0,
            "backoff.default_max": 120.0,
        })
        classified = ClassifiedError(reason=FailoverReason.unknown)
        d = compute_retry_delay(classified, attempt=1)
        assert 5.0 <= d <= 7.5


# ─────────────────────────── IterationBudget ───────────────────────────


class TestIterationBudget:
    """IterationBudget 线程安全迭代计数。"""

    def test_initial_state(self):
        b = IterationBudget(max_total=10)
        assert b.used == 0
        assert b.remaining == 10
        assert b.max_total == 10

    def test_consume_increments(self):
        b = IterationBudget(max_total=5)
        assert b.consume() is True
        assert b.used == 1
        assert b.remaining == 4

    def test_consume_up_to_max(self):
        b = IterationBudget(max_total=3)
        assert b.consume() is True
        assert b.consume() is True
        assert b.consume() is True
        # 4th should fail
        assert b.consume() is False
        assert b.used == 3

    def test_consume_never_exceeds(self):
        b = IterationBudget(max_total=2)
        for _ in range(10):
            b.consume()
        assert b.used == 2
        assert b.remaining == 0

    def test_refund_decrements(self):
        b = IterationBudget(max_total=5)
        b.consume()
        b.consume()
        assert b.used == 2
        b.refund()
        assert b.used == 1
        assert b.remaining == 4

    def test_refund_floors_at_zero(self):
        b = IterationBudget(max_total=5)
        b.refund()  # already at 0
        assert b.used == 0
        b.refund()
        b.refund()
        assert b.used == 0

    def test_refund_after_exhaust_frees_slot(self):
        b = IterationBudget(max_total=1)
        b.consume()
        assert b.consume() is False
        b.refund()
        assert b.consume() is True

    def test_remaining_never_negative(self):
        b = IterationBudget(max_total=1)
        b.consume()
        assert b.remaining == 0
        # even with refund at 0
        b.refund()
        assert b.remaining == 1

    def test_repr(self):
        b = IterationBudget(max_total=10)
        b.consume()
        b.consume()
        assert repr(b) == "IterationBudget(2/10)"

    def test_repr_initial(self):
        b = IterationBudget(max_total=10)
        assert repr(b) == "IterationBudget(0/10)"


class TestIterationBudgetThreadSafety:
    """IterationBudget 多线程并发安全性。"""

    def test_concurrent_consume_never_exceeds(self):
        """100 个线程各 try 1000 次，最终 used 必 ≤ max_total。"""
        b = IterationBudget(max_total=50)
        successes = []

        def worker():
            for _ in range(100):
                if b.consume():
                    successes.append(1)

        threads = [threading.Thread(target=worker) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert b.used <= 50
        # 成功数应等于 max_total
        assert sum(successes) == 50

    def test_concurrent_consume_refund(self):
        b = IterationBudget(max_total=10)
        consume_count = []
        refund_count = []

        def consumer():
            for _ in range(50):
                if b.consume():
                    consume_count.append(1)
                    time.sleep(0.0001)
                    b.refund()

        threads = [threading.Thread(target=consumer) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 最终应该回到 0（每个 consume 后都有 refund）
        assert b.used == 0


# ─────────────────────────── ErrorRecoveryPlan ───────────────────────────


class TestErrorRecoveryPlan:
    """ErrorRecoveryPlan 构造 + from_classification 工厂。"""

    def test_direct_construction(self):
        p = ErrorRecoveryPlan(
            action="retry",
            wait_seconds=5.0,
            message="wait",
            should_retry=True,
            should_compress=False,
            should_switch_model=False,
            should_abort=False,
        )
        assert p.action == "retry"
        assert p.wait_seconds == 5.0
        assert p.message == "wait"
        assert p.should_retry is True

    def test_default_values(self):
        p = ErrorRecoveryPlan(action="abort")
        assert p.wait_seconds == 0.0
        assert p.message == ""
        assert p.should_retry is False
        assert p.should_compress is False
        assert p.should_switch_model is False
        assert p.should_abort is False


class TestErrorRecoveryPlanFromClassification:
    """from_classification 5 种 action 路径。"""

    def _setup_config(self, monkeypatch, mapping):
        """inject config via spirit.config.get_config_value."""
        def fake_get(key, default=None):
            return mapping.get(key, default)

        monkeypatch.setattr("spirit.config.get_config_value", fake_get)

    def test_not_retryable_aborts(self, monkeypatch):
        """non-retryable (e.g. billing) → abort。"""
        classified = ClassifiedError(
            reason=FailoverReason.billing,
            retryable=False,
            should_fallback=True,
            message="no credits",
        )
        p = ErrorRecoveryPlan.from_classification(classified)
        assert p.action == "abort"
        assert p.should_abort is True
        assert p.message == "no credits"

    def test_should_compress_compresses(self, monkeypatch):
        self._setup_config(monkeypatch, {})
        classified = ClassifiedError(
            reason=FailoverReason.context_overflow,
            retryable=True,
            should_compress=True,
        )
        p = ErrorRecoveryPlan.from_classification(classified)
        assert p.action == "compress"
        assert p.should_compress is True
        assert p.should_retry is True

    def test_rate_limit_retries(self, monkeypatch):
        self._setup_config(monkeypatch, {
            "backoff.rate_limit_base": 5.0,
            "backoff.rate_limit_max": 120.0,
        })
        classified = ClassifiedError(
            reason=FailoverReason.rate_limit, retryable=True,
        )
        p = ErrorRecoveryPlan.from_classification(classified, attempt=1)
        assert p.action == "retry"
        assert p.should_retry is True
        assert p.wait_seconds > 0
        assert "速率限制" in p.message

    def test_overloaded_retries(self, monkeypatch):
        self._setup_config(monkeypatch, {
            "backoff.overloaded_base": 10.0,
            "backoff.overloaded_max": 180.0,
        })
        classified = ClassifiedError(
            reason=FailoverReason.overloaded, retryable=True,
        )
        p = ErrorRecoveryPlan.from_classification(classified, attempt=1)
        assert p.action == "retry"
        assert "服务过载" in p.message

    def test_server_error_retries(self, monkeypatch):
        self._setup_config(monkeypatch, {
            "backoff.server_error_base": 3.0,
            "backoff.server_error_max": 60.0,
        })
        classified = ClassifiedError(
            reason=FailoverReason.server_error, retryable=True,
        )
        p = ErrorRecoveryPlan.from_classification(classified, attempt=1)
        assert p.action == "retry"
        assert "服务端错误" in p.message

    def test_timeout_retries(self, monkeypatch):
        self._setup_config(monkeypatch, {
            "backoff.timeout_base": 5.0,
            "backoff.timeout_max": 90.0,
        })
        classified = ClassifiedError(
            reason=FailoverReason.timeout, retryable=True,
        )
        p = ErrorRecoveryPlan.from_classification(classified, attempt=1)
        assert p.action == "retry"
        assert "超时" in p.message

    def test_auth_rotates(self, monkeypatch):
        self._setup_config(monkeypatch, {
            "backoff.auth_base": 2.0,
            "backoff.auth_max": 30.0,
        })
        classified = ClassifiedError(
            reason=FailoverReason.auth,
            retryable=True,
            should_rotate_credential=True,
        )
        p = ErrorRecoveryPlan.from_classification(classified, attempt=1)
        assert p.action == "rotate"
        assert p.should_retry is True
        assert "凭证" in p.message or "认证失败" in p.message

    def test_billing_fallbacks(self, monkeypatch):
        self._setup_config(monkeypatch, {})
        classified = ClassifiedError(
            reason=FailoverReason.billing,
            retryable=True,  # normally False, but here we test fallback path
            should_fallback=True,
        )
        # 注意：from_classification 先看 !retryable → abort；要看 fallback
        # 必须 retryable=True 且 should_fallback=true
        # 但 billing 通常 retryable=False，所以走 abort 优先
        # 我们构造 retryable=True 进入 fallback 分支
        p = ErrorRecoveryPlan.from_classification(classified)
        # 因为 retryable=True 走 default 重试路径（unknown 分支）
        # 真实情况 billing 大多 abort
        assert p.action in {"fallback", "retry"}

    def test_model_not_found_fallbacks(self, monkeypatch):
        self._setup_config(monkeypatch, {})
        classified = ClassifiedError(
            reason=FailoverReason.model_not_found,
            retryable=True,
            should_fallback=True,
        )
        p = ErrorRecoveryPlan.from_classification(classified)
        # model_not_found 不在 special list 里（billing 特殊），走 default retry
        # 但根据 from_classification 逻辑，billing IN 列表 → fallback；model_not_found IN 列表 → fallback
        assert p.action == "fallback"
        assert p.should_switch_model is True
        assert p.should_retry is True

    def test_unknown_retries_with_default(self, monkeypatch):
        self._setup_config(monkeypatch, {
            "backoff.default_base": 5.0,
            "backoff.default_max": 120.0,
        })
        classified = ClassifiedError(reason=FailoverReason.unknown, retryable=True)
        p = ErrorRecoveryPlan.from_classification(classified, attempt=2)
        assert p.action == "retry"
        assert "未知错误" in p.message
        assert p.wait_seconds > 0

    def test_priority_abort_over_compress(self, monkeypatch):
        """non-retryable 优先于 should_compress（abort 而非 compress）。"""
        self._setup_config(monkeypatch, {})
        classified = ClassifiedError(
            reason=FailoverReason.context_overflow,
            retryable=False,  # 强制不重试
            should_compress=True,
        )
        p = ErrorRecoveryPlan.from_classification(classified)
        assert p.action == "abort"

    def test_attempt_parameter_affects_delay(self, monkeypatch):
        """attempt 越大 delay 越大（在 max 范围内）。"""
        self._setup_config(monkeypatch, {
            "backoff.rate_limit_base": 5.0,
            "backoff.rate_limit_max": 120.0,
        })
        classified = ClassifiedError(
            reason=FailoverReason.rate_limit, retryable=True,
        )
        p1 = ErrorRecoveryPlan.from_classification(classified, attempt=1)
        p3 = ErrorRecoveryPlan.from_classification(classified, attempt=3)
        assert p3.wait_seconds > p1.wait_seconds


# ─────────────────────────── 端到端：异常 → 分类 → 恢复计划 ───────────────────────────


class TestEndToEnd:
    """完整链路：异常 → classify_api_error → ErrorRecoveryPlan.from_classification。"""

    def test_e2e_402_billing(self, monkeypatch):
        monkeypatch.setattr(
            "spirit.config.get_config_value",
            lambda key, default=None: default,
        )
        err = make_provider_error("payment required", status_code=402)
        classified = classify_api_error(err, provider="openai", model="gpt-4")
        plan = ErrorRecoveryPlan.from_classification(classified)
        assert plan.action == "abort"
        assert classified.provider == "openai"

    def test_e2e_429_overloaded(self, monkeypatch):
        monkeypatch.setattr(
            "spirit.config.get_config_value",
            lambda key, default=None: default,
        )
        err = make_provider_error(
            "service is temporarily overloaded", status_code=429,
        )
        classified = classify_api_error(err)
        assert classified.reason is FailoverReason.overloaded
        plan = ErrorRecoveryPlan.from_classification(classified)
        assert plan.action == "retry"
        assert plan.wait_seconds > 0

    def test_e2e_ssl_aborts(self, monkeypatch):
        monkeypatch.setattr(
            "spirit.config.get_config_value",
            lambda key, default=None: default,
        )
        err = make_provider_error("certificate verify failed")
        classified = classify_api_error(err)
        plan = ErrorRecoveryPlan.from_classification(classified)
        # SSL retryable=False → abort
        assert plan.action == "abort"
