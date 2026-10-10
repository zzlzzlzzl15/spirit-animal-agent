"""Spirit Agent turn_context.py turn prologue 构建回归测试（R-P1.6 v0.2.4 已交付未测）。

参考 hermes:
    - hermes agent/turn_context.py (626 行)

TurnContext 数据类 + build_turn_context 9 步 prologue（safe stdio → 清理 → system prompt → session → counters → prefetch → plugin → messages → memory review）。
"""

from __future__ import annotations

import io
import os
import sys

import pytest

from spirit.agent.turn_context import (
    TurnContext,
    _install_safe_stdio,
    _sanitize_surrogates,
    build_turn_context,
)


# ─────────────────────────── TurnContext 数据类 ───────────────────────────


class TestTurnContextDataClass:
    """TurnContext 11 字段数据类。"""

    def test_minimal_construction(self):
        ctx = TurnContext(
            user_message="hello",
            original_user_message="hello",
            messages=[],
            conversation_history=None,
            active_system_prompt=None,
            effective_task_id="task-1",
            turn_id="task-1-1",
            current_turn_user_idx=0,
        )
        assert ctx.user_message == "hello"
        assert ctx.original_user_message == "hello"
        assert ctx.messages == []
        assert ctx.conversation_history is None
        assert ctx.active_system_prompt is None
        assert ctx.effective_task_id == "task-1"
        assert ctx.turn_id == "task-1-1"
        assert ctx.current_turn_user_idx == 0

    def test_default_should_review_memory_false(self):
        ctx = TurnContext(
            user_message="x", original_user_message="x",
            messages=[], conversation_history=None,
            active_system_prompt=None,
            effective_task_id="t", turn_id="t-1", current_turn_user_idx=0,
        )
        assert ctx.should_review_memory is False

    def test_default_plugin_user_context_empty(self):
        ctx = TurnContext(
            user_message="x", original_user_message="x",
            messages=[], conversation_history=None,
            active_system_prompt=None,
            effective_task_id="t", turn_id="t-1", current_turn_user_idx=0,
        )
        assert ctx.plugin_user_context == ""

    def test_default_ext_prefetch_cache_empty(self):
        ctx = TurnContext(
            user_message="x", original_user_message="x",
            messages=[], conversation_history=None,
            active_system_prompt=None,
            effective_task_id="t", turn_id="t-1", current_turn_user_idx=0,
        )
        assert ctx.ext_prefetch_cache == ""

    def test_messages_list_independent(self):
        """messages 是 dataclass field，引用独立（mutable list）。"""
        msgs = [{"role": "user", "content": "hi"}]
        ctx = TurnContext(
            user_message="x", original_user_message="x",
            messages=msgs, conversation_history=None,
            active_system_prompt=None,
            effective_task_id="t", turn_id="t-1", current_turn_user_idx=0,
        )
        ctx.messages.append({"role": "assistant", "content": "hello"})
        # 原始 msgs 也被修改（list 引用共享）
        assert len(msgs) == 2

    def test_conversation_history_can_be_list(self):
        history = [{"role": "user", "content": "prev"}]
        ctx = TurnContext(
            user_message="x", original_user_message="x",
            messages=[], conversation_history=history,
            active_system_prompt=None,
            effective_task_id="t", turn_id="t-1", current_turn_user_idx=0,
        )
        assert ctx.conversation_history is history


# ─────────────────────────── _sanitize_surrogates ───────────────────────────


class TestSanitizeSurrogates:
    """_sanitize_surrogates Unicode 代理对清理。"""

    def test_normal_text_unchanged(self):
        assert _sanitize_surrogates("Hello World") == "Hello World"

    def test_empty_string(self):
        assert _sanitize_surrogates("") == ""

    def test_chinese_text_unchanged(self):
        text = "你好世界，这是中文"
        assert _sanitize_surrogates(text) == text

    def test_unicode_emoji_unchanged(self):
        text = "Hello 🌍 World"
        assert _sanitize_surrogates(text) == text

    def test_invalid_surrogate_replaced(self):
        """含 lone surrogate → 替换为 U+FFFD。"""
        # 单独的高位代理（U+D800）不是合法的 UTF-8
        bad = "Hello\ud800World"
        result = _sanitize_surrogates(bad)
        # 不抛错，且不再含 lone surrogate
        assert isinstance(result, str)
        # 重新 encode/decode 应不抛错
        result.encode('utf-8').decode('utf-8')

    def test_non_string_input_converted(self):
        """非 str 输入 → 先 str() 转换。"""
        result = _sanitize_surrogates(123)
        assert result == "123"

    def test_non_string_none(self):
        result = _sanitize_surrogates(None)
        # None → "None"
        assert result == "None"


# ─────────────────────────── _install_safe_stdio ───────────────────────────


class TestInstallSafeStdio:
    """_install_safe_stdio broken pipe 保护。"""

    def test_normal_stdio_does_not_redirect(self):
        """stdout/stderr 正常时不重定向。"""
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        try:
            _install_safe_stdio()
            # 不应改变 stdout/stderr
            assert sys.stdout is original_stdout
            assert sys.stderr is original_stderr
        finally:
            # 还原
            sys.stdout = original_stdout
            sys.stderr = original_stderr

    def test_broken_stdout_triggers_name_error_current_bug(self):
        """spirit 现状 bug：_install_safe_stdio 函数的 except 分支引用 io.UnsupportedOperation，
        但函数体未 import io（spirit build_turn_context 里有 import io，但该函数没有）。

        所以预期 spirit 会抛 NameError，不是把 stdout 重定向到 devnull。
        修复方向：在 _install_safe_stdio 函数体加 ``import io``。
        """
        original_stdout = sys.stdout
        try:
            class BrokenStdout:
                def fileno(self):
                    raise OSError("broken pipe")

            sys.stdout = BrokenStdout()
            # spirit 现状：会抛 NameError（io 未 import）
            with pytest.raises(NameError, match="io"):
                _install_safe_stdio()
        finally:
            sys.stdout = original_stdout

    def test_broken_stderr_triggers_name_error_current_bug(self):
        """同样的 bug 在 stderr 路径。"""
        original_stderr = sys.stderr
        try:
            class BrokenStderr:
                def fileno(self):
                    raise OSError("broken pipe")

            sys.stderr = BrokenStderr()
            with pytest.raises(NameError, match="io"):
                _install_safe_stdio()
        finally:
            sys.stderr = original_stderr


# ─────────────────────────── Mock Agent for build_turn_context ───────────────────────────


class MockAgent:
    """用于测试 build_turn_context 的最小 mock SpiritAgent。"""

    def __init__(
        self,
        messages=None,
        session_id=None,
        max_iterations=50,
        api_call_count=0,
    ):
        from spirit.agent.iteration_budget import IterationBudget
        self.messages = messages or []
        self.session_id = session_id
        self.max_iterations = max_iterations
        self._api_call_count = api_call_count
        self._cached_system_prompt = None
        self._delivered_interim_texts = {"prev"}
        self._auth_pool_refresh_counts = {"old": 1}
        # 默认给一个真实 budget（避免触发 spirit line 218 的 AttributeError bug：
        # spirit 用 ``not hasattr(agent, '_iteration_budget') or
        # agent._iteration_budget.used >= ...``，当属性存在但值为 None 时会
        # AttributeError。本 mock 用真实 IterationBudget 绕开这个 bug，
        # 并在独立测试中专门验证这个 bug 行为。
        self._iteration_budget = IterationBudget(max_total=max_iterations)
        # 可选 memory / plugin manager
        self._memory_manager = None
        self._plugin_manager = None
        self._turns_since_memory_review = 0
        self._memory_review_interval = 10

    def get_system_prompt(self):
        return "you are a helpful assistant (mock)"

    def add_message(self, role, content):
        self.messages.append({"role": role, "content": content})


class MockMemoryManager:
    def __init__(self, should_raise=False):
        self.should_raise = should_raise

    def prefetch_async(self, task_id, user_message):
        if self.should_raise:
            raise RuntimeError("prefetch failed")


class MockPluginManager:
    def __init__(self, return_value="", should_raise=False):
        self.return_value = return_value
        self.should_raise = should_raise

    def get_user_context(self, agent, user_message):
        if self.should_raise:
            raise RuntimeError("plugin failed")
        return self.return_value


# ─────────────────────────── build_turn_context 首次 turn ───────────────────────────


class TestBuildTurnContextFirstTurn:
    """首次 turn：build system prompt + add_message。"""

    def test_first_turn_builds_system_prompt(self):
        agent = MockAgent(messages=[])  # 空 messages
        ctx = build_turn_context(
            agent=agent,
            user_message="hello",
            system_message=None,
            conversation_history=None,
            task_id=None,
        )
        # spirit line 181：system prompt 应被 add_message 进 agent.messages
        # R-P2.x：user 消息也走 add_message 同步落库（防止空会话 preview）
        assert len(agent.messages) == 2
        assert agent.messages[0]["role"] == "system"
        assert agent.messages[1]["role"] == "user"
        assert "helpful" in agent.messages[0]["content"]
        assert agent.messages[1]["content"] == "hello"
        # ctx.active_system_prompt 应有值
        assert ctx.active_system_prompt is not None
        # ctx.messages 是 working list：含 system + user
        assert len(ctx.messages) == 2
        assert ctx.messages[0]["role"] == "system"
        assert ctx.messages[1]["role"] == "user"

    def test_first_turn_user_message_appended(self):
        agent = MockAgent(messages=[])
        ctx = build_turn_context(
            agent=agent,
            user_message="hi there",
            system_message=None,
            conversation_history=None,
            task_id=None,
        )
        # ctx.messages 应含 system + user
        assert ctx.messages[-1]["role"] == "user"
        assert ctx.messages[-1]["content"] == "hi there"

    def test_user_message_sanitized(self):
        """含 surrogate 的 user_message 应被清理。"""
        agent = MockAgent(messages=[])
        # 含 lone surrogate
        bad_msg = "Hello\ud800World"
        ctx = build_turn_context(
            agent=agent,
            user_message=bad_msg,
            system_message=None,
            conversation_history=None,
            task_id=None,
        )
        # 应不抛错，且 ctx.user_message 不再含 lone surrogate
        assert isinstance(ctx.user_message, str)
        ctx.user_message.encode('utf-8').decode('utf-8')

    def test_original_user_message_preserved(self):
        """persist_user_message=None → original = user_message。"""
        agent = MockAgent(messages=[])
        ctx = build_turn_context(
            agent=agent,
            user_message="hello",
            system_message=None,
            conversation_history=None,
            task_id=None,
            persist_user_message=None,
        )
        assert ctx.original_user_message == "hello"

    def test_persist_user_message_override(self):
        """persist_user_message 提供时覆盖 original。"""
        agent = MockAgent(messages=[])
        ctx = build_turn_context(
            agent=agent,
            user_message="cleaned",
            system_message=None,
            conversation_history=None,
            task_id=None,
            persist_user_message="original with surrogates\ud800",
        )
        # original 用 persist_user_message
        assert ctx.original_user_message == "original with surrogates\ud800"
        # user_message 用清理后的
        assert "cleaned" in ctx.user_message


# ─────────────────────────── build_turn_context 后续 turn ───────────────────────────


class TestBuildTurnContextSubsequentTurn:
    """后续 turn：复用 cached prompt + 不重复 build。"""

    def test_subsequent_turn_reuses_cache(self):
        # 模拟已有 cached prompt
        agent = MockAgent(messages=[{"role": "system", "content": "cached"}])
        agent._cached_system_prompt = "cached system prompt"

        original_count = len(agent.messages)
        ctx = build_turn_context(
            agent=agent,
            user_message="hi",
            system_message=None,
            conversation_history=None,
            task_id=None,
        )
        # spirit 不重复 add system prompt 到 agent.messages
        # R-P2.x：但 user 消息会 add_message（同步落库）— agent.messages +1
        assert len(agent.messages) == original_count + 1
        assert agent.messages[-1]["role"] == "user"
        # ctx.active_system_prompt 应是 cached 值
        assert ctx.active_system_prompt == "cached system prompt"
        # ctx.messages 是 working list：含原有 system + 新 user
        assert len(ctx.messages) == original_count + 1
        assert ctx.messages[-1]["role"] == "user"

    def test_subsequent_turn_rebuilds_when_cache_missing(self):
        """cached prompt 缺失 → 重新 build。"""
        agent = MockAgent(messages=[{"role": "system", "content": "old"}])
        agent._cached_system_prompt = None  # cache 缺失

        ctx = build_turn_context(
            agent=agent,
            user_message="hi",
            system_message=None,
            conversation_history=None,
            task_id=None,
        )
        # 重新 build（不是 None）
        assert ctx.active_system_prompt is not None
        assert "helpful" in ctx.active_system_prompt


# ─────────────────────────── build_turn_context session 状态 ───────────────────────────


class TestBuildTurnContextSessionState:
    """task_id 来源 + turn_id 格式。"""

    def test_task_id_from_explicit_argument(self):
        agent = MockAgent()
        ctx = build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id="explicit-task-id",
        )
        assert ctx.effective_task_id == "explicit-task-id"

    def test_task_id_from_session_id_when_no_explicit(self):
        agent = MockAgent(session_id="session-1")
        ctx = build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
        )
        assert ctx.effective_task_id == "session-1"

    def test_task_id_generated_when_none_available(self):
        agent = MockAgent(session_id=None)
        ctx = build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
        )
        # 应生成 UUID
        assert ctx.effective_task_id is not None
        assert len(ctx.effective_task_id) > 10  # UUID 长度

    def test_turn_id_format(self):
        """turn_id = task_id + '-' + (api_call_count + 1)。"""
        agent = MockAgent(api_call_count=3)
        ctx = build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id="task-1",
        )
        assert ctx.turn_id == "task-1-4"

    def test_current_turn_user_idx(self):
        """current_turn_user_idx = len(agent.messages) (before adding user)。"""
        agent = MockAgent(messages=[
            {"role": "system", "content": "x"},
            {"role": "user", "content": "prev"},
        ])
        ctx = build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id="t",
        )
        # 包含 system + prev user = 2
        assert ctx.current_turn_user_idx == 2


# ─────────────────────────── build_turn_context 重置 counters ───────────────────────────


class TestBuildTurnContextResetCounters:
    """重置 per-turn counters。"""

    def test_resets_delivered_interim_texts(self):
        agent = MockAgent()
        # 已有内容
        agent._delivered_interim_texts = {"old_text"}
        build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
        )
        # 应被重置为空 set
        assert agent._delivered_interim_texts == set()

    def test_resets_auth_pool_refresh_counts(self):
        agent = MockAgent()
        agent._auth_pool_refresh_counts = {"x": 1, "y": 2}
        build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
        )
        assert agent._auth_pool_refresh_counts == {}

    def test_iteration_budget_attribute_exists_required(self):
        """spirit line 218 bug：agent 必须有 ``_iteration_budget`` 属性且不能为 None。

        原因是 spirit 代码：
        ``if not hasattr(agent, '_iteration_budget') or agent._iteration_budget.used >= ...``

        如果属性存在但值为 None（未初始化），该表达式短路到
        ``agent._iteration_budget.used`` → AttributeError。

        验证：能读取 agent._iteration_budget.used（说明 mock 默认给了真实 budget）。
        """
        agent = MockAgent()
        # 直接读取 .used 不抛错 → mock 给了真实 budget
        _ = agent._iteration_budget.used
        _ = agent._iteration_budget.max_total

    def test_iteration_budget_none_raises_attribute_error_current_bug(self):
        """spirit 现状 bug 验证：属性存在但值为 None → AttributeError。

        修复方向：spirit 应改成 ``and agent._iteration_budget is not None``。
        """
        agent = MockAgent()
        agent._iteration_budget = None  # 属性在但值为 None
        # spirit 现状会 AttributeError
        with pytest.raises(AttributeError):
            build_turn_context(
                agent=agent, user_message="x",
                system_message=None, conversation_history=None,
                task_id=None,
            )

    def test_iteration_budget_reset_when_exhausted(self):
        """budget.used >= max_total → 重新创建。"""
        from spirit.agent.iteration_budget import IterationBudget

        agent = MockAgent()
        # 耗尽的 budget
        budget = IterationBudget(max_total=10)
        budget.used = 10  # == max_total
        agent._iteration_budget = budget
        build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
        )
        # 应被替换为新的 budget（不是原来的 budget 对象）
        assert agent._iteration_budget is not budget
        assert agent._iteration_budget.used == 0

    def test_iteration_budget_kept_when_under_limit(self):
        """budget.used < max_total → 保留。"""
        from spirit.agent.iteration_budget import IterationBudget

        agent = MockAgent()
        budget = IterationBudget(max_total=50)
        budget.used = 5  # 未耗尽
        agent._iteration_budget = budget
        build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
        )
        assert agent._iteration_budget is budget
        assert agent._iteration_budget.used == 5


# ─────────────────────────── build_turn_context prefetch memory ───────────────────────────


class TestBuildTurnContextPrefetch:
    """external memory 异步 prefetch。"""

    def test_no_memory_manager_no_prefetch(self):
        agent = MockAgent()
        agent._memory_manager = None
        # 不应抛错
        ctx = build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
        )
        # 默认值
        assert ctx.ext_prefetch_cache == ""

    def test_memory_manager_prefetch_called(self):
        agent = MockAgent()
        mm = MockMemoryManager()
        agent._memory_manager = mm
        ctx = build_turn_context(
            agent=agent, user_message="hello",
            system_message=None, conversation_history=None,
            task_id="task-1",
        )
        # prefetch_async 被调用（这里没有 trace，靠不抛错验证）

    def test_memory_manager_exception_does_not_raise(self):
        agent = MockAgent()
        mm = MockMemoryManager(should_raise=True)
        agent._memory_manager = mm
        # 不应抛错（spirit 现状 catch Exception）
        ctx = build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
        )
        assert ctx is not None


# ─────────────────────────── build_turn_context plugin context ───────────────────────────


class TestBuildTurnContextPluginContext:
    """plugin user context 注入。"""

    def test_no_plugin_manager_empty_context(self):
        agent = MockAgent()
        agent._plugin_manager = None
        ctx = build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
        )
        assert ctx.plugin_user_context == ""

    def test_plugin_manager_returns_context(self):
        agent = MockAgent()
        pm = MockPluginManager(return_value="plugin info")
        agent._plugin_manager = pm
        ctx = build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
        )
        assert ctx.plugin_user_context == "plugin info"

    def test_plugin_manager_returns_empty(self):
        agent = MockAgent()
        pm = MockPluginManager(return_value="")
        agent._plugin_manager = pm
        ctx = build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
        )
        assert ctx.plugin_user_context == ""

    def test_plugin_manager_exception_does_not_raise(self):
        agent = MockAgent()
        pm = MockPluginManager(should_raise=True)
        agent._plugin_manager = pm
        # 不应抛错
        ctx = build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
        )
        assert ctx.plugin_user_context == ""


# ─────────────────────────── build_turn_context memory review ───────────────────────────


class TestBuildTurnContextMemoryReview:
    """should_review_memory 触发条件。"""

    def test_no_memory_manager_no_review(self):
        agent = MockAgent()
        agent._memory_manager = None
        ctx = build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
        )
        assert ctx.should_review_memory is False

    def test_turns_below_interval_no_review(self):
        agent = MockAgent()
        agent._memory_manager = MockMemoryManager()
        agent._turns_since_memory_review = 5  < 10
        ctx = build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
        )
        assert ctx.should_review_memory is False

    def test_turns_at_interval_triggers_review(self):
        agent = MockAgent()
        agent._memory_manager = MockMemoryManager()
        agent._turns_since_memory_review = 10  # == interval
        ctx = build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
        )
        assert ctx.should_review_memory is True

    def test_turns_above_interval_triggers_review(self):
        agent = MockAgent()
        agent._memory_manager = MockMemoryManager()
        agent._turns_since_memory_review = 15  # > 10
        ctx = build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
        )
        assert ctx.should_review_memory is True

    def test_custom_review_interval(self):
        agent = MockAgent()
        agent._memory_manager = MockMemoryManager()
        agent._turns_since_memory_review = 3
        agent._memory_review_interval = 5
        ctx = build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
        )
        assert ctx.should_review_memory is False  # 3 < 5
        agent._turns_since_memory_review = 5
        ctx2 = build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
        )
        assert ctx2.should_review_memory is True


# ─────────────────────────── 端到端集成 ───────────────────────────


class TestEndToEndIntegration:
    """完整 build_turn_context 流水线。"""

    def test_minimal_happy_path(self):
        """最小 happy path：mock agent → build_turn_context → 返回有效 ctx。"""
        agent = MockAgent(messages=[])
        ctx = build_turn_context(
            agent=agent, user_message="hello world",
            system_message=None, conversation_history=None,
            task_id="task-1",
        )
        # 关键字段都有
        assert ctx.user_message == "hello world"
        assert ctx.original_user_message == "hello world"
        assert ctx.effective_task_id == "task-1"
        assert ctx.turn_id == "task-1-1"
        assert ctx.active_system_prompt is not None
        assert len(ctx.messages) == 2  # system + user
        assert ctx.current_turn_user_idx == 1
        # counters 被重置
        assert agent._delivered_interim_texts == set()
        assert agent._auth_pool_refresh_counts == {}
        # iteration budget 被初始化
        assert agent._iteration_budget is not None

    def test_with_conversation_history(self):
        agent = MockAgent(messages=[{"role": "system", "content": "x"}])
        agent._cached_system_prompt = "x"
        history = [{"role": "user", "content": "prev"}]
        ctx = build_turn_context(
            agent=agent, user_message="hi",
            system_message=None, conversation_history=history,
            task_id=None,
        )
        # conversation_history 应被透传
        assert ctx.conversation_history is history

    def test_stream_callback_accepted(self):
        """stream_callback 参数被接受（不影响行为）。"""
        agent = MockAgent()
        callback_calls = []

        def my_callback(chunk):
            callback_calls.append(chunk)

        # 不应抛错
        ctx = build_turn_context(
            agent=agent, user_message="x",
            system_message=None, conversation_history=None,
            task_id=None,
            stream_callback=my_callback,
        )
        assert ctx is not None
        # callback 不应被 build_turn_context 主动调用
        # （stream callback 通常在 LLM 调用时触发）
        # 这里只验证参数被接受