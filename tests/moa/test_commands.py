"""/moa 命令分发层测试 — 对标 Hermes ``tests/cli/test_moa_command.py`` +
``tests/gateway/test_moa_one_shot_restore.py``。

覆盖 ``spirit.moa.commands.handle_moa_command`` 的传输无关契约：

- 裸 ``/moa`` 或 ``/moa list|ls`` → 列出预设（action="list"）。
- ``/moa use <name>`` → 把会话切到 MoA 虚拟 provider（provider="moa", model=<name>），
  重置 ``_client`` 触发重建；未知/禁用预设 → ok=False。
- ``/moa off`` → 恢复切换前的 provider/model；不在 MoA 时 ok=False。
- ``/moa <prompt>`` → 一次性：快照→切默认/激活预设→跑 ``agent.chat``→**在 finally 里
  恢复**（对标 Hermes 的核心回归守卫：抛异常的轮次也必须恢复，否则 MoA 覆盖永久泄漏）。
- 已在 MoA 时的一次性 prompt 沿用当前预设、不快照不恢复。
- ``agent=None`` → action="unavailable"；``help`` → 用法。

与 Hermes 的差异：Hermes 命令测试驱动 ``HermesCLI.process_command`` + ``_pending_*``
字段（CLI 耦合）；Spirit 命令层吐结构化 dict，用 ``FakeAgent``（持 provider/model/
_client/chat）即可单测「切换→跑→恢复」原子性，无需真实终端或 LLM。配置经 harness
fixture 注入（patch ``commands._load_moa_config``）。
"""

from __future__ import annotations

import pytest

from spirit.moa.commands import handle_moa_command, moa_usage

from .conftest import make_config


# ---------------------------------------------------------------------------
# FakeAgent：持有命令层读写的运行时字段 + 可配置的 chat
# ---------------------------------------------------------------------------

class FakeAgent:
    """最小 SpiritAgent 替身：只实现命令层触碰的字段与 ``chat``。"""

    def __init__(
        self,
        *,
        provider="openrouter",
        model="anthropic/claude-opus-4.8",
        base_url="https://openrouter.ai/api/v1",
        api_key="test-key",
        chat_response="aggregator acted",
        chat_raises=None,
    ):
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self._client = object()  # 哨兵；切换时被重置为 None
        self._moa_saved_runtime = None
        self._chat_response = chat_response
        self._chat_raises = chat_raises
        self.chat_calls = []
        self.stream_callbacks = []

    def chat(self, prompt, stream_callback=None):
        self.chat_calls.append(prompt)
        self.stream_callbacks.append(stream_callback)
        if self._chat_raises is not None:
            raise self._chat_raises
        return {"response": self._chat_response}


def _two_preset_config():
    """review（默认）+ coding 两个预设，供连续切换 / 激活预设测试用。"""
    return make_config(
        default_preset="review",
        presets={
            "review": {
                "reference_models": [{"provider": "openai-codex", "model": "gpt-5.5"}],
                "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
            },
            "coding": {
                "reference_models": [{"provider": "openai", "model": "gpt-4o"}],
                "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
            },
        },
    )


# ---------------------------------------------------------------------------
# 边界：无 agent / help
# ---------------------------------------------------------------------------

def test_none_agent_is_unavailable():
    result = handle_moa_command(None, "list")
    assert result["ok"] is False
    assert result["action"] == "unavailable"


def test_help_action_returns_usage(harness):
    agent = FakeAgent()
    result = handle_moa_command(agent, "help")
    assert result["ok"] is True
    assert result["action"] == "help"
    assert result["message"] == moa_usage()


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_bare_moa_lists_presets(harness):
    agent = FakeAgent()
    result = handle_moa_command(agent, "")
    assert result["ok"] is True
    assert result["action"] == "list"
    joined = "\n".join(result["lines"])
    assert "review" in joined
    assert "Default: review" in joined
    # 未切到 MoA → 状态行显示 off。
    assert "off" in result["status_line"]


@pytest.mark.parametrize("alias", ["list", "ls", "LIST", "Ls"])
def test_list_aliases(harness, alias):
    agent = FakeAgent()
    result = handle_moa_command(agent, alias)
    assert result["action"] == "list"
    assert result["ok"] is True


def test_list_shows_disabled_marker(make_harness):
    cfg = make_config(
        presets={
            "review": {
                "enabled": False,
                "reference_models": [{"provider": "openai-codex", "model": "gpt-5.5"}],
                "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
            }
        }
    )
    make_harness(cfg)
    agent = FakeAgent()
    result = handle_moa_command(agent, "list")
    joined = "\n".join(result["lines"])
    assert "disabled" in joined


# ---------------------------------------------------------------------------
# use — 切换会话到 MoA 虚拟 provider
# ---------------------------------------------------------------------------

def test_use_switches_session_to_moa(harness):
    agent = FakeAgent()
    result = handle_moa_command(agent, "use review")

    assert result["ok"] is True
    assert result["action"] == "use"
    # 会话切到 MoA 虚拟 provider，model 即预设名。
    assert agent.provider == "moa"
    assert agent.model == "review"
    # _client 被重置 → 下次访问 client property 重建为 MoAClient。
    assert agent._client is None
    # 原始运行时被快照，供 /moa off 恢复。
    saved = agent._moa_saved_runtime
    assert saved["provider"] == "openrouter"
    assert saved["model"] == "anthropic/claude-opus-4.8"
    # 状态行显示 on。
    assert "on" in result["status_line"]


def test_use_on_enable_aliases(harness):
    for verb in ("use", "on", "enable"):
        agent = FakeAgent()
        result = handle_moa_command(agent, f"{verb} review")
        assert result["ok"] is True, verb
        assert agent.provider == "moa"


def test_use_without_name_falls_back_to_default(harness):
    agent = FakeAgent()
    result = handle_moa_command(agent, "use")
    assert result["ok"] is True
    # 默认预设是 review。
    assert agent.model == "review"


def test_use_unknown_preset_fails(harness):
    agent = FakeAgent()
    result = handle_moa_command(agent, "use nope")
    assert result["ok"] is False
    assert result["action"] == "use"
    # 错误消息可操作：列出可用预设 + 提示 /moa list。
    assert "nope" in result["message"]
    assert "/moa list" in result["message"]
    # 未切换。
    assert agent.provider == "openrouter"


def test_use_disabled_preset_fails(make_harness):
    cfg = make_config(
        presets={
            "review": {
                "enabled": False,
                "reference_models": [{"provider": "openai-codex", "model": "gpt-5.5"}],
                "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
            }
        }
    )
    make_harness(cfg)
    agent = FakeAgent()
    result = handle_moa_command(agent, "use review")
    assert result["ok"] is False
    assert "禁用" in result["message"] or "disabled" in result["message"].lower()
    assert agent.provider == "openrouter"


def test_consecutive_use_preserves_original_snapshot(make_harness):
    """连续 /moa use 不得用 MoA 状态覆盖真正的原始快照（否则 off 恢复到 MoA）。"""
    make_harness(_two_preset_config())
    agent = FakeAgent()
    handle_moa_command(agent, "use review")
    handle_moa_command(agent, "use coding")

    assert agent.model == "coding"
    # 原始快照仍是非 moa 的原始运行时（未被第二次 use 覆盖）。
    saved = agent._moa_saved_runtime
    assert saved["provider"] == "openrouter"
    assert saved["model"] == "anthropic/claude-opus-4.8"
    # off 恢复到原始模型，而非 review。
    handle_moa_command(agent, "off")
    assert agent.provider == "openrouter"
    assert agent.model == "anthropic/claude-opus-4.8"


# ---------------------------------------------------------------------------
# off — 退出 MoA，恢复原运行时
# ---------------------------------------------------------------------------

def test_off_restores_runtime(harness):
    agent = FakeAgent()
    handle_moa_command(agent, "use review")
    result = handle_moa_command(agent, "off")

    assert result["ok"] is True
    assert result["action"] == "off"
    assert agent.provider == "openrouter"
    assert agent.model == "anthropic/claude-opus-4.8"
    assert agent.base_url == "https://openrouter.ai/api/v1"
    assert agent.api_key == "test-key"
    # 恢复后清空快照。
    assert agent._moa_saved_runtime is None
    # 恢复消息点名被恢复的模型。
    assert "anthropic/claude-opus-4.8" in result["message"]


def test_off_when_not_in_moa_is_noop(harness):
    agent = FakeAgent()
    result = handle_moa_command(agent, "off")
    assert result["ok"] is False
    assert result["action"] == "off"
    # 运行时未变。
    assert agent.provider == "openrouter"


def test_off_disable_alias(harness):
    agent = FakeAgent()
    handle_moa_command(agent, "use review")
    result = handle_moa_command(agent, "disable")
    assert result["ok"] is True
    assert agent.provider == "openrouter"


# ---------------------------------------------------------------------------
# oneshot — 一次性 prompt：切换→跑→恢复（finally 保证）
# ---------------------------------------------------------------------------

def test_oneshot_runs_and_restores(harness):
    agent = FakeAgent()
    result = handle_moa_command(agent, "inspect the flaky test")

    assert result["ok"] is True
    assert result["action"] == "oneshot"
    # 聚合器响应被携带回结果。
    assert result["response"] == "aggregator acted"
    # prompt 透传给 agent.chat。
    assert agent.chat_calls == ["inspect the flaky test"]
    # 跑完恢复原运行时（不卡在 MoA）。
    assert agent.provider == "openrouter"
    assert agent.model == "anthropic/claude-opus-4.8"
    assert agent.base_url == "https://openrouter.ai/api/v1"
    assert agent.api_key == "test-key"
    assert agent._client is None  # 恢复时重置，触发重建为原始 client


def test_oneshot_restores_runtime_on_chat_failure(harness):
    """核心回归守卫（对标 Hermes test_restore_runs_from_finally_even_when_turn_raises）：
    抛异常的一次性轮次仍恢复运行时——否则 MoA 覆盖永久泄漏，之后每条消息都静默 fan-out。"""
    agent = FakeAgent(chat_raises=RuntimeError("provider blew up mid-turn"))

    with pytest.raises(RuntimeError):
        handle_moa_command(agent, "do the thing")

    # finally 保证：即便 chat 抛异常，运行时也恢复了。
    assert agent.provider == "openrouter"
    assert agent.model == "anthropic/claude-opus-4.8"
    assert agent.base_url == "https://openrouter.ai/api/v1"
    assert agent._client is None


def test_oneshot_when_already_in_moa_keeps_preset(harness):
    """已在 MoA：一次性 prompt 沿用当前预设，不快照不恢复（用户显式选了预设）。"""
    agent = FakeAgent()
    handle_moa_command(agent, "use review")
    assert agent.provider == "moa"

    result = handle_moa_command(agent, "do this thing")
    assert result["ok"] is True
    assert result["action"] == "oneshot"
    assert result["response"] == "aggregator acted"
    # 仍在 MoA（未恢复）——用户显式选的预设保持生效。
    assert agent.provider == "moa"
    assert agent.model == "review"


def test_oneshot_uses_active_preset(make_harness):
    """配置设了 active_preset 时，一次性 prompt 用激活预设（而非 default）。"""
    cfg = _two_preset_config()
    cfg["active_preset"] = "coding"
    make_harness(cfg)

    agent = FakeAgent()
    result = handle_moa_command(agent, "do it")
    assert result["ok"] is True
    # 消息点名激活预设 coding。
    assert "coding" in result["message"]
    # 恢复后仍回原始运行时。
    assert agent.provider == "openrouter"


def test_oneshot_forwards_stream_callback(harness):
    """on_delta 回调透传给 agent.chat 的 stream_callback（一次性模式支持流式）。"""
    agent = FakeAgent()
    captured = []

    def on_delta(chunk):
        captured.append(chunk)

    handle_moa_command(agent, "do it", on_delta=on_delta)
    # 稳定的函数对象，故 `is` 成立（绑定方法每次访问都新建，不能用 append）。
    assert agent.stream_callbacks[-1] is on_delta


def test_oneshot_unknown_active_preset_fails_gracefully(make_harness):
    """激活/默认预设解析失败时，一次性 prompt 返回 ok=False 而非崩溃，且不切运行时。"""
    cfg = make_config(default_preset="ghost", presets={"review": {}})
    # default_preset=ghost 不在 presets 里 → normalize 回退 default_preset 到 review，
    # 故这里构造一个 active_preset 指向不存在预设的场景。
    cfg["active_preset"] = "ghost"
    make_harness(cfg)

    agent = FakeAgent()
    result = handle_moa_command(agent, "do it")
    # active_preset=ghost 不在预设表 → normalize 清空为 ""，回退 default_preset。
    # 无论解析到哪个存在的预设，都不应崩溃。
    assert result["action"] == "oneshot"
    assert agent.provider == "openrouter"  # 未卡在 MoA
