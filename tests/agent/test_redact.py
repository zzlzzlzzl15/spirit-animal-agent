"""spirit.agent.redact 单元测试 —— 密钥脱敏。

离线、无外部依赖。覆盖 mask_secret、前缀遮蔽、ENV/JSON/YAML 赋值、认证头、
私钥、DB 连接串、JWT、电话、URL 处理、code_file/file_read 语义、终端输出策略。
"""

from spirit.agent import redact


# --------------------------------------------------------------------------
# mask_secret
# --------------------------------------------------------------------------

class TestMaskSecret:
    def test_long_token_preserves_head_tail(self):
        assert redact.mask_secret("sk-proj-abcdef1234567890") == "sk-p...7890"

    def test_short_token_fully_masked(self):
        assert redact.mask_secret("short") == "***"

    def test_empty_returns_empty_default(self):
        assert redact.mask_secret("") == ""
        assert redact.mask_secret(None) == ""

    def test_empty_override(self):
        assert redact.mask_secret("", empty="(not set)") == "(not set)"

    def test_custom_head_tail_floor(self):
        # floor=18，"long-token" 长 10 < 18 → 整体遮蔽
        assert redact.mask_secret("long-token", head=6, tail=4, floor=18) == "***"

    def test_boundary_at_floor(self):
        # 恰好 floor 长度 → 不遮蔽，保留首尾
        val = "a" * 12
        assert redact.mask_secret(val, floor=12) == "aaaa...aaaa"


# --------------------------------------------------------------------------
# redact_sensitive_text —— 前缀 token
# --------------------------------------------------------------------------

class TestPrefixRedaction:
    def test_openai_key_masked(self):
        out = redact.redact_sensitive_text("key is sk-proj-abcdef1234567890 here")
        assert "sk-proj-abcdef1234567890" not in out
        assert "sk-pro" in out  # 保留前缀便于排查

    def test_github_pat_masked(self):
        out = redact.redact_sensitive_text("token=ghp_abcdefghij1234567890")
        assert "ghp_abcdefghij1234567890" not in out

    def test_aws_access_key_masked(self):
        out = redact.redact_sensitive_text("AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_file_read_uses_nonreusable_sentinel(self):
        out = redact.redact_sensitive_text(
            "ghp_abcdefghij1234567890", file_read=True
        )
        assert "«redacted:ghp_…»" == out
        # 绝不泄露密钥主体
        assert "abcdefghij" not in out

    def test_no_false_positive_on_plain_text(self):
        text = "The quick brown fox jumps over the lazy dog."
        assert redact.redact_sensitive_text(text) == text


# --------------------------------------------------------------------------
# ENV / JSON / YAML 赋值
# --------------------------------------------------------------------------

class TestAssignmentRedaction:
    def test_env_assignment_masked(self):
        out = redact.redact_sensitive_text(
            "OPENAI_API_KEY=abcdefghijklmnop1234567890"
        )
        assert "abcdefghijklmnop1234567890" not in out

    def test_env_lookup_not_masked(self):
        # 程序化 env 查找引用变量名，不应遮蔽
        text = "KEY=os.getenv('SECRET_VALUE')"
        assert redact.redact_sensitive_text(text) == text

    def test_json_field_masked(self):
        out = redact.redact_sensitive_text('{"api_key": "abcdef1234567890xyz"}')
        assert "abcdef1234567890xyz" not in out

    def test_yaml_colon_masked(self):
        out = redact.redact_sensitive_text("password: hunter2secretvalue")
        assert "hunter2secretvalue" not in out

    def test_code_file_skips_env_assignment(self):
        # code_file=True 跳过 ENV/JSON，避免源码常量误伤
        text = "MAX_TOKENS=100000"
        assert redact.redact_sensitive_text(text, code_file=True) == text

    def test_dotted_config_key_masked(self):
        out = redact.redact_sensitive_text(
            "spring.datasource.password=supersecret123"
        )
        assert "supersecret123" not in out


# --------------------------------------------------------------------------
# 认证头 / 私钥 / DB / JWT / 电话
# --------------------------------------------------------------------------

class TestHeaderAndSecretRedaction:
    def test_bearer_auth_header_masked(self):
        out = redact.redact_sensitive_text(
            "Authorization: Bearer abcdefghijklmnop1234"
        )
        assert "abcdefghijklmnop1234" not in out
        assert "Bearer" in out  # scheme 词保留

    def test_x_api_key_header_masked(self):
        out = redact.redact_sensitive_text("x-api-key: sk-value-abcdefghijklmnop")
        assert "sk-value-abcdefghijklmnop" not in out

    def test_private_key_block_redacted(self):
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA1234567890abcdef\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = redact.redact_sensitive_text(text)
        assert out == "[REDACTED PRIVATE KEY]"

    def test_db_connstring_password_masked(self):
        out = redact.redact_sensitive_text(
            "postgresql://user:secretpass123@localhost:5432/db"
        )
        assert "secretpass123" not in out
        assert "postgresql://user:***@localhost" in out

    def test_jwt_masked(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123def456"
        out = redact.redact_sensitive_text(f"token {jwt} end")
        assert jwt not in out

    def test_phone_masked(self):
        out = redact.redact_sensitive_text("call me at +14155552671 ok")
        assert "+14155552671" not in out

    def test_telegram_bot_token_masked(self):
        out = redact.redact_sensitive_text(
            "bot123456789:AAHdqTcvCH1ABCD1234567890abcdefghij"
        )
        assert "AAHdqTcvCH1ABCD1234567890abcdefghij" not in out


# --------------------------------------------------------------------------
# URL 处理
# --------------------------------------------------------------------------

class TestUrlRedaction:
    def test_web_url_query_passes_through_by_default(self):
        # 全局脱敏有意放过 web-URL query（OAuth 回调等工作流）
        url = "https://example.com/cb?code=ABC123&state=xyz"
        assert redact.redact_sensitive_text(url) == url

    def test_cdp_url_redacts_query(self):
        url = "https://example.com/cb?code=ABC123&state=xyz"
        out = redact.redact_cdp_url(url)
        assert "code=***" in out
        assert "state=xyz" in out  # 非敏感参数保留

    def test_bare_token_userinfo_masked(self):
        out = redact.redact_sensitive_text(
            "https://ghp_supersecrettoken123@github.com/user/repo"
        )
        assert "ghp_supersecrettoken123" not in out

    def test_cdp_url_none_safe(self):
        assert redact.redact_cdp_url(None) == ""


# --------------------------------------------------------------------------
# 边界 / force / 类型
# --------------------------------------------------------------------------

class TestEdgeCases:
    def test_none_passthrough(self):
        assert redact.redact_sensitive_text(None) is None

    def test_empty_passthrough(self):
        assert redact.redact_sensitive_text("") == ""

    def test_non_string_coerced(self):
        out = redact.redact_sensitive_text(12345)
        assert out == "12345"

    def test_force_redacts_even_shape(self):
        # force=True 无视全局偏好，仍遮蔽已知形状
        out = redact.redact_sensitive_text(
            "sk-proj-abcdef1234567890", force=True
        )
        assert "sk-proj-abcdef1234567890" not in out


# --------------------------------------------------------------------------
# 终端输出策略
# --------------------------------------------------------------------------

class TestTerminalOutput:
    def test_is_env_dump_command_true(self):
        assert redact.is_env_dump_command("env") is True
        assert redact.is_env_dump_command("printenv") is True
        assert redact.is_env_dump_command("foo && export BAR") is True

    def test_is_env_dump_command_false(self):
        assert redact.is_env_dump_command("ls -la") is False
        assert redact.is_env_dump_command("") is False
        assert redact.is_env_dump_command(None) is False

    def test_env_dump_masks_opaque_token(self):
        # env dump → code_file=False，不透明 token 也被遮蔽
        out = redact.redact_terminal_output(
            "MY_SERVICE_TOKEN=abc123randomstringvalue", command="env"
        )
        assert "abc123randomstringvalue" not in out

    def test_non_env_dump_preserves_source_constant(self):
        # 非 env dump → code_file=True，源码常量不误伤
        out = redact.redact_terminal_output(
            "MAX_TOKENS=100000", command="cat config.py"
        )
        assert "MAX_TOKENS=100000" in out

    def test_empty_output_passthrough(self):
        assert redact.redact_terminal_output("", command="env") == ""
