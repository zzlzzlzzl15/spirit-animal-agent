"""conversation_loop 空结论自愈测试。

思考模型可能把输出配额全耗在推理上，最终 message.content 为空。旧行为把空串
当作"最终回答"直接 return，用户看到任务跑完却没有任何结论就回到提示符。
本组测试锁定修复后的行为：
1. 最终回复为空 → 注入收尾提示再推一轮 → 模型补出真实结论则正常返回；
2. 持续为空（收尾提示用尽）→ 返回非空兜底总结，绝不静默收尾；
3. _synthesize_fallback_summary 能从工具调用列表合成可读总结。
"""

import pytest

import spirit.agent.conversation_loop as cl
from spirit.agent.conversation_loop import (
    run_conversation,
    _synthesize_fallback_summary,
)


# =========================================================================
# 替身
# =========================================================================

class _Msg:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _Choice:
    def __init__(self, content, tool_calls=None):
        self.message = _Msg(content, tool_calls)
        self.finish_reason = "stop"


class _Resp:
    def __init__(self, content, tool_calls=None):
        self.choices = [_Choice(content, tool_calls)]
        self.usage = None


class _TurnCtx:
    def __init__(self, messages):
        self.messages = messages


class _FakeAgent:
    """满足 run_conversation 所需最小接口的假 agent（不执行任何工具）。"""

    def __init__(self):
        self._messages = []
        self.session_id = "test-session-0001"
        self.provider = "openai"
        self.model = "qwen3.8-max"
        self.base_url = ""
        self.max_iterations = 10
        self._interrupt_requested = False
        self._context_compressor = None
        self.client = object()
        self._api_call_count = 0
        self._tc = _TurnCtx(self._messages)

    def prepare_turn_context(self, user_message):
        self._messages.append({"role": "user", "content": user_message})
        return self._tc

    @property
    def messages(self):
        return self._messages

    def add_message(self, role, content, tool_calls=None):
        m = {"role": role, "content": content}
        if tool_calls:
            m["tool_calls"] = tool_calls
        self._messages.append(m)

    def get_system_prompt(self):
        return "sys"

    def get_tool_definitions(self):
        return []


@pytest.fixture
def scripted_llm(monkeypatch):
    """把 conversation_loop._call_llm 替换为按序返回预设响应的替身。"""
    def _install(responses):
        queue = list(responses)

        def _fake_call(agent, api_messages, tools, stream_callback=None):
            return queue.pop(0)

        monkeypatch.setattr(cl, "_call_llm", _fake_call)
        return queue

    return _install


# =========================================================================
# 1. 空结论 → 收尾提示 → 补出真实结论
# =========================================================================

def test_empty_final_then_nudge_yields_real_conclusion(scripted_llm):
    scripted_llm([_Resp(""), _Resp("最终结论：美联储 2025 年 12 月降息 25bp。")])
    agent = _FakeAgent()

    result = run_conversation(agent, "查一下美联储降息")

    assert result.response == "最终结论：美联储 2025 年 12 月降息 25bp。"
    # 收尾提示确实被注入过（user 角色系统提示）
    nudges = [
        m for m in agent.messages
        if m["role"] == "user" and "你上一轮没有输出任何内容" in (m["content"] or "")
    ]
    assert len(nudges) == 1


# =========================================================================
# 2. 持续为空 → 兜底总结（绝不静默）
# =========================================================================

def test_persistent_empty_final_returns_fallback_summary(scripted_llm):
    scripted_llm([_Resp("")] * 6)
    agent = _FakeAgent()

    result = run_conversation(agent, "查一下美联储降息")

    assert result.response, "空结论不得以空串收尾"
    assert result.response.startswith("[本轮未产出最终结论]")
    assert "共执行 0 次工具调用" in result.response


# =========================================================================
# 3. 兜底总结合成
# =========================================================================

def test_synthesize_fallback_summary_lists_recent_tools():
    tcs = [
        {"function": {"name": "web_search"}},
        {"function": {"name": "read_file"}},
    ]
    s = _synthesize_fallback_summary(tcs)
    assert "共执行 2 次工具调用" in s
    assert "web_search" in s and "read_file" in s


def test_synthesize_fallback_summary_empty_tools():
    s = _synthesize_fallback_summary([])
    assert s.startswith("[本轮未产出最终结论]")
    assert "共执行 0 次工具调用" in s
