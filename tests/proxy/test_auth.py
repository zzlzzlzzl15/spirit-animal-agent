"""Bearer token 鉴权测试。"""

from spirit.proxy import auth


def test_extract_bearer_basic():
    assert auth.extract_bearer("Bearer abc123") == "abc123"


def test_extract_bearer_case_insensitive():
    assert auth.extract_bearer("bearer abc123") == "abc123"
    assert auth.extract_bearer("BEARER abc123") == "abc123"


def test_extract_bearer_strips_whitespace():
    assert auth.extract_bearer("  Bearer   abc123  ") == "abc123"


def test_extract_bearer_none_and_empty():
    assert auth.extract_bearer(None) is None
    assert auth.extract_bearer("") is None
    assert auth.extract_bearer("Bearer") is None
    assert auth.extract_bearer("Bearer   ") is None


def test_extract_bearer_wrong_scheme():
    assert auth.extract_bearer("Basic abc123") is None


def test_verify_token_no_expected_allows_all():
    assert auth.verify_token(None, None) is True
    assert auth.verify_token("anything", "") is True


def test_verify_token_match():
    assert auth.verify_token("secret", "secret") is True


def test_verify_token_mismatch():
    assert auth.verify_token("wrong", "secret") is False


def test_verify_token_missing_provided():
    assert auth.verify_token(None, "secret") is False


def test_is_authorized_end_to_end():
    assert auth.is_authorized("Bearer secret", "secret") is True
    assert auth.is_authorized("Bearer nope", "secret") is False
    assert auth.is_authorized(None, "secret") is False
    # 无期望 key → 恒放行
    assert auth.is_authorized(None, None) is True
