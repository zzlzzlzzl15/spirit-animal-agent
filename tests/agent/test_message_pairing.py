"""tests/agent/test_message_pairing.py — tool_call / tool_result 配对不变量。

背景：v0.3.4 之前，上下文压缩会把 ``assistant(tool_calls)`` 与其 ``tool``
结果切到不同区段，产出 provider 直接拒绝的 400（tool result's tool id not
found / tool call result does not follow tool call），且错误被归类为可重试的
``unknown`` → 同一个非法 payload 被重发 3 次 → 每轮都失败，只能重启进程。

本文件锁死修复后的不变量：任何形态的历史经压缩 / 修复后，payload 必须合法。
"""

from __future__ import annotations

import json

import pytest

from spirit.agent.message_pairing import (
    PairingReport,
    is_valid_tool_pairing,
    repair_tool_pairing,
    validate_tool_pairing,
)


def _assistant(call_id: str, text: str = "") -> dict:
    return {
        "role": "assistant",
        "content": text or None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "web_fetch", "arguments": json.dumps({"url": "http://x"})},
            }
        ],
    }


def _tool_result(call_id: str, content: str = "结果") -> dict:
    return {
        "role": "tool",
        "content": content,
        "tool_call_id": call_id,
        "name": "web_fetch",
    }


def _build_history(rounds: int, tail_user: bool = True) -> list:
    """真实形态的历史：system + user + [assistant(tool_calls), tool] * N + user。"""
    msgs = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "帮我查资料"},
    ]
    for i in range(rounds):
        msgs.append(_assistant(f"call_{i}"))
        msgs.append(_tool_result(f"call_{i}"))
    if tail_user:
        msgs.append({"role": "user", "content": "继续"})
    return msgs


# ── 校验 ────────────────────────────────────────────────────────────────────

class TestValidateToolPairing:

    def test_valid_history_is_ok(self):
        report = validate_tool_pairing(_build_history(4))
        assert report.ok
        assert report.issue_count == 0

    def test_orphan_tool_result_detected(self):
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "tool", "content": "孤儿", "tool_call_id": "call_x"},
        ]
        report = validate_tool_pairing(msgs)
        assert not report.ok
        assert "call_x" in report.orphan_tool_results

    def test_unanswered_tool_call_detected(self):
        msgs = [
            {"role": "system", "content": "s"},
            _assistant("call_0"),
        ]
        report = validate_tool_pairing(msgs)
        assert not report.ok
        assert "call_0" in report.unanswered_tool_calls

    def test_misordered_tool_result_detected(self):
        """tool 结果出现在其父 assistant 之前 → 错位。"""
        msgs = [
            {"role": "system", "content": "s"},
            _tool_result("call_0"),
            _assistant("call_0"),
        ]
        report = validate_tool_pairing(msgs)
        assert not report.ok
        assert "call_0" in report.misordered_tool_results

    def test_empty_tool_call_id_flagged(self):
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "tool", "content": "x", "tool_call_id": ""},
        ]
        report = validate_tool_pairing(msgs)
        assert not report.ok
        assert report.orphan_tool_results

    def test_malformed_input_returns_empty_report(self):
        report = validate_tool_pairing(None)
        assert report.ok
        assert isinstance(report, PairingReport)

    def test_is_valid_tool_pairing_helper(self):
        assert is_valid_tool_pairing(_build_history(3)) is True
        assert is_valid_tool_pairing([_assistant("call_0")]) is False


# ── 修复 ────────────────────────────────────────────────────────────────────

class TestRepairToolPairing:

    def test_orphan_tool_result_dropped(self):
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            _tool_result("call_ghost", "孤儿"),
        ]
        repaired, report = repair_tool_pairing(msgs)
        assert len(repaired) == 2
        assert all(m.get("role") != "tool" for m in repaired)
        assert report.dropped_messages == 1
        assert report.ok

    def test_unanswered_tool_call_stripped_and_assistant_dropped(self):
        """assistant 无结果且无文本 → 整条丢弃。"""
        msgs = [
            {"role": "system", "content": "s"},
            _assistant("call_0"),
        ]
        repaired, report = repair_tool_pairing(msgs)
        assert len(repaired) == 1
        assert report.repaired_tool_calls == 1
        assert report.dropped_messages == 1
        assert report.ok

    def test_unanswered_tool_call_downgraded_to_text(self):
        """assistant 无结果但有文本 → 降级成纯文本 assistant。"""
        msgs = [
            {"role": "system", "content": "s"},
            _assistant("call_0", text="我先说句话"),
        ]
        repaired, report = repair_tool_pairing(msgs)
        assert len(repaired) == 2
        assert "tool_calls" not in repaired[1]
        assert repaired[1]["content"] == "我先说句话"
        assert report.ok

    def test_partial_tool_calls_kept(self):
        """assistant 有 2 个调用，1 个有结果 → 只保留有结果的那个。"""
        msgs = [
            {"role": "system", "content": "s"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                    {"id": "b", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                ],
            },
            _tool_result("a"),
        ]
        repaired, report = repair_tool_pairing(msgs)
        assert len(repaired) == 3
        assert [tc["id"] for tc in repaired[1]["tool_calls"]] == ["a"]
        assert report.repaired_tool_calls == 1
        assert report.ok

    def test_misordered_tool_result_dropped(self):
        msgs = [
            {"role": "system", "content": "s"},
            _tool_result("call_0"),
            _assistant("call_0"),
        ]
        repaired, report = repair_tool_pairing(msgs)
        assert all(m.get("role") != "tool" for m in repaired)
        assert report.ok

    def test_repair_is_idempotent(self):
        msgs = _build_history(3)
        once, _ = repair_tool_pairing(msgs)
        twice, report = repair_tool_pairing(once)
        assert once == twice
        assert report.ok

    def test_repair_preserves_other_fields(self):
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
            ], "_model": "M3", "_provider": "minimax"},
            _tool_result("a"),
        ]
        repaired, _ = repair_tool_pairing(msgs)
        assert repaired[1]["_provider"] == "minimax"
        assert repaired[1]["_model"] == "M3"


# ── 与压缩器的组合（打穿原始 bug 的那组用例） ───────────────────────────────

class TestCompressorPairingInvariant:
    """压缩后的 payload 必须始终合法 —— 这是原始故障的直接回归测试。"""

    @pytest.mark.parametrize("rounds", [4, 5, 6, 7, 8, 9, 10])
    @pytest.mark.parametrize("tail_user", [True, False])
    def test_compress_output_is_always_valid(self, rounds, tail_user):
        from spirit.agent.context_compressor import ContextCompressor

        compressor = ContextCompressor(context_length=128_000, threshold_percent=0.75)
        original = _build_history(rounds, tail_user)
        compressed = compressor.compress(list(original), 0)

        report = validate_tool_pairing(compressed)
        assert report.ok, (
            f"rounds={rounds} tail_user={tail_user} 压缩后 payload 非法: {report.to_dict()}"
        )

    @pytest.mark.parametrize("rounds", [4, 6, 8, 10])
    def test_compress_output_pairs_with_live_api_shape(self, rounds):
        """压缩结果转成 API 格式后仍应合法（prepare_api_messages 的形状）。"""
        from spirit.agent.context_compressor import ContextCompressor
        from spirit.agent.tool_executor import prepare_api_messages

        compressor = ContextCompressor(context_length=128_000, threshold_percent=0.75)
        compressed = compressor.compress(list(_build_history(rounds)), 0)
        api_messages = prepare_api_messages(compressed)

        # API payload 里不能出现「tool 消息的 tool_call_id 没有对应 assistant」
        declared = set()
        for msg in api_messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    declared.add(tc.get("id"))
            elif msg.get("role") == "tool":
                assert msg.get("tool_call_id") in declared, (
                    f"孤儿 tool 结果: {msg.get('tool_call_id')}"
                )
