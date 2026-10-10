"""Spirit Agent provider_switch_protection.py provider 切换保护回归测试（R-P1.4 v0.2.4 已交付未测）。

参考 hermes:
  - test_turn_liveness_watchdog.py（provider 切换语义）
  - test_turn_retry_state.py

3 类保护：
  1. provider 标签：每条消息打 _provider / _model 元数据（LLM 端不可见）
  2. 保留有效工具结果：切换不丢弃任何 tool message
  3. 切换提示注入：切换后给 messages 注入一条 system 消息
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from spirit.agent.provider_switch_protection import (
    META_FROM_MODEL,
    META_FROM_PROVIDER,
    META_MODEL,
    META_PROVIDER,
    META_ROUTE_STARTED_AT,
    META_SWITCH_REASON,
    ProviderRoute,
    annotate_messages_with_provider,
    apply_provider_labels,
    build_provider_switch_notice,
    extract_provider_route,
    get_provider_switch_state,
    mark_provider_route,
    protect_provider_switch,
    snapshot_agent_route,
)


# ─────────────────────────── 元数据常量 ───────────────────────────


class TestMetadataConstants:
    """6 个元数据常量字符串值校验（防止字符意外被改）。"""

    def test_meta_provider(self):
        assert META_PROVIDER == "_provider"

    def test_meta_model(self):
        assert META_MODEL == "_model"

    def test_meta_route_started_at(self):
        assert META_ROUTE_STARTED_AT == "_route_started_at"

    def test_meta_from_provider(self):
        assert META_FROM_PROVIDER == "_from_provider"

    def test_meta_from_model(self):
        assert META_FROM_MODEL == "_from_model"

    def test_meta_switch_reason(self):
        assert META_SWITCH_REASON == "_switch_reason"


# ─────────────────────────── ProviderRoute 数据类 ───────────────────────────


class TestProviderRoute:
    """ProviderRoute 不可变数据类。"""

    def test_basic_construction(self):
        route = ProviderRoute(provider="openai", model="gpt-4")
        assert route.provider == "openai"
        assert route.model == "gpt-4"

    def test_default_base_url_empty(self):
        route = ProviderRoute(provider="openai", model="gpt-4")
        assert route.base_url == ""

    def test_default_api_key_empty(self):
        route = ProviderRoute(provider="openai", model="gpt-4")
        assert route.api_key == ""

    def test_label_format(self):
        route = ProviderRoute(provider="anthropic", model="claude-3")
        assert route.label() == "anthropic/claude-3"

    def test_immutability_provider(self):
        route = ProviderRoute(provider="openai", model="gpt-4")
        with pytest.raises(FrozenInstanceError):
            route.provider = "anthropic"  # type: ignore

    def test_immutability_model(self):
        route = ProviderRoute(provider="openai", model="gpt-4")
        with pytest.raises(FrozenInstanceError):
            route.model = "gpt-3"  # type: ignore


# ─────────────────────────── mark_provider_route ───────────────────────────


class TestMarkProviderRoute:
    """mark_provider_route 给单条消息打标签。"""

    def test_returns_new_dict_not_mutating_original(self):
        msg = {"role": "user", "content": "hi"}
        route = ProviderRoute(provider="openai", model="gpt-4")
        new_msg = mark_provider_route(msg, route)
        # 原 dict 不应被修改
        assert META_PROVIDER not in msg
        # 新 dict 含标签
        assert new_msg[META_PROVIDER] == "openai"
        assert new_msg[META_MODEL] == "gpt-4"

    def test_preserves_existing_fields(self):
        msg = {"role": "user", "content": "hi", "tool_call_id": "tc-1"}
        route = ProviderRoute(provider="openai", model="gpt-4")
        new_msg = mark_provider_route(msg, route)
        assert new_msg["role"] == "user"
        assert new_msg["content"] == "hi"
        assert new_msg["tool_call_id"] == "tc-1"

    def test_started_at_optional(self):
        msg = {"role": "user", "content": "x"}
        route = ProviderRoute(provider="openai", model="gpt-4")
        new_msg = mark_provider_route(msg, route)
        assert META_ROUTE_STARTED_AT not in new_msg

    def test_started_at_included_when_provided(self):
        msg = {"role": "user", "content": "x"}
        route = ProviderRoute(provider="openai", model="gpt-4")
        new_msg = mark_provider_route(msg, route, started_at=12345.0)
        assert new_msg[META_ROUTE_STARTED_AT] == 12345.0


# ─────────────────────────── extract_provider_route ───────────────────────────


class TestExtractProviderRoute:
    """extract_provider_route 从消息读 provider 路由。"""

    def test_extract_returns_route(self):
        msg = {
            "role": "user",
            "content": "x",
            META_PROVIDER: "openai",
            META_MODEL: "gpt-4",
        }
        route = extract_provider_route(msg)
        assert route is not None
        assert route.provider == "openai"
        assert route.model == "gpt-4"

    def test_extract_returns_none_when_no_label(self):
        msg = {"role": "user", "content": "x"}
        assert extract_provider_route(msg) is None

    def test_extract_returns_none_when_partial_label(self):
        msg = {"role": "user", META_PROVIDER: "openai"}  # 无 model
        assert extract_provider_route(msg) is None


# ─────────────────────────── annotate_messages_with_provider ───────────────────────────


class TestAnnotateMessagesWithProvider:
    """annotate_messages_with_provider 给消息列表补标签。"""

    def test_skips_system_messages_by_default(self):
        msgs = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
        ]
        route = ProviderRoute(provider="openai", model="gpt-4")
        annotated = annotate_messages_with_provider(msgs, route)
        # system 不打标签
        assert META_PROVIDER not in annotated[0]
        # user 打标签
        assert annotated[1][META_PROVIDER] == "openai"

    def test_does_not_overwrite_existing_labels(self):
        msgs = [
            {
                "role": "user",
                "content": "hi",
                META_PROVIDER: "anthropic",
                META_MODEL: "claude-3",
            },
            {"role": "user", "content": "how are you?"},
        ]
        route = ProviderRoute(provider="openai", model="gpt-4")
        annotated = annotate_messages_with_provider(msgs, route)
        # 第一条已有标签不动
        assert annotated[0][META_PROVIDER] == "anthropic"
        assert annotated[0][META_MODEL] == "claude-3"
        # 第二条打新标签
        assert annotated[1][META_PROVIDER] == "openai"

    def test_include_system_messages_when_disabled(self):
        msgs = [{"role": "system", "content": "x"}]
        route = ProviderRoute(provider="openai", model="gpt-4")
        annotated = annotate_messages_with_provider(
            msgs, route, skip_system=False,
        )
        assert annotated[0][META_PROVIDER] == "openai"

    def test_empty_messages(self):
        route = ProviderRoute(provider="openai", model="gpt-4")
        assert annotate_messages_with_provider([], route) == []


# ─────────────────────────── build_provider_switch_notice ───────────────────────────


class TestBuildProviderSwitchNotice:
    """build_provider_switch_notice 构造切换提示 system 消息。"""

    def test_role_is_system(self):
        route_from = ProviderRoute(provider="openai", model="gpt-4")
        route_to = ProviderRoute(provider="anthropic", model="claude-3")
        notice = build_provider_switch_notice(route_from, route_to)
        assert notice["role"] == "system"

    def test_includes_from_provider(self):
        route_from = ProviderRoute(provider="openai", model="gpt-4")
        route_to = ProviderRoute(provider="anthropic", model="claude-3")
        notice = build_provider_switch_notice(route_from, route_to, "timeout")
        assert "openai" in notice["content"]
        assert "gpt-4" in notice["content"]

    def test_includes_to_provider(self):
        route_from = ProviderRoute(provider="openai", model="gpt-4")
        route_to = ProviderRoute(provider="anthropic", model="claude-3")
        notice = build_provider_switch_notice(route_from, route_to, "timeout")
        assert "anthropic" in notice["content"]
        assert "claude-3" in notice["content"]

    def test_includes_reason(self):
        route_from = ProviderRoute(provider="openai", model="gpt-4")
        route_to = ProviderRoute(provider="anthropic", model="claude-3")
        notice = build_provider_switch_notice(route_from, route_to, "rate_limit")
        assert "rate_limit" in notice["content"]

    def test_default_reason_is_unknown(self):
        route_from = ProviderRoute(provider="openai", model="gpt-4")
        route_to = ProviderRoute(provider="anthropic", model="claude-3")
        notice = build_provider_switch_notice(route_from, route_to)
        assert "未知失败" in notice["content"]

    def test_meta_from_provider_recorded(self):
        route_from = ProviderRoute(provider="openai", model="gpt-4")
        route_to = ProviderRoute(provider="anthropic", model="claude-3")
        notice = build_provider_switch_notice(route_from, route_to, "timeout")
        assert notice[META_FROM_PROVIDER] == "openai"
        assert notice[META_FROM_MODEL] == "gpt-4"

    def test_meta_switch_reason_recorded(self):
        route_from = ProviderRoute(provider="openai", model="gpt-4")
        route_to = ProviderRoute(provider="anthropic", model="claude-3")
        notice = build_provider_switch_notice(route_from, route_to, "timeout")
        assert notice[META_SWITCH_REASON] == "timeout"


# ─────────────────────────── protect_provider_switch 一站式保护 ───────────────────────────


class TestProtectProviderSwitch:
    """protect_provider_switch 一站式保护：打标签 + 注入提示。"""

    def test_returns_messages_and_notice(self):
        msgs = [{"role": "user", "content": "hi"}]
        route_from = ProviderRoute(provider="openai", model="gpt-4")
        route_to = ProviderRoute(provider="anthropic", model="claude-3")
        result, notice = protect_provider_switch(
            msgs, route_from, route_to, "timeout",
        )
        assert isinstance(result, list)
        assert notice["role"] == "system"

    def test_preserves_tool_messages(self):
        """关键语义：切换不丢弃任何 tool message。"""
        msgs = [
            {"role": "user", "content": "search python"},
            {
                "role": "assistant",
                "tool_calls": [{"id": "tc-1", "function": {"name": "search"}}],
            },
            {"role": "tool", "tool_call_id": "tc-1", "content": "result"},
        ]
        route_from = ProviderRoute(provider="openai", model="gpt-4")
        route_to = ProviderRoute(provider="anthropic", model="claude-3")
        result, _ = protect_provider_switch(
            msgs, route_from, route_to, "timeout",
        )
        # tool message 应保留
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "tc-1"

    def test_inject_notice_inserts_after_last_non_system(self):
        msgs = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        route_from = ProviderRoute(provider="openai", model="gpt-4")
        route_to = ProviderRoute(provider="anthropic", model="claude-3")
        result, notice = protect_provider_switch(
            msgs, route_from, route_to, "timeout",
        )
        # notice 应在 assistant 之后（最后一条非 system 之后）
        assert result[-1]["role"] == "system"
        assert "[系统提示" in result[-1]["content"]
        # system prompt（首位）应保留在最前
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "you are helpful"

    def test_no_inject_when_disabled(self):
        msgs = [{"role": "user", "content": "hi"}]
        route_from = ProviderRoute(provider="openai", model="gpt-4")
        route_to = ProviderRoute(provider="anthropic", model="claude-3")
        result, notice = protect_provider_switch(
            msgs, route_from, route_to, "timeout",
            inject_notice=False,
        )
        # 不应有 notice 注入
        assert len(result) == len(msgs)
        # 但 notice 仍然返回（用于 caller 自行决定）
        assert notice["role"] == "system"

    def test_messages_annotated_with_new_route(self):
        msgs = [{"role": "user", "content": "hi"}]
        route_from = ProviderRoute(provider="openai", model="gpt-4")
        route_to = ProviderRoute(provider="anthropic", model="claude-3")
        result, _ = protect_provider_switch(
            msgs, route_from, route_to, "timeout",
        )
        # user 消息打新 provider 标签
        user_msg = [m for m in result if m.get("role") == "user"][0]
        assert user_msg[META_PROVIDER] == "anthropic"


# ─────────────────────────── snapshot_agent_route / get_provider_switch_state ───────────────────────────


class TestSnapshotAgentRoute:
    """snapshot_agent_route 从 agent 当前属性读 ProviderRoute。"""

    def test_prefers_primary_runtime(self):
        # Mock agent：有 _primary_runtime
        class MockAgent:
            _primary_runtime = {
                "provider": "openai",
                "model": "gpt-4",
                "base_url": "https://api.openai.com",
                "api_key": "sk-x",
            }
            provider = "anthropic"
            model = "claude-3"

        agent = MockAgent()
        route = snapshot_agent_route(agent)
        # _primary_runtime 优先
        assert route.provider == "openai"
        assert route.model == "gpt-4"
        assert route.base_url == "https://api.openai.com"

    def test_fallback_to_agent_attributes(self):
        class MockAgent:
            provider = "anthropic"
            model = "claude-3"
            base_url = "https://api.anthropic.com"
            api_key = "sk-y"
            _primary_runtime = None

        agent = MockAgent()
        route = snapshot_agent_route(agent)
        assert route.provider == "anthropic"
        assert route.model == "claude-3"


class TestGetProviderSwitchState:
    """get_provider_switch_state debug 状态快照。"""

    def test_returns_full_state_dict(self):
        class MockAgent:
            _primary_runtime = {"provider": "openai"}
            _fallback_activated = True
            _fallback_index = 1
            _primary_cooldown_until = 1700000000.0
            provider = "anthropic"
            model = "claude-3"

        agent = MockAgent()
        state = get_provider_switch_state(agent)
        assert state["fallback_activated"] is True
        assert state["fallback_index"] == 1
        assert state["primary_cooldown_until"] == 1700000000.0
        assert state["current_provider"] == "anthropic"
        assert state["current_model"] == "claude-3"

    def test_returns_defaults_when_attrs_missing(self):
        class MockAgent:
            pass

        agent = MockAgent()
        state = get_provider_switch_state(agent)
        assert state["fallback_activated"] is False
        assert state["fallback_index"] == 0
        assert state["primary_cooldown_until"] == 0


# ─────────────────────────── apply_provider_labels ───────────────────────────


class TestApplyProviderLabels:
    """apply_provider_labels：补齐标签 + 剥离（准备发 API）。"""

    def test_strips_meta_fields_by_default(self):
        msgs = [
            {
                "role": "user",
                "content": "hi",
                META_PROVIDER: "openai",
                META_MODEL: "gpt-4",
            },
        ]
        route = ProviderRoute(provider="openai", model="gpt-4")
        result = apply_provider_labels(msgs, route)
        # 标签被剥离（不影响 LLM 看到的格式）
        assert META_PROVIDER not in result[0]
        assert META_MODEL not in result[0]
        # 业务字段保留
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "hi"

    def test_preserves_business_fields(self):
        msgs = [
            {
                "role": "user",
                "content": "hi",
                "tool_call_id": "tc-1",
                META_PROVIDER: "openai",
                META_FROM_PROVIDER: "anthropic",
            },
        ]
        route = ProviderRoute(provider="openai", model="gpt-4")
        result = apply_provider_labels(msgs, route)
        assert result[0]["tool_call_id"] == "tc-1"
        # 所有 _provider / _from_provider 等元数据被剥离
        for k in result[0]:
            assert not k.startswith("_provider"), f"残留元数据: {k}"
            assert not k.startswith("_from_provider"), f"残留元数据: {k}"

    def test_strip_labels_false_keeps_labels(self):
        msgs = [{"role": "user", "content": "hi"}]
        route = ProviderRoute(provider="openai", model="gpt-4")
        result = apply_provider_labels(msgs, route, strip_labels=False)
        # 标签保留
        assert result[0][META_PROVIDER] == "openai"

    def test_annots_missing_labels_then_strips(self):
        """缺标签的消息应被补齐再剥离（不留漏洞）。"""
        msgs = [{"role": "user", "content": "hi"}]  # 无标签
        route = ProviderRoute(provider="openai", model="gpt-4")
        result = apply_provider_labels(msgs, route)
        # 标签被补齐 + 剥离，最终无标签
        assert META_PROVIDER not in result[0]


# ─────────────────────────── 端到端：切换 → 准备 API ───────────────────────────


class TestEndToEndSwitchFlow:
    """端到端：切换 → 注入 notice → 准备 API 消息 → 标签剥离。"""

    def test_full_switch_then_prepare_api(self):
        # 初始消息
        msgs = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        route_from = ProviderRoute(provider="openai", model="gpt-4")
        route_to = ProviderRoute(provider="anthropic", model="claude-3")

        # 切换
        protected, notice = protect_provider_switch(
            msgs, route_from, route_to, "rate_limit",
        )

        # 准备 API 消息（剥离标签）
        api_msgs = apply_provider_labels(protected, route_to)

        # 验证：
        # 1. system prompt 在最前
        assert api_msgs[0]["role"] == "system"
        assert api_msgs[0]["content"] == "you are helpful"

        # 2. 切换提示作为 system 消息被保留（在 user/assistant 之后）
        system_msgs = [m for m in api_msgs if m["role"] == "system"]
        assert len(system_msgs) == 2

        # 3. 业务字段保留
        assert any(
            m["role"] == "user" and m["content"] == "hi"
            for m in api_msgs
        )
        assert any(
            m["role"] == "assistant" and m["content"] == "hello"
            for m in api_msgs
        )

        # 4. 所有 _provider / _model 元数据被剥离
        for m in api_msgs:
            for k in m:
                assert not k.startswith("_") or k in {"role", "content"}, (
                    f"残留元数据: {k}"
                )
